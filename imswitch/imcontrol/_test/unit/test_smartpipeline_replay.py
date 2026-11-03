"""Phase 1's acceptance test: a pipeline replays a recording through
ImProcess's processors and logs "would move to (x, y)" per detected event.

The engine (imcontrol) runs against ImProcess's ports -- the recording
source and the processor step runner -- the way the live run will run
against the detector and the hardware. ImProcess is imported here, in the
test, never by the engine."""
import h5py
import numpy as np
import pytest

from imswitch.imcommon.model.smartpipeline import (
    Action,
    Gate,
    PipelineSettings,
    ScaleFlip,
    Sink,
    SmartPipelineDocument,
    Transform,
    TransformRegistry,
)
from imswitch.imcommon.model.workflow.steps import Process, Source, Workflow
from imswitch.imcontrol.model.smartpipeline import Engine, RunMode
from imswitch.imcontrol.model.smartpipeline.mock import MockActions

improcess_smartpipeline = pytest.importorskip("imswitch.improcess.workflows.smartpipeline")
from imswitch.improcess.workflows.runtime import bootstrap_registry  # noqa: E402

CAMERA = "detector:Camera"
STAGE = "stage:XYStage"


@pytest.fixture(scope="module")
def registry():
    return bootstrap_registry(user_plugins=False)


def _frame(discs, size=64):
    yy, xx = np.mgrid[:size, :size]
    image = np.full((size, size), 10.0, dtype=np.float32)
    for cy, cx, radius, level in discs:
        image[(yy - cy) ** 2 + (xx - cx) ** 2 <= radius ** 2] = level
    return image


def _recording(path):
    """Four frames: a cell, nothing, the cell moved, two cells."""
    stack = np.stack([
        _frame([(20, 20, 6, 100.0)]),
        _frame([]),
        _frame([(40, 12, 6, 100.0)]),
        _frame([(20, 20, 8, 100.0), (44, 44, 3, 200.0)]),
    ])
    with h5py.File(str(path), "w") as handle:
        dataset = handle.create_dataset("data", data=stack)
        dataset.attrs["element_size_um"] = [1.0, 0.1, 0.1]
        dataset.attrs["axes"] = "TYX"
    return path


def _pipeline(raw):
    workflow = Workflow("find the cell and go there", [
        Source("raw", path=str(raw)),
        Process("thr", "threshold", {"method": "otsu", "min_area": 5}, inputs=["raw"]),
        Process("rp", "regionprops", {"frame": CAMERA}, inputs=["thr", "raw"]),
        Process("pk", "pick", {"rule": "largest"}, inputs=["rp"]),
        Gate("found", "condition", inputs=["pk"]),
        Transform("tf", "cam2stage", inputs=["found"]),
        Action("mv", "move-to", {"positioner": "XYStage", "frame": STAGE}, inputs=["tf"]),
        Sink("log", "event-log", inputs=["pk"]),
    ])
    document = SmartPipelineDocument(workflow)
    document.settings = PipelineSettings(roles={"scouting": "widefield", "idle": "off"}, replay_interval_s=0.5)
    return document


def _engine(document, registry, mode, actions=None):
    source = improcess_smartpipeline.RecordingFrameSource(
        document.source.source, detector="Camera", interval_s=document.settings.replay_interval_s,
    )
    transforms = TransformRegistry([ScaleFlip("cam2stage", CAMERA, STAGE, pixel_size=(0.1, 0.1))])
    return Engine(document, source=source, steps=improcess_smartpipeline.ProcessorStepRunner(registry),
                  actions=actions or MockActions(), transforms=transforms, mode=mode, registry=registry)


def test_a_recording_replays_and_logs_would_move_to_per_detected_cell(registry, tmp_path):
    document = _pipeline(_recording(tmp_path / "cells.h5"))
    assert document.validate(registry=registry, actions=MockActions.catalog) == []

    eng = _engine(document, registry, RunMode.VALIDATE)
    summary = eng.run()

    assert summary.ok, summary.error
    assert summary.ticks == 4 and summary.ran == 4 and summary.gates_fired == 3
    messages = [e.detail["message"] for e in eng.log.events("would", step="mv")]
    assert messages == [
        f"would move-to XYStage to (y=2.000, x=2.000) in {STAGE}",
        f"would move-to XYStage to (y=4.000, x=1.200) in {STAGE}",
        f"would move-to XYStage to (y=2.000, x=2.000) in {STAGE}",   # the largest of the two cells
    ]
    assert [e.event for e in eng.log.events(step="found")] == ["open", "closed", "open", "open"]
    # the sink saw the picked points of every tick, the empty one included
    counts = [e.detail["value"]["count"] for e in eng.log.events("log", step="log")]
    assert counts == [1, 0, 1, 1]
    assert eng.actions.requests == []   # validate mode dispatches nothing


def test_the_same_pipeline_moves_the_mock_stage_in_experiment_mode(registry, tmp_path):
    document = _pipeline(_recording(tmp_path / "cells.h5"))
    actions = MockActions()
    eng = _engine(document, registry, "experiment", actions)
    summary = eng.run()

    assert summary.ok and summary.actions_completed == 3
    assert [r.action for r in actions.requests] == ["move-to"] * 3
    assert actions.position == {"XYStage": {"Y": 2.0, "X": 2.0}}
    # the points the backend received were in the stage frame, with the
    # source frame index the detection was made on
    moved = actions.requests[1].inputs[0]
    assert moved.frame == STAGE and moved.frame_index == 2


def test_the_event_log_of_a_replay_is_a_csv(registry, tmp_path):
    document = _pipeline(_recording(tmp_path / "cells.h5"))
    eng = _engine(document, registry, "validate")
    eng.run()
    path = eng.log.write_csv(tmp_path / "events.csv")
    text = path.read_text(encoding="utf-8")
    assert "would move-to XYStage" in text and "footer" in text
