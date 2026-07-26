from time import sleep

import numpy as np

from imswitch.imcommon.framework import Signal, SignalInterface, Thread, Timer, Worker
from ._acquisition_leases import (FRAME_STREAM_PURPOSES, AcquisitionLeaseTable,
                                  DetectorFaultedError, LeaseHandle, LeasePurpose)
from .MultiManager import MultiManager


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

        self._leaseTable = AcquisitionLeaseTable(
            startDetector=lambda name: self._subManagers[name].startAcquisition(),
            stopDetector=lambda name: self._subManagers[name].stopAcquisition(),
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
        self._thread.quit()
        self._thread.wait()
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

        if self._thread.isRunning():
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
        # The poll thread must be joined BEFORE the lease lock is taken, never
        # under it: the poll loop calls frameStreamMembership(), which needs
        # that same lock, so joining while a poll is mid-tick deadlocks
        # (the joiner holds the lock the pollee is blocked on).
        self.__stopPollThreadIfLastStreamer(handle)
        transition = self._leaseTable.release(handle)
        self.__emitTransitionSignals(transition)

    def __stopPollThreadIfLastStreamer(self, handle: LeaseHandle) -> None:
        """ Take down the poll thread if this release retires the last
        frame-stream lease, so the poller never reads a detector that is about
        to be stopped.

        Deliberately outside the lease lock. The check-then-act is not atomic,
        but the race is benign and self-correcting: a frame-stream lease
        acquired in the gap restarts the thread through the normal
        frameStreamFirst path, and membership is re-read every tick, so at
        worst a poll is skipped.
        """
        if handle.purpose not in FRAME_STREAM_PURPOSES:
            return
        if len(self._leaseTable.frameStreamHandles()) != 1:
            return  # other streamers remain; keep polling
        self._thread.quit()
        self._thread.wait()

    def retryStop(self, detectorName: str) -> None:
        """ Explicit recovery for a FAULTED detector: retries the hardware
        stop. On success the fault is cleared; on failure the stop exception
        propagates and the detector stays quarantined. """
        self._validateManagedDeviceName(detectorName)
        self._leaseTable.retryStop(detectorName)

    def isDetectorLeased(self, detectorName: str) -> bool:
        """ Whether the detector holds at least one acquisition lease. """
        return self._leaseTable.isLeased(detectorName)

    def isDetectorFaulted(self, detectorName: str) -> bool:
        """ Whether the detector is quarantined by a failed hardware stop. """
        return self._leaseTable.isFaulted(detectorName)

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
        handle, transition = self._leaseTable.acquire(
            names, purpose, allowEmpty=allowEmpty
        )
        # Outside the lease lock, in the legacy order: global signal first,
        # then bring up the frame-stream poll thread. The settling delay that
        # used to block here now happens inside LVWorker.run, on the worker's
        # own thread, so arming no longer freezes the UI for 300 ms.
        self.__emitTransitionSignals(transition)
        if transition.frameStreamFirst:
            self._thread.start()
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
        only if it is actually running — this used to quit and start it
        unconditionally, which resurrected a poll thread with no members when
        nothing was streaming. """
        self._lvWorker.setUpdatePeriod(updatePeriod)
        if not self._thread.isRunning():
            return
        self._thread.quit()
        self._thread.wait()
        self._thread.start()


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

    def run(self):
        sleep(self._INITIAL_SETTLE_S)
        self._pollFrameStream(init=False)
        self._vtimer = Timer()
        self._vtimer.timeout.connect(lambda: self._pollFrameStream(init=True))
        self._vtimer.start(self._updatePeriod)

    def _pollFrameStream(self, init):
        for detectorName in self._detectorsManager.frameStreamMembership():
            try:
                self._detectorsManager.execOn(
                    detectorName, lambda detector: detector.updateLatestFrame(init)
                )
            except Exception:
                # A detector released mid-tick (or otherwise unavailable) must
                # not kill the poll loop for everyone else.
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
