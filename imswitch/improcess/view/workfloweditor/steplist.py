"""The steps of the workflow, in execution order, with their issues."""

from __future__ import annotations

from pathlib import Path

from qtpy import QtCore, QtWidgets

from imswitch.improcess.workflows.steps import Consolidate, Process, Reconstruct, Save, Source

_ID_ROLE = QtCore.Qt.UserRole


class StepList(QtWidgets.QTreeWidget):
    """One row per step: id, kind, plugin, inputs, and an issue marker."""

    sigStepSelected = QtCore.Signal(str)  # "" when nothing is selected

    COLUMNS = ("Step", "Kind", "Plugin / target", "Inputs", "Issues")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(len(self.COLUMNS))
        self.setHeaderLabels(list(self.COLUMNS))
        self.setRootIsDecorated(False)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.setAllColumnsShowFocus(True)
        self.currentItemChanged.connect(self._on_current_changed)

    def refresh(self, document, issues) -> None:
        """Rebuild the rows; the selected step stays selected when it survives."""
        selected = self.current_step_id()
        by_step: dict[str, list[str]] = {}
        for issue in issues:
            by_step.setdefault(issue.step, []).append(issue.message)
        self.blockSignals(True)
        try:
            self.clear()
            for step in document.steps:
                messages = by_step.get(step.id, [])
                item = QtWidgets.QTreeWidgetItem([
                    step.id, step.kind, _target(step), _inputs(step),
                    f"⚠ {len(messages)}" if messages else "",
                ])
                item.setData(0, _ID_ROLE, step.id)
                if messages:
                    item.setToolTip(4, "\n".join(messages))
                    item.setToolTip(0, "\n".join(messages))
                self.addTopLevelItem(item)
            for column in range(len(self.COLUMNS)):
                self.resizeColumnToContents(column)
            if selected and document.has(selected):
                self.select_step(selected)   # silently: the same step stays selected
        finally:
            self.blockSignals(False)
        if selected and not document.has(selected):
            self.sigStepSelected.emit("")

    def current_step_id(self) -> str | None:
        item = self.currentItem()
        return str(item.data(0, _ID_ROLE)) if item is not None else None

    def select_step(self, step_id: str) -> None:
        for index in range(self.topLevelItemCount()):
            item = self.topLevelItem(index)
            if item.data(0, _ID_ROLE) == step_id:
                self.setCurrentItem(item)
                return

    def _on_current_changed(self, current, _previous) -> None:
        self.sigStepSelected.emit(str(current.data(0, _ID_ROLE)) if current is not None else "")


def _target(step) -> str:
    if isinstance(step, Source):
        spec = step.source
        if not spec.path:
            return "(bound at run)"
        name = Path(spec.path).name
        return f"{name}::{spec.dataset}" if spec.dataset else name
    if isinstance(step, (Reconstruct, Consolidate)):
        return step.reconstructor
    if isinstance(step, Process):
        return step.processor
    if isinstance(step, Save):
        return step.fmt
    return ""


def _inputs(step) -> str:
    if isinstance(step, Save):
        return str(step.input)
    return ", ".join(str(ref) for ref in getattr(step, "inputs", []) or [])


__all__ = ["StepList"]


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
