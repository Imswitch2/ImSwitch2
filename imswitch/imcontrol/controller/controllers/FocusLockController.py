import threading
import sip

import numpy as np
from time import perf_counter
import scipy.ndimage as ndi
from skimage.feature import peak_local_max
from qtpy import QtCore

from imswitch.imcommon.framework import Signal, Thread, Timer
from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.managers import LeasePurpose
from ..basecontrollers import ImConWidgetController

_CLOSE_WAIT_TIMEOUT_MS = 2000

# Focus-lock lifecycle states, as reported by ``focusLockState()``.
STATE_UNLOCKED = 'unlocked'
STATE_LOCKED = 'locked'
STATE_SUSPENDED = 'suspended'        # a scan owns the actuator
STATE_REACQUIRING = 'reacquiring'    # scan over, waiting for the signal back
STATE_REACQUIRE_FAILED = 'reacquire-failed'


class FocusLockController(ImConWidgetController):
    """Linked to FocusLockWidget.

    Scan arbitration
    ----------------
    The focus lock and a hardware Z scan can drive the same physical actuator
    -- the STED setup reaches one piezo through an analog scanner *and* a
    serial positioner -- in which case an actively correcting lock fights the
    intentional Z waveform.

    Actuation is therefore suspended for the duration of any scan that can
    reach the lock's axis, on this boundary:

    ``sigScanStarting``  -> suspend, before the scan writes to any hardware
    ``sigScanActuatorsResolved`` -> resume early iff the scan provably cannot
                                    reach our axis
    ``sigScanEnded``     -> the scan is over on *every* terminal path
                            (completion, failure, abort), so start reacquiring

    ``sigScanStarted``/``sigScanDone`` are deliberately not used. The first
    arrives after the NI-DAQ tasks are already running, and the second is never
    published at all when a scan fails or is aborted -- which used to leave the
    lock silently suspended for the rest of the session while the button still
    read "Unlock".
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._logger = initLogger(self)

        self._focusAcqHandle = None
        self.__processDataThread = None
        self.__focusCalibThread = None
        self.timer = None
        self._shutdownComplete = False
        self._focusLeaseLock = threading.Lock()
        self._focusCalibrationActive = False

        # Scan arbitration. Depth-counted rather than a boolean: workflows may
        # publish sigScanStarting/sigScanEnded themselves, so a duplicate start
        # must not be undone by a single end, and a stray end must not resume
        # actuation while a scan is still running.
        self._scanSuspendDepth = 0
        self._scanOwnsFocusActuator = True
        self._suspendedLock = False
        self._preScanSetPoint = None
        self._reacquireDeadline = None
        self._reacquireSamples = None
        self._reacquireFailed = False
        self._reacquireDone = threading.Event()
        self._reacquireDone.set()

        if self._setupInfo.focusLock is None:
            return

        self.camera = self._setupInfo.focusLock.camera
        self.positioner = self._setupInfo.focusLock.positioner
        self.positionerAxis = self._resolvePositionerAxis()
        self.updateFreq = self._setupInfo.focusLock.updateFreq
        self.cropFrame = (self._setupInfo.focusLock.frameCropx,
                          self._setupInfo.focusLock.frameCropy,
                          self._setupInfo.focusLock.frameCropw,
                          self._setupInfo.focusLock.frameCroph)
        self._master.detectorsManager[self.camera].crop(*self.cropFrame)
        self._widget.setKp(self._setupInfo.focusLock.piKp)
        self._widget.setKi(self._setupInfo.focusLock.piKi)

        # Connect FocusLockWidget buttons
        self._widget.kpEdit.textChanged.connect(self.unlockFocus)
        self._widget.kiEdit.textChanged.connect(self.unlockFocus)

        self._widget.lockButton.clicked.connect(self.toggleFocus)
        self._widget.camDialogButton.clicked.connect(self.cameraDialog)
        self._widget.focusCalibButton.clicked.connect(self.focusCalibrationStart)
        self._widget.calibCurveButton.clicked.connect(self.showCalibrationCurve)

        self._widget.twoFociBox.stateChanged.connect(self.twoFociVarChange)

        self._commChannel.sigScanStarting.connect(self.scanUnlockFocus)
        self._commChannel.sigScanActuatorsResolved.connect(
            self.scanActuatorsResolved
        )
        self._commChannel.sigScanEnded.connect(self.scanLockFocus)

        self.setPointSignal = 0
        self._lastPIUpdate = None
        self.locked = False
        self.aboutToLock = False
        self.twoFociVar = False
        self.focusTime = 1000 / self.updateFreq  # focus signal update interval (ms)
        self.aboutToLockDiffMax = 0.4
        self.reacquireTimeoutS = float(
            self._setupInfo.focusLock.reacquireTimeoutS
        )
        self.reacquireTolerancePx = float(
            self._setupInfo.focusLock.reacquireTolerancePx
        )
        self.reacquireSampleCount = max(
            2, int(self._setupInfo.focusLock.reacquireSamples)
        )
        self.lockPosition = 0
        self.buffer = 40
        self.currPoint = 0
        self.setPointData = np.zeros(self.buffer)
        self.timeData = np.zeros(self.buffer)

        # FOCUS lease instead of reaching past the DetectorsManager to the
        # sub-manager: the camera is now refcounted, so an unrelated global
        # stop can no longer disarm it underneath the focus lock. FOCUS leases
        # are excluded from the user-visible acquisition signals.
        self.__processDataThread = ProcessDataThread(self)
        self.__focusCalibThread = FocusCalibThread(self)
        self.__focusCalibThread.sigCalibrationFinished.connect(
            self._onFocusCalibrationFinished
        )
        self.__focusCalibThread.finished.connect(
            self._onFocusCalibrationThreadFinished
        )

        self._focusAcqHandle = self._master.detectorsManager.acquire(
            [self.camera], LeasePurpose.FOCUS
        )

        # Start the frame/estimate worker before the timer that consumes it.
        self.__processDataThread.start()

        self.timer = Timer()
        self.timer.timeout.connect(self.update)
        self.timer.start(int(self.focusTime))
        self.startTime = perf_counter()

    def __del__(self):
        try:
            self._shutdown()
        except Exception:
            pass
        try:
            parentDel = getattr(super(), '__del__', None)
            if parentDel is not None:
                parentDel()
        except Exception:
            pass

    def closeEvent(self) -> bool:
        self._shutdown()
        super().closeEvent()
        return self.shutdownComplete()

    def _shutdown(self):
        """Stop all focus work before releasing the camera lease."""
        if self.__dict__.get('_shutdownComplete', False):
            return
        self._shutdownComplete = True

        comm = self.__dict__.get('_commChannel')
        if comm is not None:
            for signalName, slot in (
                ('sigScanStarting', self.scanUnlockFocus),
                ('sigScanActuatorsResolved', self.scanActuatorsResolved),
                ('sigScanEnded', self.scanLockFocus),
            ):
                signal = getattr(comm, signalName, None)
                if signal is not None:
                    try:
                        signal.disconnect(slot)
                    except Exception:
                        pass

        timer = self.__dict__.get('timer')
        if timer is not None:
            try:
                timer.stop()
            except Exception:
                pass

        processThread = self.__dict__.get(
            '_FocusLockController__processDataThread'
        )
        processThreadStopped = True
        for thread in (
            processThread,
            self.__dict__.get('_FocusLockController__focusCalibThread'),
        ):
            if thread is None:
                continue
            try:
                stop = getattr(thread, 'stop', None)
                if stop is not None:
                    stop()
                thread.quit()
                stopped = self._waitForThread(thread)
                if thread is processThread:
                    processThreadStopped = stopped
                if not stopped:
                    self._logger.warning(
                        f'{type(thread).__name__} is still stopping after '
                        f'focus-lock close; shutdown will complete after its '
                        f'active hardware call returns.'
                    )
            except Exception:
                if thread is processThread:
                    processThreadStopped = False

        # FocusCalibThread only moves the stage and reads the last focus
        # signal. ProcessDataThread is the camera owner, so defer the lease only
        # if that thread outlives the bounded close wait.
        if processThreadStopped:
            self._releaseFocusLease()
        elif processThread is not None:
            try:
                processThread.finished.connect(
                    self._releaseFocusLease,
                    QtCore.Qt.DirectConnection,
                )
                if not processThread.isRunning():
                    self._releaseFocusLease()
            except Exception:
                self._logger.warning(
                    'Focus-lock camera worker is still active; retaining its '
                    'detector lease to avoid disarming an in-flight read.'
                )

    @staticmethod
    def _threadIsRunning(thread) -> bool:
        if thread is None:
            return False
        if isinstance(thread, QtCore.QThread) and sip.isdeleted(thread):
            return False
        isRunning = getattr(thread, 'isRunning', None)
        if isRunning is None:
            isAlive = getattr(thread, 'is_alive', None)
            return bool(isAlive()) if isAlive is not None else False
        try:
            return bool(isRunning())
        except RuntimeError:
            return (
                not sip.isdeleted(thread)
                if isinstance(thread, QtCore.QThread)
                else True
            )

    def shutdownComplete(self) -> bool:
        """Return whether camera/calibration workers and their lease are gone."""
        return (
            not self._threadIsRunning(
                self.__dict__.get(
                    '_FocusLockController__processDataThread'
                )
            )
            and not self._threadIsRunning(
                self.__dict__.get(
                    '_FocusLockController__focusCalibThread'
                )
            )
            and self.__dict__.get('_focusAcqHandle') is None
        )

    @staticmethod
    def _waitForThread(thread) -> bool:
        if isinstance(thread, QtCore.QThread):
            if sip.isdeleted(thread):
                return True
            try:
                return bool(
                    QtCore.QThread.wait(thread, _CLOSE_WAIT_TIMEOUT_MS)
                )
            except RuntimeError:
                if sip.isdeleted(thread):
                    return True
                raise
        result = thread.wait()
        return True if result is None else bool(result)

    def _releaseFocusLease(self):
        lock = self.__dict__.get('_focusLeaseLock')
        if lock is None:
            lock = threading.Lock()
            self._focusLeaseLock = lock
        with lock:
            handle = self.__dict__.get('_focusAcqHandle')
            if handle is None:
                return
            try:
                self._master.detectorsManager.release(handle)
            except Exception as e:
                logger = self.__dict__.get('_logger')
                if logger is not None:
                    logger.error(
                        f'Failed to release focus-lock detector lease: {e}',
                        exc_info=True,
                    )
            finally:
                self._focusAcqHandle = None

    # ------------------------------------------------------------------
    # Scan arbitration
    # ------------------------------------------------------------------

    def scanUnlockFocus(self):
        """``sigScanStarting``: yield the actuator before the scan takes it.

        Suspension is unconditional here because participation is not knowable
        yet -- ``sigScanStarting`` is published from ``_beginScanRun``, before
        the scan controller has even read its parameters. Yielding costs
        microseconds when the scan turns out to be harmless
        (``scanActuatorsResolved`` hands it straight back), and is the only
        safe order when it is not.
        """
        if self.__dict__.get('_shutdownComplete', False):
            return
        if not self.scanBlockEnabled():
            return

        self._scanSuspendDepth += 1
        if self._scanSuspendDepth > 1:
            # Already yielded. A nested or duplicate start must not overwrite
            # the setpoint captured when the first one arrived.
            return

        self._scanOwnsFocusActuator = True
        self._reacquireFailed = False
        self._suspendedLock = bool(self.locked) or bool(self.aboutToLock)
        if self.locked and getattr(self, 'pi', None) is not None:
            self._preScanSetPoint = self.pi.setPoint
        elif self._preScanSetPoint is None:
            self._preScanSetPoint = self.setPointSignal

        self.locked = False
        # Finding 5: without this, a pending "about to lock" kept running
        # through the scan and could re-engage mid-waveform, capturing its
        # setpoint from a scan-displaced position.
        self.aboutToLock = False
        self._lastPIUpdate = None
        self._endReacquire(notify=False)
        self._cancelCalibrationForScan()
        self._publishFocusLockState()

    def _cancelCalibrationForScan(self):
        """Abandon a calibration sweep that a scan is about to interrupt.

        Calibration drives the focus axis with absolute moves from a worker
        thread and had no scan interlock at all, so it would keep stepping
        straight through the waveform.
        """
        calibThread = self.__dict__.get(
            '_FocusLockController__focusCalibThread'
        )
        if calibThread is None:
            return
        try:
            if not calibThread.isRunning():
                return
            calibThread.stopAndYieldActuator()
        except Exception:
            self._logger.error(
                'Failed to cancel focus calibration for an imminent scan',
                exc_info=True,
            )

    def scanActuatorsResolved(self, actuators):
        """``sigScanActuatorsResolved``: hand the actuator back if it is safe.

        Only ever *releases* a suspension. A scan that cannot reach our axis
        never needed one, and this normally lands in the same synchronous call
        stack as the suspend, so the lock is not meaningfully interrupted.
        """
        if self.__dict__.get('_shutdownComplete', False):
            return
        if self._scanSuspendDepth <= 0 or not self._scanOwnsFocusActuator:
            return
        if self.scanTouchesFocusActuator(actuators):
            return

        self._scanOwnsFocusActuator = False
        if self._suspendedLock:
            # Nothing moved our axis, so there is nothing to reacquire.
            self._reengageLock(setPoint=self._preScanSetPoint)
        self._publishFocusLockState()

    def scanLockFocus(self):
        """``sigScanEnded``: the scan is over on every terminal path."""
        if self.__dict__.get('_shutdownComplete', False):
            return
        if self._scanSuspendDepth <= 0:
            # A run we never yielded to (suspension disabled, or an unpaired
            # end from an external workflow). Resuming here would re-engage the
            # loop while some other scan is still driving the actuator.
            return

        self._scanSuspendDepth -= 1
        if self._scanSuspendDepth > 0:
            return

        suspendedLock = self._suspendedLock
        conflicted = self._scanOwnsFocusActuator
        self._suspendedLock = False
        self._scanOwnsFocusActuator = True

        if not suspendedLock:
            self._preScanSetPoint = None
            self._publishFocusLockState()
            return
        if not conflicted:
            # Released early by scanActuatorsResolved; already re-engaged.
            self._preScanSetPoint = None
            self._publishFocusLockState()
            return
        self._beginReacquire()

    def scanBlockEnabled(self) -> bool:
        """Whether scans may suspend the lock.

        Defaults to enabled. The widget's checkbox is now an opt-*out*: it used
        to be an unchecked, unpersisted opt-in, so a fresh session silently
        allowed an active lock to fight a Z scan.
        """
        widget = self.__dict__.get('_widget')
        scanBlock = getattr(widget, 'ScanBlock', None)
        if scanBlock is None:
            return True
        try:
            return bool(scanBlock.isChecked())
        except Exception:
            return True

    def scanTouchesFocusActuator(self, actuators) -> bool:
        """Whether ``actuators`` can reach the actuator the lock drives.

        Conservative by construction: anything unknown counts as a conflict.

        1. The lock's own positioner is in the list.
        2. Both sides declare ``physicalActuator`` and the ids match. Declaring
           *different* ids is the explicit way to say two Z positioners really
           are separate devices, and skips rule 3 for that positioner.
        3. Otherwise, a scanned positioner carries the lock's axis. This is
           what catches the case the whole mechanism exists for -- an analog
           scanner and a serial positioner addressing one physical piezo under
           two names -- with no configuration at all.
        """
        names = [str(name) for name in (actuators or [])
                 if name and str(name) != 'None']
        if not names:
            return True
        if self.positioner in names:
            return True

        focusActuator = self._physicalActuatorOf(self.positioner)
        for name in names:
            otherActuator = self._physicalActuatorOf(name)
            if focusActuator is not None and otherActuator is not None:
                if focusActuator == otherActuator:
                    return True
                continue
            if self._carriesFocusAxis(name):
                return True
        return False

    def _physicalActuatorOf(self, positionerName):
        info = self._setupInfo.positioners.get(positionerName)
        actuator = getattr(info, 'physicalActuator', None) if info else None
        return str(actuator) if actuator else None

    def _focusAxisName(self):
        """The lock's axis as a name, resolving an index against its own axes."""
        axis = self.positionerAxis
        if not isinstance(axis, int):
            return str(axis) if axis is not None else None
        ownInfo = self._setupInfo.positioners.get(self.positioner)
        ownAxes = list(getattr(ownInfo, 'axes', None) or [])
        return str(ownAxes[axis]) if 0 <= axis < len(ownAxes) else None

    def _carriesFocusAxis(self, positionerName) -> bool:
        info = self._setupInfo.positioners.get(positionerName)
        if info is None:
            return True
        axisName = self._focusAxisName()
        if axisName is None:
            return True
        return axisName in [str(a) for a in (getattr(info, 'axes', None) or [])]

    # ------------------------------------------------------------------
    # Reacquisition barrier
    # ------------------------------------------------------------------

    def _beginReacquire(self):
        """Wait for the signal to come back before correcting against it."""
        self._reacquireSamples = np.full(self.reacquireSampleCount, np.nan)
        self._reacquireDeadline = perf_counter() + self.reacquireTimeoutS
        self._reacquireFailed = False
        self._reacquireDone.clear()
        self.aboutToLock = True
        self._publishFocusLockState()

    def _endReacquire(self, *, notify=True):
        """Leave the reacquiring state, however it ended.

        Deliberately does not touch ``_preScanSetPoint``: callers own that,
        and one of them (``scanUnlockFocus``) captures it immediately before
        calling here.
        """
        self.aboutToLock = False
        self._reacquireDeadline = None
        self._reacquireSamples = None
        self._reacquireDone.set()
        if notify:
            self._publishFocusLockState()

    def _reengageLock(self, setPoint=None):
        """Re-arm with a *fresh* controller, never a retained one.

        Flipping ``locked`` back on kept the pre-scan integrator: its retained
        output was applied again as a relative move, and its stale
        ``lastError`` turned the entire scan-induced excursion into one
        derivative kick on the first tick -- reliably large enough to trip the
        safety threshold, and before Phase 1 to be issued as a real move.
        """
        target = self.setPointSignal if setPoint is None else setPoint
        try:
            kp = float(self._widget.kpEdit.text())
            ki = float(self._widget.kiEdit.text())
        except (ValueError, AttributeError):
            self._logger.warning(
                'Focus lock could not read its PI gains while re-engaging; '
                'leaving the lock off.'
            )
            self._endReacquire()
            return

        self.pi = PI(target, 0.001, kp, ki, nominalDt=self.focusTime / 1000.0)
        self._lastPIUpdate = None
        try:
            self.lockPosition = self.getPositionerAbs()
        except Exception as e:
            self._logger.warning(
                f'Focus lock could not read the positioner while re-engaging: {e}'
            )
        self.locked = True
        self._reacquireFailed = False
        self._endReacquire(notify=False)
        try:
            self._widget.lockButton.setChecked(True)
            self._widget.lockButton.setText('Unlock')
        except Exception:
            pass
        self._publishFocusLockState()

    def focusLockState(self) -> str:
        """Current lifecycle state -- see the ``STATE_*`` constants."""
        if self.locked:
            return STATE_LOCKED
        if self.aboutToLock:
            return STATE_REACQUIRING
        if self.__dict__.get('_scanSuspendDepth', 0) > 0:
            return STATE_SUSPENDED
        if self.__dict__.get('_reacquireFailed', False):
            # Distinct from UNLOCKED on purpose: the operator asked for a lock
            # and does not have one, which is not the same as never asking.
            return STATE_REACQUIRE_FAILED
        return STATE_UNLOCKED

    def waitForFocusReacquired(self, timeoutS: float = 10.0) -> bool:
        """Block until the lock is re-engaged, or reacquisition gives up.

        For orchestrators -- a tiling run that must not start the next tile
        against an unfocused sample. Returns whether the lock is actually
        holding. Never call this from the GUI thread: the barrier is advanced
        by ``update``, which runs there.
        """
        done = self.__dict__.get('_reacquireDone')
        if done is not None:
            done.wait(timeoutS)
        return bool(self.locked)

    def _publishFocusLockState(self):
        state = self.focusLockState()
        if self.__dict__.get('_lastPublishedState') == state:
            return
        self._lastPublishedState = state
        try:
            self._widget.setLockState(state)
        except AttributeError:
            pass
        except Exception:
            self._logger.error(
                'Failed to display the focus-lock state', exc_info=True
            )

    def unlockFocus(self):
        if self.locked:
            self.locked = False
            self._lastPIUpdate = None
            self._widget.lockButton.setChecked(False)
            lineLock = getattr(
                self._widget.focusLockGraph, 'lineLock', None
            )
            if lineLock is not None:
                # Only ever added by lockFocus; a lock re-engaged after a scan
                # reuses the existing line rather than adding a second one.
                self._widget.focusPlot.removeItem(lineLock)
        # An explicit unlock also abandons a reacquisition in flight, and stops
        # the scan handler from re-engaging a lock the user has since dropped.
        self._suspendedLock = False
        self._preScanSetPoint = None
        self._reacquireFailed = False
        self._endReacquire()

    def toggleFocus(self):
        self.aboutToLock = False
        self._suspendedLock = False
        if self._widget.lockButton.isChecked():
            zpos = self.getPositionerAbs()
            self.lockFocus(zpos)
            self._widget.lockButton.setText('Unlock')
        else:
            self.unlockFocus()
            self._widget.lockButton.setText('Lock')

    def cameraDialog(self):
        self._master.detectorsManager[self.camera].openPropertiesDialog()

    def focusCalibrationStart(self):
        if self.__dict__.get('_shutdownComplete', False):
            return
        if self.__dict__.get('_scanSuspendDepth', 0) > 0:
            # Calibration sweeps the focus axis with ~20 absolute moves. Doing
            # that while a scan owns the actuator is the same conflict the
            # lock itself yields for, minus the yielding.
            self._logger.warning(
                'Focus calibration cannot start while a scan owns the focus '
                'axis. Wait for the scan to finish.'
            )
            return
        try:
            fromVal = float(self._widget.calibFromEdit.text())
            toVal = float(self._widget.calibToEdit.text())
        except ValueError:
            self._logger.warning(
                'Focus calibration range must contain numeric values.'
            )
            return
        if not np.isfinite(fromVal) or not np.isfinite(toVal):
            self._logger.warning(
                'Focus calibration range must contain finite values.'
            )
            return
        if fromVal == toVal:
            self._logger.warning(
                'Focus calibration range must span two different positions.'
            )
            return
        if self.__focusCalibThread.isRunning():
            self._logger.warning(
                'Focus calibration is already running; duplicate start ignored.'
            )
            return
        self.__focusCalibThread.configure(fromVal, toVal)
        self._focusCalibrationActive = True
        try:
            self._widget.focusCalibButton.setEnabled(False)
        except Exception:
            pass
        try:
            self.__focusCalibThread.start()
        except Exception:
            self._focusCalibrationActive = False
            try:
                self._widget.focusCalibButton.setEnabled(True)
            except Exception:
                pass
            raise

    def _onFocusCalibrationFinished(self, cal_nm):
        """Apply worker results on the controller/widget thread."""
        if self.__dict__.get('_shutdownComplete', False):
            return
        self._widget.calibrationDisplay.setText(f'1 px --> {cal_nm} nm')

    def _onFocusCalibrationThreadFinished(self):
        self._focusCalibrationActive = False
        if self.__dict__.get('_shutdownComplete', False):
            return
        try:
            self._widget.focusCalibButton.setEnabled(True)
        except Exception:
            pass

    def showCalibrationCurve(self):
        self._widget.showCalibrationCurve(self.__focusCalibThread.getData())

    def twoFociVarChange(self):
        if self.twoFociVar:
            self.twoFociVar = False
        else:
            self.twoFociVar = True

    def update(self):
        """Timer tick: grab, estimate, and correct focus.

        Frame acquisition and the focus estimate run on a worker thread, so a
        busy GUI thread (a tiling scan repainting its mosaic, say) can no
        longer stall the lock. This method only consumes the newest completed
        estimate and touches the widgets.
        """
        if self.__dict__.get('_shutdownComplete', False):
            return

        result = self.__processDataThread.takeResult()
        if result is None:
            # No estimate has completed since the last tick — either the camera
            # has not produced a frame yet, or the worker is still busy. Skip
            # rather than block the GUI thread waiting for it.
            return

        img, setPointSignal, timestamp = result
        self.setPointSignal = setPointSignal
        self._widget.center.setValue(self.setPointSignal)
        # move
        if self._focusCalibrationActive:
            pass
        elif self.locked:
            value_move = self.updatePI(timestamp)
            # updatePI can drop the lock mid-tick (safety trip, z-step
            # handover). Re-read the flag rather than trusting the branch this
            # tick was entered on, so a correction is never applied to a lock
            # that no longer exists.
            if self.locked and abs(value_move) > 0.002:
                self.movePositioner(value_move)
        elif self.aboutToLock:
           self.aboutToLockUpdate()
        # udpate graphics
        self.updateSetPointData()
        self._widget.camImg.setImage(img)
        if self.currPoint < self.buffer:
            self._widget.focusPlotCurve.setData(self.timeData[1:self.currPoint],
                                                self.setPointData[1:self.currPoint])
        else:
            self._widget.focusPlotCurve.setData(self.timeData, self.setPointData)

    def aboutToLockUpdate(self):
        """Advance the reacquisition barrier by one focus estimate.

        The old test was variance-only, so it re-engaged as soon as the signal
        stopped *moving* -- including when it had settled at whatever position
        the scan left it in. It now also requires the signal to be back within
        tolerance of the setpoint the lock was holding before it yielded, and
        gives up after a bounded wait instead of waiting forever.

        Giving up leaves the lock off. Re-engaging against a signal that never
        came back is how a whole tiling run gets acquired out of focus.
        """
        if self._reacquireSamples is None:
            self._reacquireSamples = np.full(
                self.reacquireSampleCount, np.nan
            )
        self._reacquireSamples = np.roll(self._reacquireSamples, 1)
        self._reacquireSamples[0] = self.setPointSignal

        if not np.isnan(self._reacquireSamples).any():
            settled = np.std(self._reacquireSamples) < self.aboutToLockDiffMax
            target = self._preScanSetPoint
            offset = (
                0.0 if target is None
                else abs(float(np.mean(self._reacquireSamples)) - target)
            )
            if settled and offset <= self.reacquireTolerancePx:
                self._reengageLock(setPoint=target)
                return

        if (
            self._reacquireDeadline is not None
            and perf_counter() > self._reacquireDeadline
        ):
            self._logger.warning(
                f'Focus lock did not reacquire within '
                f'{self.reacquireTimeoutS:.2f} s of the scan ending; leaving '
                f'the lock off. Raise reacquireTimeoutS if the piezo needs '
                f'longer to settle, or reacquireTolerancePx if it settles '
                f'off-setpoint by design.'
            )
            self._preScanSetPoint = None
            self._endReacquire(notify=False)
            self._reacquireFailed = True
            self._publishFocusLockState()
            try:
                self._widget.lockButton.setChecked(False)
                self._widget.lockButton.setText('Lock')
            except Exception:
                pass

    def updateSetPointData(self):
        if self.currPoint < self.buffer:
            self.setPointData[self.currPoint] = self.setPointSignal
            self.timeData[self.currPoint] = perf_counter() - self.startTime
        else:
            self.setPointData = np.roll(self.setPointData, -1)
            self.setPointData[-1] = self.setPointSignal
            self.timeData = np.roll(self.timeData, -1)
            self.timeData[-1] = perf_counter() - self.startTime
        self.currPoint += 1

    def updatePI(self, timestamp=None):
        # Feed the true interval since the last correction. The integral term
        # is a rate, so with a fixed implicit dt the loop silently detuned
        # whenever ticks were delayed or dropped — exactly when the GUI thread
        # was busy and the lock most needed to keep up.
        dt = None
        if timestamp is not None:
            if self._lastPIUpdate is not None:
                dt = timestamp - self._lastPIUpdate
            self._lastPIUpdate = timestamp

        move = self.pi.update(self.setPointSignal, dt)

        if abs(move) > 3:
            self._logger.warning(f'Safety unlocking! Current move step: {move:.3f}.')
            self.unlockFocus()
            # Report no motion, not the step that tripped the guard. The caller
            # applies whatever this returns, so returning `move` here made the
            # safety unlock self-defeating: it dropped the lock and then issued
            # the very oversized move it exists to prevent, on the way out.
            return 0.0
        return move

    def lockFocus(self, zpos):
        if not self.locked:
            kp = float(self._widget.kpEdit.text())
            ki = float(self._widget.kiEdit.text())
            self.pi = PI(self.setPointSignal, 0.001, kp, ki,
                         nominalDt=self.focusTime / 1000.0)
            self._lastPIUpdate = None
            self.lockPosition = zpos
            self.locked = True
            self._widget.focusLockGraph.lineLock = self._widget.focusPlot.addLine(
                y=self.setPointSignal, pen='r'
            )
            self._widget.lockButton.setChecked(True)

    def _resolvePositionerAxis(self):
        """Resolve which axis to use for focus-lock movements."""
        positionerAxis = getattr(self._setupInfo.focusLock, 'positionerAxis', None)
        if positionerAxis is not None:
            return positionerAxis

        positionerManager = self._master.positionersManager[self.positioner]
        if 'Z' in positionerManager.axes:
            return 'Z'

        return 0

    def getPositionerAbs(self):
        """Get absolute position from the configured positioner axis."""
        return self._master.positionersManager[self.positioner].get_abs(self.positionerAxis)

    def movePositioner(self, value):
        """Move the configured positioner axis."""
        self._master.positionersManager[self.positioner].move(value, self.positionerAxis)

    def setPositionerAbs(self, value):
        """Move the configured positioner axis to an absolute coordinate."""
        self._master.positionersManager[self.positioner].setPosition(
            value, self.positionerAxis
        )


