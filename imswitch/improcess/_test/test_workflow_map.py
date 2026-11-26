"""The ``map`` step: a group of steps run once per index, the outputs stacked.

A map over T of (SNOUTY deskew -> crop) gives the same as auto-indexed
SNOUTY followed by the crop; its body round-trips through a file with
``$item`` as written; the provenance records the body once with
``iterated_over``, and a replay maps again. A ``fold`` loads, validates and
saves, and refuses to run.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model.provenance import graph_of, output_node  # noqa: E402
from imswitch.improcess.workflows import (  # noqa: E402
    Fold,
    Map,
    Process,
    Reconstruct,
    RunError,
    Save,
    Source,
    Workflow,
    WorkflowError,
    bootstrap_registry,
    run,
    validate,
    workflow_from_file,
)
from imswitch.improcess.workflows.steps import step_from_dict  # noqa: E402

from imswitch.improcess._test.test_auto_indexing import RESHAPE, SNOUTY, side_by_side  # noqa: E402

#: Keep the upper-left quarter of every plane: stands in for "crop the
#: skewed mess off before MoNaLISA".
CROP = (
    "y, x = data.shape[-2:]\n"
    "out = make_result(data[..., : y // 2, : x // 2], axes=axes)\n"
)

#: Adds an outer (broadcast) result to every index.
ADD_OFFSET = "out = make_result(data + inputs[1].mean(), axes=axes)\n"


@pytest.fixture(scope="module")
def registry():
    return bootstrap_registry(user_plugins=False)


def head(raw):
    return [
        Source("raw", path=str(raw)),
        Reconstruct("view", "view-only", inputs=["raw"]),
        Process("reshape", "python", {"code": RESHAPE}, inputs=["view"]),
    ]


def mapped(raw):
    return Workflow("mapped", [
        *head(raw),
        Map("per_t", "reshape", "T", body=[
            {"step": "reconstruct", "id": "deskew", "reconstructor": "snouty", "params": dict(SNOUTY),
             "inputs": ["$item"]},
            {"step": "process", "id": "crop", "processor": "python", "params": {"code": CROP},
             "inputs": ["deskew"]},
        ], output="crop"),
        Save("save", input="per_t", fmt="tiff"),
    ])


def auto_indexed(raw):
    return Workflow("auto", [
        *head(raw),
        Reconstruct("deskew", "snouty", dict(SNOUTY), inputs=["reshape"]),
        Process("crop", "python", {"code": CROP}, inputs=["deskew"]),
    ])


def test_a_map_equals_the_auto_indexed_chain(registry, tmp_path):
    raw, _ = side_by_side(tmp_path / "raw.h5")
    with run(auto_indexed(raw), registry=registry, out_dir=tmp_path / "a") as report:
        expected = np.asarray(report.result("crop").data)
    progress = []
    with run(mapped(raw), registry=registry, out_dir=tmp_path / "b",
             progress=lambda i, n, text: progress.append(text)) as report:
        made = report.result("per_t")
        assert made.axis_labels[0] == "T" and made.data.shape[0] == 2
        np.testing.assert_allclose(np.asarray(made.data), expected)
        assert "per_t [T=1/2]" in progress and "per_t [T=2/2]" in progress
        node = output_node(made)
        assert node["op"] == "map" and node["iterated_over"] == {"T": 2}
        assert [step["id"] for step in node["body"]] == ["deskew", "crop"]
        assert node["body"][0]["inputs"] == ["$item"]
        # The body is recorded once, not a node per index.
        nodes = list(graph_of(made)["nodes"].values())
        assert [n["op"] for n in nodes].count("map") == 1
        assert not any(n.get("plugin_id") == "snouty" for n in nodes)
        assert len(report.receipts) == 1


def test_the_body_round_trips_through_a_file(registry, tmp_path):
    raw, _ = side_by_side(tmp_path / "raw.h5")
    workflow = mapped(raw)
    path = tmp_path / "wf.yaml"
    workflow.save(path) if hasattr(workflow, "save") else path.write_text(workflow.to_yaml())
    loaded = Workflow.load(path) if hasattr(Workflow, "load") else Workflow.from_yaml(path.read_text())
    step = loaded.step("per_t")
    assert isinstance(step, Map) and step.over == "T" and str(step.output) == "crop"
    assert step.to_dict() == workflow.step("per_t").to_dict()
    assert validate(loaded, registry) == []


def test_a_body_may_read_an_outer_step(registry, tmp_path):
    raw, _ = side_by_side(tmp_path / "raw.h5")
    workflow = Workflow("broadcast", [
        *head(raw),
        Map("per_t", "reshape", "T", body=[
            {"step": "process", "id": "shift", "processor": "python",
             "params": {"code": ADD_OFFSET}, "inputs": ["$item", "view"]},
        ]),
    ])
    assert validate(workflow, registry) == []
    assert [str(ref) for ref in workflow.step("per_t").inputs] == ["reshape", "view"]
    with run(workflow, registry=registry, out_dir=tmp_path) as report:
        reshaped = np.asarray(report.result("reshape").data)
        offset = float(np.asarray(report.result("view").data).mean())
        np.testing.assert_allclose(np.asarray(report.result("per_t").data), reshaped + offset, rtol=1e-5)


@pytest.mark.parametrize("body, output, over, message", [
    ([{"step": "save", "id": "s", "input": "$item"}], None, "T", "not allowed in a body"),
    ([{"step": "process", "id": "p", "processor": "python", "params": {"code": CROP}, "inputs": ["$item"]}],
     "nope", "T", "is not a step of its body"),
    ([{"step": "process", "id": "p", "processor": "python", "params": {"code": CROP}, "inputs": ["$item"]}],
     None, "", "'over' must name"),
    ([{"step": "process", "id": "view", "processor": "python", "params": {"code": CROP}, "inputs": ["$item"]}],
     None, "T", "has the id of a step outside it"),
    ([{"step": "process", "id": "p", "processor": "python", "params": {"code": CROP}, "inputs": ["ghost"]}],
     None, "T", "in the body, p"),
])
def test_a_bad_body_is_reported_before_running(registry, tmp_path, body, output, over, message):
    raw, _ = side_by_side(tmp_path / "raw.h5")
    workflow = Workflow("bad", [*head(raw), Map("per_t", "reshape", over, body=body, output=output)])
    issues = validate(workflow, registry)
    assert any(message in issue.message for issue in issues), issues


def test_mapping_over_a_missing_axis_names_the_axes(registry, tmp_path):
    raw, _ = side_by_side(tmp_path / "raw.h5")
    workflow = Workflow("noaxis", [*head(raw), Map("per_c", "reshape", "C", body=[
        {"step": "process", "id": "p", "processor": "python", "params": {"code": CROP}, "inputs": ["$item"]},
    ])])
    with pytest.raises(RunError, match="no 'C' axis"):
        run(workflow, registry=registry, out_dir=tmp_path)


def test_a_cancelled_map_stops_between_indices(registry, tmp_path):
    raw, _ = side_by_side(tmp_path / "raw.h5", timepoints=3)
    calls = []

    def cancel():
        calls.append(1)
        return len(calls) > 6

    with pytest.raises(RunError, match="cancelled"):
        run(mapped(raw), registry=registry, out_dir=tmp_path, cancel=cancel)


def test_a_mapped_result_replays(registry, tmp_path):
    raw, _ = side_by_side(tmp_path / "raw.h5")
    with run(mapped(raw), registry=registry, out_dir=tmp_path / "first") as report:
        original = np.asarray(report.result("per_t").data)
        saved = report.receipts[0].primary
    replay = workflow_from_file(saved, registry=registry)
    maps = [step for step in replay.workflow.steps if isinstance(step, Map)]
    assert len(maps) == 1
    assert validate(replay.workflow, registry) == []
    with run(replay.workflow, registry=registry, out_dir=tmp_path / "second", mode="replay") as again:
        np.testing.assert_allclose(np.asarray(again.result(maps[0].id).data), original)


def test_a_replayed_map_points_its_broadcast_at_the_replayed_steps(registry, tmp_path):
    raw, _ = side_by_side(tmp_path / "raw.h5")
    workflow = Workflow("broadcast", [
        *head(raw),
        Map("per_t", "reshape", "T", body=[
            {"step": "process", "id": "shift", "processor": "python",
             "params": {"code": ADD_OFFSET}, "inputs": ["$item", "view"]},
        ]),
        Save("save", input="per_t", fmt="tiff"),
    ])
    with run(workflow, registry=registry, out_dir=tmp_path / "first") as report:
        original = np.asarray(report.result("per_t").data)
        saved = report.receipts[0].primary
    replay = workflow_from_file(saved, registry=registry)
    assert validate(replay.workflow, registry) == []
    step = next(s for s in replay.workflow.steps if isinstance(s, Map))
    assert "view" not in {ref.step for ref in step.inputs}
    with run(replay.workflow, registry=registry, out_dir=tmp_path / "second", mode="replay") as again:
        np.testing.assert_allclose(np.asarray(again.result(step.id).data), original, rtol=1e-6)


# -- fold: designed, not run --------------------------------------------------------

FOLD = {
    "step": "fold", "id": "drift", "input": "reshape", "over": "T",
    "body": [{"step": "process", "id": "p", "processor": "python", "params": {"code": CROP},
              "inputs": ["$item"]}],
    "carry": {"init": None, "next": "p"},
}


def test_a_fold_loads_validates_and_round_trips(registry, tmp_path):
    raw, _ = side_by_side(tmp_path / "raw.h5")
    fold = step_from_dict(dict(FOLD))
    assert isinstance(fold, Fold) and fold.carry == {"init": None, "next": "p"}
    assert step_from_dict(fold.to_dict()).to_dict() == fold.to_dict()
    assert validate(Workflow("f", [*head(raw), fold]), registry) == []


def test_a_fold_needs_a_next(registry):
    with pytest.raises(WorkflowError, match="carry needs 'next'"):
        step_from_dict({**FOLD, "carry": {"init": None}})


def test_running_a_fold_is_refused_clearly(registry, tmp_path):
    raw, _ = side_by_side(tmp_path / "raw.h5")
    with pytest.raises(RunError, match="designed .* cannot run yet"):
        run(Workflow("f", [*head(raw), step_from_dict(dict(FOLD))]), registry=registry, out_dir=tmp_path)


# -- the editor ------------------------------------------------------------------------

def test_the_editor_shows_a_map_and_keeps_its_body(registry, tmp_path):
    from imswitch.improcess.model.workfloweditor import WorkflowDocument, build_catalog
    from imswitch.improcess.model.workfloweditor.nodes import node_infos

    raw, _ = side_by_side(tmp_path / "raw.h5")
    path = tmp_path / "wf.yaml"
    mapped(raw).save(path)
    document = WorkflowDocument.load(path, catalog=build_catalog(registry))
    assert document.issues() == []
    info = node_infos(document)["per_t"]
    assert info.title == "Map over T" and info.step_kind == "map"
    assert info.summary[0] == "each T: deskew → crop"
    assert [slot.ref for slot in info.outputs] == ["per_t"]
    # What the body's output step declares: a Python step says "any".
    assert info.outputs[0].kind == "any"
    assert any(option.step == "per_t" for option in document.port_options("save"))
    # A rename reaches into the body; the body survives a save.
    document.rename_step("reshape", "stack")
    assert str(document.step("per_t").input) == "stack"
    document.save(tmp_path / "again.yaml")
    again = Workflow.load(tmp_path / "again.yaml")
    assert again.step("per_t").to_dict()["body"] == mapped(raw).step("per_t").to_dict()["body"]


def test_a_rename_reaches_a_broadcast_read_in_the_body(registry, tmp_path):
    from imswitch.improcess.model.workfloweditor import WorkflowDocument, build_catalog

    raw, _ = side_by_side(tmp_path / "raw.h5")
    workflow = Workflow("broadcast", [
        *head(raw),
        Map("per_t", "reshape", "T", body=[
            {"step": "process", "id": "shift", "processor": "python",
             "params": {"code": ADD_OFFSET}, "inputs": ["$item", "view"]},
        ]),
    ])
    document = WorkflowDocument(workflow, catalog=build_catalog(registry))
    document.rename_step("view", "viewed")
    assert [str(ref) for ref in document.step("per_t").inputs] == ["reshape", "viewed"]
    assert document.step("per_t").to_dict()["body"][0]["inputs"] == ["$item", "viewed"]
    assert document.issues() == []


def test_the_editor_window_draws_a_map_node(registry, tmp_path):
    from qtpy import QtWidgets

    from imswitch.improcess.model.workfloweditor import build_catalog
    from imswitch.improcess.model.workfloweditor.preferences import EditorPreferences
    from imswitch.improcess.view.workfloweditor import MainWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    raw, _ = side_by_side(tmp_path / "raw.h5")
    path = tmp_path / "wf.yaml"
    mapped(raw).save(path)
    window = MainWindow(build_catalog(registry), start_folder=tmp_path,
                        preferences=EditorPreferences(tmp_path / "options.json"))
    try:
        window.show()
        app.processEvents()
        assert window.open_file(str(path))
        assert set(window.scene.nodes) == {step.id for step in window.document.steps}
        assert window.scene.node("per_t").info.title == "Map over T"
        assert window.issues() == []
        window.document.save(tmp_path / "saved.yaml")
        assert Workflow.load(tmp_path / "saved.yaml").step("per_t").to_dict() == mapped(raw).step("per_t").to_dict()
    finally:
        window.document.dirty = False
        window.close()


def test_deleting_what_a_map_reads_leaves_a_map_that_loads_and_is_reported(registry, tmp_path):
    from imswitch.improcess.model.workfloweditor import WorkflowDocument, build_catalog

    raw, _ = side_by_side(tmp_path / "raw.h5")
    workflow = Workflow("broadcast", [
        *head(raw),
        Map("per_t", "reshape", "T", body=[
            {"step": "process", "id": "shift", "processor": "python",
             "params": {"code": ADD_OFFSET}, "inputs": ["$item", "view"]},
        ]),
    ])
    document = WorkflowDocument(workflow, catalog=build_catalog(registry))
    document.delete_step("reshape")
    step = document.step("per_t")
    assert step.input is None and [str(ref) for ref in step.inputs] == ["view"]
    assert any("needs an input" in issue.message for issue in document.issues_for("per_t"))
    document.delete_step("view")
    assert step.inputs == [] and step.to_dict()["body"][0]["inputs"] == ["$item"]
    document.save(tmp_path / "left.yaml")
    assert Workflow.load(tmp_path / "left.yaml").step("per_t").input is None
