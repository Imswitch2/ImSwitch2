"""Shared scan-execution lifecycle for every NI-DAQ scan entry point.

There are five direct ``nidaqManager.runScan`` callers — the four
``SuperScanController`` subclasses and ``EtSTEDTriggeredScanRunner`` — so the
lifecycle cannot live on ``SuperScanController`` without missing one. It lives
here instead, framework-free (no Qt) so it is testable in isolation.

Two levels, deliberately distinct (see the design plan, R7-2/R7-4):

- A **scan run** is one user/API-initiated scan including all repeat
  iterations, MoNaLISA ``autoAxial`` follow-ups and ``isNonFinalPartOfSequence``
  parts. An owner-tagged reservation prevents another entry point from arming
  in the gaps between those iterations.
- A **scan iteration** is one ``runScan`` -> ``scanDone`` cycle. The participant
  snapshot, the SCAN lease, the lifecycle token and ``finishScan`` are all
  iteration-level, because each iteration is a frame.

Completion runs **exactly once** per iteration across all five terminations:
busy refusal, build failure, arm exception, normal completion and abort.

Design reference: docs/design/plans/detector-acquisition-selection.md.
"""

import threading

from ._acquisition_leases import LeasePurpose
from .NidaqManager import ScanBusyError

#: How long to wait for a participant to acknowledge its end-of-scan work
#: before giving up on it and releasing the lease anyway. A manager that never
#: acknowledges (mock mode, a worker that died) must not wedge scanning
#: forever; losing one final frame is strictly better than a stuck GUI.
DEFAULT_FINISH_TIMEOUT_S = 5.0

#: ``scanInfoDict`` key carrying the participant snapshot to the detectors.
#: Absent means "legacy scan" — every scan-driven detector participates, which
#: is what preserves behaviour for any entry point not yet routed through here.
PARTICIPANTS_KEY = 'participants'

#: ``scanInfoDict`` key listing scan-driven detectors deliberately EXCLUDED
#: from this scan. Consumers that cannot themselves tell a scan-driven detector
#: from a free-running one — notably the scan simulator, which is built from
#: setupInfo alone and has no DetectorsManager — use this to skip detectors
#: that will produce nothing, without having to reason about the ownership
#: axis. Empty until selection lands in Phase 5.
EXCLUDED_KEY = 'excludedDetectors'

FINISH_GRACEFUL = 'graceful'
FINISH_ABORT = 'abort'

_SHARED_COORDINATOR_ATTR = '_imswitchScanExecutionCoordinator'
_sharedCoordinatorLock = threading.Lock()


class ScanIterationToken:
    """Idempotent per-iteration lifecycle handle.

    Held by whoever armed the iteration; ``resolve`` is safe to call from any
    number of termination paths and starts the finish sequence at most once.

    An iteration passes through three states: in flight -> *finishing* (every
    participant asked to finish, waiting on their acknowledgements) ->
    *resolved* (lease released, next iteration may arm). The middle state is
    the barrier: without it the SCAN lease is released while a detector's
    final read is still running, and hardware teardown or the next repeat
    frame destroys the worker mid-read.
    """

    __slots__ = ('participants', 'owner', 'generation', 'leaseHandle',
                 'resolved', 'finishing', 'pending', 'acknowledgements',
                 'onComplete', 'timedOut', 'scanInfoDict', '_lock')

    def __init__(self, participants, *, owner=None, generation=0):
        self.participants = tuple(participants)
        self.owner = owner
        self.generation = int(generation)
        self.leaseHandle = None
        self.resolved = False
        self.finishing = False
        self.pending = set()
        self.acknowledgements = {}
        self.onComplete = None
        self.timedOut = False
        self.scanInfoDict = None
        self._lock = threading.Lock()

    def __repr__(self):
        return (f'ScanIterationToken(generation={self.generation},'
                f' participants={list(self.participants)},'
                f' finishing={self.finishing}, resolved={self.resolved},'
                f' pending={sorted(self.pending)})')


