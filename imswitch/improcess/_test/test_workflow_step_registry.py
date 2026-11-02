"""The step kinds are an open set, and a run can keep state per step.

A workflow file names its steps by kind (``step: process``); the kinds used to
be a closed dict in ``steps.py``. Now a package registers a kind and
``step_from_dict``, ``validate`` and the runner find it; the five built-ins
are registered at import, so a file written before the registry loads as it
did. ``run(step_state=...)`` hands a Process step's processor one mutable
dict per step id, the same object run after run, and never writes it down.
"""

from dataclasses import dataclass, field

import numpy as np
import pytest

from imswitch.improcess.model import ArrayProcessingResult
from imswitch.improcess.model.provenance import graph_of, output_of
from imswitch.improcess.processors.base import Processor
from imswitch.improcess.workflows import Process, Reconstruct, Source, Workflow, bootstrap_registry, run
from imswitch.improcess.workflows.runner import STATE_PARAM
from imswitch.improcess.workflows.steps import (
    Consolidate,
    Save,
    WorkflowError,
    _STEP_TYPES,
    parse_ref,
    register_step_type,
    step_from_dict,
    step_kinds,
    step_type,
    unregister_step_type,
    validate,
)

BUILTINS = ("source", "reconstruct", "consolidate", "process", "save")


@pytest.fixture
def clean_registry():
    """Whatever a test registers is forgotten afterwards."""
    before = step_kinds()
    yield
    for kind in step_kinds():
        if kind not in before:
            unregister_step_type(kind)


# -- the registry ---------------------------------------------------------------------------

def test_the_five_built_ins_are_registered_and_know_their_kind():
    assert step_kinds()[:5] == BUILTINS
    for kind, cls in zip(BUILTINS, (Source, Reconstruct, Consolidate, Process, Save)):
        assert step_type(kind) is cls and cls.kind == kind
    # the former closed dict is still readable, as a live read-only view
    assert dict(_STEP_TYPES)["process"] is Process
    with pytest.raises(TypeError):
        _STEP_TYPES["other"] = Process


def test_an_unknown_kind_is_refused_with_the_kind_in_the_message():
    with pytest.raises(WorkflowError, match="unknown step kind 'teleport'"):
        step_from_dict({"step": "teleport", "id": "t"})
    with pytest.raises(WorkflowError, match="'teleport'"):
        step_type("teleport")
    with pytest.raises(WorkflowError, match="unknown step kind 'teleport'"):
        Workflow.from_dict({"name": "w", "steps": [{"step": "teleport", "id": "t"}]})


@dataclass
class _Trigger:
    """A custom step: waits for a condition on an earlier step's output."""

    id: str
    condition: str = ""
    inputs: list = field(default_factory=list)
    ports = ("fired",)

    def __post_init__(self):
        self.inputs = [parse_ref(r) for r in (self.inputs or [])]

    def to_dict(self) -> dict:
        return {"step": "trigger", "id": self.id, "condition": self.condition, "inputs": [str(r) for r in self.inputs]}


def test_a_registered_kind_is_built_listed_and_round_trips(clean_registry):
    register_step_type("trigger", _Trigger)
    assert "trigger" in step_kinds() and step_type("trigger") is _Trigger
    assert _Trigger.kind == "trigger"                 # told its kind, since it did not name one
    step = step_from_dict({"step": "trigger", "id": "t", "condition": "count > 3", "inputs": ["rec"]})
    assert isinstance(step, _Trigger) and step.condition == "count > 3" and str(step.inputs[0]) == "rec"
    workflow = Workflow("w", [Source("raw"), Reconstruct("rec", "view-only", inputs=["raw"]), step])
    again = Workflow.from_yaml(workflow.to_yaml())
    assert again.to_dict() == workflow.to_dict()
    # validation follows the custom step's references and declared ports
    assert validate(workflow) == []
    bad = Workflow("w", [Source("raw"), Reconstruct("rec", "view-only", inputs=["raw"]), step,
                         Process("p", "filter", inputs=["t.missing"])])
    assert any("port 'missing'" in str(issue) for issue in validate(bad))
    ok = Workflow("w", [Source("raw"), Reconstruct("rec", "view-only", inputs=["raw"]), step,
                        Process("p", "filter", inputs=["t.fired"])])
    assert validate(ok) == []


def test_a_class_naming_another_kind_is_refused_and_built_ins_stay(clean_registry):
    class _Mismatch:
        kind = "other"

    with pytest.raises(WorkflowError, match="'other'"):
        register_step_type("trigger", _Mismatch)
    with pytest.raises(WorkflowError, match="built-in"):
        unregister_step_type("process")
    assert step_kinds()[:5] == BUILTINS


def test_a_registered_kind_runs_through_its_run_callable(clean_registry):
    seen = []

    def run_trigger(step, *, report, registry, logger):
        seen.append((step.id, step.condition, sorted(report.results)))
        return {"fired": report.result(step.inputs[0])}

    register_step_type("trigger", _Trigger, run=run_trigger)
    registry = bootstrap_registry(user_plugins=False)
    image = ArrayProcessingResult("img", np.ones((4, 4), np.float32), ["Y", "X"])
    workflow = Workflow("w", [Source("raw"), Reconstruct("rec", "view-only", inputs=["raw"]),
                              _Trigger("t", "count > 3", inputs=["rec"])])
    with run(workflow, registry=registry, bindings={"rec": image}) as report:
        assert seen == [("t", "count > 3", ["rec.out"])]
        assert report.result("t.fired") is image and report.steps_run == ["raw", "rec", "t"]


