"""napari's Duplicate Layer on a live layer makes a frozen copy.

Before: the copy, ``Live: Camera copy``, sat on top of the live layer with its
additive blending (the frozen image was added onto the live one), and became
the selected layer, so the levels button and the Line Profile's *Active*
layer moved to it.
"""
import re

import numpy as np
import pytest
from napari.layers._layer_actions import _duplicate_layer
from qtpy import QtWidgets

from imswitch.imcontrol._test.unit._napari_model_viewer import (
    HeadlessImageWidget, layerNames,
)

pytestmark = pytest.mark.nohardware


@pytest.fixture
def widget(qapp):
    widget = HeadlessImageWidget()
    widget.setLiveViewLayers(['Camera', 'APD'])
    frame = np.random.default_rng(0).poisson(100, (64, 64)).astype(np.uint16)
    widget.setImage('Camera', frame, (0.1, 0.1))
    yield widget
    widget.close()


def _duplicate(widget, name='Camera'):
    viewer = widget.napariViewer
    live = widget.imgLayers[name]
    viewer.layers.selection.active = live
    _duplicate_layer(viewer.layers)
    for _ in range(3):
        QtWidgets.QApplication.processEvents()
    copies = [layer for layer in viewer.layers if '(copy ' in layer.name]
    return live, copies[-1]


def test_the_copy_is_named_hidden_translucent_and_under_its_live_layer(widget):
    viewer = widget.napariViewer
    live, copy = _duplicate(widget)
    assert re.fullmatch(r'Camera \(copy \d\d:\d\d:\d\d\)', copy.name)
    assert copy.visible is False
    assert copy.blending == 'translucent'
    assert not getattr(copy, 'protected', False)
    assert viewer.layers.index(copy) < viewer.layers.index(live)
    np.testing.assert_array_equal(copy.data, live.data)


def test_the_live_layer_stays_selected_and_the_live_source(widget):
    viewer = widget.napariViewer
    live, copy = _duplicate(widget)
    assert viewer.layers.selection.active is live
    assert widget.imgLayers['Camera'] is live
    targets = [label for label, _ in widget.getTransformRegistrationTargets()]
    assert 'Live source: Camera' in targets
    assert f'Layer: {copy.name}' in targets                 # a static layer, like a snap


def test_the_copy_stays_frozen_and_can_be_deleted(widget):
    viewer = widget.napariViewer
    live, copy = _duplicate(widget)
    frozen = np.array(copy.data, copy=True)
    widget.setImage('Camera', np.zeros((64, 64), np.uint16), (0.1, 0.1))
    np.testing.assert_array_equal(copy.data, frozen)
    viewer.layers.remove(copy)
    assert copy.name not in layerNames(viewer)


def test_a_copy_of_a_copy_or_of_another_layer_is_napari_s_own(widget):
    viewer = widget.napariViewer
    _live, copy = _duplicate(widget)
    viewer.layers.selection.active = copy
    _duplicate_layer(viewer.layers)
    assert f'{copy.name} copy' in layerNames(viewer)
    snap = viewer.add_image(np.zeros((4, 4)), name='snap')
    viewer.layers.selection.active = snap
    _duplicate_layer(viewer.layers)
    assert 'snap copy' in layerNames(viewer)
