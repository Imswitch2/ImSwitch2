"""The Python step's small outputs -- a number, a yes/no, coordinates in a
frame -- the kinds its ports may declare, and the ``state`` a step keeps
between runs of an interactive session."""

import os
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.model.points_result import PointsResult  # noqa: E402
from imswitch.improcess.model.provenance import output_node  # noqa: E402
from imswitch.improcess.model.value_result import EventResult, ValueResult  # noqa: E402
from imswitch.improcess.model.workfloweditor import WorkflowDocument, build_catalog  # noqa: E402
from imswitch.improcess.model.workfloweditor.session import RunSession, execute  # noqa: E402
from imswitch.improcess.processors.python_step import PythonStepProcessor  # noqa: E402
from imswitch.improcess.processors.python_step.context import (  # noqa: E402
    ScriptError,
    build_namespace,
    parse_port_kinds,
    parse_ports,
    run_script,
)
from imswitch.improcess.processors.python_step.processor import STATE_PARAM  # noqa: E402
from imswitch.improcess.workflows import Process, Reconstruct, Source, Workflow, bootstrap_registry, validate  # noqa: E402

_EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "improcess_workflows"
sys.path.insert(0, str(_EXAMPLES))

from _synthetic import write_synthetic_recording  # noqa: E402


def _stack(shape=(3, 4, 4)):
    data = np.arange(int(np.prod(shape)), dtype=np.float32).reshape(shape)
    return ArrayProcessingResult("rec", data, ["Z", "Y", "X"])


def _one(code, ports=("out",), **kwargs):
    results, _printed = run_script(code, [_stack()], ports, **kwargs)
    return results[0] if len(results) == 1 else results


# -- what the code sees -------------------------------------------------------------

def test_the_namespace_offers_the_constructors_and_a_state_dict():
    namespace = build_namespace([_stack()])
    assert {"make_points", "make_value", "make_event", "state"} <= set(namespace)
    assert namespace["state"] == {} and isinstance(namespace["state"], dict)
    shared = {"n": 2}
    assert build_namespace([_stack()], state=shared)["state"] is shared


# -- numbers, events, points -------------------------------------------------------

def test_make_value_gives_a_value_result_named_after_the_port():
    out = _one('out = make_value(3.5, unit="um")')
    assert isinstance(out, ValueResult) and out.kind == "value"
    assert out.value == 3.5 and out.unit == "um"
    assert out.name == "rec (out)"                              # the step's naming, like any output
    assert out.metadata["operation"] == "python" and out.metadata["port"] == "out"


def test_a_plain_python_number_is_a_value_and_a_bool_an_event():
    out = _one("out = 3.5")
    assert isinstance(out, ValueResult) and out.kind == "value" and out.value == 3.5
    out = _one("out = int(data.shape[0])")
    assert isinstance(out, ValueResult) and out.value == 3
    out = _one("out = True")
    assert isinstance(out, EventResult) and out.kind == "event" and out.fired and out.payload == {}
    out = _one("out = bool(data.max() > 1e9)")
    assert isinstance(out, EventResult) and not out.fired


def test_a_numpy_scalar_or_0d_array_still_gets_the_old_error():
    for code in ("out = np.float32(2.0)", "out = data.mean()", "out = np.asarray(3)"):
        with pytest.raises(ScriptError) as caught:
            _one(code)
        message = str(caught.value)
        assert "'out' is a single number, not an array" in message and "print()" in message


def test_an_unwrapped_1d_array_is_still_a_curve():
    out = _one('out = make_result(data.mean(axis=(1, 2)), axes=["Z"])')
    assert out.kind == "curve" and out.data.shape == (3,)


def test_make_points_carries_the_frame_and_the_properties():
    out = _one('out = make_points([[1, 2]], frame="detector:Cam", area=[4])')
    assert isinstance(out, PointsResult) and out.kind == "points"
    assert out.frame == "detector:Cam" and out.frame_index is None
    assert np.array_equal(out.coordinates, [[1.0, 2.0]])
    assert list(out.properties) == ["area"] and out.properties["area"].tolist() == [4]
    assert out.name == "rec (out)"
    out = _one('out = make_points(np.zeros((2, 3)), "stage:XY", frame_index=7, name="spots")')
    assert out.frame == "stage:XY" and out.frame_index == 7 and out.name == "spots"


def test_make_event_takes_its_payload_as_keywords():
    out = _one("out = make_event(True, x=1.5, label='cell')")
    assert isinstance(out, EventResult) and out.fired and out.payload == {"x": 1.5, "label": "cell"}


