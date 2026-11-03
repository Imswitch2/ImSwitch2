"""Workflow schema 2: exposed controls and bypass (S1 of
``docs/design/plans/improcess-workflow-schema-2.md``).

The file is read strictly and written at the lowest schema that holds it;
every run goes through ``prepare_run``, which takes bypassed steps out,
applies control values by the value contract and hands the runner a plain
workflow; and a run with a control value or a bypass is the same run as the
plain workflow it stands for.
"""

import csv
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model.param_spec import ParamField  # noqa: E402
from imswitch.improcess.model.provenance import graph_of  # noqa: E402
from imswitch.improcess.workflows import (  # noqa: E402
    Control,
    ControlText,
    ControlValueError,
    Process,
    Reconstruct,
    Save,
    Source,
    Workflow,
    WorkflowError,
    bootstrap_registry,
    describe_controls,
    prepare_run,
    rows_from_manifest,
    run,
    run_over,
    validate,
)
from imswitch.improcess.workflows.prepare import (  # noqa: E402
    CombinedField,
    check_typed,
    combine,
    convert_text,
    recipe_notes,
)

_EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "improcess_workflows"
sys.path.insert(0, str(_EXAMPLES))

from _synthetic import write_synthetic_recording  # noqa: E402


@pytest.fixture(scope="module")
def registry():
    return bootstrap_registry(user_plugins=False)


@pytest.fixture
def recording(tmp_path):
    return str(write_synthetic_recording(tmp_path / "cells.h5", frames=2, axes="CYX"))


def _diamond(*, controls=(), bypass=(), source=None, sharp_radius=2.0):
    """The shipped diamond, both filters at the same radius."""
    steps = [
        Source("raw", path=source),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("split", "stack-split", {"axis": "C"}, inputs=["rec"]),
        Process("smooth", "filter", {"method": "gaussian", "radius": 2.0}, inputs=["split.C0"]),
        Process("sharp", "filter", {"method": "median", "radius": sharp_radius}, inputs=["split.C1"]),
        Process("merge", "channel-merge", inputs=["smooth", "sharp"]),
        Save("out", input="merge", fmt="hdf5", path_template="{out_dir}/{source_stem}_diamond{ext}"),
    ]
    for step in steps:
        if step.id in bypass:
            step.bypass = True
    return Workflow("diamond", steps, controls=list(controls))


RADIUS = Control("radius", ["smooth.radius", "sharp.radius"], label="Filter radius")
METHOD = Control("method", ["smooth.method"])


def _messages(workflow, registry):
    return [str(issue) for issue in validate(workflow, registry)]


# -- reading and writing --------------------------------------------------------------

def test_controls_and_bypass_round_trip_and_need_schema_2():
    workflow = _diamond(controls=[RADIUS], bypass=("sharp",))
    payload = workflow.to_dict()
    assert payload["schema"] == 2
    assert payload["controls"] == [{"name": "radius", "targets": ["smooth.radius", "sharp.radius"],
                                    "label": "Filter radius"}]
    assert [s.get("bypass") for s in payload["steps"]] == [None, None, None, None, True, None, None]
    again = Workflow.from_yaml(workflow.to_yaml())
    assert again.to_dict() == payload
    assert again.step("sharp").bypass is True and again.controls[0].targets == RADIUS.targets


def test_a_workflow_without_the_new_features_is_written_as_schema_1():
    assert _diamond().to_dict()["schema"] == 1
    assert Workflow.from_dict({**_diamond(controls=[RADIUS]).to_dict(), "controls": []}).to_dict()["schema"] == 1
    for path in sorted(_EXAMPLES.glob("*.yaml")):
        payload = Workflow.load(path).to_dict()
        assert payload["schema"] == 1, path.name
        assert "controls" not in payload and not any("bypass" in s for s in payload["steps"]), path.name


@pytest.mark.parametrize("schema, ok", [(None, 1), ("2", 2), (1, 1), (2, 2), (True, None), (False, None),
                                        (0, None), (3, None), ("two", None)])
def test_the_schema_is_read_strictly(schema, ok):
    payload = {"name": "w", "steps": []}
    if schema is not None:
        payload["schema"] = schema
    if ok is None:
        with pytest.raises(WorkflowError):
            Workflow.from_dict(payload)
    else:
        assert Workflow.from_dict(payload).schema == ok


