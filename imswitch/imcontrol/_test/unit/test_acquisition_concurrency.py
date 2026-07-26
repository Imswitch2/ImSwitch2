"""Concurrency guarantees of the lease table and finish barrier.

These are the failure modes that stub-based tests cannot demonstrate and that
show up on hardware as a frozen GUI or a lost final frame:

* The frame-stream poll loop runs on its own thread and reads membership from
  the lease table, while the GUI thread acquires and releases leases. An
  earlier version joined the poll thread while HOLDING the lease lock, so the
  joiner waited on a poll that was blocked on the lock it held.
* Finish acknowledgements may arrive from a detector's worker thread, racing
  the coordinator's own timeout.

Every test here has a hard deadline: a deadlock shows up as a failure, never
as a hung suite.

Design reference: docs/design/plans/detector-acquisition-selection.md.
"""

import threading
import time

from imswitch.imcontrol.model.managers._acquisition_leases import (
    AcquisitionLeaseTable, LeasePurpose,
)
from imswitch.imcontrol.model.managers._scan_execution import (
    FINISH_GRACEFUL, ScanExecutionCoordinator,
)

_DEADLINE_S = 10.0


def _runWithDeadline(target, deadline=_DEADLINE_S):
    """Run target in a thread and fail (not hang) if it does not finish."""
    error = []

    def wrapped():
        try:
            target()
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            error.append(exc)

    thread = threading.Thread(target=wrapped, daemon=True)
    thread.start()
    thread.join(deadline)
    assert not thread.is_alive(), (
        f'timed out after {deadline}s — likely a deadlock'
    )
    if error:
        raise error[0]


class _Hardware:
    def __init__(self):
        self._lock = threading.Lock()
        self.started = []
        self.stopped = []

    def start(self, name):
        with self._lock:
            self.started.append(name)

    def stop(self, name):
        # Real teardown takes time; this is where the poll thread used to be
        # joined while the lease lock was held.
        time.sleep(0.001)
        with self._lock:
            self.stopped.append(name)


def test_polling_thread_and_lease_churn_do_not_deadlock():
    """The regression test for the poll-thread deadlock.

    A poll loop hammers frameStreamMembership() while the main thread churns
    leases. Before the fix, releasing the last frame-stream lease joined the
    poll thread from inside the lock the poll needed.
    """
    hw = _Hardware()
    table = AcquisitionLeaseTable(startDetector=hw.start, stopDetector=hw.stop)
    stop = threading.Event()
    polls = [0]

    def poll():
        while not stop.is_set():
            table.leasedDetectorNames(
                [LeasePurpose.LIVE_VIEW, LeasePurpose.EVENT_STREAM]
            )
            polls[0] += 1

    poller = threading.Thread(target=poll, daemon=True)
    poller.start()
    try:
        def churn():
            for _ in range(200):
                live, _ = table.acquire(['CAM'], LeasePurpose.LIVE_VIEW)
                event, _ = table.acquire(['FAST'], LeasePurpose.EVENT_STREAM)
                table.release(live)
                table.release(event)

        _runWithDeadline(churn)
    finally:
        stop.set()
        poller.join(_DEADLINE_S)

    assert polls[0] > 0            # the poller really was contending
    assert not poller.is_alive()
    assert len(hw.stopped) == 400  # every lease released cleanly


