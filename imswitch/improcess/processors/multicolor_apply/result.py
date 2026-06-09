"""Result wrapper for multicolor-aligned sample data."""

from pathlib import Path

import h5py
import numpy as np
import tifffile

from imswitch.improcess.analysis.multicolor import alignment_summary
from imswitch.improcess.model.result import ProcessingResult, ViewMode


class MulticolorApplyResult(ProcessingResult):
    """Aligned three-color volume or time series."""

    def __init__(
        self,
        name: str,
        data: np.ndarray,
        axis_labels: list[str],
        alignment: dict,
        params: dict | None = None,
        axis_scales: list[float] | None = None,
        scale_unit: str = "px",
    ):
        self.alignment = alignment
        self.params = params or {}
        self.summary = alignment_summary(alignment)
        finite = data[np.isfinite(data)]
        display_levels = (
            (float(np.nanmin(finite)), float(np.nanmax(finite)))
            if finite.size
            else None
        )
        if axis_labels == ["C", "Z", "Y", "X"]:
            view_modes = [
                ViewMode("CZYX", (0, 1, 2, 3)),
                ViewMode("ZCYX", (1, 0, 2, 3)),
            ]
        else:
            view_modes = [
                ViewMode("TCZYX", (0, 1, 2, 3, 4)),
                ViewMode("TZCYX", (0, 2, 1, 3, 4)),
            ]
        super().__init__(
            name=name,
            data=data,
            axis_labels=axis_labels,
            view_modes=view_modes,
            display_levels=display_levels,
            axis_scales=axis_scales,
            scale_unit=scale_unit,
        )

    def save(self, path: Path, fmt: str = "tiff") -> None:
        path = Path(path)
        if fmt in ("tiff", "tif"):
            axes = "".join(self.axis_labels)
            tifffile.imwrite(
                str(path),
                np.asarray(self.data, dtype=np.float32),
                imagej=len(axes) <= 5,
                metadata={"axes": axes, "summary": self.summary},
                photometric="minisblack",
            )
        elif fmt in ("hdf5", "h5", "hdf"):
            with h5py.File(str(path), "w") as h5:
                h5.create_dataset("aligned", data=np.asarray(self.data), compression="gzip")
                h5.attrs["summary"] = self.summary
                h5.attrs["axis_labels"] = ",".join(self.axis_labels)
                h5.attrs["scale_unit"] = self.scale_unit
                h5.attrs["alignment_mode"] = self.alignment["mode"]
                h5.attrs["alignment_x_bounds"] = np.asarray(
                    self.alignment["x_bounds"],
                    dtype=np.int64,
                )
                for key, value in self.params.items():
                    if value is not None:
                        h5.attrs[f"param_{key}"] = value
        else:
            raise ValueError(f"Multicolor apply supports TIFF or HDF5, got {fmt!r}")
