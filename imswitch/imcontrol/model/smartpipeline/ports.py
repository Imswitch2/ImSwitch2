"""What the engine runs against: the four ports, as protocols, and the inbox
that turns a stream of pushed frames into the pull the engine does.

Everything hardware or ImProcess is behind one of these; the engine itself
imports neither. ``imswitch.imcontrol.model.smartpipeline.mock`` implements
them for tests and offline runs; ImProcess implements the step runner and a
recording source (``imswitch.improcess.workflows.smartpipeline``); Phase 2
implements the live source and the action backend over the scripting API.

**The completion contract** (one per started action): ``done`` says the
backend will not change ``outcome``, ``result`` or ``error`` any more;
``ended`` says nothing is still executing on the hardware -- for a stage
move the two coincide, for an aborted scan ``done`` (cancelled) comes first
and ``ended`` when the scanner has stopped. ``abort()`` asks the backend to
stop early and returns whether it can; the engine then waits for ``ended``,
bounded, and reports an action that did not end. A completion carries the
run ``epoch`` it was started in, so one that finishes after a stop or a
re-arm is ignored, never acted on.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from imswitch.imcommon.model.smartpipeline import ActionCatalog, Frame

#: What a completion ends as.
PENDING = "pending"
OK = "ok"
FAILED = "failed"
CANCELLED = "cancelled"
TIMEOUT = "timeout"
OUTCOMES = (PENDING, OK, FAILED, CANCELLED, TIMEOUT)


class StepError(RuntimeError):
    """A step runner refused or failed a step; the message says why."""


@runtime_checkable
class FrameSource(Protocol):
    """Where ticks come from. ``frames()`` yields one :class:`Frame` per
    tick and blocks until there is one; it ends when the source is exhausted
    or :meth:`stop` was called (thread-safe). ``close()`` releases what the
    source holds; idempotent."""

    def frames(self) -> Iterator[Frame]: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class StepRunner(Protocol):
    """Runs the processing steps the engine does not know (ImProcess's)."""

    def image_result(self, frame: Frame) -> Any:
        """What the source port carries for ``frame``: an object the
        processing steps read (ImProcess: an image result)."""

    def run_step(self, step, inputs: list, *, state: dict, frame: Frame | None = None) -> dict[str, Any]:
        """``{port: value}`` the step produced over ``inputs`` (one value per
        input reference, in order). ``state`` is the step's dict across ticks
        (the very dict: what is stored there is found again). Raises
        :class:`StepError` (or anything) on failure."""


@runtime_checkable
class Completion(Protocol):
    epoch: int
    done: bool
    ended: bool
    outcome: str
    result: Any
    error: str | None

    def wait(self, timeout: float | None = None) -> bool:
        """Block until ``done`` (or ``timeout`` seconds); returns ``done``."""

    def abort(self) -> bool:
        """Ask the backend to stop the action; ``True`` when it can."""


@dataclass
class ActionRequest:
    """What the engine hands the backend to start an action."""

    step_id: str
    action: str
    params: dict
    inputs: list
    mode: str
    tick: int
    epoch: int
    timeout_s: float | None = None


@runtime_checkable
class ActionBackend(Protocol):
    catalog: ActionCatalog

    def start(self, request: ActionRequest) -> Completion:
        """Start the action and return its completion. Called only in
        Experiment mode and only for an action the catalogue names; raises
        on refusal (the engine treats that as the action failing)."""


# --------------------------------------------------------------------------
# a completion anyone can make
# --------------------------------------------------------------------------

class SimpleCompletion:
    """A completion the holder settles by hand: :meth:`complete`,
    :meth:`fail`, :meth:`cancel`. Thread-safe. ``ended`` follows ``done``
    unless the holder says execution goes on (``ended=False`` at
    settlement) and later calls :meth:`end`."""

    def __init__(self, epoch: int = 0, *, result: Any = None, done: bool = False):
        self.epoch = int(epoch)
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._ended = threading.Event()
        self.outcome = PENDING
        self.result = result
        self.error: str | None = None
        self.abort_requested = False
        self._callbacks: list = []
        if done:
            self.complete(result)

    def add_done_callback(self, callback) -> None:
        """``callback(completion)`` once settled -- at once when it already is."""
        with self._lock:
            if not self._done.is_set():
                self._callbacks.append(callback)
                return
        callback(self)

    @property
    def done(self) -> bool:
        return self._done.is_set()

    @property
    def ended(self) -> bool:
        return self._ended.is_set()

    def _settle(self, outcome: str, *, result=None, error=None, ended=True) -> None:
        with self._lock:
            if self._done.is_set():
                return
            self.outcome, self.result, self.error = outcome, result, error
            self._done.set()
            if ended:
                self._ended.set()
            callbacks, self._callbacks = list(self._callbacks), []
        for callback in callbacks:
            try:
                callback(self)
            except Exception:  # noqa: BLE001 - a listener's failure is its own
                pass

    def complete(self, result: Any = None, *, ended: bool = True) -> None:
        self._settle(OK, result=result, ended=ended)

    def fail(self, error: str, *, ended: bool = True) -> None:
        self._settle(FAILED, error=str(error), ended=ended)

    def cancel(self, *, ended: bool = True) -> None:
        self._settle(CANCELLED, error="cancelled", ended=ended)

    def end(self) -> None:
        """Execution is over (for a settlement that said it was not yet)."""
        self._ended.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    def wait_ended(self, timeout: float | None = None) -> bool:
        return self._ended.wait(timeout)

    #: Optional: what an abort does besides cancelling (a scan's abort).
    abort_hook = None

    def abort(self) -> bool:
        self.abort_requested = True
        hook = self.abort_hook
        can = True
        if callable(hook):
            try:
                can = bool(hook())
            except Exception:  # noqa: BLE001
                can = False
        self.cancel(ended=can)
        return can


# --------------------------------------------------------------------------
# the inbox: pushed frames into pulled ticks
# --------------------------------------------------------------------------

@dataclass
class InboxStats:
    pushed: int = 0
    taken: int = 0
    replaced: int = 0      # a frame overwritten by a newer one before it was taken
    duplicates: int = 0    # a frame with a sequence not above the last pushed
    aged_out: int = 0      # a frame too old when it was taken
    closed: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class CoalescingInbox:
    """One slot between a frame producer and the engine: *latest wins*.

    ``push`` keeps the newest frame only (an older one still waiting is
    replaced and counted), refuses a frame whose ``sequence`` is not above
    the last one pushed (a duplicate, or a late delivery), and never blocks
    the producer. ``frames()`` is a :class:`FrameSource`: it hands the engine
    the waiting frame, drops one that waited longer than ``max_age_s``
    (counted, so the panel can show drops), and blocks while the slot is
    empty, until :meth:`stop`. The producer's thread never runs a step.
    """

    def __init__(self, *, max_age_s: float | None = None, clock=time.monotonic):
        self._slot: tuple[Frame, float] | None = None
        self._condition = threading.Condition()
        self._stopped = False
        self._last_sequence: int | None = None
        self._max_age_s = None if max_age_s is None else float(max_age_s)
        self._clock = clock
        self.stats = InboxStats()

    def push(self, frame: Frame) -> bool:
        """Offer a frame; returns whether it was kept."""
        with self._condition:
            self.stats.pushed += 1
            if self._stopped:
                return False
            if self._last_sequence is not None and frame.sequence <= self._last_sequence:
                self.stats.duplicates += 1
                return False
            self._last_sequence = frame.sequence
            if self._slot is not None:
                self.stats.replaced += 1
            self._slot = (frame, self._clock())
            self._condition.notify_all()
            return True

    def take(self, timeout: float | None = None) -> Frame | None:
        """The waiting frame, or ``None`` after ``timeout`` or once stopped."""
        deadline = None if timeout is None else self._clock() + timeout
        with self._condition:
            while True:
                if self._slot is not None:
                    frame, pushed_at = self._slot
                    self._slot = None
                    if self._max_age_s is not None and self._clock() - pushed_at > self._max_age_s:
                        self.stats.aged_out += 1
                        continue
                    self.stats.taken += 1
                    return frame
                if self._stopped:
                    return None
                remaining = None if deadline is None else deadline - self._clock()
                if remaining is not None and remaining <= 0:
                    return None
                self._condition.wait(0.05 if remaining is None else min(0.05, remaining))

    def frames(self) -> Iterator[Frame]:
        while True:
            frame = self.take()
            if frame is None:
                return
            yield frame

    @property
    def waiting(self) -> bool:
        with self._condition:
            return self._slot is not None

    def stop(self) -> None:
        with self._condition:
            self._stopped = True
            self.stats.closed = True
            self._condition.notify_all()

    def close(self) -> None:
        self.stop()
        with self._condition:
            self._slot = None


__all__ = [
    "CANCELLED",
    "FAILED",
    "OK",
    "OUTCOMES",
    "PENDING",
    "TIMEOUT",
    "ActionBackend",
    "ActionRequest",
    "CoalescingInbox",
    "Completion",
    "FrameSource",
    "InboxStats",
    "SimpleCompletion",
    "StepError",
    "StepRunner",
]


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
