"""The step list as a graph: order, ancestry, layers. Qt-free.

A workflow file is an ordered list in which every reference points to an
earlier step. A graph editor lets the user connect anything to anything, so
the list has to be put back in a valid order after an edit -- moving as little
as possible, and never changing the order of the Source steps among
themselves (the runner names a run's files after the first source it opens)
or of the Save steps among themselves (the files a failed run leaves behind
depend on which saves came first).
"""

from __future__ import annotations

import heapq

from imswitch.improcess.workflows.steps import Save, Source


class CycleError(ValueError):
    """The connections form a loop, so no step order can run them."""


def refs_of(step) -> list:
    if isinstance(step, Save):
        return [step.input] if step.input is not None else []
    return list(getattr(step, "inputs", []) or [])


def ancestors(steps, step_id: str) -> set[str]:
    """Every step ``step_id`` reads from, directly or through others."""
    by_id = {step.id: step for step in steps}
    found: set[str] = set()
    pending = [ref.step for ref in refs_of(by_id[step_id])] if step_id in by_id else []
    while pending:
        current = pending.pop()
        if current in found or current not in by_id:
            continue
        found.add(current)
        pending.extend(ref.step for ref in refs_of(by_id[current]))
    return found


def descendants(steps, step_id: str) -> set[str]:
    """Every step that reads from ``step_id``, directly or through others."""
    consumers: dict[str, set[str]] = {}
    for step in steps:
        for ref in refs_of(step):
            consumers.setdefault(ref.step, set()).add(step.id)
    found: set[str] = set()
    pending = list(consumers.get(step_id, ()))
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        pending.extend(consumers.get(current, ()))
    return found


def is_ordered(steps) -> bool:
    """Whether every reference points to an earlier step (dangling ones ignored)."""
    ids = {step.id for step in steps}
    seen: set[str] = set()
    for step in steps:
        if any(ref.step in ids and ref.step not in seen for ref in refs_of(step)):
            return False
        seen.add(step.id)
    return True


def stable_order(steps) -> list:
    """The steps in a valid order, as close to the given one as possible.

    A topological sort that always takes, among the steps that are ready,
    the one that came first; Source steps keep their order among themselves,
    and so do Save steps. A list that is already valid comes back unchanged.
    References to steps that do not exist are ignored (they are reported by
    validation, not fixed here). Raises :class:`CycleError` for a loop.
    """
    steps = list(steps)
    if is_ordered(steps):
        return steps
    index = {step.id: position for position, step in enumerate(steps)}
    depends: dict[str, set[str]] = {step.id: {ref.step for ref in refs_of(step) if ref.step in index}
                                     for step in steps}
    for kind in (Source, Save):
        chain = [step.id for step in steps if isinstance(step, kind)]
        for earlier, later in zip(chain, chain[1:]):
            depends[later].add(earlier)
    consumers: dict[str, set[str]] = {step.id: set() for step in steps}
    for step_id, needed in depends.items():
        for other in needed:
            consumers[other].add(step_id)
    waiting = {step_id: len(needed) for step_id, needed in depends.items()}
    ready = [(index[step_id], step_id) for step_id, count in waiting.items() if count == 0]
    heapq.heapify(ready)
    by_id = {step.id: step for step in steps}
    ordered = []
    while ready:
        _position, step_id = heapq.heappop(ready)
        ordered.append(by_id[step_id])
        for consumer in consumers[step_id]:
            waiting[consumer] -= 1
            if waiting[consumer] == 0:
                heapq.heappush(ready, (index[consumer], consumer))
    if len(ordered) != len(steps):
        stuck = sorted(step_id for step_id, count in waiting.items() if count > 0)
        raise CycleError(f"these steps read from each other in a loop: {', '.join(stuck)}")
    return ordered


def layers(steps) -> list[list[str]]:
    """Step ids in columns for a left-to-right drawing.

    A step's column is one more than the furthest column it reads from
    (longest path); inside a column, steps are ordered by the mean position
    of what they read from, so cables cross as little as cheaply possible.
    """
    column: dict[str, int] = {}
    ordered = stable_order(steps)
    for step in ordered:
        upstream = [column[ref.step] for ref in refs_of(step) if ref.step in column]
        column[step.id] = max(upstream) + 1 if upstream else 0
    by_column: dict[int, list[str]] = {}
    for step in ordered:
        by_column.setdefault(column[step.id], []).append(step.id)
    by_id = {step.id: step for step in ordered}
    position: dict[str, float] = {}
    result = []
    for number in sorted(by_column):
        ids = by_column[number]

        def centre(step_id):
            upstream = [position[ref.step] for ref in refs_of(by_id[step_id]) if ref.step in position]
            return sum(upstream) / len(upstream) if upstream else 0.0

        ids.sort(key=centre)
        for rank, step_id in enumerate(ids):
            position[step_id] = float(rank)
        result.append(ids)
    return result


__all__ = ["CycleError", "ancestors", "descendants", "is_ordered", "layers", "refs_of", "stable_order"]
