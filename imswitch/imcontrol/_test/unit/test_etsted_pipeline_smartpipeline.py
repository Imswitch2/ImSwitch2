"""An etSTED analysis pipeline inside a smart pipeline, side by side with etSTED.

The same frames go (a) through etSTED's own frame loop (the real ``runPipeline`` of the event-triggered
controller, with the triggered scan's bookkeeping run as the controller runs it) and (b) through a smart
pipeline (the real engine, the ``etsted-pipeline`` step, ``pick``, a gate and an action): the detections must be
the same on the same frames, across triggered scans, where etSTED clears the pipeline's state.
Also the engine's side of the reset (a scan under way, a scan ended), the cooldown counted from the end of the
scan, the shipped document and the one-line check of a pipeline file.
"""
import json
import subprocess
import sys
import textwrap
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import imswitch
from imswitch.imcommon.model.smartpipeline import Frame, SmartPipelineDocument, TransformRegistry
from imswitch.imcommon.model.smartpipeline.transforms import OneToOne
from imswitch.imcommon.model.workflow.steps import Process, Workflow
from imswitch.imcommon.model.smartpipeline import Action, Gate, Live
from imswitch.imcontrol.controller.controllers.EventTriggeredBaseController import EventTriggeredControllerBase
from imswitch.imcontrol.model.EtSTEDPipelineRunner import EtSTEDPipelineRunner
from imswitch.imcontrol.model.EventTriggeredSession import EventRunMode, EventTriggeredSessionState
from imswitch.imcontrol.model.smartpipeline import Engine
from imswitch.imcontrol.model.smartpipeline.mock import ArrayFrameSource, MockActions, sleep_clock
from imswitch.imcontrol._test.unit.test_etsted_dff_pipeline import SHAPE, Stream
from imswitch.improcess.workflows import smartpipeline as improcess_smartpipeline
from imswitch.improcess.workflows.runtime import bootstrap_registry

DEFAULTS = Path(imswitch.__file__).parent / "_data" / "user_defaults"
DFF_EVENT = DEFAULTS / "imcontrol_etsted" / "analysis_pipelines" / "dff_event.py"
SHIPPED = DEFAULTS / "smart_pipelines" / "etsted_dff_event.json"


@pytest.fixture(scope="module")
def registry():
    return bootstrap_registry(user_plugins=False)


class _Logger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


# ------------------------------------------------------------------ (a) etSTED

class EtstedPath:
    """etSTED's frame loop: the real ``runPipeline`` on a controller with no hardware.

    ``_handle_event_frame`` is what the controller does with a detection while it starts the slow scan; here it
    records the detections and runs the lines of it that touch the analysis: the previous frames are saved and
    cleared (``saveValidationImages``), ``exinfo`` is cleared, and, when the scan has ended, the frame count is 0.
    """

    def __init__(self, path, values=(), mask=None):
        controller = EventTriggeredControllerBase.__new__(EventTriggeredControllerBase)
        controller._logger = _Logger()
        controller._state = EventTriggeredSessionState()
        controller._state.detectorFast = "Camera"
        controller._state.running = True
        controller._state.runMode = EventRunMode.Experiment
        controller._experimentActive = True
        controller._stopRequested = False
        controller._closed = False
        controller._pipelineRunner = EtSTEDPipelineRunner()
        controller._pipelineRunner.load_file(path)
        controller._prevFrames = deque(maxlen=10)
        controller._prevAnaFrames = deque(maxlen=10)
        controller._binary_mask = mask
        controller._exinfo = None
        controller._param_vals = list(values)
        controller._commChannel = SimpleNamespace(sigSnapImgPrev=SimpleNamespace(emit=lambda *a: None))
        controller.setDetLogLine = lambda *a, **k: None
        controller._set_status = lambda *a, **k: None
        controller._handle_event_frame = self._event
        self.controller = controller
        self.events: dict[int, np.ndarray] = {}
        self.index = -1

    def _event(self, coords_detected, img):
        c = self.controller
        self.events[self.index] = np.array(coords_detected)
        c._prevFrames.append(img)
        c.saveValidationImages(prev=True, prev_ana=False)      # as after the scan was armed
        c._exinfo = None
        c._state.busy = False
        c._state.frame = 0                                     # the scan has ended before the next frame

    def feed(self, frames):
        for self.index, frame in enumerate(frames, start=self.index + 1):
            self.controller.runPipeline("Camera", frame, False, [], True)
        return self.events


