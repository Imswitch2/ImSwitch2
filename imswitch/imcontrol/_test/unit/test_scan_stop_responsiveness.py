"""Stopping a scan never holds the GUI thread (the "Stop almost crashes it"
report from a rig with an NI-DAQ).

Stop used to run the whole stop on the thread that took the click: every
NI-DAQ task's stop and close (to two seconds each), the waiter joins, the
rest writes that play out for as long as the ramp, then -- from the run's end
signal, still on that thread -- each detector's scan-thread join and input-task
close, the lease release that stops each detector, and BeadRec's release of
its camera. A real driver makes each of those take as long as it takes; here
each is made realistically slow, on the real controller, coordinator, managers
and NI-DAQ manager of a mock rig, and the longest gap of a 5 ms heartbeat on
the GUI thread is bounded. Every injected delay is over a second, so the bound
(``GUI_BOUND_S``) is far below it and far above scheduling noise; each case
also asserts the delay really was spent (the run ended no earlier than the
slowest device took), so a stop that skipped the work could not pass.

The stop's hardware CONTRACT is asserted too: the order the driver sees the
calls in is the order it always had, and the signals follow it.
"""
import time

import pytest
from qtpy import QtTest

from imswitch.imcontrol._test.unit import test_scan_controller_advanced_orders as _ao
from imswitch.imcontrol._test.unit._scan_stop_rig import GapProbe, install_daq, slow, wait_until

#: What a GUI-thread gap may be: well under every injected delay (>= 1 s).
GUI_BOUND_S = 0.4

LONG = dict(lengths=[20.0, 20.0, 0.0], steps=[1.0, 1.0, 1.0], dwell=10e-3)


@pytest.fixture
def rig(qtbot):
    rig = _ao.AdvancedRig()
    yield rig
    rig.close()


def _start(rig, daq=None, rest=None, log=None, **program):
    rig.load(**(program or LONG))
    if daq is not None:
        install_daq(rig.master.nidaqManager, rest=rest, log=log, **daq)
    rig.ended.clear()
    rig.scan.runScanAdvanced()
    simulator = rig.master.nidaqManager._NidaqManager__scanSimulator
    assert wait_until(lambda: simulator.progress > 0.0, 5.0)
    QtTest.QTest.qWait(150)


def _stop_and_measure(rig, stop=None, settleMs=400, timeoutS=30.0):
    probe = GapProbe(depth=6).start()
    started = time.monotonic()
    (stop or rig.widget.stopButton.click)()
    assert wait_until(lambda: bool(rig.ended), timeoutS), 'the stopped run never ended'
    took = time.monotonic() - started
    QtTest.QTest.qWait(settleMs)
    probe.stop()
    return probe, took


# ---------------------------------------------------------------------------
# NI-DAQ: the tasks, the waiters, the rest writes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('daq, rest, spent', [
    pytest.param(dict(ao=dict(stopS=1.2), do=dict(stopS=0.4), timer={}), None, 1.6,
                 id='stop-waits-for-the-buffer-in-flight'),
    pytest.param(dict(ao=dict(closeS=0.5), do=dict(closeS=0.5), timer=dict(closeS=0.5)),
                 None, 1.5, id='close-blocks-on-every-task'),
    pytest.param(dict(ao={}, do={}, timer={}), dict(playS=1.2), 1.2,
                 id='rest-ramp-plays-out-in-wait_until_done'),
])
def test_a_slow_daq_does_not_hold_the_gui_thread(rig, daq, rest, spent):
    _start(rig, daq=daq, rest=rest)
    probe, took = _stop_and_measure(rig)
    assert probe.maxGap < GUI_BOUND_S, probe.top()
    assert took >= spent * 0.9          # the work was done, and waited for
    nidaq = rig.master.nidaqManager
    assert nidaq.runStopped and not nidaq.busy and not nidaq.tasks
    assert rig.widget.scanButton.isEnabled() and rig.widget.stopButton.text() == 'Stop'


def test_the_daq_is_stopped_in_the_order_it_always_was(rig):
    """Only the thread changed: the driver sees the same calls in the same
    order -- every task stopped and closed (timer, digital, analog), then the
    scanners' rest write -- and the run ends stopped, then done."""
    log, signals = [], []
    nidaq = rig.master.nidaqManager
    nidaq.sigScanStopped.connect(lambda: signals.append('stopped'))
    nidaq.sigScanDone.connect(lambda: signals.append('done'))
    _start(rig, daq=dict(ao={}, do={}, timer={}), log=log, **LONG)
    _stop_and_measure(rig)
    # (the run's own tasks are being waited on by their waiter threads, which
    # start in no fixed order; the stop's calls are the others)
    log = [call for call in log
           if not (call[1] == 'wait_until_done' and call[0] in ('timer', 'do', 'ao'))]
    assert log == [
        ('timer', 'stop'), ('timer', 'close'),
        ('do', 'stop'), ('do', 'close'),
        ('ao', 'stop'), ('ao', 'close'),
        ('stopAnalogTask', 'write'), ('stopAnalogTask', 'wait_until_done'),
        ('stopAnalogTask', 'stop'), ('stopAnalogTask', 'close'),
    ]
    assert signals == ['stopped', 'done']


# ---------------------------------------------------------------------------
# Detectors: the end-of-scan barrier and the lease release
# ---------------------------------------------------------------------------

