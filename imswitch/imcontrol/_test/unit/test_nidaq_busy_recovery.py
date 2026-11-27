"""A failed close must not leave the NI-DAQ manager busy until restart.

The manager keeps itself busy while a task it could not close is registered
(never idle with a native task open).  That used to be permanent: nothing
retried the close, and Stop had no run left to stop.  Stop and the next
start/one-shot now retry it, and clear busy only once the task is really
closed.
"""

from __future__ import annotations

import functools
import threading
import time

import pytest

from imswitch.imcontrol._test.unit.test_nidaq_scan_transaction import (
    _BlockingTask,
    _OneShotTask,
    _Task,
    _hardwareManager,
    _hardwareSignals,
    _oneShotHardwareManager,
)
from imswitch.imcontrol.model.managers.NidaqManager import (
    NidaqManagerError,
    ScanBusyError,
)

INFO = {"img_dims": [1, 1]}


def _idle(manager):
    return (
        manager.busy is False
        and manager.tasks == {}
        and manager._taskTeardownOperations == {}
    )


def _arm(manager):
    manager.runScan(_hardwareSignals(), dict(INFO))


def test_stop_retries_a_close_that_failed_during_stop():
    task = _Task(failClose=True)
    manager = _hardwareManager(task)
    _arm(manager)
    assert manager.stopScan() is True
    assert manager.busy is True and "ao" in manager.tasks  # fail-closed

    task.failClose = False
    assert manager.stopScan() is False  # no run left; but it recovers
    assert _idle(manager)
    assert task.closeCalls == 2
    _arm(manager)  # and the next scan runs
    assert manager.busy is True and manager.tasks


def test_next_start_retries_the_close_a_failed_scan_left_behind():
    task = _Task(failStart=True, failClose=True)
    manager = _hardwareManager(task)
    failures = []
    manager.sigScanBuildFailed.connect(lambda: failures.append(True))
    with pytest.raises(RuntimeError, match="task start failed"):
        _arm(manager)
    assert failures == [True]  # signalled once
    assert manager.busy is True

    task.failStart = False
    task.failClose = False
    _arm(manager)  # no restart, no Stop needed
    assert manager.tasks and manager.busy is True
    assert failures == [True]


def test_start_stays_refused_while_the_close_keeps_failing():
    task = _Task(failStart=True, failClose=True)
    manager = _hardwareManager(task)
    with pytest.raises(RuntimeError):
        _arm(manager)
    task.failStart = False
    with pytest.raises(ScanBusyError):
        _arm(manager)  # still cannot close: never reported idle
    assert manager.busy is True and "ao" in manager.tasks
    assert manager.stopScan() is False
    assert manager.busy is True
    task.failClose = False
    assert manager.stopScan() is False
    assert _idle(manager)


def test_one_shot_close_failure_is_retried_by_the_next_move():
    task = _OneShotTask(failClose=True)
    manager = _oneShotHardwareManager(task)
    with pytest.raises(NidaqManagerError, match="one-shot close failed"):
        manager.setDigital("X", True)
    assert manager.busy is True

    task.failClose = False
    manager.setAnalog("X", 0)  # the next move heals it and runs
    assert _idle(manager)


def test_stale_task_with_idle_flag_is_closed_not_latched():
    # "already registered" used to set busy and raise, for good.
    task = _OneShotTask()
    manager = _oneShotHardwareManager(task)
    stale = _Task()
    manager.tasks["setAnalogTask"] = stale
    manager._taskGenerations["setAnalogTask"] = ("one-shot", 0)
    assert manager.busy is False

    manager.setAnalog("X", 1)
    assert stale.closeCalls == 1
    assert task.closeCalls == 1
    assert _idle(manager)


