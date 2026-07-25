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

from ._acquisition_leases import LeasePurpose

#: ``scanInfoDict`` key carrying the participant snapshot to the detectors.
#: Absent means "legacy scan" — every scan-driven detector participates, which
#: is what preserves behaviour for any entry point not yet routed through here.
PARTICIPANTS_KEY = 'participants'

FINISH_GRACEFUL = 'graceful'
FINISH_ABORT = 'abort'


class ScanIterationToken:
    """Idempotent per-iteration lifecycle handle.

    Held by whoever armed the iteration; ``resolve`` is safe to call from any
    number of termination paths and does its work at most once.
    """

    __slots__ = ('participants', 'leaseHandle', 'resolved')

    def __init__(self, participants):
        self.participants = tuple(participants)
        self.leaseHandle = None
        self.resolved = False

    def __repr__(self):
        return (f'ScanIterationToken(participants={list(self.participants)},'
                f' resolved={self.resolved})')


class ScanExecutionCoordinator:
    """Arms one scan iteration and guarantees exactly-once completion.

    The caller supplies the NI-DAQ manager and DetectorsManager; nothing here
    touches Qt, blocks the UI thread, or holds the DetectorsManager lock across
    a wait.
    """

    def __init__(self, detectorsManager, nidaqManager, logger=None):
        self._detectorsManager = detectorsManager
        self._nidaqManager = nidaqManager
        self._logger = logger
        self._activeToken = None

    @property
    def activeToken(self):
        """The unresolved token of the in-flight iteration, if any."""
        return self._activeToken

    # ------------------------------------------------------------------ #
    # Participant composition                                            #
    # ------------------------------------------------------------------ #

    def composeParticipants(self):
        """Scan-driven detectors taking part in the next iteration.

        Phase 3 keeps this at "every scan-driven detector", which is exactly
        today's behaviour. Phase 5 narrows it to
        ``(selected scan-driven) | (scan-driven held by RECORDING / SNAP /
        WORKFLOW / EVENT_* / GENERIC leases)``. Faulted detectors are excluded
        here and always: a detector whose stop failed must not rejoin a scan.
        """
        names = self._detectorsManager.getAllDeviceNames(
            condition=lambda detector: getattr(detector, 'isScanDriven', False)
        )
        return [name for name in names
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
            self._activeToken = token
            self._nidaqManager.runScan(signalDict, scanInfoDict)
        except BaseException:
            self.resolve(token, FINISH_ABORT)
            raise
        return token

    def resolve(self, token, mode=FINISH_GRACEFUL):
        """Finish an iteration exactly once: ``finishScan`` every participant,
        then release the SCAN lease.

        ``finishScan`` runs for every participant regardless of refcount — a
        TimeTagger that also holds a WORKFLOW lease goes 2->1, so no hardware
        stop happens, but it still needs its final read. General hardware stop
        happens only at aggregate zero, via the lease release below.

        Returns True if this call did the work, False if it was already done.
        """
        if token is None or token.resolved:
            return False
        token.resolved = True

        for name in token.participants:
            try:
                self._detectorsManager.execOn(
                    name, lambda detector: detector.finishScan(mode)
                )
            except Exception:
                self._log(f'finishScan({mode}) failed for detector "{name}"')

        if token.leaseHandle is not None:
            try:
                self._detectorsManager.release(token.leaseHandle)
            except Exception:
                self._log('Failed to release the scan detector lease')
            token.leaseHandle = None

        if self._activeToken is token:
            self._activeToken = None
        return True

    def resolveActive(self, mode=FINISH_GRACEFUL):
        """Resolve whichever iteration is in flight — the completion path for
        callers that only see a signal (``sigScanDone``) and not the token."""
        return self.resolve(self._activeToken, mode)

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
