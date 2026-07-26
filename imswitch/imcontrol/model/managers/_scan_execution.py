"""Shared scan-execution lifecycle for every NI-DAQ scan entry point.

There are five direct ``nidaqManager.runScan`` callers — the four
``SuperScanController`` subclasses and ``EtSTEDTriggeredScanRunner`` — so the
lifecycle cannot live on ``SuperScanController`` without missing one. It lives
here instead, framework-free (no Qt) so it is testable in isolation.

Two levels, deliberately distinct (see the design plan, R7-2/R7-4):

- A **scan run** is one user/API-initiated scan including all repeat
  iterations, MoNaLISA ``autoAxial`` follow-ups and ``isNonFinalPartOfSequence``
  parts. ``sigScanStarting``/``sigScanEnded`` and pre-arm lease span are
  run-level and are NOT this class's concern.
- A **scan iteration** is one ``runScan`` -> ``scanDone`` cycle. The participant
  snapshot, the SCAN lease, the lifecycle token and ``finishScan`` are all
  iteration-level, because each iteration is a frame.

Completion runs **exactly once** per iteration across all five terminations:
busy refusal, build failure, arm exception, normal completion and abort.

Design reference: docs/design/plans/detector-acquisition-selection.md.
"""

import threading

from ._acquisition_leases import LeasePurpose

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

    __slots__ = ('participants', 'leaseHandle', 'resolved', 'finishing',
                 'pending', 'onComplete', 'timedOut', '_lock')

    def __init__(self, participants):
        self.participants = tuple(participants)
        self.leaseHandle = None
        self.resolved = False
        self.finishing = False
        self.pending = set()
        self.onComplete = None
        self.timedOut = False
        self._lock = threading.Lock()

    def __repr__(self):
        return (f'ScanIterationToken(participants={list(self.participants)},'
                f' finishing={self.finishing}, resolved={self.resolved},'
                f' pending={sorted(self.pending)})')


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

    @property
    def activeToken(self):
        """The unresolved token of the in-flight iteration, if any."""
        return self._activeToken

    # ------------------------------------------------------------------ #
    # Participant composition                                            #
    # ------------------------------------------------------------------ #

    def scanDrivenDetectors(self):
        """Every scan-driven detector, participating or not."""
        return self._detectorsManager.getAllDeviceNames(
            condition=lambda detector: getattr(detector, 'isScanDriven', False)
        )

    def composeParticipants(self):
        """Scan-driven detectors taking part in the next iteration.

        Phase 3 keeps this at "every scan-driven detector", which is exactly
        today's behaviour. Phase 5 narrows it to
        ``(selected scan-driven) | (scan-driven held by RECORDING / SNAP /
        WORKFLOW / EVENT_* / GENERIC leases)``. Faulted detectors are excluded
        here and always: a detector whose stop failed must not rejoin a scan.
        """
        return [name for name in self.scanDrivenDetectors()
                if not self._detectorsManager.isDetectorFaulted(name)]

    # ------------------------------------------------------------------ #
    # Arm / resolve                                                      #
    # ------------------------------------------------------------------ #

    def arm(self, signalDict, scanInfoDict):
        """Compose the snapshot, take the SCAN lease, inject the participants
        and start the scan. Returns the iteration's token.

        Any failure — including a ``ScanBusyError`` refusal, which starts
        nothing and emits no lifecycle signal — resolves the token and unwinds
        the lease before propagating, so a refused arm can never strand
        ownership.
        """
        participants = self.composeParticipants()
        token = ScanIterationToken(participants)
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
            self._activeToken = token
            self._nidaqManager.runScan(signalDict, scanInfoDict)
        except BaseException:
            # Nothing ran, so no participant has async work outstanding: no
            # timeout needed, the barrier clears immediately.
            self.resolve(token, FINISH_ABORT, timeoutS=None)
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
            hasPending = bool(token.pending)

        if not hasPending:
            self._completeToken(token)
            return True

        if timeoutS and self._scheduleTimeout is not None:
            self._scheduleTimeout(
                timeoutS, lambda: self._onFinishTimeout(token)
            )

        for name in tuple(token.participants):
            try:
                self._detectorsManager.execOn(
                    name,
                    lambda detector, n=name: detector.finishScan(
                        mode, lambda n=n: self._acknowledge(token, n)
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
            if token.pending:
                return
        self._completeToken(token)

    def _onFinishTimeout(self, token):
        with token._lock:
            if token.resolved or not token.pending:
                return
            stuck = sorted(token.pending)
            token.timedOut = True
            token.pending.clear()
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

        if handle is not None:
            try:
                self._detectorsManager.release(handle)
            except Exception:
                self._log('Failed to release the scan detector lease')

        if self._activeToken is token:
            self._activeToken = None

        if onComplete is not None:
            try:
                onComplete()
            except Exception:
                self._log('Scan-completion callback failed')

    def resolveActive(self, mode=FINISH_GRACEFUL, onComplete=None,
                      timeoutS=DEFAULT_FINISH_TIMEOUT_S):
        """Resolve whichever iteration is in flight — the completion path for
        callers that only see a signal (``sigScanDone``) and not the token."""
        return self.resolve(self._activeToken, mode, onComplete=onComplete,
                            timeoutS=timeoutS)

    def _log(self, message):
        if self._logger is not None:
            self._logger.error(message, exc_info=True)


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