# ------------------------------------------------------------ (b) smart pipeline

def smart_workflow(path, params=None, **step_params):
    return Workflow("equivalence", [
        Live("cam", detector="Camera"),
        Process("etsted", "etsted-pipeline", {"pipeline": str(path), "frame": "detector:Camera", **(params or {}),
                                              **step_params}, inputs=["cam"]),
        Process("best", "pick", {"rule": "first"}, inputs=["etsted"]),
        Gate("found", "condition", inputs=["best"]),
        Action("act", "log", {"message": "scan"}, inputs=["found"]),
    ])


def smart_engine(registry, workflow, frames, actions=None, mode="experiment", **kwargs):
    source = ArrayFrameSource(frames, detector="Camera")
    engine = Engine(SmartPipelineDocument(workflow), source=source,
                    steps=improcess_smartpipeline.ProcessorStepRunner(registry),
                    actions=actions or MockActions(), transforms=TransformRegistry([OneToOne()]), mode=mode,
                    registry=registry, **kwargs)
    engine.arm()
    return engine, source


class SmartPath:
    def __init__(self, registry, workflow, frames, **kwargs):
        self.engine, source = smart_engine(registry, workflow, frames, **kwargs)
        self.detections: dict[int, np.ndarray] = {}
        self.triggers: list[int] = []
        for index, frame in enumerate(source.frames()):
            result = self.engine.tick(frame)
            assert result.error is None, result.error
            self.detections[index] = np.asarray(result.values["etsted.out"].coordinates)
            if result.started:
                self.triggers.append(index)

    def runner_globals(self):
        return self.engine._state["etsted"]["runner"].function.__globals__


def etsted_order(smart_coordinates, offset=0.5):
    """The smart pipeline's (row, column) back to the pipeline's own (column, row) numbers."""
    coords = np.asarray(smart_coordinates).reshape(-1, 2)
    return np.column_stack([coords[:, 1] + offset, coords[:, 0] + offset])


# --------------------------------------------------------------------- frames

def event_frames():
    """A noisy sample with events at known frames; after each triggered scan etSTED restarts (6 ignored frames,
    10 warm-up frames of dff_event), so the next event comes long after, and one is hidden in the ignored frames."""
    stream = Stream()
    plan = {30: [(60, 200, 400.0)],                                 # event 1 (after the warm-up)
            33: [(150, 250, 900.0)],                                # right after the scan: ignored, baseline only
            62: [(150, 250, 900.0), (40, 50, 600.0), (200, 120, 300.0)],      # event 2: three, strongest first
            95: [(20, 300, 500.0)]}                                 # event 3
    return [stream.frame(spikes=plan.get(i, ())) for i in range(110)]


# ------------------------------------------------------------------ equivalence

@pytest.mark.parametrize("params", [{}, {"dff_threshold": 0.3, "warmup_frames": 6.0}], ids=["defaults", "tuned"])
def test_the_smart_pipeline_detects_what_etsted_detects_across_triggered_scans(registry, params):
    frames = event_frames()
    names = ["dff_threshold", "bin_px", "baseline_frames", "smooth_px", "min_baseline", "warmup_frames"]
    defaults = dict(dff_threshold=0.5, bin_px=4.0, baseline_frames=20.0, smooth_px=1.0, min_baseline=20.0,
                    warmup_frames=10.0)
    values = [float({**defaults, **params}[name]) for name in names]
    etsted = EtstedPath(DFF_EVENT, values).feed(frames)
    smart = SmartPath(registry, smart_workflow(DFF_EVENT, **params), frames)
    assert len(etsted) >= 3, sorted(etsted)
    # the same frames trigger a scan; frames in the ignored stretch after a scan never do
    assert smart.triggers == sorted(etsted)
    assert 33 not in etsted
    for index, frame_coordinates in smart.detections.items():
        if index in etsted:
            # every detection, strongest first, in the pipeline's own numbers and order
            np.testing.assert_allclose(etsted_order(frame_coordinates), etsted[index], atol=1e-9)
        else:
            assert len(frame_coordinates) == 0, f"frame {index}: smart sees {frame_coordinates}"
    assert len(etsted[62]) == 3                                         # strongest first, all three reported


