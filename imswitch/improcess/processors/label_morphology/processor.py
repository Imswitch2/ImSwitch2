"""Binary/label morphology (ImageJ's Process > Binary) for segmentation output.

Post-processes ``labels``-kind results: fill holes, erode/dilate, open/close
and watershed splitting of touching objects. Operations act on the binary
foreground (any nonzero label) and the result is relabelled, so a cleaned
mask gets fresh consecutive object ids.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.result import DisplayLayerSpec, ProcessingResult
from imswitch.improcess.processors._axis_split import (
    axis_labels_for_result,
    axis_scales_for_result,
    shape_for_result,
)
from imswitch.improcess.processors.base import Processor

MORPHOLOGY_OPERATIONS = (
    "fill-holes",
    "erode",
    "dilate",
    "open",
    "close",
    "watershed-split",
)


class LabelsResult(ArrayProcessingResult):
    """Label mask published by morphology; renders as a napari labels layer."""

    kind = "labels"

    def display_layers(self) -> list[DisplayLayerSpec]:
        return [
            DisplayLayerSpec(
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
        ]


class LabelMorphologyProcessor(Processor):
    """Morphological cleanup of a label mask."""

    name = "Label morphology"
    id = "label-morphology"
    category = "Segmentation"
    # Output is pixel-for-pixel aligned with the input, so an ROI drawn
    # on one measures the same features on the other.
    preserves_grid = True
    kinds = ("labels",)

    @classmethod
    def default_params(cls) -> dict:
        return {'operation': 'fill-holes', 'radius': 1}

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: len(shape_for_result(result)) == 2

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        operation_combo = QtWidgets.QComboBox()
        operation_combo.addItems(list(MORPHOLOGY_OPERATIONS))
        layout.addRow("Operation:", operation_combo)

        radius_spin = QtWidgets.QSpinBox()
        radius_spin.setRange(1, 100)
        radius_spin.setValue(1)
        radius_spin.setToolTip(
            "Structuring-element radius for erode/dilate/open/close; minimum "
            "object distance for watershed splitting"
        )
        layout.addRow("Radius:", radius_spin)

        def get_values():
            return {
                "operation": operation_combo.currentText(),
                "radius": int(radius_spin.value()),
            }

        widget.get_values = get_values
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        operation = str(params.get("operation", "fill-holes"))
        radius = int(params.get("radius", 1))
        labels = apply_morphology(np.asarray(result.data), operation, radius=radius)
        return LabelsResult(
            name=f"{result.name} ({operation})",
            data=labels,
            axis_labels=list(axis_labels_for_result(result)),
            display_levels=(0.0, float(max(1, labels.max()))),
            axis_scales=list(axis_scales_for_result(result)),
            scale_unit=getattr(result, "scale_unit", "px"),
            metadata={"operation": self.id, "morphology": operation, "radius": radius},
        )


def apply_morphology(
    labels: np.ndarray,
    operation: str,
    *,
    radius: int = 1,
) -> np.ndarray:
    """Apply one morphology operation to a 2D label mask and relabel."""
    from scipy import ndimage
    from skimage import measure, morphology, segmentation

    labels = np.asarray(labels)
    if labels.ndim != 2:
        raise ValueError("Label morphology needs a 2D label mask")
    if radius < 1:
        raise ValueError("Radius must be at least 1")
    mask = labels > 0
    footprint = morphology.disk(radius)

    if operation == "fill-holes":
        mask = ndimage.binary_fill_holes(mask)
    elif operation == "erode":
        mask = morphology.erosion(mask, footprint)
    elif operation == "dilate":
        mask = morphology.dilation(mask, footprint)
    elif operation == "open":
        mask = morphology.opening(mask, footprint)
    elif operation == "close":
        mask = morphology.closing(mask, footprint)
    elif operation == "watershed-split":
        # Classic distance-transform watershed: one marker per local distance
        # maximum, minimum separation set by the radius parameter.
        distance = ndimage.distance_transform_edt(mask)
        peaks = morphology.local_maxima(
            ndimage.gaussian_filter(distance, sigma=max(1.0, radius / 2.0))
        ) & mask
        markers = measure.label(morphology.dilation(peaks, morphology.disk(radius)))
        if markers.max() == 0:
            return measure.label(mask).astype(np.int32)
        return segmentation.watershed(
            -distance, markers=markers, mask=mask
        ).astype(np.int32)
    else:
        raise ValueError(
            f"Unknown morphology operation {operation!r}; "
            f"expected one of {list(MORPHOLOGY_OPERATIONS)}"
        )
    return measure.label(mask).astype(np.int32)


__all__ = [
    "LabelMorphologyProcessor",
    "LabelsResult",
    "MORPHOLOGY_OPERATIONS",
    "apply_morphology",
]
