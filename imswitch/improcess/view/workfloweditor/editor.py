"""The workflow editor window.

Palette on the left, the step list over the issue panel in the middle, the
selected step's form on the right; the same shape as the config editor, over
the Qt-free :class:`WorkflowDocument`. The window owns no run: it hands the
workflow to whoever connected ``sig_run_requested`` (the
``WorkflowController``, which runs it exactly as *File → Run workflow…*
does) and shows the progress it is told about.
"""

from __future__ import annotations

from pathlib import Path

from qtpy import QtCore, QtWidgets

from imswitch.improcess.model.workfloweditor import DocumentError, WorkflowDocument, build_catalog
from imswitch.improcess.model.workfloweditor.folders import workflows_directory
from imswitch.improcess.workflows.steps import WorkflowError

from .palette import PluginPalette
from .stepforms import StepForm
from .steplist import StepList
from .validation import IssuePanel

_FILTER = "Workflow (*.yaml *.yml *.json)"
_SUFFIXES = (".yaml", ".yml", ".json")


class MainWindow(QtWidgets.QMainWindow):
    """See the module docstring."""

    sig_closed = QtCore.Signal()
    #: The workflow to run, and the folder relative source paths resolve against (or None).
    sig_run_requested = QtCore.Signal(object, object)
    sig_run_on_results_requested = QtCore.Signal(object)
    sig_cancel_requested = QtCore.Signal()

    def __init__(self, catalog=None, *, start_folder=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ImProcess workflow editor")
        self.resize(1280, 760)
        self.catalog = catalog if catalog is not None else build_catalog()
        self._folder = Path(start_folder) if start_folder else Path(workflows_directory())
        self.document = WorkflowDocument(catalog=self.catalog)
        self._running = False
        self._build_ui()
        self.load_document(self.document, confirm=False)

    # -- ui -------------------------------------------------------------------------

    def _build_ui(self) -> None:
        toolbar = self.addToolBar("Workflow")
        toolbar.setMovable(False)
        self._actions = {}

        def action(name, text, slot, shortcut=None, tip=""):
            act = QtWidgets.QAction(text, self)
            act.triggered.connect(lambda _checked=False: slot())
            if shortcut:
                act.setShortcut(shortcut)
            if tip:
                act.setToolTip(tip)
                act.setStatusTip(tip)
            toolbar.addAction(act)
            self._actions[name] = act
            return act

        action("new", "New", self.new_document, "Ctrl+N")
        action("open", "Open…", self.open_file, "Ctrl+O")
        action("save", "Save", self.save, "Ctrl+S")
        action("save_as", "Save as…", self.save_as, "Ctrl+Shift+S")
        toolbar.addSeparator()
        action("validate", "Validate", self.validate_now, "F7", "List everything that would stop the run")
        action("run", "Run…", self.run, "F5", "Run this workflow; results go to the reconstruction list")
        action("run_on_results", "Run on selected results…", self.run_on_results, None,
               "Apply the processing steps to every result selected in the reconstruction list")
        action("cancel", "Cancel run", self.cancel_run, None, "Stop the running workflow at its next step")
        toolbar.addSeparator()
        action("reload", "Reload plugins", self.reload_plugins, None, "Re-scan the drop-in plugins folder")

        # left: files and palette
        left = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self._files = QtWidgets.QListWidget()
        self._files.setToolTip(str(self._folder))
        self._files.itemActivated.connect(lambda item: self.open_file(str(self._folder / item.text())))
        self._palette = PluginPalette()
        self._palette.set_catalog(self.catalog)
        self._palette.sigAddRequested.connect(self.add_step)
        add_button = QtWidgets.QPushButton("Add step")
        add_button.clicked.connect(self._add_from_palette)
        left_layout.addWidget(QtWidgets.QLabel("Workflow files"))
        left_layout.addWidget(self._files, 1)
        left_layout.addWidget(QtWidgets.QLabel("Add a step"))
        left_layout.addWidget(self._palette, 2)
        left_layout.addWidget(add_button)

        # centre: name, steps, issues
        centre = QtWidgets.QWidget()
        centre_layout = QtWidgets.QVBoxLayout(centre)
        centre_layout.setContentsMargins(0, 0, 0, 0)
        head = QtWidgets.QFormLayout()
        self._name = QtWidgets.QLineEdit()
        self._name.editingFinished.connect(self._on_name_edited)
        self._description = QtWidgets.QLineEdit()
        self._description.editingFinished.connect(self._on_description_edited)
        head.addRow("Name", self._name)
        head.addRow("Description", self._description)
        centre_layout.addLayout(head)
        self._steps = StepList()
        self._steps.sigStepSelected.connect(self._on_step_selected)
        centre_layout.addWidget(self._steps, 3)
        buttons = QtWidgets.QHBoxLayout()
        for text, slot, tip in (("▲ Up", lambda: self.move_selected(-1), "Move the step earlier"),
                                ("▼ Down", lambda: self.move_selected(+1), "Move the step later"),
                                ("Remove", self.remove_selected, "Remove the step (references to it are reported)")):
            button = QtWidgets.QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(lambda _checked=False, s=slot: s())
            buttons.addWidget(button)
        buttons.addStretch(1)
        centre_layout.addLayout(buttons)
        centre_layout.addWidget(QtWidgets.QLabel("Issues"))
        self._issues = IssuePanel()
        self._issues.sigIssueSelected.connect(self._steps.select_step)
        centre_layout.addWidget(self._issues, 1)

        # right: the step
        self._form = StepForm()
        self._form.sigChanged.connect(self._on_step_changed)
        self._form.sigStructureChanged.connect(self._on_structure_changed)
        self._form.sigMessage.connect(self.show_status)

        splitter = QtWidgets.QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(centre)
        splitter.addWidget(self._form)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setStretchFactor(2, 2)
        self.setCentralWidget(splitter)

        self._progress = QtWidgets.QProgressBar()
        self._progress.setMaximumWidth(220)
        self._progress.hide()
        self.statusBar().addPermanentWidget(self._progress)
        self.set_running(False)

    # -- documents -----------------------------------------------------------------

    def load_document(self, document: WorkflowDocument, *, confirm: bool = True) -> bool:
        """Show ``document``; asks first when the current one has unsaved edits."""
        if confirm and not self._maybe_discard():
            return False
        if document.catalog is None:
            document.catalog = self.catalog
        self.document = document
        self._form.set_document(document)
        self._name.setText(document.name)
        self._description.setText(document.workflow.description)
        self._refresh()
        self._refresh_files()
        self._update_title()
        for note in document.notes:
            self.show_status(note)
        return True

    def new_document(self) -> None:
        self.load_document(WorkflowDocument(catalog=self.catalog))

    def open_file(self, path=None) -> bool:
        if path is None:
            path, _filter = QtWidgets.QFileDialog.getOpenFileName(self, "Open workflow", str(self._folder), _FILTER)
            if not path:
                return False
        try:
            document = WorkflowDocument.load(path, self.catalog)
        except (WorkflowError, OSError, ValueError) as exc:
            self.show_status(f"Could not read {Path(path).name}: {exc}")
            return False
        if not self.load_document(document):
            return False
        self.show_status(f"Opened {Path(path).name} ({len(document.steps)} steps).")
        return True

    def save(self, path=None) -> bool:
        if path is None and self.document.path is None:
            return self.save_as()
        try:
            written = self.document.save(path)
        except (DocumentError, OSError, ValueError) as exc:
            self.show_status(f"Could not save: {exc}")
            return False
        self._refresh_files()
        self._update_title()
        self.show_status(f"Saved {written.name}.")
        return True

    def save_as(self) -> bool:
        suggested = self.document.path or (self._folder / f"{self.document.name}.yaml")
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(self, "Save workflow", str(suggested), _FILTER)
        if not path:
            return False
        if not path.lower().endswith(_SUFFIXES):
            path += ".yaml"
        return self.save(path)

    def _maybe_discard(self) -> bool:
        if not self.document.dirty:
            return True
        answer = QtWidgets.QMessageBox.question(
            self, "Unsaved changes", f"'{self.document.name}' has unsaved changes. Discard them?",
            QtWidgets.QMessageBox.Discard | QtWidgets.QMessageBox.Cancel, QtWidgets.QMessageBox.Cancel,
        )
        return answer == QtWidgets.QMessageBox.Discard

    def closeEvent(self, event) -> None:
        if not self._maybe_discard():
            event.ignore()
            return
        super().closeEvent(event)
        self.sig_closed.emit()

    # -- steps ---------------------------------------------------------------------

    def add_step(self, kind: str, plugin_id: str = "") -> bool:
        """Add a step after the selected one (or at the end) and select it."""
        after = self._steps.current_step_id()
        try:
            step = self.document.add_step(kind, plugin_id or None, after=after)
        except DocumentError as exc:
            self.show_status(str(exc))
            return False
        self._refresh(select=step.id)
        return True

    def _add_from_palette(self) -> None:
        choice = self._palette.current_choice()
        if choice is None:
            self.show_status("Pick a step or plugin in the palette first.")
            return
        self.add_step(*choice)

    def move_selected(self, delta: int) -> None:
        step_id = self._steps.current_step_id()
        if not step_id:
            return
        try:
            self.document.move_step(step_id, self.document.index_of(step_id) + delta)
        except DocumentError as exc:
            self.show_status(str(exc))
            return
        self._refresh(select=step_id)

    def remove_selected(self) -> None:
        step_id = self._steps.current_step_id()
        if not step_id:
            return
        self.document.remove_step(step_id)
        self._refresh()

    def selected_step_id(self) -> str | None:
        return self._steps.current_step_id()

    def select_step(self, step_id: str) -> None:
        self._steps.select_step(step_id)

    @property
    def step_form(self) -> StepForm:
        return self._form

    def _on_step_selected(self, step_id: str) -> None:
        self._form.show_step(step_id or None)

    def _on_step_changed(self, _step_id: str) -> None:
        self._refresh_issues()
        self._update_title()

    def _on_structure_changed(self, step_id: str) -> None:
        self._refresh(select=step_id, rebuild_form=False)

    def _on_name_edited(self) -> None:
        if self._name.text() != self.document.name:
            self.document.set_name(self._name.text())
            self._update_title()

    def _on_description_edited(self) -> None:
        if self._description.text() != self.document.workflow.description:
            self.document.set_description(self._description.text())
            self._update_title()

    # -- refresh -------------------------------------------------------------------

    def _refresh(self, *, select: str | None = None, rebuild_form: bool = True) -> None:
        issues = self.document.issues()
        self._steps.refresh(self.document, issues)
        self._issues.set_issues(issues)
        if select:
            self._steps.select_step(select)
        current = self._steps.current_step_id()
        if rebuild_form or self._form.step_id != current:
            self._form.show_step(current)
        self._update_title()

    def _refresh_issues(self) -> None:
        issues = self.document.issues()
        self._steps.refresh(self.document, issues)
        self._issues.set_issues(issues)

    def _refresh_files(self) -> None:
        self._files.clear()
        try:
            names = sorted(p.name for p in self._folder.iterdir() if p.suffix.lower() in _SUFFIXES)
        except OSError:
            names = []
        self._files.addItems(names)

    def _update_title(self) -> None:
        name = self.document.path.name if self.document.path else self.document.name
        self.setWindowTitle(f"{'*' if self.document.dirty else ''}{name} — ImProcess workflow editor")

    def issues(self):
        return self.document.issues()

    def validate_now(self) -> list:
        issues = self.document.issues()
        self._refresh_issues()
        self.show_status("No issues." if not issues else f"{len(issues)} issue(s); see the list.")
        return issues

    # -- running ---------------------------------------------------------------------

    def run(self) -> bool:
        if self._running:
            self.show_status("A workflow is already running.")
            return False
        issues = self.validate_now()
        if issues:
            return False
        root = str(self.document.path.parent) if self.document.path else None
        self.sig_run_requested.emit(self._snapshot(), root)
        return True

    def run_on_results(self) -> bool:
        if self._running:
            self.show_status("A workflow is already running.")
            return False
        if self.validate_now():
            return False
        self.sig_run_on_results_requested.emit(self._snapshot())
        return True

    def _snapshot(self):
        """A copy of the workflow: the run reads it on another thread while
        the operator may keep editing this one."""
        from imswitch.improcess.workflows.steps import Workflow

        return Workflow.from_dict(self.document.to_dict())

    def cancel_run(self) -> None:
        if self._running:
            self.sig_cancel_requested.emit()

    def set_running(self, running: bool) -> None:
        self._running = bool(running)
        self._actions["run"].setEnabled(not running)
        self._actions["run_on_results"].setEnabled(not running)
        self._actions["cancel"].setEnabled(bool(running))
        if not running:
            self._progress.hide()

    def set_progress(self, index: int, total: int, step_id: str) -> None:
        self._progress.setMaximum(max(int(total), 1))
        self._progress.setValue(min(int(index), int(total)))
        self._progress.show()
        if step_id == "done":
            self.show_status("Workflow finished.")
        else:
            self.show_status(f"Running step {index + 1}/{total}: {step_id}")

    def show_status(self, message: str, timeout_ms: int = 8000) -> None:
        self.statusBar().showMessage(str(message), int(timeout_ms))

    def reload_plugins(self) -> None:
        self.catalog = build_catalog()
        self.document.catalog = self.catalog
        self._palette.set_catalog(self.catalog)
        self._refresh()
        self.show_status(f"{len(self.catalog.processors)} processors, {len(self.catalog.reconstructors)} reconstructors.")


__all__ = ["MainWindow"]


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