def test_the_arguments_of_the_pipeline_are_the_same_in_both_paths(registry, tmp_path):
    source = textwrap.dedent("""
        import numpy as np
        LOG = []
        def recorder(img, prev_frames, binary_mask, testmode, exinfo, a=1.5, b=2):
            LOG.append(dict(n_prev=len(prev_frames), last=None if not prev_frames else float(prev_frames[-1].flat[0]),
                            maxlen=prev_frames.maxlen, img=float(img.flat[0]), exinfo=exinfo, values=(a, b),
                            mask=None if binary_mask is None else (binary_mask.dtype.name, int(binary_mask.sum())),
                            testmode=testmode))
            n = (exinfo or 0) + 1
            if int(img.flat[0]) in (12, 30):
                return np.array([[5.0, 7.0], [1.0, 2.0]]), n
            return np.empty((0, 2)), n
    """)
    path = tmp_path / "recorder.py"
    path.write_text(source)
    mask = np.zeros((8, 6), dtype=bool)
    mask[1:3, 2:5] = True
    frames = [np.full((8, 6), float(i), dtype=np.uint16) for i in range(40)]
    etsted = EtstedPath(path, [1.5, 2.0], mask=mask)
    etsted.feed(frames)
    np.save(tmp_path / "mask.npy", mask)
    smart = SmartPath(registry, smart_workflow(path, {"mask": "file", "mask_path": str(tmp_path / "mask.npy")}), frames)
    assert smart.triggers == sorted(etsted.events) == [12, 30]
    left = etsted.controller._pipelineRunner.function.__globals__["LOG"]
    right = smart.runner_globals()["LOG"]
    assert len(left) == len(right) == 40
    assert left == right                           # previous frames, exinfo (reset after a scan), values, mask, testmode
    assert right[13]["exinfo"] is None and right[13]["n_prev"] == 0         # the frame after the scan: afresh
    assert right[12]["n_prev"] == 10


def test_the_analysis_image_is_the_one_etsted_gets_in_its_test_modes(registry):
    frames = event_frames()[:20]
    runner = EtSTEDPipelineRunner()
    runner.load_file(DFF_EVENT)
    exinfo, previous, expected = None, deque(maxlen=10), []
    for frame in frames:
        result = runner.execute(frame, previous, None, True, exinfo, [0.5, 4.0, 20.0, 1.0, 20.0, 10.0])
        exinfo = result.exinfo
        previous.append(frame)
        expected.append(result.analysis_image)
    engine, source = smart_engine(registry, smart_workflow(DFF_EVENT, testmode=True), frames, mode="validate")
    for index, frame in enumerate(source.frames()):
        result = engine.tick(frame)
        np.testing.assert_array_equal(result.values["etsted.analysis"].data, expected[index])


# ----------------------------------------------- the engine's side of the reset

def _scan_workflow(path, **gate):
    return Workflow("scan", [
        Live("cam", detector="Camera"),
        Process("etsted", "etsted-pipeline", {"pipeline": str(path), "ignore_frames": 0}, inputs=["cam"]),
        Process("best", "pick", {"rule": "first"}, inputs=["etsted"]),
        Gate("found", "condition", inputs=["best"]),
        Gate("rest", "min-interval", params={"seconds": 10.0, **gate}, inputs=["found"]),
        Action("act", "log", {"message": "scan"}, inputs=["rest"]),
    ])


COUNTER = textwrap.dedent("""
    import numpy as np
    def counter(img, prev_frames, binary_mask, testmode, exinfo):
        n = (exinfo or 0) + 1
        return np.array([[3.0, 4.0]]), n
""")


