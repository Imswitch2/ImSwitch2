"""Segmentation helpers for ImProcess.

The algorithm itself moved to the shared ``imcommon.algorithms.segmentation``
layer so imcontrol workflows and improcess can both use it without importing
each other. This module re-exports the public API for backward compatibility
with existing ``from imswitch.improcess.analysis.segmentation import ...`` call
sites; the ``SegmentationRegion.to_roi`` / ``SegmentationAnalysis.rois``
convenience methods (which build the shared ``ROIRecord``) are preserved.
"""

from __future__ import annotations

from imswitch.imcommon.algorithms.segmentation import (
    LabelMethod,
    SegmentationAnalysis,
    SegmentationRegion,
    ThresholdMethod,
    otsu_threshold,
    prepare_segmentation_image,
    segment_image,
)

__all__ = [
    "ThresholdMethod",
    "LabelMethod",
    "SegmentationRegion",
    "SegmentationAnalysis",
    "segment_image",
    "prepare_segmentation_image",
    "otsu_threshold",
]
