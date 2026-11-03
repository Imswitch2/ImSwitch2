"""Workflow schema 2 in the editor: the document's control and bypass rules
(plan 8.1), runs planned through ``prepare_run`` with bypass-aware
fingerprints (8.2), and what the window shows (8.3), offscreen.

Plan: ``docs/design/plans/improcess-workflow-schema-2.md``.
"""

import copy
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtWidgets  # noqa: E402

from imswitch.improcess.model.workfloweditor import DocumentError, WorkflowDocument, build_catalog  # noqa: E402
from imswitch.improcess.model.workfloweditor.nodes import kinds_of, node_infos  # noqa: E402
from imswitch.improcess.model.workfloweditor.preferences import EditorPreferences  # noqa: E402
from imswitch.improcess.model.workfloweditor.session import RunSession  # noqa: E402
from imswitch.improcess.workflows import Control, Process, Reconstruct, Source, Workflow  # noqa: E402
from imswitch.improcess.workflows import bootstrap_registry  # noqa: E402
from imswitch.improcess.workflows.steps import Save  # noqa: E402

_EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "improcess_workflows"
sys.path.insert(0, str(_EXAMPLES))

from _synthetic import write_synthetic_recording  # noqa: E402


@pytest.fixture(scope="module")
def registry():
    return bootstrap_registry(user_plugins=False)


@pytest.fixture(scope="module")
def catalog(registry):
    return build_catalog(registry)


def _diamond(catalog, *, source=None, controls=(), bypass=()):
    steps = [
        Source("raw", path=source),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("split", "stack-split", {"axis": "C"}, inputs=["rec"]),
        Process("smooth", "filter", {"method": "gaussian", "radius": 2.0}, inputs=["split.C0"]),
        Process("sharp", "filter", {"method": "median", "radius": 2.0}, inputs=["split.C1"]),
        Process("merge", "channel-merge", inputs=["smooth", "sharp"]),
        Save("out", input="merge", fmt="hdf5", path_template="{out_dir}/{source_stem}_diamond{ext}"),
    ]
    for step in steps:
        if step.id in bypass:
            step.bypass = True
    return WorkflowDocument(Workflow("diamond", steps, controls=[copy.deepcopy(c) for c in controls]),
                            catalog=catalog)


def _edit(document, label, action):
    with document.history.edit(label):
        action()


# -- the document: controls (plan 8.1) --------------------------------------------------------------

def test_exposing_writes_the_value_explicitly_and_undoes(catalog):
    document = _diamond(catalog)
    document.step("smooth").params.pop("radius")                 # at its default, so not written
    _edit(document, "Expose", lambda: document.add_control("radius", "smooth.radius"))
    assert document.step("smooth").params["radius"] == 2.0      # explicit, though it is the default
    assert document.control_of("smooth", "radius").name == "radius"
    assert document.to_dict()["schema"] == 2
    assert document.history.undo() == "Expose"
    assert document.controls == [] and "radius" not in document.step("smooth").params
    assert document.to_dict()["schema"] == 1


@pytest.mark.parametrize("name, message", [
    ("1r", "bad control name '1r'"),
    ("smooth", "'smooth' is the id of a step"),
])
def test_a_control_name_is_checked(catalog, name, message):
    document = _diamond(catalog)
    with pytest.raises(DocumentError, match=message):
        document.add_control(name, "smooth.radius")
    assert document.controls == []


@pytest.mark.parametrize("target, message", [
    ("smooth.nope", "'filter' has no parameter 'nope'"),
    ("out.fmt", "is a save step"),
    ("smooth", "bad target"),
])
def test_a_target_the_rules_refuse_leaves_the_document_unchanged(catalog, target, message):
    document = _diamond(catalog)
    before = document.snapshot()
    with pytest.raises(DocumentError, match=message):
        document.add_control("x", target)
    assert document.snapshot() == before


def test_a_new_target_adopts_the_controls_value_or_is_refused(catalog):
    document = _diamond(catalog)
    document.add_control("radius", "smooth.radius")
    document.set_control_value("radius", 3.0)
    document.step("sharp").params["radius"] = 1.0
    document.add_target("radius", "sharp.radius")
    assert document.step("sharp").params["radius"] == 3.0       # adopted
    with pytest.raises(DocumentError, match="its targets are"):
        document.add_target("radius", "smooth.method")          # a select cannot share a float's value
    assert "smooth.method" not in document.control("radius").targets
    # A value outside the new target's bounds is refused, with the reason.
    document.workflow.steps.insert(5, Process("bg", "subtract-background", {"radius": 50.0}, inputs=["smooth"]))
    document.set_control_value("radius", 0.5)
    with pytest.raises(DocumentError, match="0.5 is below 1"):
        document.add_target("radius", "bg.radius")
    assert document.step("bg").params["radius"] == 50.0 and document.control("radius").targets == [
        "smooth.radius", "sharp.radius"]


