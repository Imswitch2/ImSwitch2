"""A request to put the stage somewhere, resolved by whoever owns the stage.

The seam between a session that decides *when* the next acquisition happens and
a workflow that decides *where*. A lapse advances from Qt timers on the GUI
thread; a tiling run moves and settles on its own worker. If the session simply
called the workflow and waited, the event loop would stop for the whole move —
at every point — starving exactly the timers the focus lock runs on. That
failure has been paid for once already in this project, when the focus estimator
ran on the GUI thread and the lock silently stopped holding for an entire run.

So positioning is requested, not called: the session hands over a request, goes
back to its event loop, and arms only once the request reports success.

Copyright (C) 2020-2026 ImSwitch developers
This file is part of ImSwitch.

ImSwitch is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

ImSwitch is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

from __future__ import annotations

import enum
import threading
import time
from typing import Optional


class PositioningOutcome(enum.Enum):
    """How a positioning request ended.

    Four terminals, not two. ``CANCELLED`` because the operator can stop a run
    mid-move and the session must not then arm; ``TIMED_OUT`` because a stage
    that never reports arrival would otherwise wedge the session forever.
    """

    PENDING = 'pending'
    RESOLVED = 'resolved'
    FAILED = 'failed'
    CANCELLED = 'cancelled'
    TIMED_OUT = 'timed-out'

    @property
    def settled(self) -> bool:
        return self is not PositioningOutcome.PENDING

    @property
    def mayProceed(self) -> bool:
        """Only one outcome permits acquiring at this position."""
        return self is PositioningOutcome.RESOLVED


class PositioningRequest:
    """One "put the stage at point N and tell me when it is settled".

    Resolved from the worker that owns the stage; observed from wherever the
    session runs. Settling is one-way and one-time: the first terminal wins, so
    a late resolve after a cancel cannot revive a run the operator stopped.
    """

    def __init__(self, index: int, timeout_s: float = 60.0):
        self.index = int(index)
        self.timeout_s = float(timeout_s)
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._outcome = PositioningOutcome.PENDING
        self._message = ''
        self._deadline = time.monotonic() + self.timeout_s

    # -- resolved by the stage owner ----------------------------------

    def resolve(self) -> bool:
        """The stage is there and has settled."""
        return self._settle(PositioningOutcome.RESOLVED, '')

    def fail(self, message: str) -> bool:
        return self._settle(PositioningOutcome.FAILED, str(message))

    def cancel(self, message: str = 'cancelled') -> bool:
        return self._settle(PositioningOutcome.CANCELLED, str(message))

    def _settle(self, outcome: PositioningOutcome, message: str) -> bool:
        with self._lock:
            if self._outcome.settled:
                return False
            self._outcome = outcome
            self._message = message
        self._event.set()
        return True

    # -- observed by the session --------------------------------------

    @property
    def outcome(self) -> PositioningOutcome:
        """The current terminal, expiring the request if its time is up.

        Checked rather than scheduled: a timer would have to live on some
        thread, and the whole point is not to depend on which one.
        """
        with self._lock:
            if self._outcome.settled:
                return self._outcome
            expired = time.monotonic() >= self._deadline
        if expired:
            self._settle(
                PositioningOutcome.TIMED_OUT,
                f'the stage did not report position {self.index} within '
                f'{self.timeout_s:g} s',
            )
        with self._lock:
            return self._outcome

    @property
    def message(self) -> str:
        with self._lock:
            return self._message

    @property
    def settled(self) -> bool:
        return self.outcome.settled

    @property
    def mayProceed(self) -> bool:
        return self.outcome.mayProceed

    def wait(self, timeout_s: Optional[float] = None) -> PositioningOutcome:
        """Block until this settles. **Never call from the GUI thread.**

        Present for the worker side and for tests. The session must poll
        :attr:`outcome` from its own event loop instead — blocking there is
        the exact thing this class exists to prevent.
        """
        remaining = self.timeout_s if timeout_s is None else timeout_s
        self._event.wait(max(0.0, remaining))
        return self.outcome

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return (f'<PositioningRequest #{self.index} '
                f'{self.outcome.value}{": " + self.message if self.message else ""}>')