def test_a_result_that_is_not_one_of_the_small_kinds_is_refused():
    with pytest.raises(ScriptError, match="is a 'image' result, not an array"):
        _one("out = results[0]")


def test_several_ports_may_mix_arrays_and_values():
    out, score, hit = _one(
        'outputs = {"out": data * 2, "score": float(data.mean()), "hit": bool(data.max() > 3)}',
        ports=("out", "score", "hit"),
    )
    assert out.kind == "image" and score.kind == "value" and hit.kind == "event"
    assert [r.name for r in (out, score, hit)] == ["rec (out)", "rec (score)", "rec (hit)"]


# -- declared port kinds ------------------------------------------------------------

def test_a_port_may_declare_its_kind_after_a_colon():
    assert parse_ports("spots:points, score:value") == ("spots", "score")
    assert parse_port_kinds("spots:points, score:value") == {"spots": "points", "score": "value"}
    assert parse_port_kinds("a, b") == {} and parse_ports("a, b") == ("a", "b")
    assert parse_ports(" out : image ") == ("out",) and parse_port_kinds("out:image") == {"out": "image"}
    with pytest.raises(ValueError, match="declares the kind 'bogus'"):
        parse_ports("a:bogus")
    with pytest.raises(ValueError, match="not a valid name"):
        parse_ports("a b:points")
    with pytest.raises(ValueError, match="more than once"):
        parse_ports("a:points, a:value")


def test_output_spec_promises_a_kind_only_when_every_port_declares_the_same_one():
    processor = PythonStepProcessor()
    assert processor.output_spec({"ports": "score:value"}).kind == "value"
    spec = processor.output_spec({"ports": "a:points, b:points"})
    assert spec.ports == ("a", "b") and spec.kind == "points"
    assert processor.output_spec({"ports": "a:points, b:value"}).kind == "any"      # one kind per spec
    assert processor.output_spec({"ports": "a:points, b"}).kind == "any"
    assert processor.output_spec({"ports": "a, b"}).kind == "any"
    assert processor.output_spec({"ports": "a:bogus"}).ports == ()


def test_the_run_checks_a_port_against_the_kind_it_declared():
    processor = PythonStepProcessor()
    out = processor.apply(_stack(), {"code": "out = 2.5", "ports": "out:value"})
    assert out.results[0].kind == "value"
    with pytest.raises(ScriptError, match="declared as 'points' but the code produced a 'value' result"):
        processor.apply(_stack(), {"code": "out = 2.5", "ports": "out:points"})
    with pytest.raises(ScriptError, match="declared as 'value'"):
        run_script("out = data", [_stack()], ("out",), port_kinds={"out": "value"})


# -- state ---------------------------------------------------------------------------

_COUNTING = 'state["n"] = state.get("n", 0) + 1\nout = make_value(state["n"])'
#: Counts too, and passes the image on: a value cannot feed another Python step.
_COUNTING_THROUGH = 'state["n"] = state.get("n", 0) + 1\noutputs = {"out": data, "n": make_value(state["n"])}'


def test_state_counts_across_apply_calls_sharing_one_dict_and_restarts_with_a_fresh_one():
    processor = PythonStepProcessor()
    shared = {}
    first = processor.apply(_stack(), {"code": _COUNTING, "ports": "out", STATE_PARAM: shared})
    second = processor.apply(_stack(), {"code": _COUNTING, "ports": "out", STATE_PARAM: shared})
    assert [first.results[0].value, second.results[0].value] == [1.0, 2.0]
    assert shared == {"n": 2}
    fresh = processor.apply(_stack(), {"code": _COUNTING, "ports": "out", STATE_PARAM: {}})
    assert fresh.results[0].value == 1.0
    # No state at all (a headless run): the code starts from an empty dict each time.
    alone = processor.apply(_stack(), {"code": _COUNTING, "ports": "out"})
    again = processor.apply(_stack(), {"code": _COUNTING, "ports": "out"})
    assert alone.results[0].value == again.results[0].value == 1.0


