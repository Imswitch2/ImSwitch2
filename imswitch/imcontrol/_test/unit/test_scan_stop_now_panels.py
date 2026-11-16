"""Stop means now, on every panel (plan scan-stop-now, Lenny 2026-10-18).

On the simulated NI-DAQ with the mock APD: a scan that would run for
seconds is stopped from its panel mid-iteration. It ends within a fraction
of that time, does not repeat, drops its unfinished frame, brings its
outputs to rest -- and the Expert panel tracks the drive where the stop left
it, not "unknown".
"""
import time

import numpy as np
import pytest
from qtpy import QtTest

from .test_scan_controller_advanced_orders import AdvancedRig
from .test_scan_controller_expert import ExpertRig, _ms_resolft


def _simulator(rig):
    return rig.master.nidaqManager._NidaqManager__scanSimulator


def _until(predicate, timeout_s):
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        QtTest.QTest.qWait(20)
    return predicate()


@pytest.fixture
def advanced(qtbot):
    rig = AdvancedRig()
    yield rig
    rig.close()


def test_the_advanced_panel_stops_a_running_scan_now(advanced):
    rig = advanced
    rig.load(lengths=[20.0, 20.0, 0.0], steps=[1.0, 1.0, 1.0], dwell=10e-3)  # seconds of scan
    rig.widget.repeatBox.setChecked(True)               # it would go on, unstopped
    assert not rig.widget.stopButton.isEnabled()
    rig.ended.clear()
    rig.scan.runScanAdvanced()
    assert _until(lambda: _simulator(rig).progress > 0.0, 5.0)
    assert rig.widget.stopButton.isEnabled() and not rig.widget.scanButton.isEnabled()
    started = time.monotonic()
    rig.widget.stopButton.click()                       # the panel's own Stop
    assert _until(lambda: bool(rig.ended), 5.0)
    assert time.monotonic() - started < 1.0             # not the rest of the iteration
    nidaq = rig.master.nidaqManager
    assert nidaq.runStopped and not nidaq.busy
    stop = nidaq.lastStop()
    devices, signals, _shift = nidaq._lastAnalogWrite
    for device, signal in zip(devices, signals):         # back where the scan started
        assert stop['left'][device] == pytest.approx(float(np.asarray(signal)[0]))
    assert rig.master.detectorsManager['APD']._rawReady is False   # its frame is dropped
    QtTest.QTest.qWait(300)
    assert rig.ended == [1]                              # no repeat after the stop
    assert rig.widget.scanButton.isEnabled() and not rig.widget.stopButton.isEnabled()


def test_the_expert_panel_stops_on_the_nidaq_and_tracks_where_the_stop_left_z(qtbot):
    rig = ExpertRig()
    try:
        z = rig.master.positionersManager['Z']
        z.updateTrackedPosition({'Z': 3.0})
        rig.scan.setProgram(_ms_resolft(rig, timepoints=60))   # long enough to stop
        rig.ended.clear()
        rig.scan.runScanAdvanced()
        assert _until(lambda: _simulator(rig).progress > 0.0, 5.0)
        rig.widget.stopButton.click()
        assert _until(lambda: bool(rig.ended), 5.0)
        nidaq = rig.master.nidaqManager
        stop = nidaq.lastStop()
        assert stop is not None and 'Z' in stop['left']
        # Z is ramped back to its waveform's first sample -- with this
        # program's jump approach, its first position, 1 µm -- and the panel
        # tracks it there (µm = volts x 1 µm/V), not "unknown"
        devices, signals, _shift = nidaq._lastAnalogWrite
        first = float(np.asarray(signals[devices.index('Z')])[0])
        assert stop['left']['Z'] == pytest.approx(first) and first == pytest.approx(1.0)
        assert float(z.position['Z']) == pytest.approx(first * 1.0)
        assert 'Z' not in rig.scan._unknownPositions
    finally:
        rig.close()


def test_the_simple_point_scan_stop_ends_the_frame_now(qtbot):
    from .test_scan_cloak_contract import CloakRig
    rig = CloakRig('SimplePointScan')
    try:
        rig.view.setLive(True)
        rig.view.startButton.click()
        assert _until(lambda: rig.view.isRunning() and _simulator(rig).progress > 0.0, 5.0)
        rig.view.stopButton.click()
        assert 'Stopped' in rig.view.messageLabel.text()
        assert rig.waitForEnd(timeoutS=2.0)
        assert rig.master.nidaqManager.runStopped
    finally:
        rig.close()