def test_an_empty_schema_reads_as_omitted():
    payload = {**_diamond().to_dict(), "schema": ""}
    assert Workflow.from_dict(payload).to_dict()["schema"] == 1


def test_an_unversioned_file_is_schema_1_and_may_not_use_the_new_keys():
    with pytest.raises(WorkflowError, match="'controls' needs schema: 2"):
        Workflow.from_dict({"name": "w", "controls": [], "steps": []})
    with pytest.raises(WorkflowError, match="'bypass' needs schema: 2"):
        Workflow.from_dict({"name": "w", "steps": [
            {"step": "source", "id": "raw"},
            {"step": "process", "id": "p", "processor": "filter", "inputs": ["raw"], "bypass": False}]})


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, "no"])
def test_bypass_must_be_a_boolean(value):
    with pytest.raises(WorkflowError, match="bypass must be true or false"):
        Workflow.from_dict({"name": "w", "schema": 2, "steps": [
            {"step": "source", "id": "raw"},
            {"step": "process", "id": "p", "processor": "filter", "inputs": ["raw"], "bypass": value}]})


def test_only_process_and_save_steps_can_carry_bypass():
    with pytest.raises(WorkflowError, match="only process and save steps can be bypassed"):
        Workflow.from_dict({"name": "w", "schema": 2, "steps": [{"step": "source", "id": "raw", "bypass": True}]})


@pytest.mark.parametrize("entry, message", [
    ({"name": "r", "target": ["a.b"]}, "unknown key"),
    ({"name": "r", "targets": []}, "non-empty list"),
    ({"name": "r", "targets": "a.b"}, "non-empty list"),
    ({"targets": ["a.b"]}, "needs a name"),
    ({"name": "r", "targets": ["a.b"], "label": 3}, "label must be text"),
    ("radius", "must be an object"),
])
def test_a_control_entry_is_read_strictly(entry, message):
    with pytest.raises(WorkflowError, match=message):
        Workflow.from_dict({"name": "w", "schema": 2, "controls": [entry], "steps": []})


# -- recipe rules -----------------------------------------------------------------------

@pytest.mark.parametrize("control, message", [
    (Control("1radius", ["smooth.radius"]), "bad control name '1radius'"),
    (Control("split", ["smooth.radius"]), "has the name of a step"),
    (Control("r", ["smooth"]), "bad target 'smooth'; expected 'step.param'"),
    (Control("r", ["smooth.radius", "smooth.radius"]), "names target 'smooth.radius' twice"),
    (Control("r", ["nowhere.radius"]), "target 'nowhere.radius' names no step"),
    (Control("r", ["out.fmt"]), "is a save step; a control sets parameters of reconstruct and process steps"),
    (Control("r", ["smooth.size"]), "'filter' has no parameter 'size'"),
    (Control("r", ["smooth.radius", "sharp.method"]), "one value cannot fit them"),
])
def test_each_control_rule_is_refused_with_its_message(registry, control, message):
    messages = _messages(_diamond(controls=[control]), registry)
    assert any(message in m for m in messages), messages


def test_two_controls_cannot_set_one_parameter(registry):
    messages = _messages(_diamond(controls=[RADIUS, Control("r2", ["smooth.radius"])]), registry)
    assert any("is set by two controls, 'radius' and 'r2'" in m for m in messages), messages


def test_targets_that_disagree_are_refused_with_both_values(registry):
    messages = _messages(_diamond(controls=[RADIUS], sharp_radius=1.0), registry)
    assert any("its targets disagree (smooth.radius = 2.0, sharp.radius = 1.0)" in m for m in messages), messages


def test_code_and_python_ports_cannot_be_controls(registry):
    workflow = Workflow("py", [
        Source("raw"), Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("script", "python", {"code": "out = data\n", "ports": "out"}, inputs=["rec"]),
    ], controls=[Control("code", ["script.code"]), Control("ports", ["script.ports"])])
    messages = _messages(workflow, registry)
    assert any("is a code parameter" in m for m in messages), messages
    assert any("a Python step's ports are its outputs" in m for m in messages), messages


def test_a_valid_recipe_has_no_issues(registry):
    assert _messages(_diamond(controls=[RADIUS, METHOD]), registry) == []


