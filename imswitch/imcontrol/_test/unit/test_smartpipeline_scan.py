"""Triggered scans: an action that runs on arm, the chain starting at the
first action a tick has inputs for, inverted gates, the scan-geometry
transform, the scan-driven live source and the run-scan adapter against
fakes of the Scan panel and the scan workflow service."""
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcommon.model.smartpipeline import (
    Action, CoordinateError, Frame, Gate, Live, PipelineSettings, Points, ScanGeometry, Sink,
    SmartPipelineDocument, Transform, TransformRegistry, apply_transform, transform_from_dict,
)
from imswitch.imcommon.model.workflow.steps import Process, Workflow
from imswitch.imcontrol.model.smartpipeline import Engine
from imswitch.imcontrol.model.smartpipeline.live_actions import RunScanAdapter
from imswitch.imcontrol.model.smartpipeline.live_source import LiveFrameSource
from imswitch.imcontrol.model.smartpipeline.mock import ArrayFrameSource, FunctionStepRunner, MockActions
from imswitch.imcontrol.model.smartpipeline.ports import ActionRequest
from imswitch.imcontrol._test.unit.test_smartpipeline_live import FakeDetectorsManager

APD = "detector:APD"
GALVO = "scanner:galvo"


def centroid(inputs, params, state):
    image = inputs[0]
    mask = image > 50
    if not mask.any():
        return Points(np.zeros((0, 2)), frame=APD)
    yy, xx = np.nonzero(mask)
    return Points([[yy.mean(), xx.mean()]], frame=APD)


def _frame(cy, cx, size=32):
    image = np.zeros((size, size), np.float32)
    if cy is not None:
        image[cy - 1:cy + 2, cx - 1:cx + 2] = 100
    return image


GEOMETRY = {"axes": [{"device": "X", "step_um": 0.5, "count": 32, "first_um": -7.75},
                     {"device": "Y", "step_um": 0.5, "count": 32, "first_um": -7.75}]}


def _document():
    """Overview on arm; a fine scan where a cell is, then an overview again;
    an overview alone when nothing was found."""
    document = SmartPipelineDocument(Workflow("scan the cells", [
        Live("apd", detector="APD"),
        Process("c", "centroid", {}, inputs=["apd"]),
        Gate("found", "condition", inputs=["c"]),
        Gate("nothing", "condition", {"invert": True}, inputs=["c"]),
        Transform("tf", "geom", inputs=["found"]),
        Action("fine", "run-scan", {"mode": "acquisition", "length_um": 4.0, "pixel_um": 0.1, "frame": GALVO},
               inputs=["tf"]),
        Action("back", "run-scan", {"mode": "overview", "field_um": 16.0}, inputs=["fine.done"]),
        Action("again", "run-scan", {"mode": "overview", "field_um": 16.0}, inputs=["nothing"], on_arm=True),
        Sink("log", "event-log", inputs=["c"]),
    ]))
    document.settings = PipelineSettings(roles={"scouting": "a", "idle": "b"})
    return document


def test_the_scan_geometry_transform_reads_the_frame_it_is_given():
    transform = transform_from_dict({"type": "scan-geometry", "id": "geom", "source_frame": APD, "target_frame": GALVO})
    assert isinstance(transform, ScanGeometry)
    frame = Frame(np.zeros((32, 32)), "APD", 0, 0.0, metadata={"scan_driven": True, "geometry": GEOMETRY})
    points = Points([[16.0, 0.0]], frame=APD)              # row 16, column 0
    moved = apply_transform(points, transform, {"frame": frame})
    assert moved.frame == GALVO and moved.coordinates.tolist() == [[-7.75 + 16 * 0.5, -7.75]]
    with pytest.raises(CoordinateError, match="no scan geometry"):
        apply_transform(points, transform, {"frame": Frame(np.zeros((32, 32)), "APD", 1, 0.0)})
    assert transform.apply(np.zeros((0, 2)), {}).shape == (0, 2)


