"""The workflow editor window, offscreen: the graph shows exactly what the file
says, gestures on it make the edits a user means (and only valid ones), every
edit undoes, and runs go through the workflow controller."""

import os
from pathlib import Path
from types import SimpleNamespace

import h5py
import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402

from imswitch.improcess.model.workfloweditor import WorkflowDocument, build_catalog  # noqa: E402
from imswitch.improcess.model.workfloweditor.preferences import EditorPreferences  # noqa: E402
from imswitch.improcess.workflows import (  # noqa: E402
    Process,
    Reconstruct,
    Source,
    Workflow,
    bootstrap_registry,
    run,
)
from imswitch.improcess.workflows.steps import Ref, Save  # noqa: E402

_EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "improcess_workflows"
_EXAMPLE = _EXAMPLES / "split_process_merge_diamond.yaml"


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(scope="module")
def registry():
    return bootstrap_registry(user_plugins=False)


@pytest.fixture(scope="module")
def catalog(registry):
    return build_catalog(registry)


@pytest.fixture
def editor(qapp, catalog, tmp_path):
    from imswitch.improcess.view.workfloweditor import MainWindow

    window = MainWindow(catalog, start_folder=tmp_path, preferences=EditorPreferences(tmp_path / "options.json"))
    window.resize(1500, 900)
    window.show()
    qapp.processEvents()
    yield window
    window.document.dirty = False   # no "discard?" box offscreen
    window.close()


def _settle(qapp, rounds: int = 3) -> None:
    for _ in range(rounds):
        qapp.processEvents()


def _mouse(view, kind, scene_pos, *, buttons=QtCore.Qt.LeftButton, button=QtCore.Qt.LeftButton,
           modifiers=QtCore.Qt.NoModifier) -> None:
    point = QtCore.QPointF(view.mapFromScene(scene_pos))
    # The scene finds items by the screen position: give the real one, not
    # the cursor's (which the short constructor would use).
    screen = QtCore.QPointF(view.viewport().mapToGlobal(point.toPoint()))
    event = QtGui.QMouseEvent(kind, point, point, screen, button, buttons, modifiers)
    QtWidgets.QApplication.sendEvent(view.viewport(), event)


def _drag(qapp, view, start, end, *, modifiers=QtCore.Qt.NoModifier) -> None:
    """Press at ``start``, move in steps, release at ``end`` (scene coordinates)."""
    _mouse(view, QtCore.QEvent.MouseButtonPress, start, buttons=QtCore.Qt.LeftButton, modifiers=modifiers)
    for fraction in (0.25, 0.5, 0.75, 1.0):
        point = start + (end - start) * fraction
        _mouse(view, QtCore.QEvent.MouseMove, point, button=QtCore.Qt.NoButton, modifiers=modifiers)
    _mouse(view, QtCore.QEvent.MouseButtonRelease, end, buttons=QtCore.Qt.NoButton, modifiers=modifiers)
    _settle(qapp)


def _out(editor, step_id, port=None):
    node = editor.scene.node(step_id)
    ref = f"{step_id}.{port}" if port else step_id
    return node.output_port(ref).centre()


def _in(editor, step_id, row=0):
    return editor.scene.node(step_id).inputs[row].centre()


def _refs(editor, step_id):
    step = editor.document.step(step_id)
    return [str(step.input)] if isinstance(step, Save) else [str(ref) for ref in step.inputs]


def _h5(path, shape=(2, 8, 8)):
    with h5py.File(str(path), "w") as handle:
        handle.create_dataset("data", data=np.random.default_rng(0).random(shape).astype(np.float32))
    return path


