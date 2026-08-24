"""RGB visualization result."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import h5py
import numpy as np
import tifffile

from imswitch.improcess.model.result import DisplayLayerSpec, ProcessingResult, ViewMode
from imswitch.improcess.model.result_io import save_image_result


class RGBResult(ProcessingResult):
    """Channel-last RGB image or stack for display/export."""

    kind = "rgb"

    def __init__(
        self,
        name: str,
        data: np.ndarray,
        axis_labels: list[str],
        *,
        source_result: str,
        source_channel_axis: str,
        axis_scales: list[float] | None = None,
        scale_unit: str = "px",
        params: dict[str, Any] | None = None,
    ):
        super().__init__(
            name=name,
            data=np.asarray(data, dtype=np.uint8),
            axis_labels=axis_labels,
            view_modes=[ViewMode("RGB", tuple(range(len(axis_labels))))],
            display_levels=(0.0, 255.0),
            axis_scales=axis_scales,
            scale_unit=scale_unit,
        )
        self.source_result = source_result
        self.source_channel_axis = source_channel_axis
        self.params = dict(params or {})

    def display_layers(self) -> list[DisplayLayerSpec]:
        return [
            DisplayLayerSpec(
                name=self.name,
                data=self.data,
                axis_labels=list(self.axis_labels[:-1]),
                display_levels=(0.0, 255.0),
                axis_scales=list(self.axis_scales[:-1]),
                scale_unit=self.scale_unit,
                colormap="grayclip",
                rgb=True,
                metadata={
                    "source_result": self.name,
                    "component": "rgb",
                    "rgb": True,
                    "source_channel_axis": self.source_channel_axis,
                },
            )
        ]

    def save(self, path: Path, fmt: str = "tiff") -> None:
        save_image_result(self, path, fmt, extra={
            "source_result": self.source_result,
            "source_channel_axis": self.source_channel_axis,
        })


__all__ = ["RGBResult"]