def test_teardown_that_outlived_its_deadline_is_reaped_by_stop():
    task = _BlockingTask(blockClose=True)
    manager = _hardwareManager(task)
    manager._cleanupScanResources = functools.partial(
        manager._cleanupScanResources, waiterTimeoutMs=30
    )
    _arm(manager)
    assert manager.stopScan() is True
    assert manager.busy is True
    assert "ao" in manager._taskTeardownOperations

    # the driver is still inside close(): stays fail-closed, nothing retried
    assert manager.stopScan() is False
    assert manager.busy is True
    assert task.closeCalls == 1
    with pytest.raises(NidaqManagerError, match="unresolved: ao"):
        _arm(manager)

    task.releaseClose.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if manager._taskTeardownOperations["ao"]["event"].is_set():
            break
        time.sleep(0.005)
    assert manager.stopScan() is False
    assert _idle(manager)
    assert task.closeCalls == 1  # it had closed; nothing was closed twice
    _arm(manager)


def test_a_scan_in_flight_is_never_touched_by_a_refused_start_or_stop_retry():
    task = _Task()
    manager = _hardwareManager(task)
    _arm(manager)
    with pytest.raises(ScanBusyError):
        _arm(manager)
    assert task.stopCalls == 0 and task.closeCalls == 0
    assert manager.busy is True and "ao" in manager.tasks
    assert manager.signalSent is False
    assert manager.stopScan() is True  # the refusal changed nothing
    assert _idle(manager)
    _arm(manager)


def test_a_live_one_shot_is_not_closed_by_a_refused_scan_start():
    task = _OneShotTask(blockWait=True)
    manager = _oneShotHardwareManager(task)
    mover = threading.Thread(target=manager.setAnalog, args=("X", 1))
    mover.start()
    assert task.waitEntered.wait(2)
    with pytest.raises(ScanBusyError):
        manager.runScan(_hardwareSignals(), dict(INFO))
    assert task.closeCalls == 0 and manager.busy is True
    task.releaseWait.set()
    mover.join(2)
    assert _idle(manager)
    manager.runScan(_hardwareSignals(), dict(INFO))


def test_a_refusal_names_what_holds_the_daq():
    manager = _hardwareManager(_Task())
    _arm(manager)
    with pytest.raises(ScanBusyError) as refused:
        _arm(manager)
    message = str(refused.value)
    assert "scan #1" in message and "running" in message
    assert "open tasks: ao" in message

    mover = _OneShotTask(blockWait=True)
    other = _oneShotHardwareManager(mover)
    thread = threading.Thread(target=other.setAnalog, args=("X", 1))
    thread.start()
    assert mover.waitEntered.wait(2)
    with pytest.raises(ScanBusyError, match='one-shot "setAnalogTask"'):
        other.setDigital("X", True)
    mover.releaseWait.set()
    thread.join(2)


def test_reset_daq_closes_left_over_tasks_and_reports_idle():
    task = _Task(failClose=True)
    manager = _hardwareManager(task)
    _arm(manager)
    manager.stopScan()
    assert manager.busy is True

    ok, message = manager.resetDaq()
    assert ok is False and "ao" in message  # truthful: still open
    assert manager.busy is True

    task.failClose = False
    ok, message = manager.resetDaq()
    assert ok is True, message
    assert _idle(manager)
    _arm(manager)


def test_reset_daq_ends_a_run_in_flight_like_stop():
    manager = _hardwareManager(_Task())
    _arm(manager)
    ended = []
    manager.sigScanStopped.connect(lambda: ended.append("stopped"))
    manager.sigScanDone.connect(lambda: ended.append("done"))
    ok, _message = manager.resetDaq()
    assert ok is True and _idle(manager)
    assert ended == ["stopped", "done"]


def test_reset_daq_refuses_while_a_move_is_writing():
    task = _OneShotTask(blockWait=True)
    manager = _oneShotHardwareManager(task)
    thread = threading.Thread(target=manager.setAnalog, args=("X", 1))
    thread.start()
    assert task.waitEntered.wait(2)
    ok, message = manager.resetDaq()
    assert ok is False and "one-shot" in message
    assert task.closeCalls == 0
    task.releaseWait.set()
    thread.join(2)
    assert _idle(manager)