def test_the_chain_starts_at_whichever_action_the_tick_has_inputs_for_and_on_arm_runs_first():
    frames = [_frame(10, 20), _frame(None, None), _frame(5, 5)]
    source = ArrayFrameSource(frames, detector="APD")
    # the mock source's frames carry a scan geometry, as a captured scan frame would
    original = source.frames

    def frames_with_geometry():
        for frame in original():
            frame.metadata.update({"scan_driven": True, "geometry": GEOMETRY})
            yield frame

    source.frames = frames_with_geometry
    actions = MockActions()
    engine = Engine(_document(), source=source, steps=FunctionStepRunner({"centroid": centroid}), actions=actions,
                    transforms=TransformRegistry([ScanGeometry("geom", APD, GALVO)]), mode="experiment")
    summary = engine.run()
    assert summary.ok, summary.error
    # on arm: the overview; tick 1 (a cell): fine then back; tick 2 (nothing): the overview; tick 3: fine, back
    assert [mode for mode, _ in actions.scans] == ["overview", "acquisition", "overview", "overview",
                                                     "acquisition", "overview"]
    fine_centres = [centre for mode, centre in actions.scans if mode == "acquisition"]
    assert fine_centres[0] == pytest.approx([-7.75 + 10 * 0.5, -7.75 + 20 * 0.5])
    assert [e.event for e in engine.log.events(step="nothing")] == ["closed", "open", "closed"]
    assert engine.log.events("started", step="again")[0].tick == 0     # before any frame
    assert summary.actions_completed == 6


def test_a_scan_driven_source_takes_no_lease_and_ticks_only_on_capture():
    class ScanFrame(np.ndarray):
        geometry = SimpleNamespace(to_dict=lambda: GEOMETRY)

    class APDManager:
        name, isScanDriven, shape, binning, frameStart, parameters = "APD", True, (32, 32), 1, (0, 0), {}
        scale = [0.5, 0.5]

        def getLatestFrameShared(self, is_save=True):
            return np.asarray(_frame(3, 4)).view(ScanFrame)

    manager = FakeDetectorsManager([APDManager()])
    source = LiveFrameSource(manager, "APD")
    source.start()
    assert source.scan_driven and manager.leases == [] and not source.leased
    assert not source.on_image("APD", _frame(1, 1))              # partial frames mid-scan are not ticks
    assert source.capture_latest(metadata={"scan": {"mode": "overview"}})
    frame = source._inbox.take(timeout=0)
    assert frame.metadata["scan_driven"] and frame.metadata["geometry"] == GEOMETRY
    assert frame.metadata["scan"] == {"mode": "overview"} and frame.scale == (0.5, 0.5) and frame.unit == "um"
    assert frame.sequence == 0 and np.array_equal(frame.image, _frame(3, 4))
    source.close()                                                # nothing to release


class _Completion:
    def __init__(self):
        self._callbacks, self.successful, self.message, self.runToken = [], True, "", "tok-1"
        self._done = threading.Event()

    def add_done_callback(self, callback):
        self._callbacks.append(callback)

    def wait(self, timeout=None):
        return self._done.wait(timeout)

    def finish(self, successful=True, message=""):
        self.successful, self.message = successful, message
        self._done.set()
        for callback in self._callbacks:
            callback(self)


class _Workflow:
    def __init__(self, *, accept=True):
        self.accept, self.calls, self.aborted = accept, [], []
        self.completion = None
        self.started = threading.Event()

    def run_scan_prepared(self, recalculate, non_final, notify_starting=True, preferred_source=None,
                          exact_completion=False):
        self.calls.append(preferred_source)
        self.completion = _Completion()
        self.started.set()
        return SimpleNamespace(accepted=self.accept, rejectionMessage="busy" if not self.accept else "",
                               acceptedCompletions=[("owner", "tok-1", self.completion)] if self.accept else [])

    def next_scan(self, timeout=2.0):
        """The completion of the scan the adapter just started (blocks until it has)."""
        assert self.started.wait(timeout), "no scan was started"
        self.started.clear()
        return self.completion

    def abort_scan_from(self, source, runToken=None):
        self.aborted.append((source, runToken))


class _ScanPanel:
    def __init__(self, busy_for=0):
        self.overviews, self.acquisitions = [], []
        self._busy_polls = busy_for

    def _isBusy(self):
        if self._busy_polls > 0:
            self._busy_polls -= 1
            return True
        return False

    def applyOverviewPlan(self, *, field_um=None, lasers=None):
        self.overviews.append((field_um, lasers))

    def applyAcquisitionPlan(self, *, centres_um=None, length_um=None, step_um=None, dwell_s=None, lasers=None):
        self.acquisitions.append((centres_um, length_um, step_um, dwell_s, lasers))


def _request(action_params, inputs=(), epoch=1):
    return ActionRequest(step_id="s", action="run-scan", params={**RunScanAdapter.spec.params, **action_params},
                         inputs=list(inputs), mode="experiment", tick=1, epoch=epoch)


