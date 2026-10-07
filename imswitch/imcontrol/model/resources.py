"""Resource reservations: who may command which physical resource.

P-1 scope (``docs/design/plans/transient-instruments-step-scans.md`` §12): exclusive,
owner-tokened reservations of resource keys, with expiring tokens. Command
admission with in-flight tickets (§7.2) and enforcement in manager methods
(§7.3) come in P-3 and build on this registry.
"""
from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, Optional


class ResourceReservedError(RuntimeError):
    """The resource is reserved by another owner."""


class ReservationExpiredError(RuntimeError):
    """The token's reservation has ended; it never becomes valid again."""


@dataclass(frozen=True)
class Reservation:
    token: str
    owner: str
    resources: FrozenSet[str]


class ResourceRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: Dict[str, Reservation] = {}
        self._active: Dict[str, Reservation] = {}
        self._counter = itertools.count(1)

    def reserve(self, resources: Iterable[str], owner: str) -> Reservation:
        """Reserve every resource or none of them."""
        wanted = frozenset(resources)
        with self._lock:
            taken = {r: self._held[r] for r in wanted if r in self._held}
            if taken:
                holders = ', '.join(sorted(f'{r} (held by {h.owner})' for r, h in taken.items()))
                raise ResourceReservedError(f'cannot reserve: {holders}')
            reservation = Reservation(f'rsv-{next(self._counter)}', owner, wanted)
            for resource in wanted:
                self._held[resource] = reservation
            self._active[reservation.token] = reservation
            return reservation

    def release(self, token: str, resources: Optional[Iterable[str]] = None) -> None:
        """Release all (or some) resources of a reservation.

        The token expires once its last resource is released.
        """
        with self._lock:
            reservation = self._active.get(token)
            if reservation is None:
                return
            subset = reservation.resources if resources is None else frozenset(resources)
            for resource in subset:
                if self._held.get(resource) is reservation:
                    del self._held[resource]
            if not any(h is reservation for h in self._held.values()):
                del self._active[token]

    def check(self, resource: str, token: Optional[str]) -> None:
        """Raise unless ``token`` may command ``resource`` now."""
        with self._lock:
            if token is not None and token not in self._active:
                raise ReservationExpiredError(f'reservation {token} has ended')
            holder = self._held.get(resource)
            if holder is not None and holder.token != token:
                raise ResourceReservedError(f'{resource} is reserved by {holder.owner}')

    def holder(self, resource: str) -> Optional[Reservation]:
        with self._lock:
            return self._held.get(resource)

    def is_active(self, token: str) -> bool:
        with self._lock:
            return token in self._active
