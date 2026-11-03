"""The smart-pipeline document: its step kinds load and save through the
workflow file format, the pipeline rules, frame tags, the coordinate
contract and the two shipped transforms."""
import json

import numpy as np
import pytest

from imswitch.imcommon.model.smartpipeline import (
    Action,
    ActionCatalog,
    ActionSpec,
    CoordinateError,
    Event,
    Frame,
    FrameIdentity,
    FrameTag,
    Gate,
    Identity,
    Live,
    PipelineSettings,
    Points,
    ScaleFlip,
    Sink,
    SmartPipelineDocument,
    Transform,
    TransformRegistry,
    Value,
    apply_transform,
    check_points,
    fired_of,
    kind_of,
    pipeline_kinds,
    points_of,
    transform_from_dict,
    validate_pipeline,
    value_of,
)
from imswitch.imcommon.model.workflow.steps import Process, Source, Workflow, WorkflowError, step_kinds

CAMERA = "detector:Camera"
STAGE = "stage:XY"

CATALOG = ActionCatalog([
    ActionSpec("move-to", accepts=("points",), params={"positioner": "", "frame": "", "relative": False},
               frame_param="frame"),
    ActionSpec("snap", accepts=("event",), params={"detector": ""}),
    ActionSpec("log", accepts=("any",), params={"message": ""}, hardware=False),
])


def transforms():
    return TransformRegistry([
        ScaleFlip("cam2stage", CAMERA, STAGE, pixel_size=(0.1, 0.1)),
        Identity("same", CAMERA),
    ])


def pipeline():
    return Workflow("find the cell", [
        Live("cam", detector="Camera"),
        Process("thr", "threshold", {"method": "otsu"}, inputs=["cam"]),
        Process("rp", "regionprops", {"frame": CAMERA}, inputs=["thr", "cam"]),
        Process("pk", "pick", {"rule": "largest"}, inputs=["rp"]),
        Gate("g", "condition", inputs=["pk"]),
        Transform("tf", "cam2stage", inputs=["g"]),
        Action("mv", "move-to", {"positioner": "XY", "frame": STAGE}, inputs=["tf"]),
        Action("sn", "snap", {"detector": "Camera"}, inputs=["mv"]),
        Sink("log", "event-log", inputs=["g.fired"]),
    ])


# -- the file ---------------------------------------------------------------------

def test_the_pipeline_step_kinds_are_registered():
    assert {"live", "transform", "gate", "action", "sink"} <= set(step_kinds())


def test_a_pipeline_round_trips_through_json_and_yaml(tmp_path):
    document = SmartPipelineDocument(pipeline())
    document.settings = PipelineSettings(roles={"scouting": "widefield", "idle": "off"}, replay_interval_s=0.5)

    for suffix in (".json", ".yaml"):
        path = document.save(tmp_path / f"pipeline{suffix}")
        loaded = SmartPipelineDocument.load(path)
        assert loaded.to_dict() == document.to_dict()
        assert [type(s).__name__ for s in loaded.steps] == [type(s).__name__ for s in document.steps]
        assert loaded.settings.roles == {"scouting": "widefield", "idle": "off"}
        assert loaded.settings.replay_interval_s == 0.5 and loaded.settings.arms_live
    payload = json.loads(document.to_json())
    assert payload["metadata"]["smart_pipeline"] == {"roles": {"scouting": "widefield", "idle": "off"},
                                                     "replay_interval_s": 0.5}
    assert payload["steps"][0] == {"step": "live", "id": "cam", "detector": "Camera"}
    assert payload["steps"][4] == {"step": "gate", "id": "g", "rule": "condition", "inputs": ["pk"]}
    assert payload["steps"][6]["step"] == "action" and payload["steps"][8]["sink"] == "event-log"


def test_settings_are_checked_on_load_and_empty_ones_leave_no_metadata():
    document = SmartPipelineDocument(pipeline())
    assert "smart_pipeline" not in document.to_dict().get("metadata", {})
    document.settings = PipelineSettings()
    assert "smart_pipeline" not in document.to_dict().get("metadata", {})
    with pytest.raises(WorkflowError, match="role 'dinner'"):
        PipelineSettings(roles={"dinner": "x"})
    bad = {"name": "p", "metadata": {"smart_pipeline": {"colour": "red"}}, "steps": []}
    with pytest.raises(WorkflowError, match="unknown key"):
        SmartPipelineDocument.from_dict(bad)


