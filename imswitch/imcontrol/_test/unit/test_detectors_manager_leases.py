"""DetectorsManager lease integration + legacy compat (Phase 1).

Covers acceptance-gate item #1 (legacy startAcquisition/stopAcquisition compat
alongside explicit-purpose acquire) and #9 (fault quarantine + retry stop),
plus the mirror attributes managers read instead of the lease table.

The DetectorsManager is exercised with stub sub-managers so no camera/DAQ
hardware or Qt event loop is needed; the poll-thread handle is stubbed too.

Design reference: docs/design/plans/detector-acquisition-selection.md.
"""

import sys
import threading
import time

import pytest

from imswitch.imcontrol.model.managers.DetectorsManager import DetectorsManager
from imswitch.imcontrol.model.managers.MultiManager import NoSuchSubManagerError
from imswitch.imcontrol.model.managers._acquisition_leases import (
    DetectorFaultedError, LeasePurpose,
)


class _StubDetector:
    def __init__(self, name, forAcquisition=True, failStop=False):
        self.name = name
        self.forAcquisition = forAcquisition
        self.failStop = failStop
        self.startCalls = 0
        self.stopCalls = 0
        # Mirrors written by the lease table (see DetectorManager).
        self._acquisitionLeased = False
        self._hardwareFaulted = False

    def startAcquisition(self):
        self.startCalls += 1

    def stopAcquisition(self):
        self.stopCalls += 1
        if self.failStop:
            raise RuntimeError(f'stop failed: {self.name}')


class _StubThread:
    def __init__(self):
        self.startCalls = 0
        self.quitCalls = 0

    def start(self):
        self.startCalls += 1

    def quit(self):
        self.quitCalls += 1

    def wait(self):
        pass

    def isRunning(self):
        return self.startCalls > self.quitCalls


@pytest.fixture
def manager(monkeypatch):
    """A DetectorsManager over stub detectors, with __init__ bypassed so no
    real managers, Qt threads or signals are constructed."""
    # The package re-exports the class under the module's own name, so reach
    # the module through sys.modules rather than attribute traversal.
    monkeypatch.setattr(
        sys.modules['imswitch.imcontrol.model.managers.DetectorsManager'],
        'sleep', lambda _seconds: None,
    )

    mgr = DetectorsManager.__new__(DetectorsManager)
    detectors = {
        'CAM': _StubDetector('CAM'),
        'APD': _StubDetector('APD'),
        'FOCUSCAM': _StubDetector('FOCUSCAM', forAcquisition=False),
    }
    mgr._subManagers = detectors
    mgr._currentDetectorName = 'CAM'
    mgr._thread = _StubThread()
    mgr._frameStreamLifecycleLock = threading.RLock()
    mgr._framePollLock = threading.RLock()
    mgr._detectorStopLock = threading.RLock()
    mgr._detectorStopOperations = {}
    mgr._lvWorker = None

    emitted = []
    mgr.sigAcquisitionStarted = _StubSignal(lambda: emitted.append('started'))
    mgr.sigAcquisitionStopped = _StubSignal(lambda: emitted.append('stopped'))
    mgr.signals = emitted

    # Build the lease table exactly as __init__ does.
    from imswitch.imcontrol.model.managers._acquisition_leases import (
        AcquisitionLeaseTable,
    )
    mgr._leaseTable = AcquisitionLeaseTable(
        startDetector=lambda name: detectors[name].startAcquisition(),
        stopDetector=mgr._stopDetectorBounded,
        onStateChanged=mgr._DetectorsManager__onLeaseStateChanged,
    )
    return mgr


class _StubSignal:
    def __init__(self, onEmit):
        self._onEmit = onEmit

    def emit(self, *args):
        self._onEmit()


# --------------------------------------------------------------------------- #
# Legacy compat (acceptance #1)                                                #
# --------------------------------------------------------------------------- #

def test_legacy_start_acquisition_leases_all_for_acquisition_detectors(manager):
    handle = manager.startAcquisition()

    assert manager['CAM'].startCalls == 1
    assert manager['APD'].startCalls == 1
    assert manager['FOCUSCAM'].startCalls == 0  # not forAcquisition
    assert manager.signals == ['started']

    manager.stopAcquisition(handle)
    assert manager['CAM'].stopCalls == 1
    assert manager['APD'].stopCalls == 1
    assert manager.signals == ['started', 'stopped']


def test_legacy_liveview_start_stops_and_starts_the_poll_thread(manager):
    handle = manager.startAcquisition(liveView=True)
    assert manager._thread.startCalls == 1

    manager.stopAcquisition(handle, liveView=True)
    assert manager._thread.quitCalls == 1


def test_liveview_flag_on_stop_is_ignored_handle_knows_its_purpose(manager):
    handle = manager.startAcquisition(liveView=True)
    manager.stopAcquisition(handle)  # legacy callers sometimes omit the flag

    assert manager['CAM'].stopCalls == 1
    assert manager._thread.quitCalls == 1


def test_legacy_none_means_all_not_empty(manager):
    """`detectorNames is None` resolves to all forAcquisition; an explicitly
    empty iterable is a caller error, never 'all'."""
    handle = manager.startAcquisition(detectorNames=None)
    assert manager['CAM'].startCalls == 1
    manager.stopAcquisition(handle)

    with pytest.raises(ValueError):
        manager.startAcquisition(detectorNames=[])


