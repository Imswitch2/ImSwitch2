"""The right-hand panel: the selected step, editable, one form per step kind.

Every widget edit becomes one call on the document; the document answers
with an exception when the edit cannot be made, and the form then shows the
message and puts the widget back. Structural edits (a renamed step, another
plugin, other inputs) are reported so the window can rebuild the step list;
a parameter edit only touches the document.
"""

from __future__ import annotations

from pathlib import Path

from qtpy import QtCore, QtWidgets

from imswitch.improcess.model.workfloweditor.catalog import PROCESSOR
from imswitch.improcess.model.workfloweditor.document import DocumentError, WorkflowDocument
from imswitch.improcess.workflows.sources import SOURCE_KINDS
from imswitch.improcess.workflows.steps import Consolidate, Process, Reconstruct, Save, Source

from .paramform import ParamForm


class _InputsEditor(QtWidgets.QWidget):
    """An ordered list of references, each an editable combo over the ports
    the earlier steps offer (a pattern port is typed: ``split.C1``)."""

    sigInputsChanged = QtCore.Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._options: list[str] = []
        self._rows: list[QtWidgets.QComboBox] = []
        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._rows_box = QtWidgets.QVBoxLayout()
        self._layout.addLayout(self._rows_box)
        self._add = QtWidgets.QPushButton("+ Add input")
        self._add.clicked.connect(self._on_add)
        self._layout.addWidget(self._add, 0, QtCore.Qt.AlignLeft)

    def set_inputs(self, options: list[str], refs: list[str]) -> None:
        self._options = list(options)
        while self._rows_box.count():
            item = self._rows_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._rows = []
        for ref in refs:
            self._add_row(ref)

    def refs(self) -> list[str]:
        return [combo.currentText().strip() for combo in self._rows if combo.currentText().strip()]

    def _add_row(self, ref: str) -> None:
        row = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        combo = QtWidgets.QComboBox()
        combo.setEditable(True)
        combo.addItems(self._options)
        combo.setCurrentText(ref)
        combo.activated.connect(lambda _index: self._emit())
        combo.lineEdit().editingFinished.connect(self._emit)
        remove = QtWidgets.QToolButton()
        remove.setText("−")
        remove.setToolTip("Remove this input")
        remove.clicked.connect(lambda _checked=False, r=row, c=combo: self._remove_row(r, c))
        layout.addWidget(combo, 1)
        layout.addWidget(remove)
        self._rows_box.addWidget(row)
        self._rows.append(combo)

    def _remove_row(self, row, combo) -> None:
        self._rows.remove(combo)
        row.deleteLater()
        self._emit()

    def _on_add(self) -> None:
        self._add_row(self._options[-1] if self._options else "")
        self._emit()

    def _emit(self) -> None:
        self.sigInputsChanged.emit(self.refs())


