"""The workflow editor window, offscreen: it edits exactly what the file can
express, reports what is wrong, and runs through the workflow controller."""

import os
from pathlib import Path
from types import SimpleNamespace

import h5py
import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model.workfloweditor import WorkflowDocument, build_catalog  # noqa: E402
from imswitch.improcess.workflows import (  # noqa: E402
    Process,
    Reconstruct,
    Source,
    Workflow,
    bootstrap_registry,
    run,
)
from imswitch.improcess.workflows.steps import Ref  # noqa: E402

_EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "improcess_workflows" / "split_process_merge_diamond.yaml"


@pytest.fixture(scope="module")
def qapp():
    from qtpy import QtWidgets

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

    window = MainWindow(catalog, start_folder=tmp_path)
    yield window
    window.document.dirty = False   # no "discard?" box offscreen
    window.close()


def _h5(path, shape=(2, 8, 8)):
    with h5py.File(str(path), "w") as handle:
        handle.create_dataset("data", data=np.random.default_rng(0).random(shape).astype(np.float32))
    return path


# -- the window over a document --------------------------------------------------

def test_opening_a_file_lists_its_steps_and_no_issues(editor):
    assert editor.open_file(str(_EXAMPLE))
    assert [step.id for step in editor.document.steps] == ["raw", "rec", "split", "smooth", "sharp", "merge", "out"]
    assert editor.issues() == [] and editor._issues.count_issues() == 0
    assert editor._steps.topLevelItemCount() == 7
    assert not editor.document.dirty and "split" in editor.windowTitle()


def test_a_parameter_edit_writes_only_that_key(editor):
    editor.open_file(str(_EXAMPLE))
    editor.select_step("smooth")
    form = editor.step_form.param_form
    assert form.keys() == ["method", "radius", "amount"]
    form.set_value("method", "median")
    assert editor.document.step("smooth").params == {"method": "median", "radius": 2.0}
    form.set_value("amount", 0.9)
    form.set_value("amount", 0.6)                   # back to the default: dropped
    assert editor.document.step("smooth").params == {"method": "median", "radius": 2.0}
    assert editor.document.dirty and editor.windowTitle().startswith("*")
    assert editor.step_form.param_form is form      # a parameter edit does not rebuild the form
    assert editor._steps.topLevelItem(3).text(4) == "" and editor.selected_step_id() == "smooth"


def test_a_step_added_from_the_palette_follows_the_selection(editor):
    editor.open_file(str(_EXAMPLE))
    editor.select_step("rec")
    assert editor.add_step("process", "subtract-background")
    assert [step.id for step in editor.document.steps][:4] == ["raw", "rec", "proc1", "split"]
    assert editor.selected_step_id() == "proc1"
    assert editor.document.step("proc1").inputs == [Ref("rec")]
    editor.step_form.param_form.set_value("output_background", True)
    refs = [option.ref for option in editor.document.port_options(None)]
    assert "proc1.signal" in refs and "proc1.background" in refs
    assert editor.issues() == []


def test_the_palette_offers_every_runnable_plugin(editor, catalog):
    palette = editor._palette
    seen = set()

    def walk(item):
        for index in range(item.childCount()):
            child = item.child(index)
            seen.add((child.data(0, 0x0100), child.data(0, 0x0101)))
            walk(child)

    for index in range(palette.topLevelItemCount()):
        top = palette.topLevelItem(index)
        seen.add((top.data(0, 0x0100), top.data(0, 0x0101)))
        walk(top)
    assert ("source", "") in seen and ("save", "") in seen
    assert {("process", e.id) for e in catalog.processors} <= seen
    assert {("reconstruct", e.id) for e in catalog.reconstructors} <= seen


def test_a_broken_reference_is_an_issue_that_selects_the_step(editor):
    editor.open_file(str(_EXAMPLE))
    editor.document.set_inputs("smooth", ["nowhere.C0"])
    editor.validate_now()
    issues = editor.issues()
    assert issues and issues[0].step == "smooth"
    assert editor._issues.count_issues() == len(issues)
    editor._issues.sigIssueSelected.emit("smooth")
    assert editor.selected_step_id() == "smooth"
    assert editor.run() is False               # refused while issues remain


