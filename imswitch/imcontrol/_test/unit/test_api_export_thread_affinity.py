"""Widget-touching API exports must run on the GUI thread (plan A-06).

Two explicit regressions (D-01 changeScanPower, D-11 loadSetupMode) plus a
runtime check that the generated API wrapper really executes the laser
widget update on the GUI thread when called from a worker thread. Each of
the explicit tests fails as soon as ``runOnUIThread=True`` is removed from
the repaired export.
"""
import threading

import pytest
from qtpy import QtCore

from imswitch.imcommon.model import generateAPI
from imswitch.imcontrol.controller.SetupModeController import SetupModeController
from imswitch.imcontrol.controller.controllers.LaserController import LaserController


def test_change_scan_power_is_a_ui_thread_export():
    assert getattr(LaserController.changeScanPower, '_APIExport', False) is True
    assert getattr(LaserController.changeScanPower, '_APIRunOnUIThread', False) is True


def test_load_setup_mode_is_a_ui_thread_export():
    assert getattr(SetupModeController.loadSetupMode, '_APIExport', False) is True
    assert getattr(SetupModeController.loadSetupMode, '_APIRunOnUIThread', False) is True


class _RecordingLaserWidget:
    def __init__(self):
        self.calls = []

    def setValue(self, laserName, value, emitSignal=True):
        self.calls.append(('setValue', laserName, value, threading.get_ident()))

    def setLaserActive(self, laserName, active, emitSignal=True):
        self.calls.append(('setLaserActive', laserName, active, threading.get_ident()))


class _FakeLaser:
    isBinary = False

    def __init__(self):
        self.values = []
        self.enabled = None

    def setValue(self, value):
        self.values.append(value)

    def setEnabled(self, enabled):
        self.enabled = enabled


class _FakeSharedAttrs(dict):
    pass


class _FakeCommChannel:
    def __init__(self):
        self.sharedAttrs = _FakeSharedAttrs()


def _bare_laser_controller():
    ctrl = LaserController.__new__(LaserController)
    QtCore.QObject.__init__(ctrl)  # the C++ side, without the widget/master wiring of __init__
    ctrl._widget = _RecordingLaserWidget()
    ctrl._commChannel = _FakeCommChannel()
    lasers = {'405': _FakeLaser()}

    class _Master:
        lasersManager = lasers

    ctrl._master = _Master()
    ctrl.settingAttr = False
    return ctrl


def test_change_scan_power_updates_the_widget_on_the_gui_thread(qtbot):
    ctrl = _bare_laser_controller()
    api = generateAPI([ctrl])
    gui_ident = threading.get_ident()
    outcome = {}

    def worker():
        try:
            api.changeScanPower('405', 0.0)   # 0 mW also toggles the enable button
            api.changeScanPower('405', 12.5)
            outcome['ok'] = True
        except BaseException as error:  # noqa: BLE001 - surfaced to the test
            outcome['error'] = error

    thread = threading.Thread(target=worker)
    thread.start()
    qtbot.waitUntil(lambda: len(ctrl._widget.calls) >= 3, timeout=3000)
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert 'error' not in outcome, outcome.get('error')
    assert ctrl._master.lasersManager['405'].values == [0.0, 12.5]
    kinds = [call[0] for call in ctrl._widget.calls]
    assert kinds == ['setValue', 'setLaserActive', 'setValue']
    assert all(call[-1] == gui_ident for call in ctrl._widget.calls)