def test_a_started_scan_pauses_the_pipeline_and_its_end_restarts_it(registry, tmp_path):
    path = tmp_path / "counter.py"
    path.write_text(COUNTER)
    actions = MockActions(auto_complete=False)
    frames = [np.zeros((8, 6)) + i for i in range(8)]
    engine, source = smart_engine(registry, _scan_workflow(path), frames, actions=actions, mode="experiment")
    frames_iter = source.frames()
    first = engine.tick(next(frames_iter))                       # the pipeline fires: a scan starts, pending
    assert first.started == "act" and engine.busy
    assert engine._state["etsted"]["exinfo"] == 1
    during = engine.tick(next(frames_iter))                      # a scan is under way: the pipeline is not called
    assert engine._state["etsted"]["exinfo"] == 1 and len(during.values["etsted.out"].coordinates) == 0
    assert during.values["etsted.out"].metadata["message"].startswith("paused")
    actions.settle_all()                                         # the scan ended
    assert not engine.busy
    after = engine.tick(next(frames_iter))                       # the first frame after it: afresh (exinfo None -> 1)
    assert engine._state["etsted"]["exinfo"] == 1 and engine._state["etsted"]["calls"] == 1
    assert len(after.values["etsted.out"].coordinates) == 1


def test_validate_and_visualize_start_nothing_and_so_reset_nothing(registry, tmp_path):
    path = tmp_path / "counter.py"
    path.write_text(COUNTER)
    for mode in ("validate", "visualize"):
        engine, source = smart_engine(registry, _scan_workflow(path, seconds=0.001), [np.zeros((8, 6))] * 5, mode=mode)
        for frame in source.frames():
            engine.tick(frame)
        assert engine._state["etsted"]["exinfo"] == 5, mode


def test_a_scan_that_did_nothing_does_not_restart_the_pipeline(registry, tmp_path):
    path = tmp_path / "counter.py"
    path.write_text(COUNTER)

    class Nothing(MockActions):
        def _perform(self, request):
            from imswitch.imcontrol.model.smartpipeline.engine import NOTHING_DONE
            return {NOTHING_DONE: True}

    engine, source = smart_engine(registry, _scan_workflow(path), [np.zeros((8, 6))] * 4, actions=Nothing())
    for frame in source.frames():
        engine.tick(frame)
    assert engine._state["etsted"]["exinfo"] >= 3


def test_the_cooldown_counts_from_the_end_of_the_scan(registry, tmp_path):
    path = tmp_path / "counter.py"
    path.write_text(COUNTER)
    for from_end, second in ((False, True), (True, False)):
        clock = sleep_clock()
        actions = MockActions(auto_complete=False)
        engine, source = smart_engine(registry, _scan_workflow(path, from_end=from_end), [np.zeros((8, 6))] * 12,
                                      actions=actions, clock=clock)
        frames = source.frames()
        engine.tick(next(frames))                                # t=0: the gate opens, the scan starts
        clock.advance(8.0)                                       # the scan takes 8 s
        actions.settle_all()
        clock.advance(4.0)                                       # t=12: 12 s after the start, 4 s after the end
        started = None
        for _ in range(3):
            result = engine.tick(next(frames))
            started = started or result.started
        assert bool(started) is second, from_end
    gate = Gate("rest", "min-interval", params={"seconds": 3.0, "from_end": True}, inputs=["a"])
    assert gate.to_dict()["params"]["from_end"] is True
    with pytest.raises(Exception, match="from_end must be true or false"):
        Gate("rest", "min-interval", params={"seconds": 3.0, "from_end": "yes"}, inputs=["a"])


def test_the_state_key_the_engine_writes_is_the_one_the_step_reads():
    from imswitch.improcess.processors.etsted_pipeline.processor import ACTION_STATE_KEY

    assert ACTION_STATE_KEY == "engine_actions"
    source = (Path(imswitch.__file__).parent / "imcontrol" / "model" / "smartpipeline" / "engine.py").read_text()
    assert f'state.get("{ACTION_STATE_KEY}")' in source


# ------------------------------------------------------------ the shipped document

@pytest.fixture
def user_folder(tmp_path, monkeypatch):
    """The ImSwitch user folder as the first start leaves it: the shipped dff_event in etSTED's folder."""
    from imswitch.imcommon.model import dirtools

    monkeypatch.setattr(dirtools.UserFileDirs, "Root", str(tmp_path))
    folder = tmp_path / "imcontrol_etsted" / "analysis_pipelines"
    folder.mkdir(parents=True)
    (folder / "dff_event.py").write_text(DFF_EVENT.read_text())
    return tmp_path


