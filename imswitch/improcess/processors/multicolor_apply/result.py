"""Result wrapper for multicolor-aligned sample data."""

from pathlib import Path

import h5py
import numpy as np
import tifffile

from imswitch.improcess.analysis.multicolor import alignment_summary
from imswitch.improcess.model.result import ProcessingResult, ViewMode
from imswitch.improcess.model.result_io import save_image_result


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
        save_image_result(self, path, fmt, extra={"summary": self.summary})
