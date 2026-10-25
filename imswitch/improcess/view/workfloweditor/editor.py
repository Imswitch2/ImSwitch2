"""The workflow editor window.

The palette on the left, the workflow in the middle -- as a graph of boxes and
cables (the default) or as a list -- and the selected step in the inspector
on the right; all over one Qt-free :class:`WorkflowDocument`. Every edit,
from whichever view, is one ``document.history.edit``: undoable, and
followed by one refresh of every view.

**Runs happen in the editor.** *Run* and *Run to here* go through a
:class:`RunSession`: what is up to date comes from its cache, the rest runs on
the ``WorkflowController``'s worker (the window's ``host``), and every box
shows its state, timing and output as the run goes. Results stay in the editor
until *Show in viewer* (temporary) or *Pin* (into the results list). *Run and
add every result to the list* is the batch run of before: it hands the
workflow to whoever connected ``sig_run_requested``.
"""

from __future__ import annotations

import time
from pathlib import Path

from qtpy import QtCore, QtGui, QtWidgets

from imswitch.improcess.model.workfloweditor import DocumentError, WorkflowDocument, build_catalog
from imswitch.improcess.model.workfloweditor.folders import workflows_directory
from imswitch.improcess.model.workfloweditor.graph import ancestors
from imswitch.improcess.model.workfloweditor.nodes import kinds_of
from imswitch.improcess.model.workfloweditor.preferences import EditorPreferences
from imswitch.improcess.model.workfloweditor.previews import make_preview
from imswitch.improcess.model.workfloweditor.session import RunSession
from imswitch.improcess.workflows.steps import Reconstruct, Save, Source, WorkflowError

from .canvas import CanvasView, EdgeItem, NodeItem, QuickAddPopup, RunView, WorkflowScene
from .canvas import style as st
from .inspector import Inspector
from .listview import StepListView
from .outputpanel import failure_text
from .palette import StepPalette
from .startpage import StartPage
from .thumbnails import box_pixmap

#: Auto-run waits this long after the last edit, and runs only what took less than this last time.
AUTO_RUN_DELAY_MS = 600
AUTO_RUN_BUDGET_S = 2.0

_SHORTCUTS = (
    ("Tab, double-click", "Search for a step to add"),
    ("Drag output → input", "Connect (Shift: add the cable instead of replacing the input)"),
    ("Drag a cable into empty space", "Pick a step that takes it"),
    ("Drag an input's end away", "Disconnect"),
    ("Drop a new box on a cable", "Put it in between"),
    ("Del / Backspace", "Delete the selected steps, cables or frames"),
    ("Ctrl+D", "Duplicate"),
    ("Ctrl+C, Ctrl+X, Ctrl+V", "Copy, cut, paste (as text you can also paste elsewhere)"),
    ("Ctrl+G", "Put a frame around the selected steps"),
    ("Ctrl+Z, Ctrl+Shift+Z", "Undo, redo"),
    ("F5 / F6 / Esc", "Run / Run to selected / Stop"),
    ("F / H", "Fit everything / tidy up"),
    ("Wheel, middle button, Space+drag", "Zoom, pan"),
    ("Ctrl+1 / Ctrl+2", "Graph / List"),
)

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


class _PreviewSignals(QtCore.QObject):
    done = QtCore.Signal(object, object)     # token, Preview


