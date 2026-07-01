"""Generic array-backed ProcessingResult implementation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import h5py
import numpy as np
import tifffile

from .result import ProcessingResult, ViewMode


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
        data = np.array(source.data, copy=copy_data)
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
        path = Path(path)
        data = np.asarray(self.data)
        if fmt in ("tiff", "tif"):
            tifffile.imwrite(str(path), data)
        elif fmt in ("hdf5", "h5", "hdf"):
            with h5py.File(str(path), "w") as h5:
                h5.create_dataset("data", data=data)
                h5.attrs["axis_labels"] = ",".join(self.axis_labels)
                h5.attrs["scale_unit"] = self.scale_unit
                for key, value in self.metadata.items():
                    if isinstance(value, (str, int, float, bool, np.number)):
                        h5.attrs[key] = value
        else:
            raise ValueError(f"ArrayProcessingResult supports TIFF or HDF5, got {fmt!r}")


__all__ = ["ArrayProcessingResult"]