def test_editing_a_target_sets_the_control_everywhere(catalog):
    document = _diamond(catalog, controls=[Control("radius", ["smooth.radius", "sharp.radius"])])
    _edit(document, "Change radius", lambda: document.set_param("sharp", "radius", 3))
    assert document.step("smooth").params["radius"] == document.step("sharp").params["radius"] == 3.0
    assert isinstance(document.step("smooth").params["radius"], float)   # canonical: 3 and 3.0 are one value
    with pytest.raises(DocumentError, match="above 1000"):
        document.set_param("smooth", "radius", 5000.0)
    assert document.step("smooth").params["radius"] == 3.0


def test_reset_writes_the_shared_default_and_is_refused_without_one(catalog):
    document = _diamond(catalog, controls=[Control("radius", ["smooth.radius", "sharp.radius"])])
    document.set_control_value("radius", 5.0)
    assert document.control_reset("radius") == (2.0, None)
    document.clear_param("smooth", "radius")                      # the row's reset is the control's
    assert document.step("smooth").params["radius"] == document.step("sharp").params["radius"] == 2.0
    # Different defaults: no reset, with the reason.
    document.workflow.steps.insert(5, Process("bg", "subtract-background", {"radius": 5.0}, inputs=["smooth"]))
    document.add_target("radius", "bg.radius")
    value, problem = document.control_reset("radius")
    assert value is None and "smooth.radius defaults to 2.0" in problem and "bg.radius defaults to 50.0" in problem
    assert problem.endswith("set a value instead")
    with pytest.raises(DocumentError, match="set a value instead"):
        document.reset_control("radius")


def test_bulk_edits_keep_targets_unless_they_set_them(catalog):
    document = _diamond(catalog, controls=[Control("radius", ["smooth.radius", "sharp.radius"])])
    document.set_control_value("radius", 3.0)
    document.set_params("smooth", {"method": "mean"})            # Reset all: radius is not in the call
    assert document.step("smooth").params == {"method": "mean", "radius": 3.0}
    document.set_params("smooth", {"method": "mean", "radius": 4.0})
    assert document.step("sharp").params["radius"] == 4.0
    both = _diamond(catalog, controls=[Control("pair", ["smooth.radius", "smooth.amount"])])
    with pytest.raises(DocumentError, match="different values"):
        both.set_params("smooth", {"radius": 3.0, "amount": 4.0})


def test_minimize_keeps_target_values(catalog):
    document = _diamond(catalog, controls=[Control("radius", ["smooth.radius"])])
    document.step("sharp").params["radius"] = 2.0                 # the default, not a target: dropped
    document.minimize_params()
    assert document.step("smooth").params["radius"] == 2.0 and "radius" not in document.step("sharp").params


def test_rename_delete_and_paste_keep_targets_right(catalog):
    document = _diamond(catalog, controls=[Control("radius", ["smooth.radius", "sharp.radius"])])
    document.rename_step("sharp", "crisp")
    assert document.control("radius").targets == ["smooth.radius", "crisp.radius"]
    pasted = document.paste(document.fragment(["smooth"]))
    assert pasted == ["smooth2"] and document.control_of("smooth2", "radius") is None
    document.delete_step("crisp")
    assert document.control("radius").targets == ["smooth.radius"]
    document.delete_step("smooth")
    assert document.controls == []                               # a control left without targets goes


def test_rename_and_stop_exposing(catalog):
    document = _diamond(catalog, controls=[Control("radius", ["smooth.radius", "sharp.radius"])])
    document.rename_control("radius", "blur")
    assert document.exposed("smooth") == {"radius": "blur"}
    with pytest.raises(DocumentError, match="id of a step"):
        document.rename_control("blur", "merge")
    document.remove_target("blur", "sharp.radius")
    document.remove_control("blur")
    assert document.controls == [] and document.step("smooth").params["radius"] == 2.0


# -- the document: bypass ------------------------------------------------------------------------------

def test_what_may_be_bypassed(catalog):
    document = _diamond(catalog)
    assert document.bypass_problem("smooth") is None and document.bypass_problem("out") is None
    assert "reads 2 inputs" in document.bypass_problem("merge")
    assert "'smooth' reads its port 'C0'" in document.bypass_problem("split")
    assert "only processing and save steps" in document.bypass_problem("rec")
    with pytest.raises(DocumentError, match="cannot bypass 'merge'"):
        document.set_bypass("merge", True)