def test_bypass_rules(registry):
    # A refused bypass is reported once, by itself: the step stays in for the
    # rest of validation, so its readers are not reported as dangling.
    assert _messages(_diamond(bypass=("merge",)), registry) == [
        "merge: cannot be bypassed: it reads 2 inputs, and a bypassed step passes exactly one through"]
    assert _messages(_diamond(bypass=("split",)), registry) == [
        "split: cannot be bypassed: 'smooth' reads its port 'C0', which nothing would stand for",
        "split: cannot be bypassed: 'sharp' reads its port 'C1', which nothing would stand for"]


def test_a_bypassed_step_need_not_have_its_plugin(registry):
    workflow = _diamond(controls=[Control("gone", ["ghost.level"])])
    workflow.steps.insert(5, Process("ghost", "not-installed", {"level": 2}, inputs=["smooth"], bypass=True))
    workflow.step("merge").inputs[0] = workflow.steps[5].inputs[0].__class__("ghost")
    assert _messages(workflow, registry) == []
    assert recipe_notes(workflow, registry) == ["control 'gone' has no effect while ghost is bypassed"]


# -- the value contract --------------------------------------------------------------------

def _spec(kind, **kw):
    return CombinedField(type=kind, **kw)


@pytest.mark.parametrize("kind, text, expected", [
    ("int", "007", 7), ("int", "+3", 3), ("int", "-2", -2),
    ("float", "3", 3.0), ("float", "2.5", 2.5), ("float", "-1e-3", -0.001), ("float", ".5", 0.5),
    ("bool", "true", True), ("bool", "FALSE", False),
    ("text", "007", "007"), ("text", "on", "on"), ("text", "2026-11-02", "2026-11-02"),
    ("text", "''", ""), ("text", "'null'", "null"), ("text", "'it''s'", "it's"), ("path", "a b", "a b"),
])
def test_text_is_converted_by_the_field_not_by_yaml(kind, text, expected):
    value = convert_text(text, _spec(kind))
    assert value == expected and type(value) is type(expected)


@pytest.mark.parametrize("kind, text, reason", [
    ("int", "2.0", "not a whole number"), ("int", "seven", "not a whole number"),
    ("float", "nan", "not a number"), ("float", "inf", "not a number"), ("float", "-inf", "not a number"),
    ("float", "1e999", "too large"), ("float", "'3'", "quotes make text"),
    ("bool", "on", "not true or false"), ("bool", "yes", "not true or false"), ("bool", "1", "not true or false"),
    ("text", "", "no value"), ("text", "null", "null is not allowed"), ("text", "'a'b'", "bad quoting"),
])
def test_text_the_contract_refuses(kind, text, reason):
    with pytest.raises(ValueError, match=reason):
        convert_text(text, _spec(kind))


def test_null_needs_every_target_nullable_and_select_matches_text():
    assert convert_text("null", _spec("float", nullable=True)) is None
    numbers = _spec("select", options=(1, 2, 3))
    assert convert_text("1", numbers) == 1 and type(convert_text("1", numbers)) is int
    with pytest.raises(ValueError, match="not one of 1, 2, 3"):
        convert_text("01", numbers)
    words = _spec("select", options=("gaussian", "median"))
    assert convert_text("'median'", words) == "median"


def test_typed_values_are_made_canonical():
    assert check_typed(3, _spec("float")) == 3.0 and type(check_typed(3, _spec("float"))) is float
    assert check_typed(np.int64(3), _spec("int")) == 3
    for bad, kind in ((True, "int"), (True, "float"), (float("nan"), "float"), (1, "bool"), (3, "text")):
        with pytest.raises(ValueError):
            check_typed(bad, _spec(kind))
    assert check_typed(2, _spec("select", options=(1, 2))) == 2


def test_fields_combine_by_the_rules():
    def field(key, **kw):
        return ParamField(key=key, type=kw.pop("type", "float"), default=kw.pop("default", 1.0), **kw)

    control = Control("c", ["a.x", "b.x"])
    combined, problems = combine(control, [("a.x", field("x", min=0.0, max=10.0, decimals=1, step=0.5)),
                                           ("b.x", field("x", min=2.0, max=20.0, decimals=3, step=0.1))])
    assert problems == [] and (combined.min, combined.max) == (2.0, 10.0)
    assert (combined.decimals, combined.step) == (3, 0.1) and combined.has_reset and combined.reset == 1.0
    _c, problems = combine(control, [("a.x", field("x", min=0.0, max=1.0)), ("b.x", field("x", min=2.0, max=3.0))])
    assert any("no value fits all its targets" in p for p in problems)
    _c, problems = combine(control, [("a.x", field("x", suffix="px")), ("b.x", field("x", suffix="µm"))])
    assert any("different units" in p for p in problems)
    combined, _p = combine(control, [("a.x", field("x", nullable=True, default=None)),
                                     ("b.x", field("x", default=2.0))])
    assert combined.nullable is False and combined.has_reset is False