def test_a_code_field_is_shown_and_keeps_the_text_as_typed(qapp):
    from imswitch.improcess.model.param_spec import ParamField
    from imswitch.improcess.model.workfloweditor.catalog import PluginEntry
    from imswitch.improcess.view.workfloweditor.paramform import ParamForm

    entry = PluginEntry(
        id="t.code", kind="processor", name="Code", category="Test", version="1",
        description="", origin="builtin",
        fields=(ParamField("code", "code", "pass\n", label="Code"),),
        extra_keys=(), accepts_any_key=False, min_inputs=1, max_inputs=1,
        accepted_kinds=("image",), supports_consolidation=False, accepts_roi=False,
        roi_modes=(), gui_only=None,
    )
    form = ParamForm()
    form.set_plugin(entry, {})
    assert form.keys() == ["code"]
    seen = []
    form.sigValueChanged.connect(lambda key, value: seen.append((key, value)))
    script = "ax = 0\n  group = (np.arange(6) // 3) % 2\noutputs = {}\n"
    form.set_value("code", script)
    assert form.values() == {"code": script}
    assert seen == [("code", script)]


# -- the graph shows the file ------------------------------------------------------

def test_every_shipped_example_opens_as_a_graph(editor):
    for path in sorted(_EXAMPLES.glob("*.yaml")):
        assert editor.open_file(str(path)), path.name
        document = editor.document
        assert set(editor.scene.nodes) == {step.id for step in document.steps}
        expected = sum(1 if isinstance(s, Save) else len(getattr(s, "inputs", []) or []) for s in document.steps)
        assert len(editor.scene.edges) == expected, path.name
        assert editor.issues() == [] and editor.chip.text().startswith("✓"), path.name
        assert not document.dirty


def test_the_diamond_reads_left_to_right_with_typed_ports(editor):
    editor.open_file(str(_EXAMPLE))
    nodes = editor.scene.nodes
    xs = {step_id: node.pos().x() for step_id, node in nodes.items()}
    assert xs["raw"] < xs["rec"] < xs["split"] < xs["smooth"] == xs["sharp"] < xs["merge"] < xs["out"]
    split = nodes["split"]
    assert [port.slot.label for port in split.outputs] == ["C0", "C1", "C2"]
    assert split.outputs[-1].hollow and split.outputs[0].kind == "image"
    assert nodes["raw"].outputs[0].kind == "recording"
    merge = nodes["merge"]
    assert len(merge.inputs) == 3 and merge.inputs[-1].slot.adds
    assert nodes["out"].info.title == "Save · HDF5" and nodes["smooth"].info.title == "Filter"
    assert "Radius/sigma  2" in nodes["smooth"].info.summary


# -- gestures --------------------------------------------------------------------------

def test_dragging_an_output_onto_an_input_connects(editor, qapp):
    editor.open_file(str(_EXAMPLE))
    _drag(qapp, editor.view, _out(editor, "split", "C2"), _in(editor, "merge", 2))
    assert _refs(editor, "merge") == ["smooth", "sharp", "split.C2"]
    assert editor.document.history.undo_label == "Connect"
    editor.undo()
    assert _refs(editor, "merge") == ["smooth", "sharp"]
    editor.redo()
    assert _refs(editor, "merge") == ["smooth", "sharp", "split.C2"]


def test_a_cable_on_a_single_input_replaces_it_and_shift_adds_it(editor, qapp):
    editor.open_file(str(_EXAMPLE))
    _drag(qapp, editor.view, _out(editor, "split", "C1"), _in(editor, "smooth"))
    assert _refs(editor, "smooth") == ["split.C1"]
    body = editor.scene.node("smooth").mapToScene(QtCore.QPointF(100, 70))
    _drag(qapp, editor.view, _out(editor, "split", "C0"), body, modifiers=QtCore.Qt.ShiftModifier)
    assert _refs(editor, "smooth") == ["split.C1", "split.C0"]
    assert [port.slot.label for port in editor.scene.node("smooth").outputs] == ["out", "out1"]
    assert editor.issues() == []


