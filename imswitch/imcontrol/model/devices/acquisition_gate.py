"""Mutual exclusion between acquisitions and device maintenance.

A reconnect must not overlap a scan or a recording, and neither may start
while a reconnect replaces a backend. A check before the reconnect is not
enough: an acquisition admitted a moment later runs alongside it. So both
sides admit through this gate, under one lock, for their whole duration:

- every scan run (or run-less scan iteration) and every recording holds an
  *acquisition ticket* from start until it has physically ended;
- a lifecycle transition holds the *maintenance* state for the whole
  adapter call. It is refused while any ticket is held, and while it is held
  every new acquisition is refused with the reason.

``docs/design/plans/transient-instruments-step-scans.md`` builds the general
form (per-resource reservations, ``ResourceRegistry``) on the calibration
branch; when the two branches meet, this gate becomes a reservation there.
"""
from __future__ import annotations

import itertools
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional


class AcquisitionBlockedError(RuntimeError):
    """An acquisition cannot start: a device is under maintenance."""


class MaintenanceBlockedError(RuntimeError):
    """Maintenance cannot start: acquisitions are running (or maintenance is)."""


@dataclass(frozen=True)
class AcquisitionTicket:
    id: int
    label: str


class AcquisitionGate:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tickets: Dict[int, AcquisitionTicket] = {}
        self._maintenance: Optional[str] = None
        self._ids = itertools.count(1)

    def admit(self, label: str) -> AcquisitionTicket:
        """Admit one acquisition; hold the ticket until it has fully ended."""
        with self._lock:
            if self._maintenance is not None:
                raise AcquisitionBlockedError(
                    f'{label} cannot start while {self._maintenance} is running')
            ticket = AcquisitionTicket(next(self._ids), label)
            self._tickets[ticket.id] = ticket
            return ticket

    def release(self, ticket: Optional[AcquisitionTicket]) -> None:
        if ticket is None:
            return
        with self._lock:
            self._tickets.pop(ticket.id, None)

    @contextmanager
    def maintenance(self, label: str) -> Iterator[None]:
        """Hold exclusive maintenance for the duration of the block."""
        with self._lock:
            if self._maintenance is not None:
                raise MaintenanceBlockedError(
                    f'{label} cannot start while {self._maintenance} is running')
            if self._tickets:
                running = ', '.join(sorted(t.label for t in self._tickets.values()))
                raise MaintenanceBlockedError(
                    f'{label} is blocked while acquisitions run: {running}')
            self._maintenance = label
        try:
            yield
        finally:
            with self._lock:
                self._maintenance = None

    def active(self) -> List[str]:
        with self._lock:
            return sorted(t.label for t in self._tickets.values())

    @property
    def maintenance_label(self) -> Optional[str]:
        with self._lock:
            return self._maintenance


_gate = AcquisitionGate()


def get_acquisition_gate() -> AcquisitionGate:
    """The process-wide gate (scans, recordings and lifecycle share it)."""
    return _gate


def set_acquisition_gate(gate: AcquisitionGate) -> AcquisitionGate:
    """Replace the process-wide gate (tests). Returns the previous one."""
    global _gate
    previous, _gate = _gate, gate
    return previous
