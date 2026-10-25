"""The workflow editor window.

The palette on the left, the workflow in the middle -- as a graph of boxes and
cables (the default) or as a list -- and the selected step in the inspector
on the right; all over one Qt-free :class:`WorkflowDocument`. Every edit,
from whichever view, is one ``document.history.edit``: undoable, and
followed by one refresh of every view.

The window owns no batch run: *Run and publish* hands the workflow to whoever
connected ``sig_run_requested`` (the ``WorkflowController``, which runs it as
*File → Run workflow…* does).
"""

from __future__ import annotations

from pathlib import Path

from qtpy import QtCore, QtGui, QtWidgets

from imswitch.improcess.model.workfloweditor import DocumentError, WorkflowDocument, build_catalog
from imswitch.improcess.model.workfloweditor.folders import workflows_directory
from imswitch.improcess.model.workfloweditor.nodes import kinds_of
from imswitch.improcess.model.workfloweditor.preferences import EditorPreferences
from imswitch.improcess.workflows.steps import Save, WorkflowError

from .canvas import CanvasView, EdgeItem, NodeItem, QuickAddPopup, WorkflowScene
from .canvas import style as st
from .inspector import Inspector
from .palette import StepPalette
from .steplist import StepList

_FILTER = "Workflow (*.yaml *.yml *.json)"
_SUFFIXES = (".yaml", ".yml", ".json")

_STYLE = """
QToolBar#wfToolbar { spacing: 4px; padding: 4px 8px; }
QToolBar#wfToolbar QToolButton { padding: 4px 9px; border-radius: 5px; }
QToolBar#wfToolbar QToolButton#runButton { background: #2f7d55; border: 1px solid #3c9a6e; color: white;
  font-weight: bold; padding: 4px 14px; }
QToolBar#wfToolbar QToolButton#runButton:disabled { background: #2b3f35; color: #9fb3a8; }
QToolBar#wfToolbar QToolButton:checked { background: #34407a; border: 1px solid #6666cc; color: white; }
QToolButton#chipOk { background: #1d3a2c; color: #8fe0b2; border: 1px solid #2f6a4b; border-radius: 10px; padding: 3px 10px; }
QToolButton#chipWarn { background: #3d2c17; color: #f5c48a; border: 1px solid #7a5420; border-radius: 10px; padding: 3px 10px; }
QToolButton#chipBusy { background: #16283a; color: #9fd0f5; border: 1px solid #2a5a85; border-radius: 10px; padding: 3px 10px; }
QLabel#wfTitle { font-weight: bold; font-size: 11pt; padding: 0 8px; }
"""


