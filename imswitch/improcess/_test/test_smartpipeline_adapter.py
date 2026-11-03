"""ImProcess as a smart pipeline's ports: a frame becomes a result the
processors take, one step runs per tick the way a workflow runs it, and a
recording replays as frames."""
import h5py
import numpy as np
import pytest

from imswitch.imcommon.model.smartpipeline.values import Frame
from imswitch.improcess.model import PointsResult
from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.processors.python_step.context import make_points
from imswitch.improcess.workflows.runtime import bootstrap_registry
from imswitch.improcess.workflows.smartpipeline import (
    ProcessorStepRunner,
    RecordingFrameSource,
    StepError,
    frame_to_result,
)
from imswitch.improcess.workflows.steps import Process, Save, Source


@pytest.fixture(scope="module")
def registry():
    return bootstrap_registry(user_plugins=False)


@pytest.fixture
def runner(registry):
    return ProcessorStepRunner(registry)


def _two_discs(size=64):
    """A dark frame with a big dim disc at (20, 20) and a small bright one at (44, 44)."""
    yy, xx = np.mgrid[:size, :size]
    image = np.full((size, size), 10.0, dtype=np.float32)
    image[(yy - 20) ** 2 + (xx - 20) ** 2 <= 8 ** 2] = 100.0
    image[(yy - 44) ** 2 + (xx - 44) ** 2 <= 4 ** 2] = 200.0
    return image


def _frame(image=None, **kwargs):
    fields = dict(detector="Camera", sequence=3, t=0.3, exposure_s=0.01,
                  scale=(0.1, 0.1), unit="um", frame_index=3)
    fields.update(kwargs)
    return Frame(image=_two_discs() if image is None else image, **fields)


def _recording(path, frames=3):
    stack = np.stack([_two_discs() * (index + 1) for index in range(frames)])
    with h5py.File(str(path), "w") as handle:
        dataset = handle.create_dataset("data", data=stack)
        dataset.attrs["element_size_um"] = [1.0, 0.1, 0.1]
        dataset.attrs["axes"] = "TYX"
    return path, stack


THRESHOLD = Process("thr", "threshold", {"method": "otsu", "min_area": 5}, inputs=["img"])
REGIONS = Process("rp", "regionprops", {"frame": "detector:Camera"}, inputs=["thr", "img"])
PICK = Process("pk", "pick", {"rule": "largest"}, inputs=["rp"])


# -- frame -> result ---------------------------------------------------------------

def test_frame_to_result_is_an_image_the_processors_accept(registry):
    frame = _frame()
    result = frame_to_result(frame)

    assert isinstance(result, ArrayProcessingResult) and result.kind == "image"
    assert result.name == "Camera #3"
    assert result.axis_labels == ["Y", "X"]
    assert result.axis_scales == [0.1, 0.1] and result.scale_unit == "um"
    assert np.array_equal(result.data, frame.image)
    assert registry.get_processor("threshold").accepts(result)
    assert registry.get_processor("regionprops").accepts(result)

    envelope = result.metadata["frame"]
    assert envelope["detector"] == "Camera" and envelope["sequence"] == 3
    assert envelope["t"] == 0.3 and envelope["exposure_s"] == 0.01 and envelope["frame_index"] == 3
    assert envelope["identity"] == frame.identity.to_dict() == {
        "shape": [64, 64], "binning": [1, 1], "offset": [0, 0], "axis_order": "yx"}
    assert result.metadata["frame_index"] == 3


def test_a_frame_without_a_scale_is_in_pixels_and_a_stack_has_a_z_axis():
    plain = frame_to_result(Frame(image=_two_discs(), detector="Cam", sequence=0, t=0.0))
    assert plain.axis_scales == [1.0, 1.0] and plain.scale_unit == "px"
    assert "frame_index" not in plain.metadata and plain.metadata["frame"]["frame_index"] is None

    stack = frame_to_result(Frame(image=np.zeros((2, 8, 8)), detector="Cam", sequence=0, t=0.0,
                                  scale=(0.5, 0.5), unit="um"))
    assert stack.axis_labels == ["Z", "Y", "X"] and stack.axis_scales == [1.0, 0.5, 0.5]

    with pytest.raises(StepError, match=r"\(Y, X\) or \(Z, Y, X\)"):
        frame_to_result(Frame(image=np.zeros(8), detector="Cam", sequence=0, t=0.0))