class ScanRunToken:
    """Identity-safe reservation for one complete user-visible scan run.

    The token deliberately is not interchangeable with an owner object.  A
    stale completion callback from an earlier run therefore cannot release a
    newer reservation made by the same controller.

    ``releaseRequested`` supports terminal failures while an arm exception's
    participant barrier is still draining.  The reservation remains exclusive
    until that iteration actually resolves, then is released automatically.
    """

    __slots__ = (
        'owner', 'generation', 'releaseRequested',
        'releaseBarrierCleared', 'holdReleaseUntilFinalized', 'released',
        'releaseCallbacks', 'releaseCallbackInFlight',
    )

    def __init__(self, owner, *, generation=0):
        self.owner = owner
        self.generation = int(generation)
        self.releaseRequested = False
        self.releaseBarrierCleared = False
        self.holdReleaseUntilFinalized = False
        self.released = False
        self.releaseCallbacks = []
        self.releaseCallbackInFlight = False

    def __repr__(self):
        return (f'ScanRunToken(generation={self.generation},'
                f' releaseRequested={self.releaseRequested},'
                f' releaseBarrierCleared={self.releaseBarrierCleared},'
                f' holdReleaseUntilFinalized={self.holdReleaseUntilFinalized},'
                f' released={self.released})')


class ScanExecutionCoordinator:
    """Arms one scan iteration and guarantees exactly-once completion.

    The caller supplies the NI-DAQ manager and DetectorsManager; nothing here
    touches Qt, blocks the UI thread, or holds the DetectorsManager lock across
    a wait.
    """

    def __init__(self, detectorsManager, nidaqManager, logger=None,
                 scheduleTimeout=None):
        self._detectorsManager = detectorsManager
        self._nidaqManager = nidaqManager
        self._logger = logger
        # ``scheduleTimeout(delaySeconds, callback)`` arms a non-blocking
        # deadline for the finish barrier. Injected (QTimer.singleShot in the
        # GUI) so this class stays framework-free; without it the barrier has
        # no deadline and tests drive it explicitly.
        self._scheduleTimeout = scheduleTimeout
        self._activeToken = None
        self._activeRunToken = None
        self._stateLock = threading.RLock()
        self._generation = 0
        self._runGeneration = 0

    def configure(self, *, logger=None, scheduleTimeout=None):
        """Supply integration-specific services to an existing shared instance.

        Controllers are constructed after the managers and can therefore add a
        Qt timeout scheduler to the coordinator created by ``MasterController``.
        The operation is idempotent so every controller may safely call it.
        """
        with self._stateLock:
            if logger is not None and self._logger is None:
                self._logger = logger
            if scheduleTimeout is not None:
                self._scheduleTimeout = scheduleTimeout
        return self

    @property
    def activeToken(self):
        """The unresolved token of the in-flight iteration, if any."""
        with self._stateLock:
            return self._activeToken

    def tokenForOwner(self, owner):
        """Return the active token only when it belongs to ``owner``.

        NI-DAQ completion signals are broadcast to every scan controller.  A
        shared coordinator makes ownership explicit so only the entry point
        that armed the iteration can finish it or publish run-level completion.
        """
        with self._stateLock:
            token = self._activeToken
            return token if token is not None and token.owner is owner else None

    @property
    def activeRunToken(self):
        """The unreleased run-level reservation, if any."""
        with self._stateLock:
            return self._activeRunToken

    def runForOwner(self, owner):
        """Return the run reservation only when it belongs to ``owner``."""
        with self._stateLock:
            token = self._activeRunToken
            return token if token is not None and token.owner is owner else None

    def reserveRun(self, owner):
        """Reserve all scan iterations in one user-visible run for ``owner``.

        Re-reserving by the same owner is idempotent; it is how repeat and
        MoNaLISA follow-up iterations retain ownership during their no-iteration
        gaps. A different owner is refused even when NI-DAQ currently has no
        active iteration.
        """
        if owner is None:
            raise ValueError('A scan run reservation requires a non-None owner.')
        with self._stateLock:
            active = self._activeRunToken
            if active is not None:
                if active.releaseRequested:
                    raise ScanBusyError(
                        'The previous scan run is still finishing '
                        f'(generation {active.generation})'
                    )
                if active.owner is owner:
                    return active
                raise ScanBusyError(
                    'Another scan run is already reserved '
                    f'(generation {active.generation})'
                )
            self._runGeneration += 1
            token = ScanRunToken(owner, generation=self._runGeneration)
            self._activeRunToken = token
            return token

    def releaseRun(
        self, token, *, onReleased=None,
        holdUntilFinalized=False,
    ):
        """Request idempotent release of an exact run-reservation token.

        If the owner's final iteration is still finishing, release is deferred
        until its participant barrier clears. ``onReleased`` runs only after
        that barrier and the SCAN lease have actually cleared, so run-level
        ``sigScanEnded`` can never precede detector finalization.
        """
        if token is None:
            return False
        callbacks = []
        with self._stateLock:
            # Prove exact identity before reading token state. Stale or opaque
            # handles are harmless refusals, not attribute errors that can
            # derail a concurrent terminal path.
            if self._activeRunToken is not token:
                return False
            if token.released:
                return False
            if holdUntilFinalized and onReleased is None:
                raise ValueError(
                    'A held scan-run release requires an onReleased callback.'
                )
            if holdUntilFinalized:
                token.holdReleaseUntilFinalized = True
            if token.releaseRequested:
                if onReleased is None:
                    return False
                if token.releaseBarrierCleared:
                    # A held release callback can itself publish compatibility
                    # signals and yield to another terminal path. Treat a
                    # concurrent/re-entrant release request as the same request,
                    # rather than dispatching the terminal twice. If the first
                    # callback returns without finalizing, a later retry is
                    # accepted once ``releaseCallbackInFlight`` is cleared.
                    if not token.releaseCallbackInFlight:
                        token.releaseCallbackInFlight = True
                        callbacks = (onReleased,)
                elif not token.releaseCallbacks:
                    token.releaseCallbacks.append(onReleased)
                accepted = True
            else:
                activeIteration = self._activeToken
                if (activeIteration is not None
                        and activeIteration.owner is token.owner):
                    # ``resolved`` is set before the detector lease is released.
                    # The coordinator does not consider the iteration completely
                    # drained until ``_completeToken`` clears ``_activeToken``
                    # after that release.  Treating a merely-resolved token as
                    # idle would let a concurrent terminal path publish
                    # ``sigScanEnded`` while detector finalization is still
                    # running.
                    if onReleased is not None:
                        token.releaseCallbacks.append(onReleased)
                    token.releaseRequested = True
                    return True
                token.releaseRequested = True
                token.releaseBarrierCleared = True
                if onReleased is not None:
                    token.releaseCallbacks.append(onReleased)
                callbacks = tuple(token.releaseCallbacks)
                token.releaseCallbacks.clear()
                if callbacks:
                    token.releaseCallbackInFlight = True
                if not token.holdReleaseUntilFinalized:
                    token.released = True
                    self._activeRunToken = None
                accepted = True
        self._runReleaseCallbacks(token, callbacks)
        return accepted

    def finalizeRunRelease(self, token) -> bool:
        """Make a physically drained run reservable after terminal publication.

        Controllers that must marshal ``sigScanEnded``/exact completion onto
        their UI thread ask ``releaseRun(..., holdUntilFinalized=True)`` to keep
        the global reservation closed across that queued boundary. They call
        this method only after terminal publication has returned.
        """
        if token is None:
            return False
        with self._stateLock:
            if (
                self._activeRunToken is not token
                or token.released
                or not token.releaseRequested
                or not token.releaseBarrierCleared
            ):
                return False
            token.released = True
            self._activeRunToken = None
            return True

    # ------------------------------------------------------------------ #
    # Participant composition                                            #
    # ------------------------------------------------------------------ #

    def scanDrivenDetectors(self):
        """Every scan-driven detector, participating or not."""
        return self._detectorsManager.getAllDeviceNames(
            condition=lambda detector: getattr(detector, 'isScanDriven', False)
        )

    #: Purposes whose holder needs a scan-driven detector to run even when the
    #: user deselected it. Deselecting is a preference; an active recording,
    #: workflow or event modality is a commitment, and silently dropping its
    #: detector mid-run would corrupt its output. GENERIC stays in the set for
    #: as long as the legacy startAcquisition shim can produce it.
    OVERRIDE_PURPOSES = (
        LeasePurpose.RECORDING, LeasePurpose.SNAP, LeasePurpose.WORKFLOW,
        LeasePurpose.EVENT_STREAM, LeasePurpose.EVENT_DIRECT,
        LeasePurpose.GENERIC,
    )

    def composeParticipants(self):
        """Scan-driven detectors taking part in the next iteration.

        ``(selected scan-driven) | (scan-driven held by an override purpose)``,
        minus faulted ones — a detector whose stop failed must never rejoin a
        scan, whatever anybody asked for.

        Selection changes deferred while the previous iteration owned a
        detector are applied here, which is the one point where a new selection
        can take effect without disturbing an iteration already in flight.
        """
        detectorsManager = self._detectorsManager
        flush = getattr(detectorsManager, 'flushQueuedSelectionChanges', None)
        if flush is not None:
            flush()

        scanDriven = self.scanDrivenDetectors()
        getSelected = getattr(detectorsManager, 'getSelectedDetectors', None)
        if getSelected is None:
            # A manager without selection support (tests, older embedders)
            # behaves as though everything is selected.
            selected = set(scanDriven)
        else:
            selected = getSelected()

        overridden = detectorsManager.leasedDetectorNames(
            self.OVERRIDE_PURPOSES
        ) if hasattr(detectorsManager, 'leasedDetectorNames') else set()

        return [
            name for name in scanDriven
            if (name in selected or name in overridden)
            and not detectorsManager.isDetectorFaulted(name)
        ]

    # ------------------------------------------------------------------ #
    # Arm / resolve                                                      #
    # ------------------------------------------------------------------ #

    def arm(self, signalDict, scanInfoDict, *, owner=None):
        """Compose the snapshot, take the SCAN lease, inject the participants
        and start the scan. Returns the iteration's token.

        Any failure — including a ``ScanBusyError`` refusal, which starts
        nothing and emits no lifecycle signal — resolves the token and unwinds
        the lease before propagating, so a refused arm can never strand
        ownership.
        """
        participants = self.composeParticipants()
        implicitRunToken = None
        with self._stateLock:
            activeRun = self._activeRunToken
            if activeRun is not None:
                if activeRun.releaseRequested:
                    raise ScanBusyError(
                        'The previous scan run is still finishing '
                        f'(generation {activeRun.generation})'
                    )
                if activeRun.owner is not owner:
                    raise ScanBusyError(
                        'Another scan run is already reserved '
                        f'(generation {activeRun.generation})'
                    )
            active = self._activeToken
            if active is not None:
                raise ScanBusyError(
                    'A scan iteration is still in flight or finishing '
                    f'(generation {active.generation})'
                )
            if activeRun is None and owner is not None:
                # Preserve safety for direct coordinator users that did not
                # explicitly reserve first. Full controllers retain this token
                # themselves and release it at run-level completion.
                implicitRunToken = self.reserveRun(owner)
            self._generation += 1
            token = ScanIterationToken(
                participants, owner=owner, generation=self._generation
            )
            token.scanInfoDict = scanInfoDict
            # Reserve global ownership before touching hardware. Another entry
            # point must not slip in while acquire() or runScan() is running.
            self._activeToken = token
        try:
            if participants:
                token.leaseHandle = self._detectorsManager.acquire(
                    participants, LeasePurpose.SCAN
                )
            # Participation travels as scan-scoped data on the dict that
            # sigScanBuilt already carries, so there is no mirror to go stale
            # between iterations.
            scanInfoDict[PARTICIPANTS_KEY] = list(participants)
            participantSet = set(participants)
            scanInfoDict[EXCLUDED_KEY] = [
                name for name in self.scanDrivenDetectors()
                if name not in participantSet
            ]
            self._nidaqManager.runScan(signalDict, scanInfoDict)
        except BaseException:
            # Nothing ran, so no participant has async work outstanding: no
            # timeout needed, the barrier clears immediately.
            self.resolve(
                token,
                FINISH_ABORT,
                onComplete=(
                    (lambda: self.releaseRun(implicitRunToken))
                    if implicitRunToken is not None else None
                ),
                timeoutS=None,
            )
            if scanInfoDict.get(PARTICIPANTS_KEY) == list(participants):
                scanInfoDict.pop(PARTICIPANTS_KEY, None)
            scanInfoDict.pop(EXCLUDED_KEY, None)
            raise
        return token

    def resolve(self, token, mode=FINISH_GRACEFUL, onComplete=None,
                timeoutS=DEFAULT_FINISH_TIMEOUT_S):
        """Begin finishing an iteration exactly once.

        Asks every participant to finish and then WAITS for their
        acknowledgements before releasing the SCAN lease. The wait is
        asynchronous — this call never blocks — so the UI thread is free while
        a detector completes its final read.

        ``finishScan`` runs for every participant regardless of refcount: a
        TimeTagger that also holds a WORKFLOW lease goes 2->1, so no hardware
        stop happens, but it still needs its final read. General hardware stop
        happens only at aggregate zero, via the lease release once the barrier
        clears.

        ``onComplete`` fires when the barrier clears, on whichever thread
        acknowledged last. Repeat re-arm hangs off this, so the next frame
        cannot start while the previous frame's data is still being read out.

        Returns True if this call started the finish, False if it was already
        started or done.
        """
        if token is None:
            return False
        with token._lock:
            if token.finishing or token.resolved:
                return False
            token.finishing = True
            token.onComplete = onComplete
            token.pending = set(token.participants)
            token.acknowledgements = {}
            hasPending = bool(token.pending)

        if not hasPending:
            self._completeToken(token)
            return True

        if timeoutS and self._scheduleTimeout is not None:
            try:
                self._scheduleTimeout(
                    timeoutS, lambda: self._onFinishTimeout(token)
                )
            except Exception:
                # The deadline is a liveness backstop, not part of physical
                # finalization. A closing/deleted event-loop scheduler must not
                # prevent participants from receiving finishScan and clearing
                # the barrier normally.
                self._log('Failed to schedule the scan-finish timeout')

        for name in tuple(token.participants):
            acknowledge = lambda n=name: self._acknowledge(token, n)
            with token._lock:
                if token.resolved:
                    break
                token.acknowledgements[name] = acknowledge
            try:
                self._detectorsManager.execOn(
                    name,
                    lambda detector, ack=acknowledge: detector.finishScan(
                        mode, ack
                    ),
                )
            except Exception:
                # A manager that could not even be asked must not hold the
                # barrier open.
                self._log(f'finishScan({mode}) failed for detector "{name}"')
                self._acknowledge(token, name)
        return True

    def _acknowledge(self, token, detectorName):
        """Called by a participant when its end-of-scan work is done. Safe
        from any thread and safe to call more than once."""
        with token._lock:
            if token.resolved:
                return
            token.pending.discard(detectorName)
            token.acknowledgements.pop(detectorName, None)
            if token.pending:
                return
        self._completeToken(token)

    def _onFinishTimeout(self, token):
        with token._lock:
            if token.resolved or not token.pending:
                return
            stuck = sorted(token.pending)
            acknowledgements = {
                name: token.acknowledgements.get(name) for name in stuck
            }
            token.timedOut = True
            token.pending.clear()
            token.acknowledgements.clear()
        # Give a detector the chance to discard an iteration-scoped callback.
        # Without this, a late final frame can acknowledge a later iteration.
        for name, acknowledge in acknowledgements.items():
            if acknowledge is None:
                continue
            try:
                self._detectorsManager.execOn(
                    name,
                    lambda detector, ack=acknowledge: (
                        detector.cancelFinishScan(ack)
                        if callable(getattr(detector, 'cancelFinishScan', None))
                        else None
                    ),
                )
            except Exception:
                self._log(
                    f'Failed to cancel timed-out finish callback for "{name}"'
                )
        self._log(
            f'Timed out waiting for end-of-scan acknowledgement from '
            f'{stuck}; releasing the scan lease anyway. Their final frame for '
            f'this iteration may be missing.'
        )
        self._completeToken(token)

    def _completeToken(self, token):
        """Barrier cleared: release the lease and let the next iteration arm."""
        with token._lock:
            if token.resolved:
                return
            token.resolved = True
            handle, token.leaseHandle = token.leaseHandle, None
            onComplete = token.onComplete
            token.acknowledgements.clear()

        if handle is not None:
            try:
                self._detectorsManager.release(handle)
            except Exception:
                self._log('Failed to release the scan detector lease')

        scanInfoDict, token.scanInfoDict = token.scanInfoDict, None
        if scanInfoDict is not None:
            if scanInfoDict.get(PARTICIPANTS_KEY) == list(token.participants):
                scanInfoDict.pop(PARTICIPANTS_KEY, None)
            scanInfoDict.pop(EXCLUDED_KEY, None)

        runReleaseCallbacks = ()
        runReleaseToken = None
        with self._stateLock:
            if self._activeToken is token:
                self._activeToken = None
            activeRun = self._activeRunToken
            if (activeRun is not None
                    and activeRun.owner is token.owner
                    and activeRun.releaseRequested):
                activeRun.releaseBarrierCleared = True
                if not activeRun.holdReleaseUntilFinalized:
                    activeRun.released = True
                    self._activeRunToken = None
                runReleaseCallbacks = tuple(activeRun.releaseCallbacks)
                activeRun.releaseCallbacks.clear()
                if runReleaseCallbacks:
                    activeRun.releaseCallbackInFlight = True
                    runReleaseToken = activeRun

        self._runReleaseCallbacks(runReleaseToken, runReleaseCallbacks)
        if onComplete is not None:
            try:
                onComplete()
            except Exception:
                self._log('Scan-completion callback failed')

    def resolveActive(self, mode=FINISH_GRACEFUL, onComplete=None,
                      timeoutS=DEFAULT_FINISH_TIMEOUT_S):
        """Resolve whichever iteration is in flight — the completion path for
        callers that only see a signal (``sigScanDone``) and not the token."""
        with self._stateLock:
            token = self._activeToken
        return self.resolve(token, mode, onComplete=onComplete,
                            timeoutS=timeoutS)

    def _log(self, message):
        if self._logger is not None:
            self._logger.error(message, exc_info=True)

    def _runReleaseCallbacks(self, token, callbacks):
        try:
            for callback in callbacks:
                try:
                    callback()
                except Exception:
                    self._log('Scan-run release callback failed')
        finally:
            if token is not None and callbacks:
                with self._stateLock:
                    token.releaseCallbackInFlight = False