def test_a_cable_that_does_not_fit_is_refused_and_says_why(editor, qapp):
    editor.open_file(str(_EXAMPLE))
    messages = []
    editor.scene.sigMessage.connect(messages.append)
    _drag(qapp, editor.view, _out(editor, "raw"), _in(editor, "smooth"))
    assert _refs(editor, "smooth") == ["split.C0"]
    assert messages and "recording" in messages[-1]
    _drag(qapp, editor.view, _out(editor, "merge"), _in(editor, "split"))      # a loop
    assert _refs(editor, "split") == ["rec"]
    assert not editor.document.history.can_undo


def test_dragging_an_input_away_disconnects_but_a_save_keeps_its_input(editor, qapp):
    editor.open_file(str(_EXAMPLE))
    away = _in(editor, "merge", 1) + QtCore.QPointF(-30, 400)
    _drag(qapp, editor.view, _in(editor, "merge", 1), away)
    assert _refs(editor, "merge") == ["smooth"]
    messages = []
    editor.scene.sigMessage.connect(messages.append)
    _drag(qapp, editor.view, _in(editor, "out"), _in(editor, "out") + QtCore.QPointF(0, 400))
    _settle(qapp)
    assert _refs(editor, "out") == ["merge"] and "Save needs an input" in messages[-1]


def test_dragging_an_input_end_to_another_input_moves_the_cable(editor, qapp):
    editor.open_file(str(_EXAMPLE))
    _drag(qapp, editor.view, _in(editor, "merge", 1), _in(editor, "merge", 2))
    assert _refs(editor, "merge") == ["smooth", "sharp"]          # same producer, same box: still one cable
    editor.document.dirty = False
    editor.add_step("process", "filter", at=QtCore.QPointF(900, 400))
    new = editor.selected_step_id()
    _drag(qapp, editor.view, _in(editor, "merge", 1), _in(editor, new))
    assert _refs(editor, new) == ["sharp"] and _refs(editor, "merge") == ["smooth"]
    assert editor.document.history.undo_label == "Move a cable"


def test_a_cable_dropped_in_empty_space_offers_only_steps_that_take_it(editor, qapp):
    editor.open_file(str(_EXAMPLE))
    empty = editor.scene.node("out").pos() + QtCore.QPointF(0, 500)
    _drag(qapp, editor.view, _out(editor, "split", "C1"), empty)
    popup = editor._quick_add
    assert popup.isVisible()
    shown = popup.shown_candidates()
    assert all(c.step_kind in ("process", "save") for c in shown)
    assert {"filter", "subtract-background"} <= {c.plugin_id for c in shown}
    assert "table-to-localizations" not in {c.plugin_id for c in shown}
    popup.search.setText("classic spatial filter")
    assert [c.plugin_id for c in popup.shown_candidates()][:1] == ["filter"]
    popup.accept_current()
    _settle(qapp)
    new = editor.selected_step_id()
    assert editor.document.step(new).processor == "filter"
    assert _refs(editor, new) == ["split.C1"]
    x, y = editor.document.positions[new]
    assert abs(x + 105 - empty.x()) < 1 and abs(y + 22 - empty.y()) < 1


def test_a_cable_from_a_recording_offers_reconstructions(editor, qapp):
    editor.open_file(str(_EXAMPLE))
    _drag(qapp, editor.view, _out(editor, "raw"), _out(editor, "raw") + QtCore.QPointF(0, 400))
    shown = editor._quick_add.shown_candidates()
    assert shown and {c.step_kind for c in shown} == {"reconstruct"}
    editor._quick_add.close()


def test_a_fresh_step_dropped_on_a_cable_is_spliced_in(editor, qapp):
    editor.open_file(str(_EXAMPLE))
    editor.add_step("process", "filter", at=QtCore.QPointF(300, 700))
    new = editor.selected_step_id()
    node = editor.scene.node(new)
    grab = node.mapToScene(QtCore.QPointF(105, 22))
    edge = next(e for e in editor.scene.edges if e.target.step_id == "out")
    middle = edge.path().pointAtPercent(0.5)
    _drag(qapp, editor.view, grab, middle)
    assert _refs(editor, new) == ["merge"] and _refs(editor, "out") == [new]
    assert editor.issues() == []
    assert editor.document.history.undo_label == "Put on the cable"


