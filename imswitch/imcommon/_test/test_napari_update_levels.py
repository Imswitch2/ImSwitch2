"""The contrast widget must tolerate layers that are not images.

``NapariUpdateLevelsWidget`` iterates the whole selection, and the reconstruction
viewer can now hold a point-cloud layer alongside the image one. That layer
keeps a tuple of geometry arrays in ``data``, so asking it for ``.max()`` raised
``AttributeError: 'tuple' object has no attribute 'max'`` out of the button
handler and took the click with it.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("qtpy.QtWidgets")

from imswitch.imcommon.view.guitools.naparitools import (  # noqa: E402
    NapariUpdateLevelsWidget,
)


class _ImageLayer:
    def __init__(self, data):
        self.data = data
        self.contrast_limits = (0.0, 1.0)
        self.contrast_limits_range = (0.0, 1.0)


class _PointCloudLayer:
    """What napari-storm puts in the viewer: geometry, not an image."""

    def __init__(self):
        self.data = (np.zeros((4, 3)), np.ones((4, 3)), np.ones(4))


def _widget(layers):
    """The widget with only what these handlers touch."""
    instance = NapariUpdateLevelsWidget.__new__(NapariUpdateLevelsWidget)
    instance.viewer = SimpleNamespace(layers=SimpleNamespace(selection=layers))
    instance._minInput = SimpleNamespace(_text="", setText=lambda t: None, text=lambda: "")
    instance._maxInput = SimpleNamespace(_text="", setText=lambda t: None, text=lambda: "")
    return instance


def test_a_point_cloud_layer_is_skipped_rather_than_crashing():
    cloud = _PointCloudLayer()
    _widget([cloud])._on_update_levels()  # must not raise


def test_an_image_beside_a_point_cloud_still_gets_its_levels():
    image = _ImageLayer(np.arange(100, dtype=np.uint16).reshape(10, 10))
    _widget([_PointCloudLayer(), image])._on_update_levels()

    assert image.contrast_limits == (0, 101)
    assert image.contrast_limits_range == (0, 101)


def test_applying_a_manual_range_also_skips_non_image_layers():
    image = _ImageLayer(np.zeros((4, 4)))
    widget = _widget([_PointCloudLayer(), image])
    widget._minInput = SimpleNamespace(text=lambda: "5")
    widget._maxInput = SimpleNamespace(text=lambda: "42")

    widget._on_apply_range()  # must not raise

    assert image.contrast_limits == (5.0, 42.0)


def test_an_empty_image_layer_is_skipped():
    """min/max over nothing has no answer; skipping beats an exception."""
    empty = _ImageLayer(np.zeros((0, 0)))
    _widget([empty])._on_update_levels()

    assert empty.contrast_limits == (0.0, 1.0)
