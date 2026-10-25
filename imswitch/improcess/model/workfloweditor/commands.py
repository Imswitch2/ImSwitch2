"""Undo and redo for a workflow document. Qt-free.

Every edit is recorded as the document before and after it. A workflow is a
small structure (tens of steps, a few parameters each), so a snapshot is
cheap, and it makes undo exact for every operation without each one having to
know how to reverse itself -- including the ones that touch several steps at
once (a delete that also removes the Saves reading from the step, a paste, a
connection that reorders the list).
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class _Entry:
    label: str
    before: dict
    after: dict


class DocumentHistory:
    """The undo and redo stacks of one :class:`WorkflowDocument`.

    Use :meth:`edit` around an operation::

        with document.history.edit("Connect"):
            document.connect("split.C0", "smooth")

    An operation that raises inside the block records nothing and leaves the
    document as it was before the block. A block inside another one is part
    of it: a gesture made of several operations (add a step, then connect it)
    is undone in one go.
    """

    def __init__(self, document, limit: int = 200):
        self._document = document
        self._limit = int(limit)
        self._undo: list[_Entry] = []
        self._redo: list[_Entry] = []
        self._depth = 0

    @contextmanager
    def edit(self, label: str):
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        before, was_dirty = self._snapshot(), self._document.dirty
        self._depth = 1
        try:
            yield
        except BaseException:
            # The revision still moves: a view may have drawn the half-made edit.
            self._restore(before)
            self._document.dirty = was_dirty
            raise
        finally:
            self._depth = 0
        after = self._snapshot()
        if after != before:
            self._undo.append(_Entry(str(label), before, after))
            del self._undo[: max(0, len(self._undo) - self._limit)]
            self._redo.clear()

    def undo(self) -> str | None:
        """Go back one edit; returns its label, or ``None`` when there is none."""
        if not self._undo:
            return None
        entry = self._undo.pop()
        self._restore(entry.before)
        self._redo.append(entry)
        return entry.label

    def redo(self) -> str | None:
        """Re-apply the last undone edit; returns its label, or ``None``."""
        if not self._redo:
            return None
        entry = self._redo.pop()
        self._restore(entry.after)
        self._undo.append(entry)
        return entry.label

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def undo_label(self) -> str | None:
        return self._undo[-1].label if self._undo else None

    @property
    def redo_label(self) -> str | None:
        return self._redo[-1].label if self._redo else None

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()

    def _snapshot(self) -> dict:
        return self._document.snapshot()

    def _restore(self, snapshot: dict) -> None:
        from imswitch.improcess.workflows.steps import Workflow

        self._document.workflow = Workflow.from_dict(copy.deepcopy(snapshot))
        self._document._touch()


__all__ = ["DocumentHistory"]
