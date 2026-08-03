"""Autofocus must not sweep Z while a scan is driving it.

AutofocusController had no scan awareness at all -- not a single ``sigScan*``
reference -- while driving the configured Z positioner from a worker thread.
On any rig where it targets the actuator a scan drives, that is the same
conflict the focus lock yields for, without the yielding.

Narrower in scope than the focus-lock case: this only bites where autofocus
and the scan share an actuator, and the shipped STED example does not
configure autofocus at all.
"""

import threading

from imswitch.imcontrol.controller.controllers.AutofocusController import (
    AutofocusController,
)


class _Logger:
    def __init__(self):
        self.warnings = []

    def warning(self, message, *args, **kwargs):
        self.warnings.append(message)

    def error(self, message, *args, **kwargs):  # pragma: no cover - unused
        pass


class _Button:
    def __init__(self):
        self.text = 'Focus'
        self.enabled = True

    def setText(self, value):
        self.text = value

    def setEnabled(self, value):
        self.enabled = value


class _Widget:
    def __init__(self):
        self.focusButton = _Button()


def _makeController(*, focusing=False):
    ctrl = AutofocusController.__new__(AutofocusController)
    ctrl._closed = False
    ctrl._focusing = focusing
    ctrl._scanDepth = 0
    ctrl._scanActive = False
    ctrl._focusCancel = threading.Event()
    ctrl._focusThread = None
    ctrl._logger = _Logger()
    ctrl._widget = _Widget()

    ctrl.stoppedCalls = []
    ctrl._onFocusStopped = lambda: ctrl.stoppedCalls.append(True)
    return ctrl


def _start(ctrl):
    AutofocusController._onScanStarting(ctrl)


def _end(ctrl):
    AutofocusController._onScanEnded(ctrl)


def test_autofocus_refuses_to_start_during_a_scan():
    ctrl = _makeController()

    _start(ctrl)
    AutofocusController.autoFocus(ctrl, 100.0, 10.0)

    assert ctrl._focusing is False
    assert ctrl._logger.warnings
    assert ctrl.stoppedCalls, 'the button must be released, not left "Focusing..."'


def test_a_scan_cancels_a_sweep_already_running():
    ctrl = _makeController(focusing=True)

    _start(ctrl)

    assert ctrl._focusCancel.is_set() is True
    assert ctrl._logger.warnings


def test_an_idle_autofocus_is_not_cancelled_by_a_scan():
    ctrl = _makeController(focusing=False)

    _start(ctrl)

    assert ctrl._focusCancel.is_set() is False
    assert ctrl._logger.warnings == []


def test_the_block_lifts_when_the_scan_ends():
    ctrl = _makeController()

    _start(ctrl)
    _end(ctrl)

    assert ctrl._scanActive is False


def test_nested_scans_need_matching_ends():
    ctrl = _makeController()

    _start(ctrl)
    _start(ctrl)
    _end(ctrl)

    assert ctrl._scanActive is True

    _end(ctrl)
    assert ctrl._scanActive is False


def test_a_stray_end_does_not_unblock_autofocus():
    ctrl = _makeController()

    _start(ctrl)
    _end(ctrl)
    _end(ctrl)   # unpaired
    _start(ctrl)

    assert ctrl._scanActive is True
    assert ctrl._scanDepth == 1