def test_non_exception_interrupt_during_arming_still_rolls_back():
    class Interrupted(_Task):
        def start(self):
            raise KeyboardInterrupt()

    task = Interrupted()
    manager = _hardwareManager(task)
    with pytest.raises(KeyboardInterrupt):
        _arm(manager)
    assert _idle(manager)
    assert task.closeCalls == 1


def test_hardware_status_offers_reset_nidaq_even_while_a_scan_run_is_held():
    from types import SimpleNamespace

    from imswitch.imcontrol.model.devices import (
        DeviceLifecycleService,
        DeviceSupervisor,
        HardwareDeviceId,
    )
    from imswitch.imcontrol.model.managers.MultiManager import MultiManager

    class _Group(MultiManager):
        def __init__(self):
            self._subManagers = {}
            self._shutdownFinalizedSubManagerObjects = []

    task = _Task(failClose=True)
    nidaq = _hardwareManager(task)
    nidaq._configuredDeviceNames = ("Dev1",)
    master = SimpleNamespace(
        detectorsManager=_Group(), lasersManager=_Group(),
        positionersManager=_Group(), rotatorsManager=_Group(),
        flipMirrorsManager=_Group(), rs232sManager=_Group(),
        slmsManager=_Group(), nidaqManager=nidaq,
        pulseGeneratorManager=None, triggerScopeManager=None,
        standManager=None,
        # a run token that never ended: the guard of other devices would block
        scanExecutionCoordinator=SimpleNamespace(
            activeRunToken=object(), activeToken=None),
        recordingManager=SimpleNamespace(record=False),
    )
    service = DeviceLifecycleService(master, DeviceSupervisor(master))
    hardwareId = HardwareDeviceId("infrastructure", "nidaq:Dev1")
    assert service.canReconnect(hardwareId) is True

    _arm(nidaq)
    nidaq.stopScan()
    assert nidaq.busy is True
    result = service.reconnect(hardwareId)
    assert result.success is False and "ao" in result.summary
    task.failClose = False
    result = service.reconnect(hardwareId)
    assert result.success is True, result.summary
    assert _idle(nidaq)


class _AnyTask(_OneShotTask):
    """One fake that serves as scan output task and as one-shot move task."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.failStart = False

    def start(self):
        if self.failStart:
            raise RuntimeError("task start failed")


def test_tiling_sequence_xy_move_z_move_scan_next_tile_survives_failures():
    """tiling-widget-dev's worker: XY move, Z one-shot, triggered scan, next
    tile, with a failed scan and a manual start in the middle."""
    task = _AnyTask()
    manager = _oneShotHardwareManager(task)
    refused = []

    def tile(index):
        manager.setAnalog("X", 0.1 * index)   # XY move (one-shot)
        manager.setAnalog("X", -0.1 * index)  # Z compensation (one-shot)
        manager.runScan(_hardwareSignals(), dict(INFO))
        # a manual Scan press while this tile's scan is in flight
        try:
            manager.runScan(_hardwareSignals(), dict(INFO))
        except ScanBusyError as error:
            refused.append(str(error))

    tile(1)
    assert len(refused) == 1 and "scan #1" in refused[0]
    assert manager.busy is True and manager.signalSent is False  # untouched
    manager.scanDone()          # tile 1's scan completes ...
    manager.stopTask("ao")      # ... its tasks having closed
    assert _idle(manager)

    # tile 2: the scan fails to start and its close fails too (stuck state)
    manager.setAnalog("X", 0.2)
    task.failStart = True
    task.failClose = True
    with pytest.raises(RuntimeError, match="task start failed"):
        manager.runScan(_hardwareSignals(), dict(INFO))
    assert manager.busy is True

    # the worker's next moves heal it instead of failing for good
    task.failStart = False
    task.failClose = False
    manager.setAnalog("X", 0.3)
    manager.setAnalog("X", -0.3)
    assert _idle(manager)
    tile(3)                      # and tiling carries on
    assert manager.busy is True