def test_moving_and_removing_go_through_the_document(editor):
    editor.open_file(str(_EXAMPLE))
    editor.select_step("smooth")
    editor.move_selected(-1)                    # before split, its input: refused
    assert [s.id for s in editor.document.steps][2:4] == ["split", "smooth"]
    editor.move_selected(+1)
    assert [s.id for s in editor.document.steps][3:5] == ["sharp", "smooth"]
    editor.select_step("sharp")
    editor.remove_selected()
    assert not editor.document.has("sharp")
    assert any(issue.step == "merge" for issue in editor.issues())


def test_save_and_reopen_round_trip(editor, tmp_path):
    editor.open_file(str(_EXAMPLE))
    editor.select_step("smooth")
    editor.step_form.param_form.set_value("method", "median")
    target = tmp_path / "edited.yaml"
    assert editor.save(str(target))
    assert not editor.document.dirty and editor.document.path == target
    assert [editor._files.item(i).text() for i in range(editor._files.count())] == ["edited.yaml"]
    reopened = WorkflowDocument.load(target, editor.catalog)
    assert reopened.step("smooth").params == {"method": "median", "radius": 2.0}
    assert reopened.issues() == []


def test_the_step_form_edits_sources_and_saves(editor, tmp_path):
    editor.open_file(str(_EXAMPLE))
    editor.select_step("raw")
    editor.document.set_source("raw", path=str(tmp_path / "scan.h5"))
    editor.select_step("out")
    editor.document.set_save("out", fmt="zarr", path_template="{out_dir}/{name}{ext}")
    editor.validate_now()
    assert editor.issues() == []
    assert editor._steps.topLevelItem(6).text(2) == "zarr"
    assert editor._steps.topLevelItem(0).text(2) == "scan.h5"


def test_run_emits_the_workflow_and_its_folder(editor, tmp_path):
    editor.open_file(str(_EXAMPLE))
    got = []
    editor.sig_run_requested.connect(lambda workflow, root: got.append((workflow, root)))
    assert editor.run()
    assert got and got[0][1] == str(_EXAMPLE.parent)
    assert got[0][0] is not editor.document.workflow            # a copy: the run reads it elsewhere
    assert got[0][0].to_dict() == editor.document.to_dict()
    editor.set_running(True)
    assert editor.run() is False and not editor._actions["run"].isEnabled()
    cancelled = []
    editor.sig_cancel_requested.connect(lambda: cancelled.append(True))
    editor.cancel_run()
    assert cancelled
    editor.set_progress(1, 7, "split")
    assert editor._progress.isVisible() or editor._progress.value() == 1
    editor.set_running(False)
    assert editor._actions["run"].isEnabled()


# -- through the controller ----------------------------------------------------------

def _controller(monkeypatch, tmp_path, loaded, active=None):
    from imswitch.improcess.controller.WorkflowController import WorkflowController
    from imswitch.improcess.model.workfloweditor import folders

    monkeypatch.setattr(folders, "workflows_directory", lambda create=True: str(tmp_path / "workflows"))
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
    assert not editor._actions["run"].isEnabled()
    qtbot.waitUntil(lambda: controller._thread is None, timeout=60000)
    assert editor._actions["run"].isEnabled()
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
        editor.document.dirty = False
        editor.close()
    controller, messages = _controller(monkeypatch, tmp_path, [], active=None)
    assert controller.editWorkflowOfResult() is None
    assert messages[-1] == "No result selected."


def test_the_main_view_exposes_the_editor_signals():
    from imswitch.improcess.view.ImProcessMainView import ImProcessMainView

    assert hasattr(ImProcessMainView, "sigOpenWorkflowEditor")
    assert hasattr(ImProcessMainView, "sigEditWorkflowOfResultRequested")
