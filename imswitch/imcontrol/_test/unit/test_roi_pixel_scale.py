"""Unit tests for VispyROIVisual pixel-scale handling.

The ROI keeps its position/size/bounds in DATA-PIXEL units, but the napari image
layer is drawn scaled by the detector pixel size. These tests verify the node
transforms convert pixel units to world units by the pixel scale, so the ROI
renders aligned to the image while bounds stay in pixels (what the crop uses).
"""
import numpy as np
import pytest

# Importing naparitools pulls in napari/vispy; skip cleanly if unavailable.
naparitools = pytest.importorskip(
    "imswitch.imcommon.view.guitools.naparitools"
)
VispyROIVisual = naparitools.VispyROIVisual


class _FakeTransform:
    def __init__(self):
        self.translate = [0, 0, 0, 0]
        self.scale = [1, 1, 1, 1]


class _FakeNode:
    def __init__(self):
        self.transform = _FakeTransform()


def _attached_roi():
    """A VispyROIVisual with fake render nodes, as if attached to a viewer."""
    roi = VispyROIVisual()
    roi.rect_node = _FakeNode()
    roi.handle_node = _FakeNode()
    roi._attached = True
    return roi


def test_default_scale_renders_one_to_one():
    roi = _attached_roi()
    roi.position = (10, 20)
    roi.size = (64, 64)
    # pixel scale 1: world == pixels, minus the half-pixel centring offset.
    assert roi.rect_node.transform.translate[:2] == [9.5, 19.5]
    assert roi.rect_node.transform.scale[:2] == [64, 64]


def test_bounds_are_in_pixel_units_regardless_of_scale():
    roi = _attached_roi()
    roi.position = (10, 20)
    roi.size = (64, 64)
    roi.setPixelScale((0.5, 2.0))
    # Bounds must stay in data pixels (what the raw-frame crop indexes).
    assert roi.bounds == (10, 20, 74, 84)


def test_pixel_scale_scales_the_render_transform():
    roi = _attached_roi()
    roi.position = (10, 20)
    roi.size = (64, 64)
    roi.setPixelScale((0.5, 2.0))  # (x, y) world units per pixel
    # translate = (position - 0.5) * scale ; size = size * scale
    assert roi.rect_node.transform.translate[:2] == [(10 - 0.5) * 0.5,
                                                     (20 - 0.5) * 2.0]
    assert roi.rect_node.transform.scale[:2] == [64 * 0.5, 64 * 2.0]


def test_invalid_pixel_scale_is_ignored():
    roi = _attached_roi()
    roi.setPixelScale((1.0, 1.0))
    for bad in [(0.0, 1.0), (-1.0, 2.0), (np.nan, 1.0), (1.0,), (1.0, 2.0, 3.0)]:
        roi.setPixelScale(bad)
        assert np.array_equal(roi._pixel_scale, np.array([1.0, 1.0]))


# --- VispyScatterVisual (the uLenses grid overlay) ---
# It was rendered in raw pixel coords with no setPixelScale, so the grid was
# mis-sized against the pixel-scaled image. It now scales like the ROI overlays.

VispyScatterVisual = naparitools.VispyScatterVisual


def test_scatter_pixel_scale_scales_node_transform():
    scatter = VispyScatterVisual()
    scatter.node = _FakeNode()
    scatter.setPixelScale((0.5, 2.0))  # (x, y) world units per pixel
    assert scatter.node.transform.scale == [0.5, 2.0, 1, 1]
    assert scatter._pixel_scale == (0.5, 2.0)


def test_scatter_invalid_pixel_scale_is_ignored():
    scatter = VispyScatterVisual()
    scatter.node = _FakeNode()
    scatter.setPixelScale((1.0, 1.0))
    for bad in [(0.0, 1.0), (-1.0, 2.0), (np.nan, 1.0), (1.0,), None]:
        scatter.setPixelScale(bad)
        assert scatter._pixel_scale == (1.0, 1.0)
        assert scatter.node.transform.scale == [1.0, 1.0, 1, 1]


def test_scatter_pixel_scale_stored_before_attach_applies_on_attach():
    # setPixelScale before the render node exists must be remembered and applied
    # when the node is created in attach() (overlays attach on demand).
    scatter = VispyScatterVisual()
    scatter.setPixelScale((3.0, 4.0))
    assert scatter._pixel_scale == (3.0, 4.0)
    # emulate attach() building the node and seeding it from _pixel_scale
    node = _FakeNode()
    node.transform.scale = [scatter._pixel_scale[0], scatter._pixel_scale[1], 1, 1]
    assert node.transform.scale == [3.0, 4.0, 1, 1]
