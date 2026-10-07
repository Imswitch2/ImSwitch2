"""Resource reservations and command admission.

Design: ``docs/design/plans/transient-instruments-step-scans.md`` §7.

Two separate problems:

- **transaction ownership** (this module): who may *change the state* of a
  device — a rotator controller, a stage controller, a laser, an instrument,
  the waveform outputs — while a measurement holds it;
- **byte serialisation** on a shared transport, which stays with that
  transport's own I/O lock.

Every mutating command on a reservable resource is *admitted* here first:
:meth:`ResourceRegistry.admit` returns an **in-flight ticket**, which is
released only when the command has physically returned. Reservations take
the same lock. :meth:`ResourceRegistry.reserve` marks its resources
*pending* (new foreign admissions are refused from that instant), then waits,
bounded, until every foreign ticket on them has been released. So a
reservation never succeeds while a conflicting command is still running. A
command that was admitted just before the reservation finishes first.

Ownership is never ambient. A reservation's **token** is passed explicitly
with every command (``owner=token``) and expires when the reservation ends;
an expired token is refused, never silently treated as "no owner".
Re-entrant calls on the same thread (a manager method calling another one of
its own) share the outer command's ticket.
"""
from __future__ import annotations

import itertools
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, Iterator, List, Optional


class ResourceReservedError(RuntimeError):
    """The resource is reserved (or being reserved) by another owner."""


class ReservationExpiredError(RuntimeError):
    """The token's reservation has ended; it never becomes valid again."""


#: Reserved for the waveform-output tasks of scans (NI-DAQ, TriggerScope).
#: Coarse on purpose: a waveform scan and a measurement run exclude each
#: other until per-channel mapping is reliable (plan §7.5).
WAVEFORM_OUTPUT = 'waveform-output'


def rotator_key(name: str) -> str:
    return f'rotator:{name}'


def positioner_key(name: str) -> str:
    # The manager is the controller: reserving any axis reserves all of them.
    return f'positioner:{name}'


def laser_key(name: str) -> str:
    return f'laser:{name}'


def instrument_key(name: str) -> str:
    return f'instrument:{name}'


@dataclass(frozen=True)
class Reservation:
    token: str
    owner: str
    resources: FrozenSet[str]


@dataclass(eq=False)
class Ticket:
    """One admitted command, in flight until :meth:`ResourceRegistry.release_ticket`."""

    key: str
    token: Optional[str]
    label: str
    started: float
    thread: int
    nested: bool = False
    reentrant: bool = True
    released: bool = False


@dataclass
class _Pending:
    owner: str
    keys: FrozenSet[str]