def test_legacy_empty_liveview_handle_never_keeps_an_idle_poller(manager):
    for detector in manager._subManagers.values():
        detector.forAcquisition = False

    empty = manager.startAcquisition(liveView=True)
    assert manager.frameStreamMembership() == set()
    assert not manager._thread.isRunning()

    explicit = manager.acquire(['FOCUSCAM'], LeasePurpose.LIVE_VIEW)
    assert manager._thread.isRunning()
    manager.release(explicit)

    # The empty compatibility handle remains active, but has no detector
    # membership and therefore must not keep the poll thread alive.
    assert manager._leaseTable.isActiveHandle(empty)
    assert manager.frameStreamMembership() == set()
    assert not manager._thread.isRunning()
    manager.release(empty)


def test_legacy_and_explicit_leases_coexist(manager):
    legacy = manager.startAcquisition()          # GENERIC over everything
    explicit = manager.acquire(['CAM'], LeasePurpose.RECORDING)

    assert manager['CAM'].startCalls == 1        # armed once

    manager.release(explicit)
    assert manager['CAM'].stopCalls == 0         # legacy lease still holds it

    manager.stopAcquisition(legacy)
    assert manager['CAM'].stopCalls == 1


def test_concurrent_last_release_notifies_stopped_before_new_started(manager):
    """Global acquisition signals must follow serialized lease order."""
    first = manager.acquire(['CAM'], LeasePurpose.GENERIC)
    manager.signals.clear()
    stopEntered = threading.Event()
    allowStopSignal = threading.Event()

    def emitStopped():
        stopEntered.set()
        assert allowStopSignal.wait(2)
        manager.signals.append('stopped')

    manager.sigAcquisitionStopped = _StubSignal(emitStopped)

    releasing = threading.Thread(target=lambda: manager.release(first), daemon=True)
    releasing.start()
    assert stopEntered.wait(2)

    acquired = []
    acquiring = threading.Thread(
        target=lambda: acquired.append(
            manager.acquire(['CAM'], LeasePurpose.GENERIC)
        ),
        daemon=True,
    )
    acquiring.start()
    acquiring.join(0.05)
    assert acquiring.is_alive()

    allowStopSignal.set()
    releasing.join(2)
    acquiring.join(2)

    assert not releasing.is_alive()
    assert not acquiring.is_alive()
    assert manager.signals == ['stopped', 'started']
    manager.release(acquired[0])


def test_acquire_validates_detector_names(manager):
    with pytest.raises(NoSuchSubManagerError):
        manager.acquire(['NOPE'], LeasePurpose.SCAN)


def test_acquire_rejects_an_empty_selection(manager):
    with pytest.raises(ValueError):
        manager.acquire([], LeasePurpose.SCAN)


# --------------------------------------------------------------------------- #
# Mirrors                                                                      #
# --------------------------------------------------------------------------- #

def test_lease_state_is_mirrored_onto_the_detector(manager):
    assert manager['APD']._acquisitionLeased is False

    handle = manager.acquire(['APD'], LeasePurpose.SCAN)
    assert manager['APD']._acquisitionLeased is True
    assert manager.isDetectorLeased('APD')

    manager.release(handle)
    assert manager['APD']._acquisitionLeased is False
    assert not manager.isDetectorLeased('APD')


def test_scoped_acquire_does_not_arm_unrelated_detectors(manager):
    manager.acquire(['APD'], LeasePurpose.SCAN)

    assert manager['APD'].startCalls == 1
    assert manager['CAM'].startCalls == 0


# --------------------------------------------------------------------------- #
# Fault quarantine (acceptance #9)                                             #
# --------------------------------------------------------------------------- #

def test_failed_stop_faults_the_detector_and_blocks_reacquisition(manager):
    manager['APD'].failStop = True

    handle = manager.acquire(['APD'], LeasePurpose.SCAN)
    manager.release(handle)

    assert manager.isDetectorFaulted('APD')
    assert manager['APD']._hardwareFaulted is True
    assert manager['APD']._acquisitionLeased is False

    with pytest.raises(DetectorFaultedError):
        manager.acquire(['APD'], LeasePurpose.SCAN)


def test_retry_stop_clears_the_fault(manager):
    manager['APD'].failStop = True
    handle = manager.acquire(['APD'], LeasePurpose.SCAN)
    manager.release(handle)

    manager['APD'].failStop = False
    manager.retryStop('APD')

    assert not manager.isDetectorFaulted('APD')
    assert manager['APD']._hardwareFaulted is False
    manager.acquire(['APD'], LeasePurpose.SCAN)  # usable again


def test_blocking_detector_stop_times_out_without_duplicate_retry(
        manager, monkeypatch):
    module = sys.modules[
        'imswitch.imcontrol.model.managers.DetectorsManager'
    ]
    monkeypatch.setattr(module, 'DETECTOR_STOP_TIMEOUT_MS', 20)
    stopEntered = threading.Event()
    releaseStop = threading.Event()
    calls = []

    def blockingStop():
        calls.append(True)
        stopEntered.set()
        releaseStop.wait()

    manager['CAM'].stopAcquisition = blockingStop
    handle = manager.acquire(['CAM'], LeasePurpose.SCAN)

    started = time.monotonic()
    manager.release(handle)
    assert time.monotonic() - started < 0.5
    assert stopEntered.is_set()
    assert manager.isDetectorFaulted('CAM')
    assert 'CAM' in manager._detectorStopOperations
    with pytest.raises(DetectorFaultedError):
        manager.acquire(['CAM'], LeasePurpose.SCAN)

    releaseStop.set()
    manager.retryStop('CAM')

    assert calls == [True]
    assert not manager.isDetectorFaulted('CAM')
    assert manager._detectorStopOperations == {}


def test_a_faulted_detector_does_not_break_the_others(manager):
    manager['APD'].failStop = True

    handle = manager.startAcquisition()
    manager.stopAcquisition(handle)

    assert manager.isDetectorFaulted('APD')
    assert not manager.isDetectorFaulted('CAM')
    assert manager['CAM'].stopCalls == 1  # CAM still stopped cleanly


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
