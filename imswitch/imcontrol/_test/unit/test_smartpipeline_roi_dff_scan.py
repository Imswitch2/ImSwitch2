"""The shipped ROI dF/F -> bounding-box APD scan pipeline, end to end on the mock.

A camera frame sequence in which known ROIs brighten, the real ``roi-dff`` / ``pick`` processors through the
step runner, the real gates and ``transform`` step (a scale-flip), and the real ``run-scan`` adapter against a
fake Advanced scan panel (setters, getters, a scanner range) and a fake scan workflow."""
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcommon.model.smartpipeline import (
    PipelineSettings, ScaleFlip, SmartPipelineDocument, TransformRegistry,
)
from imswitch.imcontrol.model.smartpipeline import Engine
from imswitch.imcontrol.model.smartpipeline.boxes import map_boxes, plan_box_scan
from imswitch.imcontrol.model.smartpipeline.catalog import catalog
from imswitch.imcontrol.model.smartpipeline.live_actions import LiveActionBackend, RunScanAdapter
from imswitch.imcontrol.model.smartpipeline.mock import ArrayFrameSource
from imswitch.improcess.workflows import smartpipeline as improcess_smartpipeline
from imswitch.improcess.workflows.runtime import bootstrap_registry

import imswitch

DOCUMENT = Path(imswitch.__file__).parent / "_data" / "user_defaults" / "smart_pipelines" / "roi_dff_triggered_apd_scan.json"
CAMERA = "detector:Camera"
GALVO = "scanner:galvo"


@pytest.fixture(scope="module")
def registry():
    return bootstrap_registry(user_plugins=False)


# ---------------------------------------------------------------- fakes

class _Done:
    successful, message, runToken = True, "", "tok"

    def add_done_callback(self, callback):
        callback(self)

    def wait(self, timeout=None):
        return True


class _Workflow:
    """Starts a scan: records what the panel held at that moment; finishes at once."""

    def __init__(self, panel):
        self.panel, self.scans = panel, []

    def run_scan_prepared(self, *args, **kwargs):
        self.scans.append({"centre": dict(self.panel.centres), "size": dict(self.panel.sizes),
                           "step": dict(self.panel._widget.steps), "lines": dict(self.panel._widget.lines)})
        return SimpleNamespace(accepted=True, rejectionMessage="", acceptedCompletions=[("owner", "tok", _Done())])

    def abort_scan_from(self, *args, **kwargs):
        pass


class _Widget:
    def __init__(self):
        self.steps, self.dwell, self.lines = {"Y": 0.5, "X": 0.5}, 0.0001, {"488 (EXC)": True, "561 (EXC)": False}
        self.centres, self.sizes = {}, {}

    def setScanStepSize(self, name, step):
        self.steps[name] = step

    def getScanStepSize(self, name):
        return self.steps[name]

    def getScanCenterPos(self, name):
        return self.centres[name]

    def getScanSize(self, name):
        return self.sizes[name]

    def setSeqTimePar(self, seconds):
        self.dwell = seconds

    def getSeqTimePar(self):
        return self.dwell

    def setLineStepEnabled(self, device, index, enabled):
        self.lines[device] = enabled

    def getLineStepEnabled(self, device, index):
        return self.lines[device]


class _Advanced:
    """No applyAcquisitionPlan: the Advanced panel's controller, with a +-20 um scanner."""

    def __init__(self):
        self._widget = _Widget()
        self.centres, self.sizes = {"Y": 1.0, "X": -2.0}, {"Y": 60.0, "X": 60.0}
        self._widget.centres, self._widget.sizes = self.centres, self.sizes
        props = {"minVolt": -10.0, "maxVolt": 10.0, "conversionFactor": 2.0}
        self._setupInfo = SimpleNamespace(
            lasers={"488 (EXC)": None, "561 (EXC)": None},
            positioners={"Y": SimpleNamespace(managerProperties=props), "X": SimpleNamespace(managerProperties=props)})

    def changeScanCenterPos(self, name, value):
        self.centres[name] = value

    def changeScanSize(self, name, size):
        self.sizes[name] = size

    def _isBusy(self):
        return False


# ---------------------------------------------------------------- scene

def _scene(a=1.0, b=1.0, shape=(64, 64)):
    image = np.full(shape, 20.0)
    image[8:16, 8:16] = 200.0 * a        # cell A: rows/cols 8..15  -> scan um -12.25..-8.25
    image[40:48, 40:48] = 200.0 * b      # cell B: rows/cols 40..47 -> scan um   3.75..7.75
    return image


def _frames(n_reference_and_baseline, *events):
    """Resting frames, then one frame per ``(a, b)`` in ``events``."""
    return [_scene() for _ in range(n_reference_and_baseline)] + [_scene(a, b) for a, b in events]


