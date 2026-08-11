"""Model changes as commands.

Every mutation of the ROI list goes through a command object that knows how to
undo itself.  The undo *stack* and its shortcuts come later; what this buys
immediately is that there is exactly one audited path for changing the model,
so an operation cannot quietly bypass the invariants the model maintains
(unique names, assigned identities, preserved fields).

Commands hold records, never pixels: an undo history must not pin image data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from imswitch.imcommon.algorithms.roi import ROIRecord


class Command(Protocol):
    """Something done to the model that can be undone."""

    label: str

    def do(self, model) -> object:
        ...

    def undo(self, model) -> None:
        ...


@dataclass
class AddROI:
    """Add one ROI, remembering the name the model actually gave it."""

    roi: ROIRecord
    label: str = "Add ROI"
    _added_name: str | None = None

    def do(self, model):
        added = model.add(self.roi)
        self._added_name = added.name
        return added

    def undo(self, model) -> None:
        if self._added_name is not None:
            model.remove(self._added_name)
            self._added_name = None


@dataclass
class DeleteROI:
    """Delete one ROI, keeping the record and its position for undo."""

    name: str
    label: str = "Delete ROI"
    _removed: ROIRecord | None = None
    _index: int | None = None

    def do(self, model):
        rois = model.rois
        for index, roi in enumerate(rois):
            if roi.name == self.name:
                self._removed, self._index = roi, index
                break
        model.remove(self.name)
        return self._removed

    def undo(self, model) -> None:
        if self._removed is None:
            return
        # Restore in place: ROI order is user-visible and drives the overlay's
        # colour cycle, so putting it back at the end would be a visible change.
        rois = model.rois
        index = self._index if self._index is not None else len(rois)
        rois.insert(min(index, len(rois)), self._removed)
        model.set_rois(rois)
        self._removed = None


@dataclass
class RenameROI:
    name: str
    new_name: str
    label: str = "Rename ROI"
    _previous: str | None = None
    _applied: str | None = None

    def do(self, model):
        self._previous = self.name
        updated = model.rename(self.name, self.new_name)
        self._applied = updated.name
        return updated

    def undo(self, model) -> None:
        if self._applied and self._previous:
            model.rename(self._applied, self._previous)
            self._applied = None


@dataclass
class UpdateROI:
    """Replace an ROI's geometry, keeping its identity."""

    name: str
    changes: dict
    label: str = "Update ROI"
    _before: ROIRecord | None = None

    def do(self, model):
        self._before = model.get(self.name)
        return model.update(self.name, **self.changes)

    def undo(self, model) -> None:
        if self._before is None:
            return
        model.replace_record(self._before)
        self._before = None


@dataclass
class SetVisible:
    """Show or hide one ROI."""

    name: str
    visible: bool
    label: str = "Set visibility"
    _before: bool | None = None

    def do(self, model):
        roi = model.get(self.name)
        self._before = None if roi is None else roi.visible
        return model.set_visible(self.name, self.visible)

    def undo(self, model) -> None:
        if self._before is not None:
            model.set_visible(self.name, self._before)
            self._before = None


@dataclass
class ClearROIs:
    """Remove every ROI, keeping them all for undo.

    Clearing a set of hand-drawn regions is the most expensive thing to redo by
    hand, so it is the operation that most needs to be undoable.
    """

    label: str = "Clear ROIs"
    _removed: tuple = ()

    def do(self, model):
        self._removed = tuple(model.rois)
        model.clear()
        return None

    def undo(self, model) -> None:
        if self._removed:
            model.set_rois(list(self._removed))
            self._removed = ()


@dataclass
class RemoveSliceInfo:
    """Detach every ROI from the slice it was captured on (ImageJ parity)."""

    label: str = "Remove slice info"
    _before: tuple = ()

    def do(self, model):
        self._before = tuple(
            (roi.name, roi.position) for roi in model.rois if roi.position
        )
        for name, _position in self._before:
            model.update(name, position=())
        return None

    def undo(self, model) -> None:
        for name, position in self._before:
            model.update(name, position=position)
        self._before = ()


@dataclass
class ReplaceROIs:
    """Swap a set of ROIs for the ROIs an operation produced.

    Every P-5 operation reduces to this: the inputs it consumed (which may be
    none, for an operation that only adds) and the outputs it produced. One
    command rather than one per operation, because what has to be undone is
    the same in each case, and an operation-specific inverse would be a second
    place for the two to disagree.
    """

    consumed: tuple = ()
    produced: tuple = ()
    label: str = "ROI operation"
    _before: tuple = ()

    def do(self, model):
        self._before = tuple(model.rois)
        for name in self.consumed:
            model.remove(name)
        added = [model.add(roi) for roi in self.produced]
        return added

    def undo(self, model) -> None:
        # Restored wholesale rather than by inverse steps: order is
        # user-visible, and re-adding a consumed ROI would append it to the
        # end rather than put it back where it was.
        model.set_rois(list(self._before))
        self._before = ()


class CommandLog:
    """Runs commands and keeps what is needed to undo them.

    Bounded, because an unbounded history of a long session is a slow memory
    leak. The UI for this arrives with the undo phase; the log itself lands
    now so every mutation has been going through it from the start.
    """

    def __init__(self, model, *, limit: int = 100):
        self._model = model
        self._limit = int(limit)
        self._done: list[Command] = []
        self._undone: list[Command] = []

    def run(self, command: Command):
        result = command.do(self._model)
        self._done.append(command)
        if len(self._done) > self._limit:
            del self._done[0]
        # A new action invalidates anything that was undone past this point.
        self._undone.clear()
        return result

    @property
    def can_undo(self) -> bool:
        return bool(self._done)

    @property
    def can_redo(self) -> bool:
        return bool(self._undone)

    def undo(self) -> None:
        if not self._done:
            return
        command = self._done.pop()
        command.undo(self._model)
        self._undone.append(command)

    def redo(self) -> None:
        if not self._undone:
            return
        command = self._undone.pop()
        command.do(self._model)
        self._done.append(command)

    def labels(self) -> list[str]:
        return [command.label for command in self._done]


__all__ = [
    "AddROI",
    "ClearROIs",
    "RemoveSliceInfo",
    "SetVisible",
    "Command",
    "CommandLog",
    "DeleteROI",
    "RenameROI",
    "ReplaceROIs",
    "UpdateROI",
]