class ResourceRegistry:
    def __init__(self) -> None:
        self._lock = threading.Condition(threading.Lock())
        self._held: Dict[str, Reservation] = {}
        self._active: Dict[str, Reservation] = {}
        self._pending: List[_Pending] = []
        self._tickets: Dict[str, List[Ticket]] = {}
        self._counter = itertools.count(1)
        self._local = threading.local()
        self._listeners: List = []

    # ------------------------------------------------------------- admission
    def admit(self, key: str, token: Optional[str] = None, *, label: str = '',
              reentrant: bool = True) -> Ticket:
        """Admit one command on ``key``; returns its in-flight ticket.

        Raises :class:`ResourceReservedError` if another owner holds or is
        reserving ``key``, and :class:`ReservationExpiredError` for a token
        whose reservation has ended.

        ``reentrant=False`` is for holds that outlive the call and may be
        released on another thread (a scan's waveform outputs): they do not
        make later calls on this thread count as nested.
        """
        depth = self._depth()
        if reentrant and depth.get(key, 0) > 0:
            # A call nested inside a command this thread already holds.
            depth[key] += 1
            return Ticket(key, token, label, time.monotonic(), threading.get_ident(),
                          nested=True)
        with self._lock:
            if token is not None and token not in self._active:
                raise ReservationExpiredError(f'reservation {token} has ended')
            holder = self._held.get(key)
            if holder is not None and holder.token != token:
                raise ResourceReservedError(
                    f'{key} is reserved by {holder.owner}'
                    + (f'; refused: {label}' if label else ''))
            if holder is None:
                for pending in self._pending:
                    if key in pending.keys:
                        raise ResourceReservedError(
                            f'{key} is being reserved by {pending.owner}'
                            + (f'; refused: {label}' if label else ''))
            ticket = Ticket(key, token, label, time.monotonic(), threading.get_ident(),
                            reentrant=reentrant)
            self._tickets.setdefault(key, []).append(ticket)
        if reentrant:
            depth[key] = 1
        return ticket

    def release_ticket(self, ticket: Ticket) -> None:
        if ticket.nested:
            depth = self._depth()
            if depth.get(ticket.key, 0) > 1:
                depth[ticket.key] -= 1
            return
        if ticket.reentrant and ticket.thread == threading.get_ident():
            self._depth().pop(ticket.key, None)
        with self._lock:
            if ticket.released:
                return
            ticket.released = True
            tickets = self._tickets.get(ticket.key, [])
            if ticket in tickets:
                tickets.remove(ticket)
            if not tickets:
                self._tickets.pop(ticket.key, None)
            self._lock.notify_all()
        self._notify()

    @contextmanager
    def command(self, key: str, token: Optional[str] = None, *, label: str = '') -> Iterator[Ticket]:
        """``with registry.command(key, token): <the hardware call>``."""
        ticket = self.admit(key, token, label=label)
        try:
            yield ticket
        finally:
            self.release_ticket(ticket)

    def _depth(self) -> Dict[str, int]:
        depth = getattr(self._local, 'depth', None)
        if depth is None:
            depth = self._local.depth = {}
        return depth

    # ----------------------------------------------------------- reservation
    def reserve(self, resources: Iterable[str], owner: str, *,
                deadline_s: float = 0.0) -> Reservation:
        """Reserve every resource or none, once their in-flight commands end.

        Waits at most ``deadline_s`` for foreign in-flight commands; a command
        still running at the deadline refuses the reservation, naming it.
        """
        wanted = frozenset(resources)
        deadline = time.monotonic() + max(0.0, float(deadline_s))
        with self._lock:
            self._check_free(wanted)
            pending = _Pending(owner, wanted)
            self._pending.append(pending)
            try:
                while True:
                    busy = [t for k in wanted for t in self._tickets.get(k, ())]
                    if not busy:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        names = ', '.join(sorted(
                            f'{t.key} ({t.label or "a command"} running '
                            f'{time.monotonic() - t.started:.1f} s)' for t in busy))
                        raise ResourceReservedError(f'cannot reserve, still running: {names}')
                    self._lock.wait(remaining)
                    # Nobody else can have reserved meanwhile -- our pending
                    # mark refuses them -- but recheck for robustness.
                    self._check_free(wanted, ignore=pending)
                reservation = Reservation(f'rsv-{next(self._counter)}', owner, wanted)
                for key in wanted:
                    self._held[key] = reservation
                self._active[reservation.token] = reservation
            finally:
                self._pending.remove(pending)
                self._lock.notify_all()
        self._notify()
        return reservation

    def _check_free(self, wanted: FrozenSet[str], ignore: Optional[_Pending] = None) -> None:
        taken = {k: self._held[k] for k in wanted if k in self._held}
        if taken:
            holders = ', '.join(sorted(f'{k} (held by {h.owner})' for k, h in taken.items()))
            raise ResourceReservedError(f'cannot reserve: {holders}')
        for pending in self._pending:
            if pending is ignore:
                continue
            overlap = wanted & pending.keys
            if overlap:
                raise ResourceReservedError(
                    f'cannot reserve: {", ".join(sorted(overlap))} being reserved by '
                    f'{pending.owner}')

    def release(self, token: str, resources: Optional[Iterable[str]] = None) -> None:
        """Release all (or some) resources; the token expires with the last one."""
        with self._lock:
            reservation = self._active.get(token)
            if reservation is None:
                return
            subset = reservation.resources if resources is None else frozenset(resources)
            for key in subset:
                if self._held.get(key) is reservation:
                    del self._held[key]
            if not any(h is reservation for h in self._held.values()):
                del self._active[token]
            self._lock.notify_all()
        self._notify()

    def end_all_reservations(self) -> List[Reservation]:
        """Expire every reservation (application shutdown); returns them.

        Runs after the scripting drain, so the safety actions of shutdown
        (lasers off) are admitted. Commands already in flight keep their
        tickets: hardware finalization still waits for them.
        """
        with self._lock:
            ended = list(self._active.values())
            self._held.clear()
            self._active.clear()
            self._lock.notify_all()
        self._notify()
        return ended

    # ----------------------------------------------------------------- query
    def check(self, key: str, token: Optional[str]) -> None:
        """Raise unless ``token`` may command ``key`` now (no ticket taken)."""
        ticket = self.admit(key, token, label='check')
        self.release_ticket(ticket)

    def holder(self, key: str) -> Optional[Reservation]:
        with self._lock:
            return self._held.get(key)

    def is_active(self, token: str) -> bool:
        with self._lock:
            return token in self._active

    def in_flight(self, key: Optional[str] = None) -> List[Ticket]:
        with self._lock:
            if key is not None:
                return list(self._tickets.get(key, ()))
            return [t for tickets in self._tickets.values() for t in tickets]

    def wait_idle(self, keys: Iterable[str], timeout_s: float) -> bool:
        """Wait until no command is in flight on ``keys``."""
        keys = frozenset(keys)
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._lock:
            while any(self._tickets.get(k) for k in keys):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._lock.wait(remaining)
            return True

    def reservations(self) -> List[Reservation]:
        with self._lock:
            return list(self._active.values())

    def add_listener(self, callback) -> None:
        """``callback()`` after any reservation or ticket change (any thread)."""
        self._listeners.append(callback)

    def _notify(self) -> None:
        for callback in list(self._listeners):
            try:
                callback()
            except Exception:
                pass