def _run(frames, *, mode="experiment", dff=None, scan=None, gates=None, panel=None, interval=10.0):
    payload = json.loads(DOCUMENT.read_text(encoding="utf-8"))
    for step in payload["steps"]:
        if step["id"] == "dff":
            step["params"].update({"reference_frames": 1, "baseline_frames": 4, "smooth_sigma": 0.0,
                                   "background": "none", "min_f0": 1.0, "min_consecutive": 1, "refractory_frames": 0,
                                   "clear_border": False, **(dff or {})})
        elif step["id"] == "scan":
            step["params"].update(scan or {})
        elif step["id"] in (gates or {}):
            step["params"].update(gates[step["id"]])
    document = SmartPipelineDocument.from_dict(payload)
    assert document.settings.roles
    panel = panel or _Advanced()
    workflow = _Workflow(panel)
    actions = LiveActionBackend([RunScanAdapter(lambda: {"Scan": panel}, workflow)], catalog=catalog())
    clock = {"t": 0.0}
    source = ArrayFrameSource(frames, detector="Camera", interval_s=interval)
    original = source.frames

    def timed():
        for frame in original():
            clock["t"] = frame.t
            yield frame

    source.frames = timed
    registry = bootstrap_registry(user_plugins=False)
    transforms = TransformRegistry([ScaleFlip("cam_to_scan", CAMERA, GALVO, pixel_size=(0.5, 0.5), origin=(32.0, 32.0))])
    engine = Engine(document, source=source, steps=improcess_smartpipeline.ProcessorStepRunner(registry),
                    actions=actions, transforms=transforms, mode=mode, registry=registry, clock=lambda: clock["t"])
    summary = engine.run()
    assert summary.ok, summary.error
    return engine, workflow, panel


# ---------------------------------------------------------------- tests

def test_the_shipped_document_validates(registry):
    document = SmartPipelineDocument.load(DOCUMENT)
    transforms = TransformRegistry([ScaleFlip("cam_to_scan", CAMERA, GALVO, pixel_size=(0.5, 0.5))])
    issues = document.validate(registry=registry, actions=catalog(), transforms=transforms)
    assert issues == [], [str(i) for i in issues]


def test_no_trigger_during_warmup():
    # ticks 1..4 are the baseline: a cell that is bright there is baseline, not an event
    engine, workflow, _ = _run([_scene(), _scene(), _scene(3.0, 3.0), _scene()])
    assert workflow.scans == [] and engine.log.events("started", step="scan") == []
    assert {e.event for e in engine.log.events(step="triggered")} == {"closed"}   # no row ever triggered


def test_the_brightest_of_two_triggered_rois_is_scanned_as_the_transformed_padded_box():
    frames = _frames(4, (1.5, 2.0))        # tick 5: A +50 %, B +100 %: both over 0.3, B is the strongest
    engine, workflow, panel = _run(frames)
    starts = engine.log.events("started", step="scan")
    assert len(starts) == 1 and starts[0].tick == 5          # not during warm-up (ticks 1..4)
    assert len(workflow.scans) == 1
    scan = workflow.scans[0]
    # B (the stronger of the two over threshold): box 3.75..7.75 um padded by 1 + 10 % per side = 6.8 um,
    # a whole number of 0.5 um steps (14) = 7.0 um, centred on the box centre 5.75 um, both axes
    assert scan["centre"] == pytest.approx({"Y": 5.75, "X": 5.75})
    assert scan["size"] == pytest.approx({"Y": 7.0, "X": 7.0})
    assert scan["step"] == {"Y": 0.5, "X": 0.5}               # the panel's step is kept
    assert scan["lines"] == {"488 (EXC)": False, "561 (EXC)": True}
    # the panel is put back
    assert panel.centres == {"Y": 1.0, "X": -2.0} and panel.sizes == {"Y": 60.0, "X": 60.0}
    assert panel._widget.lines == {"488 (EXC)": True, "561 (EXC)": False} and panel._widget.dwell == 0.0001
    # the trigger is in the log: ROI, dff, F, F0, the box in camera px and in scan um, the transform
    detail = starts[0].detail["inputs"][0]
    row = detail["first_row"]
    assert row["dff"] == pytest.approx(1.0) and row["F"] == pytest.approx(400.0) and row["F0"] == pytest.approx(200.0)
    assert [row[k] for k in ("src_bbox_y0", "src_bbox_x0", "src_bbox_y1", "src_bbox_x1")] == [39.5, 39.5, 47.5, 47.5]
    assert [row[k] for k in ("bbox_y0", "bbox_x0", "bbox_y1", "bbox_x1")] == [3.75, 3.75, 7.75, 7.75]
    assert detail["transform"]["id"] == "cam_to_scan"
    done = engine.log.events("done", step="scan")[0].detail["result"]
    assert done["scanned"] and done["trigger"]["label"] == row["label"] and done["pixels"] == {"Y": 14, "X": 14}
    assert done["latency_s"]["wait_idle"] < 0.5


