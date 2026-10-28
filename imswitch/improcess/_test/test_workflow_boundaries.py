"""A result with an interactive step (Fiji, a napari plugin) still yields a workflow.

Everything except the interactive step becomes the workflow: what fed it is
saved, the step itself is an input the user binds, and what follows runs from
that input. A broken recipe (a missing plugin) still refuses.
"""

import os
from types import SimpleNamespace

import h5py
import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtWidgets  # noqa: E402

from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.model.provenance import graph_of, record_import  # noqa: E402
from imswitch.improcess.model.save_protocol import ProvenanceDocument  # noqa: E402
from imswitch.improcess.processors.run import run_processor  # noqa: E402
from imswitch.improcess.workflows import (  # noqa: E402
    Process,
    Reconstruct,
    ReplayError,
    RunError,
    Save,
    Source,
    Workflow,
    bootstrap_registry,
    run,
    workflow_from_provenance,
)
from imswitch.improcess.workflows.replay import workflow_from_provenance_around_interactive_steps  # noqa: E402


@pytest.fixture(scope="module")
def registry():
    return bootstrap_registry(user_plugins=False)


def _h5(path, shape=(2, 8, 8)):
    with h5py.File(str(path), "w") as handle:
        dataset = handle.create_dataset("data", data=np.random.default_rng(0).random(shape).astype(np.float32))
        dataset.attrs["element_size_um"] = [1.0, 0.1, 0.1]
        dataset.attrs["axes"] = "CYX"
    return path


FIJI_DETAILS = {
    "image_id": -3,
    "fiji_history": {"schema": "fiji-history-v1", "steps": [{"command": "Invert"}, {"command": "Gaussian Blur..."}]},
}


def _chain(registry, tmp_path, *, tool="fiji", details=None, after_import=True):
    """source -> reconstruct -> process -> import(tool) [-> process]; the final result."""
    raw = _h5(tmp_path / "scan.h5")
    wf = Workflow("w", [
        Source("raw", path=str(raw)),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("blur", "filter", {"method": "gaussian", "radius": 1.0}, inputs=["rec"]),
    ])
    with run(wf, registry=registry, out_dir=tmp_path) as report:
        sent = report.result("blur")
        imported = record_import(
            ArrayProcessingResult("dup", np.random.default_rng(2).random((2, 8, 8)).astype(np.float32),
                                  ["C", "Y", "X"]),
            plugin_name="Fiji" if tool == "fiji" else "napari-x", widget_name=None if tool == "fiji" else "w",
            layer_name="dup", layer_type="image", grid="fresh", source_result=sent, tool=tool,
            details=FIJI_DETAILS if details is None else details,
        )
        if not after_import:
            return imported
        final, _ = run_processor(
            registry.get_processor("filter"), [imported], {"method": "median", "radius": 1.0}, None
        )
        return final[0]


def _replay(result, registry, **kw):
    return workflow_from_provenance(ProvenanceDocument(graph=graph_of(result)), registry=registry, **kw)


def test_the_default_still_refuses_with_the_boundary_as_a_structured_reason(registry, tmp_path):
    final = _chain(registry, tmp_path)
    with pytest.raises(ReplayError) as excinfo:
        _replay(final, registry)
    error = excinfo.value
    assert any("interactively in Fiji" in r for r in error.reasons)
    assert error.only_boundaries and len(error.boundaries) == 1 and error.blocking == []