# -- preparation ------------------------------------------------------------------------------

def test_a_schema_1_recipe_is_prepared_unchanged(registry):
    recipe = _diamond()
    prepared = prepare_run(recipe, registry=registry)
    assert prepared.workflow.to_dict() == recipe.to_dict() and prepared.bypassed == ()


def test_values_are_written_into_every_active_target_and_definitions_dropped(registry):
    recipe = _diamond(controls=[RADIUS, METHOD])
    prepared = prepare_run(recipe, registry=registry, values={"radius": ControlText("3"), "method": "mean"})
    wf = prepared.workflow
    assert wf.controls == [] and wf.required_schema == 1
    assert wf.step("smooth").params["radius"] == 3.0 and wf.step("sharp").params["radius"] == 3.0
    assert wf.step("smooth").params["method"] == "mean"
    assert prepared.values == {"radius": 3.0, "method": "mean"}
    assert recipe.step("smooth").params["radius"] == 2.0          # the recipe is not changed


def test_a_shared_control_across_two_branches_survives_a_partial_run(registry):
    recipe = _diamond(controls=[RADIUS, METHOD])
    prepared = prepare_run(recipe, registry=registry, subset=["smooth"], values={"radius": 4})
    ids = [step.id for step in prepared.workflow.steps]
    assert ids == ["raw", "rec", "split", "smooth"]
    assert prepared.workflow.step("smooth").params["radius"] == 4.0
    # A control whose targets are all outside the run: fine without a value...
    only_sharp = prepare_run(recipe, registry=registry, subset=["sharp"])
    assert "method" in only_sharp.inactive
    # ...and an error with one, saying why.
    with pytest.raises(ControlValueError, match=r"smooth.method \(outside this run\)"):
        prepare_run(recipe, registry=registry, subset=["sharp"], values={"method": "mean"})


def test_a_value_for_a_control_with_no_active_target_is_refused(registry):
    recipe = _diamond(controls=[METHOD], bypass=("smooth",))
    with pytest.raises(ControlValueError, match=r"no active target.*smooth.method \(bypassed\)"):
        prepare_run(recipe, registry=registry, values={"method": "mean"})
    with pytest.raises(ControlValueError, match="no such control"):
        prepare_run(recipe, registry=registry, values={"nope": 1})


def test_bypassed_steps_are_taken_out_and_their_readers_rewired(registry):
    recipe = _diamond(bypass=("smooth", "out"))
    recipe.steps.insert(4, Process("smooth2", "filter", inputs=["smooth"], bypass=True))
    recipe.step("merge").inputs = [recipe.step("merge").inputs[0].__class__("smooth2"),
                                   recipe.step("merge").inputs[1]]
    prepared = prepare_run(recipe, registry=registry)
    assert prepared.bypassed == ("smooth", "smooth2", "out")
    assert [str(ref) for ref in prepared.workflow.step("merge").inputs] == ["split.C0", "sharp"]
    assert [step.id for step in prepared.workflow.steps] == ["raw", "rec", "split", "sharp", "merge"]
    # Requesting a bypassed step runs what it passes through; a bypassed
    # Save, what it would have saved.
    assert [s.id for s in prepare_run(recipe, registry=registry, subset=["smooth"]).workflow.steps] == [
        "raw", "rec", "split"]
    assert [s.id for s in prepare_run(recipe, registry=registry, subset=["out"]).workflow.steps] == [
        "raw", "rec", "split", "sharp", "merge"]


def test_a_partial_run_is_not_blocked_by_problems_outside_it(registry):
    # A control target and a bypass on 'smooth' that the rules refuse: a run
    # of 'sharp' does not include smooth, so neither stops it; the whole run does.
    recipe = _diamond(controls=[RADIUS], bypass=("smooth",))
    recipe.step("smooth").processor = "projection"                    # no 'radius' any more
    recipe.step("smooth").params = {}
    recipe.step("smooth").inputs.append(recipe.step("smooth").inputs[0].__class__("split", "C1"))
    prepared = prepare_run(recipe, registry=registry, subset=["sharp"])
    assert [s.id for s in prepared.workflow.steps] == ["raw", "rec", "split", "sharp"]
    with pytest.raises(WorkflowError) as whole:
        prepare_run(recipe, registry=registry)
    assert "cannot be bypassed: it reads 2 inputs" in str(whole.value)
    assert "'projection' has no parameter 'radius'" in str(whole.value)