class _PreviewJob(QtCore.QRunnable):
    """A larger preview (or another plane) of a cached result, off the GUI thread."""

    def __init__(self, token, result, size: int, plane):
        super().__init__()
        self.token, self.result, self.size, self.plane = token, result, size, plane
        self.signals = _PreviewSignals()

    def run(self) -> None:
        self.signals.done.emit(self.token, make_preview(self.result, self.size, self.plane))


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
        #: Whoever runs interactive runs (the WorkflowController); None standalone.
        self.host = None
        self.session = RunSession(self.catalog.registry)
        self._write_files = False
        self._out_dir = None
        self._overwrite = False
        self._queued = None
        self._active_plan = None
        self._pixmaps: dict = {}
        self._preview_token = None
        self._preview_jobs: list = []
        #: Auto-run may run this document's Python steps: built here, or run once on purpose.
        self._python_trusted = True
        self._auto_timer = QtCore.QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.setInterval(AUTO_RUN_DELAY_MS)
        self._auto_timer.timeout.connect(self._auto_run)
        self._build_ui()
        self.load_document(self.document, confirm=False)
        self.set_mode(self.preferences.mode, remember=False)
        self.show_start_page()

    # -- ui -------------------------------------------------------------------------

    def _build_ui(self) -> None:
        self._actions: dict[str, QtWidgets.QAction] = {}
        self.scene = WorkflowScene(self)
        self.view = CanvasView(self.scene)
        self.list_view = StepListView()
        self.start_page = StartPage()
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
        self.centre.addWidget(self.start_page)

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

        auto = self._actions["auto_run"]
        auto.blockSignals(True)
        auto.setChecked(bool(self.preferences.get("autoRun", False)))
        auto.blockSignals(False)
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
        self.list_view.sigActivated.connect(self._on_activated)
        self.list_view.sigContextMenu.connect(self._on_list_menu)
        self.scene.sigFrameRename.connect(self.rename_frame)
        page = self.start_page
        page.sigBlank.connect(self.new_blank)
        page.sigFromRecording.connect(self.new_from_recording)
        page.sigFromResult.connect(self.new_from_result)
        page.sigRecipe.connect(self.new_from_recipe)
        page.sigOpenFile.connect(self.open_file)
        page.sigOpenDialog.connect(lambda: self.open_file())
        self.inspector.sigMessage.connect(self.show_status)
        self.inspector.sigRenamed.connect(lambda _old, new: self.select_step(new))
        output = self.inspector.output
        output.sigRunToHere.connect(self.run_to_selected)
        output.sigShowInViewer.connect(lambda port: self.show_in_viewer(port=port))
        output.sigPin.connect(lambda port: self.pin(port=port))
        output.sigPreviewWanted.connect(self._want_preview)
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
        self._actions[name] = act
        if context is not None:
            # Canvas keys (Del, F, Ctrl+C...) act where the steps are shown, not
            # in the inspector's fields or the palette.
            act.setShortcutContext(context)
            self.view.addAction(act)
            self.list_view.addAction(act)
        else:
            self.addAction(act)
        return act

    def _build_actions(self) -> None:
        canvas = QtCore.Qt.WidgetWithChildrenShortcut
        a = self._action
        a("new", "New…", self.show_start_page, "Ctrl+N", "Start a workflow: from the open recording, a result, "
          "a recipe, or a blank canvas")
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
        a("frame", "Frame the selection", self.frame_selection, "Ctrl+G",
          "Put a labelled frame around the selected steps", context=canvas)
        a("shortcuts", "Keyboard shortcuts", self.show_shortcuts, QtGui.QKeySequence.HelpContents)
        a("graph", "Graph", lambda checked: checked and self.set_mode("graph"), "Ctrl+1",
          "Show the workflow as boxes and cables", checkable=True)
        a("list", "List", lambda checked: checked and self.set_mode("list"), "Ctrl+2",
          "Show the workflow as a list of steps in run order", checkable=True)
        group = QtWidgets.QActionGroup(self)
        group.addAction(self._actions["graph"])
        group.addAction(self._actions["list"])
        a("validate", "Check", self.validate_now, "F7", "List everything that would stop the run")
        a("run", "▶  Run", self.run, "F5",
          "Run every step that is not up to date; results stay in the editor (Pin adds one to the list)")
        a("run_to", "▶|  Run to selected", self.run_to_selected, "F6",
          "Run the selected step and what it needs; what is up to date comes from the cache")
        a("rerun", "↻  Re-run from selected", self.rerun_from_selected, None,
          "Forget the selected step's results and everything after it, and run to it again")
        a("cancel", "■  Stop", self.cancel_run, "Esc", "Stop the running workflow")
        a("write_files", "⤓  Write files", self.set_write_files, None,
          "Off: runs keep results in memory and Save steps only say what they would write", checkable=True)
        a("auto_run", "⟳  Auto-run", self.set_auto_run, None,
          f"Re-run outdated steps a moment after each edit, when they took less than {AUTO_RUN_BUDGET_S:g} s last "
          "time", checkable=True)
        a("run_publish", "Run and add every result to the list…", self.run_and_publish, None,
          "Run the whole workflow as File → Run workflow does: every result goes to the reconstruction list")
        a("run_on_results", "Run on selected results…", self.run_on_results, None,
          "Apply the processing steps to every result selected in the reconstruction list")
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
                     "quick_add", "frame"):
            edit_menu.addSeparator() if name is None else edit_menu.addAction(self._actions[name])
        view_menu = menus.addMenu("&View")
        for name in ("graph", "list", None, "fit", "tidy", "zoom_in", "zoom_out"):
            view_menu.addSeparator() if name is None else view_menu.addAction(self._actions[name])
        run_menu = menus.addMenu("&Run")
        self.run_menu = run_menu
        for name in ("run", "run_to", "rerun", "cancel", None, "write_files", "auto_run", None, "run_publish",
                     "run_on_results", None, "validate"):
            run_menu.addSeparator() if name is None else run_menu.addAction(self._actions[name])
        plugins = menus.addMenu("&Plugins")
        plugins.addAction(self._actions["reload"])
        help_menu = menus.addMenu("&Help")
        help_menu.addAction(self._actions["shortcuts"])

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
        bar.addAction(self._actions["run_to"])
        bar.addAction(self._actions["cancel"])
        bar.addAction(self._actions["auto_run"])
        bar.addAction(self._actions["write_files"])
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

    def show_start_page(self) -> None:
        """Offer the ways into a workflow (and recent files) in the centre."""
        host = self.host
        recording = host.currentRecording() if callable(getattr(host, "currentRecording", None)) else None
        result = host.activeResultName() if callable(getattr(host, "activeResultName", None)) else None
        try:
            from imswitch.improcess.model.workfloweditor.recipes import installed_recipes

            recipes = installed_recipes()
        except Exception:  # noqa: BLE001 - the page still offers the rest
            recipes = []
        try:
            folder_files = sorted(str(p) for p in self._folder.iterdir() if p.suffix.lower() in _SUFFIXES)
        except OSError:
            folder_files = []
        recent = [path for path in self.preferences.recent_files() if Path(path).exists()]
        self.start_page.set_state(recording=recording[0] if recording else None, result=result, recipes=recipes,
                                  recent=recent, folder=str(self._folder), folder_files=folder_files)
        self.centre.setCurrentWidget(self.start_page)
        for name in ("graph", "list"):
            self._actions[name].setChecked(False)

    def _leave_start_page(self) -> None:
        if self.centre.currentWidget() is self.start_page:
            self.set_mode(self.preferences.mode, remember=False)

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
        if self._running:
            self.show_status("Stop the run before opening another workflow.")
            return False
        self.release_session()
        self.session = RunSession(self.catalog.registry)
        self._pixmaps = {}
        self.document = document
        self._python_trusted = not _has_python(document) or (document.path is None and not document.notes)
        self.inspector.set_document(document)
        self.scene.set_document(document)
        self.refresh(inspector=True)
        QtCore.QTimer.singleShot(0, self.view.fit)
        self.view.fit()
        for note in document.notes:
            self.show_status(note)
        self._leave_start_page()
        return True

    def new_document(self) -> None:
        self.load_document(WorkflowDocument(catalog=self.catalog))

    def new_blank(self) -> bool:
        return self.load_document(WorkflowDocument(catalog=self.catalog))

    def new_from_recording(self) -> bool:
        """A source bound to the open recording, shown as it is (View only)."""
        from imswitch.improcess.workflows.steps import Workflow

        recording = self.host.currentRecording() if callable(getattr(self.host, "currentRecording", None)) else None
        if not recording:
            self.show_status("Open a recording in ImProcess first.")
            return False
        path = recording[0]
        stem = Path(path).stem.replace(" ", "-") or "recording"
        document = WorkflowDocument(Workflow(f"from-{stem}", [
            Source("raw"), Reconstruct("rec", "view-only", inputs=["raw"]),
        ]), catalog=self.catalog)
        if not self.load_document(document):
            return False
        self.bind_source("raw", path)
        self.select_step("rec")
        return True

    def new_from_result(self) -> bool:
        edit = getattr(self.host, "editWorkflowOfResult", None)
        if not callable(edit):
            self.show_status("Select a result in ImProcess first.")
            return False
        return edit() is not None

    def new_from_recipe(self, recipe) -> bool:
        from imswitch.improcess.model.workfloweditor.recipes import recipe_document

        if recipe is None:
            return False
        try:
            document = recipe_document(recipe, self.catalog)
        except ValueError as exc:
            self.show_status(str(exc))
            return False
        if not self.load_document(document):
            return False
        self.select_step("script")
        self.show_status(f"Recipe '{recipe.title}': drop a recording on 'raw', then Run (F5).")
        return True

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
        if self._running:
            self.cancel_run()
            self.show_status("Stopping the run; close the editor again once it has stopped.")
            event.ignore()
            return
        if not self._maybe_discard():
            event.ignore()
            return
        self.release_session()
        super().closeEvent(event)
        self.sig_closed.emit()

    def release_session(self) -> None:
        """Hand the session's open files over: pinned results' to the host
        (which keeps them while those results are loaded), the rest closed."""
        session = getattr(self, "session", None)
        if session is None or session.closed:
            return
        handover = session.close()
        adopt = getattr(self.host, "adoptHandles", None)
        if callable(adopt):
            adopt(handover)
        else:
            for _uids, handles in handover:
                for handle in handles:
                    session._close(handle)

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
        if self._actions["auto_run"].isChecked():
            self._auto_timer.start()
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
        frames = self.scene.selected_frames() if self.mode == "graph" else []
        if frames and not steps and not edges:
            def remove():
                for index in sorted(frames, reverse=True):
                    self.document.remove_frame(index)

            self.edit("Delete a frame", remove, inspector=False)
            return
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

    def _on_moved(self, moved: dict, splice, frames=None) -> None:
        def action():
            self._freeze_positions()
            for step_id, (x, y) in moved.items():
                if self.document.has(step_id):
                    self.document.set_position(step_id, x, y)
            for index, rect in (frames or {}).items():
                self.document.set_frame(index, rect=rect)
            if splice is not None:
                self.document.splice(*splice)

        self.edit("Put on the cable" if splice else "Move", action, inspector=bool(splice))

    def frame_selection(self, title: str | None = None) -> bool:
        """A frame around the selected steps (or in the middle of the view)."""
        selected = [self.scene.nodes[s] for s in self.selected_step_ids() if s in self.scene.nodes]
        if selected:
            area = QtCore.QRectF()
            for node in selected:
                area = area.united(node.sceneBoundingRect())
            area = area.adjusted(-24, -48, 24, 24)
        else:
            centre = self._canvas_point()
            area = QtCore.QRectF(centre.x() - 220, centre.y() - 140, 440, 280)
        if title is None:
            title, accepted = QtWidgets.QInputDialog.getText(self, "Frame", "What does this part do?", text="")
            if not accepted:
                return False
        rect = [area.x(), area.y(), area.width(), area.height()]
        return self.edit("Add a frame", lambda: (self._freeze_positions(), self.document.add_frame(title, rect)),
                         inspector=False)

    def rename_frame(self, index: int, title: str | None = None) -> bool:
        frames = self.document.frames
        if not 0 <= index < len(frames):
            return False
        if title is None:
            title, accepted = QtWidgets.QInputDialog.getText(self, "Frame", "Title:", text=frames[index]["title"])
            if not accepted:
                return False
        return self.edit("Rename a frame", lambda: self.document.set_frame(index, title=title), inspector=False)

    def show_shortcuts(self) -> None:
        rows = "".join(f"<tr><td style='padding-right:18px'><b>{keys}</b></td><td>{what}</td></tr>"
                       for keys, what in _SHORTCUTS)
        QtWidgets.QMessageBox.information(self, "Keyboard shortcuts", f"<table>{rows}</table>")

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
            self.bind_source(node.step_id, paths[0])
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

    def _on_list_menu(self, step_id: str, global_pos: QtCore.QPoint) -> None:
        menu = QtWidgets.QMenu(self)
        self._step_menu(menu, step_id)
        self._context_menu = menu
        menu.popup(global_pos)

    def _node_menu(self, menu: QtWidgets.QMenu, node: NodeItem) -> None:
        self._step_menu(menu, node.step_id)

    def _step_menu(self, menu: QtWidgets.QMenu, step_id: str) -> None:
        for text, slot in (("▶|  Run to here", lambda: self.run_to(step_id)),
                           ("↻  Re-run from here", lambda: self.rerun_from(step_id))):
            menu.addAction(text).triggered.connect(lambda _c=False, s=slot: s())
        held = self.session.result(self.document, step_id, source_root=self._source_root()) is not None
        view = menu.addAction("Show output in viewer")
        view.setEnabled(held)
        view.triggered.connect(lambda: self.show_in_viewer(step_id))
        pin = menu.addAction("Pin output to the results list")
        pin.setEnabled(held)
        pin.triggered.connect(lambda: self.pin(step_id))
        menu.addSeparator()
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
            self._update_output()

    @property
    def step_form(self) -> Inspector:
        return self.inspector

    # -- refresh -------------------------------------------------------------------------------

    def refresh(self, *, inspector: bool = True, select=None) -> None:
        self._issues = self.document.issues()
        keep = list(select) if select else self.selected_step_ids()
        runs = self._run_views()
        self.scene.runs = runs
        self.scene.refresh(self._issues)
        self.list_view.refresh(self.document, self._issues, runs)
        if keep:
            self.scene.select_steps([s for s in keep if self.document.has(s)])
            if len(keep) == 1 and self.document.has(keep[0]):
                self.list_view.select_step(keep[0])
        self.view.set_empty_hint(not self.document.steps)
        if inspector or self.inspector.step_id != self.selected_step_id():
            self.inspector.show_step(self.selected_step_id())
        self._update_output()
        self._update_chip()
        self._update_title()
        self._update_undo()

    def _update_chip(self) -> None:
        issues = self._issues
        if self._running:
            return
        steps = len(self.document.steps)
        states = self.session.states(self.document, self._source_root())
        failed = [sid for sid, state in states.items() if state.status == "failed"]
        outdated = [sid for sid, state in states.items() if state.status == "outdated"]
        if not steps:
            text, name = "Empty: add a step", "chipOk"
        elif issues:
            text, name = f"⚠  {len(issues)} issue{'s' if len(issues) != 1 else ''}", "chipWarn"
        elif failed:
            text, name = f"✕  Stopped at '{failed[0]}'", "chipWarn"
        elif outdated:
            text, name = f"↻  {len(outdated)} step{'s' if len(outdated) != 1 else ''} outdated", "chipWarn"
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

    def _source_root(self):
        return str(self.document.path.parent) if self.document.path else None

    def run(self) -> bool:
        """Run every step that is not up to date."""
        return self._start_run(None, explicit=True)

    def run_to_selected(self) -> bool:
        selected = self.selected_step_ids()
        if not selected:
            self.show_status("Select the step to run to.")
            return False
        return self._start_run(selected, explicit=True)

    def set_auto_run(self, on: bool) -> None:
        self.preferences.set("autoRun", bool(on))
        self.show_status("Auto-run is on: outdated steps run again a moment after each edit." if on else
                         "Auto-run is off.")
        if on:
            self._auto_timer.start()

    def _auto_run(self) -> bool:
        """After an edit: re-run the outdated steps up to the selection, when
        that is quick; never an opened file's Python code before one Run."""
        if not self._actions["auto_run"].isChecked() or not self.document.steps:
            return False
        states = self.session.states(self.document, self._source_root())
        selected = [s for s in self.selected_step_ids() if self.document.has(s)]
        outdated = [sid for sid, state in states.items() if state.status == "outdated"]
        targets = selected or outdated
        if not targets:
            return False
        if self._running:
            self._queued = (targets,)
            return True
        if self._issues_for(targets):
            return False
        plan = self.session.plan(self.document, targets, write_saves=False, out_dir=self._out_dir,
                                 source_root=self._source_root())
        if plan.empty:
            return False
        steps = [self.document.step(sid) for sid in plan.to_run if self.document.has(sid)]
        if not self._python_trusted and any(getattr(step, "processor", "") == "python" for step in steps):
            self.show_status("Auto-run waits for one Run (F5) before it runs this file's Python code.")
            return False
        last = []
        for step in steps:
            if isinstance(step, (Source, Save)):
                continue
            entry = states[step.id].entry
            last.append(entry.seconds if entry is not None else None)
        if any(seconds is None for seconds in last) or sum(s for s in last if s) > AUTO_RUN_BUDGET_S:
            self.show_status("Auto-run skipped: these steps were slow, or have not run yet. Run to here: F6.")
            return False
        return self._start_run(targets)

    def run_to(self, step_id: str) -> bool:
        self.select_step(step_id)
        return self._start_run([step_id])

    def rerun_from_selected(self) -> bool:
        selected = self.selected_step_ids()
        if not selected:
            self.show_status("Select the step to re-run from.")
            return False
        return self.rerun_from(*selected)

    def rerun_from(self, *step_ids: str) -> bool:
        for step_id in step_ids:
            self.session.rerun_from(self.document, step_id, self._source_root())
        self.refresh(inspector=False)
        return self._start_run(list(step_ids))

    def _issues_for(self, targets) -> list:
        if not targets:
            return list(self._issues)
        needed = set(targets)
        for target in targets:
            needed |= ancestors(self.document.steps, target)
        return [issue for issue in self._issues if issue.step in needed]

    def _start_run(self, targets, *, explicit: bool = False) -> bool:
        if self.host is None or not callable(getattr(self.host, "runInteractive", None)):
            self.show_status("Runs need ImProcess: open the editor from ImProcess (Workflow → Workflow editor).")
            return False
        if self._running:
            # Edits made meanwhile only mark steps outdated; one queued re-run
            # picks them all up when this run ends.
            self._queued = (targets,)
            self.show_status("The workflow will run again when this run ends.")
            return True
        self._issues = self.document.issues()
        issues = self._issues_for(targets)
        if issues:
            self.show_status(f"Cannot run: {issues[0]}")
            self.select_step(issues[0].step)
            return False
        root = self._source_root()
        plan = self.session.plan(self.document, targets, write_saves=self._write_files, out_dir=self._out_dir,
                                 overwrite=self._overwrite, source_root=root)
        if plan.empty:
            self.show_status("Everything you asked for is up to date.")
            return False
        for step_id in plan.to_run:
            step = self.document.step(step_id) if self.document.has(step_id) else None
            if isinstance(step, Source) and not step.source.path and step_id not in self.session.source_bindings:
                self.show_status(f"'{step_id}' has no recording yet: drop a file on it, or choose one in the inspector.")
                self.select_step(step_id)
                return False
        if explicit:
            self._python_trusted = True
        self.session.begin(plan)
        self._active_plan = plan
        self._run_started = time.monotonic()
        self._show_runs()
        if not self.host.runInteractive(plan, self.catalog.registry):
            self.session.finish(None, "not started")
            self._active_plan = None
            self.refresh(inspector=False)
            return False
        return True

    # Called by the host, on the GUI thread, as the run goes.

    def on_run_progress(self, index: int, total: int, step_id: str) -> None:
        if step_id == "done" or self._active_plan is None:
            return
        self.session.step_started(step_id)
        self._set_chip(f"Running {min(index + 1, total)}/{total} · {step_id}", "chipBusy")
        self._show_runs()

    def on_run_step(self, step_id: str, ports, seconds: float, cached: bool, previews) -> None:
        if self._active_plan is None:
            return
        self.session.step_done(step_id, dict(ports or {}), float(seconds), bool(cached), dict(previews or {}))
        self._show_runs()
        if step_id == self.selected_step_id():
            self._update_output()

    def on_run_ended(self, report, error) -> None:
        if self._active_plan is None:
            return
        self._active_plan = None
        summary = self.session.finish(report, error, document=self.document, keep=self.selected_step_ids())
        self.refresh(inspector=False)
        self.show_status(self._summary_text(summary), 20000)
        failure = summary.failure
        if failure and not summary.cancelled and failure.get("step") and self.document.has(failure["step"]):
            self.select_step(failure["step"])

    def _show_runs(self) -> None:
        runs = self._run_views()
        self.scene.set_runs(runs)
        if self.mode == "list":
            self.list_view.set_runs(runs)
        else:
            self.list_view._runs = runs

    def _summary_text(self, summary) -> str:
        from imswitch.imcommon.model.memory_limits import describeBytes, settingRef

        if summary.cancelled:
            text = "Stopped."
        elif summary.failure:
            failure = summary.failure
            where = f" at line {failure['line']}" if failure.get("line") else ""
            text = f"'{failure.get('step')}' failed{where}: {failure_text(failure, with_line=False)}"
        elif summary.error:
            text = f"The run failed: {summary.error}"
        else:
            text = f"Ran {len(summary.ran)} step{'s' if len(summary.ran) != 1 else ''} in {summary.seconds:.2f} s"
        if summary.cached:
            text += f" · {len(summary.cached)} from the cache"
        for step_id, path in list(summary.would_write.items())[:2]:
            text += f" · Write files is off: '{step_id}' would write {Path(path).name}"
        if summary.wrote:
            count = sum(len(files) for files in summary.wrote.values())
            text += f" · wrote {count} file{'s' if count != 1 else ''} to {self._out_dir}"
        text += (f" · cache {describeBytes(self.session.cache_bytes())} of {describeBytes(self.session.budget)}"
                 f" ({settingRef('workflowCacheBytes')})")
        return text

    def _run_views(self) -> dict:
        states = self.session.states(self.document, self._source_root(), write_saves=self._write_files)
        views = {}
        for step_id, state in states.items():
            entry = state.entry
            if not state.status and not state.bound and entry is None:
                continue
            thumbnail = self._thumbnail(entry) if entry is not None and state.status != "running" else None
            footer = ""
            if entry is not None and state.status in ("done", "outdated"):
                first = next(iter(entry.previews.values()), None)
                described = first.describe() if first is not None else ""
                footer = _seconds(entry.seconds) + (f"   {described}" if described else "")
                if state.cached and state.status == "done":
                    footer += "   · cached"
            summary = None
            if state.status == "would-write" and state.would_write is not None:
                summary = ("would write", Path(state.would_write).name)
            elif state.wrote:
                summary = ("wrote",) + tuple(Path(p).name for p in state.wrote[:2])
            elif state.bound and self.document.has(step_id):
                step = self.document.step(step_id)
                name = Path(state.bound).name if isinstance(step, Source) else state.bound
                summary = (f"runs on {name}", "(this session)")
            error = failure_text(state.error) if state.error else ""
            kinds = {port: preview.kind for port, preview in (entry.previews.items() if entry else ())
                     if preview.kind not in ("recording",)}
            tips = {port: preview.describe() + (" · a projection over sampled planes" if preview.sampled else "")
                    for port, preview in (entry.previews.items() if entry else ())}
            step = self.document.step(step_id) if self.document.has(step_id) else None
            refs = [step.input] if isinstance(step, Save) else list(getattr(step, "inputs", []) or [])
            for index, ref in enumerate(refs):
                producer = states.get(ref.step)
                previews = producer.entry.previews if producer is not None and producer.entry is not None else {}
                preview = previews.get(ref.port) if ref.port else next(iter(previews.values()), None)
                if preview is not None:
                    tips[f"in{index}"] = f"Last run: {preview.describe()}"
            views[step_id] = RunView(status=state.status, thumbnail=thumbnail, footer=footer, error=error,
                                     summary=summary, kinds=kinds, tips=tips,
                                     line=state.error.get("line") if state.error else None)
        return views

    def _thumbnail(self, entry):
        key = (entry.key, entry.run_id)
        if key not in self._pixmaps:
            if len(self._pixmaps) > 400:
                self._pixmaps.clear()
            self._pixmaps[key] = box_pixmap(entry.previews)
        return self._pixmaps[key]

    def _update_output(self) -> None:
        step_id = self.selected_step_id()
        panel = self.inspector.output
        if step_id is None or not self.document.has(step_id):
            panel.clear()
            return
        root = self._source_root()
        state = self.session.states(self.document, root, write_saves=self._write_files).get(step_id)
        if state is None:
            panel.clear()
            return
        panel.show_state(state.status, state.entry, state.error, cached=state.cached,
                         would_write=state.would_write, wrote=state.wrote)
        self.inspector.mark_line(state.error.get("line") if state.error else None)
        self._update_binding(step_id, state)

    def _want_preview(self, port: str, plane: int) -> None:
        step_id = self.selected_step_id()
        if step_id is None:
            return
        state = self.session.states(self.document, self._source_root()).get(step_id)
        entry = state.entry if state is not None else None
        if entry is None or not entry.held or port not in (entry.ports or {}):
            return
        result = entry.ports[port]
        preview = entry.previews.get(port)
        if preview is not None and preview.curve is not None:
            x = getattr(result, "x_values", None)
            try:
                import numpy as np

                y = np.asarray(result.data, dtype=float)
                self.inspector.output.show_full_curve(np.asarray(x) if x is not None else np.arange(y.size), y)
            except Exception:  # noqa: BLE001 - the sparkline stays
                pass
            return
        token = (step_id, port, plane, entry.key)
        self._preview_token = token
        job = _PreviewJob(token, result, 480, None if plane < 0 else plane)
        job.signals.done.connect(self._preview_ready)
        self._preview_jobs.append(job)
        QtCore.QThreadPool.globalInstance().start(job)

    def _preview_ready(self, token, preview) -> None:
        self._preview_jobs = [job for job in self._preview_jobs if job.token != token]
        if token != self._preview_token:
            return
        self.inspector.output.set_preview(token[1], preview)

    def _update_binding(self, step_id: str, state) -> None:
        area = self.inspector.binding_area
        layout = area.layout()
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        step = self.document.step(step_id)
        host = self.host
        if isinstance(step, Source):
            bound = self.session.source_bindings.get(step_id)
            if bound:
                text = f"In this session it runs on <b>{Path(bound).name}</b>; the file keeps its own setting."
            elif step.source.path:
                text = f"Runs on its file, <b>{Path(step.source.path).name}</b>."
            else:
                text = "No recording yet: drop a file on the box, or choose what to run it on."
            label = QtWidgets.QLabel(text)
            label.setWordWrap(True)
            layout.addWidget(label)
            row = QtWidgets.QHBoxLayout()
            choose = QtWidgets.QPushButton("Run on a file…")
            choose.setToolTip("Choose a recording to run this workflow on, in this session")
            choose.clicked.connect(lambda: self._choose_binding(step_id))
            row.addWidget(choose)
            recording = host.currentRecording() if callable(getattr(host, "currentRecording", None)) else None
            open_button = QtWidgets.QPushButton("Use the open recording")
            open_button.setEnabled(recording is not None)
            open_button.setToolTip(f"Run on {recording[0]}" if recording else "No recording with a file is open in ImProcess")
            open_button.clicked.connect(lambda: recording and self.bind_source(step_id, recording[0]))
            row.addWidget(open_button)
            if bound:
                unbind = QtWidgets.QPushButton("Unbind")
                unbind.clicked.connect(lambda: self.bind_source(step_id, None))
                row.addWidget(unbind)
            layout.addLayout(row)
            consumers = [s.id for s in self.document.steps if isinstance(s, Reconstruct)
                         and any(ref.step == step_id for ref in s.inputs)]
            if consumers and callable(getattr(host, "selectedResults", None)):
                use = QtWidgets.QPushButton(f"Start '{consumers[0]}' from the selected result")
                use.setToolTip("Use a result from the reconstruction list instead of reconstructing, as "
                               "Run on selected results does")
                use.clicked.connect(lambda: self.bind_selected_result(consumers[0]))
                layout.addWidget(use)
        elif isinstance(step, Reconstruct) and step_id in self.session.result_bindings:
            result = self.session.result_bindings[step_id]
            label = QtWidgets.QLabel(f"In this session it starts from the result "
                                     f"<b>{getattr(result, 'name', 'result')}</b> and is not run.")
            label.setWordWrap(True)
            layout.addWidget(label)
            unbind = QtWidgets.QPushButton("Reconstruct again")
            unbind.clicked.connect(lambda: self.bind_result(step_id, None))
            layout.addWidget(unbind)

    def _choose_binding(self, step_id: str) -> None:
        try:
            from imswitch.improcess.model.dataset_sources import SOURCE_SPECS, file_dialog_filter

            name_filter = file_dialog_filter(SOURCE_SPECS)
        except Exception:
            name_filter = "All files (*)"
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(self, "Run on which recording?", "", name_filter)
        if path:
            self.bind_source(step_id, path)

    def bind_source(self, step_id: str, path) -> None:
        """Run ``step_id`` on ``path`` in this session (``None`` unbinds)."""
        self.session.bind_source(self.document, step_id, path, self._source_root())
        self.refresh(inspector=False)
        self.show_status(f"'{step_id}' runs on {Path(path).name} in this session." if path else f"'{step_id}' unbound.")

    def bind_result(self, step_id: str, result) -> None:
        self.session.bind_result(self.document, step_id, result, self._source_root())
        self.refresh(inspector=False)

    def bind_selected_result(self, step_id: str) -> bool:
        results = self.host.selectedResults() if callable(getattr(self.host, "selectedResults", None)) else []
        if not results:
            return False
        self.bind_result(step_id, results[0])
        self.show_status(f"'{step_id}' starts from {getattr(results[0], 'name', 'the selected result')} in this session.")
        return True

    def show_in_viewer(self, step_id: str | None = None, port: str = "") -> bool:
        step_id = step_id or self.selected_step_id()
        if step_id is None:
            return False
        result = self.session.result(self.document, step_id, port or None, self._source_root())
        show = getattr(self.host, "showPreview", None)
        if result is None or not callable(show):
            self.show_status("Run the step first: nothing to show yet.")
            return False
        show(result, f"{step_id}{'.' + port if port else ''} (workflow preview)")
        self.show_status(f"Showing '{step_id}' in the viewer until the next preview; it is not in the results list.")
        return True

    def pin(self, step_id: str | None = None, port: str = "") -> bool:
        step_id = step_id or self.selected_step_id()
        if step_id is None:
            return False
        publish = getattr(self.host, "pinResult", None)
        result = self.session.pin(self.document, step_id, port or None, self._source_root())
        if result is None or not callable(publish):
            self.show_status("Run the step first: nothing to pin yet.")
            return False
        publish(result, f"{self.document.name}:{step_id}{'.' + port if port else ''}")
        self.show_status(f"Pinned '{step_id}' to the results list.")
        return True

    def set_write_files(self, on: bool, *, folder=None, overwrite=None) -> bool:
        """Turn *Write files* on (asking once for the folder) or off."""
        if on and (self._out_dir is None or folder is not None):
            if folder is None:
                folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Folder for the files Save steps write",
                                                                    str(self._folder))
            if not folder:
                self._actions["write_files"].blockSignals(True)
                self._actions["write_files"].setChecked(False)
                self._actions["write_files"].blockSignals(False)
                return False
            if overwrite is None:
                overwrite = self._ask_overwrite(Path(folder))
                if overwrite is None:
                    self._actions["write_files"].blockSignals(True)
                    self._actions["write_files"].setChecked(False)
                    self._actions["write_files"].blockSignals(False)
                    return False
            self._out_dir, self._overwrite = Path(folder), bool(overwrite)
        self._write_files = bool(on)
        self._actions["write_files"].blockSignals(True)
        self._actions["write_files"].setChecked(self._write_files)
        self._actions["write_files"].blockSignals(False)
        self.refresh(inspector=False)
        self.show_status(f"Save steps write to {self._out_dir}." if on else
                         "Write files is off: runs keep results in memory and Saves say what they would write.")
        return True

    def _ask_overwrite(self, folder: Path):
        try:
            occupied = any(folder.iterdir())
        except OSError:
            occupied = False
        if not occupied:
            return False
        answer = QtWidgets.QMessageBox.question(
            self, "The folder is not empty", f"{folder} already holds files. Overwrite files of the same name?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.No)
        if answer == QtWidgets.QMessageBox.Cancel:
            return None
        return answer == QtWidgets.QMessageBox.Yes

    def run_and_publish(self) -> bool:
        """The batch run: the whole workflow, every result into the list."""
        if self._running:
            self.show_status("A workflow is already running.")
            return False
        if self.validate_now():
            return False
        self.sig_run_requested.emit(self._snapshot(), self._source_root())
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
            self._queued = None
            self.sig_cancel_requested.emit()

    def set_running(self, running: bool) -> None:
        self._running = bool(running)
        for name in ("run", "run_to", "rerun", "run_publish", "run_on_results"):
            self._actions[name].setEnabled(not running or name in ("run", "run_to"))
        self._actions["cancel"].setEnabled(bool(running))
        if running:
            self._set_chip("Running…", "chipBusy")
            return
        self._progress.hide()
        if self._active_plan is not None:
            # The worker ended without saying how (it was torn down): what it
            # made so far stays, the rest is not run.
            self._active_plan = None
            self.session.finish(None, "stopped", document=self.document, keep=self.selected_step_ids())
            self.refresh(inspector=False)
        self._update_chip()
        if self._queued is not None:
            (targets,), self._queued = self._queued, None
            QtCore.QTimer.singleShot(0, lambda: self._start_run(targets))

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
        self.session.registry = self.catalog.registry
        self.palette.set_catalog(self.catalog, self.preferences.recent_steps())
        self.refresh(inspector=True)
        self.show_status(f"{len(self.catalog.processors)} processors, {len(self.catalog.reconstructors)} reconstructors.")


def _seconds(value: float) -> str:
    value = float(value)
    return f"{value * 1000:.0f} ms" if value < 1 else f"{value:.1f} s"


def _has_python(document) -> bool:
    return any(getattr(step, "processor", "") == "python" for step in document.steps)


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