def test_a_fiji_chain_becomes_a_workflow_with_an_input(registry, tmp_path):
    final = _chain(registry, tmp_path)
    replay = _replay(final, registry, allow_inputs=True)
    steps = replay.workflow.steps
    assert [type(s).__name__ for s in steps] == [
        "Source", "Reconstruct", "Process", "Save", "Source", "Process", "Save",
    ]
    raw, _rec, _blur, fed, from_fiji, post, last = steps
    assert raw.source.path and not from_fiji.source.path
    assert fed.id == "save_for_fiji" and fed.path_template == "{out_dir}/{source_stem}_for_fiji{ext}"
    assert str(fed.input) == "proc1.out"
    assert from_fiji.id == "from_fiji" and str(post.inputs[0]) == "from_fiji"
    assert str(last.input) == f"{post.id}.out"
    assert replay.inputs == [("from_fiji", from_fiji.source.metadata["input"])]
    assert "Fiji image -3" in replay.inputs[0][1] and "bind a file or a result" in replay.inputs[0][1]
    (warning,) = replay.interactive
    assert warning == (
        "The Fiji step (image 'dup', Fiji image -3, 2 recorded Fiji steps) cannot be part of a workflow; the workflow "
        "saves what was sent to Fiji and starts again from an input 'from_fiji' you bind when running it."
    )
    assert warning in replay.warnings
    from imswitch.improcess.workflows import validate

    assert validate(replay.workflow, registry) == []


def test_the_import_as_the_last_step_leaves_the_fed_chain_and_its_save(registry, tmp_path):
    imported = _chain(registry, tmp_path, after_import=False)
    replay = _replay(imported, registry, allow_inputs=True)
    assert [type(s).__name__ for s in replay.workflow.steps] == ["Source", "Reconstruct", "Process", "Save"]
    assert replay.inputs == []
    (warning,) = replay.interactive
    assert warning.startswith("The last step happened in Fiji") and "stops at what was sent to Fiji" in warning


def test_a_napari_import_behaves_the_same(registry, tmp_path):
    final = _chain(registry, tmp_path, tool="napari", details={})
    replay = _replay(final, registry, allow_inputs=True)
    assert [s.id for s in replay.workflow.steps if isinstance(s, Source)] == ["src1", "from_napari"]
    assert replay.workflow.step("save_for_napari").path_template.endswith("_for_napari{ext}")
    (warning,) = replay.interactive
    assert "napari" in warning and "plugin 'napari-x'" in warning and "'from_napari'" in warning


def test_an_origin_less_result_becomes_an_input_without_a_save(registry, tmp_path):
    loose = ArrayProcessingResult("loose", np.random.default_rng(3).random((8, 8)).astype(np.float32), ["Y", "X"])
    final, _ = run_processor(registry.get_processor("filter"), [loose], {"method": "median", "radius": 1.0}, None)
    replay = _replay(final[0], registry, allow_inputs=True)
    assert [type(s).__name__ for s in replay.workflow.steps] == ["Source", "Process", "Save"]
    assert replay.inputs[0][0] == "input"


def test_a_missing_plugin_still_refuses_even_with_allow_inputs(registry, tmp_path):
    final = _chain(registry, tmp_path)
    graph = graph_of(final)
    for node in graph["nodes"].values():
        if node.get("op") == "process" and node.get("plugin_id") == "filter":
            node["plugin_id"] = "no-such-processor"
    with pytest.raises(ReplayError, match="not installed") as strict:
        workflow_from_provenance(ProvenanceDocument(graph=graph), registry=registry)
    assert not strict.value.only_boundaries          # a broken recipe is not an interactive step
    with pytest.raises(ReplayError, match="not installed"):
        workflow_from_provenance(ProvenanceDocument(graph=graph), registry=registry, allow_inputs=True)
    with pytest.raises(ReplayError, match="not installed"):
        workflow_from_provenance_around_interactive_steps(ProvenanceDocument(graph=graph), registry=registry)


def test_the_exported_workflow_runs_with_the_input_bound_to_a_result(registry, tmp_path):
    final = _chain(registry, tmp_path)
    replay = _replay(final, registry, allow_inputs=True)
    exported = Workflow.load(replay.workflow.save(tmp_path / "around.yaml"))    # through the file, as a user would
    stand_in = ArrayProcessingResult("from fiji", np.random.default_rng(4).random((2, 8, 8)).astype(np.float32),
                                     ["C", "Y", "X"])
    out = tmp_path / "out"
    with run(exported, registry=registry, bindings={"from_fiji": stand_in}, out_dir=out) as report:
        assert report.bound == ["from_fiji.data"]
        assert report.result("proc2").data.shape == (2, 8, 8)
    names = sorted(p.name for p in out.iterdir())
    assert any("_for_fiji" in n for n in names) and len(names) >= 2     # what was sent, and the final result