def test_a_value_that_cannot_be_checked_is_refused_not_dropped():
    # Without a registry no field is known: the value must not be silently
    # left out of the run.
    with pytest.raises(ControlValueError, match="cannot be set: no installed plugin declares the field of "
                                                "smooth.radius, sharp.radius"):
        prepare_run(_diamond(controls=[RADIUS]), values={"radius": 3.0})


def test_a_binding_cannot_name_a_bypassed_step(registry, recording):
    recipe = _diamond(bypass=("smooth",))
    stand_in = SimpleNamespace(axis_labels=["Y", "X"], data=np.zeros((4, 4)))
    with pytest.raises(WorkflowError, match="smooth: is bypassed and cannot be bound; bind 'split', which it passes through"):
        prepare_run(recipe, registry=registry, bindings={"smooth": stand_in})


def test_a_value_that_changes_declared_ports_is_refused_before_the_run(registry):
    recipe = _diamond(controls=[Control("axis", ["split.axis"])])
    assert _messages(recipe, registry) == []
    with pytest.raises(WorkflowError, match="port 'C0' is not one 'split' produces"):
        prepare_run(recipe, registry=registry, values={"axis": ControlText("Z")})
    recipe.step("split").params["axis"] = "Z"
    assert any("port 'C0' is not one 'split' produces" in m for m in _messages(_diamond_with_axis("Z"), registry))


def _diamond_with_axis(axis):
    workflow = _diamond()
    workflow.step("split").params["axis"] = axis
    return workflow


def test_a_data_dependent_port_fails_at_the_reference(registry, recording, tmp_path):
    workflow = _diamond(source=recording)
    workflow.step("split").params["axis"] = "Auto"
    workflow.step("sharp").inputs[0] = workflow.step("sharp").inputs[0].__class__("split", "C5")
    assert _messages(workflow, registry) == []
    with pytest.raises(Exception, match=r"split.C5: no such result"):
        run(workflow, registry=registry, out_dir=tmp_path)


# -- runs: the same run as the plain workflow it stands for -------------------------------

def _merged(report):
    return np.asarray(report.result("merge").data)


def test_a_control_value_is_the_same_run_as_writing_it_into_the_steps(registry, recording, tmp_path):
    recipe = _diamond(controls=[RADIUS], source=recording)
    with run(recipe, registry=registry, out_dir=tmp_path / "a", controls={"radius": ControlText("3")}) as a:
        plain = _diamond(source=recording, sharp_radius=3.0)
        plain.step("smooth").params["radius"] = 3.0
        with run(plain, registry=registry, out_dir=tmp_path / "b") as b:
            assert np.array_equal(_merged(a), _merged(b))
            params = [node.get("params", {}).get("radius") for node in graph_of(a.result("merge"))["nodes"].values()
                      if node.get("plugin_id") == "filter"]
            assert sorted(params) == [3.0, 3.0]
        assert a.controls == {"radius": 3.0}


def test_a_bypassed_step_is_the_same_run_as_wiring_around_it(registry, recording, tmp_path):
    recipe = _diamond(bypass=("smooth",), source=recording)
    with run(recipe, registry=registry, out_dir=tmp_path / "a") as a:
        plain = _diamond(source=recording)
        plain.steps.remove(plain.step("smooth"))
        plain.step("merge").inputs[0] = plain.step("merge").inputs[0].__class__("split", "C0")
        with run(plain, registry=registry, out_dir=tmp_path / "b") as b:
            assert np.array_equal(_merged(a), _merged(b))
        assert a.bypassed == ["smooth"] and "smooth.out" not in a.results
        filters = [n for n in graph_of(a.result("merge"))["nodes"].values() if n.get("plugin_id") == "filter"]
        assert [n["params"]["method"] for n in filters] == ["median"]


def test_dropping_the_controls_computes_the_same(registry, recording, tmp_path):
    with run(_diamond(controls=[RADIUS], source=recording), registry=registry, out_dir=tmp_path / "a") as a:
        with run(_diamond(source=recording), registry=registry, out_dir=tmp_path / "b") as b:
            assert np.array_equal(_merged(a), _merged(b))


