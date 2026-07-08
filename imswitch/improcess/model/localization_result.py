"""Table-backed processing result for single-molecule localization data.

``LocalizationResult`` is the source of truth for the SMLM pipeline: it wraps
the canonical localization recarray (see
:mod:`~imswitch.improcess.model.localization_schema`) and everything else —
the in-house renderer, table processors, the napari-storm export — is a
derived view of it.

``ProcessingResult`` demands a viewable ``data`` array, but a coordinate table
has no natural image. Rather than leave the viewer blank, the result exposes a
**lazy low-res histogram** as its default ``data`` (question 2 in the port
plan): construction only measures the point-cloud extent, and the histogram is
filled the first time the viewer materialises the array. The real payload
stays the recarray on :attr:`locs`; the high-quality render is produced
separately by the Phase 3 render processor.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .localization_schema import (
    LOCALIZATION_COLUMNS,
    as_localizations,
    empty_localizations,
    to_napari_storm_recarray,
)
from .plotting import PlotPayload, PlotSeries
from .result import ProcessingResult, ViewMode

#: Target long-edge size (px) of the default preview histogram. Small on
#: purpose — it is a "where are the points" thumbnail, not the science render.
_PREVIEW_MAX_PX = 256


class _LazyHistogramPreview:
    """Deferred 2D histogram of a localization table used as preview ``data``.

    Behaves enough like an ndarray for the viewer (``ndim``/``shape``/``dtype``
    plus ``__array__``/``__getitem__``); the expensive ``histogram2d`` runs
    only on first materialisation and is then cached.
    """

    supports_lazy_indexing = False

    def __init__(
        self,
        locs: np.recarray,
        *,
        x_min_nm: float,
        y_min_nm: float,
        pixel_size_nm: float,
        shape: tuple[int, int],
    ):
        self._locs = locs
        self._x_min_nm = float(x_min_nm)
        self._y_min_nm = float(y_min_nm)
        self._pixel_size_nm = float(pixel_size_nm)
        self._shape = (int(shape[0]), int(shape[1]))
        self._cache: np.ndarray | None = None

    @property
    def shape(self) -> tuple[int, int]:
        return self._shape

    @property
    def ndim(self) -> int:
        return 2

    @property
    def dtype(self) -> np.dtype:
        return np.dtype(np.float32)

    def _materialize(self) -> np.ndarray:
        if self._cache is None:
            height, width = self._shape
            image = np.zeros((height, width), dtype=np.float32)
            if len(self._locs):
                col = (self._locs.x_nm - self._x_min_nm) / self._pixel_size_nm
                row = (self._locs.y_nm - self._y_min_nm) / self._pixel_size_nm
                col = np.clip(col.astype(np.intp), 0, width - 1)
                row = np.clip(row.astype(np.intp), 0, height - 1)
                np.add.at(image, (row, col), 1.0)
            self._cache = image
        return self._cache

    def __array__(self, dtype=None) -> np.ndarray:
        array = self._materialize()
        return np.asarray(array, dtype=dtype) if dtype is not None else array

    def __getitem__(self, key: Any) -> np.ndarray:
        return self._materialize()[key]

    def transpose(self, *axes: Any) -> "np.ndarray | _LazyHistogramPreview":
        """ndarray-compatible transpose.

        The viewer transposes every result's ``data`` by the active view
        mode's axis order; for the identity order the preview stays lazy,
        anything else falls back to the materialised array.
        """
        if len(axes) == 1 and isinstance(axes[0], (tuple, list)):
            axes = tuple(axes[0])
        if axes in ((), (0, 1)):
            return self
        return self._materialize().transpose(*axes)


class LocalizationResult(ProcessingResult):
    """A localization table presented as a first-class ImProcess result."""

    def __init__(
        self,
        name: str,
        locs: Any,
        *,
        pixel_size_nm: float,
        z_step_nm: float | None = None,
        dims: str | None = None,
        source_name: str | None = None,
        source_shape: tuple[int, int] | None = None,
        metadata: dict[str, Any] | None = None,
        preview_pixel_size_nm: float | None = None,
    ):
        """
        Args:
            name: Human-readable result name shown in the recon list.
            locs: Localization recarray (canonical schema) or a mapping/array
                coercible to it via :func:`as_localizations`.
            pixel_size_nm: Camera pixel size of the source stack, in nm. Used
                for the napari-storm pixel view and as a floor for the preview.
            z_step_nm: Axial sampling in nm for 3D data (optional).
            dims: "2D" or "3D". Auto-detected from z values when omitted.
            source_name: Name of the stack the localizations came from.
            source_shape: (height, width) of the source frames in px, used to
                anchor the preview extent when the point cloud is sparse.
            metadata: Free-form extra metadata (kept on save).
            preview_pixel_size_nm: Override the preview histogram bin size.
        """
        self._locs = as_localizations(locs)
        self.pixel_size_nm = float(pixel_size_nm)
        if self.pixel_size_nm <= 0:
            raise ValueError("pixel_size_nm must be positive")
        self.z_step_nm = float(z_step_nm) if z_step_nm else None
        self.dims = dims if dims is not None else self._infer_dims(self._locs)
        if self.dims not in ("2D", "3D"):
            raise ValueError(f"dims must be '2D' or '3D', got {self.dims!r}")
        self.source_name = source_name
        self.source_shape = tuple(source_shape) if source_shape is not None else None
        self.metadata = dict(metadata or {})

        preview, extent, bin_nm = self._build_preview(preview_pixel_size_nm)
        self.preview_extent_nm = extent
        self.preview_pixel_size_nm = bin_nm

        super().__init__(
            name=name,
            data=preview,
            axis_labels=["Y", "X"],
            view_modes=[ViewMode("Preview", (0, 1))],
            axis_scales=[bin_nm, bin_nm],
            scale_unit="nm",
        )

    # -- payload access ---------------------------------------------------

    @property
    def locs(self) -> np.recarray:
        """The canonical localization recarray — the real result payload."""
        return self._locs

    def __len__(self) -> int:
        return int(len(self._locs))

    @property
    def count(self) -> int:
        return int(len(self._locs))

    def to_recarray(self) -> np.recarray:
        """Return a copy of the canonical localization recarray."""
        return self._locs.copy()

    @classmethod
    def from_recarray(
        cls,
        name: str,
        locs: Any,
        *,
        pixel_size_nm: float,
        **kwargs: Any,
    ) -> "LocalizationResult":
        """Construct a result from a recarray/array coercible to the schema."""
        return cls(name, locs, pixel_size_nm=pixel_size_nm, **kwargs)

    def to_napari_storm_recarray(self) -> np.recarray:
        """Pixel-native view of the table for the napari-storm export."""
        return to_napari_storm_recarray(
            self._locs,
            self.pixel_size_nm,
            z_pixel_size_nm=self.z_step_nm,
        )

    def to_dataframe(self):
        """Return the localizations as a pandas DataFrame (lazy import)."""
        import pandas as pd

        return pd.DataFrame({name: self._locs[name] for name in LOCALIZATION_COLUMNS})

    # -- results-table projection ----------------------------------------

    def table_columns(self) -> list[str]:
        """Column order for the shared ResultsTableWidget."""
        columns = list(LOCALIZATION_COLUMNS)
        if self.dims == "2D":
            columns = [c for c in columns if c not in ("z_nm", "sigma_z_nm")]
        return columns

    def table_records(self) -> list[dict[str, Any]]:
        """One dict per localization, keyed by :meth:`table_columns`.

        Shaped for ``ResultsTableWidget.append_records`` (columns, records).
        """
        columns = self.table_columns()
        locs = self._locs
        return [
            {
                column: (
                    int(locs[column][i])
                    if column == "frame"
                    else float(locs[column][i])
                )
                for column in columns
            }
            for i in range(len(locs))
        ]

    def plot_payloads(self) -> list[PlotPayload]:
        if not len(self._locs):
            return []
        payloads = [
            PlotPayload(
                title="Photon count",
                x_label="Photons",
                y_label="Count",
                series=[
                    PlotSeries(
                        name="photons",
                        y=np.asarray(self._locs.photons, dtype=float),
                        kind="histogram",
                        style={"bins": 50},
                    )
                ],
            )
        ]
        return payloads

    # -- persistence ------------------------------------------------------

    def save(self, path: Path, fmt: str = "csv") -> None:
        path = Path(path)
        if fmt in ("csv",):
            self._save_csv(path)
        elif fmt in ("hdf5", "h5", "hdf"):
            self._save_hdf5(path)
        elif fmt in ("picasso", "napari-storm"):
            # Deferred import to avoid a model -> analysis import cycle.
            from imswitch.improcess.analysis.smlm_export import export_picasso_hdf5

            export_picasso_hdf5(self, path)
        else:
            raise ValueError(
                f"LocalizationResult supports CSV, HDF5, or picasso/napari-storm, "
                f"got {fmt!r}"
            )

    @classmethod
    def load(cls, path: Path, *, name: str | None = None) -> "LocalizationResult":
        """Load a result previously written by :meth:`save` (HDF5 only)."""
        import h5py

        path = Path(path)
        with h5py.File(str(path), "r") as h5:
            locs = as_localizations(np.asarray(h5["localizations"]))
            pixel_size_nm = float(h5.attrs.get("pixel_size_nm", 1.0))
            z_step_nm = h5.attrs.get("z_step_nm", None)
            dims = h5.attrs.get("dims", None)
            source_name = h5.attrs.get("source_name", None)
        return cls(
            name=name or (source_name or path.stem),
            locs=locs,
            pixel_size_nm=pixel_size_nm,
            z_step_nm=float(z_step_nm) if z_step_nm else None,
            dims=str(dims) if dims else None,
            source_name=str(source_name) if source_name else None,
        )

    def _save_csv(self, path: Path) -> None:
        columns = list(LOCALIZATION_COLUMNS)
        rows = np.column_stack([self._locs[name] for name in columns])
        header = ",".join(columns)
        np.savetxt(str(path), rows, delimiter=",", header=header, comments="")

    def _save_hdf5(self, path: Path) -> None:
        import h5py

        with h5py.File(str(path), "w") as h5:
            h5.create_dataset("localizations", data=self._locs, compression="gzip")
            h5.attrs["pixel_size_nm"] = self.pixel_size_nm
            h5.attrs["dims"] = self.dims
            if self.z_step_nm is not None:
                h5.attrs["z_step_nm"] = self.z_step_nm
            if self.source_name is not None:
                h5.attrs["source_name"] = str(self.source_name)
            for key, value in self.metadata.items():
                if isinstance(value, (str, int, float, bool, np.number)):
                    h5.attrs[key] = value

    # -- internals --------------------------------------------------------

    @staticmethod
    def _infer_dims(locs: np.recarray) -> str:
        if len(locs) and np.any(locs.z_nm != 0):
            return "3D"
        return "2D"

    def _build_preview(
        self, preview_pixel_size_nm: float | None
    ) -> tuple[_LazyHistogramPreview | np.ndarray, tuple[float, float, float, float], float]:
        locs = self._locs
        if not len(locs):
            # Nothing to show yet; a 1x1 blank keeps the viewer contract happy.
            return np.zeros((1, 1), dtype=np.float32), (0.0, 0.0, 0.0, 0.0), 1.0

        x_min = float(np.min(locs.x_nm))
        x_max = float(np.max(locs.x_nm))
        y_min = float(np.min(locs.y_nm))
        y_max = float(np.max(locs.y_nm))
        extent = (x_min, x_max, y_min, y_max)

        span_x = max(x_max - x_min, self.pixel_size_nm)
        span_y = max(y_max - y_min, self.pixel_size_nm)
        if preview_pixel_size_nm is not None and preview_pixel_size_nm > 0:
            bin_nm = float(preview_pixel_size_nm)
        else:
            bin_nm = max(span_x, span_y) / _PREVIEW_MAX_PX
            bin_nm = max(bin_nm, self.pixel_size_nm)

        width = max(1, int(np.ceil(span_x / bin_nm)))
        height = max(1, int(np.ceil(span_y / bin_nm)))
        preview = _LazyHistogramPreview(
            locs,
            x_min_nm=x_min,
            y_min_nm=y_min,
            pixel_size_nm=bin_nm,
            shape=(height, width),
        )
        return preview, extent, bin_nm


__all__ = ["LocalizationResult", "empty_localizations"]