def test_state_is_never_a_parameter():
    from imswitch.improcess.workflows.runner import STATE_PARAM as RUNNER_STATE_PARAM

    assert STATE_PARAM == RUNNER_STATE_PARAM == "_state"
    assert STATE_PARAM not in PythonStepProcessor.param_keys()
    assert STATE_PARAM not in PythonStepProcessor.default_params()
    assert STATE_PARAM not in {field.key for field in PythonStepProcessor.param_spec()}
    # A workflow file naming it is refused before the run.
    workflow = Workflow("w", [
        Source("raw", path="x.h5"), Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("py", "python", {"code": "out = 1.0", "ports": "out", STATE_PARAM: {}}, inputs=["rec"]),
    ])
    issues = validate(workflow, bootstrap_registry(user_plugins=False))
    assert any("_state" in str(issue) for issue in issues)
    # And the provenance of a result made with state does not record it.
    out = PythonStepProcessor().apply(_stack(), {"code": _COUNTING, "ports": "out", STATE_PARAM: {"n": 4}})
    node = output_node(out.results[0])
    assert node is None or STATE_PARAM not in node.get("params", {})


# -- state in an interactive session ------------------------------------------------------

@pytest.fixture(scope="module")
def catalog():
    return build_catalog(bootstrap_registry(user_plugins=False))


@pytest.fixture
def recording(tmp_path):
    return str(write_synthetic_recording(tmp_path / "cells.h5", frames=2, axes="CYX"))


def _run(session, document, targets=None, **options):
    plan = session.plan(document, targets, **options)
    session.begin(plan)
    report, error = execute(plan, document.catalog.registry, on_step=session.step_done,
                            progress=lambda _i, _n, step_id: session.step_started(step_id))
    summary = session.finish(report, error, document=document)
    assert error is None, error
    return plan, summary


def _counting_document(catalog, recording):
    return WorkflowDocument(Workflow("w", [
        Source("raw", path=recording), Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("count", "python", {"code": _COUNTING_THROUGH, "ports": "out, n:value"}, inputs=["rec"]),
        Process("after", "python", {"code": _COUNTING_THROUGH, "ports": "out, n:value"}, inputs=["count"]),
    ]), catalog=catalog)


def test_a_session_keeps_a_steps_state_across_its_reruns(catalog, recording):
    document = _counting_document(catalog, recording)
    session = RunSession(catalog.registry)
    plan, _ = _run(session, document)
    assert plan.step_state["count"] is session.step_state("count")   # the session's own dict, not a copy
    assert session.result(document, "count", "n").value == 1.0
    assert session.step_state("count") == {"n": 1} and session.step_state("after") == {"n": 1}
    session.rerun_from(document, "count")                         # Re-run from here: state survives
    _run(session, document)
    assert session.result(document, "count", "n").value == 2.0
    # "after" ran again because "count" did (a new realisation), with its own state kept:
    # a nonce upstream is not a change of what "after" is asked to do.
    assert session.result(document, "after", "n").value == 2.0
    session.close()
    assert session.step_state("count") is None


def test_a_change_of_a_steps_parameters_drops_its_state_and_what_was_built_on_it(catalog, recording):
    document = _counting_document(catalog, recording)
    session = RunSession(catalog.registry)
    _run(session, document)
    document.set_param("after", "code", "# changed\n" + _COUNTING_THROUGH)
    _run(session, document)
    assert session.result(document, "count", "n").value == 1.0      # cached: not run again
    assert session.step_state("count") == {"n": 1}
    assert session.result(document, "after", "n").value == 1.0      # restarted
    document.set_param("count", "code", "# changed\n" + _COUNTING_THROUGH)
    _run(session, document)
    assert session.result(document, "count", "n").value == 1.0      # restarted
    # "after" is asked to do something different too -- its input is the output of other
    # code -- so what it accumulated from the old input is dropped with it.
    assert session.result(document, "after", "n").value == 1.0
    # The plan alone changes nothing: only a run begun with it drops the state.
    document.set_param("count", "code", "# changed twice\n" + _COUNTING_THROUGH)
    session.plan(document)
    assert session.step_state("count") == {"n": 1}


def test_a_removed_step_loses_its_state_and_a_renamed_one_keeps_it(catalog, recording):
    document = _counting_document(catalog, recording)
    session = RunSession(catalog.registry)
    _run(session, document)
    document.rename_step("after", "later")
    session.rename_step("after", "later")
    assert session.step_state("later") == {"n": 1} and session.step_state("after") is None
    document.remove_step("later")
    session.plan(document)
    assert session.step_state("later") is None and session.step_state("count") == {"n": 1}


def test_a_batch_run_without_a_session_starts_every_run_afresh(catalog, recording):
    from imswitch.improcess.workflows import run

    document = _counting_document(catalog, recording)
    with run(document.workflow, registry=catalog.registry, write_saves=False) as report:
        assert report.result("count.n").value == 1.0
    with run(document.workflow, registry=catalog.registry, write_saves=False) as report:
        assert report.result("count.n").value == 1.0
