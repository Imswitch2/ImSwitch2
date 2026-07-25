"""Acquisition lease bookkeeping for DetectorsManager.

A detector participates in ImSwitch-managed acquisition iff it holds at least
one acquisition lease. This module owns the per-detector refcounts, the
transactional arming/teardown of detector hardware, and the FAULTED quarantine
for detectors whose teardown failed. It is framework-free (no Qt): the
DetectorsManager supplies hardware start/stop callbacks and reacts to the
returned transitions (live-view poll thread, global signals).

Design reference: docs/design/plans/detector-acquisition-selection.md.
"""

import threading
from enum import Enum
from typing import Callable, Dict, Iterable, List, Optional, Set


class LeasePurpose(Enum):
    """ Why a consumer holds detectors. LIVE_VIEW and SCAN interact with
    selection; FOCUS is excluded from the user-visible global acquisition
    signals; EVENT_STREAM detectors are polled into sigUpdateImage even with
    live view off; EVENT_DIRECT consumers read frames themselves; GENERIC is
    the compat bucket for un-migrated ``startAcquisition(liveView=False)``
    callers. """

    LIVE_VIEW = 'liveView'
    SCAN = 'scan'
    RECORDING = 'recording'
    SNAP = 'snap'
    FOCUS = 'focus'
    WORKFLOW = 'workflow'
    EVENT_STREAM = 'eventStream'
    EVENT_DIRECT = 'eventDirect'
    GENERIC = 'generic'


#: Purposes whose detectors must be POLLED into sigImageUpdated. Arming a
#: detector is not the same as delivering its frames: an event-detection loop
#: with live view off holds an EVENT_STREAM lease so the poll thread keeps
#: running for it, which is what stops etSTED/EtMonalisa stalling on an armed
#: but never-read detectorFast. EVENT_DIRECT consumers read frames themselves
#: and are deliberately absent.
FRAME_STREAM_PURPOSES = frozenset({LeasePurpose.LIVE_VIEW,
                                   LeasePurpose.EVENT_STREAM})


class DetectorFaultedError(RuntimeError):
    """ Raised when acquiring a detector that is quarantined because a previous
    hardware stop failed. Recovery is an explicit retryStop(), never a natural
    refcount transition. """


class LeaseHandle:
    """ Opaque token identifying one acquisition lease. Compared by identity;
    the detector set and purpose are fixed at acquire time. """

    __slots__ = ('purpose', 'detectorNames')

    def __init__(self, purpose: LeasePurpose, detectorNames: Iterable[str]):
        self.purpose = purpose
        self.detectorNames = tuple(detectorNames)

    def __repr__(self):
        return (f'LeaseHandle(purpose={self.purpose.name},'
                f' detectorNames={list(self.detectorNames)})')


class LeaseTransition:
    """ What one acquire/release changed, for the DetectorsManager to react to
    outside the lease lock (global signals) or in the under-lock hooks
    (frame-stream poll thread lifecycle). """

    __slots__ = ('started', 'stopped', 'newlyFaulted',
                 'nonFocusFirst', 'nonFocusLast',
                 'frameStreamFirst', 'frameStreamLast')

    def __init__(self):
        self.started: List[str] = []       # detectors armed 0 -> 1
        self.stopped: List[str] = []       # detectors stopped 1 -> 0
        self.newlyFaulted: List[str] = []  # detectors whose stop failed
        self.nonFocusFirst = False         # first active non-FOCUS lease
        self.nonFocusLast = False          # last active non-FOCUS lease gone
        self.frameStreamFirst = False      # first frame-stream lease (start poll)
        self.frameStreamLast = False       # last frame-stream lease gone (stop poll)