_registry = ResourceRegistry()
_registry_lock = threading.Lock()


def get_resource_registry() -> ResourceRegistry:
    """The process-wide registry every manager admits its commands through."""
    return _registry


def set_resource_registry(registry: ResourceRegistry) -> ResourceRegistry:
    """Replace the process-wide registry (tests). Returns the previous one."""
    global _registry
    with _registry_lock:
        previous, _registry = _registry, registry
    return previous


def guard_methods(cls, kind_key, method_names) -> None:
    """Wrap ``cls``'s own definitions of ``method_names`` with admission.

    Used from ``__init_subclass__`` of manager base classes, so every concrete
    manager — built-in or plugin — admits its mutating commands at the manager
    boundary. The wrapper takes an optional ``owner=`` keyword (a reservation
    token) and holds an in-flight ticket for the duration of the call.
    """
    import functools

    for name in method_names:
        original = cls.__dict__.get(name)
        if original is None or getattr(original, '__resource_guarded__', False):
            continue
        if isinstance(original, (staticmethod, classmethod, property)):
            continue

        def make(fn, method_name):
            @functools.wraps(fn)
            def guarded(self, *args, owner: Optional[str] = None, **kwargs):
                key = kind_key(self)
                registry = get_resource_registry()
                ticket = registry.admit(key, owner, label=f'{type(self).__name__}.{method_name}')
                try:
                    return fn(self, *args, **kwargs)
                finally:
                    registry.release_ticket(ticket)
            guarded.__resource_guarded__ = True
            return guarded

        setattr(cls, name, make(original, name))
