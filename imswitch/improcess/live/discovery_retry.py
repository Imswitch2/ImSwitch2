"""Bounded retry scheduling for not-yet-readable Session root entries.

Discovery retries do not represent new filesystem arrivals.  In particular,
this scheduler deliberately has no knowledge of processing or Auto settings.
"""
from __future__ import annotations

import time


class UnresolvedDiscoveryRetry:
    """Schedule slow, fair retries without reopening recognized recordings.

    ``due()`` advances only the selected paths, so a backlog of unresolved
    folders cannot starve later arrivals.  Times are monotonic to avoid wall-
    clock adjustments changing retry behavior.
    """

    def __init__(
        self,
        *,
        initial_delay_s: float = 3.0,
        max_delay_s: float = 30.0,
        max_per_tick: int = 2,
    ) -> None:
        if initial_delay_s <= 0 or max_delay_s < initial_delay_s or max_per_tick < 1:
            raise ValueError("Invalid discovery retry limits")
        self.initial_delay_s = float(initial_delay_s)
        self.max_delay_s = float(max_delay_s)
        self.max_per_tick = int(max_per_tick)
        # path -> (next_due_monotonic, most_recent_delay_s)
        self._entries: dict[str, tuple[float, float]] = {}

    def __bool__(self) -> bool:
        return bool(self._entries)

    def track(self, path: str, *, now: float | None = None) -> None:
        """Start tracking once; repeated watcher changes do not restart backoff."""
        path = str(path)
        if path not in self._entries:
            current = time.monotonic() if now is None else float(now)
            self._entries[path] = (
                current + self.initial_delay_s,
                self.initial_delay_s,
            )

    def discard(self, path: str) -> None:
        self._entries.pop(str(path), None)

    def clear(self) -> None:
        self._entries.clear()

    def due(self, *, now: float | None = None) -> tuple[str, ...]:
        """Return at most ``max_per_tick`` ready paths and back off each one."""
        current = time.monotonic() if now is None else float(now)
        ready = sorted(
            (deadline, path) for path, (deadline, _delay) in self._entries.items()
            if deadline <= current
        )[: self.max_per_tick]
        for _deadline, path in ready:
            old_delay = self._entries[path][1]
            new_delay = min(self.max_delay_s, old_delay * 2.0)
            self._entries[path] = (current + new_delay, new_delay)
        return tuple(path for _deadline, path in ready)
