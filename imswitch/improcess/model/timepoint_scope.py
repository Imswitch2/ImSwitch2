"""Immutable reconstruction scope for logical time-lapse datasets.

Scope is input-selection metadata, not a reconstructor parameter.  Queue jobs
snapshot it when submitted so ``Current T`` can never drift while a job waits.
Internal indices are zero-based; presentation helpers render one-based labels.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TimepointScopeMode(str, Enum):
    ALL = "all"
    SINGLE = "single"
    RANGE = "range"


@dataclass(frozen=True)
class TimepointScope:
    mode: TimepointScopeMode = TimepointScopeMode.ALL
    first: int | None = None
    last: int | None = None

    def __post_init__(self) -> None:
        mode = TimepointScopeMode(self.mode)
        object.__setattr__(self, "mode", mode)
        if mode == TimepointScopeMode.ALL:
            object.__setattr__(self, "first", None)
            object.__setattr__(self, "last", None)
            return
        if self.first is None:
            raise ValueError("a bounded timepoint scope requires a first index")
        first = int(self.first)
        last = first if mode == TimepointScopeMode.SINGLE else self.last
        if last is None:
            raise ValueError("a range timepoint scope requires a last index")
        last = int(last)
        if first < 0 or last < first:
            raise ValueError("timepoint scope indices must satisfy 0 <= first <= last")
        object.__setattr__(self, "first", first)
        object.__setattr__(self, "last", first if mode == TimepointScopeMode.SINGLE else last)

    @classmethod
    def all(cls) -> "TimepointScope":
        return cls(TimepointScopeMode.ALL)

    @classmethod
    def single(cls, index: int) -> "TimepointScope":
        return cls(TimepointScopeMode.SINGLE, int(index), int(index))

    @classmethod
    def range(cls, first: int, last: int) -> "TimepointScope":
        return cls(TimepointScopeMode.RANGE, int(first), int(last))

    @property
    def bounded(self) -> bool:
        return self.mode != TimepointScopeMode.ALL

    def bounds(self, total: int | None = None) -> tuple[int, int | None]:
        """Return ``(start, stop)`` with an exclusive stop.

        ``total`` validates/clamps the logical source extent.  ``All`` without a
        known total deliberately returns ``(0, None)`` so live readers retain
        their existing grow-until-complete behaviour.
        """
        total_i = int(total) if total not in (None, 0) else None
        if total_i is not None and total_i < 1:
            total_i = None
        if self.mode == TimepointScopeMode.ALL:
            return 0, total_i
        start = int(self.first)
        stop = int(self.last) + 1
        if total_i is not None:
            if start >= total_i:
                raise IndexError(
                    f"timepoint {start + 1} is outside this {total_i}-point lapse"
                )
            stop = min(stop, total_i)
        return start, stop

    def count(self, total: int | None = None) -> int | None:
        start, stop = self.bounds(total)
        return None if stop is None else max(0, int(stop) - int(start))

    def is_full(self, total: int | None = None) -> bool:
        if self.mode == TimepointScopeMode.ALL:
            return True
        if total in (None, 0):
            return False
        start, stop = self.bounds(int(total))
        return start == 0 and stop == int(total)

    def label(self, total: int | None = None) -> str:
        if self.mode == TimepointScopeMode.ALL:
            count = self.count(total)
            return f"All T ({count})" if count and count > 1 else "All T"
        if self.mode == TimepointScopeMode.SINGLE:
            return f"T{int(self.first) + 1}"
        return f"T{int(self.first) + 1}\N{EN DASH}T{int(self.last) + 1}"

    def to_dict(self, total: int | None = None) -> dict:
        payload = {"mode": self.mode.value, "index_base": 0}
        if self.mode != TimepointScopeMode.ALL:
            payload["first"] = int(self.first)
            payload["last"] = int(self.last)
        if total not in (None, 0):
            total_i = max(1, int(total))
            payload["total"] = total_i
            count = self.count(total_i)
            if count is not None:
                payload["count"] = int(count)
        return payload


def coerce_timepoint_scope(value) -> TimepointScope:
    if value is None:
        return TimepointScope.all()
    if isinstance(value, TimepointScope):
        return value
    if isinstance(value, str):
        return TimepointScope(TimepointScopeMode(value))
    if isinstance(value, dict):
        mode = TimepointScopeMode(value.get("mode", TimepointScopeMode.ALL.value))
        if mode == TimepointScopeMode.ALL:
            return TimepointScope.all()
        if mode == TimepointScopeMode.SINGLE:
            return TimepointScope.single(int(value.get("first", value.get("index", 0))))
        return TimepointScope.range(int(value["first"]), int(value["last"]))
    raise TypeError(f"cannot convert {type(value).__name__} to TimepointScope")


__all__ = [
    "TimepointScope",
    "TimepointScopeMode",
    "coerce_timepoint_scope",
]