def test_moving_a_box_stores_its_place_and_freezes_the_layout(editor, qapp):
    editor.open_file(str(_EXAMPLE))
    assert editor.document.positions == {}
    node = editor.scene.node("sharp")
    start = node.mapToScene(QtCore.QPointF(105, 22))
    before = node.pos().y()
    _drag(qapp, editor.view, start, start + QtCore.QPointF(0, 120))
    positions = editor.document.positions
    assert set(positions) == {step.id for step in editor.document.steps}
    assert positions["sharp"][1] == pytest.approx(before + 120, abs=2)
    assert editor.scene.node("sharp").pos().y() == pytest.approx(positions["sharp"][1])
    assert editor.document.history.undo_label == "Move"
    editor.undo()
    assert editor.document.positions == {}


def test_a_palette_drop_adds_the_step_where_it_lands(editor, qapp):
    from imswitch.improcess.view.workfloweditor.canvas import STEP_MIME

    editor.new_document()
    _settle(qapp)
    mime = QtCore.QMimeData()
    mime.setData(STEP_MIME, b"source\n")
    point = QtCore.QPoint(300, 300)
    where = editor.view.mapToScene(point)
    drop = QtGui.QDropEvent(QtCore.QPointF(point), QtCore.Qt.CopyAction, mime, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
    editor.view.dropEvent(drop)
    _settle(qapp)
    assert [step.kind for step in editor.document.steps] == ["source"]
    x, y = editor.document.positions["src1"]
    assert abs(x + 105 - where.x()) < 1 and abs(y + 22 - where.y()) < 1
    assert ("source", "") in editor.palette.all_choices()
    assert editor.preferences.recent_steps()[0] == ("source", "")


def test_deleting_a_box_takes_its_saves_and_undo_brings_both_back(editor, qapp):
    editor.open_file(str(_EXAMPLE))
    editor.select_step("merge")
    editor.view.setFocus()
    editor.delete_selected()
    assert not editor.document.has("merge") and not editor.document.has("out")
    editor.undo()
    assert editor.document.has("merge") and editor.document.has("out")
    edge = next(e for e in editor.scene.edges if e.target.step_id == "merge" and e.index == 1)
    editor.scene.select_steps([])
    edge.setSelected(True)
    editor.delete_selected()
    assert _refs(editor, "merge") == ["smooth"]


def test_duplicate_and_copy_paste_keep_their_own_cables(editor, qapp):
    editor.open_file(str(_EXAMPLE))
    editor.scene.select_steps(["smooth", "merge"])
    editor.duplicate_selected()
    assert set(editor.selected_step_ids()) == {"smooth2", "merge2"}
    assert _refs(editor, "merge2") == ["smooth2"] and _refs(editor, "smooth2") == []
    editor.scene.select_steps(["split"])
    assert editor.copy_selected()
    editor.paste()
    assert editor.document.has("split2") and editor.selected_step_ids() == ["split2"]


def test_tidy_up_lays_out_everything(editor, qapp):
    editor.open_file(str(_EXAMPLE))
    editor.tidy_up()
    assert set(editor.document.positions) == {step.id for step in editor.document.steps}


def test_a_workflow_is_built_from_an_empty_canvas_saved_and_reopened(editor, qapp, tmp_path):
    editor.new_document()
    editor.add_step("source", at=QtCore.QPointF(0, 0))
    _drag(qapp, editor.view, _out(editor, "src1"), _out(editor, "src1") + QtCore.QPointF(260, 0))
    editor._quick_add.search.setText("view only")
    editor._quick_add.accept_current()
    _settle(qapp)
    _drag(qapp, editor.view, _out(editor, "rec1"), _out(editor, "rec1") + QtCore.QPointF(260, 0))
    editor._quick_add.search.setText("filter")
    editor._quick_add.accept_current()
    _settle(qapp)
    _drag(qapp, editor.view, _out(editor, "proc1"), _out(editor, "proc1") + QtCore.QPointF(260, 0))
    editor._quick_add.search.setText("save")
    editor._quick_add.accept_current()
    _settle(qapp)
    kinds = [step.kind for step in editor.document.steps]
    assert kinds == ["source", "reconstruct", "process", "save"]
    assert editor.issues() == []
    target = tmp_path / "built.yaml"
    assert editor.save(str(target))
    reopened = WorkflowDocument.load(target, editor.catalog)
    assert [s.id for s in reopened.steps] == [s.id for s in editor.document.steps]
    assert set(reopened.positions) == {"src1", "rec1", "proc1", "save1"}
    for _ in range(4):
        editor.undo()
    assert editor.document.steps == []


# -- the inspector and the list ------------------------------------------------------------

def test_a_parameter_edit_writes_only_that_key_and_undoes_in_one_step(editor, qapp):
    editor.open_file(str(_EXAMPLE))
    editor.select_step("smooth")
    form = editor.inspector.param_form
    assert form.keys() == ["method", "radius", "amount"]
    form.set_value("amount", 0.9)
    form.set_value("amount", 0.7)
    assert editor.document.step("smooth").params == {"method": "gaussian", "radius": 2.0, "amount": 0.7}
    assert editor.inspector.param_form is form       # a parameter edit does not rebuild the inspector
    assert "Unsharp amount  0.7" in editor.scene.node("smooth").info.summary
    editor.undo()                                    # both changes of one field are one entry
    assert editor.document.step("smooth").params == {"method": "gaussian", "radius": 2.0}
    assert editor.document.dirty and editor.windowTitle().startswith("*")


def test_the_inspector_renames_and_rewires(editor, qapp):
    editor.open_file(str(_EXAMPLE))
    editor.select_step("smooth")
    editor.inspector.id_edit.setText("blur")
    editor.inspector.id_edit.editingFinished.emit()
    _settle(qapp)
    assert editor.document.has("blur") and _refs(editor, "merge") == ["blur", "sharp"]
    assert editor.selected_step_id() == "blur" and editor.inspector.step_id == "blur"
    editor.inspector._replace_input(0, "split.C1")
    assert _refs(editor, "blur") == ["split.C1"]


def test_a_python_steps_code_is_edited_in_the_inspector(editor, qapp):
    editor.open_file(str(_EXAMPLES / "python_step_signal_trace.yaml"))
    editor.select_step("script")
    code = editor.inspector.python_editor
    assert code is not None and "level" in code.text()
    code.setText("out = data.copy()\n")
    editor.inspector._commit_code()
    assert editor.document.step("script").params["code"] == "out = data.copy()\n"
    editor.inspector.ports_edit.setText("out")
    editor.inspector.ports_edit.editingFinished.emit()
    _settle(qapp)
    assert [port.slot.ref for port in editor.scene.node("script").outputs] == ["script"]


def test_graph_and_list_show_one_selection_and_the_mode_is_remembered(editor, qapp, tmp_path):
    editor.open_file(str(_EXAMPLE))
    editor.select_step("sharp")
    editor.set_mode("list")
    assert editor.list_view.current_step_id() == "sharp" and editor.inspector.step_id == "sharp"
    assert EditorPreferences(tmp_path / "options.json").mode == "list"
    editor.list_view.select_step("merge")
    editor.set_mode("graph")
    assert editor.scene.selected_step_ids() == ["merge"]


def test_an_issue_is_listed_on_the_chip_and_selects_the_step(editor, qapp):
    editor.open_file(str(_EXAMPLE))
    editor.edit("Break", lambda: editor.document.set_inputs("smooth", ["nowhere.C0"]))
    assert editor.chip.text().startswith("⚠")
    assert editor.scene.node("smooth").info.issues
    editor._fill_issue_menu()
    editor.chip.menu().actions()[0].trigger()
    assert editor.selected_step_id() == "smooth"
    assert editor.run() is False


def test_run_and_publish_emits_a_copy_of_the_workflow_and_its_folder(editor):
    editor.open_file(str(_EXAMPLE))
    got = []
    editor.sig_run_requested.connect(lambda workflow, root: got.append((workflow, root)))
    assert editor.run_and_publish()
    assert got and got[0][1] == str(_EXAMPLE.parent)
    assert got[0][0] is not editor.document.workflow
    assert got[0][0].to_dict() == editor.document.to_dict()
    editor.set_running(True)
    assert editor.run_and_publish() is False and not editor._actions["run_publish"].isEnabled()
    cancelled = []
    editor.sig_cancel_requested.connect(lambda: cancelled.append(True))
    editor.cancel_run()
    assert cancelled
    editor.set_running(False)
    assert editor._actions["run_publish"].isEnabled()


# -- through the controller ----------------------------------------------------------

def _controller(monkeypatch, tmp_path, loaded, active=None):
    from imswitch.improcess.controller.WorkflowController import WorkflowController
    from imswitch.improcess.model import folder_preferences
    from imswitch.improcess.model.workfloweditor import folders

    monkeypatch.setattr(folders, "workflows_directory", lambda create=True: str(tmp_path / "workflows"))
    monkeypatch.setattr(folder_preferences, "options_file_path", lambda: str(tmp_path / "options.json"))
    comm = SimpleNamespace(sigResultProduced=SimpleNamespace(emit=lambda r, n: loaded.append((n, r))),
                           getAllResults=lambda: [r for _n, r in loaded],
                           getSelectedResults=lambda: list(loaded))
    messages = []
    view = SimpleNamespace(showStatusMessage=lambda m, timeout_ms=6000: messages.append(m))
    recon = SimpleNamespace(getActiveResult=lambda: active)
    controller = WorkflowController(
        comm, view, recon, registry_factory=lambda: bootstrap_registry(user_plugins=False),
    )
    return controller, messages


def test_the_controller_opens_one_editor_and_runs_its_workflow(qapp, qtbot, monkeypatch, tmp_path):
    loaded = []
    controller, messages = _controller(monkeypatch, tmp_path, loaded)
    editor = controller.openEditor()
    assert editor is not None and controller.openEditor() is editor
    raw = _h5(tmp_path / "scan.h5")
    editor.load_document(WorkflowDocument(Workflow("made", [
        Source("raw", path=str(raw)),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("blur", "filter", {"radius": 1.5}, inputs=["rec"]),
    ]), catalog=editor.catalog), confirm=False)
    assert controller.runWorkflowObject(editor.document.workflow, None, out_dir=str(tmp_path / "out"), overwrite=True)
    assert not editor._actions["run_publish"].isEnabled()
    qtbot.waitUntil(lambda: controller._thread is None, timeout=60000)
    assert editor._actions["run_publish"].isEnabled()
    assert any(name.startswith("made:") for name, _r in loaded)
    assert any("done" in message for message in messages)
    editor.document.dirty = False
    editor.close()
    assert controller._editor is None


def test_the_current_result_opens_in_the_editor_minimized(qapp, monkeypatch, tmp_path, registry):
    raw = _h5(tmp_path / "scan.h5")
    workflow = Workflow("made", [
        Source("raw", path=str(raw)),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("blur", "filter", {"method": "gaussian", "radius": 1.5}, inputs=["rec"]),
    ])
    with run(workflow, registry=registry, out_dir=tmp_path) as report:
        result = report.result("blur")
        controller, messages = _controller(monkeypatch, tmp_path, [], active=result)
        editor = controller.editWorkflowOfResult()
        assert editor is not None
        steps = editor.document.steps
        assert [type(s).__name__ for s in steps] == ["Source", "Reconstruct", "Process", "Save"]
        assert steps[2].params == {"radius": 1.5}
        assert editor.document.dirty and editor.issues() == []
        assert set(editor.scene.nodes) == {s.id for s in steps}
        editor.document.dirty = False
        editor.close()
    controller, messages = _controller(monkeypatch, tmp_path, [], active=None)
    assert controller.editWorkflowOfResult() is None
    assert messages[-1] == "No result selected."


def test_the_main_view_exposes_the_editor_signals():
    from imswitch.improcess.view.ImProcessMainView import ImProcessMainView

    assert hasattr(ImProcessMainView, "sigOpenWorkflowEditor")
    assert hasattr(ImProcessMainView, "sigEditWorkflowOfResultRequested")


def test_the_editor_remembers_its_settings_in_the_options_file(tmp_path):
    path = tmp_path / "options.json"
    path.write_text('{"dataFolder": "/data", "workflowEditor": {"mode": "list"}}', encoding="utf-8")
    preferences = EditorPreferences(path)
    assert preferences.mode == "list"
    preferences.add_recent_step("process", "filter")
    preferences.add_recent_step("save", "")
    preferences.add_recent_step("process", "filter")
    assert preferences.recent_steps() == [("process", "filter"), ("save", "")]
    reread = EditorPreferences(path)
    assert reread.recent_steps() == [("process", "filter"), ("save", "")]
    assert '"dataFolder": "/data"' in path.read_text(encoding="utf-8")
    path.write_text("not json", encoding="utf-8")
    assert EditorPreferences(path).mode == "graph"


def test_undoing_a_code_edit_is_not_overwritten_by_the_code_box(editor, qapp):
    editor.open_file(str(_EXAMPLES / "python_step_signal_trace.yaml"))
    editor.select_step("script")
    original = editor.document.step("script").params["code"]
    editor.inspector.python_editor.setText("out = data\n")
    editor.inspector._commit_code()
    assert editor.document.step("script").params["code"] == "out = data\n"
    editor.undo()                     # rebuilds the inspector, which must not write its old text back
    assert editor.document.step("script").params["code"] == original
    assert editor.inspector.python_editor.text() == original
    assert editor.document.history.can_redo
    editor.redo()
    assert editor.document.step("script").params["code"] == "out = data\n"


# -- what the independent review found ------------------------------------------------------

def _line_edit(editor, text):
    return next(e for e in editor.inspector.widget().findChildren(QtWidgets.QLineEdit) if e.text() == text)


def _type(qapp, edit, text):
    edit.window().activateWindow()
    edit.setFocus()
    _settle(qapp)
    edit.selectAll()
    QtTest.QTest.keyClicks(edit, text)
    assert edit.isModified()


def test_typing_left_in_a_field_is_not_written_over_an_undo(editor, qapp):
    editor.open_file(str(_EXAMPLE))
    editor.edit("radius", lambda: editor.document.set_param("smooth", "radius", 4.0))
    editor.select_step("out")
    template = editor.document.step("out").path_template
    _type(qapp, _line_edit(editor, template), "typed_{source_stem}")
    editor.undo()                     # hides the field, which must not finish into the document
    _settle(qapp)
    assert editor.document.step("smooth").params.get("radius") == 2.0
    assert editor.document.step("out").path_template == template
    assert editor.document.history.can_redo
    editor.select_step("smooth")      # the rebuilt inspector works
    editor.inspector.param_form.set_value("amount", 0.9)
    assert editor.document.step("smooth").params["amount"] == 0.9


def test_an_undo_with_nothing_to_undo_leaves_the_inspector_working(editor, qapp):
    editor.open_file(str(_EXAMPLE))
    editor.select_step("smooth")
    editor.undo()
    editor.redo()
    editor.inspector.param_form.set_value("amount", 0.9)
    assert editor.document.step("smooth").params["amount"] == 0.9


def test_typing_is_written_to_its_own_step_when_the_program_selects_another(editor, qapp):
    editor.open_file(str(_EXAMPLES / "python_step_signal_trace.yaml"))
    editor.select_step("script")
    raw_before = editor.document.step("raw").to_dict()
    _type(qapp, editor.inspector.ports_edit, "level")
    editor.select_step("raw")         # by the program: no focus moved first
    _settle(qapp)
    assert editor.document.step("script").params["ports"] == "level"
    assert editor.document.step("raw").to_dict() == raw_before
    assert editor.inspector.step_id == "raw"


def test_typing_left_in_a_field_stays_with_its_document(editor, qapp):
    editor.open_file(str(_EXAMPLES / "python_step_signal_trace.yaml"))
    editor.select_step("script")
    _type(qapp, editor.inspector.ports_edit, "level")
    editor.inspector.python_editor.setText("out = data\n")
    editor.inspector._code_dirty = True      # as typing sets it
    editor.document.dirty = False
    assert editor.open_file(str(_EXAMPLES / "python_step_signal_trace.yaml"))
    _settle(qapp)
    params = editor.document.step("script").params
    assert params["ports"] != "level" and params["code"] != "out = data\n"
    assert not editor.document.dirty and not editor.document.history.can_undo


def test_a_frame_dragged_right_after_framing_carries_its_boxes(editor, qapp):
    editor.open_file(str(_EXAMPLE))
    editor.scene.select_steps(["smooth", "sharp"])
    assert editor.frame_selection("filters")
    _settle(qapp)
    assert set(editor.selected_step_ids()) == {"smooth", "sharp"}     # still selected, as after Ctrl+G
    frame = editor.scene.frames[0]
    before = {sid: editor.scene.node(sid).pos() for sid in ("smooth", "sharp", "merge")}
    grip = frame.mapToScene(QtCore.QPointF(40, frame.TITLE_H / 2))
    _drag(qapp, editor.view, grip, grip + QtCore.QPointF(120, 80))
    _settle(qapp)
    for sid in ("smooth", "sharp"):
        moved = editor.scene.node(sid).pos() - before[sid]
        assert (round(moved.x()), round(moved.y())) == (120, 80), sid
        assert editor.document.positions[sid] == pytest.approx(
            (before[sid].x() + 120, before[sid].y() + 80), abs=0.5)
    assert editor.scene.node("merge").pos() == before["merge"]


def test_a_cable_end_dropped_back_on_its_own_box_changes_nothing(editor, qapp):
    editor.open_file(str(_EXAMPLE))
    body = editor.scene.node("merge").mapToScene(QtCore.QPointF(100, 70))
    _drag(qapp, editor.view, _in(editor, "merge", 0), body)     # not moved to the free input
    assert _refs(editor, "merge") == ["smooth", "sharp"]
    assert not editor.document.history.can_undo


def test_runs_asked_for_during_a_run_are_merged_into_one(editor):
    editor._queue(["smooth"])
    editor._queue(["sharp", "smooth"])
    assert editor._queued == (["smooth", "sharp"],)
    editor._queue(None)               # everything
    editor._queue(["merge"])
    assert editor._queued == (None,)


def test_python_code_that_came_with_a_document_waits_for_one_run(editor, catalog):
    python = Workflow("w", [Source("raw"), Reconstruct("rec", "view-only", inputs=["raw"]),
                            Process("py", "python", {"code": "out = data\n"}, inputs=["rec"])])
    assert editor.load_document(WorkflowDocument(python, catalog=catalog), confirm=False)
    assert editor._python_trusted is False         # e.g. a result's history
    assert editor.load_document(WorkflowDocument(Workflow.from_dict(python.to_dict()), catalog=catalog),
                                confirm=False, trusted=True)
    assert editor._python_trusted is True          # e.g. a recipe from the user's own snippets
    assert editor.new_blank() and editor._python_trusted is True