def test_a_registered_kind_without_a_runner_cannot_run(clean_registry):
    register_step_type("trigger", _Trigger)
    registry = bootstrap_registry(user_plugins=False)
    image = ArrayProcessingResult("img", np.ones((4, 4), np.float32), ["Y", "X"])
    workflow = Workflow("w", [Source("raw"), Reconstruct("rec", "view-only", inputs=["raw"]),
                              _Trigger("t", inputs=["rec"])])
    with pytest.raises(Exception, match="unknown step kind 'trigger'"):
        run(workflow, registry=registry, bindings={"rec": image})


# -- a file written before the registry loads unchanged --------------------------------------

_OLD_FILE = """\
name: before-the-registry
schema: 1
description: written by hand, the way the examples were
steps:
  - {step: source, id: raw, path: cells.h5, dataset: data}
  - {step: reconstruct, id: rec, reconstructor: view-only, inputs: [raw]}
  - {step: process, id: split, processor: stack-split, params: {axis: C}, inputs: [rec]}
  - {step: process, id: smooth, processor: filter, params: {method: gaussian, radius: 2.0}, inputs: [split.C0]}
  - {step: consolidate, id: both, reconstructor: view-only, inputs: [rec]}
  - {step: save, id: out, input: smooth, fmt: hdf5, path_template: "{out_dir}/{source_stem}_x{ext}"}
"""


def test_a_workflow_saved_before_the_registry_loads_and_round_trips(tmp_path):
    path = tmp_path / "old.yaml"
    path.write_text(_OLD_FILE, encoding="utf-8")
    workflow = Workflow.load(path)
    assert [type(s) for s in workflow.steps] == [Source, Reconstruct, Process, Process, Consolidate, Save]
    assert [s.kind for s in workflow.steps] == ["source", "reconstruct", "process", "process", "consolidate", "save"]
    assert workflow.step("raw").source.path == "cells.h5" and workflow.step("raw").source.dataset == "data"
    assert workflow.step("smooth").params == {"method": "gaussian", "radius": 2.0}
    assert str(workflow.step("smooth").inputs[0]) == "split.C0"
    assert workflow.step("out").path_template == "{out_dir}/{source_stem}_x{ext}"
    saved = workflow.save(tmp_path / "again.yaml")
    assert Workflow.load(saved).to_dict() == workflow.to_dict()
    assert Workflow.from_json(workflow.to_json()).to_dict() == workflow.to_dict()


# -- per-step state -------------------------------------------------------------------------

class _Counter(Processor):
    """Counts its runs in the state it is handed, and reports what it saw."""

    name = "Counter"
    id = "test.counter"
    seen: list = []

    @classmethod
    def default_params(cls):
        return {"gain": 1.0}

    @property
    def applies_to(self):
        return lambda result: True

    def apply(self, result, params):
        _Counter.seen.append(params)
        state = params.get(STATE_PARAM)
        if state is not None:
            state["runs"] = state.get("runs", 0) + 1
        return ArrayProcessingResult("counted", np.asarray(result.data) * params["gain"], list(result.axis_labels))

    def make_param_widget(self, parent):
        return None


@pytest.fixture
def counter():
    registry = bootstrap_registry(user_plugins=False)
    registry.register_processor(_Counter())
    _Counter.seen = []
    return registry


def _counting_workflow():
    return Workflow("w", [Source("raw"), Reconstruct("rec", "view-only", inputs=["raw"]),
                          Process("count", "test.counter", {"gain": 2.0}, inputs=["rec"])])


def test_the_same_state_dict_reaches_the_processor_on_consecutive_runs(counter, tmp_path):
    image = ArrayProcessingResult("img", np.ones((4, 4), np.float32), ["Y", "X"])
    state: dict = {}
    for expected in (1, 2):
        with run(_counting_workflow(), registry=counter, bindings={"rec": image}, out_dir=tmp_path,
                 step_state=state) as report:
            assert report.result("count").data[0, 0] == 2.0
    assert state == {"count": {"runs": 2}}
    first, second = _Counter.seen
    assert first[STATE_PARAM] is state["count"] and second[STATE_PARAM] is state["count"]
    assert first["gain"] == 2.0


def test_the_state_is_never_written_down(counter, tmp_path):
    image = ArrayProcessingResult("img", np.ones((4, 4), np.float32), ["Y", "X"])
    state: dict = {}
    workflow = _counting_workflow()
    with run(workflow, registry=counter, bindings={"rec": image}, out_dir=tmp_path, step_state=state) as report:
        result = report.result("count")
        node = graph_of(result)["nodes"][output_of(graph_of(result))[0]]
        assert node["params"] == {"gain": 2.0} and node["replayable"] is True
        assert STATE_PARAM not in repr(report.results) and report.failure is None
    assert workflow.step("count").params == {"gain": 2.0}
    assert STATE_PARAM not in workflow.to_yaml()
    # the registry's processor is untouched: a run without state sees no key
    assert type(counter.get_processor("test.counter")) is _Counter


def test_no_state_key_without_step_state(counter, tmp_path):
    image = ArrayProcessingResult("img", np.ones((4, 4), np.float32), ["Y", "X"])
    with run(_counting_workflow(), registry=counter, bindings={"rec": image}, out_dir=tmp_path):
        pass
    (params,) = _Counter.seen
    assert STATE_PARAM not in params and params["gain"] == 2.0


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