def test_step_parameters_are_checked_when_made():
    with pytest.raises(WorkflowError, match="live mode"):
        Live("cam", detector="Camera", mode="sometimes")
    with pytest.raises(WorkflowError, match="interval_s above 0"):
        Live("cam", detector="Camera", mode="clock")
    with pytest.raises(WorkflowError, match="gate rule"):
        Gate("g", "maybe", inputs=["x"])
    with pytest.raises(WorkflowError, match="seconds above 0"):
        Gate("g", "min-interval", {"seconds": 0}, inputs=["x"])
    assert Gate("g", "min-interval", inputs=["x"]).params == {}       # one second by default
    with pytest.raises(WorkflowError, match="count of at least 1"):
        Gate("g", "count-limit", {"count": 0}, inputs=["x"])
    with pytest.raises(WorkflowError, match="names an action"):
        Action("a", "", inputs=["x"])
    with pytest.raises(WorkflowError, match="sink must be"):
        Sink("s", "printer", inputs=["x"])
    assert Gate("g", "count-limit", {"count": "2"}, inputs=["x"]).params["count"] == 2


# -- the rules ----------------------------------------------------------------------

def test_a_well_formed_pipeline_has_no_issues_and_declares_its_kinds():
    workflow = pipeline()
    assert validate_pipeline(workflow, actions=CATALOG, transforms=transforms()) == []
    kinds = pipeline_kinds(workflow)
    assert kinds["cam"] == "image" and kinds["tf"] == "points" and kinds["mv"] == "event"


def test_one_source_first():
    issues = validate_pipeline(Workflow("p", [Gate("g", "condition", inputs=["g"])]))
    assert any("starts with one source" in str(i) for i in issues)
    two = Workflow("p", [Live("a", detector="A"), Live("b", detector="B")])
    assert any("one source; this is the second" in str(i) for i in validate_pipeline(two))
    late = Workflow("p", [Gate("g", "once", inputs=["cam"]), Live("cam", detector="A")])
    assert any("must be the first step" in str(i) for i in validate_pipeline(late))
    assert any("names a detector" in str(i) for i in validate_pipeline(Workflow("p", [Live("cam")])))


def test_arity_and_unknown_names():
    workflow = Workflow("p", [
        Live("cam", detector="Camera"),
        Transform("tf", "nope", inputs=["cam", "cam"]),
        Action("mv", "fly", {"speed": 1}, inputs=["cam"]),
    ])
    messages = [str(i) for i in validate_pipeline(workflow, actions=CATALOG, transforms=transforms())]
    assert any("exactly one input, got 2" in m for m in messages)
    assert any("unknown action 'fly'" in m for m in messages)
    single = Workflow("p", [Live("cam", detector="Camera"), Transform("tf", "nope", inputs=["cam"])])
    messages = [str(i) for i in validate_pipeline(single, transforms=transforms())]
    assert any("unknown transform 'nope'" in m for m in messages)
    params = Workflow("p", [Live("cam", detector="Camera"), Process("rp", "regionprops", {}, inputs=["cam"]),
                            Transform("tf", "same", inputs=["rp"]),
                            Action("mv", "move-to", {"speed": 1, "frame": CAMERA}, inputs=["tf"])])
    messages = [str(i) for i in validate_pipeline(params, actions=CATALOG, transforms=transforms())]
    assert any("unknown parameter(s) for 'move-to': ['speed']" in m for m in messages)


def test_points_reach_an_action_only_through_a_transform():
    direct = Workflow("p", [Live("cam", detector="Camera"), Process("rp", "regionprops", {}, inputs=["cam"]),
                            Action("mv", "move-to", {"frame": STAGE}, inputs=["rp"])])
    messages = [str(i) for i in validate_pipeline(direct, actions=CATALOG, transforms=transforms())]
    assert any("without a transform step" in m and "identity" in m for m in messages)

    through_gate = Workflow("p", [Live("cam", detector="Camera"), Process("rp", "regionprops", {}, inputs=["cam"]),
                                  Transform("tf", "cam2stage", inputs=["rp"]),
                                  Gate("g", "once", inputs=["tf"]), Process("pk", "pick", {}, inputs=["g"]),
                                  Action("mv", "move-to", {"frame": STAGE}, inputs=["pk"])])
    assert validate_pipeline(through_gate, actions=CATALOG, transforms=transforms()) == []

    image_in = Workflow("p", [Live("cam", detector="Camera"), Transform("tf", "cam2stage", inputs=["cam"])])
    messages = [str(i) for i in validate_pipeline(image_in, transforms=transforms())]
    assert messages == ["tf: a transform takes points; cam carries image"]

    wrong_frame = Workflow("p", [Live("cam", detector="Camera"), Process("rp", "regionprops", {}, inputs=["cam"]),
                                 Transform("tf", "same", inputs=["rp"]),
                                 Action("mv", "move-to", {"frame": STAGE}, inputs=["tf"])])
    messages = [str(i) for i in validate_pipeline(wrong_frame, actions=CATALOG, transforms=transforms())]
    assert any("'tf' writes 'detector:Camera', but 'move-to' takes 'stage:XY'" in m for m in messages)

    bad_tag = Workflow("p", [Live("cam", detector="Camera"), Process("rp", "regionprops", {}, inputs=["cam"]),
                             Transform("tf", "same", inputs=["rp"]),
                             Action("mv", "move-to", {"frame": "somewhere"}, inputs=["tf"])])
    messages = [str(i) for i in validate_pipeline(bad_tag, actions=CATALOG)]
    assert any("bad frame tag 'somewhere'" in m for m in messages)