class AcquisitionLeaseTable:
    """ Per-detector acquisition refcounts driven by leases.

    All refcount mutations and hardware start/stop calls run under one
    serializing lock:

    - ``startDetector(name)`` arms hardware; called on a detector's 0 -> 1.
    - ``stopDetector(name)`` tears down; called on 1 -> 0. Per the detector
      stop contract it must RAISE on failure; a failed stop marks the detector
      FAULTED and quarantines it (acquire rejected, no further stop attempts)
      until ``retryStop`` succeeds.
    - Acquire is transactional: on failure every detector this call incremented
      is decremented again, and only detectors this call started are stopped.

    Hooks (optional, invoked under the lock — they must not call back into
    this table):

    - ``onStateChanged(name, leased, faulted)`` fires whenever a detector's
      lease/fault state changes — the DetectorsManager mirrors these onto the
      read-only ``_acquisitionLeased`` / ``_hardwareFaulted`` base attributes.
    - ``onBeforeStops(transition)`` fires during release before any hardware
      stop, so the live-view poll thread can be taken down first and never
      reads a detector that is being stopped.

    Start-side reactions (poll thread bring-up, global signals) are driven by
    the returned :class:`LeaseTransition` *after* the lock is dropped.
    """

    def __init__(self,
                 startDetector: Callable[[str], None],
                 stopDetector: Callable[[str], None],
                 onStateChanged: Optional[Callable[[str, bool, bool], None]] = None,
                 onBeforeStops: Optional[Callable[[LeaseTransition], None]] = None):
        self._startDetector = startDetector
        self._stopDetector = stopDetector
        self._onStateChanged = onStateChanged
        self._onBeforeStops = onBeforeStops

        self._lock = threading.Lock()
        self._refcounts: Dict[str, int] = {}
        self._faulted: Set[str] = set()
        self._handles: List[LeaseHandle] = []  # identity semantics

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    def acquire(self, detectorNames: Iterable[str], purpose: LeasePurpose,
                *, allowEmpty: bool = False):
        """ Takes a lease on the given detectors, arming any that are not
        already armed. Returns ``(handle, transition)``. An explicit empty
        iterable is rejected (never silently "all"); ``allowEmpty`` exists
        only for the legacy ``startAcquisition()`` shim on setups without
        forAcquisition detectors. """
        names = list(dict.fromkeys(detectorNames))
        if not names and not allowEmpty:
            raise ValueError(
                'acquire() requires at least one detector name; an empty'
                ' selection is never implicitly "all detectors"'
            )
        if not isinstance(purpose, LeasePurpose):
            raise TypeError(f'purpose must be a LeasePurpose, got {purpose!r}')

        with self._lock:
            faulted = [name for name in names if name in self._faulted]
            if faulted:
                raise DetectorFaultedError(
                    f'Cannot acquire faulted detector(s) {faulted}: a previous'
                    f' hardware stop failed; call retryStop() first'
                )

            transition = LeaseTransition()
            incremented: List[str] = []
            try:
                for name in names:
                    newCount = self._refcounts.get(name, 0) + 1
                    self._refcounts[name] = newCount
                    incremented.append(name)
                    if newCount == 1:
                        self._notifyState(name)
                        self._startDetector(name)
                        transition.started.append(name)
            except Exception:
                self._rollbackAcquire(incremented, transition)
                raise

            handle = LeaseHandle(purpose, names)
            self._handles.append(handle)
            if purpose is not LeasePurpose.FOCUS:
                transition.nonFocusFirst = self._purposeCount(
                    LeasePurpose.FOCUS, invert=True) == 1
            if purpose in FRAME_STREAM_PURPOSES:
                transition.frameStreamFirst = self._frameStreamLeaseCount() == 1
        return handle, transition

    def release(self, handle: LeaseHandle) -> LeaseTransition:
        """ Releases a lease, stopping any detector whose refcount reaches
        zero. A failed stop marks the detector FAULTED (and the release
        continues with the remaining detectors). """
        with self._lock:
            if not any(existing is handle for existing in self._handles):
                raise ValueError('Invalid or already used handle')

            transition = LeaseTransition()
            self._handles = [existing for existing in self._handles
                             if existing is not handle]
            if handle.purpose is not LeasePurpose.FOCUS:
                transition.nonFocusLast = self._purposeCount(
                    LeasePurpose.FOCUS, invert=True) == 0
            if handle.purpose in FRAME_STREAM_PURPOSES:
                transition.frameStreamLast = self._frameStreamLeaseCount() == 0
            if self._onBeforeStops is not None:
                self._onBeforeStops(transition)

            for name in handle.detectorNames:
                newCount = self._refcounts.get(name, 0) - 1
                self._refcounts[name] = newCount
                if newCount != 0:
                    continue
                self._notifyState(name)
                if name in self._faulted:
                    # Quarantined: hardware state unknown, recovery only via
                    # an explicit retryStop, never a natural 1 -> 0.
                    continue
                try:
                    self._stopDetector(name)
                    transition.stopped.append(name)
                except Exception:
                    self._markFaulted(name)
                    transition.newlyFaulted.append(name)
        return transition

    def retryStop(self, detectorName: str) -> None:
        """ Explicit recovery path for a FAULTED detector: retries the
        hardware stop. On success the fault is cleared; on failure the stop
        exception propagates and the detector stays quarantined. """
        with self._lock:
            if detectorName not in self._faulted:
                return
            self._stopDetector(detectorName)  # raises -> fault remains
            self._faulted.discard(detectorName)
            self._notifyState(detectorName)

    # ------------------------------------------------------------------ #
    # Queries                                                            #
    # ------------------------------------------------------------------ #

    def isLeased(self, detectorName: str) -> bool:
        with self._lock:
            return self._refcounts.get(detectorName, 0) > 0

    def isFaulted(self, detectorName: str) -> bool:
        with self._lock:
            return detectorName in self._faulted

    def faultedDetectors(self) -> List[str]:
        with self._lock:
            return sorted(self._faulted)

    def activeLeases(self) -> List[LeaseHandle]:
        with self._lock:
            return list(self._handles)

    def leasedDetectorNames(
            self, purposes: Optional[Iterable[LeasePurpose]] = None
    ) -> Set[str]:
        """ Union of detector names over active leases, optionally filtered to
        the given purposes (e.g. the frame-stream membership
        LIVE_VIEW | EVENT_STREAM). """
        purposeSet = None if purposes is None else set(purposes)
        with self._lock:
            names: Set[str] = set()
            for handle in self._handles:
                if purposeSet is None or handle.purpose in purposeSet:
                    names.update(handle.detectorNames)
            return names

    # ------------------------------------------------------------------ #
    # Internals (call only with the lock held)                           #
    # ------------------------------------------------------------------ #

    def _rollbackAcquire(self, incremented: List[str],
                         transition: LeaseTransition) -> None:
        """ Undo every increment this acquire performed; stop only detectors
        this acquire started (0 -> 1). A rollback stop that itself fails
        faults that detector, and the original acquire error still
        propagates. """
        for name in reversed(incremented):
            newCount = self._refcounts.get(name, 0) - 1
            self._refcounts[name] = newCount
            if newCount != 0:
                continue
            self._notifyState(name)
            if name in transition.started:
                try:
                    self._stopDetector(name)
                except Exception:
                    self._markFaulted(name)

    def _markFaulted(self, name: str) -> None:
        self._faulted.add(name)
        self._notifyState(name)

    def _frameStreamLeaseCount(self) -> int:
        return sum(1 for handle in self._handles
                   if handle.purpose in FRAME_STREAM_PURPOSES)

    def _purposeCount(self, purpose: LeasePurpose, *, invert: bool = False) -> int:
        return sum(1 for handle in self._handles
                   if (handle.purpose is not purpose if invert
                       else handle.purpose is purpose))

    def _notifyState(self, name: str) -> None:
        if self._onStateChanged is not None:
            self._onStateChanged(name,
                                 self._refcounts.get(name, 0) > 0,
                                 name in self._faulted)


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
