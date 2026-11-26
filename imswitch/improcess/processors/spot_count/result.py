"""One result for a spot search: the points, the image with the spots drawn on it, the counts.

The table (and ``coordinates``) are the spots -- or, for a tiling run, the
ranked tiles with their stage targets -- so a later step can pick from them.
The viewer shows a copy of the searched image with a ring on every spot
(``display_layers``), and the Graph panel the count per frame or per tile
(``plot_payloads``). With no spot found the image is shown alone: an empty
points layer is never handed to the viewer.
"""

from __future__ import annotations

import numpy as np

from imswitch.improcess.model.contrast import finite_range
from imswitch.improcess.model.plotting import PlotPayload, PlotSeries
from imswitch.improcess.model.points_result import UNKNOWN_FRAME, PointsResult
from imswitch.improcess.model.result import DisplayLayerSpec

SPOT_COLOR = "#ffd400"
TARGET_COLOR = "#00e5ff"


class SpotResult(PointsResult):
    """Points plus the image they were found on and a count graph."""

    def __init__(
        self,
        name: str,
        coordinates,
        *,
        image: np.ndarray,
        image_axis_labels: list[str],
        image_axis_scales: list[float] | None = None,
        image_scale_unit: str = "px",
        overlay: np.ndarray | None = None,
        marker_size_px: float = 7.0,
        targets: np.ndarray | None = None,
        target_labels: list[str] | None = None,
        counts: np.ndarray | None = None,
        counts_x: np.ndarray | None = None,
        counts_title: str = "Spots",
        counts_x_label: str = "Frame",
        frame: str = UNKNOWN_FRAME,
        axis_names: list[str] | None = None,
        properties: dict | None = None,
        coordinate_scale: list[float] | None = None,
        scale_unit: str = "px",
        metadata: dict | None = None,
    ):
        super().__init__(
            name, coordinates, frame=frame, axis_names=axis_names, properties=properties,
            coordinate_scale=coordinate_scale, scale_unit=scale_unit, metadata=metadata,
        )
        self.image = image
        self.image_axis_labels = list(image_axis_labels)
        self.image_axis_scales = list(image_axis_scales or [1.0] * np.ndim(image))
        self.image_scale_unit = image_scale_unit
        self.overlay = _rows(overlay)
        self.marker_size_px = float(marker_size_px)
        self.targets = _rows(targets)
        self.target_labels = list(target_labels or [])
        self.counts = None if counts is None else np.asarray(counts, dtype=np.float64)
        self.counts_x = None if counts_x is None else np.asarray(counts_x, dtype=np.float64)
        self.counts_title = counts_title
        self.counts_x_label = counts_x_label

    # -- viewer ----------------------------------------------------------------------------
    def display_layers(self) -> list[DisplayLayerSpec]:
        image = np.asarray(self.image)
        if image.ndim < 2 or image.size == 0:
            return []
        labels, scales, unit = self.image_axis_labels, self.image_axis_scales, self.image_scale_unit
        layers = [DisplayLayerSpec(
            name=f"{self.name} image", data=image, axis_labels=labels, axis_scales=scales,
            scale_unit=unit, display_levels=finite_range(image), colormap="gray",
            kind="image", role="context", component="image",
        )]
        size = max(6.0, 2.0 * self.marker_size_px)
        for data, component, color, texts in (
            (self.overlay, "spots", SPOT_COLOR, None),
            (self.targets, "targets", TARGET_COLOR, self.target_labels),
        ):
            if len(data) == 0:
                continue  # napari cannot draw an empty points layer with styling
            dims = data.shape[1]
            kwargs = {
                "size": size if component == "spots" else 2 * size,
                "face_color": "transparent",
                "border_color": color,
                "border_width": 0.12,
                "border_width_is_relative": True,
                "opacity": 0.9,
            }
            if texts and len(texts) == len(data):
                kwargs["features"] = {"label": list(texts)}
                kwargs["text"] = {"string": "{label}", "size": 9, "color": color,
                                  "anchor": "upper_left"}
            layers.append(DisplayLayerSpec(
                name=f"{self.name} {component}", data=data, axis_labels=labels[-dims:],
                axis_scales=scales[-dims:], scale_unit=unit, kind="points",
                role="primary" if component == "spots" else "overlay",
                component=component, layer_kwargs=kwargs,
            ))
        return layers

    def display_layer_data(self) -> list[np.ndarray]:
        return [np.asarray(layer.data) for layer in self.display_layers()]

    # -- graph -----------------------------------------------------------------------------
    def plot_payloads(self) -> list[PlotPayload]:
        if self.counts is None or self.counts.size == 0:
            return []
        x = self.counts_x if self.counts_x is not None else np.arange(self.counts.size, dtype=np.float64)
        kind = "line" if self.counts.size > 1 else "scatter"
        return [PlotPayload(
            title=self.counts_title, x_label=self.counts_x_label, y_label="Spots",
            series=[PlotSeries(name="spots", x=x, y=self.counts, kind=kind)],
            metadata={"total_spots": int(self.counts.sum())},
        )]


def _rows(points) -> np.ndarray:
    """``points`` as a float ``(N, D)`` array; ``(0, 2)`` when there are none."""
    if points is None:
        return np.empty((0, 2))
    array = np.asarray(points, dtype=np.float64)
    if array.size == 0:
        return np.empty((0, array.shape[1] if array.ndim == 2 else 2))
    return array.reshape(len(array), -1)


__all__ = ["SpotResult"]
