import threading
from time import monotonic, sleep

import numpy as np
from qtpy import QtCore

from imswitch.imcommon.framework import Signal, SignalInterface, Thread, Timer, Worker
from ._acquisition_leases import (FRAME_STREAM_PURPOSES, AcquisitionLeaseTable,
                                  DetectorFaultedError, LeaseHandle, LeasePurpose)
from .MultiManager import MultiManager


POLL_THREAD_STOP_TIMEOUT_MS = 5000
DETECTOR_STOP_TIMEOUT_MS = 2000


class DetectorsManager(MultiManager, SignalInterface):
    """ DetectorsManager is an interface for dealing with DetectorManagers. It
    is a MultiManager for detectors, and the sole authority over which
    detectors participate in ImSwitch-managed acquisition: a detector is armed
    iff it holds at least one acquisition lease (see _acquisition_leases). """

    sigAcquisitionStarted = Signal()
    sigAcquisitionStopped = Signal()
    sigDetectorSwitched = Signal(str, str)  # (newDetectorName, oldDetectorName)
    sigImageUpdated = Signal(
        str, np.ndarray, bool, list, bool
    )  # (detectorName, image, init, scale, isCurrentDetector)
    sigNewFrame = Signal()

    def __init__(self, detectorInfos, updatePeriod, **lowLevelManagers):
        MultiManager.__init__(self, detectorInfos, 'detectors', **lowLevelManagers)
        SignalInterface.__init__(self)

        # Serializes manager-level frame-stream transitions without involving
        # the lease-table lock. It must be re-entrant because synchronously
        # connected acquisition signals may call back into this manager.
        self._frameStreamLifecycleLock = threading.RLock()
        # Held while the poller reads hardware and while a non-last streaming
        # lease is released. This prevents stopAcquisition() racing a stale
        # membership snapshot while other streamers keep the thread alive.
        self._framePollLock = threading.RLock()
        self._detectorStopLock = threading.RLock()
        self._detectorStopOperations = {}

        self._leaseTable = AcquisitionLeaseTable(
            startDetector=lambda name: self._subManagers[name].startAcquisition(),
            stopDetector=self._stopDetectorBounded,
            onStateChanged=self.__onLeaseStateChanged,
        )

        self._currentDetectorName = None
        for detectorName, detectorInfo in detectorInfos.items():
            if not self._subManagers[detectorName].forAcquisition:
                continue
            # Connect signals
            self._subManagers[detectorName].sigImageUpdated.connect(
                lambda image, init, scale, detectorName=detectorName: self.sigImageUpdated.emit(
                    detectorName, image, init, scale, detectorName==self._currentDetectorName
                )
            )
            self._subManagers[detectorName].sigNewFrame.connect(lambda: self.sigNewFrame.emit())

            # Set as default if first detector
            if self._currentDetectorName is None:
                self._currentDetectorName = detectorName

        # A timer will collect the new frame and update it through the communication channel
        self._lvWorker = LVWorker(self, updatePeriod)
        self._thread = Thread()
        self._lvWorker.moveToThread(self._thread)
        self._thread.started.connect(self._lvWorker.run)
        self._thread.finished.connect(self._lvWorker.stop)

    def __del__(self):
        try:
            self.__stopPollThread()
        except Exception:
            # Explicit release/close paths surface the timeout. Destructors
            # cannot do so safely.
            pass
        if hasattr(super(), '__del__'):
            super().__del__()

    def getCurrentDetectorName(self):
        """ Returns the name of the current detector. """

        if not self.hasDevices():
            raise NoDetectorsError

        return self._currentDetectorName

    def getCurrentDetector(self):
        """ Returns the current detector. """

        if not self.hasDevices():
            raise NoDetectorsError

        return self._subManagers[self._currentDetectorName]

    def setCurrentDetector(self, detectorName):
        """ Sets the current detector by its name. """

        self._validateManagedDeviceName(detectorName)

        oldDetectorName = self._currentDetectorName
        self._currentDetectorName = detectorName
        self.sigDetectorSwitched.emit(detectorName, oldDetectorName)

        # A running poller does not imply that the newly selected detector is
        # armed: EVENT_STREAM may be keeping the thread alive for a different
        # detector while live view is off. Keep this eager refresh inside the
        # same read/stop exclusion as the normal poll loop and never touch a
        # detector outside current frame-stream membership.
        with self._framePollLock:
            if (
                self._thread.isRunning()
                and detectorName in self.frameStreamMembership()
            ):
                self.execOnCurrent(lambda c: c.updateLatestFrame(True))

    def execOnCurrent(self, func):
        """ Executes a function on the current detector and returns the result. """
        if not self.hasDevices():
            raise NoDetectorsError

        return self.execOn(self._currentDetectorName, func)

    def acquire(self, detectorNames, purpose: LeasePurpose) -> LeaseHandle:
        """ Takes an acquisition lease on the given detectors with the given
        purpose, arming any detector that is not already armed. An explicit
        empty iterable is rejected (never silently "all detectors"); acquiring
        a FAULTED detector raises DetectorFaultedError. Returns a handle to
        pass to release(). """
        names = list(dict.fromkeys(detectorNames))
        for detectorName in names:
            self._validateManagedDeviceName(detectorName)
        return self.__acquireImpl(names, purpose, allowEmpty=False)

    def release(self, handle: LeaseHandle) -> None:
        """ Releases an acquisition lease; any detector whose last lease this
        was is stopped. A detector whose stop fails is marked FAULTED and
        quarantined until retryStop() succeeds. """
        with self._frameStreamLifecycleLock:
            # Validate before quit()/wait(): an unknown or already released
            # token must never disrupt legitimate streamers.
            if not self._leaseTable.isActiveHandle(handle):
                raise ValueError('Invalid or already used handle')

            if handle.purpose in FRAME_STREAM_PURPOSES:
                streamHandles = self._leaseTable.frameStreamHandles()
                if len(streamHandles) == 1:
                    # Join before taking the lease-table lock. Acquires and
                    # releases cannot slip into this gap because they use the
                    # lifecycle lock, while the poller remains free to finish
                    # its final membership read.
                    self.__stopPollThread()
                    transition = self._leaseTable.release(handle)
                else:
                    # The thread remains alive for other streamers. Wait for
                    # any poll using the old membership to finish, then remove
                    # the lease and stop its newly-unleased detectors while no
                    # hardware read can overlap.
                    with self._framePollLock:
                        transition = self._leaseTable.release(handle)

                if self.frameStreamMembership():
                    # Self-heal an unexpectedly stopped poller when streaming
                    # membership remains.
                    self.__ensurePollThreadForMembership()
                else:
                    # Multiple legacy empty handles can make this a non-last
                    # handle release even though actual membership is empty.
                    self.__stopPollThread()
            else:
                transition = self._leaseTable.release(handle)
            # Keep global start/stop notification ordered with the serialized
            # lease transition. The lease-table lock is already released, and
            # the lifecycle lock is re-entrant for synchronous callbacks.
            self.__emitTransitionSignals(transition)

    def __stopPollThread(self) -> None:
        """Stop and join the poller without holding the lease-table lock."""
        if not self._thread.isRunning():
            return
        self._thread.quit()
        if isinstance(self._thread, QtCore.QThread):
            stopped = QtCore.QThread.wait(
                self._thread, POLL_THREAD_STOP_TIMEOUT_MS
            )
        else:
            result = self._thread.wait()
            stopped = True if result is None else bool(result)
        if not stopped:
            raise TimeoutError(
                'Detector frame poller did not stop within '
                f'{POLL_THREAD_STOP_TIMEOUT_MS / 1000:g} seconds'
            )

    def __ensurePollThreadForMembership(self) -> None:
        """Run the poller exactly when non-empty frame membership requires it."""
        if not self.frameStreamMembership() or self._thread.isRunning():
            return
        self._thread.start()

    def retryStop(self, detectorName: str) -> None:
        """ Explicit recovery for a FAULTED detector: retries the hardware
        stop. On success the fault is cleared; on failure the stop exception
        propagates and the detector stays quarantined. """
        self._validateManagedDeviceName(detectorName)
        self._leaseTable.retryStop(detectorName)

    def _runDetectorStop(self, detectorName, operation) -> None:
        try:
            self._subManagers[detectorName].stopAcquisition()
        except Exception as error:
            operation['error'] = error
        finally:
            operation['event'].set()

    def _stopDetectorBounded(self, detectorName: str) -> None:
        """Serialize one detector SDK stop and wait only to a deadline.

        A timed-out operation remains strongly referenced and is joined by a
        later ``retryStop`` rather than duplicated. The lease table marks the
        detector FAULTED while the native state is unknown, so acquisition is
        rejected even if the background SDK call eventually returns.
        """
        with self._detectorStopLock:
            operation = self._detectorStopOperations.get(detectorName)
            joinedExisting = operation is not None
            if operation is None:
                operation = {
                    'event': threading.Event(),
                    'error': None,
                }
                self._detectorStopOperations[detectorName] = operation
                thread = threading.Thread(
                    target=self._runDetectorStop,
                    args=(detectorName, operation),
                    name=f'DetectorStop-{detectorName}',
                    daemon=True,
                )
                operation['thread'] = thread
                try:
                    thread.start()
                except Exception:
                    self._detectorStopOperations.pop(
                        detectorName, None
                    )
                    raise

        if not operation['event'].wait(
            DETECTOR_STOP_TIMEOUT_MS / 1000
        ):
            raise TimeoutError(
                f'Detector {detectorName!r} did not stop within '
                f'{DETECTOR_STOP_TIMEOUT_MS / 1000:g} seconds'
            )

        with self._detectorStopLock:
            if (
                self._detectorStopOperations.get(detectorName)
                is operation
            ):
                self._detectorStopOperations.pop(detectorName, None)
            error = operation.get('error')

        if error is not None:
            if joinedExisting:
                # The previous caller timed out before this failure became
                # known. This invocation is the explicit recovery attempt, so
                # make one fresh serialized SDK stop instead of requiring an
                # otherwise surprising second retry click.
                return self._stopDetectorBounded(detectorName)
            raise error

    def isDetectorLeased(self, detectorName: str) -> bool:
        """ Whether the detector holds at least one acquisition lease. """
        return self._leaseTable.isLeased(detectorName)

    def isDetectorFaulted(self, detectorName: str) -> bool:
        """ Whether the detector is quarantined by a failed hardware stop. """
        return self._leaseTable.isFaulted(detectorName)

    def activeAcquisitionLeases(self):
        """Snapshot active ownership handles for shutdown diagnostics."""
        return tuple(self._leaseTable.activeLeases())

    def faultedAcquisitionDetectors(self):
        """Snapshot detectors whose hardware-off state is still unknown."""
        return tuple(self._leaseTable.faultedDetectors())

    def frameStreamMembership(self):
        """ Detectors whose frames must be polled into sigImageUpdated:
        LIVE_VIEW union EVENT_STREAM. Arming and delivery are different
        questions — an event-detection loop holds an EVENT_STREAM lease so its
        detector keeps being polled with live view off. """
        return self._leaseTable.leasedDetectorNames(FRAME_STREAM_PURPOSES)

    def startAcquisition(self, liveView=False, *, detectorNames=None):
        """ Legacy compat shim over acquire(). When detectorNames is None
        (identity check — an explicit empty iterable is a caller error, never
        "all"), leases all forAcquisition detectors, preserving the historical
        all-or-nothing behaviour. If liveView is True, sigImageUpdated will be
        emitted for every new frame. Returns a handle that can be passed to
        stopAcquisition when the detector data is no longer needed. """
        if detectorNames is None:
            names = self.getAllDeviceNames(condition=lambda c: c.forAcquisition)
            allowEmpty = True  # legacy: no forAcquisition detectors was valid
        else:
            names = list(dict.fromkeys(detectorNames))
            for detectorName in names:
                self._validateManagedDeviceName(detectorName)
            allowEmpty = False
        purpose = LeasePurpose.LIVE_VIEW if liveView else LeasePurpose.GENERIC
        return self.__acquireImpl(names, purpose, allowEmpty=allowEmpty)

    def stopAcquisition(self, handle, liveView=False):
        """ Legacy compat shim over release(). The liveView argument is
        ignored — the handle knows its own purpose. """
        self.release(handle)

    def __acquireImpl(self, names, purpose, *, allowEmpty):
        with self._frameStreamLifecycleLock:
            if purpose in FRAME_STREAM_PURPOSES:
                # Keep the worker out until both the lease membership and its
                # per-detector settle deadlines have been published.
                with self._framePollLock:
                    before = self.frameStreamMembership()
                    handle, transition = self._leaseTable.acquire(
                        names, purpose, allowEmpty=allowEmpty
                    )
                    added = self.frameStreamMembership() - before
                    lvWorker = self.__dict__.get('_lvWorker')
                    if lvWorker is not None:
                        lvWorker.noteFrameStreamDetectorsAdded(added)
            else:
                handle, transition = self._leaseTable.acquire(
                    names, purpose, allowEmpty=allowEmpty
                )

            # Preserve the legacy order: global signal first, poller second.
            # The settling delay remains on the worker thread, never here.
            self.__emitTransitionSignals(transition)
            if purpose in FRAME_STREAM_PURPOSES:
                self.__ensurePollThreadForMembership()
        return handle

    def __onLeaseStateChanged(self, detectorName, leased, faulted):
        # Mirror lease/fault state onto read-only base attributes; managers
        # never read the lease table itself.
        manager = self._subManagers[detectorName]
        manager._acquisitionLeased = leased
        manager._hardwareFaulted = faulted

    def __emitTransitionSignals(self, transition):
        # Emitted outside the lease lock: synchronously-connected slots may
        # call back into acquire()/release().
        if transition.nonFocusFirst:
            self.sigAcquisitionStarted.emit()
        if transition.nonFocusLast:
            self.sigAcquisitionStopped.emit()

    def setUpdatePeriod(self, updatePeriod):
        """ Changes the frame-stream poll period, restarting the poll thread
        only when current membership requires it. The lifecycle lock prevents
        a last-streamer release or first-streamer acquire from slipping between
        the join and the membership recheck. """
        self._lvWorker.setUpdatePeriod(updatePeriod)
        with self._frameStreamLifecycleLock:
            self.__stopPollThread()
            self.__ensurePollThreadForMembership()