def test_an_unbound_input_is_refused_by_name_before_any_work(registry, tmp_path):
    final = _chain(registry, tmp_path)
    replay = _replay(final, registry, allow_inputs=True)
    out = tmp_path / "out"
    with pytest.raises(RunError, match="input 'from_fiji' is not bound; bind a file or run it on a selected result") as e:
        run(replay.workflow, registry=registry, out_dir=out)
    assert e.value.report.failed_step == "from_fiji"
    assert not out.exists() or not any(out.iterdir())              # nothing ran, nothing was saved


def test_run_on_results_binds_only_the_reconstruction_so_the_input_stays_unbound(registry, tmp_path):
    """Reported behaviour, not a redesign: the entry step is the one reconstruction."""
    final = _chain(registry, tmp_path)
    replay = _replay(final, registry, allow_inputs=True)
    shown = ArrayProcessingResult("shown", np.random.default_rng(5).random((2, 8, 8)).astype(np.float32),
                                  ["C", "Y", "X"])
    with pytest.raises(RunError, match="input 'from_fiji' is not bound"):
        run(replay.workflow, registry=registry, bindings={"rec1": shown}, out_dir=tmp_path / "o")


def test_the_editor_document_shows_the_input_without_a_path(registry, tmp_path):
    from imswitch.improcess.model.workfloweditor import WorkflowDocument, build_catalog

    final = _chain(registry, tmp_path)
    document = WorkflowDocument.from_result(final, build_catalog(registry), name="around")
    assert [sid for sid, _ in document.inputs] == ["from_fiji"]
    assert document.interactive and any("from_fiji" in note for note in document.notes)
    step = document.workflow.step("from_fiji")
    assert step.source.path is None
    # Workflow editor 2.0 (main) shows a path-less source as one to bind at
    # run time; editor 1.0's steplist._target label no longer exists.


def _controller(result):
    from imswitch.improcess.controller.WorkflowController import WorkflowController

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    messages = []
    view = SimpleNamespace(showStatusMessage=lambda m, timeout_ms=6000: messages.append(m))
    comm = SimpleNamespace(sigResultProduced=SimpleNamespace(emit=lambda r, n: None))
    recon = SimpleNamespace(getActiveResult=lambda: result)
    return WorkflowController(comm, view, recon), messages


def test_the_controller_exports_around_the_step_and_tells_the_user(registry, tmp_path, monkeypatch):
    final = _chain(registry, tmp_path)
    shown = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", lambda *a, **k: shown.append(a[1:]))
    controller, messages = _controller(final)
    written = controller.exportWorkflow(tmp_path / "exported.yaml")
    assert written is not None and written.exists()
    assert messages[-1].startswith("Wrote workflow exported.yaml (7 steps; 1 input to bind: from_fiji)")
    ((title, text),) = shown
    assert title == "Export workflow" and "The Fiji step" in text and "from_fiji" in text


def test_the_controller_still_refuses_a_broken_recipe(registry, tmp_path, monkeypatch):
    final = _chain(registry, tmp_path)
    for node in graph_of(final)["nodes"].values():
        if node.get("plugin_id") == "filter":
            node["plugin_id"] = "no-such-processor"
    shown = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", lambda *a, **k: shown.append(a))
    controller, messages = _controller(final)
    assert controller.exportWorkflow(tmp_path / "x.yaml") is None
    assert "Cannot export a runnable workflow" in messages[-1] and shown == []


