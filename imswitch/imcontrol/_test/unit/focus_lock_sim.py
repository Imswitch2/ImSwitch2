"""A simulated focus-lock rig for tests: a reflected spot whose row follows
the focus actuator's Z, real frames through the real estimator and trust
features, and a FocusLockController built without ``__init__``.

Nothing here touches hardware or Qt widgets; the controller's GUI-thread
hand-off is a queue the test drains (``rig.pump()``), so worker-thread calls
(pause/resume) are exercised with a genuine second thread.
"""
import queue
import threading

import numpy as np

from imswitch.imcontrol.controller.controllers import FocusLockController as flc_module
from imswitch.imcontrol.controller.controllers.FocusLockController import (
    FocusLockController, ProcessDataThread,
)

H = W = 300


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _Text:
    def __init__(self, value):
        self.value = value

    def text(self):
        return self.value


class _Button:
    def __init__(self):
        self.checked = False
        self.label = 'Lock'

    def isChecked(self):
        return self.checked

    def setChecked(self, value):
        self.checked = bool(value)

    def setText(self, text):
        self.label = text


class _Any:
    def __getattr__(self, name):
        return lambda *a, **k: None


class _Widget:
    def __init__(self):
        self.kpEdit = _Text('20')
        self.kiEdit = _Text('20')
        self.lockButton = _Button()
        self.center = _Any()
        self.camImg = _Any()
        self.focusPlotCurve = _Any()
        self.focusPlot = _Any()
        self.focusLockGraph = type('G', (), {'lineLock': None})()
        self.calibrationDisplay = _Any()
        self.calibCurveButton = _Any()
        self.states = []
        self.quality = []

    def setLockState(self, state):
        self.states.append(state)

    def setLockQuality(self, status):
        self.quality.append(status)


class Rig:
    """Spot row = ``row0 + pxPerUm * (z - zFocus)``; ``move`` changes z."""

    def __init__(self, monkeypatch, *, pxPerUm=10.0, calibrated=True, seed=0, noise=10.0):
        self.rng = np.random.default_rng(seed)
        self.clock = Clock()
        monkeypatch.setattr(flc_module, 'perf_counter', self.clock)
        self.pxPerUm = pxPerUm
        self.noise = noise
        self.z = 0.0
        self.zFocus = 0.0
        self.row0 = 150.0
        self.moves = []
        self.moveError = None
        self.queue = queue.Queue()
        self.main = threading.current_thread()
        yy, xx = np.mgrid[0:H, 0:W]
        self._yy, self._xx = yy, xx
        self.ctrl = self._build(calibrated)
        self.worker = ProcessDataThread.__new__(ProcessDataThread)
        self.worker._controller = self.ctrl

    # -- controller ------------------------------------------------------
    def _build(self, calibrated):
        ctrl = FocusLockController.__new__(FocusLockController)
        d = dict(
            _shutdownComplete=False, _focusCalibrationActive=False, locked=False,
            aboutToLock=False, _lastPIUpdate=None, _scanSuspendDepth=0,
            _scanOwnsFocusActuator=True, _suspendedLock=False, _preScanSetPoint=None,
            _reacquireDeadline=None, _reacquireSamples=None, _reacquireFailed=False,
            _pendingMove=0.0, _reacquireDone=threading.Event(), setPointSignal=0.0,
            buffer=40, currPoint=0, setPointData=np.zeros(40), timeData=np.zeros(40),
            startTime=0.0, focusTime=100.0, twoFociVar=False, positioner='Z',
            positionerAxis='Z', _setupNameForLock='sim-setup', lockPosition=0,
        )
        for key, value in d.items():
            setattr(ctrl, key, value)
        ctrl._reacquireDone.set()
        ctrl._logger = Log()
        ctrl._widget = _Widget()
        ctrl.movePositioner = self.move
        ctrl.getPositionerAbs = lambda: self.z
        ctrl.focusCameraAcquisitionActive = lambda: True
        ctrl._invokeOnControllerThreadIfNeeded = self.queue.put
        ctrl._onControllerThread = lambda: threading.current_thread() is self.main
        if calibrated:
            ctrl._lockEngine().pxPerUm = self.pxPerUm
        return ctrl

    def move(self, dz):
        if self.moveError is not None:
            raise self.moveError
        self.moves.append(float(dz))
        self.z += float(dz)

    # -- frames ----------------------------------------------------------
    def spotRow(self):
        return self.row0 + self.pxPerUm * (self.z - self.zFocus)

    def image(self, *, amp=1000.0, sigma=12.0, occlude=None, extra=None, row=None):
        row = self.spotRow() if row is None else row
        img = amp * np.exp(-((self._yy - row) ** 2 + (self._xx - 150) ** 2) / (2 * sigma ** 2))
        if occlude is not None:
            img[: int(row + occlude[0]), :] *= occlude[1]
        if extra is not None:
            r2, a2 = extra
            img = img + a2 * np.exp(-((self._yy - r2) ** 2 + (self._xx - 150) ** 2) / (2 * 144))
        img = 100 + img + self.rng.normal(0, self.noise, img.shape)
        return np.clip(img, 0, 65535).astype(np.uint16)

    def frame(self, dt=0.1, **kw):
        """One focus estimate through the real estimator, then one tick."""
        raw = self.image(**kw)
        self.worker.latestimg = raw
        signal = self.worker.update(False)
        features = self.worker.measureFeatures()
        self.clock.t += dt
        self.ctrl._FocusLockController__processDataThread = _OneShot(
            (raw, signal, self.clock.t, features))
        FocusLockController.update(self.ctrl)
        return self.state()

    def frames(self, n, **kw):
        return [self.frame(**kw) for _ in range(n)]

    def pump(self):
        while True:
            try:
                callback = self.queue.get_nowait()
            except queue.Empty:
                return
            callback()

    def state(self):
        return FocusLockController.focusLockState(self.ctrl)

    def lock(self, learnFrames=40):
        """Lock at the current spot and run long enough to learn thresholds."""
        self.frame()
        self.ctrl._widget.lockButton.checked = True
        FocusLockController.lockFocus(self.ctrl, self.z)
        self.frames(learnFrames)
        return self.ctrl._lockEngine()


class _OneShot:
    def __init__(self, result):
        self._result = result

    def takeResult(self):
        result, self._result = self._result, None
        return result


class Log:
    def __init__(self):
        self.records = []

    def _add(self, level, message, *args, **kwargs):
        self.records.append((level, message % args if args else message))

    def info(self, m, *a, **k):
        self._add('info', m, *a)

    def warning(self, m, *a, **k):
        self._add('warning', m, *a)

    def error(self, m, *a, **k):
        self._add('error', m, *a)

    def debug(self, m, *a, **k):
        pass

    def text(self):
        return '\n'.join(m for _, m in self.records)