class LVWorker(Worker):
    """ Polls exactly the frame-stream membership (LIVE_VIEW | EVENT_STREAM)
    into sigImageUpdated. Membership is re-read every tick, so a lease taken or
    released mid-flight takes effect on the next poll without restarting the
    thread. """

    #: Settling delay before the first poll of a freshly armed detector. Runs
    #: on the worker thread — it must never move back onto the caller's thread,
    #: where it blocked the UI for 300 ms on every live-view start.
    _INITIAL_SETTLE_S = 0.3

    def __init__(self, detectorsManager, updatePeriod):
        super().__init__()
        self._detectorsManager = detectorsManager
        self._updatePeriod = updatePeriod
        self._vtimer = None
        self._settleDeadlines = {}

    def noteFrameStreamDetectorsAdded(self, detectorNames):
        """Delay the first poll of every detector newly added to membership.

        DetectorsManager calls this while holding ``_framePollLock``, before a
        running worker can observe the new lease.
        """
        deadline = monotonic() + self._INITIAL_SETTLE_S
        for detectorName in detectorNames:
            self._settleDeadlines[detectorName] = deadline

    def run(self):
        sleep(self._INITIAL_SETTLE_S)
        self._pollFrameStream(init=False)
        self._vtimer = Timer()
        self._vtimer.timeout.connect(lambda: self._pollFrameStream(init=True))
        self._vtimer.start(self._updatePeriod)

    def _pollFrameStream(self, init):
        with self._detectorsManager._framePollLock:
            membership = self._detectorsManager.frameStreamMembership()
            for detectorName in set(self._settleDeadlines) - membership:
                self._settleDeadlines.pop(detectorName, None)

            now = monotonic()
            for detectorName in membership:
                if now < self._settleDeadlines.get(detectorName, 0):
                    continue
                try:
                    self._detectorsManager.execOn(
                        detectorName,
                        lambda detector: detector.updateLatestFrame(init)
                    )
                except Exception:
                    # One unavailable detector must not kill the poll loop for
                    # everyone else. Release is serialized with this block, so
                    # this is no longer the expected stale-membership path.
                    pass

    def stop(self):
        if self._vtimer is not None:
            self._vtimer.stop()

    def setUpdatePeriod(self, updatePeriod):
        self._updatePeriod = updatePeriod


class NoDetectorsError(RuntimeError):
    """ Error raised when a function related to the current detector is called
    if the DetectorsManager doesn't manage any detectors (i.e. the manager is
    initialized without any detectors). """
    pass


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