# -- the step runner ---------------------------------------------------------------

def test_threshold_regionprops_pick_yield_the_largest_disc(runner):
    frame = _frame()
    image = runner.image_result(frame)
    state = {}

    labels = runner.run_step(THRESHOLD, [image], state=state, frame=frame)
    assert list(labels) == ["out"] and labels["out"].kind == "labels"

    regions = runner.run_step(REGIONS, [labels["out"], image], state=state, frame=frame)
    assert list(regions) == ["out"]
    assert isinstance(regions["out"], PointsResult) and regions["out"].count == 2
    assert regions["out"].frame == "detector:Camera" and regions["out"].frame_index == 3
    assert set(regions["out"].properties) >= {"label", "area", "mean_intensity"}
    assert regions["out"].coordinate_scale == [0.1, 0.1] and regions["out"].scale_unit == "um"

    picked = runner.run_step(PICK, [regions["out"]], state=state, frame=frame)
    assert list(picked) == ["out"]
    assert isinstance(picked["out"], PointsResult) and picked["out"].count == 1
    assert picked["out"].frame == "detector:Camera"
    y, x = picked["out"].coordinates[0]
    assert abs(y - 20) < 0.5 and abs(x - 20) < 0.5
    assert picked["out"].properties["area"][0] > 150


def test_a_python_step_runs_and_its_state_persists_between_ticks(runner):
    image = runner.image_result(_frame())
    code = 'state["n"] = state.get("n", 0) + 1\nout = make_value(state["n"])'
    step = Process("py", "python", {"code": code, "ports": "out"}, inputs=["img"])
    shared = {}

    first = runner.run_step(step, [image], state=shared)
    second = runner.run_step(step, [image], state=shared)
    assert list(first) == ["out"] and first["out"].kind == "value"
    assert first["out"].value == 1.0 and second["out"].value == 2.0
    assert shared == {"n": 2}

    fresh = runner.run_step(step, [image], state={})
    assert fresh["out"].value == 1.0
    # The state is live, never a parameter: the step's params are untouched.
    assert step.params == {"code": code, "ports": "out"}


def test_a_python_step_may_hand_back_an_empty_points_result(runner):
    image = runner.image_result(_frame())
    step = Process("py", "python", {"code": 'out = make_points([], "detector:Camera")', "ports": "out"},
                   inputs=["img"])
    out = runner.run_step(step, [image], state={})["out"]
    assert isinstance(out, PointsResult)
    assert out.count == 0 and out.coordinates.shape == (0, 2) and out.frame == "detector:Camera"


def test_an_all_dark_frame_yields_empty_points_from_regionprops_and_pick(runner):
    frame = _frame(np.full((64, 64), 10.0, dtype=np.float32), sequence=0, t=0.0, frame_index=0)
    image = runner.image_result(frame)

    labels = runner.run_step(THRESHOLD, [image], state={}, frame=frame)["out"]
    assert int(np.asarray(labels.data).max()) == 0
    regions = runner.run_step(REGIONS, [labels, image], state={}, frame=frame)["out"]
    assert isinstance(regions, PointsResult) and regions.count == 0
    assert regions.coordinates.shape == (0, 2) and regions.frame == "detector:Camera"
    picked = runner.run_step(PICK, [regions], state={}, frame=frame)["out"]
    assert isinstance(picked, PointsResult) and picked.count == 0
    assert picked.frame == "detector:Camera"


def test_a_single_input_processor_over_two_inputs_fans_out(runner):
    first = runner.image_result(_frame(sequence=0))
    second = runner.image_result(_frame(sequence=1))
    produced = runner.run_step(THRESHOLD, [first, second], state={})
    assert list(produced) == ["out", "out1"]


