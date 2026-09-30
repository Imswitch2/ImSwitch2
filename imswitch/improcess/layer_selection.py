"""Active-layer selection — moved to ``imswitch.imcommon.algorithms.layer_selection``.

imcontrol's Line Profile panel shares the Profile widget with ImProcess and may
not import improcess, so the one definition of "what counts as an image
source" lives in the common layer. This module keeps the historical import path
working with the very same objects.
"""

from imswitch.imcommon.algorithms.layer_selection import (  # noqa: F401
    ANNOTATION_LAYER_NAMES,
    ROI_OVERLAY_LAYER_NAME,
    VIEWER_TOOL_POINTS_LAYER_NAME,
    VIEWER_TOOLS_LAYER_NAME,
    active_image_layer,
    is_image_layer,
)

__all__ = [
    "ANNOTATION_LAYER_NAMES",
    "ROI_OVERLAY_LAYER_NAME",
    "VIEWER_TOOL_POINTS_LAYER_NAME",
    "VIEWER_TOOLS_LAYER_NAME",
    "active_image_layer",
    "is_image_layer",
]