def test_the_run_scan_adapter_sets_the_plan_runs_the_scan_and_captures_the_frame():
    panel, workflow = _ScanPanel(), _Workflow()
    captured = []
    adapter = RunScanAdapter(lambda: {"Scan": panel}, workflow, capture=lambda metadata=None: captured.append(metadata) or True)

    completion = adapter.start(_request({"mode": "overview", "field_um": 60.0, "lasers": ["488 (EXC)"]}), None)
    scan = workflow.next_scan()
    assert not completion.done and panel.overviews == [(60.0, ["488 (EXC)"])] and workflow.calls == ["Scan"]
    scan.finish()
    assert completion.wait(2) and completion.outcome == "ok" and completion.result["scanned"]
    assert captured == [{"scan": {"mode": "overview", "field_um": 60.0}}]

    point = Points([[2.5, -4.0]], frame=GALVO)
    completion = adapter.start(_request({"mode": "acquisition", "length_um": 8.0, "pixel_um": 0.05,
                                         "dwell_s": 0.0001, "lasers": ["561 (EXC)"]}, [point]), None)
    scan = workflow.next_scan()
    assert panel.acquisitions == [({"Y": 2.5, "X": -4.0}, 8.0, 0.05, 0.0001, ["561 (EXC)"])]
    scan.finish(False, "the scan failed")
    assert completion.wait(2) and completion.outcome == "failed" and "the scan failed" in completion.error

    refused = RunScanAdapter(lambda: {"Scan": panel}, _Workflow(accept=False)).start(_request({"mode": "overview"}), None)
    assert refused.wait(2) and refused.outcome == "failed" and "busy" in refused.error
    missing = RunScanAdapter(lambda: {}, workflow).start(_request({"source": "Nope"}), None)
    assert missing.wait(2) and "no scan panel 'Nope'" in missing.error


def test_every_input_point_is_scanned_one_after_the_other():
    panel, workflow = _ScanPanel(), _Workflow()
    captured = []
    adapter = RunScanAdapter(lambda: {"Scan": panel}, workflow, capture=lambda metadata=None: captured.append(metadata) or True)
    cells = Points([[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]], frame=GALVO)
    completion = adapter.start(_request({"mode": "acquisition", "length_um": 12.0, "pixel_um": 0.1}, [cells]), None)
    for index in range(3):
        scan = workflow.next_scan()
        assert not completion.done
        assert panel.acquisitions[index][0] == {"Y": cells.coordinates[index][0], "X": cells.coordinates[index][1]}
        scan.finish()
    assert completion.wait(2) and completion.outcome == "ok"
    assert completion.result["scans"] == 3 and len(captured) == 3 and workflow.calls == ["Scan"] * 3

    first_only = adapter.start(_request({"mode": "acquisition", "points": "first"}, [cells]), None)
    workflow.next_scan().finish()
    assert first_only.wait(2) and first_only.result["scans"] == 1
    limited = adapter.start(_request({"mode": "acquisition", "max_points": 2}, [cells]), None)
    workflow.next_scan().finish()
    workflow.next_scan().finish()
    assert limited.wait(2) and limited.result["scans"] == 2


def test_the_adapter_waits_for_the_panel_to_be_idle_before_setting_a_plan():
    panel, workflow = _ScanPanel(busy_for=3), _Workflow()
    adapter = RunScanAdapter(lambda: {"Scan": panel}, workflow)
    completion = adapter.start(_request({"mode": "overview"}), None)
    scan = workflow.next_scan()                   # started once the panel reported idle
    assert panel.overviews == [(60.0, None)] and panel._busy_polls == 0
    scan.finish()
    assert completion.wait(2) and completion.outcome == "ok"

    class Stuck(_ScanPanel):
        def _isBusy(self):
            return True

    stuck = RunScanAdapter(lambda: {"Scan": Stuck()}, _Workflow())
    completion = stuck.start(_request({"mode": "overview"}), None)
    stuck._wait_idle = lambda source, timeout_s=0.1: RunScanAdapter._wait_idle(stuck, source, 0.1)
    assert completion.wait(8) and completion.outcome == "failed" and "stayed busy" in completion.error


def test_aborting_a_running_scan_asks_the_workflow_to_abort_it():
    panel, workflow = _ScanPanel(), _Workflow()
    adapter = RunScanAdapter(lambda: {"Scan": panel}, workflow)
    cells = Points([[1.0, 2.0], [3.0, 4.0]], frame=GALVO)
    completion = adapter.start(_request({"mode": "acquisition"}, [cells]), None)
    scan = workflow.next_scan()
    assert completion.abort() and completion.outcome == "cancelled" and completion.ended
    assert workflow.aborted == [("owner", "tok-1")]
    scan.finish()                          # the current scan ends: no second scan is started
    time.sleep(0.2)
    assert workflow.calls == ["Scan"]
