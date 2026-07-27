"""NI-DAQ scan transaction and stale-completion regressions."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.model.managers.NidaqManager import (
    NidaqManager,
    NidaqManagerError,
    ScanBusyError,
    _ONE_SHOT_WAIT_TIMEOUT_S,
)


class _NidaqConfig:
    def __init__(self):
        self.simulation = True
        self.startTrigger = False

    def getTimerCounterChannel(self):
        return None


class _Device:
    def __init__(self, analog=None, digital=None):
        self._analog = analog
        self._digital = digital

    def getAnalogChannel(self):
        return self._analog

    def getDigitalLine(self):
        return self._digital


class _Setup:
    def __init__(self, devices=None):
        self.nidaq = _NidaqConfig()
        self.scan = SimpleNamespace(
            lineClockLine=None,
            frameStartClockLine=None,
            frameEndClockLine=None,
            sampleRate=100000,
        )
        self.detectors = {}
        self._devices = devices or {}

    def getAllDevices(self):
        return self._devices

    def getDevice(self, name):
        return self._devices[name]


class _Simulator:
    def __init__(self, *, failStart=False):
        self.failStart = failStart
        self.startCalls = 0
        self.stopCalls = []

    def start(self, _signals, _scanInfo):
        self.startCalls += 1
        if self.failStart:
            raise RuntimeError("simulator failed after allocation")

    def stop(self, wait=False):
        self.stopCalls.append(wait)


class _Task:
    def __init__(
        self, *, failStart=False, failStop=False, failClose=False,
        events=None,
    ):
        self.failStart = failStart
        self.failStop = failStop
        self.failClose = failClose
        self.startCalls = 0
        self.stopCalls = 0
        self.closeCalls = 0
        self.writes = []
        self.events = events

    def write(self, values, auto_start=False):
        self.writes.append((np.asarray(values), auto_start))

    def start(self):
        self.startCalls += 1
        if self.failStart:
            raise RuntimeError("task start failed")

    def stop(self):
        self.stopCalls += 1
        if self.events is not None:
            self.events.append("task-stop")
        if self.failStop:
            raise RuntimeError("task stop failed")

    def close(self):
        self.closeCalls += 1
        if self.events is not None:
            self.events.append("task-close")
        if self.failClose:
            raise RuntimeError("task close failed")


class _BlockingTask(_Task):
    def __init__(self, *, blockStop=False, blockClose=False):
        super().__init__()
        self.blockStop = blockStop
        self.blockClose = blockClose
        self.stopEntered = threading.Event()
        self.closeEntered = threading.Event()
        self.releaseStop = threading.Event()
        self.releaseClose = threading.Event()

    def stop(self):
        self.stopCalls += 1
        self.stopEntered.set()
        if self.blockStop:
            self.releaseStop.wait()

    def close(self):
        self.closeCalls += 1
        self.closeEntered.set()
        if self.blockClose:
            self.releaseClose.wait()


class _OneShotTask(_BlockingTask):
    def __init__(
        self,
        *,
        blockWait=False,
        blockClose=False,
        closeReleasesWait=False,
        failWait=False,
        failClose=False,
    ):
        super().__init__(blockClose=blockClose)
        self.blockWait = blockWait
        self.closeReleasesWait = closeReleasesWait
        self.failWait = failWait
        self.failClose = failClose
        self.waitCalls = 0
        self.waitTimeouts = []
        self.waitEntered = threading.Event()
        self.releaseWait = threading.Event()

    def wait_until_done(self, timeout=None):
        self.waitCalls += 1
        self.waitTimeouts.append(timeout)
        self.waitEntered.set()
        if self.blockWait:
            self.releaseWait.wait()
        if self.failWait:
            raise RuntimeError("one-shot wait failed")

    def close(self):
        self.closeCalls += 1
        self.closeEntered.set()
        if self.closeReleasesWait:
            self.releaseWait.set()
        if self.blockClose:
            self.releaseClose.wait()
        if self.failClose:
            raise RuntimeError("one-shot close failed")


class _Waiter:
    def __init__(self, running=False):
        self.running = running


class _FinalizeWaiter:
    def __init__(self, events=None, *, stops=True):
        self.events = events
        self.stops = stops
        self.running = True
        self.threadRunning = True
        self.quitCalls = 0
        self.waitTimeouts = []

    def quit(self):
        self.quitCalls += 1
        if self.events is not None:
            self.events.append("waiter-quit")

    def wait(self, timeout):
        self.waitTimeouts.append(timeout)
        if self.events is not None:
            self.events.append("waiter-wait")
        if self.stops:
            self.threadRunning = False
        return self.stops

    def isRunning(self):
        return self.threadRunning


class _FrameworkFinalizeWaiter:
    """Matches framework.Thread, whose wait() has no timeout parameter."""

    def __init__(self):
        self.running = True
        self.threadRunning = True
        self.quitCalls = 0
        self.waitCalls = 0

    def quit(self):
        self.quitCalls += 1
        self.threadRunning = False

    def wait(self):
        self.waitCalls += 1

    def isRunning(self):
        return self.threadRunning


def _simulatedManager(*, simulator=None):
    manager = NidaqManager(_Setup())
    manager._NidaqManager__scanSimulator = simulator or _Simulator()
    return manager


def _hardwareManager(task):
    setup = _Setup({"X": _Device(analog="Dev1/ao0")})
    manager = NidaqManager(setup)
    # Construct in simulation so tests never require an NI driver, then expose
    # the hardware branch with a fully controlled task factory.
    manager._NidaqManager__simulating = False
    manager._NidaqManager__scanSimulator = None
    manager._NidaqManager__createChanAOTask = (
        lambda *_args, **_kwargs: task
    )
    return manager


def _oneShotHardwareManager(task):
    setup = _Setup({
        "X": _Device(analog="Dev1/ao0", digital="Dev1/port0/line0")
    })
    manager = NidaqManager(setup)
    manager._NidaqManager__simulating = False
    manager._NidaqManager__scanSimulator = None
    manager._NidaqManager__createChanAOTask = (
        lambda *_args, **_kwargs: task
    )
    manager._NidaqManager__createLineDOTask = (
        lambda *_args, **_kwargs: task
    )
    return manager


def _signals():
    return {
        "scanSignalsDict": {},
        "TTLCycleSignalsDict": {},
    }


def _hardwareSignals():
    return {
        "scanSignalsDict": {"X": np.zeros(4, dtype=float)},
        "TTLCycleSignalsDict": {},
    }


def test_detector_reports_collapse_to_one_transaction_failure():
    simulator = _Simulator()
    manager = _simulatedManager(simulator=simulator)
    failures = []
    started = []
    manager.sigScanBuildFailed.connect(lambda: failures.append(True))
    manager.sigScanStarted.connect(lambda: started.append(True))

    def reportTwice(*_args):
        manager.reportScanBuildFailure("APD.prepare", RuntimeError("first"))
        manager.reportScanBuildFailure("PMT.prepare", RuntimeError("second"))

    manager.sigScanBuilt.connect(reportTwice)
    with pytest.raises(NidaqManagerError, match="APD.prepare"):
        manager.runScan(_signals(), {"img_dims": [1, 1]})

    assert failures == [True]
    assert started == []
    assert simulator.startCalls == 0
    assert simulator.stopCalls == [False]
    assert manager.busy is False
    assert manager.signalSent is True
    assert manager.tasks == {}


def test_report_from_scan_started_rolls_back_before_simulator_start():
    simulator = _Simulator()
    manager = _simulatedManager(simulator=simulator)
    failures = []
    manager.sigScanBuildFailed.connect(lambda: failures.append(True))
    manager.sigScanStarted.connect(
        lambda: manager.reportScanBuildFailure(
            "APD.start", RuntimeError("thread start failed")
        )
    )

    with pytest.raises(NidaqManagerError, match="APD.start"):
        manager.runScan(_signals(), {"img_dims": [1, 1]})

    assert failures == [True]
    assert simulator.startCalls == 0
    assert simulator.stopCalls == [False]
    assert manager.busy is False


def test_simulator_start_failure_is_transactional():
    simulator = _Simulator(failStart=True)
    manager = _simulatedManager(simulator=simulator)
    events = []
    manager.sigScanBuilt.connect(lambda *_args: events.append("built"))
    manager.sigScanStarted.connect(lambda: events.append("started"))
    manager.sigScanBuildFailed.connect(lambda: events.append("failed"))

    with pytest.raises(RuntimeError, match="simulator failed"):
        manager.runScan(_signals(), {"img_dims": [1, 1]})

    assert events == ["built", "started", "failed"]
    assert simulator.startCalls == 1
    assert simulator.stopCalls == [False]
    assert manager.busy is False
    assert manager.tasks == {}


def test_output_task_start_failure_closes_every_partial_resource():
    task = _Task(failStart=True)
    manager = _hardwareManager(task)
    failures = []
    manager.sigScanBuildFailed.connect(lambda: failures.append(True))

    with pytest.raises(RuntimeError, match="task start failed"):
        manager.runScan(_hardwareSignals(), {"img_dims": [1, 1]})

    assert failures == [True]
    assert task.startCalls == 1
    assert task.stopCalls == 1
    assert task.closeCalls == 1
    assert manager.aoTaskWaiter is None
    assert manager.tasks == {}
    assert manager.busy is False


def test_detector_build_failure_closes_registered_but_unstarted_task():
    task = _Task()
    manager = _hardwareManager(task)
    failures = []
    manager.sigScanBuildFailed.connect(lambda: failures.append(True))
    manager.sigScanBuilt.connect(
        lambda *_args: manager.reportScanBuildFailure(
            "APD.prepare", RuntimeError("bad scan metadata")
        )
    )

    with pytest.raises(NidaqManagerError, match="APD.prepare"):
        manager.runScan(_hardwareSignals(), {"img_dims": [1, 1]})

    assert failures == [True]
    assert task.startCalls == 0
    assert task.stopCalls == 1
    assert task.closeCalls == 1
    assert manager.tasks == {}


def test_stale_input_completion_cannot_close_new_same_named_task():
    simulator = _Simulator()
    manager = _simulatedManager(simulator=simulator)
    task = _Task()
    manager.tasks["APD"] = task
    manager._taskGenerations["APD"] = 22
    manager._scanGeneration = 22
    manager._scanTransactionActive = False
    manager.signalSent = False
    manager.busy = True
    done = []
    manager.sigScanDone.connect(lambda: done.append(True))

    assert manager.inputTaskDone("APD") is False
    assert manager.inputTaskDone("APD", 21) is False
    assert manager.tasks["APD"] is task
    assert task.closeCalls == 0

    assert manager.inputTaskDone("APD", 22) is True
    assert task.closeCalls == 1
    assert done == [True]


def test_stale_output_waiter_or_generation_cannot_complete_new_task():
    simulator = _Simulator()
    manager = _simulatedManager(simulator=simulator)
    task = _Task()
    currentWaiter = _Waiter(running=False)
    staleWaiter = _Waiter(running=False)
    manager.tasks["ao"] = task
    manager._taskGenerations["ao"] = 31
    manager._taskWaiters["ao"] = currentWaiter
    manager._scanGeneration = 31
    manager._scanTransactionActive = False
    manager.signalSent = False
    manager.busy = True

    assert manager.taskDone("ao", currentWaiter) is False
    assert manager.taskDone("ao", staleWaiter, 31) is False
    assert manager.taskDone("ao", currentWaiter, 30) is False
    assert manager.tasks["ao"] is task

    assert manager.taskDone("ao", currentWaiter, 31) is True
    assert task.closeCalls == 1
    assert manager.tasks == {}


def test_output_waiter_failure_rolls_back_and_reports_terminal_failure():
    simulator = _Simulator()
    manager = _simulatedManager(simulator=simulator)
    task = _Task()
    waiter = _Waiter(running=False)
    waiter.failure = RuntimeError("driver wait failed")
    manager.tasks["ao"] = task
    manager._taskGenerations["ao"] = 41
    manager._taskWaiters["ao"] = waiter
    manager._scanGeneration = 41
    manager._scanTransactionActive = False
    manager.signalSent = False
    manager.busy = True
    failures = []
    done = []
    manager.sigScanBuildFailed.connect(lambda: failures.append(True))
    manager.sigScanDone.connect(lambda: done.append(True))

    assert manager._taskWaiterDone("ao", waiter, 41) is True

    assert failures == [True]
    assert done == []
    assert task.stopCalls == 1
    assert task.closeCalls == 1
    assert manager.tasks == {}
    assert manager.busy is False


def test_waiter_failure_keeps_busy_true_until_cleanup_returns(monkeypatch):
    manager = _simulatedManager()
    task = _Task()
    waiter = _Waiter(running=False)
    waiter.failure = RuntimeError("driver wait failed")
    manager.tasks["ao"] = task
    manager._taskGenerations["ao"] = 51
    manager._taskWaiters["ao"] = waiter
    manager._scanGeneration = 51
    manager._scanTransactionActive = False
    manager.signalSent = False
    manager.busy = True
    observed = []
    originalCleanup = manager._cleanupScanResources

    def cleanupWhileObserving(**kwargs):
        observed.append(manager.busy)
        return originalCleanup(**kwargs)

    monkeypatch.setattr(
        manager, "_cleanupScanResources", cleanupWhileObserving
    )

    assert manager._taskWaiterDone("ao", waiter, 51) is True

    assert observed == [True]
    assert manager.busy is False


def test_input_task_teardown_failure_is_strict_and_fail_closed():
    manager = _simulatedManager()
    task = _Task(failClose=True)
    manager.tasks["APD"] = task
    manager._taskGenerations["APD"] = 61
    manager._scanGeneration = 61
    manager._scanTransactionActive = False
    manager.signalSent = False
    manager.busy = True
    failures = []
    done = []
    manager.sigScanBuildFailed.connect(lambda: failures.append(True))
    manager.sigScanDone.connect(lambda: done.append(True))

    with pytest.raises(NidaqManagerError, match="APD.*task close failed"):
        manager.inputTaskDone("APD", 61)

    # The failed task is retained for finalize/retry; no later scan can replace
    # a driver resource whose close state is unknown.
    assert manager.tasks["APD"] is task
    assert manager.busy is True
    assert failures == [True]
    assert done == []
    assert task.stopCalls == 2  # strict attempt + transaction cleanup retry
    assert task.closeCalls == 2

    # A terminal scan must still retry the exact retained input generation.
    # signalSent suppresses duplicate lifecycle signals, not resource cleanup.
    with pytest.raises(NidaqManagerError, match="APD.*task close failed"):
        manager.inputTaskDone("APD", 61)
    assert task.stopCalls == 3
    assert task.closeCalls == 3
    assert failures == [True]

    task.failClose = False
    assert manager.finalize(waiterTimeoutMs=1) is True
    assert manager.tasks == {}


def test_finalize_stops_resources_before_bounded_wait_and_is_idempotent():
    events = []
    simulator = _Simulator()
    originalSimulatorStop = simulator.stop

    def stopSimulator(wait=False):
        events.append("simulator-stop")
        originalSimulatorStop(wait=wait)

    simulator.stop = stopSimulator
    manager = _simulatedManager(simulator=simulator)
    task = _Task(events=events)
    waiter = _FinalizeWaiter(events)
    manager.tasks["ao"] = task
    manager._taskGenerations["ao"] = 71
    manager._taskWaiters["ao"] = waiter
    manager.aoTaskWaiter = waiter
    manager.busy = True

    assert manager.finalize(waiterTimeoutMs=17) is True

    assert events == [
        "task-stop",
        "task-close",
        "simulator-stop",
        "waiter-quit",
        "waiter-wait",
    ]
    assert waiter.waitTimeouts == [17]
    assert manager.tasks == {}
    assert manager.aoTaskWaiter is None

    # close() is the same idempotent shutdown operation.
    assert manager.close(waiterTimeoutMs=99) is True
    assert waiter.waitTimeouts == [17]
    with pytest.raises(NidaqManagerError, match="shutting down"):
        manager.runScan(_signals(), {"img_dims": [1, 1]})


def test_finalize_timeout_retains_waiter_for_a_bounded_retry():
    manager = _simulatedManager()
    waiter = _FinalizeWaiter(stops=False)
    manager.aoTaskWaiter = waiter

    assert manager.finalize(waiterTimeoutMs=23) is False
    assert waiter.waitTimeouts == [23]
    assert manager.aoTaskWaiter is waiter
    assert manager.busy is True

    waiter.stops = True
    assert manager.close(waiterTimeoutMs=29) is True
    assert waiter.waitTimeouts == [23, 29]
    assert manager.aoTaskWaiter is None


def test_native_task_stop_timeout_is_serialized_and_retryable():
    manager = _simulatedManager()
    task = _BlockingTask(blockStop=True)
    manager.tasks["ao"] = task
    manager._taskGenerations["ao"] = 81

    started = time.monotonic()
    assert manager.stopTask(
        "ao", 81, teardownTimeoutMs=20
    ) is False
    assert time.monotonic() - started < 0.5
    assert task.stopEntered.is_set()
    assert task.closeCalls == 0
    assert manager.tasks == {}
    assert "ao" in manager._taskTeardownOperations
    assert manager.busy is True

    with pytest.raises(NidaqManagerError, match="teardown is unresolved"):
        manager._registerScanTask("new", _Task(), None, 82)

    task.releaseStop.set()
    assert manager.stopTask(
        "ao", 81, teardownTimeoutMs=500
    ) is True
    assert task.stopCalls == 1
    assert task.closeCalls == 1
    assert manager._taskTeardownOperations == {}


def test_native_task_close_timeout_is_serialized_and_retryable():
    manager = _simulatedManager()
    task = _BlockingTask(blockClose=True)
    manager.tasks["APD"] = task
    manager._taskGenerations["APD"] = 91

    with pytest.raises(NidaqManagerError, match="Timed out"):
        manager.stopTask(
            "APD",
            91,
            strict=True,
            teardownTimeoutMs=20,
        )
    assert task.closeEntered.is_set()
    assert task.stopCalls == 1
    assert task.closeCalls == 1
    assert "APD" in manager._taskTeardownOperations

    task.releaseClose.set()
    assert manager.stopTask(
        "APD", 91, strict=True, teardownTimeoutMs=500
    ) is True
    assert task.stopCalls == 1
    assert task.closeCalls == 1
    assert manager._taskTeardownOperations == {}


def test_teardown_thread_start_failure_restores_exact_state_for_retry(
        monkeypatch):
    manager = _simulatedManager()
    task = _Task()
    waiter = _FinalizeWaiter()
    generation = 96
    manager.tasks["ao"] = task
    manager._taskGenerations["ao"] = generation
    manager._taskWaiters["ao"] = waiter

    realStart = threading.Thread.start
    failed = False

    def failFirstTeardownStart(thread):
        nonlocal failed
        if not failed and thread.name == "NidaqTaskTeardown-ao":
            failed = True
            raise RuntimeError("cannot start teardown thread")
        return realStart(thread)

    monkeypatch.setattr(threading.Thread, "start", failFirstTeardownStart)

    assert manager.stopTask(
        "ao", generation, teardownTimeoutMs=20
    ) is False
    assert manager.tasks["ao"] is task
    assert manager._taskGenerations["ao"] == generation
    assert manager._taskWaiters["ao"] is waiter
    assert manager._taskTeardownOperations == {}
    assert manager.busy is True
    assert task.stopCalls == 0
    assert task.closeCalls == 0

    assert manager.stopTask(
        "ao", generation, teardownTimeoutMs=500
    ) is True
    assert task.stopCalls == 1
    assert task.closeCalls == 1
    assert manager.tasks == {}
    assert manager._taskTeardownOperations == {}


@pytest.mark.parametrize(
    ("methodName", "taskName"),
    [
        ("setAnalog", "setAnalogTask"),
        ("setDigital", "setDigitalTask"),
    ],
)
def test_one_shot_outputs_use_registered_bounded_teardown(
        methodName, taskName):
    task = _OneShotTask()
    manager = _oneShotHardwareManager(task)

    getattr(manager, methodName)("X", 1)

    assert task.waitCalls == 1
    assert task.waitTimeouts == [_ONE_SHOT_WAIT_TIMEOUT_S]
    assert task.stopCalls == 1
    assert task.closeCalls == 1
    assert manager.tasks == {}
    assert manager._taskTeardownOperations == {}
    assert manager.busy is False
    assert taskName not in manager._taskGenerations


def test_finalize_can_close_one_shot_task_blocked_in_native_wait():
    task = _OneShotTask(
        blockWait=True,
        closeReleasesWait=True,
    )
    manager = _oneShotHardwareManager(task)
    errors = []

    def setAnalog():
        try:
            manager.setAnalog("X", 1)
        except BaseException as error:
            errors.append(error)

    setterThread = threading.Thread(target=setAnalog)
    setterThread.start()
    assert task.waitEntered.wait(1)
    assert manager.tasks["setAnalogTask"] is task

    started = time.monotonic()
    assert manager.finalize(waiterTimeoutMs=200) is True
    assert time.monotonic() - started < 1

    setterThread.join(1)
    assert not setterThread.is_alive()
    assert errors == []
    assert task.stopCalls == 1
    assert task.closeCalls == 1
    assert manager.tasks == {}
    assert manager._taskTeardownOperations == {}
    assert manager.busy is False


def test_blocked_one_shot_close_keeps_finalize_fail_closed_until_retry():
    task = _OneShotTask(
        blockWait=True,
        blockClose=True,
    )
    manager = _oneShotHardwareManager(task)
    errors = []

    def setDigital():
        try:
            manager.setDigital("X", True)
        except BaseException as error:
            errors.append(error)

    setterThread = threading.Thread(target=setDigital)
    setterThread.start()
    assert task.waitEntered.wait(1)

    started = time.monotonic()
    assert manager.finalize(waiterTimeoutMs=20) is False
    assert time.monotonic() - started < 0.5
    assert "setDigitalTask" in manager._taskTeardownOperations
    assert manager.busy is True
    with pytest.raises(NidaqManagerError, match="shutting down"):
        manager.setAnalog("X", 0)

    task.releaseWait.set()
    assert task.closeEntered.wait(1)
    task.releaseClose.set()
    setterThread.join(1)
    assert not setterThread.is_alive()
    assert errors == []

    assert manager.finalize(waiterTimeoutMs=500) is True
    assert manager.tasks == {}
    assert manager._taskTeardownOperations == {}


def test_one_shot_close_error_retains_task_and_refuses_new_output():
    task = _OneShotTask(failClose=True)
    manager = _oneShotHardwareManager(task)

    with pytest.raises(NidaqManagerError, match="one-shot close failed"):
        manager.setDigital("X", True)

    assert manager.tasks["setDigitalTask"] is task
    assert manager.busy is True
    assert manager._taskTeardownOperations == {}
    with pytest.raises(ScanBusyError, match="manager is busy"):
        manager.setAnalog("X", 0)

    task.failClose = False
    assert manager.finalize(waiterTimeoutMs=500) is True
    assert manager.tasks == {}


def test_one_shot_wait_error_still_closes_task_and_releases_busy():
    task = _OneShotTask(failWait=True)
    manager = _oneShotHardwareManager(task)

    with pytest.raises(RuntimeError, match="one-shot wait failed"):
        manager.setAnalog("X", 1)

    assert task.stopCalls == 1
    assert task.closeCalls == 1
    assert manager.tasks == {}
    assert manager._taskTeardownOperations == {}
    assert manager.busy is False


def test_finalize_supports_framework_thread_wait_without_timeout_argument():
    manager = _simulatedManager()
    waiter = _FrameworkFinalizeWaiter()
    manager.aoTaskWaiter = waiter

    assert manager.finalize(waiterTimeoutMs=17) is True
    assert waiter.quitCalls == 1
    assert waiter.waitCalls == 1
    assert manager.aoTaskWaiter is None


def test_terminal_cleanup_timeout_keeps_manager_fail_closed():
    manager = _simulatedManager()
    waiter = _FinalizeWaiter(stops=False)
    manager.aoTaskWaiter = waiter
    manager.busy = True
    manager.signalSent = False

    assert manager._terminalScanFailure(RuntimeError("waiter failed")) is True

    assert waiter.waitTimeouts == [2000]
    assert manager.aoTaskWaiter is waiter
    assert manager.tasks == {}
    assert manager.busy is True


def test_finalize_permanently_closes_every_task_creation_entry_point():
    manager = _simulatedManager()
    created = []
    manager._NidaqManager__createChanAITask = (
        lambda *_args, **_kwargs: created.append(True)
    )

    assert manager.finalize(waiterTimeoutMs=1) is True

    with pytest.raises(NidaqManagerError, match="shutting down"):
        manager.startInputTask("APD", "ai")
    with pytest.raises(NidaqManagerError, match="shutting down"):
        manager._registerScanTask("ao", _Task(), None, 91)
    with pytest.raises(NidaqManagerError, match="shutting down"):
        manager.setAnalog("X", 0)
    with pytest.raises(NidaqManagerError, match="shutting down"):
        manager.setDigital("X", False)
    assert created == []
    assert manager.tasks == {}


def test_stop_failure_with_successful_close_does_not_retain_closed_task():
    manager = _simulatedManager()
    task = _Task(failStop=True)
    manager.tasks["APD"] = task
    manager._taskGenerations["APD"] = 101

    with pytest.raises(NidaqManagerError, match="task stop failed"):
        manager.stopTask("APD", 101, strict=True)

    assert task.closeCalls == 1
    assert manager.tasks == {}
    assert manager._taskGenerations == {}
    assert manager.finalize(waiterTimeoutMs=1) is True


def test_waiter_stop_failure_after_successful_close_is_reported_terminally():
    manager = _simulatedManager()
    task = _Task(failStop=True)
    waiter = _FinalizeWaiter()
    waiter.running = False
    manager.tasks["ao"] = task
    manager._taskGenerations["ao"] = 111
    manager._taskWaiters["ao"] = waiter
    manager.aoTaskWaiter = waiter
    manager._scanGeneration = 111
    manager._scanTransactionActive = False
    manager.signalSent = False
    manager.busy = True
    failures = []
    manager.sigScanBuildFailed.connect(lambda: failures.append(True))

    assert manager._taskWaiterDone("ao", waiter, 111) is True

    assert failures == [True]
    assert manager.tasks == {}
    assert manager.busy is False


def test_reentrant_finalize_from_build_slot_cannot_restart_simulator():
    simulator = _Simulator()
    manager = _simulatedManager(simulator=simulator)
    failures = []
    manager.sigScanBuildFailed.connect(lambda: failures.append(True))
    manager.sigScanBuilt.connect(
        lambda *_args: manager.finalize(waiterTimeoutMs=1)
    )

    with pytest.raises(NidaqManagerError, match="shutting down"):
        manager.runScan(_signals(), {"img_dims": [1, 1]})

    assert manager._finalized is True
    assert simulator.startCalls == 0
    assert manager.tasks == {}
    assert failures == [True]
