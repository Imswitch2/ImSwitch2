"""First-time use of the workflow editor: the start page's four ways in, list
mode as cards, auto-run (and when it waits), frames, recipes from snippets,
and what a port says after a run."""

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtCore, QtGui, QtWidgets  # noqa: E402

from imswitch.improcess.model.workfloweditor import WorkflowDocument, build_catalog  # noqa: E402
from imswitch.improcess.workflows import Workflow, bootstrap_registry  # noqa: E402

_EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "improcess_workflows"
_SNIPPETS = Path(__file__).resolve().parents[2] / "_data" / "user_defaults" / "improcess_snippets"
sys.path.insert(0, str(_EXAMPLES))

from _synthetic import write_synthetic_recording  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def rig(qapp, monkeypatch, tmp_path):
    from imswitch.improcess.controller.WorkflowController import WorkflowController
    from imswitch.improcess.model import folder_preferences, snippets
    from imswitch.improcess.model.workfloweditor import folders

    monkeypatch.setattr(folders, "workflows_directory", lambda create=True: str(tmp_path / "workflows"))
    monkeypatch.setattr(folder_preferences, "options_file_path", lambda: str(tmp_path / "options.json"))
    monkeypatch.setattr(snippets, "snippets_directory", lambda create=True: str(_SNIPPETS))
    loaded = []
    comm = SimpleNamespace(sigResultProduced=SimpleNamespace(emit=lambda r, n: loaded.append((n, r))),
                           getAllResults=lambda: [r for _n, r in loaded], getSelectedResults=lambda: list(loaded))
    controller = WorkflowController(
        comm, SimpleNamespace(showStatusMessage=lambda m, timeout_ms=0: None), SimpleNamespace(getActiveResult=lambda: None),
        registry_factory=lambda: bootstrap_registry(user_plugins=False),
    )
    editor = controller.openEditor()
    editor.resize(1500, 900)
    editor.show()
    qapp.processEvents()
    recording = str(write_synthetic_recording(tmp_path / "cells.h5", frames=2, axes="CYX"))
    yield SimpleNamespace(controller=controller, editor=editor, recording=recording, tmp=tmp_path, app=qapp)
    controller.cancelRun(5000)
    if controller._editor is not None:
        editor.document.dirty = False
        editor.close()
    qapp.processEvents()


def _wait(rig, timeout=60.0):
    deadline = time.time() + timeout
    while rig.controller._thread is not None and time.time() < deadline:
        rig.app.processEvents()
        time.sleep(0.005)
    for _ in range(3):
        rig.app.processEvents()


def _wait_for(rig, condition, timeout=20.0):
    deadline = time.time() + timeout
    while not condition() and time.time() < deadline:
        rig.app.processEvents()
        time.sleep(0.005)
    assert condition()


# -- the start page ------------------------------------------------------------------

def test_the_editor_opens_on_the_start_page_with_four_ways_in(rig):
    editor = rig.editor
    page = editor.start_page
    assert editor.centre.currentWidget() is page
    assert not page.recording_card.isEnabled() and not page.result_card.isEnabled()
    assert "Open a recording" in page.recording_card.text.text()
    names = [page.recipes.item(i).data(QtCore.Qt.UserRole).name for i in range(page.recipes.count())]
    assert "signal_trace" in names and "crosstalk" in names
    page.sigBlank.emit()
    assert editor.centre.currentWidget() is editor.view and editor.document.steps == []


def test_a_recipe_becomes_a_workflow_that_runs(rig):
    editor = rig.editor
    page = editor.start_page
    recipe = next(page.recipes.item(i).data(QtCore.Qt.UserRole) for i in range(page.recipes.count())
                  if page.recipes.item(i).data(QtCore.Qt.UserRole).name == "despeckle")
    page.sigRecipe.emit(recipe)
    assert [step.kind for step in editor.document.steps] == ["source", "reconstruct", "process", "save"]
    assert editor.selected_step_id() == "script" and editor.inspector.python_editor is not None
    assert editor.issues() == []
    editor.bind_source("raw", rig.recording)
    assert editor.run()
    _wait(rig)
    assert editor.scene.node("script").run.status == "done"


def test_the_open_recording_starts_a_workflow_bound_to_it(rig):
    editor = rig.editor
    rig.controller._onCurrentData(SimpleNamespace(dataPath=rig.recording, datasetName=None))
    editor.show_start_page()
    page = editor.start_page
    assert page.recording_card.isEnabled() and "cells.h5" in page.recording_card.text.text()
    page.sigFromRecording.emit()
    assert [step.id for step in editor.document.steps] == ["raw", "rec"]
    assert editor.session.source_bindings == {"raw": rig.recording}
    assert editor.document.step("raw").source.path is None
    assert editor.selected_step_id() == "rec"