class ProcessDataThread(Thread):
    """Grabs focus-camera frames and computes the focus signal off the GUI thread.

    This class used to be constructed but never started, so its gaussian
    filter, peak search and centre-of-mass all executed inside the Qt event
    loop via the controller's timer. Any other GUI-thread work — a tiling scan
    repainting its mosaic being the worst offender — directly delayed the
    focus correction. It now runs as a real worker: it publishes the newest
    completed estimate, and the controller's timer consumes whatever is ready.
    """

    def __init__(self, controller, *args, **kwargs):
        self._controller = controller
        self._stopRequested = threading.Event()
        self._resultLock = threading.Lock()
        self._result = None
        self.latestimg = None
        super().__init__(*args, **kwargs)

    def stop(self):
        self._stopRequested.set()

    def takeResult(self):
        """Pop the newest ``(image, setPointSignal, timestamp)``, or None."""
        with self._resultLock:
            result = self._result
            self._result = None
            return result

    def run(self):
        while not self._stopRequested.is_set():
            iterationStart = perf_counter()
            try:
                img = self.grabCameraFrame()
                if img is None:
                    if self._stopRequested.wait(0.01):
                        break
                    continue
                setPointSignal = self.update(self._controller.twoFociVar)
            except Exception as e:
                self._controller._logger.error(
                    f'Focus signal update failed: {e}', exc_info=True
                )
                if self._stopRequested.wait(0.1):
                    break
                continue

            with self._resultLock:
                self._result = (img, setPointSignal, perf_counter())

            # Pace the worker to the configured update rate instead of
            # spinning: the camera cannot deliver useful new information
            # faster than it delivers frames.
            processingTime = perf_counter() - iterationStart
            waitTime = max(
                0.001,
                self._controller.focusTime / 1000.0 - processingTime,
            )
            if self._stopRequested.wait(waitTime):
                break

    def grabCameraFrame(self):
        detectorManager = self._controller._master.detectorsManager[self._controller.camera]
        sharedImage = detectorManager.getLatestFrameShared()
        if sharedImage is None:
            return None
        # The detector may recycle its shared buffer while this worker applies
        # filters. Work on an owned snapshot so one estimate never combines
        # pixels from two camera frames.
        self.latestimg = np.array(sharedImage, copy=True)
        # 1.5 swap axes of frame (depending on setup, make this a variable in the json)
        if self._controller._setupInfo.focusLock.swapImageAxes:
            self.latestimg = np.swapaxes(self.latestimg,0,1)
        return self.latestimg

    def update(self, twoFociVar):
        # Gaussian filter the image, to remove noise and so on, to get a better center estimate
        imagearraygf = ndi.gaussian_filter(self.latestimg, 7)

        # Update the focus signal
        if twoFociVar:
            allmaxcoords = peak_local_max(imagearraygf, min_distance=60)
            size = allmaxcoords.shape
            maxvals = np.zeros(size[0])
            maxvalpos = np.zeros(2)
            for n in range(0, size[0]):
                if imagearraygf[allmaxcoords[n][0], allmaxcoords[n][1]] > maxvals[0]:
                    if imagearraygf[allmaxcoords[n][0], allmaxcoords[n][1]] > maxvals[1]:
                        tempval = maxvals[1]
                        maxvals[0] = tempval
                        maxvals[1] = imagearraygf[allmaxcoords[n][0], allmaxcoords[n][1]]
                        tempval = maxvalpos[1]
                        maxvalpos[0] = tempval
                        maxvalpos[1] = n
                    else:
                        maxvals[0] = imagearraygf[allmaxcoords[n][0], allmaxcoords[n][1]]
                        maxvalpos[0] = n
            xcenter = allmaxcoords[int(maxvalpos[0])][0]
            ycenter = allmaxcoords[int(maxvalpos[0])][1]
            if allmaxcoords[int(maxvalpos[1])][0] < xcenter:
                xcenter = allmaxcoords[int(maxvalpos[1])][0]
                ycenter = allmaxcoords[int(maxvalpos[1])][1]
            centercoords2 = np.array([xcenter, ycenter])
        else:
            centercoords = np.where(imagearraygf == np.array(imagearraygf.max()))
            centercoords2 = np.array([centercoords[0][0], centercoords[1][0]])

        subsizey = 50
        subsizex = 50
        # Clamp to the frame's real shape. These bounds were hardcoded to
        # 1024/1280, which silently truncated the sub-window (or missed part of
        # it) on any focus camera or crop that is not exactly that size.
        frame_h, frame_w = imagearraygf.shape[:2]
        xlow = max(0, (centercoords2[0] - subsizex))
        xhigh = min(frame_h, (centercoords2[0] + subsizex))
        ylow = max(0, (centercoords2[1] - subsizey))
        yhigh = min(frame_w, (centercoords2[1] + subsizey))

        imagearraygfsub = imagearraygf[xlow:xhigh, ylow:yhigh]
        massCenter = np.array(ndi.center_of_mass(imagearraygfsub))
        # add the information about where the center of the subarray is
        massCenterGlobal = massCenter[0] + centercoords2[0]  # - subsizey - self.sensorSize[1] / 2
        #self._controller._widget.center.setValue(massCenterGlobal)
        #print(massCenterGlobal)
        return massCenterGlobal


