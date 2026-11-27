"""Exposed parameters of a smart pipeline: workflow controls (schema 2) that
may also target a gate's or an action's parameter, with the field the step
kind's descriptor declares; made in the shared editor's document model,
saved and read back, an old file without controls unchanged."""
import json

import pytest

from imswitch.imcommon.model.smartpipeline import (
    Action,
    ActionCatalog,
    ActionSpec,
    Gate,
    Live,
    Sink,
    SmartPipelineDocument,
    Transform,
)
from imswitch.imcommon.model.smartpipeline.descriptors import param_field, pipeline_descriptors
from imswitch.imcommon.model.workflow.prepare import control_states
from imswitch.imcommon.model.workflow.steps import Control, Workflow, WorkflowError
from imswitch.imcommon.model.workfloweditor.catalog import Catalog
from imswitch.imcommon.model.workfloweditor.document import DocumentError, WorkflowDocument

ACTIONS = ActionCatalog([
    ActionSpec("move-to", accepts=("points",), params={"positioner": "", "frame": "", "deadline_s": 10.0},
               frame_param="frame"),
])


def workflow(controls=()):
    return Workflow("p", [
        Live("cam", detector="Camera"),
        Gate("rest", "min-interval", params={"seconds": 5.0}, inputs=["cam"]),
        Transform("tf", "identity", inputs=["rest"]),
        Action("mv", "move-to", {"positioner": "XY", "frame": "stage:XY"}, inputs=["tf"]),
        Sink("log", "event-log", inputs=["rest"]),
    ], controls=list(controls))


def editor_document(flow=None):
    steps = pipeline_descriptors(detectors=("Camera",), transforms=("identity",), actions=ACTIONS)
    catalog = Catalog(registry=None, processors=(), reconstructors=(), save_formats=(), path_placeholders=(),
                      steps=steps)
    return WorkflowDocument(flow or workflow(), catalog=catalog)


def test_a_gate_or_action_parameter_is_a_target_only_with_the_hosts_field_hook():
    flow = workflow([Control("cooldown", ["rest.seconds"]), Control("deadline", ["mv.deadline_s"])])
    # without the hook (ImProcess): only processing and reconstruction parameters
    _states, issues = control_states(flow)
    assert any("is a gate step" in issue.message for issue in issues)
    states, issues = control_states(flow, field_for=lambda step, key: param_field(step, key, ACTIONS))
    assert issues == []
    cooldown, deadline = states["cooldown"], states["deadline"]
    assert cooldown.spec.type == "float" and cooldown.spec.suffix == "s" and cooldown.spec.min == pytest.approx(1e-3)
    assert cooldown.value == 5.0 and cooldown.has_value
    assert deadline.spec.type == "float" and deadline.value == 10.0      # the catalogue's default
    # a parameter the rule does not have is refused, with the reason
    _states, issues = control_states(Workflow("p", workflow().steps, controls=[Control("x", ["rest.count"])]),
                                     field_for=lambda step, key: param_field(step, key, ACTIONS))
    assert any("has no parameter 'count'" in issue.message for issue in issues)


def test_the_designer_exposes_renames_relabels_and_stops_exposing_a_gate_parameter():
    doc = editor_document()
    doc.add_control("cooldown_s", "rest.seconds", label="Cooldown")
    assert doc.exposed("rest") == {"seconds": "cooldown_s"}
    state = doc.control_state("cooldown_s")
    assert state.spec.label == "Cooldown" and state.spec.suffix == "s"
    doc.set_control_value("cooldown_s", 2.5)
    assert doc.step("rest").params["seconds"] == 2.5
    with pytest.raises(DocumentError, match="below"):
        doc.set_control_value("cooldown_s", 0.0)
    assert doc.step("rest").params["seconds"] == 2.5          # refused, unchanged
    doc.rename_control("cooldown_s", "cooldown")
    doc.set_control_label("cooldown", "Rest between scans", "seconds between two triggered scans")
    doc.add_control("deadline", "mv.deadline_s")
    assert doc.step("mv").params["deadline_s"] == 10.0       # the value is written explicitly
    assert doc.issues() == []
    doc.remove_control("deadline")
    assert [c.name for c in doc.controls] == ["cooldown"] and doc.step("mv").params["deadline_s"] == 10.0


def test_controls_round_trip_through_the_file_and_an_old_file_still_loads(tmp_path):
    doc = editor_document()
    doc.add_control("cooldown", "rest.seconds", label="Cooldown")
    path = tmp_path / "p.json"
    SmartPipelineDocument(doc.workflow).save(path)
    payload = json.loads(path.read_text())
    assert payload["schema"] == 2
    assert payload["controls"] == [{"name": "cooldown", "targets": ["rest.seconds"], "label": "Cooldown"}]
    loaded = SmartPipelineDocument.load(path)
    assert [c.name for c in loaded.workflow.controls] == ["cooldown"]
    # the controls add no issue to what the pipeline check says without them
    old = workflow().to_dict()
    plain = [str(issue) for issue in SmartPipelineDocument.from_dict(old).validate(actions=ACTIONS)]
    assert [str(issue) for issue in loaded.validate(actions=ACTIONS)] == plain
    # an old file: schema 1, no controls -- loads as before
    assert old["schema"] == 1 and "controls" not in old
    assert SmartPipelineDocument.from_dict(old).workflow.controls == []
    # and a schema-1 file that names controls is refused, as for any workflow
    with pytest.raises(WorkflowError, match="schema: 2"):
        SmartPipelineDocument.from_dict({**old, "controls": payload["controls"]})


def test_the_pipeline_check_reports_a_control_that_names_a_missing_step():
    flow = workflow([Control("gone", ["nope.seconds"])])
    issues = SmartPipelineDocument(flow).validate(actions=ACTIONS)
    assert any("names no step" in issue.message for issue in issues)