def test_steps_that_are_not_process_steps_are_refused(runner, tmp_path):
    image = runner.image_result(_frame())
    with pytest.raises(StepError, match="'save' step cannot run in a pipeline"):
        runner.run_step(Save("s", input="pk", fmt="csv"), [image], state={})
    with pytest.raises(StepError, match="'source' step cannot run in a pipeline"):
        runner.run_step(Source("src", path=str(tmp_path / "x.h5")), [image], state={})

    class Odd:
        id = "odd"
        kind = "gate"

    with pytest.raises(StepError, match="'gate' step is not a process step"):
        runner.run_step(Odd(), [image], state={})


def test_a_processor_refusing_its_input_or_failing_raises_step_error(runner):
    image = runner.image_result(_frame())
    with pytest.raises(StepError, match="'pick' does not accept result 'Camera #3'"):
        runner.run_step(PICK, [image], state={})
    with pytest.raises(StepError, match="no processor 'no-such-processor'"):
        runner.run_step(Process("x", "no-such-processor", inputs=["img"]), [image], state={})

    boom = Process("py", "python", {"code": "raise ValueError('nope')", "ports": "out"}, inputs=["img"])
    with pytest.raises(StepError, match="'python' failed on 'Camera #3'.*nope") as info:
        runner.run_step(boom, [image], state={})
    assert info.value.__cause__ is not None


# -- the frame source --------------------------------------------------------------

def test_recording_frame_source_replays_the_planes_as_frames(tmp_path):
    path, stack = _recording(tmp_path / "rec.h5")
    with RecordingFrameSource(path, detector="Replay", interval_s=0.5) as source:
        assert source.frame_count == 3 and len(source) == 3
        frames = list(source.frames())

    assert [frame.sequence for frame in frames] == [0, 1, 2]
    assert [frame.frame_index for frame in frames] == [0, 1, 2]
    assert [frame.t for frame in frames] == [0.0, 0.5, 1.0]
    for frame in frames:
        assert frame.detector == "Replay"
        assert np.array_equal(frame.image, stack[frame.sequence])
        assert frame.scale == (0.1, 0.1) and frame.unit == "um"
        assert frame.identity.shape == (64, 64)
        assert frame.frame_tag == "detector:Replay"


def test_recording_frame_source_takes_a_spec_and_a_dataset_binding(tmp_path):
    path, _stack = _recording(tmp_path / "rec.h5")
    with RecordingFrameSource(f"{path}::data", detector="A") as source:
        assert sum(1 for _frame in source.frames()) == 3
    with RecordingFrameSource(str(path), dataset="data") as source:
        assert source.spec.dataset == "data"
        assert sum(1 for _frame in source) == 3


def test_recording_frame_source_stops_closes_once_and_refuses_after_close(tmp_path):
    path, _stack = _recording(tmp_path / "rec.h5")
    source = RecordingFrameSource(path)
    frames = source.frames()
    first = next(frames)
    assert first.sequence == 0
    source.stop()
    assert list(frames) == [] and source.stopped

    source.close()
    source.close()
    with pytest.raises(RuntimeError, match="closed"):
        list(source.frames())
    with pytest.raises(RuntimeError, match="closed"):
        source.open()


def test_a_plain_image_is_one_frame_in_pixels(tmp_path):
    with h5py.File(str(tmp_path / "plane.h5"), "w") as handle:
        handle.create_dataset("data", data=_two_discs())
    with RecordingFrameSource(tmp_path / "plane.h5") as source:
        assert source.frame_count == 1
        frames = list(source.frames())
    assert len(frames) == 1 and frames[0].image.shape == (64, 64)
    assert frames[0].scale == () and frames[0].unit == "px"


# -- empty points ------------------------------------------------------------------

def test_points_result_accepts_no_points():
    assert PointsResult("x", []).coordinates.shape == (0, 2)
    assert PointsResult("x", np.zeros((0,))).coordinates.shape == (0, 2)
    assert PointsResult("x", np.zeros((0, 3))).coordinates.shape == (0, 3)
    assert PointsResult("x", []).count == 0
    empty = make_points([], "detector:Camera")
    assert empty.coordinates.shape == (0, 2) and empty.frame == "detector:Camera"
    with pytest.raises(ValueError, match=r"\(N, 2\) or \(N, 3\)"):
        PointsResult("x", np.zeros((0, 4)))
    with pytest.raises(ValueError, match=r"\(N, 2\) or \(N, 3\)"):
        PointsResult("x", [1.0])