def test_concurrent_acquire_release_keeps_refcounts_consistent():
    """Live view (GUI thread) and recording (worker thread) overlap on the
    same detector; the detector must end up stopped exactly once."""
    hw = _Hardware()
    table = AcquisitionLeaseTable(startDetector=hw.start, stopDetector=hw.stop)
    barrier = threading.Barrier(4)

    def worker(purpose):
        def run():
            barrier.wait()
            for _ in range(100):
                handle, _ = table.acquire(['CAM'], purpose)
                table.release(handle)
        return run

    threads = [
        threading.Thread(target=worker(p), daemon=True)
        for p in (LeasePurpose.LIVE_VIEW, LeasePurpose.RECORDING,
                  LeasePurpose.WORKFLOW, LeasePurpose.SNAP)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(_DEADLINE_S)
        assert not thread.is_alive(), 'timed out — likely a deadlock'

    assert not table.isLeased('CAM')
    assert not table.isFaulted('CAM')
    # Never more stops than starts, and balanced at rest.
    assert len(hw.started) == len(hw.stopped)


def test_frame_stream_query_is_safe_while_another_thread_holds_leases():
    hw = _Hardware()
    table = AcquisitionLeaseTable(startDetector=hw.start, stopDetector=hw.stop)
    table.acquire(['CAM'], LeasePurpose.LIVE_VIEW)

    def query():
        for _ in range(500):
            assert len(table.frameStreamHandles()) >= 1

    _runWithDeadline(query)


# --------------------------------------------------------------------------- #
# Finish barrier under threads                                                 #
# --------------------------------------------------------------------------- #

class _AsyncDetector:
    """Acknowledges from a worker thread, like the TimeTagger's final read.

    When ``gate`` is given the worker blocks on it, so a test can assert the
    barrier is still open without racing a sleep.
    """

    def __init__(self, name, delayS=0.0, gate=None):
        self.name = name
        self.isScanDriven = True
        self._delayS = delayS
        self._gate = gate
        self.threads = []

    def finishScan(self, mode, acknowledge):
        def later():
            if self._gate is not None:
                self._gate.wait(_DEADLINE_S)
            elif self._delayS:
                time.sleep(self._delayS)
            acknowledge()

        thread = threading.Thread(target=later, daemon=True)
        self.threads.append(thread)
        thread.start()


class _Manager:
    def __init__(self, detectors):
        self._detectors = {d.name: d for d in detectors}
        self.released = []
        self._lock = threading.Lock()

    def getAllDeviceNames(self, condition=None):
        return [n for n, d in self._detectors.items()
                if condition is None or condition(d)]

    def isDetectorFaulted(self, name):
        return False

    def execOn(self, name, func):
        return func(self._detectors[name])

    def acquire(self, detectorNames, purpose):
        return 'lease'

    def release(self, handle):
        with self._lock:
            self.released.append(handle)


class _Nidaq:
    def runScan(self, signalDict, scanInfoDict):
        pass


def test_barrier_waits_for_acknowledgements_arriving_off_thread():
    gate = threading.Event()
    detectors = [_AsyncDetector('APD', gate=gate), _AsyncDetector('TT', gate=gate)]
    manager = _Manager(detectors)
    coordinator = ScanExecutionCoordinator(manager, _Nidaq())
    token = coordinator.arm({}, {})
    completed = threading.Event()

    coordinator.resolve(token, FINISH_GRACEFUL,
                        onComplete=completed.set, timeoutS=None)

    # Deterministic, not a race against a sleep: both detectors are blocked.
    assert manager.released == []
    assert token.resolved is False

    gate.set()

    assert completed.wait(_DEADLINE_S), 'barrier never cleared'
    assert manager.released == ['lease']
    assert token.resolved is True


def test_late_acknowledgement_racing_the_timeout_releases_only_once():
    """The ack and the deadline can land together; exactly one must win."""
    for _ in range(50):
        detectors = [_AsyncDetector('APD', 0.0)]
        manager = _Manager(detectors)
        scheduled = []
        coordinator = ScanExecutionCoordinator(
            manager, _Nidaq(),
            scheduleTimeout=lambda d, cb: scheduled.append(cb),
        )
        token = coordinator.arm({}, {})
        coordinator.resolve(token, FINISH_GRACEFUL)

        timeoutThread = threading.Thread(target=scheduled[0], daemon=True)
        timeoutThread.start()
        timeoutThread.join(_DEADLINE_S)
        for thread in detectors[0].threads:
            thread.join(_DEADLINE_S)

        assert manager.released == ['lease'], 'lease released more than once'


def test_many_participants_acknowledging_simultaneously_release_once():
    detectors = [_AsyncDetector(f'D{i}', 0.01) for i in range(8)]
    manager = _Manager(detectors)
    coordinator = ScanExecutionCoordinator(manager, _Nidaq())
    token = coordinator.arm({}, {})
    completed = threading.Event()

    coordinator.resolve(token, FINISH_GRACEFUL,
                        onComplete=completed.set, timeoutS=None)

    assert completed.wait(_DEADLINE_S), 'barrier never cleared'
    for detector in detectors:
        for thread in detector.threads:
            thread.join(_DEADLINE_S)
    assert manager.released == ['lease']


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