def test_refractory_and_the_scan_budget_are_respected():
    events = [(1.0, 2.0)] * 6
    engine, workflow, _ = _run(_frames(4, *events), dff={"refractory_frames": 3}, gates={"rest": {"seconds": 0.5}})
    # triggers on frames 5, 9 (refractory 3 frames) -> two scans
    assert len(workflow.scans) == 2
    engine, workflow, _ = _run(_frames(4, *events), gates={"rest": {"seconds": 0.5}, "budget": {"count": 3}})
    assert len(workflow.scans) == 3
    engine, workflow, _ = _run(_frames(4, *events), interval=1.0)         # min-interval 5 s of the clock
    assert len(workflow.scans) == 2


def test_dry_run_starts_no_scan_and_logs_the_rectangle_it_would_scan():
    engine, workflow, panel = _run(_frames(4, (1.0, 2.0)), mode="validate")
    assert workflow.scans == [] and engine.log.events("started", step="scan") == []
    would = engine.log.events("would", step="scan")
    assert len(would) == 1
    plan = would[0].detail["scan_plan"][0]
    assert plan["skip"] is None and plan["centre_um"] == pytest.approx({"Y": 5.75, "X": 5.75})
    assert plan["size_um"] == pytest.approx({"Y": 7.0, "X": 7.0})
    assert "would scan" in would[0].detail["message"]
    assert panel.centres == {"Y": 1.0, "X": -2.0}                          # untouched


def test_a_box_outside_the_range_is_skipped_with_a_reason_and_nothing_is_started():
    engine, workflow, panel = _run(_frames(4, (3.0, 1.0)), scan={"range_um": {"Y": [-8, 8], "X": [-8, 8]}})
    assert workflow.scans == []                       # cell A (-12 .. -8 um) is outside +-8 um
    done = engine.log.events("done", step="scan")[0].detail["result"]
    assert done["scanned"] is False and "outside the scanner range" in done["skipped"][0]["reason"]
    assert panel.centres == {"Y": 1.0, "X": -2.0}


def test_a_box_partly_outside_is_clipped_to_the_range():
    engine, workflow, _ = _run(_frames(4, (1.0, 2.0)), scan={"range_um": {"Y": [0, 8], "X": [0, 8]}})
    scan = workflow.scans[0]     # padded box [2.35, 9.15] clipped to [2.35, 8], 12 steps = 6 um, centre kept inside
    assert scan["centre"] == pytest.approx({"Y": 5.0, "X": 5.0}) and scan["size"] == pytest.approx({"Y": 6.0, "X": 6.0})
    # less than half of the padded box inside the range: skipped, not squeezed
    engine, workflow, _ = _run(_frames(4, (1.0, 2.0)), scan={"range_um": {"Y": [0, 6], "X": [0, 6]}})
    assert workflow.scans == [] and "of the padded box lies inside" in \
        engine.log.events("done", step="scan")[0].detail["result"]["skipped"][0]["reason"]
    engine, workflow, _ = _run(_frames(4, (1.0, 2.0)), scan={"range_um": {"Y": [0, 8], "X": [0, 8]}})
    done = engine.log.events("done", step="scan")[0].detail["result"]
    assert any("clipped" in note for note in done["notes"])


def test_the_range_comes_from_the_positioner_volts_times_conversion():
    # +-10 V x 2 um/V = +-20 um; a 60 um field is not a box, a box beyond 20 um is skipped
    plan = plan_box_scan({"Y": (30.0, 34.0), "X": (0.0, 4.0)}, steps={"Y": 0.5, "X": 0.5},
                         ranges={"Y": (-20.0, 20.0), "X": (-20.0, 20.0)})
    assert plan["skip"] and "outside the scanner range" in plan["skip"]


def test_size_limits_and_whole_steps():
    plan = plan_box_scan({"Y": (0.0, 1.0), "X": (0.0, 100.0)}, steps={"Y": 0.3, "X": 0.5}, min_size_um=2.0,
                         max_size_um=40.0)
    assert plan["size"]["Y"] == pytest.approx(2.1) and plan["size"]["X"] == pytest.approx(40.0)
    assert plan["pixels"] == {"Y": 7, "X": 80} and len(plan["notes"]) == 2


def test_box_columns_are_mapped_through_all_four_corners_of_a_rotation():
    from imswitch.imcommon.model.smartpipeline import Points

    class Rotate90:
        id = "rot"

        def apply(self, points, context=None):
            points = np.asarray(points)
            return np.column_stack([points[:, 1], -points[:, 0]])

    props = {"bbox_y0": np.array([0.0]), "bbox_x0": np.array([0.0]), "bbox_y1": np.array([2.0]), "bbox_x1": np.array([6.0])}
    source = Points([[1.0, 3.0]], frame=CAMERA, properties=props)
    moved = Points([[3.0, -1.0]], frame=GALVO, properties=dict(props))
    map_boxes(source, moved, Rotate90(), {})
    assert [moved.properties[k][0] for k in ("bbox_y0", "bbox_x0", "bbox_y1", "bbox_x1")] == [0.0, -2.0, 6.0, 0.0]
    assert moved.properties["src_bbox_x1"][0] == 6.0