def test_switching_a_bypass_off_adopts_the_controls_value(catalog):
    document = _diamond(catalog, controls=[Control("radius", ["smooth.radius", "sharp.radius"])], bypass=("smooth",))
    document.set_control_value("radius", 3.0)
    document.step("smooth").params["radius"] = 9.0                # an inactive target may differ
    assert document.issues() == []
    _edit(document, "Stop bypassing", lambda: document.set_bypass("smooth", False))
    assert document.step("smooth").params["radius"] == 3.0
    assert document.history.undo() and document.step("smooth").bypass and document.step("smooth").params["radius"] == 9.0


def test_the_editors_own_checks_skip_bypassed_steps(catalog):
    # A bypassed step never runs: its parameters are not checked, and a
    # bypassed Save writes no file to collide with another's (plan 5.3).
    document = _diamond(catalog, bypass=("smooth",))
    document.step("smooth").params["radius"] = -5.0
    document.workflow.steps.append(Save("copy", input="merge", fmt="hdf5",
                                        path_template="{out_dir}/{source_stem}_diamond{ext}", bypass=True))
    assert document.issues() == []
    document.set_bypass("copy", False)
    assert [i.message for i in document.issues()] == ["writes the same file as save 'out'"]


def test_renaming_a_step_keeps_a_malformed_target_for_validation(catalog):
    document = _diamond(catalog, controls=[Control("r", ["smooth"])])
    document.rename_step("smooth", "soft")
    assert document.control("r").targets == ["smooth"]
    assert any("bad target 'smooth'" in issue.message for issue in document.issues())


def test_the_first_save_with_schema_2_says_so_once(catalog, tmp_path):
    document = _diamond(catalog)
    path = document.save(tmp_path / "d.yaml")
    assert not document.schema_raised
    document = WorkflowDocument.load(path, catalog=catalog)
    document.set_bypass("out", True)
    document.save()
    assert document.schema_raised
    document.save()
    assert not document.schema_raised


def test_boxes_show_bypass_and_exposed_parameters(catalog):
    document = _diamond(catalog, controls=[Control("radius", ["smooth.radius"])], bypass=("smooth", "out"))
    infos = node_infos(document)
    assert infos["smooth"].bypassed and infos["smooth"].passes == "split.C0"
    assert infos["out"].bypassed and infos["out"].passes == "merge"
    assert infos["smooth"].exposed == (("radius", "radius"),)
    assert any(line.endswith("⇄ radius") for line in infos["smooth"].summary)
    assert kinds_of(document)["smooth"] == kinds_of(document)["split"]


# -- runs (plan 8.2) -----------------------------------------------------------------------------------

def test_fingerprints_resolve_bypass_and_find_old_results_again(catalog, registry):
    session = RunSession(registry)
    document = _diamond(catalog)
    before = session.fingerprints(document)
    document.set_bypass("smooth", True)
    bypassed = session.fingerprints(document)
    assert bypassed["merge"] != before["merge"] and bypassed["sharp"] == before["sharp"]
    # 'merge' as if it read split.C0 straight away:
    wired = _diamond(catalog)
    wired.step("merge").inputs[0] = wired.step("smooth").inputs[0]
    assert session.fingerprints(wired)["merge"] == bypassed["merge"]
    document.set_bypass("smooth", False)
    assert session.fingerprints(document) == before


def test_setting_a_control_outdates_exactly_its_targets_and_what_follows(catalog, registry):
    session = RunSession(registry)
    document = _diamond(catalog, controls=[Control("radius", ["smooth.radius"])])
    before = session.fingerprints(document)
    document.set_control_value("radius", 4)
    after = session.fingerprints(document)
    assert {sid for sid in before if before[sid] != after[sid]} == {"smooth", "merge", "out"}
    document.set_control_value("radius", 4.0)
    assert session.fingerprints(document) == after                # 4 and 4.0 are one run


def test_a_partial_run_is_prepared_without_the_control_definitions(catalog, registry, tmp_path):
    recording = str(write_synthetic_recording(tmp_path / "cells.h5", frames=2, axes="CYX"))
    document = _diamond(catalog, source=recording, controls=[Control("radius", ["smooth.radius", "sharp.radius"])])
    session = RunSession(registry)
    plan = session.plan(document, ["sharp"])
    assert plan.workflow.controls == [] and [s.id for s in plan.workflow.steps] == ["raw", "rec", "split", "sharp"]
    # A control entirely outside the run cannot make it invalid either.
    assert [s.id for s in session.plan(document, ["split"]).workflow.steps] == ["raw", "rec", "split"]
    # A bypassed step asked for runs what it passes through.
    document.set_bypass("smooth", True)
    plan = session.plan(document, ["smooth"])
    assert [s.id for s in plan.workflow.steps] == ["raw", "rec", "split"] and plan.targets == ["split"]
    plan = session.plan(document)
    assert "smooth" not in {s.id for s in plan.workflow.steps}
    assert [str(r) for r in plan.workflow.step("merge").inputs] == ["split.C0", "sharp"]