class FocusCalibThread(Thread):
    sigCalibrationFinished = Signal(float)

    def __init__(self, controller, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._controller = controller
        self._stopRequested = threading.Event()
        self._yieldActuator = threading.Event()
        self._dataLock = threading.Lock()
        self.fromVal = None
        self.toVal = None
        self.scan_list = np.array([])
        self.signalData = []
        self.positionData = []
        self.poly = np.array([])
        self.calibrationResult = np.array([])

    def configure(self, fromVal, toVal):
        self.fromVal = float(fromVal)
        self.toVal = float(toVal)
        self._stopRequested.clear()
        self._yieldActuator.clear()

    def stop(self):
        self._stopRequested.set()

    def stopAndYieldActuator(self):
        """Cancel without the usual restore-to-start move.

        Normal cancellation ends by driving the axis back to where calibration
        found it. That is exactly wrong when the reason for cancelling is that
        a scan has taken the actuator: the restore would be one more command
        fighting the scan waveform, issued from a worker thread. The scan owns
        the axis from here, so this abandons the sweep where it stands and
        lets the scan's own return-to-center define the final position.
        """
        self._yieldActuator.set()
        self._stopRequested.set()

    def run(self):
        if self.fromVal is None or self.toVal is None:
            return

        signalData = []
        positionData = []
        scan_list = np.round(np.linspace(self.fromVal, self.toVal, 20), 2)
        startPosition = None
        completed = False
        failed = False
        cancelled = False
        poly = None
        calibrationResult = None
        cal_nm = None

        def setAbsolutePosition(target):
            setter = getattr(self._controller, 'setPositionerAbs', None)
            if setter is not None:
                setter(target)
                return
            current = self._controller.getPositionerAbs()
            self._controller.movePositioner(target - current)

        try:
            startPosition = self._controller.getPositionerAbs()
            for offset in scan_list:
                if self._stopRequested.is_set():
                    cancelled = True
                    break
                setAbsolutePosition(startPosition + offset)
                # Event.wait makes the former fixed sleep cooperatively
                # cancellable during controller shutdown.
                if self._stopRequested.wait(0.5):
                    cancelled = True
                    break
                signalData.append(self._controller.setPointSignal)
                positionData.append(self._controller.getPositionerAbs())

            if not cancelled and not self._stopRequested.is_set():
                poly = np.polyfit(positionData, signalData, 1)
                if poly[0] == 0:
                    raise ValueError('focus calibration slope is zero')
                calibrationResult = np.around(poly, 4)
                cal_nm = float(np.round(1000 / poly[0], 1))
                completed = True
            else:
                cancelled = True
        except Exception as e:
            failed = True
            self._controller._logger.error(
                f'Focus calibration failed: {e}', exc_info=True
            )
        finally:
            if self._yieldActuator.is_set():
                # A scan took the axis. Restoring our own start position here
                # would be one more command fighting the scan waveform.
                self._controller._logger.warning(
                    'Focus calibration was cancelled because a scan took the '
                    'focus axis; its results are discarded and the axis is '
                    'left to the scan.'
                )
            elif startPosition is not None:
                try:
                    setAbsolutePosition(startPosition)
                except Exception as e:
                    failed = True
                    self._controller._logger.error(
                        f'Focus calibration could not restore its starting '
                        f'position: {e}',
                        exc_info=True,
                    )

        if failed or cancelled or not completed:
            return
        with self._dataLock:
            self.scan_list = scan_list
            self.signalData = signalData
            self.positionData = positionData
            self.poly = poly
            self.calibrationResult = calibrationResult

        if not self._stopRequested.is_set():
            # The receiver lives on the GUI thread, so Qt queues the widget
            # update instead of touching it from this worker thread.
            self.sigCalibrationFinished.emit(cal_nm)

    def getData(self):
        with self._dataLock:
            return {
                'signalData': list(self.signalData),
                'positionData': list(self.positionData),
                'poly': np.array(self.poly, copy=True),
            }


class PI:
    """Simple implementation of a discrete PI controller.
    Taken from http://code.activestate.com/recipes/577231-discrete-pid-controller/
    Author: Federico Barabas"""
    def __init__(self, setPoint, multiplier=1, kp=0, ki=0, nominalDt=0.0,
                 maxIntegralScale=5.0):
        self._kp = multiplier * kp
        self._ki = multiplier * ki
        self._setPoint = setPoint
        self.multiplier = multiplier
        self.error = 0.0
        self._started = False
        # Interval the gains were tuned at. dt-scaling is relative to this, so
        # existing kp/ki values keep their meaning when the loop runs on time.
        self.nominalDt = nominalDt
        self.maxIntegralScale = maxIntegralScale

    def update(self, currentValue, dt=None):
        """ Calculate PI output value for given reference input and feedback.
        Using the iterative formula to avoid integrative part building.

        ``dt`` is the interval in seconds since the previous update. The
        integral term is scaled by it so the loop behaves the same whether it
        is running at its nominal rate or has been delayed; pass None to keep
        the historical fixed-interval behaviour. """
        self.error = self.setPoint - currentValue
        # Guard against a stalled or non-monotonic clock producing a huge or
        # negative integral kick after a delay.
        scale = 1.0
        if dt is not None and self.nominalDt > 0:
            scale = min(max(dt, 0.0) / self.nominalDt, self.maxIntegralScale)
        if self.started:
            self.dError = self.error - self.lastError
            self.out = self.out + self.kp * self.dError + self.ki * self.error * scale
        else:
            # This only runs in the first step
            self.out = self.kp * self.error
            self.started = True
        self.lastError = self.error
        return self.out

    def restart(self):
        self.started = False

    @property
    def started(self):
        return self._started

    @started.setter
    def started(self, value):
        self._started = value

    @property
    def setPoint(self):
        return self._setPoint

    @setPoint.setter
    def setPoint(self, value):
        self._setPoint = value

    @property
    def kp(self):
        return self._kp

    @kp.setter
    def kp(self, value):
        self._kp = value

    @property
    def ki(self):
        return self._ki

    @ki.setter
    def ki(self, value):
        self._ki = value


# Copyright (C) 2020-2021 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
