"""Result wrapper for multicolor registration calibration."""

from pathlib import Path

import h5py
import numpy as np
import tifffile

from imswitch.improcess.analysis.multicolor import alignment_summary, save_alignment
from imswitch.improcess.model.result import ProcessingResult, ViewMode


class MulticolorRegistrationResult(ProcessingResult):
    """Aligned bead preview plus the extracted registration transform."""

    def __init__(
        self,
        name: str,
        data: np.ndarray,
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
        super().__init__(
            name=name,
            data=data,
            axis_labels=["C", "Z", "Y", "X"],
            view_modes=[
                ViewMode("CZYX", (0, 1, 2, 3)),
                ViewMode("ZCYX", (1, 0, 2, 3)),
            ],
            display_levels=display_levels,
            axis_scales=axis_scales or [1.0, 1.0, 1.0, 1.0],
            scale_unit=scale_unit,
        )

    def save(self, path: Path, fmt: str = "hdf5") -> None:
        path = Path(path)
        if fmt in ("hdf5", "h5", "hdf"):
            save_alignment(self.alignment, path)
            with h5py.File(str(path), "a") as h5:
                h5.create_dataset("aligned_preview", data=np.asarray(self.data), compression="gzip")
                h5.attrs["summary"] = self.summary
                h5.attrs["axis_labels"] = ",".join(self.axis_labels)
                h5.attrs["scale_unit"] = self.scale_unit
                for key, value in self.params.items():
                    if value is not None:
                        h5.attrs[f"param_{key}"] = value
        elif fmt in ("tiff", "tif"):
            tifffile.imwrite(
                str(path),
                np.asarray(self.data, dtype=np.float32),
                imagej=True,
                metadata={"axes": "CZYX", "summary": self.summary},
                photometric="minisblack",
            )
        else:
            raise ValueError(f"Multicolor registration supports HDF5 or TIFF, got {fmt!r}")
