"""Cooperative cancellation for long-running callers (scripts, workers).

A :class:`CancelToken` is a small state machine shared between the thread
that *requests* a stop (normally the GUI thread) and the thread that has to
*honour* it (normally a script thread). It is deliberately independent of Qt
so that the API layer in ``imcommon`` can consult it without importing
``imscripting``.

State machine (all transitions under the token's lock)::

    IDLE ──requestStop()──▶ REQUESTED ──first checkpoint()──▶ DELIVERED ──budget elapsed──▶ EXPIRED

* ``REQUESTED``: a stop is pending but the runner has not noticed yet. The
  first cooperative wait that reaches :meth:`checkpoint` raises
  :class:`OperationCancelled` exactly once and moves the token on.
* ``DELIVERED``: the *cleanup window*. Cooperative waits and API calls work
  normally so that ``finally`` blocks can stop a recording, park stages or
  close files, bounded by their own timeouts.
* ``EXPIRED``: the cleanup budget ran out. Every checkpoint raises again,
  immediately, and new API invocations are refused before dispatch.

:class:`OperationCancelled` derives from :class:`BaseException`, like
``KeyboardInterrupt``, so a user script's ``except Exception:`` cannot
swallow it.
"""

import threading
import time


class OperationCancelled(BaseException):
    """Raised inside a cancelled run at its next cooperative checkpoint."""


class CancelToken:
    IDLE = 'idle'
    REQUESTED = 'requested'
    DELIVERED = 'delivered'
    EXPIRED = 'expired'

    def __init__(self, cleanupBudgetS=30.0, clock=time.monotonic):
        self._lock = threading.RLock()
        self._clock = clock
        self._state = self.IDLE
        self._cleanupBudgetS = float(cleanupBudgetS)
        self._requestedAt = None
        self._deliveredAt = None
        self._inflight = None  # the one API invocation this run is blocked on

    # ------------------------------------------------------------------ #
    # State                                                               #
    # ------------------------------------------------------------------ #
    @property
    def state(self):
        with self._lock:
            self._expireIfDue()
            return self._state

    @property
    def cleanupBudgetS(self):
        return self._cleanupBudgetS

    @property
    def cleanupDeadline(self):
        """Monotonic time after which cancellation re-arms, or None."""
        with self._lock:
            if self._requestedAt is None:
                return None
            return self._requestedAt + self._cleanupBudgetS

    def isStopRequested(self):
        return self.state != self.IDLE

    def isDelivered(self):
        return self.state in (self.DELIVERED, self.EXPIRED)

    def isExpired(self):
        return self.state == self.EXPIRED

    def _expireIfDue(self):
        if (
            self._state == self.DELIVERED
            and self._requestedAt is not None
            and self._clock() >= self._requestedAt + self._cleanupBudgetS
        ):
            self._state = self.EXPIRED

    # ------------------------------------------------------------------ #
    # Requesting side (GUI thread)                                        #
    # ------------------------------------------------------------------ #
    def requestStop(self, *, cleanupBudgetS=None):
        """Ask the run to stop. Returns True on the IDLE → REQUESTED edge.

        Also cancels the run's in-flight API invocation if it is still
        pending (never started on the GUI thread) and was issued before the
        stop, so that no command dispatched before Stop executes after it.
        """
        with self._lock:
            if cleanupBudgetS is not None:
                self._cleanupBudgetS = min(self._cleanupBudgetS, float(cleanupBudgetS))
            if self._state != self.IDLE:
                return False
            self._state = self.REQUESTED
            self._requestedAt = self._clock()
            inflight = self._inflight
            if inflight is not None and getattr(inflight, 'epoch', 0) == 0:
                cancelPending = getattr(inflight, 'cancelPending', None)
                if callable(cancelPending):
                    cancelPending()
            return True

    def expireNow(self):
        """Force the EXPIRED state (shutdown escalation)."""
        with self._lock:
            if self._state == self.IDLE:
                self._state = self.REQUESTED
                self._requestedAt = self._clock()
            self._state = self.EXPIRED

    # ------------------------------------------------------------------ #
    # Honouring side (the cancelled thread)                               #
    # ------------------------------------------------------------------ #
    def checkpoint(self):
        """Cooperative check. Raises OperationCancelled per the state rules."""
        with self._lock:
            self._expireIfDue()
            if self._state == self.REQUESTED:
                self._state = self.DELIVERED
                self._deliveredAt = self._clock()
                raise OperationCancelled('Script execution was cancelled')
            if self._state == self.EXPIRED:
                raise OperationCancelled(
                    'Script execution was cancelled and its cleanup budget '
                    f'of {self._cleanupBudgetS:g} s has expired'
                )

    def markDelivered(self):
        """Record that cancellation reached the run by another route
        (asynchronous exception injection). No-op unless REQUESTED."""
        with self._lock:
            if self._state == self.REQUESTED:
                self._state = self.DELIVERED
                self._deliveredAt = self._clock()
                return True
            return False

    def currentEpoch(self):
        """0 before delivery, 1 during the cleanup window."""
        with self._lock:
            self._expireIfDue()
            return 1 if self._state == self.DELIVERED else 0

    # ------------------------------------------------------------------ #
    # API invocation protocol (see imcommon.model.api)                    #
    # ------------------------------------------------------------------ #
    def openInvocation(self, invocation):
        """Register the run's in-flight API invocation before dispatch.

        REQUESTED → deliver now (raise, nothing dispatched); EXPIRED → raise;
        IDLE → epoch 0; DELIVERED → epoch 1 (an authorised cleanup call).
        """
        with self._lock:
            self.checkpoint()
            invocation.epoch = self.currentEpoch()
            self._inflight = invocation
            return invocation.epoch

    def closeInvocation(self, invocation):
        with self._lock:
            if self._inflight is invocation:
                self._inflight = None

    def admit(self, invocation):
        """GUI-thread decision whether a pending invocation may start."""
        with self._lock:
            self._expireIfDue()
            epoch = getattr(invocation, 'epoch', 0)
            if self._state == self.IDLE:
                return epoch == 0
            if self._state == self.DELIVERED:
                return epoch == 1
            return False


_local = threading.local()


def setCurrentCancelToken(token):
    _local.token = token


def clearCurrentCancelToken():
    _local.token = None


def currentCancelToken():
    return getattr(_local, 'token', None)


def checkpoint():
    """Module-level convenience: checkpoint the current thread's token."""
    token = currentCancelToken()
    if token is not None:
        token.checkpoint()


def cancellableSleep(seconds, pollIntervalS=0.05):
    """Sleep in slices, honouring the current cancel token."""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        checkpoint()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(pollIntervalS, remaining))


# Copyright (C) 2020-2026 ImSwitch developers
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