class MainWindow(QtWidgets.QMainWindow):
    """See the module docstring."""

    sig_closed = QtCore.Signal()
    #: The workflow to run, and the folder relative source paths resolve against (or None).
    sig_run_requested = QtCore.Signal(object, object)
    sig_run_on_results_requested = QtCore.Signal(object)
    sig_cancel_requested = QtCore.Signal()

    def __init__(self, catalog=None, *, start_folder=None, preferences=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ImProcess workflow editor")
        self.setStyleSheet(_STYLE)
        self.resize(1480, 860)
        self.catalog = catalog if catalog is not None else build_catalog()
        self.preferences = preferences if preferences is not None else EditorPreferences()
        self._folder = Path(start_folder) if start_folder else Path(workflows_directory())
        self.document = WorkflowDocument(catalog=self.catalog)
        self._running = False
        self._issues = []
        self._build_ui()
        self.load_document(self.document, confirm=False)
        self.set_mode(self.preferences.mode, remember=False)

    # -- ui -------------------------------------------------------------------------

    def _build_ui(self) -> None:
        self._actions: dict[str, QtWidgets.QAction] = {}
        self.scene = WorkflowScene(self)
        self.view = CanvasView(self.scene)
        self.list_view = StepList()
        self.inspector = Inspector()
        self.inspector.set_editor(lambda label, action, structural=False, merge=None: self.edit(
            label, action, inspector=structural, merge=merge))
        self.palette = StepPalette()
        self.palette.set_catalog(self.catalog, self.preferences.recent_steps())

        self._build_actions()
        self._build_menus()
        self._build_toolbar()

        self.centre = QtWidgets.QStackedWidget()
        self.centre.addWidget(self.view)
        self.centre.addWidget(self.list_view)

        left = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 4, 8)
        left_layout.addWidget(self.palette)
        left.setMinimumWidth(220)

        self.splitter = QtWidgets.QSplitter()
        self.splitter.addWidget(left)
        self.splitter.addWidget(self.centre)
        self.splitter.addWidget(self.inspector)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([250, 830, 400])
        self.setCentralWidget(self.splitter)

        self._progress = QtWidgets.QProgressBar()
        self._progress.setMaximumWidth(220)
        self._progress.hide()
        self.statusBar().addPermanentWidget(self._progress)

        self.palette.sigAddRequested.connect(self.add_step)
        self.scene.selectionChanged.connect(self._on_scene_selection)
        self.scene.sigConnect.connect(self._on_connect)
        self.scene.sigReconnect.connect(self._on_reconnect)
        self.scene.sigDisconnect.connect(self._on_disconnect)
        self.scene.sigQuickAdd.connect(self._on_quick_add)
        self.scene.sigMoved.connect(self._on_moved)
        self.scene.sigMessage.connect(self.show_status)
        self.scene.sigActivated.connect(self._on_activated)
        self.view.sigDropStep.connect(self._on_drop_step)
        self.view.sigDropFiles.connect(self._on_drop_files)
        self.view.sigQuickAddHere.connect(lambda pos: self._on_quick_add(None, pos))
        self.view.sigContextMenu.connect(self._on_context_menu)
        self.view.sigTidyRequested.connect(self.tidy_up)
        self.list_view.sigStepSelected.connect(self._on_list_selection)
        self.inspector.sigMessage.connect(self.show_status)
        self.inspector.sigRenamed.connect(lambda _old, new: self.select_step(new))
        self.set_running(False)

    def _action(self, name, text, slot, shortcut=None, tip="", *, checkable=False, context=None) -> QtWidgets.QAction:
        act = QtWidgets.QAction(text, self)
        act.setCheckable(checkable)
        if checkable:
            act.toggled.connect(lambda checked: slot(checked))
        else:
            act.triggered.connect(lambda _checked=False: slot())
        if shortcut:
            act.setShortcuts([QtGui.QKeySequence(s) for s in (shortcut if isinstance(shortcut, (list, tuple)) else [shortcut])])
        if tip:
            act.setToolTip(tip)
            act.setStatusTip(tip)
        if context is not None:
            act.setShortcutContext(context)
        self._actions[name] = act
        self.addAction(act)
        return act

    def _build_actions(self) -> None:
        canvas = QtCore.Qt.WidgetWithChildrenShortcut
        a = self._action
        a("new", "New", self.new_document, "Ctrl+N", "Start an empty workflow")
        a("open", "Open…", self.open_file, "Ctrl+O", "Open a workflow file")
        a("save", "Save", self.save, "Ctrl+S", "Save the workflow")
        a("save_as", "Save as…", self.save_as, "Ctrl+Shift+S")
        a("close", "Close", self.close, "Ctrl+W")
        a("undo", "Undo", self.undo, QtGui.QKeySequence.Undo, "Undo the last edit")
        a("redo", "Redo", self.redo, [QtGui.QKeySequence.Redo, "Ctrl+Shift+Z"], "Redo the edit you undid")
        a("delete", "Delete", self.delete_selected, [QtCore.Qt.Key_Delete, QtCore.Qt.Key_Backspace],
          "Delete the selected steps or cables", context=canvas)
        a("duplicate", "Duplicate", self.duplicate_selected, "Ctrl+D", "Copy the selected steps next to them",
          context=canvas)
        a("copy", "Copy", self.copy_selected, QtGui.QKeySequence.Copy, context=canvas)
        a("cut", "Cut", self.cut_selected, QtGui.QKeySequence.Cut, context=canvas)
        a("paste", "Paste", self.paste, QtGui.QKeySequence.Paste, context=canvas)
        a("select_all", "Select all", self.select_all, QtGui.QKeySequence.SelectAll, context=canvas)
        a("fit", "Fit to window", self.view.fit, "F", "Show the whole workflow", context=canvas)
        a("tidy", "Tidy up", self.tidy_up, "H", "Lay the steps out left to right", context=canvas)
        a("zoom_in", "Zoom in", lambda: self.view.zoom_by(1.2), QtGui.QKeySequence.ZoomIn, context=canvas)
        a("zoom_out", "Zoom out", lambda: self.view.zoom_by(1 / 1.2), QtGui.QKeySequence.ZoomOut, context=canvas)
        a("quick_add", "Add a step…", lambda: self._on_quick_add(None, self._canvas_point()), None,
          "Search for a step to add (Tab on the canvas)")
        a("graph", "Graph", lambda checked: checked and self.set_mode("graph"), "Ctrl+1",
          "Show the workflow as boxes and cables", checkable=True)
        a("list", "List", lambda checked: checked and self.set_mode("list"), "Ctrl+2",
          "Show the workflow as a list of steps in run order", checkable=True)
        group = QtWidgets.QActionGroup(self)
        group.addAction(self._actions["graph"])
        group.addAction(self._actions["list"])
        a("validate", "Check", self.validate_now, "F7", "List everything that would stop the run")
        a("run", "▶  Run", self.run, "F5", "Run the workflow; results go to the reconstruction list")
        a("run_on_results", "Run on selected results…", self.run_on_results, None,
          "Apply the processing steps to every result selected in the reconstruction list")
        a("cancel", "■  Stop", self.cancel_run, "Esc", "Stop the running workflow")
        a("reload", "Reload plugins", self.reload_plugins, None, "Re-scan the drop-in plugins folder")

    def _build_menus(self) -> None:
        menus = self.menuBar()
        file_menu = menus.addMenu("&File")
        for name in ("new", "open"):
            file_menu.addAction(self._actions[name])
        self._recent_menu = file_menu.addMenu("Open recent")
        self._recent_menu.aboutToShow.connect(self._fill_recent_menu)
        for name in ("save", "save_as"):
            file_menu.addAction(self._actions[name])
        file_menu.addSeparator()
        file_menu.addAction(self._actions["close"])
        edit_menu = menus.addMenu("&Edit")
        for name in ("undo", "redo", None, "cut", "copy", "paste", "duplicate", "delete", None, "select_all",
                     "quick_add"):
            edit_menu.addSeparator() if name is None else edit_menu.addAction(self._actions[name])
        view_menu = menus.addMenu("&View")
        for name in ("graph", "list", None, "fit", "tidy", "zoom_in", "zoom_out"):
            view_menu.addSeparator() if name is None else view_menu.addAction(self._actions[name])
        run_menu = menus.addMenu("&Run")
        self.run_menu = run_menu
        for name in ("run", "run_on_results", "cancel", None, "validate"):
            run_menu.addSeparator() if name is None else run_menu.addAction(self._actions[name])
        plugins = menus.addMenu("&Plugins")
        plugins.addAction(self._actions["reload"])

    def _build_toolbar(self) -> None:
        bar = self.addToolBar("Workflow")
        bar.setObjectName("wfToolbar")
        bar.setMovable(False)
        self.toolbar = bar
        self._title = QtWidgets.QLabel("workflow")
        self._title.setObjectName("wfTitle")
        bar.addWidget(self._title)
        bar.addAction(self._actions["new"])
        open_button = QtWidgets.QToolButton()
        open_button.setDefaultAction(self._actions["open"])
        open_button.setPopupMode(QtWidgets.QToolButton.MenuButtonPopup)
        open_button.setMenu(self._recent_menu)
        bar.addWidget(open_button)
        bar.addAction(self._actions["save"])
        bar.addSeparator()
        bar.addAction(self._actions["undo"])
        bar.addAction(self._actions["redo"])
        bar.addSeparator()
        bar.addAction(self._actions["run"])
        bar.widgetForAction(self._actions["run"]).setObjectName("runButton")
        bar.addAction(self._actions["cancel"])
        self._toolbar_run_slot = bar.addSeparator()
        spacer = QtWidgets.QWidget()
        spacer.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        bar.addWidget(spacer)
        bar.addAction(self._actions["graph"])
        bar.addAction(self._actions["list"])
        bar.addSeparator()
        self.chip = QtWidgets.QToolButton()
        self.chip.setObjectName("chipOk")
        self.chip.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.chip.setMenu(QtWidgets.QMenu(self.chip))
        self.chip.menu().aboutToShow.connect(self._fill_issue_menu)
        bar.addWidget(self.chip)

    def _fill_recent_menu(self) -> None:
        menu = self._recent_menu
        menu.clear()
        files = [path for path in self.preferences.recent_files() if Path(path).exists()]
        for path in files:
            act = menu.addAction(Path(path).name)
            act.setToolTip(path)
            act.triggered.connect(lambda _c=False, p=path: self.open_file(p))
        if not files:
            act = menu.addAction("No recent workflows")
            act.setEnabled(False)
        menu.addSeparator()
        folder = menu.addAction(f"Workflow folder: {self._folder}")
        folder.triggered.connect(lambda: self.open_file())

    def _fill_issue_menu(self) -> None:
        menu = self.chip.menu()
        menu.clear()
        if not self._issues:
            act = menu.addAction("Nothing stops this workflow from running.")
            act.setEnabled(False)
            return
        for issue in self._issues:
            act = menu.addAction(f"{issue.step}: {issue.message}")
            act.triggered.connect(lambda _c=False, step=issue.step: self.select_step(step))

    # -- modes ---------------------------------------------------------------------------

    @property
    def mode(self) -> str:
        return "list" if self.centre.currentWidget() is self.list_view else "graph"

    def set_mode(self, mode: str, *, remember: bool = True) -> None:
        mode = "list" if mode == "list" else "graph"
        self._actions[mode].setChecked(True)
        selected = self.selected_step_ids()
        self.centre.setCurrentWidget(self.list_view if mode == "list" else self.view)
        if selected:
            self.select_step(selected[0])
        if remember:
            self.preferences.mode = mode

    # -- documents -----------------------------------------------------------------------

    def load_document(self, document: WorkflowDocument, *, confirm: bool = True) -> bool:
        """Show ``document``; asks first when the current one has unsaved edits."""
        if confirm and not self._maybe_discard():
            return False
        if document.catalog is None:
            document.catalog = self.catalog
        self.document = document
        self.inspector.set_document(document)
        self.scene.set_document(document)
        self.refresh(inspector=True)
        QtCore.QTimer.singleShot(0, self.view.fit)
        self.view.fit()
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
        self.preferences.add_recent_file(path)
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
        self.preferences.add_recent_file(written)
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

    # -- editing ---------------------------------------------------------------------------

    def edit(self, label: str, action, *, inspector: bool = True, merge=None, select=None) -> bool:
        """Make one undoable edit and refresh every view; False (and why, in the
        status bar) when the document refused it."""
        try:
            with self.document.history.edit(label, merge=merge):
                action()
        except (DocumentError, KeyError, ValueError) as exc:
            message = exc.args[0] if isinstance(exc, KeyError) and exc.args else exc
            self.show_status(f"{label}: {message}" if isinstance(exc, KeyError) else str(message))
            self.refresh(inspector=True)
            return False
        self.refresh(inspector=inspector, select=select)
        return True

    def _freeze_positions(self) -> None:
        """Store where every box is drawn, before an edit that places one: a
        file nobody laid out keeps its layout once someone starts to."""
        stored = self.document.positions
        for step_id, (x, y) in self.scene.shown_positions().items():
            if step_id not in stored:
                self.document.set_position(step_id, x, y)

    def add_step(self, kind: str, plugin_id: str = "", *, at: QtCore.QPointF | None = None, inputs=(),
                 splice=None) -> bool:
        """Add a step and select it: at ``at``, or next to the selected step
        (reading from it when it can)."""
        selected = self.selected_step_ids()
        after = selected[0] if selected and not inputs and at is None else None
        created = []

        def action():
            self._freeze_positions()
            step = self.document.add_step(kind, plugin_id or None, after=after, inputs=inputs)
            created.append(step.id)
            if at is not None:
                self.document.set_position(step.id, at.x() - st.NODE_W / 2, at.y() - st.HEADER_H / 2)
            elif after is not None and after in self.scene.nodes:
                node = self.scene.nodes[after]
                self.document.set_position(step.id, node.pos().x() + st.NODE_W + st.H_GAP, node.pos().y())
            if splice is not None:
                self.document.splice(step.id, *splice)

        title = self._candidate_title(kind, plugin_id)
        ok = self.edit(f"Add {title}", action, select=created)
        if ok:
            self.preferences.add_recent_step(kind, plugin_id or "")
            self.palette.set_recent(self.preferences.recent_steps())
        return ok

    def _candidate_title(self, kind: str, plugin_id: str) -> str:
        if kind in ("source", "save"):
            return "Recording" if kind == "source" else "Save"
        entry = self.catalog.processor(plugin_id) if kind == "process" else self.catalog.reconstructor(plugin_id)
        return entry.name if entry is not None else plugin_id

    def delete_selected(self) -> None:
        steps = self.selected_step_ids()
        edges = [edge for edge in self.scene.selected_edges() if edge.target.step_id not in steps]
        if not steps and not edges:
            return
        if any(isinstance(self.document.step(edge.target.step_id), Save) for edge in edges) and not steps:
            self.show_status("A Save needs an input: delete the Save instead")
            return

        def action():
            for edge in sorted(edges, key=lambda e: (e.target.step_id, -e.index)):
                if not isinstance(self.document.step(edge.target.step_id), Save):
                    self.document.disconnect(edge.target.step_id, edge.index)
            for step_id in steps:
                if self.document.has(step_id):
                    self.document.delete_step(step_id)

        label = f"Delete {', '.join(steps)}" if steps else "Delete a cable"
        self.edit(label, action)

    def duplicate_selected(self) -> None:
        steps = self.selected_step_ids()
        if not steps:
            return
        created = []

        def action():
            self._freeze_positions()
            created.extend(self.document.paste(self.document.fragment(steps), offset=(40.0, 40.0)))

        self.edit("Duplicate", action, select=created)

    def copy_selected(self) -> bool:
        steps = self.selected_step_ids()
        if not steps:
            return False
        self._freeze_positions_quietly()
        QtWidgets.QApplication.clipboard().setText(self.document.fragment(steps))
        self.show_status(f"Copied {len(steps)} step(s).")
        return True

    def _freeze_positions_quietly(self) -> None:
        """Positions for a copy, without an undo entry: the fragment carries them."""
        if len(self.document.positions) < len(self.document.steps):
            with self.document.history.edit("Lay out"):
                self._freeze_positions()

    def cut_selected(self) -> None:
        if self.copy_selected():
            self.delete_selected()

    def paste(self) -> None:
        import yaml

        text = QtWidgets.QApplication.clipboard().text()
        try:
            payload = yaml.safe_load(text)
            positions = dict(payload.get("positions") or {})
        except Exception:
            self.show_status("The clipboard holds no workflow steps.")
            return
        point = self._canvas_point()
        offset = (40.0, 40.0)
        if positions:
            xs = [float(xy[0]) for xy in positions.values()]
            ys = [float(xy[1]) for xy in positions.values()]
            offset = (point.x() - min(xs), point.y() - min(ys))
        created = []

        def action():
            self._freeze_positions()
            created.extend(self.document.paste(text, offset=offset))

        self.edit("Paste", action, select=created)

    def select_all(self) -> None:
        self.scene.select_steps(list(self.scene.nodes))

    def tidy_up(self) -> None:
        positions = self.scene.tidy_positions()

        def action():
            for step_id, (x, y) in positions.items():
                self.document.set_position(step_id, x, y)

        if self.edit("Tidy up", action, inspector=False):
            self.view.fit()

    def undo(self) -> None:
        label = self.document.history.undo()
        if label is None:
            return
        self.refresh(inspector=True)
        self.show_status(f"Undid: {label}")

    def redo(self) -> None:
        label = self.document.history.redo()
        if label is None:
            return
        self.refresh(inspector=True)
        self.show_status(f"Redid: {label}")

    # -- canvas requests ---------------------------------------------------------------------

    def _on_connect(self, ref: str, target: str, index, add: bool) -> None:
        self.edit("Connect", lambda: self.document.connect(ref, target, index=index, add=add), select=[target])

    def _on_reconnect(self, step_id: str, index: int, target: str, target_index) -> None:
        step = self.document.step(step_id)
        if isinstance(step, Save):
            self.show_status("A Save needs an input: delete the Save instead")
            self.refresh(inspector=False)
            return
        ref = str(step.inputs[index])

        def action():
            self.document.connect(ref, target, index=target_index)
            if not (target == step_id and target_index == index):
                self.document.disconnect(step_id, index)

        self.edit("Move a cable", action, select=[target])

    def _on_disconnect(self, step_id: str, index: int) -> None:
        self.edit("Disconnect", lambda: self.document.disconnect(step_id, index))

    def _on_moved(self, moved: dict, splice) -> None:
        def action():
            self._freeze_positions()
            for step_id, (x, y) in moved.items():
                if self.document.has(step_id):
                    self.document.set_position(step_id, x, y)
            if splice is not None:
                self.document.splice(*splice)

        self.edit("Put on the cable" if splice else "Move", action, inspector=bool(splice))

    def _on_quick_add(self, ref, pos: QtCore.QPointF) -> None:
        kind = producer = None
        title = "Add a step"
        if ref:
            producer_id = str(ref).split(".", 1)[0]
            if not self.document.has(producer_id):
                return
            producer = self.document.step(producer_id)
            kind = kinds_of(self.document).get(producer_id, "unknown")
            port = str(ref).split(".", 1)[1] if "." in str(ref) else producer_id
            title = f"Add a step that takes {port} · {st.KIND_WORDS.get(kind, kind)}"
        popup = QuickAddPopup(self.catalog, kind=kind, producer=producer, title=title,
                              recent=self.preferences.recent_steps(), parent=self)
        popup.sigChosen.connect(lambda candidate: self.add_step(
            candidate.step_kind, candidate.plugin_id, at=pos, inputs=[ref] if ref else ()))
        self._quick_add = popup
        popup.popup_at(self.view.mapToGlobal(self.view.mapFromScene(pos)))

    def _on_drop_step(self, kind: str, plugin_id: str, pos: QtCore.QPointF) -> None:
        edge = self.scene.edge_at(pos)
        splice = None
        if edge is not None and kind == "process":
            splice = (edge.target.step_id, edge.index)
        self.add_step(kind, plugin_id, at=pos, splice=splice)

    def _on_drop_files(self, paths: list, pos: QtCore.QPointF) -> None:
        workflows = [p for p in paths if p.lower().endswith(_SUFFIXES)]
        if workflows:
            self.open_file(workflows[0])
            return
        node = self.scene.node_at(pos)
        if node is not None and node.info.step_kind == "source":
            self.edit("Change the recording", lambda: self.document.set_source(node.step_id, path=paths[0]))
            return
        created = []

        def action():
            self._freeze_positions()
            step = self.document.add_step("source")
            self.document.set_source(step.id, path=paths[0])
            self.document.set_position(step.id, pos.x() - st.NODE_W / 2, pos.y() - st.HEADER_H / 2)
            created.append(step.id)

        self.edit("Add a recording", action, select=created)

    def _on_activated(self, step_id: str) -> None:
        self.select_step(step_id)
        editor = self.inspector.python_editor
        if editor is not None:
            editor.setFocus()

    def _on_context_menu(self, target, pos: QtCore.QPointF, global_pos: QtCore.QPoint) -> None:
        menu = QtWidgets.QMenu(self)
        if isinstance(target, NodeItem):
            self._node_menu(menu, target)
        elif isinstance(target, EdgeItem):
            insert = menu.addAction("Insert a step…")
            insert.triggered.connect(lambda: self._insert_on_edge(target, pos))
            delete = menu.addAction("Delete the cable")
            delete.triggered.connect(self.delete_selected)
        else:
            add = menu.addAction("Add a step…")
            add.triggered.connect(lambda: self._on_quick_add(None, pos))
            paste = menu.addAction("Paste")
            paste.triggered.connect(self.paste)
            menu.addSeparator()
            menu.addAction(self._actions["fit"])
            menu.addAction(self._actions["tidy"])
        self._context_menu = menu
        menu.popup(global_pos)

    def _node_menu(self, menu: QtWidgets.QMenu, node: NodeItem) -> None:
        menu.addAction(self._actions["duplicate"])
        menu.addAction(self._actions["copy"])
        menu.addAction(self._actions["delete"])

    def _insert_on_edge(self, edge: EdgeItem, pos: QtCore.QPointF) -> None:
        target, index = edge.target.step_id, edge.index
        kind = edge.kind
        popup = QuickAddPopup(self.catalog, kind=kind, title=f"Insert a step that takes {st.KIND_WORDS.get(kind, kind)}",
                              recent=self.preferences.recent_steps(), parent=self)
        popup.sigChosen.connect(lambda c: c.step_kind == "process" and self.add_step(
            c.step_kind, c.plugin_id, at=pos, splice=(target, index)))
        self._quick_add = popup
        popup.popup_at(self.view.mapToGlobal(self.view.mapFromScene(pos)))

    def _canvas_point(self) -> QtCore.QPointF:
        local = self.view.mapFromGlobal(QtGui.QCursor.pos())
        if not self.view.rect().contains(local):
            local = self.view.rect().center()
        return self.view.mapToScene(local)

    # -- selection ----------------------------------------------------------------------------

    def selected_step_ids(self) -> list[str]:
        if self.mode == "list":
            current = self.list_view.current_step_id()
            return [current] if current else []
        return self.scene.selected_step_ids()

    def selected_step_id(self) -> str | None:
        selected = self.selected_step_ids()
        return selected[0] if len(selected) == 1 else None

    def select_step(self, step_id: str) -> None:
        self.scene.select_steps([step_id])
        self.list_view.select_step(step_id)
        node = self.scene.node(step_id)
        if node is not None and self.mode == "graph":
            self.view.ensureVisible(node, 40, 40)
        self._show_selected()

    def _on_scene_selection(self) -> None:
        if self.mode == "graph":
            self._show_selected()

    def _on_list_selection(self, step_id: str) -> None:
        if self.mode == "list":
            self.scene.select_steps([step_id] if step_id else [])
            self._show_selected()

    def _show_selected(self) -> None:
        current = self.selected_step_id()
        if current != self.inspector.step_id:
            # Typing into another step's fields is another undo entry.
            self.document.history.seal()
            self.inspector.show_step(current)

    @property
    def step_form(self) -> Inspector:
        return self.inspector

    # -- refresh -------------------------------------------------------------------------------

    def refresh(self, *, inspector: bool = True, select=None) -> None:
        self._issues = self.document.issues()
        keep = list(select) if select else self.selected_step_ids()
        self.scene.refresh(self._issues)
        self.list_view.refresh(self.document, self._issues)
        if keep:
            self.scene.select_steps([s for s in keep if self.document.has(s)])
            if len(keep) == 1 and self.document.has(keep[0]):
                self.list_view.select_step(keep[0])
        self.view.set_empty_hint(not self.document.steps)
        if inspector or self.inspector.step_id != self.selected_step_id():
            self.inspector.show_step(self.selected_step_id())
        self._update_chip()
        self._update_title()
        self._update_undo()

    def _update_chip(self) -> None:
        issues = self._issues
        if self._running:
            return
        steps = len(self.document.steps)
        if not steps:
            text, name = "Empty: add a step", "chipOk"
        elif issues:
            text, name = f"⚠  {len(issues)} issue{'s' if len(issues) != 1 else ''}", "chipWarn"
        else:
            text, name = f"✓  Ready to run · {steps} step{'s' if steps != 1 else ''}", "chipOk"
        self._set_chip(text, name)

    def _set_chip(self, text: str, name: str) -> None:
        self.chip.setText(text)
        if self.chip.objectName() != name:
            self.chip.setObjectName(name)
            self.chip.style().unpolish(self.chip)
            self.chip.style().polish(self.chip)

    def _update_undo(self) -> None:
        history = self.document.history
        self._actions["undo"].setEnabled(history.can_undo)
        self._actions["redo"].setEnabled(history.can_redo)
        self._actions["undo"].setToolTip(f"Undo {history.undo_label}" if history.can_undo else "Nothing to undo")
        self._actions["redo"].setToolTip(f"Redo {history.redo_label}" if history.can_redo else "Nothing to redo")

    def _update_title(self) -> None:
        name = self.document.path.name if self.document.path else self.document.name
        self._title.setText(self.document.name)
        self.setWindowTitle(f"{'*' if self.document.dirty else ''}{name} — ImProcess workflow editor")

    def issues(self):
        return self.document.issues()

    def validate_now(self) -> list:
        self.refresh(inspector=False)
        issues = self._issues
        self.show_status("No issues: the workflow can run." if not issues else f"{len(issues)} issue(s): {issues[0]}")
        return issues

    # -- running -------------------------------------------------------------------------------

    def run(self) -> bool:
        if self._running:
            self.show_status("A workflow is already running.")
            return False
        if self.validate_now():
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
        if running:
            self._set_chip("Running…", "chipBusy")
        else:
            self._progress.hide()
            self._update_chip()

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
        self.palette.set_catalog(self.catalog, self.preferences.recent_steps())
        self.refresh(inspector=True)
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
