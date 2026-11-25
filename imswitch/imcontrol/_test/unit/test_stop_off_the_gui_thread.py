"""The pieces a GUI Stop stands on, one at a time (the panel-level cases are
``test_scan_stop_responsiveness``): the NI-DAQ manager's background stop, the
coordinator's background finish, and the detector manager's background release.
"""
import threading
import time

import numpy as np
import pytest
from qtpy import QtCore, QtTest

from imswitch.imcontrol.model.managers import _scan_execution as se
from imswitch.imcontrol.model.managers._acquisition_leases import LeasePurpose
from imswitch.imcontrol.model.managers.NidaqManager import NidaqManagerError

from . import test_nidaq_stop_scan as nss
from .test_detectors_manager_leases import manager  # noqa: F401  (the shared fixture)
from .test_scan_execution_coordinator import _setup as coordinatorSetup


def _until(predicate, timeoutS=5.0):
    deadline = time.monotonic() + timeoutS
    while not predicate() and time.monotonic() < deadline:
        QtTest.QTest.qWait(5)
    return predicate()


# ---------------------------------------------------------------------------
# NidaqManager.stopScan(background=True)
# ---------------------------------------------------------------------------

def test_a_background_stop_returns_at_once_and_ends_the_run_on_this_thread(qtbot):
    manager, made, ends = nss._manager()
    where = []
    manager.sigScanStopped.connect(lambda: where.append(threading.current_thread()))
    nss._run(manager)
    original = made['ScanAOTask'].stop

    def slowStop():
        time.sleep(0.6)
        original()
    made['ScanAOTask'].stop = slowStop

    started = time.monotonic()
    assert manager.stopScan(background=True) is True
    assert time.monotonic() - started < 0.1            # accepted, not waited for
    assert manager.runStopped and ends == []           # not ended yet
    assert manager.stopScan(background=True) is False  # a second stop: nothing to add
    assert manager.stopScan() is False
    assert _until(lambda: ends == ['stopped', 'done'])
    assert time.monotonic() - started >= 0.6           # the stop really waited for the driver
    assert where == [threading.main_thread()]          # the end is published from this thread
    assert made['ScanAOTask'].closed and not manager.busy
    assert manager.lastStop()['errors'] == []
    assert manager.stopScan(background=True) is False  # and nothing after it either


def test_a_scan_is_refused_while_a_background_stop_is_in_flight(qtbot):
    manager, made, ends = nss._manager()
    nss._run(manager)
    gate = threading.Event()
    original = made['ScanDOTask'].stop
    made['ScanDOTask'].stop = lambda: (gate.wait(5.0), original())[1]
    manager.stopScan(background=True)
    with pytest.raises(NidaqManagerError):      # busy / a teardown still unresolved
        nss._run(manager)
    gate.set()
    assert _until(lambda: ends == ['stopped', 'done'])


def test_a_background_stops_driver_calls_are_those_of_a_synchronous_one(qtbot):
    def calls(background):
        manager, made, ends = nss._manager()
        nss._run(manager)
        manager.stopScan(background=background)
        assert _until(lambda: ends == ['stopped', 'done'])
        return {name: (task.stopCalls, task.closed, [np.asarray(w).tolist() for w in task.writes])
                for name, task in sorted(made.items())}, manager.lastStop()
    assert calls(True) == calls(False)


def test_finalize_waits_for_a_stop_still_writing_the_rest_levels(qtbot):
    manager, made, ends = nss._manager()
    nss._run(manager)
    original = made['ScanAOTask'].stop
    made['ScanAOTask'].stop = lambda: (time.sleep(0.4), original())[1]
    manager.stopScan(background=True)
    manager.finalize()
    assert 'stopAnalogTask' in made                     # the rest write happened first
    assert manager.lastStop()['errors'] == []


# ---------------------------------------------------------------------------
# ScanExecutionCoordinator.resolve(background=True)
# ---------------------------------------------------------------------------

def _slowFinishers(manager, order, seconds):
    for name in ('APD', 'TimeTagger'):
        detector = manager[name]

        def finish(mode, acknowledge, _name=name):
            order.append((_name, mode, threading.current_thread().name))
            time.sleep(seconds)
            acknowledge()
        detector.finishScan = finish


def test_a_background_finish_asks_every_participant_in_order_off_this_thread():
    coordinator, manager, _ = coordinatorSetup()
    order = []
    _slowFinishers(manager, order, 0.2)
    token = coordinator.arm({}, {})
    done = threading.Event()
    where = []
    started = time.monotonic()
    assert coordinator.resolve(
        token, se.FINISH_ABORT, background=True,
        onComplete=lambda: (where.append(threading.current_thread().name), done.set()))
    assert time.monotonic() - started < 0.1             # asked, not waited for
    assert done.wait(5.0)
    assert [item[:2] for item in order] == [('APD', 'abort'), ('TimeTagger', 'abort')]
    assert all(item[2] == 'ScanFinish' for item in order)
    assert where == ['ScanFinish']                      # the lease release and completion too
    assert manager.released == ['lease-1']
    assert coordinator.activeToken is None


def test_a_finish_without_the_flag_is_unchanged():
    coordinator, manager, _ = coordinatorSetup()
    order = []
    _slowFinishers(manager, order, 0.0)
    token = coordinator.arm({}, {})
    done = []
    coordinator.resolve(token, se.FINISH_ABORT, onComplete=lambda: done.append(1))
    assert done == [1]                                  # synchronously, as before
    assert {item[2] for item in order} == {threading.current_thread().name}


def test_a_participant_that_never_answers_is_timed_out_and_released_off_this_thread(qtbot):
    coordinator, manager, _ = coordinatorSetup()
    coordinator.configure(scheduleTimeout=lambda delayS, callback: QtCore.QTimer.singleShot(
        int(delayS * 1000), callback))
    manager['APD'].autoAcknowledge = False
    where = []
    release = manager.release
    manager.release = lambda handle: (where.append(threading.current_thread().name),
                                      release(handle))[1]
    token = coordinator.arm({}, {})
    done = []
    coordinator.resolve(token, se.FINISH_ABORT, background=True, timeoutS=0.2,
                        onComplete=lambda: done.append(1))
    assert _until(lambda: done == [1], 5.0)             # the timer fired on this thread ...
    assert where == ['ScanFinishTimeout']               # ... and the release was not made on it


# ---------------------------------------------------------------------------
# DetectorsManager.releaseInBackground
# ---------------------------------------------------------------------------

def test_a_lease_released_in_the_background_stops_its_detector_there(manager):
    handle = manager.acquire(['CAM'], LeasePurpose.WORKFLOW)
    where = []
    stop = manager._subManagers['CAM'].stopAcquisition
    manager._subManagers['CAM'].stopAcquisition = lambda: (
        where.append(threading.current_thread().name), stop())[1]
    results = []
    manager.releaseInBackground(handle, results.append).join(5.0)
    assert results == [None]
    assert not manager.isDetectorLeased('CAM')
    assert where and where[0] != threading.current_thread().name
    # a second release is an error, handed to the callback and never raised here
    manager.releaseInBackground(handle, results.append).join(5.0)
    assert isinstance(results[1], ValueError)