def test_a_bypassed_save_writes_nothing(registry, recording, tmp_path):
    out_dir = tmp_path / "out"
    with run(_diamond(bypass=("out",), source=recording), registry=registry, out_dir=out_dir) as report:
        assert report.bypassed == ["out"] and report.receipts == []
        assert not out_dir.exists() or not any(out_dir.iterdir())


def test_a_bound_consumer_of_a_bypassed_step_skips_its_ancestors(registry, recording, tmp_path):
    with run(_diamond(source=recording), registry=registry, out_dir=tmp_path / "pre") as first:
        blurred = first.result("sharp")
        # No source path: the run fails if 'raw' is opened, so binding the
        # consumer of a bypassed step must skip everything upstream of it.
        recipe = _diamond(bypass=("smooth",))
        with run(recipe, registry=registry, out_dir=tmp_path / "b", bindings={"merge": blurred},
                 write_saves=False) as report:
            assert report.bound == ["merge.out"] and report.bypassed == ["smooth"]
            assert report.result("merge") is blurred


def test_the_gui_publishes_each_result_once(registry, recording, tmp_path):
    from imswitch.improcess.controller.WorkflowController import WorkflowController

    published = []
    host = SimpleNamespace(_commChannel=SimpleNamespace(
        sigResultProduced=SimpleNamespace(emit=lambda result, name: published.append((name, result)))))
    with run(_diamond(bypass=("smooth",), source=recording), registry=registry, out_dir=tmp_path / "a") as report:
        WorkflowController._publish(host, report)
        names = [name for name, _r in published]
        assert "diamond:smooth.out" not in names
        assert len({id(result) for _n, result in published}) == len(published)
    # A result bound from the list that a bypassed step passes on is not
    # republished through it: 'smooth' is bypassed and reads the bound split.C0.
    with run(_diamond(source=recording), registry=registry, out_dir=tmp_path / "pre") as first:
        listed = {port: first.result(f"split.{port}") for port in first.ports_of("split")}
        published.clear()
        recipe = _diamond(bypass=("smooth",))
        with run(recipe, registry=registry, out_dir=tmp_path / "b", bindings={"split": listed}) as report:
            assert report.bound == ["split.C0", "split.C1"] and report.bypassed == ["smooth"]
            WorkflowController._publish(host, report)
            assert all(result is not bound for _n, result in published for bound in listed.values())
            assert [name for name, _r in published] == ["diamond:sharp.out", "diamond:merge.out"]


# -- batch and command line -------------------------------------------------------------------

