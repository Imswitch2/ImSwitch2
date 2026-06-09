"""WidefieldSTARSS ImProcess result."""

from __future__ import annotations

from pathlib import Path

import h5py
import numpy as np
import tifffile as tiff

from imswitch.improcess.model.plotting import PlotPayload, PlotSeries
from imswitch.improcess.model.result import ProcessingResult, ViewMode

from .analysis import WidefieldStarssAnalysis


class WidefieldStarssResult(ProcessingResult):
    """Display and save output from one WidefieldSTARSS H/V analysis."""

    def __init__(self, name: str, analysis: WidefieldStarssAnalysis, params: dict):
        self.analysis = analysis
        self.params = params

        data = np.stack(
            [
                analysis.anis_maps.r_smooth.astype(np.float32),
                analysis.anis_maps.r_raw.astype(np.float32),
                analysis.mask.astype(np.float32),
                analysis.base_image.astype(np.float32),
            ],
            axis=0,
        )
        finite = data[np.isfinite(data)]
        display_levels = None
        if finite.size:
            display_levels = (float(np.nanpercentile(finite, 1)), float(np.nanpercentile(finite, 99)))

        super().__init__(
            name=name,
            data=data,
            axis_labels=["C", "Y", "X"],
            view_modes=[ViewMode("Maps", (0, 1, 2))],
            display_levels=display_levels,
        )

    def save(self, path: Path, fmt: str = "tiff") -> None:
        path = Path(path)
        if fmt in ("tiff", "tif"):
            self._save_tiff(path)
        elif fmt in ("hdf5", "h5", "hdf"):
            self._save_hdf5(path)
        else:
            raise ValueError(f"WidefieldSTARSS result supports TIFF or HDF5, got {fmt!r}")

    def plot_payloads(self) -> list[PlotPayload]:
        regions = self.analysis.regions
        payloads = [
            PlotPayload(
                title="Anisotropy histogram",
                x_label="r smooth",
                y_label="Count",
                series=[
                    PlotSeries(
                        name="r smooth",
                        y=self.analysis.anis_maps.r_smooth.ravel(),
                        kind="histogram",
                        style={"bins": 50},
                    )
                ],
            )
        ]

        if (
            "area_superpixels" in regions.columns
            and "anisotropy_direct" in regions.columns
            and len(regions) > 0
        ):
            payloads.append(
                PlotPayload(
                    title="Region anisotropy",
                    x_label="Area (superpixels)",
                    y_label="Anisotropy",
                    series=[
                        PlotSeries(
                            name="regions",
                            x=regions["area_superpixels"].to_numpy(dtype=float),
                            y=regions["anisotropy_direct"].to_numpy(dtype=float),
                            kind="scatter",
                        )
                    ],
                )
            )
        return payloads

    def _save_tiff(self, path: Path) -> None:
        tiff.imwrite(
            str(path),
            self.data.astype(np.float32),
            imagej=True,
            metadata={"axes": "CYX", "Labels": ["r_smooth", "r_raw", "mask", "base_image"]},
            photometric="minisblack",
        )
        self.analysis.regions.to_csv(path.with_suffix(".regions.csv"), index=False)

    def _save_hdf5(self, path: Path) -> None:
        with h5py.File(str(path), "w") as f:
            f.create_dataset("r_smooth", data=self.analysis.anis_maps.r_smooth, compression="gzip")
            f.create_dataset("r_raw", data=self.analysis.anis_maps.r_raw, compression="gzip")
            f.create_dataset("r_raw_se", data=self.analysis.anis_maps.r_raw_se, compression="gzip")
            f.create_dataset("r_smooth_se", data=self.analysis.anis_maps.r_smooth_se, compression="gzip")
            f.create_dataset("valid_mask", data=self.analysis.anis_maps.valid_mask, compression="gzip")
            f.create_dataset("mask", data=self.analysis.mask.astype(np.int32), compression="gzip")
            f.create_dataset("base_image", data=self.analysis.base_image.astype(np.float32), compression="gzip")
            for key in ("ihh", "ihv", "ivh", "ivv"):
                f.create_dataset(key, data=getattr(self.analysis.anis_maps, key), compression="gzip")
            f.attrs["anisotropy_mode"] = self.analysis.anis_maps.anisotropy_mode

            region_group = f.create_group("regions")
            for column in self.analysis.regions.columns:
                values = self.analysis.regions[column].to_numpy()
                if values.dtype.kind in ("O", "U", "S"):
                    values = np.asarray(values, dtype=h5py.string_dtype(encoding="utf-8"))
                region_group.create_dataset(column, data=values)

            for key, value in self.params.items():
                if value is None:
                    continue
                f.attrs[key] = value
