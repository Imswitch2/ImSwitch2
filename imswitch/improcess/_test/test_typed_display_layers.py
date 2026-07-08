"""Phase 1 result-unification: typed/owned display-layer rendering.

Verifies that ReconstructionView.setDisplayLayers dispatches on DisplayLayerSpec
``kind`` (image/labels/points/shapes), reuses the protected imgLayer only for
the first image-kind spec, hides it when the primary is non-image, and tracks
the primary component. Uses a fake napari viewer so no real napari/Qt is needed.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import numpy as np

from imswitch.improcess.model.result import DisplayLayerSpec
from imswitch.improcess.view.ReconstructionView import ReconstructionView


class _FakeLayer:
    def __init__(self, data, name="x", kind="image", protected=False):
        self.data = np.asarray(data)
        self.name = name
        self.kind = kind
        self.protected = protected
        self.visible = True
        self.metadata = {}
        self.scale = (1.0,) * self.data.ndim
        self.colormap = "gray"
        self.contrast_limits = (0.0, 1.0)
        self.contrast_limits_range = (0.0, 1.0)
        self.rgb = False


class _FakeLayers(list):
    def __init__(self):
        super().__init__()
        self.selection = SimpleNamespace(active=None)

    def remove(self, layer):
        if layer in self:
            super().remove(layer)


class _FakeViewer:
    def __init__(self):
        self.layers = _FakeLayers()
        self.dims = SimpleNamespace(axis_labels=(), events=SimpleNamespace(connect=lambda *a: None))
        self.scale_bar = SimpleNamespace(unit="px")

    def _add(self, data, kind, kwargs):
        layer = _FakeLayer(data, kwargs.get("name", kind), kind, kwargs.get("protected", False))
        layer.metadata = kwargs.get("metadata", {}) or {}
        if "colormap" in kwargs:
            layer.colormap = kwargs["colormap"]
        self.layers.append(layer)
        return layer

    def add_image(self, data, **kwargs):
        return self._add(data, "image", kwargs)

    def add_labels(self, data, **kwargs):
        return self._add(data, "labels", kwargs)

    def add_points(self, data, **kwargs):
        return self._add(data, "points", kwargs)

    def add_shapes(self, data, **kwargs):
        return self._add(data, "shapes", kwargs)

    def reset_view(self):
        pass


def _view():
    rv = ReconstructionView.__new__(ReconstructionView)
    rv._logger = logging.getLogger("test")
    rv.napariViewer = _FakeViewer()
    rv.imgLayer = rv.napariViewer.add_image(
        np.zeros((1, 1)), name="Reconstruction", protected=True
    )
    rv._displayLayers = []
    rv._primaryLayer = rv.imgLayer
    rv._primaryComponent = None
    rv._patchLayerForNdimChange = lambda *a, **k: None
    return rv


def _image_spec(data, **kw):
    return DisplayLayerSpec(name=kw.pop("name", "img"), data=data, axis_labels=["Y", "X"], **kw)


def test_labels_over_context_reuses_imglayer_for_image_and_adds_labels_layer():
    rv = _view()
    src = np.arange(16, dtype=np.float32).reshape(4, 4)
    mask = (src > 7).astype(np.int32)

    rv.setDisplayLayers([
        _image_spec(src, name="source", role="context", component="source"),
        DisplayLayerSpec(name="mask", data=mask, axis_labels=["Y", "X"],
                         kind="labels", role="primary", component="labels"),
    ])

    # imgLayer holds the context image; the labels are a separate managed layer.
    np.testing.assert_array_equal(rv.imgLayer.data, src)
    assert rv.imgLayer.visible
    labels = [l for l in rv.napariViewer.layers if l.kind == "labels"]
    assert len(labels) == 1
    assert rv._primaryComponent == "labels"
    assert rv._primaryLayer is labels[0]


def test_non_image_primary_hides_the_protected_imglayer():
    rv = _view()
    mask = np.eye(5, dtype=np.int32)

    rv.setDisplayLayers([
        DisplayLayerSpec(name="mask", data=mask, axis_labels=["Y", "X"],
                         kind="labels", role="primary", component="labels"),
    ])

    assert rv.imgLayer.visible is False  # can't remove it (protected) -> hide
    labels = [l for l in rv.napariViewer.layers if l.kind == "labels"]
    assert len(labels) == 1
    assert rv._primaryLayer is labels[0]


def test_plain_image_after_labels_restores_imglayer_and_clears_managed():
    rv = _view()
    rv.setDisplayLayers([
        DisplayLayerSpec(name="mask", data=np.eye(5, dtype=np.int32), axis_labels=["Y", "X"],
                         kind="labels", role="primary", component="labels"),
    ])
    assert rv.imgLayer.visible is False

    rv.setImage(np.ones((6, 6), dtype=np.float32), ["Y", "X"], name="plain")

    assert rv.imgLayer.visible is True
    assert rv.imgLayer.name == "plain"
    assert rv._primaryLayer is rv.imgLayer
    assert not any(getattr(l, "kind", "image") == "labels" for l in rv._displayLayers)


def test_multi_image_spec_reuses_imglayer_for_first_only():
    rv = _view()
    a = np.ones((4, 4), dtype=np.float32)
    b = np.zeros((4, 4), dtype=np.float32)

    rv.setDisplayLayers([
        _image_spec(a, name="chan0", component="c0"),
        _image_spec(b, name="chan1", component="c1"),
    ])

    assert rv.imgLayer.data.shape == (4, 4)
    assert rv.imgLayer.name == "chan0"
    managed_images = [l for l in rv._displayLayers if l.kind == "image"]
    assert len(managed_images) == 1
    assert managed_images[0].name == "chan1"


def test_labels_layer_carries_component_metadata_for_persistence():
    rv = _view()
    rv.setDisplayLayers([
        DisplayLayerSpec(name="mask", data=np.eye(3, dtype=np.int32), axis_labels=["Y", "X"],
                         kind="labels", role="primary", component="labels"),
    ])
    labels = [l for l in rv.napariViewer.layers if l.kind == "labels"][0]
    assert labels.metadata.get("component") == "labels"