def _manifest(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_values_take_file_then_run_wide_then_row_precedence(registry, tmp_path):
    first = str(write_synthetic_recording(tmp_path / "a.h5", frames=2, axes="CYX"))
    second = str(write_synthetic_recording(tmp_path / "b.h5", frames=2, axes="CYX", seed=4))
    recipe = _diamond(controls=[RADIUS])
    manifest = _manifest(tmp_path / "m.csv", [{"raw": first, "radius": "", "notes": "x"},
                                              {"raw": second, "radius": "4", "notes": "y"}])
    rows = rows_from_manifest(manifest, recipe)
    assert rows[0].warnings == ["manifest column 'notes' is neither a source nor a control; ignored"]
    batch = run_over(recipe, rows, registry=registry, out_dir=tmp_path / "out", overwrite=True,
                     controls={"radius": ControlText("3")})
    assert batch.ok
    assert [row.controls["radius"] for row in batch.rows] == ["3.0", "4.0"]
    summary = list(csv.DictReader(open(batch.write_summary(tmp_path / "s.csv"), encoding="utf-8")))
    assert [r["control:radius"] for r in summary] == ["3.0", "4.0"] and summary[0]["bypassed"] == ""


def test_a_bad_cell_fails_its_row_only(registry, tmp_path):
    first = str(write_synthetic_recording(tmp_path / "a.h5", frames=2, axes="CYX"))
    recipe = _diamond(controls=[RADIUS])
    manifest = _manifest(tmp_path / "m.csv", [{"raw": first, "radius": "abc"}, {"raw": first, "radius": "2.5"}])
    batch = run_over(recipe, rows_from_manifest(manifest, recipe), registry=registry, out_dir=tmp_path / "out",
                     overwrite=True)
    assert [row.ok for row in batch.rows] == [False, True]
    assert batch.rows[0].error == "line 2, column radius: 'abc' is not a number (nan and inf are not accepted)"


def test_summary_values_read_back_as_what_they_are():
    from imswitch.improcess.workflows.prepare import convert_text, format_value

    text = _spec("text")
    for value in ["", "null", "'quoted'", " padded ", "it's", "plain"]:
        assert convert_text(format_value(value), text) == value
    assert format_value(None) == "null" and format_value(True) == "true" and format_value(2.5) == "2.5"


def test_an_invalid_recipe_with_controls_fails_each_row_as_before(registry, tmp_path):
    first = str(write_synthetic_recording(tmp_path / "a.h5", frames=2, axes="CYX"))
    recipe = _diamond(controls=[RADIUS])
    recipe.step("merge").inputs[0] = recipe.step("merge").inputs[0].__class__("nowhere")
    batch = run_over(recipe, [{"raw": first}, {"raw": first}], registry=registry, out_dir=tmp_path / "out")
    assert [row.ok for row in batch.rows] == [False, False]
    assert "workflow is invalid" in batch.rows[0].error


def test_an_inactive_control_set_for_the_batch_refuses_it(registry, tmp_path):
    first = str(write_synthetic_recording(tmp_path / "a.h5", frames=2, axes="CYX"))
    recipe = _diamond(controls=[METHOD], bypass=("smooth",))
    with pytest.raises(ControlValueError, match="no active target"):
        run_over(recipe, [{"raw": first}], registry=registry, out_dir=tmp_path / "out",
                 controls={"method": ControlText("mean")})
    manifest = _manifest(tmp_path / "m.csv", [{"raw": first, "method": ""}, {"raw": first, "method": "mean"}])
    with pytest.raises(ControlValueError, match=r"the manifest sets it \(line 3\)"):
        run_over(recipe, rows_from_manifest(manifest, recipe), registry=registry, out_dir=tmp_path / "out")


def test_the_command_line_sets_lists_and_validates_controls(tmp_path, capsys, recording):
    from imswitch.improcess.workflows.__main__ import main

    path = tmp_path / "wf.yaml"
    _diamond(controls=[RADIUS, METHOD]).save(path)
    assert main(["--no-user-plugins", "run", str(path), "--input", recording, "--out", str(tmp_path / "out"),
                 "--set", "radius=3", "--set", "method=mean"]) == 0
    summary = next((tmp_path / "out").glob("*_summary.csv"))
    record = next(csv.DictReader(open(summary, encoding="utf-8")))
    assert record["control:radius"] == "3.0" and record["control:method"] == "mean"
    capsys.readouterr()
    assert main(["--no-user-plugins", "controls", str(path), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [entry["name"] for entry in listed] == ["radius", "method"]
    assert listed[0]["type"] == "float" and listed[0]["value"] == 2.0 and listed[0]["min"] == 0.1
    assert listed[1]["options"] == ["gaussian", "median", "mean", "unsharp"]
    assert main(["--no-user-plugins", "validate", str(path), "--set", "radius=abc"]) == 1
    assert main(["--no-user-plugins", "validate", str(path), "--set", "radius"]) == 2
    assert main(["--no-user-plugins", "run", str(path), "--input", recording, "--out", str(tmp_path / "x"),
                 "--set", "nope=1"]) == 2


def test_describe_controls_says_which_targets_run(registry):
    described = describe_controls(_diamond(controls=[METHOD], bypass=("smooth",)), registry)
    assert described[0]["active"] is False
    assert described[0]["targets"] == [{"target": "smooth.method", "active": False, "reason": "bypassed"}]


# -- replay -------------------------------------------------------------------------------------

def test_replay_of_a_controlled_bypassed_run_is_a_plain_schema_1_workflow(registry, recording, tmp_path):
    from imswitch.improcess.workflows import workflow_from_file

    recipe = _diamond(controls=[RADIUS], bypass=("smooth",), source=recording)
    with run(recipe, registry=registry, out_dir=tmp_path, controls={"radius": 3.5}) as report:
        saved = report.receipts[0].files[0]
    replayed = workflow_from_file(saved, registry=registry).workflow
    assert replayed.required_schema == 1 and replayed.controls == []
    filters = [step for step in replayed.steps if isinstance(step, Process) and step.processor == "filter"]
    assert [step.params.get("radius") for step in filters] == [3.5]
    assert [step.params.get("method") for step in filters] == ["median"]
