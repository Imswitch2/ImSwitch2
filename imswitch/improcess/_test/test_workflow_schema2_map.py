"""Workflow schema 2 with ``map`` and ``fold`` steps.

Controls and bypass were designed (``docs/design/plans/improcess-workflow-schema-2.md``)
before maps and folds existed. What they mean now:

* a map or fold **cannot be bypassed**, and neither can a step in its body
  (there is no single result to pass through, and the body is one unit);
* a control **cannot target** a map, a fold or a step in a body; it can set a
  top-level step that feeds one;
* a **bypassed processing step that a map or fold reads** (as its input, as a
  broadcast read in its body, or as a fold's ``carry.init``) is taken out like
  any other and the read is pointed at what it passed through, in the body
  too -- a run equals the run of the workflow written without the step.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess._test.test_auto_indexing import RESHAPE, side_by_side  # noqa: E402
from imswitch.improcess.workflows import (  # noqa: E402
    Control,
    Fold,
    Map,
    Process,
    Reconstruct,
    Source,
    Workflow,
    WorkflowError,
    bootstrap_registry,
    prepare_run,
    run,
    validate,
)
from imswitch.improcess.workflows.steps import step_from_dict  # noqa: E402

DOUBLE = "out = make_result(data * 2.0, axes=axes)\n"
ADD_OFFSET = "out = make_result(data + inputs[1].mean(), axes=axes)\n"


@pytest.fixture(scope="module")
def registry():
    return bootstrap_registry(user_plugins=False)


def _head(raw, *, bypass=False):
    return [
        Source("raw", path=str(raw)),
        Reconstruct("view", "view-only", inputs=["raw"]),
        Process("reshape", "python", {"code": RESHAPE}, inputs=["view"]),
        Process("double", "python", {"code": DOUBLE}, inputs=["reshape"], bypass=bypass),
    ]


def _body(*, reads="$item", extra=None):
    inputs = [reads] + ([extra] if extra else [])
    code = ADD_OFFSET if extra else DOUBLE
    return [{"step": "process", "id": "inner", "processor": "python", "params": {"code": code}, "inputs": inputs}]


def _map(raw, *, bypass=False, input="double", extra=None):
    return Workflow("m", [*_head(raw, bypass=bypass), Map("per_t", input, "T", body=_body(extra=extra))])


def _values(workflow, registry, tmp_path):
    with run(workflow, registry=registry, out_dir=tmp_path) as report:
        return np.asarray(report.result("per_t").data), list(report.bypassed)


def test_a_bypassed_step_a_map_iterates_over_is_taken_out(registry, tmp_path):
    raw, _ = side_by_side(tmp_path / "raw.h5")
    bypassed = _map(raw, bypass=True)
    prepared = prepare_run(bypassed, registry=registry)
    assert prepared.bypassed == ("double",)
    assert [s.id for s in prepared.workflow.steps] == ["raw", "view", "reshape", "per_t"]
    assert str(prepared.workflow.step("per_t").input) == "reshape"
    assert prepared.rewired[("per_t", 0)] == ("double", "reshape")
    got, taken_out = _values(bypassed, registry, tmp_path / "a")
    assert taken_out == ["double"]
    # The same run as the workflow written without the step.
    plain = Workflow("p", [*_head(raw)[:3], Map("per_t", "reshape", "T", body=_body())])
    expected, _ = _values(plain, registry, tmp_path / "b")
    np.testing.assert_allclose(got, expected)
    # And switching the bypass off gives the doubled input, so it did matter.
    doubled, _ = _values(_map(raw), registry, tmp_path / "c")
    np.testing.assert_allclose(doubled, expected * 2.0)


def test_a_body_read_of_a_bypassed_step_follows_it_upstream(registry, tmp_path):
    raw, _ = side_by_side(tmp_path / "raw.h5")
    # The body adds the mean of the broadcast ``double``; bypassed, that is the mean of ``reshape``.
    bypassed = _map(raw, bypass=True, input="reshape", extra="double")
    prepared = prepare_run(bypassed, registry=registry)
    body = prepared.workflow.step("per_t").body
    assert [str(ref) for ref in body[0].inputs] == ["_item", "reshape"], "the broadcast read must survive"
    assert [str(ref) for ref in prepared.workflow.step("per_t").inputs] == ["reshape"]
    got, _ = _values(bypassed, registry, tmp_path / "a")
    plain = Workflow("p", [*_head(raw)[:3], Map("per_t", "reshape", "T", body=_body(extra="reshape"))])
    expected, _ = _values(plain, registry, tmp_path / "b")
    np.testing.assert_allclose(got, expected)
    # The recipe itself is untouched by preparing it.
    assert [str(ref) for ref in bypassed.step("per_t").body[0].inputs] == ["_item", "double"]


def test_a_fold_reads_through_a_bypassed_step_too():
    fold = {"step": "fold", "id": "f", "input": "double", "over": "T",
            "body": [{"step": "process", "id": "p", "processor": "python",
                      "params": {"code": DOUBLE}, "inputs": ["$item", "$carry"]}],
            "carry": {"init": "double", "next": "p"}}
    workflow = Workflow("f", [
        Source("raw", path="x.h5"),
        Reconstruct("view", "view-only", inputs=["raw"]),
        Process("double", "python", {"code": DOUBLE}, inputs=["view"], bypass=True),
        step_from_dict(fold),
    ])
    prepared = prepare_run(workflow)
    step = prepared.workflow.step("f")
    assert isinstance(step, Fold)
    assert str(step.input) == "view" and step.carry["init"] == "view"
    assert [str(ref) for ref in step.inputs] == ["view"]


def test_a_map_and_a_fold_cannot_be_bypassed(registry, tmp_path):
    raw, _ = side_by_side(tmp_path / "raw.h5")
    payload = _map(raw).step("per_t").to_dict()
    with pytest.raises(WorkflowError, match="only process and save steps can be bypassed"):
        step_from_dict({**payload, "bypass": True})
    fold = {**payload, "step": "fold", "carry": {"init": None, "next": "inner"}}
    with pytest.raises(WorkflowError, match="only process and save steps can be bypassed"):
        step_from_dict({**fold, "bypass": True})


def test_a_body_step_cannot_be_bypassed(registry, tmp_path):
    raw, _ = side_by_side(tmp_path / "raw.h5")
    body = _body()
    body[0]["bypass"] = True
    with pytest.raises(WorkflowError, match="body step 'inner' cannot be bypassed"):
        Map("per_t", "reshape", "T", body=body)


def test_a_control_cannot_target_a_map_or_a_body_step(registry, tmp_path):
    raw, _ = side_by_side(tmp_path / "raw.h5")
    for target in ("per_t.over", "inner.code"):
        workflow = _map(raw)
        workflow.controls = [Control("c", [target])]
        issues = [str(issue) for issue in validate(workflow, registry)]
        assert any("control 'c'" in i for i in issues), issues
        with pytest.raises(WorkflowError):
            prepare_run(workflow, registry=registry)


def test_a_control_on_a_step_that_feeds_a_map_applies_and_is_recorded(registry, tmp_path):
    raw, _ = side_by_side(tmp_path / "raw.h5")
    steps = [
        Source("raw", path=str(raw)),
        Reconstruct("view", "view-only", inputs=["raw"]),
        Process("reshape", "python", {"code": RESHAPE}, inputs=["view"]),
        Process("smooth", "filter", {"method": "gaussian", "radius": 1.0}, inputs=["reshape"]),
        Map("per_t", "smooth", "T", body=_body()),
    ]
    recipe = Workflow("c", steps, controls=[Control("radius", ["smooth.radius"])])
    assert recipe.to_dict()["schema"] == 2
    with run(recipe, registry=registry, out_dir=tmp_path / "a", controls={"radius": 2.5}) as report:
        assert report.controls == {"radius": 2.5}
        made = np.asarray(report.result("per_t").data)
    explicit = Workflow("e", [*steps[:3], Process("smooth", "filter", {"method": "gaussian", "radius": 2.5},
                                                  inputs=["reshape"]), steps[4]])
    with run(explicit, registry=registry, out_dir=tmp_path / "b") as report:
        np.testing.assert_allclose(made, np.asarray(report.result("per_t").data))


def test_a_workflow_with_a_map_but_no_schema_2_feature_stays_schema_1(registry, tmp_path):
    raw, _ = side_by_side(tmp_path / "raw.h5")
    payload = _map(raw).to_dict()
    assert payload["schema"] == 1 and "controls" not in payload
    assert Workflow.from_dict(payload).to_dict() == payload
    # A schema-1 file with a map and a body still loads, validates and runs.
    assert validate(Workflow.from_dict(payload), registry) == []
    got, taken_out = _values(Workflow.from_dict(payload), registry, tmp_path)
    assert taken_out == [] and got.shape[0] == 2


def test_a_recorded_map_replays_to_a_schema_1_workflow(registry, tmp_path):
    from imswitch.improcess.workflows import Save, workflow_from_file

    raw, _ = side_by_side(tmp_path / "raw.h5")
    recipe = _map(raw, bypass=True)
    recipe.steps.append(Save("save", input="per_t", fmt="tiff"))
    with run(recipe, registry=registry, out_dir=tmp_path / "first") as report:
        original = np.asarray(report.result("per_t").data)
        saved = report.receipts[0].primary
    replay = workflow_from_file(saved, registry=registry)
    payload = replay.workflow.to_dict()
    assert payload["schema"] == 1 and "bypass" not in str(payload) and "controls" not in payload
    assert "double" not in [step["id"] for step in payload["steps"]]
    assert validate(replay.workflow, registry) == []
    maps = [step for step in replay.workflow.steps if isinstance(step, Map)]
    with run(replay.workflow, registry=registry, out_dir=tmp_path / "second", mode="replay") as again:
        np.testing.assert_allclose(np.asarray(again.result(maps[0].id).data), original)