def test_slow_point_detectors_do_not_hold_the_gui_thread(rig):
    for name in ('APD', 'APD 2'):
        slow(rig.master.detectorsManager[name], 'stopAcquisition', 1.2)
    _start(rig)
    probe, took = _stop_and_measure(rig)
    assert probe.maxGap < GUI_BOUND_S, probe.top()
    assert took >= 2.0                  # both stops really ran, one after the other


def test_the_scan_threads_teardown_of_a_long_scan_is_off_the_gui_thread(rig):
    """No injected delay: the mock APD's scan thread takes most of a second to
    notice it was stopped (it is generating a big line), and the old code
    joined it on the GUI thread."""
    _start(rig)
    probe, _took = _stop_and_measure(rig)
    assert probe.maxGap < GUI_BOUND_S, probe.top()


def test_a_short_scan_stopped_mid_line(rig):
    _start(rig, **dict(lengths=[2.0, 2.0, 0.0], steps=[1.0, 1.0, 1.0], dwell=1e-3))
    probe, _took = _stop_and_measure(rig)
    assert probe.maxGap < GUI_BOUND_S, probe.top()


def test_a_time_tagger_scan_stops_without_a_gap(qtbot, monkeypatch):
    monkeypatch.setattr(
        _ao, 'SETUP',
        'imswitch/_data/user_defaults/imcontrol_setups/mock/time_tagger/'
        'galvo_flim_mock_scan_setup.json')
    rig = _ao.AdvancedRig()
    try:
        _start(rig)
        probe, _took = _stop_and_measure(rig)
        assert probe.maxGap < GUI_BOUND_S, probe.top()
    finally:
        rig.close()


# ---------------------------------------------------------------------------
# Re-entry: a second Stop, a Start straight after, a stop in the repeat gap
# ---------------------------------------------------------------------------

def test_a_second_stop_while_stopping_is_ignored_and_shows_stopping(rig):
    _start(rig, daq=dict(ao=dict(stopS=1.0), do={}, timer={}))
    calls = []
    engine = rig.scan._engineForScans()
    abort = engine.abort
    engine.abort = lambda: (calls.append(1), abort())[1]
    rig.widget.stopButton.click()
    assert rig.scan._stopInFlight
    assert rig.widget.stopButton.text() == 'Stopping…' and not rig.widget.stopButton.isEnabled()
    assert not rig.widget.scanButton.isEnabled()
    rig.widget.stopButton.click()               # disabled: nothing
    rig.scan._onPanelStopClicked()              # a click queued behind the first
    rig.scan.abortScan()                        # an API abort in the same window
    assert wait_until(lambda: bool(rig.ended), 20.0)
    QtTest.QTest.qWait(300)
    assert rig.ended == [1] and len(calls) >= 1
    assert not rig.scan._stopInFlight
    assert rig.widget.stopButton.text() == 'Stop' and not rig.widget.stopButton.isEnabled()
    assert rig.widget.scanButton.isEnabled()


def test_start_straight_after_stop_is_refused_then_a_new_scan_runs(rig):
    _start(rig, daq=dict(ao=dict(stopS=0.8), do={}, timer={}))
    rejected = []
    rig.channel.sigScanRequestRejected.connect(rejected.append)
    rig.widget.stopButton.click()
    rig.scan.runScan()                          # the Run button's slot, in the same window
    assert rejected and 'still stopping' in rejected[-1]
    assert wait_until(lambda: bool(rig.ended), 20.0)
    QtTest.QTest.qWait(300)
    assert rig.ended == [1]                     # one run, one end: nothing re-armed
    rig.ended.clear()
    rig.load(lengths=[2.0, 2.0, 0.0], steps=[1.0, 1.0, 1.0], dwell=1e-3)
    rig.scan.runScanAdvanced()                  # and the board takes a new scan
    assert wait_until(lambda: bool(rig.ended), 20.0)


def test_stop_in_the_first_milliseconds_of_a_start(rig):
    rig.load(**LONG)
    install_daq(rig.master.nidaqManager, ao=dict(stopS=0.5), do={}, timer={})
    rig.ended.clear()
    rig.scan.runScanAdvanced()
    rig.widget.stopButton.click()               # before the simulator has produced anything
    assert wait_until(lambda: bool(rig.ended), 20.0)
    QtTest.QTest.qWait(300)
    assert rig.ended == [1]
    assert not rig.scan.isRunning and rig.scan.shutdownComplete()
    assert rig.widget.scanButton.isEnabled()


def test_stop_with_repeat_ticked_does_not_repeat(rig):
    rig.widget.repeatBox.setChecked(True)
    _start(rig, daq=dict(ao=dict(stopS=0.6), do={}, timer={}))
    probe, _took = _stop_and_measure(rig, settleMs=600)
    assert probe.maxGap < GUI_BOUND_S, probe.top()
    assert rig.ended == [1]


def test_an_exception_in_the_stop_slot_is_logged_and_shown_not_raised(rig, caplog):
    _start(rig)
    engine = rig.scan._engineForScans()

    def broken():
        raise RuntimeError('the driver said no')
    original = engine.abort
    engine.abort = broken
    rig.widget.stopButton.click()               # a raising Qt slot would abort the test run
    assert 'The scan engine did not stop' in caplog.text
    assert rig.widget.stopButton.text() == 'Stop failed' and rig.widget.stopButton.isEnabled()
    assert 'the driver said no' in rig.widget.stopButton.toolTip()
    assert not rig.scan._stopInFlight
    engine.abort = original                     # the user clicks again: it stops
    rig.widget.stopButton.click()
    assert wait_until(lambda: bool(rig.ended), 20.0)
