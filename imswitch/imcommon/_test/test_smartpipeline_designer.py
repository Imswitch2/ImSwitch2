"""The editor's document model on registered step kinds, through the
smart-pipeline descriptors: adding, connecting, editing a step's own fields
and parameters, the boxes and cables it draws, what the quick-add offers."""
import pytest

from imswitch.imcommon.model.smartpipeline import (
    Action,
    ActionCatalog,
    ActionSpec,
    Gate,
    Live,
    Sink,
    Transform,
)
from imswitch.imcommon.model.smartpipeline.descriptors import PIPELINE_CATEGORY, pipeline_descriptors
from imswitch.imcommon.model.workflow.steps import Process, Workflow
from imswitch.imcommon.model.workfloweditor.catalog import Catalog
from imswitch.imcommon.model.workfloweditor.document import DocumentError, WorkflowDocument
from imswitch.imcommon.model.workfloweditor.nodes import can_feed, candidates, kinds_of, node_infos
from imswitch.imcommon.model.workfloweditor.session import RunSession

ACTIONS = ActionCatalog([
    ActionSpec("move-to", accepts=("points",), params={"positioner": "", "frame": "", "relative": False},
               frame_param="frame"),
    ActionSpec("log", accepts=("any",), params={"message": ""}, hardware=False),
])


def catalog():
    base = Catalog.empty()
    steps = pipeline_descriptors(detectors=("Camera", "Other"), transforms=("cam2stage",), actions=ACTIONS)
    return Catalog(registry=None, processors=(), reconstructors=(), save_formats=(), path_placeholders=(),
                   steps=steps)


def document(workflow=None):
    return WorkflowDocument(workflow or Workflow("p"), catalog=catalog())


def test_the_catalog_knows_the_pipeline_kinds():
    cat = catalog()
    assert [d.kind for d in cat.steps] == ["live", "transform", "gate", "action", "sink"]
    assert cat.step("gate").name == "Gate" and cat.step("nope") is None
    assert cat.step_categories() == (PIPELINE_CATEGORY,)
    live = cat.step("live")
    assert live.is_source and live.attribute("detector").options == ("Camera", "Other")
    assert cat.step("action").attribute("action").options == ("move-to", "log")
    assert cat.step("sink").produces is False


def test_steps_are_added_with_valid_defaults_and_follow_the_selection():
    doc = document()
    live = doc.add_step("live")
    assert isinstance(live, Live) and live.id == "live1" and live.detector == "Camera"
    gate = doc.add_step("gate", after="live1")
    assert isinstance(gate, Gate) and [str(r) for r in gate.inputs] == ["live1"] and gate.rule == "condition"
    transform = doc.add_step("transform", after="gate1")
    assert isinstance(transform, Transform) and transform.transform == "cam2stage"
    action = doc.add_step("action", after="tran1")
    assert isinstance(action, Action) and action.action == "move-to" and [str(r) for r in action.inputs] == ["tran1"]
    sink = doc.add_step("sink", inputs=["gate1.fired"])
    assert isinstance(sink, Sink) and str(sink.inputs[0]) == "gate1.fired"
    assert [s.id for s in doc.steps] == ["live1", "gate1", "tran1", "acti1", "sink1"]
    with pytest.raises(DocumentError, match="unknown step kind"):
        doc.add_step("rocket")
    # a sink produces nothing, so nothing follows it automatically
    gate2 = doc.add_step("gate", after="sink1")
    assert gate2.inputs == []


def test_own_fields_are_edited_through_the_descriptor_and_checked():
    doc = document()
    doc.add_step("live")
    doc.add_step("gate", after="live1")
    doc.set_attribute("live1", "detector", "Other")
    assert doc.step("live1").detector == "Other"
    with pytest.raises(DocumentError, match="live mode"):
        doc.set_attribute("live1", "mode", "sometimes")
    assert doc.step("live1").mode == "live"          # rolled back
    with pytest.raises(DocumentError, match="no field"):
        doc.set_attribute("live1", "colour", "red")
    # a gate's parameters follow its rule
    doc.set_param("gate1", "min_count", 2)
    assert doc.step("gate1").params == {"min_count": 2}
    doc.set_attribute("gate1", "rule", "count-limit")
    assert doc.step("gate1").rule == "count-limit" and doc.step("gate1").params == {}
    assert doc.effective_params("gate1") == {"count": 1}
    doc.set_param("gate1", "count", 3)
    assert doc.step("gate1").params == {"count": 3}
    doc.set_param("gate1", "count", 1)                # the default is not written
    assert doc.step("gate1").params == {}
    with pytest.raises(DocumentError, match="takes no parameters"):
        doc.set_param("live1", "x", 1)


