"""Focus-lock camera acquisition ownership.

The focus camera historically held a FOCUS detector lease for the whole
ImSwitch session. The Focus Lock widget can now explicitly release that camera
when it is not needed, which also makes detector reconnect possible without
special-casing Focus Lock in the lifecycle layer.
"""

import importlib
import threading

from imswitch.imcontrol.controller.controllers.FocusLockController import (
    FocusLockController,
)


focus_module = importlib.import_module(
    'imswitch.imcontrol.controller.controllers.FocusLockController'
)


class _Logger:
    def __init__(self):
        self.warnings = []
        self.errors = []

    def warning(self, message, *args, **kwargs):
        self.warnings.append(message)

    def error(self, message, *args, **kwargs):
        self.errors.append(message)


class _Button:
    def __init__(self, checked=False):
        self.checked = checked
        self.enabled = True

    def setChecked(self, value):
        self.checked = bool(value)

    def isChecked(self):
        return self.checked

    def setEnabled(self, value):
        self.enabled = bool(value)


class _Widget:
    def __init__(self):
        self.cameraAcqButton = _Button(True)
        self.lockButton = _Button(False)
        self.focusCalibButton = _Button(False)

    def setFocusCameraActive(self, active):
        self.cameraAcqButton.setChecked(active)


class _Timer:
    def __init__(self):
        self.started = []
        self.stopCount = 0

    def start(self, interval):
        self.started.append(interval)

    def stop(self):
        self.stopCount += 1


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot, *args, **kwargs):
        self.slots.append(slot)

    def emit(self):
        for slot in list(self.slots):
            slot()


class _Worker:
    def __init__(self, controller=None, *, waitResult=True):
        self.controller = controller
        self.waitResult = waitResult
        self.running = False
        self.started = 0
        self.stopped = 0
        self.quitCount = 0
        self.finished = _Signal()

    def start(self):
        self.started += 1
        self.running = True

    def stop(self):
        self.stopped += 1

    def quit(self):
        self.quitCount += 1

    def wait(self):
        if self.waitResult:
            self.running = False
        return self.waitResult

    def isRunning(self):
        return self.running

    def finish(self):
        self.running = False
        self.finished.emit()


class _DetectorsManager:
    def __init__(self):
        self.acquired = []
        self.released = []
        self._next = 0

    def acquire(self, names, purpose):
        self._next += 1
        handle = f'lease-{self._next}'
        self.acquired.append((tuple(names), purpose, handle))
        return handle

    def release(self, handle):
        self.released.append(handle)


class _Master:
    def __init__(self):
        self.detectorsManager = _DetectorsManager()


def _makeController(*, active=False, worker=None):
    ctrl = FocusLockController.__new__(FocusLockController)
    ctrl._logger = _Logger()
    ctrl._widget = _Widget()
    ctrl._master = _Master()
    ctrl.camera = 'FocusCam'
    ctrl.focusTime = 100.0
    ctrl.timer = _Timer()
    ctrl._shutdownComplete = False
    ctrl._focusCalibrationActive = False
    ctrl._focusCameraStopPending = False
    ctrl._focusLeaseLock = threading.Lock()
    ctrl._focusAcqHandle = 'lease-existing' if active else None
    ctrl._FocusLockController__processDataThread = worker

    ctrl.unlockCalls = 0
    ctrl.unlockFocus = lambda: setattr(ctrl, 'unlockCalls', ctrl.unlockCalls + 1)
    ctrl._publishFocusLockState = lambda: None

    # waitForFocusReacquired state
    ctrl.locked = False
    ctrl.aboutToLock = False
    ctrl._suspendedLock = False
    ctrl._reacquireFailed = False
    ctrl._reacquireDone = threading.Event()
    ctrl._reacquireDone.set()
    return ctrl


def test_start_acquires_focus_lease_and_starts_fresh_worker(monkeypatch):
    ctrl = _makeController(active=False)
    worker = _Worker()
    monkeypatch.setattr(focus_module, 'ProcessDataThread', lambda controller: worker)

    assert FocusLockController._startFocusCameraAcquisition(ctrl) is True

    assert len(ctrl._master.detectorsManager.acquired) == 1
    assert worker.started == 1
    assert ctrl.timer.started == [100]
    assert ctrl._focusAcqHandle is not None
    assert ctrl._widget.cameraAcqButton.checked is True
    assert ctrl._widget.cameraAcqButton.enabled is True
    assert ctrl._widget.lockButton.enabled is True
    assert ctrl._widget.focusCalibButton.enabled is True


def test_stop_waits_for_worker_then_releases_focus_lease():
    worker = _Worker(waitResult=True)
    worker.running = True
    ctrl = _makeController(active=True, worker=worker)

    assert FocusLockController._stopFocusCameraAcquisition(ctrl) is True

    assert ctrl.unlockCalls == 1
    assert ctrl.timer.stopCount == 1
    assert worker.stopped == 1
    assert worker.quitCount == 1
    assert ctrl._master.detectorsManager.released == ['lease-existing']
    assert ctrl._focusAcqHandle is None
    assert ctrl._FocusLockController__processDataThread is None
    assert ctrl._widget.cameraAcqButton.checked is False
    assert ctrl._widget.cameraAcqButton.enabled is True
    assert ctrl._widget.lockButton.enabled is False
    assert ctrl._widget.focusCalibButton.enabled is False


def test_stuck_worker_retains_lease_until_it_actually_finishes():
    worker = _Worker(waitResult=False)
    worker.running = True
    ctrl = _makeController(active=True, worker=worker)

    assert FocusLockController._stopFocusCameraAcquisition(ctrl) is False

    assert ctrl._focusAcqHandle == 'lease-existing'
    assert ctrl._master.detectorsManager.released == []
    assert ctrl._widget.cameraAcqButton.checked is False
    assert ctrl._widget.cameraAcqButton.enabled is False

    worker.finish()

    assert ctrl._focusAcqHandle is None
    assert ctrl._master.detectorsManager.released == ['lease-existing']
    assert ctrl._widget.cameraAcqButton.enabled is True


def test_camera_acquisition_cannot_be_stopped_during_calibration():
    worker = _Worker(waitResult=True)
    worker.running = True
    ctrl = _makeController(active=True, worker=worker)
    ctrl._focusCalibrationActive = True

    assert FocusLockController._stopFocusCameraAcquisition(ctrl) is False

    assert worker.stopped == 0
    assert ctrl._focusAcqHandle == 'lease-existing'
    assert ctrl._master.detectorsManager.released == []
    assert ctrl._widget.cameraAcqButton.checked is True
    assert ctrl._widget.cameraAcqButton.enabled is False


def test_unlocked_focus_lock_does_not_block_reacquisition_barrier():
    ctrl = _makeController(active=False)

    assert FocusLockController.waitForFocusReacquired(ctrl, timeoutS=0) is True