def test_applied_to_what_came_back_only_the_part_after_the_input_runs(registry, tmp_path):
    from imswitch.improcess.workflows.steps import Save, part_from_input, unbound_inputs

    final = _chain(registry, tmp_path)
    workflow = _replay(final, registry, allow_inputs=True).workflow
    assert unbound_inputs(workflow) == ["from_fiji"]
    part = part_from_input(workflow, "from_fiji")
    kept = [step.id for step in part.steps]
    assert "from_fiji" in kept and "rec1" not in kept                  # the branch to Fiji is not rerun
    assert not any(isinstance(s, Save) and "_for_fiji" in s.path_template for s in part.steps)
    stand_in = ArrayProcessingResult("from fiji", np.random.default_rng(6).random((2, 8, 8)).astype(np.float32),
                                     ["C", "Y", "X"])
    out = tmp_path / "out"
    with run(part, registry=registry, bindings={"from_fiji": stand_in}, out_dir=out) as report:
        assert report.result("proc2").data.shape == (2, 8, 8)
    assert len(list(out.iterdir())) == 1 and not any("_for_fiji" in p.name for p in out.iterdir())


def test_run_on_selected_results_fills_the_one_input(registry, tmp_path, monkeypatch):
    final = _chain(registry, tmp_path)
    workflow = _replay(final, registry, allow_inputs=True).workflow
    controller, _messages = _controller(final)
    calls = []
    monkeypatch.setattr(controller, "_runBatch", lambda path, bindings, **kw: calls.append((bindings, kw)) or True)
    picked = ArrayProcessingResult("picked", np.zeros((2, 8, 8), np.float32), ["C", "Y", "X"])
    assert controller.runWorkflowObjectOnResults(workflow, results=[picked]) is True
    ((bindings, kw),) = calls
    assert bindings == [{"from_fiji": picked}]
    assert "rec1" not in [s.id for s in kw["workflow"].steps]


# -- two interactive steps in a row, a deleted Save, and the wording --------------------


def _import(sent, name, *, tool="fiji"):
    """An import from ``tool`` of an image called ``name`` that was made from ``sent`` (or from nothing)."""
    data = np.random.default_rng(7).random((2, 8, 8)).astype(np.float32)
    return record_import(
        ArrayProcessingResult(name, data, ["C", "Y", "X"]),
        plugin_name="Fiji" if tool == "fiji" else "napari-x", widget_name=None, layer_name=name,
        layer_type="image", grid="fresh", source_result=sent, tool=tool, details={"image_id": -3},
    )


def _fiji_twice(registry, tmp_path, *, then_process):
    """source -> reconstruct -> blur -> Fiji -> Fiji [-> median]: the second step was sent the first one's image."""
    sent = _chain(registry, tmp_path, after_import=False)         # the first import, already one Fiji step
    second = _import(sent, "dup2")
    if not then_process:
        return second
    final, _ = run_processor(registry.get_processor("filter"), [second], {"method": "median", "radius": 1.0}, None)
    return final[0]


def _sources(workflow):
    return [s for s in workflow.steps if isinstance(s, Source)]


def test_a_fiji_step_fed_by_another_leaves_only_the_input_the_workflow_reads(registry, tmp_path):
    from imswitch.improcess.workflows.steps import unbound_inputs

    final = _fiji_twice(registry, tmp_path, then_process=True)
    replay = _replay(final, registry, allow_inputs=True)
    workflow = Workflow.load(replay.workflow.save(tmp_path / "twice.yaml"))
    assert [sid for sid, _ in replay.inputs] == ["from_fiji2"] == unbound_inputs(workflow)
    assert [s.id for s in _sources(workflow) if not s.source.path] == ["from_fiji2"]
    assert not any(s.id == "from_fiji" for s in workflow.steps)
    first, second = replay.interactive
    assert "saves what was sent to Fiji" in first and "'dup'" in first
    assert "starts again from an input" not in first and "'from_fiji'" not in first
    assert "starts again from an input 'from_fiji2'" in second and "'dup2'" in second
    assert first in replay.warnings and second in replay.warnings
    assert replay.warnings.index(first) < replay.warnings.index(second)
    assert [step.id for step in workflow.steps if isinstance(step, Save)] == ["save_for_fiji", "save1"]