def test_a_binding_on_a_bypassed_step_is_refused_when_planning(catalog, registry):
    from imswitch.improcess.workflows.steps import WorkflowError

    document = _diamond(catalog, bypass=("smooth",))
    session = RunSession(registry)
    session.result_bindings["smooth"] = SimpleNamespace(data=None, name="x", result_uid="u")
    from imswitch.imcommon.model.workflow import prepare as runner  # where is_result_binding lives now

    original = runner._is_result
    runner._is_result = lambda value: isinstance(value, SimpleNamespace) or original(value)
    try:
        with pytest.raises(WorkflowError, match="is bypassed and cannot be bound"):
            session.plan(document)
    finally:
        runner._is_result = original


# -- the window (plan 8.3), offscreen ------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def editor(qapp, catalog, tmp_path):
    from imswitch.improcess.view.workfloweditor import MainWindow

    window = MainWindow(catalog, start_folder=tmp_path, preferences=EditorPreferences(tmp_path / "options.json"))
    window.resize(1500, 900)
    window.show()
    qapp.processEvents()
    yield window
    window.document.dirty = False
    window.close()


def _load(editor, document):
    editor.load_document(document, confirm=False)
    editor.refresh()


def test_bypass_is_one_key_and_is_disabled_with_its_reason(editor, catalog, qapp):
    _load(editor, _diamond(catalog))
    action = editor._actions["bypass"]
    editor.select_step("merge")
    assert not action.isEnabled() and "reads 2 inputs" in action.toolTip()
    editor.select_step("smooth")
    assert action.isEnabled() and action.text() == "Bypass"
    assert editor.toggle_bypass()
    assert editor.document.step("smooth").bypass and editor.scene.node("smooth").info.bypassed
    assert action.text() == "Stop bypassing"
    banner = editor.inspector.stop_bypass_button
    assert banner.isVisible()
    labels = " ".join(label.text() for label in editor.inspector.widget().findChildren(QtWidgets.QLabel))
    assert "<b>merge</b> reads <b>split.C0</b> through this step unchanged" in labels
    banner.click()
    qapp.processEvents()
    assert not editor.document.step("smooth").bypass
    editor.undo()
    assert editor.document.step("smooth").bypass


def test_exposing_from_the_form_and_the_controls_panel(editor, catalog, qapp):
    _load(editor, _diamond(catalog))
    editor.select_step("smooth")
    form = editor.inspector.param_form
    assert form.context_actions("radius") == {"expose": "Expose as control…"}
    assert editor.inspector.expose_param("radius", "radius")
    form = editor.inspector.param_form
    assert form.exposed() == {"radius": ("radius", 2.0)}
    assert form.context_actions("radius") == {"unexpose": "Stop exposing (control 'radius')"}
    editor.select_step("sharp")
    assert editor.inspector.compatible_controls("radius") == ["radius"]
    assert editor.inspector.compatible_controls("method") == []
    assert editor.inspector.expose_param("radius", join="radius")
    assert editor.document.control("radius").targets == ["smooth.radius", "sharp.radius"]
    # The Controls panel: nothing selected.
    editor.scene.clearSelection()
    qapp.processEvents()
    assert editor.inspector.step_id is None
    panel = editor.inspector.control_forms["radius"]
    panel.set_value("radius", 3.0)
    assert editor.document.step("smooth").params["radius"] == editor.document.step("sharp").params["radius"] == 3.0
    buttons = editor.inspector.control_buttons["radius"]
    assert buttons["reset"].isEnabled()
    buttons["reset"].click()
    qapp.processEvents()
    assert editor.document.step("sharp").params["radius"] == 2.0
    assert editor.inspector.rename_control("radius", "blur")
    assert editor.document.exposed("smooth") == {"radius": "blur"}
    editor.inspector.control_buttons["blur"]["stop"].click()
    qapp.processEvents()
    assert editor.document.controls == []


