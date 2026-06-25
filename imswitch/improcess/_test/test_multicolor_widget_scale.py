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


def test_output_scale_maps_source_scale_onto_output_labels_by_name():
    layer = _FakeLayer(np.ones((3, 4, 5, 6)), scale=[1.0, 5.0, 2.0, 3.0])
    widget = _widget_with_layer(layer)
    out = widget._output_scale(layer, ["T", "Z", "Y", "X"], ["C", "Z", "Y", "X"])
    # T is dropped, C is new (1.0), Z/Y/X carry their source scale.
    assert out == [1.0, 5.0, 2.0, 3.0]


def test_output_scale_returns_none_for_unscaled_source():
    layer = _FakeLayer(np.ones((4, 5, 6)), scale=[1.0, 1.0, 1.0])
    widget = _widget_with_layer(layer)
    assert widget._output_scale(layer, ["Z", "Y", "X"], ["C", "Z", "Y", "X"]) is None