def test_an_action_offers_the_catalogue_entry_s_parameters():
    doc = document()
    doc.add_step("live")
    doc.add_step("action", after="live1")
    assert doc.defaults_for("acti1") == {"positioner": "", "frame": "", "relative": False}
    doc.set_attribute("acti1", "action", "log")
    assert doc.defaults_for("acti1") == {"message": ""}


def test_ports_cables_and_boxes():
    doc = document()
    doc.add_step("live")
    doc.add_step("gate", after="live1")
    doc.add_step("transform", after="gate1")
    doc.add_step("action", after="tran1")
    doc.add_step("sink", inputs=["gate1.fired"])
    labels = [option.label for option in doc.port_options(None)]
    assert labels == ["live1 (data)", "gate1.out", "gate1.fired", "tran1 (out)", "acti1 (done)"]
    kinds = kinds_of(doc)
    assert kinds["live1"] == "image" and kinds["tran1"] == "points" and kinds["acti1"] == "event"
    infos = node_infos(doc)
    assert infos["live1"].title == "Live detector · Camera" and infos["live1"].inputs == ()
    assert [slot.port for slot in infos["gate1"].outputs] == ["out", "fired"]
    assert [slot.kind for slot in infos["gate1"].outputs] == ["image", "event"]
    assert infos["sink1"].outputs == () and infos["sink1"].title == "Sink · event-log"
    assert infos["acti1"].title == "Action · move-to" and infos["acti1"].category == PIPELINE_CATEGORY
    assert infos["gate1"].summary == ("Rule  condition",)
    assert infos["live1"].summary == ("Detector  Camera", "Mode  live", "Interval  0")
    # cables: a sink produces nothing, a source takes nothing, a transform takes points
    assert not can_feed(doc, "sink1", "gate1") and not can_feed(doc, "gate1", "live1")
    assert can_feed(doc, "gate1", "sink1") and can_feed(doc, "tran1", "acti1")
    assert not can_feed(doc, "live1", "tran1")          # an image into a transform
    with pytest.raises(DocumentError, match="produces nothing"):
        doc.connect("sink1", "gate1")
    with pytest.raises(DocumentError, match="no inputs"):
        doc.connect("gate1", "live1")
    doc.connect("tran1", "acti1", add=True)             # an action takes several inputs
    assert [str(r) for r in doc.step("acti1").inputs] == ["tran1", "tran1"]
    doc.delete_step("gate1")
    assert [s.id for s in doc.steps] == ["live1", "tran1", "acti1", "sink1"]
    assert doc.step("tran1").inputs == [] and doc.step("sink1").inputs == []


def test_quick_add_offers_the_pipeline_kinds_that_take_the_cable():
    cat = catalog()
    everything = {c.step_kind for c in candidates(cat)}
    assert everything == {"source", "save", "live", "transform", "gate", "action", "sink"}
    from_points = {c.step_kind for c in candidates(cat, "points")}
    assert from_points == {"save", "transform", "gate", "action", "sink"}
    from_image = {c.step_kind for c in candidates(cat, "image")}
    assert "transform" not in from_image and "live" not in from_image and "gate" in from_image


def test_a_pipeline_round_trips_through_paste_undo_and_the_session():
    doc = document()
    doc.add_step("live")
    doc.add_step("gate", after="live1")
    doc.add_step("sink", after="gate1")
    doc.set_attribute("gate1", "rule", "once")
    fragment = doc.fragment(["live1", "gate1", "sink1"])
    pasted = doc.paste(fragment)
    assert len(doc.steps) == 6 and {s.kind for s in doc.steps} == {"live", "gate", "sink"}
    assert doc.step(pasted[1]).rule == "once" if hasattr(doc.step(pasted[1]), "rule") else True
    session = RunSession(None)
    prints = session.fingerprints(doc)
    assert set(prints) == {s.id for s in doc.steps}
    doc.set_attribute("gate1", "rule", "busy")
    assert session.fingerprints(doc)["gate1"] != prints["gate1"]
    assert session.states(doc)["gate1"].status == ""


def test_an_improcess_style_catalog_without_descriptors_is_unchanged():
    doc = WorkflowDocument(Workflow("w", [Live("cam", detector="Camera")]), catalog=Catalog.empty())
    assert doc.descriptor_for(doc.step("cam")) is None
    with pytest.raises(DocumentError, match="unknown step kind"):
        doc.add_step("gate")
    infos = node_infos(doc)           # a kind the catalog does not describe still draws
    assert infos["cam"].title == " (not installed)"
    assert {c.step_kind for c in candidates(Catalog.empty())} == {"source", "save"}