def test_recent_and_folder_workflows_are_listed(rig):
    editor = rig.editor
    folder = Path(editor._folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "mine.yaml"
    editor.open_file(str(_EXAMPLES / "split_process_merge_diamond.yaml"))
    assert editor.save(str(target))
    editor.show_start_page()
    page = editor.start_page
    assert page.recent.item(0).data(QtCore.Qt.UserRole) == str(target.resolve())
    assert [page.folder.item(i).text() for i in range(page.folder.count())] == ["mine.yaml"]
    page.sigOpenFile.emit(str(target))
    assert editor.centre.currentWidget() is editor.view and editor.document.name == "split-process-merge"


def test_recipes_make_exactly_the_shipped_example_workflows():
    from imswitch.improcess.model.snippets import parse_snippet
    from imswitch.improcess.model.workfloweditor.recipes import recipe_workflow

    for path in sorted(_SNIPPETS.glob("*.py")):
        code, ports = parse_snippet(path.read_text(encoding="utf-8"))
        made = recipe_workflow(path.stem, code, ports)
        shipped = Workflow.load(_EXAMPLES / f"python_step_{path.stem}.yaml")
        assert made.to_dict() == shipped.to_dict(), path.stem
    with pytest.raises(ValueError, match="curves"):
        recipe_workflow("bad", "# curves: nope\nout = data\n", "out")


# -- list mode ------------------------------------------------------------------------

def test_list_mode_shows_cards_in_run_order_with_lanes(rig):
    from imswitch.improcess.view.workfloweditor.listview import lanes

    editor = rig.editor
    editor.open_file(str(_EXAMPLES / "split_process_merge_diamond.yaml"))
    editor.set_mode("list")
    cards = editor.list_view.cards
    assert list(cards) == ["raw", "rec", "split", "smooth", "sharp", "merge", "out"]
    assert cards["split"].pos().y() < cards["smooth"].pos().y() < cards["sharp"].pos().y()
    assert lanes(editor.document) == {"raw": 0, "rec": 0, "split": 0, "smooth": 0, "sharp": 1, "merge": 0, "out": 0}
    editor.list_view.select_step("sharp")
    assert editor.inspector.step_id == "sharp"
    editor.bind_source("raw", rig.recording)
    editor.run()
    _wait(rig)
    assert editor.list_view.cards["merge"].run.status == "done"
    assert editor.list_view.cards["merge"].run.thumbnail is not None
    editor.set_mode("graph")
    assert editor.scene.selected_step_ids() == ["sharp"]


# -- auto-run ------------------------------------------------------------------------------

def _diamond_ran(rig):
    editor = rig.editor
    editor.open_file(str(_EXAMPLES / "split_process_merge_diamond.yaml"))
    editor.bind_source("raw", rig.recording)
    editor.run()
    _wait(rig)
    return editor


def test_auto_run_reruns_what_an_edit_outdated(rig):
    editor = _diamond_ran(rig)
    editor._actions["auto_run"].setChecked(True)
    editor.select_step("merge")
    editor.edit("radius", lambda: editor.document.set_param("smooth", "radius", 3.0))
    assert editor.scene.node("smooth").run.status == "outdated"
    _wait_for(rig, lambda: rig.controller._thread is not None or editor.scene.node("merge").run.status == "done")
    _wait(rig)
    assert editor.scene.node("smooth").run.status == editor.scene.node("merge").run.status == "done"
    assert editor.preferences.get("autoRun") is True


def test_auto_run_waits_for_one_run_before_running_an_opened_files_python(rig):
    editor = rig.editor
    editor.open_file(str(_EXAMPLES / "python_step_despeckle.yaml"))
    editor.bind_source("raw", rig.recording)
    editor._actions["auto_run"].blockSignals(True)
    editor._actions["auto_run"].setChecked(True)
    editor._actions["auto_run"].blockSignals(False)
    editor.select_step("script")
    assert editor._auto_run() is False
    assert "waits for one Run" in editor.statusBar().currentMessage()
    assert editor.run()                       # an explicit Run trusts the file's code
    _wait(rig)
    editor.edit("ports", lambda: editor.document.set_param("script", "code", editor.document.effective_params(
        "script")["code"] + "\n# changed\n"))
    rig.editor._auto_timer.stop()
    assert editor._auto_run() is True, editor.statusBar().currentMessage()
    _wait(rig)
    assert editor.scene.node("script").run.status == "done"


def test_auto_run_leaves_slow_steps_to_run_to_here(rig):
    editor = _diamond_ran(rig)
    entry = editor.session.states(editor.document)["smooth"].entry
    entry.seconds = 30.0
    editor._actions["auto_run"].blockSignals(True)
    editor._actions["auto_run"].setChecked(True)
    editor._actions["auto_run"].blockSignals(False)
    editor.edit("radius", lambda: editor.document.set_param("smooth", "radius", 3.0))
    editor._auto_timer.stop()
    assert editor._auto_run() is False
    assert "F6" in editor.statusBar().currentMessage()


# -- frames -----------------------------------------------------------------------------

def test_a_frame_groups_steps_moves_them_and_is_kept_in_the_file(rig, tmp_path):
    editor = rig.editor
    editor.open_file(str(_EXAMPLES / "split_process_merge_diamond.yaml"))
    editor.scene.select_steps(["smooth", "sharp"])
    assert editor.frame_selection("the two filters")
    frames = editor.document.frames
    assert len(frames) == 1 and frames[0]["title"] == "the two filters"
    frame = editor.scene.frames[0]
    smooth_before = editor.scene.node("smooth").pos()
    title = frame.mapToScene(QtCore.QPointF(40, 10))
    view = editor.view

    def mouse(kind, pos, buttons):
        point = QtCore.QPointF(view.mapFromScene(pos))
        screen = QtCore.QPointF(view.viewport().mapToGlobal(point.toPoint()))
        button = QtCore.Qt.LeftButton if kind != QtCore.QEvent.MouseMove else QtCore.Qt.NoButton
        QtWidgets.QApplication.sendEvent(view.viewport(), QtGui.QMouseEvent(
            kind, point, point, screen, button, buttons, QtCore.Qt.NoModifier))

    editor.scene.select_steps([])
    mouse(QtCore.QEvent.MouseButtonPress, title, QtCore.Qt.LeftButton)
    for step in range(1, 5):
        mouse(QtCore.QEvent.MouseMove, title + QtCore.QPointF(0, 30 * step), QtCore.Qt.LeftButton)
    mouse(QtCore.QEvent.MouseButtonRelease, title + QtCore.QPointF(0, 120), QtCore.Qt.NoButton)
    for _ in range(3):
        rig.app.processEvents()
    moved = editor.document.frames[0]["rect"][1] - frames[0]["rect"][1]
    assert moved == pytest.approx(120, abs=2)
    assert editor.document.positions["smooth"][1] == pytest.approx(smooth_before.y() + moved, abs=1)
    assert editor.rename_frame(0, "smoothing")
    target = tmp_path / "framed.yaml"
    editor.save(str(target))
    assert WorkflowDocument.load(target, editor.catalog).frames[0]["title"] == "smoothing"
    editor.scene.frames[0].setSelected(True)
    editor.delete_selected()
    assert editor.document.frames == []
    assert "frames" not in editor.document.to_yaml()
    editor.undo()
    assert editor.document.frames[0]["title"] == "smoothing"


def test_frames_are_checked_like_any_document_edit():
    catalog = build_catalog(bootstrap_registry(user_plugins=False))
    document = WorkflowDocument(Workflow("w"), catalog=catalog)
    from imswitch.improcess.model.workfloweditor import DocumentError

    with pytest.raises(DocumentError):
        document.add_frame("flat", [0, 0, 0, 10])
    assert document.add_frame("a", [0, 0, 100, 50], "#ff0000") == 0
    document.set_frame(0, rect=[5, 5, 10, 10])
    assert document.frames[0]["rect"] == [5.0, 5.0, 40.0, 40.0]
    with pytest.raises(DocumentError):
        document.remove_frame(3)
    document.workflow.metadata["editor"]["frames"].append({"title": "broken"})
    assert len(document.frames) == 1


# -- small things that make it readable -------------------------------------------------

def test_after_a_run_a_port_says_what_it_carried(rig):
    editor = _diamond_ran(rig)
    out = editor.scene.node("split").outputs[0]
    assert "96 × 96" in out.toolTip()
    into = editor.scene.node("smooth").inputs[0]
    assert "Last run: 96 × 96" in into.toolTip()


def test_canvas_keys_belong_to_the_canvas_and_the_shortcuts_are_listed(rig):
    editor = rig.editor
    delete = editor._actions["delete"]
    assert delete.shortcutContext() == QtCore.Qt.WidgetWithChildrenShortcut
    assert delete in editor.view.actions() and delete not in editor.actions()
    assert editor._actions["undo"] in editor.actions()
    from imswitch.improcess.view.workfloweditor.editor import _SHORTCUTS

    assert any("Ctrl+G" in keys for keys, _what in _SHORTCUTS)