class StepForm(QtWidgets.QScrollArea):
    """Shows and edits one step of a :class:`WorkflowDocument`."""

    sigChanged = QtCore.Signal(str)           # the step's id after a parameter edit
    sigStructureChanged = QtCore.Signal(str)  # the step's (new) id after rename/plugin/inputs
    sigMessage = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self._document: WorkflowDocument | None = None
        self._step_id: str | None = None
        self._body = QtWidgets.QWidget()
        self.setWidget(self._body)
        self._param_form: ParamForm | None = None
        self.show_step(None)

    def set_document(self, document: WorkflowDocument) -> None:
        self._document = document
        self.show_step(None)

    @property
    def step_id(self) -> str | None:
        return self._step_id

    @property
    def param_form(self) -> ParamForm | None:
        return self._param_form

    # -- building -----------------------------------------------------------------

    def show_step(self, step_id: str | None) -> None:
        self._step_id = step_id if step_id and self._document is not None and self._document.has(step_id) else None
        body = QtWidgets.QWidget()
        layout = QtWidgets.QFormLayout(body)
        layout.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        self._param_form = None
        if self._step_id is None:
            hint = QtWidgets.QLabel("Select a step, or add one from the palette.")
            hint.setEnabled(False)
            layout.addRow(hint)
        else:
            step = self._document.step(self._step_id)
            self._build_header(layout, step)
            if isinstance(step, Source):
                self._build_source(layout, step)
            elif isinstance(step, Reconstruct):
                self._build_reconstruct(layout, step)
            elif isinstance(step, Consolidate):
                self._build_consolidate(layout, step)
            elif isinstance(step, Process):
                self._build_process(layout, step)
            elif isinstance(step, Save):
                self._build_save(layout, step)
        # The old body may hold the very widget whose signal brought us here;
        # deleting it now would pull the rug from under that emission. Take
        # it out and let the event loop delete it.
        old = self.takeWidget()
        if old is not None:
            old.hide()
            old.setParent(None)
            old.deleteLater()
        self.setWidget(body)
        self._body = body

    def _build_header(self, layout, step) -> None:
        kind = QtWidgets.QLabel(f"<b>{step.kind.capitalize()}</b>")
        layout.addRow(kind)
        id_edit = QtWidgets.QLineEdit(step.id)
        id_edit.setToolTip("Letters, digits, '_' and '-'; later steps refer to it")
        id_edit.editingFinished.connect(lambda e=id_edit: self._rename(e))
        layout.addRow("Id", id_edit)

    def _build_source(self, layout, step: Source) -> None:
        spec = step.source
        path_row = QtWidgets.QWidget()
        path_layout = QtWidgets.QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_edit = QtWidgets.QLineEdit(spec.path or "")
        path_edit.setPlaceholderText("No path: bound when the workflow runs")
        path_edit.editingFinished.connect(lambda e=path_edit: self._edit(
            lambda: self._document.set_source(self._step_id, path=e.text().strip() or None)))
        browse = QtWidgets.QPushButton("Browse…")
        browse.clicked.connect(lambda _checked=False, e=path_edit: self._browse_source(e))
        path_layout.addWidget(path_edit, 1)
        path_layout.addWidget(browse)
        layout.addRow("Path", path_row)
        dataset = QtWidgets.QLineEdit(spec.dataset or "")
        dataset.setPlaceholderText("Only when the container holds several datasets")
        dataset.editingFinished.connect(lambda e=dataset: self._edit(
            lambda: self._document.set_source(self._step_id, dataset=e.text().strip() or None)))
        layout.addRow("Dataset", dataset)
        kind = QtWidgets.QComboBox()
        kind.addItems(list(SOURCE_KINDS))
        kind.setCurrentText(spec.source_kind)
        kind.activated.connect(lambda _index, c=kind: self._edit(
            lambda: self._document.set_source(self._step_id, source_kind=c.currentText())))
        layout.addRow("Source kind", kind)
        if spec.fingerprint:
            note = QtWidgets.QLabel("Carries a recorded fingerprint; replay verifies the file against it.")
            note.setWordWrap(True)
            note.setEnabled(False)
            layout.addRow(note)

    def _build_reconstruct(self, layout, step: Reconstruct) -> None:
        catalog = self._document.catalog
        combo = self._plugin_combo(catalog.reconstructors if catalog else (), step.reconstructor)
        combo.activated.connect(lambda _index, c=combo: self._set_plugin(c))
        layout.addRow("Reconstructor", combo)
        sources = [s.id for s in self._document.producers_before(self._step_id) if isinstance(s, Source)]
        source_combo = QtWidgets.QComboBox()
        source_combo.setEditable(True)
        source_combo.addItems(sources)
        source_combo.setCurrentText(str(step.inputs[0]) if step.inputs else "")
        source_combo.activated.connect(lambda _index, c=source_combo: self._set_inputs([c.currentText()]))
        source_combo.lineEdit().editingFinished.connect(lambda c=source_combo: self._set_inputs([c.currentText()]))
        layout.addRow("Source", source_combo)
        self._add_param_form(layout, step)

    def _build_consolidate(self, layout, step: Consolidate) -> None:
        catalog = self._document.catalog
        combo = self._plugin_combo(catalog.consolidators() if catalog else (), step.reconstructor)
        combo.activated.connect(lambda _index, c=combo: self._set_plugin(c))
        layout.addRow("Reconstructor", combo)
        chooser = QtWidgets.QListWidget()
        chosen = [str(ref) for ref in step.inputs]
        for earlier in self._document.producers_before(self._step_id):
            if isinstance(earlier, Reconstruct):
                item = QtWidgets.QListWidgetItem(f"{earlier.id} ({earlier.reconstructor})")
                item.setData(QtCore.Qt.UserRole, earlier.id)
                item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
                item.setCheckState(QtCore.Qt.Checked if earlier.id in chosen else QtCore.Qt.Unchecked)
                chooser.addItem(item)
        chooser.itemChanged.connect(lambda _item, c=chooser: self._set_inputs([
            c.item(i).data(QtCore.Qt.UserRole) for i in range(c.count())
            if c.item(i).checkState() == QtCore.Qt.Checked
        ]))
        layout.addRow("Reconstructions", chooser)
        note = QtWidgets.QLabel("A consolidation takes no parameters; it merges what the reconstructions are.")
        note.setWordWrap(True)
        note.setEnabled(False)
        layout.addRow(note)

    def _build_process(self, layout, step: Process) -> None:
        catalog = self._document.catalog
        combo = self._plugin_combo(catalog.processors if catalog else (), step.processor, by_category=True)
        combo.activated.connect(lambda _index, c=combo: self._set_plugin(c))
        layout.addRow("Processor", combo)
        entry = self._document.entry_for(step)
        if entry is not None:
            info = QtWidgets.QLabel(
                f"{entry.description or entry.name} — inputs: {entry.arity}; "
                f"accepts: {', '.join(entry.accepted_kinds) or 'any'}"
            )
            info.setWordWrap(True)
            info.setEnabled(False)
            layout.addRow(info)
        inputs = _InputsEditor()
        inputs.set_inputs([o.ref for o in self._document.port_options(self._step_id)],
                          [str(ref) for ref in step.inputs])
        inputs.sigInputsChanged.connect(self._set_inputs)
        layout.addRow("Inputs", inputs)
        self._add_param_form(layout, step)
        restriction = step.restriction or {}
        if restriction:
            row = QtWidgets.QWidget()
            row_layout = QtWidgets.QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            rois = restriction.get("rois") or []
            summary = QtWidgets.QLabel(
                f"{restriction.get('mode', '?')} to {len(rois)} region(s)"
                + (" (by reference: cannot run)" if restriction.get("by_reference") else "")
            )
            clear = QtWidgets.QPushButton("Clear")
            clear.clicked.connect(lambda _checked=False: self._clear_restriction())
            row_layout.addWidget(summary, 1)
            row_layout.addWidget(clear)
            layout.addRow("ROI restriction", row)

    def _build_save(self, layout, step: Save) -> None:
        catalog = self._document.catalog
        input_combo = QtWidgets.QComboBox()
        input_combo.setEditable(True)
        input_combo.addItems([o.ref for o in self._document.port_options(self._step_id)])
        input_combo.setCurrentText(str(step.input))
        input_combo.activated.connect(lambda _index, c=input_combo: self._set_inputs([c.currentText()]))
        input_combo.lineEdit().editingFinished.connect(lambda c=input_combo: self._set_inputs([c.currentText()]))
        layout.addRow("Input", input_combo)
        fmt = QtWidgets.QComboBox()
        fmt.setEditable(True)
        fmt.addItems(list(catalog.save_formats) if catalog else [step.fmt])
        fmt.setCurrentText(step.fmt)
        fmt.activated.connect(lambda _index, c=fmt: self._edit(
            lambda: self._document.set_save(self._step_id, fmt=c.currentText()), structural=True))
        fmt.lineEdit().editingFinished.connect(lambda c=fmt: self._edit(
            lambda: self._document.set_save(self._step_id, fmt=c.currentText()), structural=True))
        layout.addRow("Format", fmt)
        template = QtWidgets.QLineEdit(step.path_template)
        template.editingFinished.connect(lambda e=template: self._edit(
            lambda: self._document.set_save(self._step_id, path_template=e.text()), structural=True))
        layout.addRow("Path template", template)
        placeholders = ", ".join("{%s}" % name for name in (catalog.path_placeholders if catalog else ()))
        hint = QtWidgets.QLabel(f"Placeholders: {placeholders}. Files are written under the output folder only.")
        hint.setWordWrap(True)
        hint.setEnabled(False)
        layout.addRow(hint)

    def _add_param_form(self, layout, step) -> None:
        entry = self._document.entry_for(step)
        form = ParamForm()
        form.set_plugin(entry, step.params)
        form.sigValueChanged.connect(self._set_param)
        form.sigValueCleared.connect(self._clear_param)
        self._param_form = form
        layout.addRow(form)

    def _plugin_combo(self, entries, current: str, *, by_category: bool = False) -> QtWidgets.QComboBox:
        combo = QtWidgets.QComboBox()
        found = False
        for entry in entries:
            label = f"{entry.category} · {entry.name}" if by_category and entry.kind == PROCESSOR else entry.name
            combo.addItem(f"{label} ({entry.id})", entry.id)
            index = combo.count() - 1
            if entry.gui_only:
                item = combo.model().item(index)
                item.setEnabled(False)
                item.setToolTip(entry.gui_only)
            if entry.id == current:
                combo.setCurrentIndex(index)
                found = True
        if not found:
            combo.addItem(f"{current} (not installed)", current)
            combo.setCurrentIndex(combo.count() - 1)
        return combo

    # -- edits --------------------------------------------------------------------

    def _edit(self, action, *, structural: bool = False) -> bool:
        """Run one document edit; on refusal show why and rebuild the form."""
        if self._document is None or self._step_id is None:
            return False
        try:
            action()
        except (DocumentError, KeyError, ValueError) as exc:
            self.sigMessage.emit(str(exc))
            self.show_step(self._step_id)
            return False
        if structural:
            self.sigStructureChanged.emit(self._step_id)
        else:
            self.sigChanged.emit(self._step_id)
        return True

    def _rename(self, edit: QtWidgets.QLineEdit) -> None:
        new_id = edit.text().strip()
        if not new_id or new_id == self._step_id:
            edit.setText(self._step_id or "")
            return
        old = self._step_id
        if self._edit(lambda: self._document.rename_step(old, new_id), structural=False):
            self._step_id = new_id
            self.sigStructureChanged.emit(new_id)

    def _set_plugin(self, combo: QtWidgets.QComboBox) -> None:
        plugin_id = combo.currentData()
        if self._edit(lambda: self._document.set_plugin(self._step_id, plugin_id), structural=True):
            self.show_step(self._step_id)

    def _clear_restriction(self) -> None:
        if self._edit(lambda: self._document.set_restriction(self._step_id, None), structural=True):
            self.show_step(self._step_id)

    def _set_inputs(self, refs) -> None:
        refs = [str(ref).strip() for ref in refs if str(ref).strip()]
        self._edit(lambda: self._document.set_inputs(self._step_id, refs), structural=True)

    def _set_param(self, key: str, value) -> None:
        self._edit(lambda: self._document.set_param(self._step_id, key, value))

    def _clear_param(self, key: str) -> None:
        self._edit(lambda: self._document.clear_param(self._step_id, key))

    def _browse_source(self, edit: QtWidgets.QLineEdit) -> None:
        try:
            from imswitch.improcess.model.dataset_sources import SOURCE_SPECS, file_dialog_filter

            name_filter = file_dialog_filter(SOURCE_SPECS)
        except Exception:
            name_filter = "All files (*)"
        start = str(Path(edit.text()).parent) if edit.text() else ""
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(self, "Recording", start, name_filter)
        if path:
            edit.setText(path)
            self._edit(lambda: self._document.set_source(self._step_id, path=path), structural=True)


__all__ = ["StepForm"]


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
