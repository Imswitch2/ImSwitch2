"""Segmentation processing result."""

from pathlib import Path

import h5py
import numpy as np
import tifffile

from imswitch.improcess.analysis.segmentation import SegmentationAnalysis
from imswitch.improcess.model.contrast import finite_range
from imswitch.improcess.model.plotting import PlotPayload, PlotSeries
from imswitch.improcess.model.result import DisplayLayerSpec, ProcessingResult


class SegmentationResult(ProcessingResult):
    """Result wrapper for segmentation label images.

    The canonical output is the label mask, but a mask is only interpretable
    over the image it was computed from — so the result also carries the
    segmented source plane and renders as two owned display layers: a
    ``context`` image behind a ``primary`` labels overlay. Both belong to this
    one reconstruction-list entry.
    """

    kind = "labels"

    def __init__(
        self,
        name: str,
        analysis: SegmentationAnalysis,
        params: dict | None = None,
        axis_scales: list[float] | None = None,
        scale_unit: str = "px",
        source_image: np.ndarray | None = None,
    ):
        self.analysis = analysis
        self.params = params or {}
        self.source_image = (
            np.asarray(source_image) if source_image is not None else None
        )
        super().__init__(
            name=name,
            data=analysis.labels.astype(np.int32),
            axis_labels=["Y", "X"],
            display_levels=(0.0, float(max(1, len(analysis.regions)))),
            axis_scales=axis_scales,
            scale_unit=scale_unit,
        )

    def display_layers(self) -> list[DisplayLayerSpec]:
        labels_layer = DisplayLayerSpec(
            name=f"{self.name} labels",
            data=self.data,
            axis_labels=list(self.axis_labels),
            axis_scales=list(self.axis_scales),
            scale_unit=self.scale_unit,
            kind="labels",
            role="primary",
            component="labels",
            layer_kwargs={"opacity": 0.6},
        )
        if self.source_image is None:
            return [labels_layer]
        # Context image sits behind the mask; listed first so it anchors the
        # protected imgLayer, with the labels rendered as a managed overlay.
        context_layer = DisplayLayerSpec(
            name=f"{self.name} source",
            data=self.source_image,
            axis_labels=list(self.axis_labels),
            axis_scales=list(self.axis_scales),
            scale_unit=self.scale_unit,
            display_levels=finite_range(self.source_image),
            colormap="gray",
            kind="image",
            role="context",
            component="source",
        )
        return [context_layer, labels_layer]

    def save(self, path: Path, fmt: str = "hdf5") -> None:
        path = Path(path)
        if fmt in ("hdf5", "h5", "hdf"):
            with h5py.File(str(path), "w") as h5:
                h5.create_dataset("labels", data=self.analysis.labels)
                h5.create_dataset("mask", data=self.analysis.mask.astype(np.uint8))
                regions = h5.create_group("regions")
                regions.create_dataset(
                    "label",
                    data=np.array([region.label for region in self.analysis.regions], dtype=np.int32),
                )
                regions.create_dataset(
                    "area_pixels",
                    data=np.array(
                        [region.area_pixels for region in self.analysis.regions],
                        dtype=np.int64,
                    ),
                )
                regions.create_dataset(
                    "bounds",
                    data=np.array(
                        [region.bounds for region in self.analysis.regions],
                        dtype=np.int64,
                    ).reshape((-1, 4)),
                )
                regions.create_dataset(
                    "mean_intensity",
                    data=np.array(
                        [region.mean_intensity for region in self.analysis.regions],
                        dtype=np.float64,
                    ),
                )
                regions.create_dataset(
                    "max_intensity",
                    data=np.array(
                        [region.max_intensity for region in self.analysis.regions],
                        dtype=np.float64,
                    ),
                )
                h5.attrs["threshold"] = self.analysis.threshold
                h5.attrs["region_count"] = len(self.analysis.regions)
                h5.attrs["axis_scales"] = np.asarray(self.axis_scales, dtype=float)
                h5.attrs["scale_unit"] = self.scale_unit
                for key, value in self.analysis.metadata.items():
                    if isinstance(value, (str, int, float, bool)):
                        h5.attrs[key] = value
        elif fmt in ("tiff", "tif"):
            tifffile.imwrite(str(path), self.analysis.labels.astype(np.int32))
        else:
            raise ValueError(f"Segmentation result supports HDF5 or TIFF, got {fmt!r}")

    def plot_payloads(self) -> list[PlotPayload]:
        if not self.analysis.regions:
            return []
        labels = np.array([region.label for region in self.analysis.regions], dtype=np.float64)
        areas = np.array([region.area_pixels for region in self.analysis.regions], dtype=np.float64)
        return [
            PlotPayload(
                title=f"Segmentation regions: {len(self.analysis.regions)}",
                x_label="Region label",
                y_label="Area (px)",
                series=[
                    PlotSeries(
                        name="Area",
                        x=labels,
                        y=areas,
                        kind="bar",
                    )
                ],
                metadata=dict(self.analysis.metadata),
            )
        ]