def test_the_points_frame_tag_must_match_the_source_and_the_transform():
    other_detector = Workflow("p", [Live("cam", detector="Camera"),
                                   Process("rp", "regionprops", {"frame": "detector:Other"}, inputs=["cam"]),
                                   Transform("tf", "cam2stage", inputs=["rp"])])
    messages = [str(i) for i in validate_pipeline(other_detector, transforms=transforms())]
    assert any("measured in 'detector:Other', but the source is detector 'Camera'" in m for m in messages)

    other_frame = Workflow("p", [Live("cam", detector="Camera"),
                                Process("rp", "regionprops", {"frame": "stage:XY"}, inputs=["cam"]),
                                Gate("g", "once", inputs=["rp"]), Process("pk", "pick", {}, inputs=["g"]),
                                Transform("tf", "cam2stage", inputs=["pk"])])
    messages = [str(i) for i in validate_pipeline(other_frame, transforms=transforms())]
    assert any("'cam2stage' reads 'detector:Camera', but the points are measured in 'stage:XY'" in m
               for m in messages)


def test_points_that_only_trigger_an_action_need_no_transform():
    # a run-scan overview takes anything and names no frame: the points are a trigger, not coordinates
    scan = ActionSpec("run-scan", accepts=("any",), params={"mode": "overview", "frame": ""}, frame_param="frame")
    catalog = ActionCatalog([scan])
    trigger = Workflow("p", [Live("cam", detector="Camera"), Process("rp", "regionprops", {"frame": CAMERA}, inputs=["cam"]),
                             Gate("nothing", "condition", {"invert": True}, inputs=["rp"]),
                             Action("again", "run-scan", {"mode": "overview"}, inputs=["nothing"])])
    assert validate_pipeline(trigger, actions=catalog) == []


def test_an_action_input_of_the_wrong_kind_is_refused():
    workflow = Workflow("p", [Live("cam", detector="Camera"), Process("rp", "regionprops", {}, inputs=["cam"]),
                              Transform("tf", "cam2stage", inputs=["rp"]),
                              Action("sn", "snap", {"detector": "Camera"}, inputs=["tf"])])
    messages = [str(i) for i in validate_pipeline(workflow, actions=CATALOG)]
    assert any("'snap' takes event on input 1; tf carries points" in m for m in messages)


def test_a_replay_source_is_an_improcess_source(tmp_path):
    workflow = Workflow("p", [Source("raw", path=str(tmp_path / "rec.h5")), Gate("g", "once", inputs=["raw"]),
                              Sink("log", "event-log", inputs=["g.fired"])])
    assert validate_pipeline(workflow) == []
    assert SmartPipelineDocument(workflow).source.id == "raw"


# -- frames and the coordinate contract ---------------------------------------------

def test_frame_tags_parse_and_print():
    assert FrameTag.parse("detector:Camera") == FrameTag("detector", "Camera")
    assert str(FrameTag.parse("stage:XY")) == "stage:XY" and FrameTag.parse("unknown") == FrameTag("unknown")
    assert not FrameTag.parse("unknown").known and FrameTag.parse(" scanner:galvo ").name == "galvo"
    for bad in ("Camera", "detector:", "table:Camera"):
        with pytest.raises(CoordinateError, match="bad frame tag"):
            FrameTag.parse(bad)


def test_frame_identity_says_what_differs():
    base = FrameIdentity((64, 64))
    assert base.differs_from(FrameIdentity((64, 64))) == []
    reasons = base.differs_from(FrameIdentity((32, 64), binning=(2, 2), offset=(0, 8), axis_order="xy"))
    assert reasons == ["shape (64, 64) -> (32, 64)", "binning (1, 1) -> (2, 2)", "offset (0, 0) -> (0, 8)",
                       "axis_order 'yx' -> 'xy'"]
    assert FrameIdentity.from_dict(base.to_dict()) == base
    assert Frame(np.zeros((10, 20)), "Camera", 0, 0.0).identity == FrameIdentity((10, 20))
    assert Frame(np.zeros((3, 10, 20)), "Camera", 0, 0.0).identity.shape == (10, 20)