def getSharedScanExecutionCoordinator(detectorsManager, nidaqManager, *,
                                      logger=None, scheduleTimeout=None):
    """Return the one scan coordinator associated with an NI-DAQ manager.

    Scan-widget and event-triggered controllers receive the same global
    iteration owner.  Storing it on the NI-DAQ manager keeps the lifetime tied
    to the hardware instance and avoids a process-global registry retaining
    managers after shutdown.
    """
    with _sharedCoordinatorLock:
        coordinator = getattr(
            nidaqManager, _SHARED_COORDINATOR_ATTR, None
        )
        # Mock-like managers often synthesize arbitrary attributes from
        # __getattr__; only a real coordinator counts as an existing binding.
        if not isinstance(coordinator, ScanExecutionCoordinator):
            coordinator = ScanExecutionCoordinator(
                detectorsManager,
                nidaqManager,
                logger=logger,
                scheduleTimeout=scheduleTimeout,
            )
            setattr(nidaqManager, _SHARED_COORDINATOR_ATTR, coordinator)
        elif coordinator._detectorsManager is not detectorsManager:
            raise RuntimeError(
                'NI-DAQ manager is already bound to a different '
                'DetectorsManager scan coordinator'
            )
    return coordinator.configure(
        logger=logger, scheduleTimeout=scheduleTimeout
    )


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
