"""Overlay/world-space scale handling in the MulticolorWidget.

Bead points and split-preview rectangles are produced in image pixel-index
space, but napari places overlay layers in world coordinates. The widget must
forward the active image layer's physical scale so the overlays line up with a
scaled reconstruction (regression for the multicolor counterpart of the
ProfileWidget world-space fix).

The widget is exercised without constructing its Qt tree (``object.__new__``)
so the test needs no QApplication or napari viewer.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

# Direct import to avoid the view package import chain (matplotlib/napari).
_view_path = Path(__file__).parent.parent / "view"
sys.path.insert(0, str(_view_path))
try:
    from MulticolorWidget import MulticolorWidget
finally:
    sys.path.pop(0)


class _FakeLayer:
    def __init__(self, data, scale, name="img"):
        self.data = data
        self.scale = scale
        self.name = name
        self.visible = True
        self.metadata = {}


class _FakeLayerList(list):
    def __init__(self, layers, active):
        super().__init__(layers)
        self.selection = SimpleNamespace(active=active)


class _FakeViewer:
    def __init__(self, layers, active):
        self.layers = _FakeLayerList(layers, active)


def _widget_with_layer(layer):
    widget = MulticolorWidget.__new__(MulticolorWidget)
    widget._viewer = _FakeViewer([layer], active=layer)
    return widget


def test_overlay_scale_uses_trailing_dims_of_layer_scale():
    layer = _FakeLayer(np.ones((2, 4, 5, 6)), scale=[1.0, 5.0, 2.0, 3.0])
    widget = _widget_with_layer(layer)
    # Rectangles are 2D (Y, X); points are 3D (Z, Y, X).
    assert widget._overlay_scale(2) == [2.0, 3.0]
    assert widget._overlay_scale(3) == [5.0, 2.0, 3.0]


def test_overlay_scale_falls_back_to_one_without_layer():
    widget = MulticolorWidget.__new__(MulticolorWidget)
    widget._viewer = _FakeViewer([], active=None)
    assert widget._overlay_scale(2) == [1.0, 1.0]
    assert widget._overlay_scale(3) == [1.0, 1.0, 1.0]


def test_overlay_scale_pads_when_scale_shorter_than_requested():
    layer = _FakeLayer(np.ones((4, 5, 6)), scale=[2.0, 3.0])  # malformed: too short
    widget = _widget_with_layer(layer)
    assert widget._overlay_scale(3) == [1.0, 1.0, 1.0]


def test_czyx_scales_inserts_color_axis_before_source_scales():
    source = SimpleNamespace(axis_scales=[5.0, 2.0, 3.0])
    # C is new (1.0), Z/Y/X carry their source scale.
    assert MulticolorWidget._czyx_scales(source) == [1.0, 5.0, 2.0, 3.0]


def test_czyx_scales_returns_none_when_source_scale_unusable():
    assert MulticolorWidget._czyx_scales(SimpleNamespace(axis_scales=[2.0])) is None
    assert MulticolorWidget._czyx_scales(SimpleNamespace(axis_scales=[])) is None
