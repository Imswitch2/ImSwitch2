"""Shared active-image-layer selection used by all viewer-side analysis tools."""

from types import SimpleNamespace

import numpy as np

from imswitch.improcess.layer_selection import active_image_layer, is_image_layer


class _FakeLayer:
    def __init__(self, data, name="image", visible=True):
        self.data = data
        self.name = name
        self.visible = visible


class _FakeLayerList(list):
    def __init__(self, layers, active):
        super().__init__(layers)
        self.selection = SimpleNamespace(active=active)


class _FakeViewer:
    def __init__(self, layers, active=None):
        self.layers = _FakeLayerList(layers, active)


def _image(name="image", ndim=2, visible=True):
    return _FakeLayer(np.zeros((4,) * ndim), name=name, visible=visible)


def test_prefers_active_layer_when_image_like():
    first = _image("first")
    active = _image("active")
    viewer = _FakeViewer([first, active], active=active)

    assert active_image_layer(viewer) is active


def test_falls_back_to_first_valid_layer():
    valid = _image("valid")
    tools = _FakeLayer(np.zeros((3, 2)), name="Viewer Tools")
    viewer = _FakeViewer([tools, valid], active=tools)

    assert active_image_layer(viewer) is valid


def test_returns_none_without_valid_layers():
    hidden = _image("hidden", visible=False)
    helper = _image("_helper")
    viewer = _FakeViewer([hidden, helper], active=None)

    assert active_image_layer(viewer) is None


def test_min_ndim_filters_flat_layers():
    plane = _image("plane", ndim=2)
    volume = _image("volume", ndim=3)
    viewer = _FakeViewer([plane, volume], active=plane)

    assert active_image_layer(viewer, min_ndim=3) is volume


def test_exclude_names_skips_named_layers():
    preview = _image("Segmentation preview")
    source = _image("source")
    viewer = _FakeViewer([preview, source], active=preview)

    layer = active_image_layer(viewer, exclude_names=("Segmentation preview",))

    assert layer is source


def test_viewer_without_selection_attribute_is_tolerated():
    valid = _image("valid")
    viewer = SimpleNamespace(layers=[valid])

    assert active_image_layer(viewer) is valid


def test_is_image_layer_rejects_non_arrays_and_viewer_tools():
    assert not is_image_layer(None)
    assert not is_image_layer(_FakeLayer([[1, 2], [3, 4]]))
    assert not is_image_layer(_FakeLayer(np.zeros(4)))
    assert not is_image_layer(_FakeLayer(np.zeros((3, 3)), name="Viewer Tools"))
    assert is_image_layer(_FakeLayer(np.zeros((3, 3))))