def test_the_editor_document_and_the_controller_agree_with_the_one_input(registry, tmp_path, monkeypatch):
    from imswitch.improcess.model.workfloweditor import WorkflowDocument, build_catalog

    final = _fiji_twice(registry, tmp_path, then_process=True)
    document = WorkflowDocument.from_result(final, build_catalog(registry), name="twice")
    assert [sid for sid, _ in document.inputs] == ["from_fiji2"] and len(document.interactive) == 2
    shown = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", lambda *a, **k: shown.append(a[1:]))
    controller, messages = _controller(final)
    written = controller.exportWorkflow(tmp_path / "twice.yaml")
    assert written is not None
    assert "1 input to bind: from_fiji2" in messages[-1] and "from_fiji," not in messages[-1]
    ((_title, text),) = shown
    assert "Bind these when you run it:\n- from_fiji2:" in text and "- from_fiji:" not in text
    # A single selected result runs it, instead of being told there are two inputs to bind.
    calls = []
    monkeypatch.setattr(controller, "_runBatch", lambda path, bindings, **kw: calls.append(bindings) or True)
    picked = ArrayProcessingResult("picked", np.zeros((2, 8, 8), np.float32), ["C", "Y", "X"])
    assert controller.runWorkflowObjectOnResults(Workflow.load(written), results=[picked]) is True
    assert calls == [[{"from_fiji2": picked}]] and "inputs to bind" not in messages[-1]


def test_two_fiji_steps_ending_the_result_have_no_input_and_two_true_texts(registry, tmp_path):
    from imswitch.improcess.workflows.steps import unbound_inputs

    final = _fiji_twice(registry, tmp_path, then_process=False)
    replay = _replay(final, registry, allow_inputs=True)
    assert [type(s).__name__ for s in replay.workflow.steps] == ["Source", "Reconstruct", "Process", "Save"]
    assert replay.inputs == [] and unbound_inputs(replay.workflow) == []
    first, last = replay.interactive
    assert "saves what was sent to Fiji" in first and "starts again" not in first
    assert last.startswith("The last step happened in Fiji (image 'dup2'") and "nothing replayable was sent" in last
    assert first in replay.warnings and last in replay.warnings


def test_two_fiji_steps_are_told_apart_by_what_the_images_were_called(registry, tmp_path):
    final = _fiji_twice(registry, tmp_path, then_process=True)
    first, second = _replay(final, registry, allow_inputs=True).interactive
    assert first != second
    assert first.startswith("The Fiji step (image 'dup',") and second.startswith("The Fiji step (image 'dup2',")


def test_a_sole_import_with_nothing_before_it_is_refused_in_true_words(registry):
    fresh = _import(None, "fresh")
    with pytest.raises(ReplayError) as excinfo:
        _replay(fresh, registry, allow_inputs=True)
    assert "came from Fiji directly and nothing replayable precedes it" in str(excinfo.value)
    assert "recording" not in str(excinfo.value)


def test_run_on_results_says_so_when_the_inputs_save_was_deleted(registry, tmp_path):
    """The editor's signal calls this directly: an exception there would show the user nothing."""
    final = _chain(registry, tmp_path)
    workflow = _replay(final, registry, allow_inputs=True).workflow
    workflow.steps = [s for s in workflow.steps if s.id != "save1"]         # the user deleted the final Save
    controller, messages = _controller(final)
    controller._runBatch = lambda *a, **k: pytest.fail("nothing should run")
    picked = ArrayProcessingResult("picked", np.zeros((2, 8, 8), np.float32), ["C", "Y", "X"])
    assert controller.runWorkflowObjectOnResults(workflow, results=[picked]) is False
    assert "'from_fiji' reaches no Save step" in messages[-1] and "add a Save" in messages[-1]
