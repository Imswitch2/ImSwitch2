"""A sum projection in the live view (scan timing plan, S6): a Sum checkbox
per dimension of a live layer beyond its image -- line steps, z, t -- that
sums the view over it. Display only: the frame and the recording are
untouched; the levels follow what is shown."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pytest

from imswitch.imcommon.view.guitools.naparitools import NapariSumAxesWidget
from imswitch.imcontrol.controller.controllers.ImageController import ImageController
from imswitch.imcontrol.controller.display_transform import sum_axes_for_display
from imswitch.imcontrol.model.state_contracts import ComponentStateApplyMode


# --- the reduction ----------------------------------------------------------------------------

def test_the_sum_takes_leading_dimensions_only_and_keeps_counts_exact():
    im = np.arange(3 * 4 * 5, dtype=np.uint16).reshape(3, 4, 5)
    out, scale = sum_axes_for_display(im, (2.0, 0.5, 0.25), (0,))
    np.testing.assert_array_equal(out, im.sum(axis=0))
    assert out.dtype == np.float64 and scale == (0.5, 0.25)
    out, scale = sum_axes_for_display(im, (0.5, 0.25), ())          # nothing summed
    assert out.shape == (3, 4, 5) and scale == (0.5, 0.25)
    out, _ = sum_axes_for_display(im, None, (1, 2, 7))                  # image axes never
    assert out.shape == (3, 4, 5)
    stack = np.full((2, 3, 2, 2), 40000, dtype=np.uint16)
    out, _ = sum_axes_for_display(stack, None, (0, 1))
    assert out.max() == 6 * 40000                                      # no uint16 wrap


# --- the checkboxes ---------------------------------------------------------------------------

def _viewer(active_name):
    active = None if active_name is None else SimpleNamespace(name=active_name)
    return SimpleNamespace(layers=SimpleNamespace(selection=SimpleNamespace(active=active)))


def test_the_widget_shows_a_checkbox_per_extra_dimension_of_the_selected_layer(qtbot):
    widget = NapariSumAxesWidget(_viewer('Live: APD'))
    qtbot.addWidget(widget)
    assert widget.rows.count() == 0 and 'no dimension' in widget.note.text()
    widget.setAxes('Live: APD', ['line steps', 'z'], summed=(1,))
    assert [b.text() for b in widget._boxes] == ['line steps', 'z']
    assert [b.isChecked() for b in widget._boxes] == [False, True]
    changed = []
    widget.sigSumAxesChanged.connect(lambda name, axes: changed.append((name, axes)))
    widget._boxes[0].setChecked(True)
    widget._boxes[1].setChecked(False)
    assert changed == [('Live: APD', (0, 1)), ('Live: APD', (0,))]
    assert widget.summedAxes('Live: APD') == (0,)
    widget.setAxes('Live: Camera', [])                                 # another layer: no rows
    widget.viewer.layers.selection.active = SimpleNamespace(name='Live: Camera')
    widget.refresh()
    assert widget.rows.count() == 0


# --- the controller ---------------------------------------------------------------------------

class _Signal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in self.callbacks:
            callback(*args)


def _controller(axisNames=None):
    widget = Mock()
    widget.getLayerVisibilityState.return_value = {'layers': {}, 'liveLayers': {}}
    widget.applyLayerVisibilityState.return_value = []
    widget.sigSumAxesChanged = _Signal()
    commChannel = Mock()
    commChannel.sigSetVisibleLayers = _Signal()
    master = Mock()
    master.detectorsManager.hasDevices.return_value = False
    detector = SimpleNamespace(displayAxisNames=axisNames) if axisNames else SimpleNamespace()
    master.detectorsManager.__getitem__ = Mock(return_value=detector)
    with patch('imswitch.imcontrol.controller.controllers.ImageController.'
               'getWidgetStatePersistence', return_value=Mock()):
        controller = ImageController(Mock(), commChannel, master, widget=widget,
                                     factory=Mock(), moduleCommChannel=Mock())
    controller._setupInfo = SimpleNamespace(detectors={})
    return controller, widget


def _shown(widget):
    name, im, scale = widget.setImage.call_args[0][:3]
    return name, np.asarray(im), scale


def test_the_controller_sums_the_frame_the_checkbox_asks_for_and_redraws(qtbot):
    controller, widget = _controller(axisNames=('line steps', 'y', 'x'))
    frame = np.arange(2 * 3 * 4, dtype=np.uint16).reshape(2, 3, 4)
    controller.update('APD', frame, False, (1.0, 0.5, 0.5), True)
    widget.setSummableAxes.assert_called_with('APD', ['line steps'], ())
    _name, im, scale = _shown(widget)
    assert im.shape == (2, 3, 4)                                       # the stack, unchecked
    widget.sigSumAxesChanged.emit('APD', (0,))                         # the checkbox
    _name, im, scale = _shown(widget)                                  # redrawn at once
    np.testing.assert_array_equal(im, frame.sum(axis=0))
    assert tuple(scale) == (0.5, 0.5)
    widget.setSummableAxes.assert_called_with('APD', ['line steps'], (0,))
    assert controller.getComponentState()['summedAxes'] == {'APD': [0]}
    controller.update('APD', frame, False, (1.0, 0.5, 0.5), True)     # the next frame too
    assert _shown(widget)[1].shape == (3, 4)


def test_dimensions_without_a_name_are_numbered_and_a_2d_frame_offers_none():
    controller, widget = _controller()
    controller.update('Cam', np.zeros((3, 4, 5, 6)), False, None, True)
    widget.setSummableAxes.assert_called_with('Cam', ['dimension 0', 'dimension 1'], ())
    controller.update('Cam', np.zeros((5, 6)), False, None, True)
    widget.setSummableAxes.assert_called_with('Cam', [], ())


def test_the_summed_dimensions_are_kept_in_the_panel_state():
    controller, widget = _controller()
    assert 'summedAxes' not in controller.getComponentState()         # nothing summed: absent
    controller.applyComponentState({'layers': {}, 'liveLayers': {}, 'summedAxes': {'APD': [0]}},
                                   applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY)
    controller.update('APD', np.ones((2, 3, 4)), False, None, True)
    assert _shown(widget)[1].shape == (3, 4)
    assert 'APD over [0]' in ' '.join(controller.describeComponentState(
        {'summedAxes': {'APD': [0]}}))