def test_check_points_enforces_the_contract():
    points = Points([[1.0, 2.0]], frame=STAGE)
    assert check_points(points, frame=STAGE).tolist() == [[1.0, 2.0]]
    assert check_points(Points([], frame=STAGE), frame=STAGE).shape == (0, 2)   # no detection passes
    with pytest.raises(CoordinateError, match="in frame 'stage:XY'; the action takes 'detector:Camera'"):
        check_points(points, frame=CAMERA)
    with pytest.raises(CoordinateError, match="NaN or infinite"):
        check_points(Points([[np.nan, 1.0]]))
    with pytest.raises(CoordinateError, match="no coordinates"):
        check_points(Value(3.0))
    with pytest.raises(CoordinateError, match="coordinate columns; expected 2"):
        check_points(Points([[1.0, 2.0, 3.0]]), dims=(2,))
    with pytest.raises(CoordinateError, match=r"column 1 outside \[0, 10\]"):
        check_points(Points([[1.0, 20.0]]), bounds=(None, (0, 10)))
    with pytest.raises(CoordinateError, match="different grid"):
        check_points(points, identity=FrameIdentity((32, 32)), expected_identity=FrameIdentity((64, 64)))


def test_the_readers_are_duck_typed():
    class Foreign:
        coordinates = np.array([[1.0, 1.0]])
        metadata = {"frame": CAMERA}
        kind = "points"

    assert points_of(Foreign()).shape == (1, 2) and kind_of(Foreign()) == "points"
    assert points_of([3.0, 4.0]).tolist() == [[3.0, 4.0]] and points_of([]).shape == (0, 2)
    assert value_of(2) == 2.0 and value_of(Value(1.5, "um")) == 1.5 and value_of(True) is None
    assert fired_of(Event(False)) is False and fired_of(np.bool_(True)) is True and fired_of(3) is None
    assert kind_of(True) == "event" and kind_of(2.5) == "value" and kind_of("text") == "unknown"
    assert bool(Event(True)) and not Event(False)


# -- transforms ---------------------------------------------------------------------

def test_scale_flip_maps_pixels_to_stage_units():
    transform = ScaleFlip("t", CAMERA, STAGE, pixel_size=(0.5, 0.25), origin=(32, 32), swap_xy=True,
                          invert=(False, True), offset=(100.0, 200.0))
    out = transform.apply(np.array([[32.0, 40.0]]), {})
    # (40 - 32) * 0.25 = 2 and (32 - 32) * 0.5 = 0; swapped -> (2, 0); invert column 1 -> (2, -0); + offset
    assert out.tolist() == [[102.0, 200.0]]
    assert transform.apply(np.zeros((0, 2)), {}).shape == (0, 2)
    with pytest.raises(CoordinateError, match="pixel size of 0"):
        ScaleFlip("t", CAMERA, STAGE, pixel_size=(0.0, 1.0))


def test_identity_cannot_relabel_a_frame():
    assert Identity("same", CAMERA).target_frame == CAMERA
    with pytest.raises(CoordinateError, match="cannot relabel"):
        Identity("same", CAMERA, STAGE)


def test_apply_transform_keeps_the_class_and_checks_the_source_frame():
    transform = ScaleFlip("t", CAMERA, STAGE, pixel_size=(2.0, 2.0))
    points = Points([[1.0, 2.0]], frame=CAMERA, frame_index=4, properties={"area": [9]})
    moved = apply_transform(points, transform, name="tf")
    assert isinstance(moved, Points) and moved.frame == STAGE and moved.coordinates.tolist() == [[2.0, 4.0]]
    assert moved.frame_index == 4 and moved.properties == {"area": [9]} and moved.name == "tf"
    with pytest.raises(CoordinateError, match="reads 'detector:Camera', but the points are in 'stage:XY'"):
        apply_transform(moved, transform)
    bare = apply_transform(np.array([[1.0, 1.0]]), Identity("same", "unknown"))
    assert isinstance(bare, Points) and bare.frame == "unknown"


def test_transforms_round_trip_through_dicts():
    registry = transforms()
    again = TransformRegistry.from_dicts(registry.to_dicts())
    assert again.ids() == ("cam2stage", "same") and again.get("cam2stage") == registry.get("cam2stage")
    with pytest.raises(CoordinateError, match="unknown transform type"):
        transform_from_dict({"type": "polynomial"})
    with pytest.raises(CoordinateError, match="needs 'source_frame'"):
        transform_from_dict({"type": "identity"})