def test_the_shipped_document_validates_and_exposes_the_pipelines_parameters(registry, user_folder):
    from imswitch.imcontrol.model.smartpipeline.catalog import catalog
    from imswitch.imcontrol.model.smartpipeline.parameters import exposed_parameters
    from imswitch.imcommon.model.smartpipeline import ScaleFlip

    document = SmartPipelineDocument.load(SHIPPED)
    transforms = TransformRegistry([ScaleFlip("cam_to_scan", "detector:Camera", "scanner:galvo", pixel_size=(0.5, 0.5)),
                                    OneToOne()])
    issues = document.validate(registry=registry, actions=catalog(), transforms=transforms)
    assert [str(i) for i in issues] == []
    parameters = {p.name: p for p in exposed_parameters(document.workflow, registry=registry, actions=catalog())}
    assert list(parameters) == ["dff_threshold", "bin_px", "baseline_frames", "smooth_px", "min_baseline",
                                "warmup_frames", "cooldown_s", "max_scans"]
    assert all(p.usable for p in parameters.values()), {n: p.problem for n, p in parameters.items() if not p.usable}
    assert parameters["dff_threshold"].file_value == 0.5 and "dF/F" in parameters["dff_threshold"].help
    assert parameters["cooldown_s"].file_value == 5.0 and parameters["max_scans"].file_value == 10
    # switching the calibration for a demonstration is one word
    payload = json.loads(SHIPPED.read_text())
    next(s for s in payload["steps"] if s["id"] == "tf")["transform"] = "identity"
    demo = SmartPipelineDocument.from_dict(payload)
    assert [str(i) for i in demo.validate(registry=registry, actions=catalog(), transforms=transforms)] == []
    scan = next(s for s in payload["steps"] if s["id"] == "scan")
    # etSTED recentres the loaded scan: no box, no size, no pixel size, no dwell of its own
    assert scan["params"]["box"] == "off" and scan["params"]["length_um"] == 0 and scan["params"]["pixel_um"] == 0


def test_the_shipped_document_in_validate_mode_says_where_it_would_scan(registry, user_folder):
    """The document as shipped (the identity transform, a mock scan action), offline in Validate."""
    payload = json.loads(SHIPPED.read_text())
    next(s for s in payload["steps"] if s["id"] == "tf")["transform"] = "identity"
    next(s for s in payload["steps"] if s["id"] == "etsted")["params"].update(warmup_frames=4)
    frames = event_frames()[:40]
    source = ArrayFrameSource(frames, detector="Camera")
    engine = Engine(SmartPipelineDocument.from_dict(payload), source=source,
                    steps=improcess_smartpipeline.ProcessorStepRunner(registry), actions=MockActions(),
                    transforms=TransformRegistry([OneToOne()]), mode="validate", registry=registry)
    assert engine.run().ok
    would = engine.log.events("would", step="scan")
    assert len(would) >= 1 and would[0].tick == 31          # the spike of frame index 30
    detail = would[0].detail
    assert detail["action"] == "run-scan"
    # the strongest event as (row, column) of the camera: row 60, column 200 are in 4-px bins centred 62 and 202
    assert detail["message"].startswith("would run-scan") and "61.5" in detail["message"] and "201.5" in detail["message"]


# ------------------------------------------------------------- the one-line check

def test_the_check_command_loads_a_file_and_reports_its_parameters(tmp_path):
    done = subprocess.run([sys.executable, "-m", "imswitch.improcess.processors.etsted_pipeline", str(DFF_EVENT)],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    out = done.stdout
    assert "dff_event" in out and "OK" in out
    for name in ("dff_threshold", "bin_px", "baseline_frames", "smooth_px", "min_baseline", "warmup_frames"):
        assert name in out
    assert "(column, row)" in out
    broken = tmp_path / "broken.py"
    broken.write_text("def broken(img, prev_frames, binary_mask, testmode, exinfo, k=1):\n    return 1 / 0, exinfo\n")
    done = subprocess.run([sys.executable, "-m", "imswitch.improcess.processors.etsted_pipeline", str(broken)],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 1 and "broken.py:2" in done.stdout + done.stderr
