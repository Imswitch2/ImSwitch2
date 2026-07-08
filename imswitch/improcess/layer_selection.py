"""Shared active-layer selection for viewer-side analysis tools.

Interacting/measuring tools (ROI manager, Profile, ROI stats) and panel
previews (Segmentation, Multicolor) all need "the image the user is looking
at". This module is the one place that encodes what counts as an image
source, so the tools cannot drift apart again.

Kept dependency-light (numpy only) so widgets remain importable without the
view package's matplotlib/napari import chain.
"""

from __future__ import annotations

import numpy as np

VIEWER_TOOLS_LAYER_NAME = "Viewer Tools"


def is_image_layer(layer, *, min_ndim: int = 2, exclude_names=()) -> bool:
    """True for a visible, ndarray-backed napari layer usable as an image source.

    Hidden layers, underscore-prefixed helper layers and the shared
    "Viewer Tools" annotation layer are never image sources.
    """
    if layer is None or not hasattr(layer, "data"):
        return False
    name = str(getattr(layer, "name", ""))
    return (
        isinstance(layer.data, np.ndarray)
        and layer.data.ndim >= min_ndim
        and getattr(layer, "visible", True)
        and not name.startswith("_")
        and name != VIEWER_TOOLS_LAYER_NAME
        and name not in tuple(exclude_names)
    )


def active_image_layer(viewer, *, min_ndim: int = 2, exclude_names=()):
    """Return the active image-like layer, else the first valid one, else None.

    Aligned with ReconstructionView.getActiveImageLayer() semantics: prefer
    the viewer's active layer when it qualifies, otherwise scan the layer list.
    """
    try:
        active = viewer.layers.selection.active
    except Exception:
        active = None
    if is_image_layer(active, min_ndim=min_ndim, exclude_names=exclude_names):
        return active
    for layer in viewer.layers:
        if is_image_layer(layer, min_ndim=min_ndim, exclude_names=exclude_names):
            return layer
    return None
