"""A sum projection in the live view (scan timing plan, S6): a Sum checkbox
per dimension of a live layer beyond its image -- line steps, z, t -- that
sums the view over it. Display only: the frame and the recording are
untouched; the levels follow what is shown."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pytest
from qtpy import QtWidgets

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

def _docked(qtbot):
    """The widget in a dock, as napari docks it under the levels tool."""
    widget = NapariSumAxesWidget(SimpleNamespace())
    dock = QtWidgets.QDockWidget()
    dock.setWidget(widget)
    qtbot.addWidget(dock)
    widget._showIfOffered()
    return widget, dock


def test_one_checkbox_per_dimension_and_hidden_while_no_frame_has_one(qtbot):
    widget, dock = _docked(qtbot)
    assert dock.isHidden() and not widget.isOffered()                # nothing to sum: no panel
    widget.setAxes('APD', ['line steps'])
    widget.setAxes('PMT', ['line steps', 'z'])                         # one box per dimension
    assert not dock.isHidden()
    assert [box.text() for box in widget._boxes.values()] == ['Sum line steps', 'Sum z']
    assert not any(isinstance(child, QtWidgets.QLabel)                 # no text, just the boxes
                   for child in widget.findChildren(QtWidgets.QWidget))
    changed = []
    widget.sigSumAxesChanged.connect(changed.append)
    widget._boxes['line steps'].setChecked(True)
    widget._boxes['z'].setChecked(True)
    widget._boxes['z'].setChecked(False)
    assert changed == [('line steps',), ('line steps', 'z'), ('line steps',)]
    widget.setAxes('APD', [])
    widget.setAxes('PMT', [])                                          # 2-D frames again
    assert dock.isHidden() and widget.summedAxes() == ('line steps',)  # the choice outlasts them
    widget.setAxes('APD', ['line steps'])
    assert widget._boxes['line steps'].isChecked()
    widget.setAxes('APD', ['line steps'], summed=())                   # a restored state
    assert not widget._boxes['line steps'].isChecked() and changed[-1] == ('line steps',)


# --- the controller ---------------------------------------------------------------------------

class _Signal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in self.callbacks:
            callback(*args)


def _controller(axisNames=None, widget=None):
    if widget is None:
        widget = Mock()
        widget.sigSumAxesChanged = _Signal()
        widget.getImage.side_effect = lambda name: _shown(widget)[1]   # the layer's data
    widget.getLayerVisibilityState.return_value = {'layers': {}, 'liveLayers': {}}
    widget.applyLayerVisibilityState.return_value = []
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
    controller._shouldResetView = False      # as __init__ sets it on a rig with detectors
    return controller, widget


def _shown(widget):
    name, im, scale = widget.setImage.call_args[0][:3]
    return name, np.asarray(im), scale


def test_the_controller_sums_the_frame_the_checkbox_asks_for_and_redraws(qtbot):
    controller, widget = _controller(axisNames=('line steps', 'y', 'x'))
    frame = np.arange(2 * 3 * 4, dtype=np.uint16).reshape(2, 3, 4)
    controller.update('APD', frame, True, (1.0, 0.5, 0.5), True)
    widget.setSummableAxes.assert_called_with('APD', ['line steps'], ())
    _name, im, scale = _shown(widget)
    assert im.shape == (2, 3, 4)                                       # the stack, unchecked
    widget.setImageDisplayLevels.reset_mock()
    widget.sigSumAxesChanged.emit(('line steps',))                     # the checkbox
    _name, im, scale = _shown(widget)                                  # redrawn at once
    np.testing.assert_array_equal(im, frame.sum(axis=0))
    assert tuple(scale) == (0.5, 0.5)
    widget.setImageDisplayLevels.assert_called()                       # levelled anew
    widget.resetView.assert_not_called()                               # the zoom stays
    widget.setSummableAxes.assert_called_with('APD', ['line steps'], ('line steps',))
    assert controller.getComponentState()['summedDimensions'] == ['line steps']
    controller.update('APD', frame, True, (1.0, 0.5, 0.5), True)      # the next frame too
    assert _shown(widget)[1].shape == (3, 4)


def test_the_real_checkbox_reaches_the_live_layer(qtbot):
    """End to end through the real widget, wired as ImageWidget wires it:
    the controller says which dimensions a detector's frames have, the
    panel shows a box for them, and checking it redraws that detector's
    layer summed. (The two halves once keyed the dimensions differently --
    detector name against layer name -- and the box never appeared.)"""
    from imswitch.imcontrol.view.widgets.ImageWidget import ImageWidget
    panel, dock = _docked(qtbot)
    view = Mock(spec=['sumAxesWidget', 'sigSumAxesChanged', 'getLayerVisibilityState',
                      'applyLayerVisibilityState', 'setImage', 'getImage',
                      'setImageDisplayLevels', 'setSummableAxes', 'setOverlayPixelScale',
                      'resetView'])
    view.sumAxesWidget = panel
    view.sigSumAxesChanged = panel.sigSumAxesChanged
    view.setSummableAxes = lambda *args: ImageWidget.setSummableAxes(view, *args)
    view.getImage = lambda name: _shown(view)[1]
    controller, _ = _controller(axisNames=('line steps', 'y', 'x'), widget=view)
    frame = np.arange(3 * 4 * 5, dtype=np.uint16).reshape(3, 4, 5)
    controller.update('APD', frame, True, (1.0, 0.5, 0.5), True)
    assert not dock.isHidden()
    panel._boxes['line steps'].setChecked(True)
    name, im, _scale = _shown(view)
    assert name == 'APD'
    np.testing.assert_array_equal(im, frame.sum(axis=0))
    panel._boxes['line steps'].setChecked(False)
    assert _shown(view)[1].shape == (3, 4, 5)
    controller.update('APD', np.zeros((4, 5), dtype=np.uint16), True, (0.5, 0.5), True)
    assert dock.isHidden()                                             # a 2-D scan: no panel


def test_dimensions_without_a_name_are_numbered_and_a_2d_frame_offers_none():
    controller, widget = _controller()
    controller.update('Cam', np.zeros((3, 4, 5, 6)), False, None, True)
    widget.setSummableAxes.assert_called_with('Cam', ['dimension 0', 'dimension 1'], ())
    controller.update('Cam', np.zeros((5, 6)), False, None, True)
    widget.setSummableAxes.assert_called_with('Cam', [], ())
    assert 'Cam' not in controller._lastFrames                        # no 2-D frame kept


def test_the_summed_dimensions_are_kept_in_the_panel_state():
    controller, widget = _controller(axisNames=('line steps', 'y', 'x'))
    assert 'summedDimensions' not in controller.getComponentState()   # nothing summed: absent
    controller.applyComponentState(
        {'layers': {}, 'liveLayers': {}, 'summedDimensions': ['line steps']},
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY)
    controller.update('APD', np.ones((2, 3, 4)), False, None, True)
    assert _shown(widget)[1].shape == (3, 4)
    widget.setSummableAxes.assert_called_with('APD', ['line steps'], ('line steps',))
    assert 'line steps' in ' '.join(controller.describeComponentState(
        {'summedDimensions': ['line steps']}))
