"""Composite display result for channel-like image stacks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import h5py
import numpy as np
import tifffile

from imswitch.improcess.model.contrast import finite_range
from imswitch.improcess.model.result import DisplayLayerSpec, ProcessingResult, ViewMode


class CompositeResult(ProcessingResult):
    """ProcessingResult that renders one display layer per channel."""

    kind = "composite"

    def __init__(
        self,
        name: str,
        data,
        axis_labels: list[str],
        *,
        channel_axis: int,
        channel_colormaps: list[str],
        axis_scales: list[float] | None = None,
        scale_unit: str = "px",
        params: dict[str, Any] | None = None,
    ):
        super().__init__(
            name=name,
            data=data,
            axis_labels=axis_labels,
            view_modes=[ViewMode("Composite", tuple(range(len(axis_labels))))],
            display_levels=None,
            axis_scales=axis_scales,
            scale_unit=scale_unit,
        )
        self.channel_axis = int(channel_axis)
        self.channel_colormaps = list(channel_colormaps)
        self.params = dict(params or {})

    def display_layers(self) -> list[DisplayLayerSpec]:
        layers = []
        channel_label = self.axis_labels[self.channel_axis]
        output_labels = [
            label for index, label in enumerate(self.axis_labels)
            if index != self.channel_axis
        ]
        output_scales = [
            scale for index, scale in enumerate(self.axis_scales)
            if index != self.channel_axis
        ]
        for channel_index in range(self.data.shape[self.channel_axis]):
            layer_data = np.take(self.data, channel_index, axis=self.channel_axis)
            layer_id = f"{channel_label}_{channel_index}"
            layers.append(
                DisplayLayerSpec(
                    name=f"{self.name} {layer_id}",
                    data=layer_data,
                    axis_labels=output_labels,
                    display_levels=finite_range(layer_data),
                    axis_scales=output_scales,
                    scale_unit=self.scale_unit,
                    colormap=self.channel_colormaps[
                        channel_index % len(self.channel_colormaps)
                    ],
                    metadata={
                        "source_result": self.name,
                        "component": layer_id,
                        "channel_axis": self.channel_axis,
                        "channel_axis_label": channel_label,
                        "channel_index": channel_index,
                    },
                )
            )
        return layers

    def save(self, path: Path, fmt: str = "tiff") -> None:
        path = Path(path)
        data = np.asarray(self.data)
        if fmt in ("tiff", "tif"):
            tifffile.imwrite(
                str(path),
                data,
                imagej=data.ndim <= 5,
                metadata={
                    "axes": "".join(self.axis_labels),
                    "mode": "composite",
                },
            )
        elif fmt in ("hdf5", "h5", "hdf"):
            with h5py.File(str(path), "w") as h5:
                h5.create_dataset("data", data=data)
                h5.attrs["axis_labels"] = ",".join(self.axis_labels)
                h5.attrs["scale_unit"] = self.scale_unit
                h5.attrs["display_mode"] = "composite"
                h5.attrs["channel_axis"] = self.channel_axis
                h5.attrs["channel_colormaps"] = ",".join(self.channel_colormaps)
        else:
            raise ValueError(f"Composite result supports TIFF or HDF5, got {fmt!r}")


__all__ = ["CompositeResult"]
