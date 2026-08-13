"""Generic array-backed ProcessingResult implementation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .lazy_array import identity_lazy_view
from .result import ProcessingResult, ViewMode
from .result_io import save_image_result


class ArrayProcessingResult(ProcessingResult):
    """Small generic result for toolbar-created image arrays."""

    def __init__(
        self,
        name: str,
        data: np.ndarray | Any,
        axis_labels: list[str],
        *,
        view_modes: list[ViewMode] | None = None,
        display_levels: tuple[float, float] | None = None,
        axis_scales: list[float] | None = None,
        scale_unit: str = "px",
        metadata: dict[str, Any] | None = None,
    ):
        super().__init__(
            name=name,
            data=data,
            axis_labels=axis_labels,
            view_modes=view_modes,
            display_levels=display_levels,
            axis_scales=axis_scales,
            scale_unit=scale_unit,
        )
        self.metadata = dict(metadata or {})

    @classmethod
    def duplicate(cls, source: ProcessingResult, *, copy_data: bool = True) -> "ArrayProcessingResult":
        source_data = source.data
        if isinstance(source_data, np.ndarray):
            data = np.array(source_data, copy=copy_data)
        else:
            # Lazy/virtual source: don't eagerly materialize the whole
            # array. Hand out a deferred full-range view instead; reads
            # happen only when the duplicate is actually displayed/saved.
            data = identity_lazy_view(source_data, source_shape=source_data.shape)
        duplicate = cls(
            name=f"{source.name} (duplicate)",
            data=data,
            axis_labels=list(source.axis_labels),
            view_modes=list(source.view_modes),
            display_levels=source.display_levels,
            axis_scales=list(source.axis_scales),
            scale_unit=source.scale_unit,
            metadata={"source_result": source.name, "operation": "duplicate"},
        )
        if hasattr(source, "getDisplayColormap"):
            duplicate.setDisplayColormap(source.getDisplayColormap())
        return duplicate

    def save(self, path: Path, fmt: str = "tiff") -> None:
        """Written through the shared image writer, which carries the
        calibration, the metadata and the processing footprint into whichever
        container the filename asks for."""
        save_image_result(self, path, fmt)


__all__ = ["ArrayProcessingResult"]