def test_the_chip_and_the_bypassed_box_are_drawn(editor, catalog, qapp):
    from qtpy import QtGui

    _load(editor, _diamond(catalog, controls=[Control("radius", ["smooth.radius"])], bypass=("smooth", "out")))
    editor.select_step("smooth")
    form = editor.inspector.param_form
    assert form._params["radius"].title() == "Radius/sigma  ⇄ radius"
    assert form._params["radius"].opts["default"] == 2.0          # the control's reset
    for mode in ("graph", "list"):
        editor.set_mode(mode)
        qapp.processEvents()
        image = QtGui.QImage(800, 600, QtGui.QImage.Format_ARGB32)
        painter = QtGui.QPainter(image)
        (editor.scene if mode == "graph" else editor.list_view.scene()).render(painter)
        painter.end()
    node = editor.scene.node("smooth")
    assert "Bypassed: passes split.C0 on unchanged" in node.toolTip()


def test_the_controls_panel_says_when_a_control_has_no_effect(editor, catalog, qapp):
    _load(editor, _diamond(catalog, controls=[Control("method", ["smooth.method"])], bypass=("smooth",)))
    editor.scene.clearSelection()
    editor.refresh()
    texts = " ".join(label.text() for label in editor.inspector.widget().findChildren(QtWidgets.QLabel))
    assert "No effect while smooth is bypassed" in texts and "smooth.method</a> (bypassed)" in texts
    editor.inspector.control_forms["method"].set_value("method", "mean")   # still editable
    assert editor.document.step("smooth").params["method"] == "mean"


def test_reset_is_disabled_with_the_reason_when_defaults_differ(editor, catalog, qapp):
    document = _diamond(catalog, controls=[Control("radius", ["smooth.radius"])])
    document.workflow.steps.insert(5, Process("bg", "subtract-background", {"radius": 5.0}, inputs=["smooth"]))
    document.add_target("radius", "bg.radius")
    _load(editor, document)
    editor.scene.clearSelection()
    editor.refresh()
    reset = editor.inspector.control_buttons["radius"]["reset"]
    assert not reset.isEnabled() and "set a value instead" in reset.toolTip()


# -- a run in the window, through the controller -------------------------------------------------------

class _Signal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


@pytest.fixture
def rig(qapp, monkeypatch, tmp_path):
    from imswitch.improcess.controller.WorkflowController import WorkflowController
    from imswitch.improcess.model import folder_preferences
    from imswitch.improcess.model.workfloweditor import folders

    monkeypatch.setattr(folders, "workflows_directory", lambda create=True: str(tmp_path / "workflows"))
    monkeypatch.setattr(folder_preferences, "options_file_path", lambda: str(tmp_path / "options.json"))
    loaded = []
    comm = SimpleNamespace(
        sigResultProduced=SimpleNamespace(emit=lambda r, n: loaded.append((n, r))),
        sigWorkflowPreview=_Signal(),
        getAllResults=lambda: [r for _n, r in loaded],
        getSelectedResults=lambda: list(loaded),
    )
    controller = WorkflowController(
        comm, SimpleNamespace(showStatusMessage=lambda m, timeout_ms=0: None),
        SimpleNamespace(getActiveResult=lambda: None),
        registry_factory=lambda: bootstrap_registry(user_plugins=False),
    )
    editor = controller.openEditor()
    recording = str(write_synthetic_recording(tmp_path / "cells.h5", frames=2, axes="CYX"))
    yield SimpleNamespace(controller=controller, editor=editor, recording=recording, app=qapp, loaded=loaded)
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
    for _ in range(5):
        rig.app.processEvents()
    assert rig.controller._thread is None, "the run did not end"


def test_a_run_with_a_bypassed_step_and_switching_it_back_on(rig):
    editor = rig.editor
    _load(editor, _diamond(editor.catalog, source=rig.recording,
                           controls=[Control("radius", ["smooth.radius", "sharp.radius"])]))
    assert editor.run()
    _wait(rig)
    first = editor.session.result(editor.document, "merge")
    editor.select_step("smooth")
    assert editor.toggle_bypass()
    node = editor.scene.node("smooth")
    assert node.run.status == "bypassed" and "passes split.C0 on" in node.run.footer
    assert node.run.thumbnail is not None                         # what it passes on, dimmed
    assert editor.scene.node("merge").run.status == "outdated"
    assert editor.run()
    _wait(rig)
    assert "Ran 1 step " in editor.statusBar().currentMessage()   # merge; the rest from the cache
    assert editor.session.result(editor.document, "smooth") is editor.session.result(editor.document, "split", "C0")
    assert editor.toggle_bypass()                                  # back on: the old results are found
    assert editor.scene.node("merge").run.status == "done"
    assert editor.session.result(editor.document, "merge") is first
