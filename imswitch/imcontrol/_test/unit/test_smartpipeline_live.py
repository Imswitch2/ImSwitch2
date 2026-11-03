"""The live run's parts against fakes of ImControl's managers: the detector
source (lease, envelope, duplicates), the owner-aware move-to adapter
(reservation tokens on the manager call, quarantine), and the run's arm /
stop order (reserve, role, lease; idle, release, lease)."""
import threading
import time

import numpy as np
import pytest

from imswitch.imcommon.model.smartpipeline import (
    Action,
    Gate,
    Live,
    PipelineSettings,
    Points,
    ScaleFlip,
    SmartPipelineDocument,
    Transform,
    TransformRegistry,
)
from imswitch.imcommon.model.workflow.steps import Process, Workflow
from imswitch.imcontrol.model.measurement.controls import ControlExecutor
from imswitch.imcontrol.model.resources import ResourceRegistry, ResourceReservedError, positioner_key
from imswitch.imcontrol.model.smartpipeline.catalog import axis_targets
from imswitch.imcontrol.model.smartpipeline.live_actions import MoveToAdapter, live_backend
from imswitch.imcontrol.model.smartpipeline.live_source import LiveFrameSource, exposure_seconds, frame_identity_of
from imswitch.imcontrol.model.smartpipeline.mock import FunctionStepRunner
from imswitch.imcontrol.model.smartpipeline.ports import ActionRequest
from imswitch.imcontrol.model.smartpipeline.run import ARMED, FAILED, RUNNING, STOPPED, PipelineRun
from imswitch.imcontrol.model.smartpipeline.engine import ArmError

CAMERA = "detector:Camera"
STAGE = "stage:XY"


# -- fakes ------------------------------------------------------------------------------

class _Parameter:
    def __init__(self, value, units):
        self.value, self.valueUnits = value, units


class FakeDetector:
    def __init__(self, name="Camera", shape=(800, 600), binning=2, frameStart=(10, 20)):
        self.name = name
        self.shape = shape            # (width, height), as DetectorManager has it
        self.binning = binning
        self.frameStart = frameStart  # (x, y)
        self.parameters = {"exposure": _Parameter(20.0, "ms")}


class FakeDetectorsManager:
    """acquire / release / [] like DetectorsManager; release can time out."""

    def __init__(self, detectors, *, release_timeouts=0):
        self._detectors = {d.name: d for d in detectors}
        self.leases = []
        self.released = []
        self._release_timeouts = release_timeouts

    def __getitem__(self, name):
        return self._detectors[name]

    def getAllDeviceNames(self):
        return list(self._detectors)

    def acquire(self, names, purpose):
        for name in names:
            if name not in self._detectors:
                raise KeyError(name)
        lease = (tuple(names), purpose)
        self.leases.append(lease)
        return lease

    def release(self, lease):
        if lease not in self.leases:
            raise ValueError("not leased")
        if self._release_timeouts > 0:
            self._release_timeouts -= 1
            raise TimeoutError("poll thread did not stop")
        self.leases.remove(lease)
        self.released.append(lease)


class FakePositioner:
    """A single-axis stage whose guarded calls admit through the registry."""

    def __init__(self, name, axis, registry, *, delay_s=0.0):
        self.name, self.axis, self._registry = name, axis, registry
        self.position = {axis: 0.0}
        self.calls = []
        self.delay_s = delay_s

    def _admit(self, method, owner):
        ticket = self._registry.admit(positioner_key(self.name), owner, label=f"FakePositioner.{method}")
        self._registry.release_ticket(ticket)

    def setPosition(self, value, axis, owner=None):
        self._admit("setPosition", owner)
        self.calls.append(("set", axis, value, owner))
        time.sleep(self.delay_s)
        self.position[axis] = float(value)

    def move(self, dist, axis, owner=None):
        self._admit("move", owner)
        self.calls.append(("move", axis, dist, owner))
        self.position[axis] = self.position.get(axis, 0.0) + float(dist)


class FakePositioners(dict):
    pass


def centroid(inputs, params, state):
    image = inputs[0]
    mask = image > 50
    if not mask.any():
        return Points(np.zeros((0, 2)), frame=CAMERA)
    yy, xx = np.nonzero(mask)
    return Points([[yy.mean(), xx.mean()]], frame=CAMERA)


def _image(cy=20, cx=20, size=64):
    image = np.zeros((size, size), np.float32)
    if cy is not None:
        image[cy - 2:cy + 3, cx - 2:cx + 3] = 100
    return image


def _document(*, roles=True, axes=("Mock Y:Y", "Mock X:X")):
    steps = [
        Live("cam", detector="Camera"),
        Process("c", "centroid", {}, inputs=["cam"]),
        Gate("g", "condition", inputs=["c"]),
        Transform("tf", "cam2stage", inputs=["g"]),
        Action("mv", "move-to", {"axes": list(axes), "frame": STAGE, "deadline_s": 0.2}, inputs=["tf"]),
    ]
    document = SmartPipelineDocument(Workflow("live", steps))
    if roles:
        document.settings = PipelineSettings(roles={"scouting": "scout", "idle": "rest"})
    return document


def _transforms():
    return TransformRegistry([ScaleFlip("cam2stage", CAMERA, STAGE, pixel_size=(0.5, 0.5))])


@pytest.fixture
def registry():
    return ResourceRegistry()


@pytest.fixture
def positioners(registry):
    return FakePositioners({"Mock X": FakePositioner("Mock X", "X", registry),
                            "Mock Y": FakePositioner("Mock Y", "Y", registry)})


# -- the live source ---------------------------------------------------------------------

def test_the_source_takes_the_lease_and_stamps_the_envelope():
    manager = FakeDetectorsManager([FakeDetector()])
    clock = iter([10.0, 11.0, 12.0]).__next__
    source = LiveFrameSource(manager, "Camera", clock=clock)
    assert not source.leased and not source.on_image("Camera", _image())   # not leased yet: dropped
    source.start()
    assert manager.leases and str(manager.leases[0][1]).endswith("EVENT_STREAM")
    assert source.identity.shape == (600, 800) and source.identity.binning == (2, 2)
    assert source.identity.offset == (20, 10)

    assert source.on_image("Other", _image()) is False and source.ignored == 1
    image = _image()
    assert source.on_image("Camera", image, False, [0.1, 0.1], True)
    frame = source._inbox.take(timeout=0)
    assert frame.sequence == 0 and frame.t == 10.0 and frame.exposure_s == 0.02
    assert frame.scale == (0.1, 0.1) and frame.unit == "um" and frame.identity == source.identity
    assert frame.image is not image and np.array_equal(frame.image, image)   # a copy
    assert frame.metadata == {"init": False, "current": True}
    source.close()
    assert manager.released == [manager.leases[0]] if manager.leases else manager.released


def test_a_re_read_frame_is_a_duplicate_and_the_latest_wins():
    manager = FakeDetectorsManager([FakeDetector()])
    source = LiveFrameSource(manager, "Camera")
    source.start()
    image = _image()
    assert source.on_image("Camera", image)
    assert not source.on_image("Camera", image.copy())        # the poll re-read the same frame
    assert source.duplicates == 1 and source.stats.duplicates == 1
    assert source.on_image("Camera", _image(30, 30))           # replaces the waiting one
    assert source.on_image("Camera", _image(40, 40))
    assert source.stats.replaced == 2
    frames = []
    source.stop()
    frames.extend(source.frames())
    assert [f.sequence for f in frames] == [2]


def test_a_release_that_times_out_stays_pending_and_can_be_retried():
    manager = FakeDetectorsManager([FakeDetector()], release_timeouts=1)
    source = LiveFrameSource(manager, "Camera")
    source.start()
    source.close()
    assert source.release_pending and source.leased
    assert source.retry_release() and not source.release_pending and not source.leased
    assert manager.leases == []


def test_detector_readers_are_tolerant():
    detector = FakeDetector(shape=(100,), binning="x", frameStart=None)
    identity = frame_identity_of(detector)
    assert identity.shape == (100,) and identity.binning == (1, 1) and identity.offset == (0, 0)
    detector.parameters = {"Exposure": _Parameter("0.5", "s")}
    assert exposure_seconds(detector) == 0.5
    detector.parameters = {}
    assert exposure_seconds(detector) is None


# -- the move-to adapter -------------------------------------------------------------------

def test_axis_targets_resolve_columns_to_positioner_axes():
    assert axis_targets({"positioner": "XY", "axes": ["Y", "X"]}) == [("XY", "Y"), ("XY", "X")]
    assert axis_targets({"axes": ["Mock Y:Y", "Mock X:X"]}) == [("Mock Y", "Y"), ("Mock X", "X")]
    assert axis_targets({"positioner": "XY", "axes": "Y"}) == [("XY", "Y")]
    with pytest.raises(ValueError, match="names no positioner"):
        axis_targets({"axes": ["Y"]})
    with pytest.raises(ValueError, match="is empty"):
        axis_targets({"positioner": "XY", "axes": ["Y", ""]})


def _request(points, params, epoch=1):
    return ActionRequest(step_id="mv", action="move-to", params={"relative": False, "deadline_s": 1.0, **params},
                         inputs=[points], mode="experiment", tick=1, epoch=epoch)


def test_move_to_drives_each_column_with_the_reservation_token(registry, positioners):
    adapter = MoveToAdapter(positioners, executor=ControlExecutor())
    params = {"axes": ["Mock Y:Y", "Mock X:X"]}
    assert adapter.resources(params) == {positioner_key("Mock Y"), positioner_key("Mock X")}
    reservation = registry.reserve(adapter.resources(params), "pipeline")

    # the GUI (no token) is refused while the pipeline holds the stages
    with pytest.raises(ResourceReservedError):
        positioners["Mock X"].setPosition(1.0, "X")

    completion = adapter.start(_request(Points([[3.0, 4.0]], frame=STAGE), params), reservation.token)
    assert completion.done and completion.ended and completion.outcome == "ok"
    assert positioners["Mock Y"].position == {"Y": 3.0} and positioners["Mock X"].position == {"X": 4.0}
    assert positioners["Mock X"].calls[0][3] == reservation.token
    assert completion.result == {"moved": True, "relative": False,
                                 "position": {"Mock Y": {"Y": 3.0}, "Mock X": {"X": 4.0}}}

    relative = adapter.start(_request(Points([[1.0, 1.0]], frame=STAGE), {**params, "relative": True}),
                             reservation.token)
    assert relative.outcome == "ok" and positioners["Mock Y"].position == {"Y": 4.0}

    empty = adapter.start(_request(Points([], frame=STAGE), params), reservation.token)
    assert empty.outcome == "ok" and empty.result == {"moved": False, "reason": "no points"}


def test_move_to_without_a_token_is_refused_by_a_reserved_stage(registry, positioners):
    registry.reserve({positioner_key("Mock X")}, "someone else")
    adapter = MoveToAdapter(positioners, executor=ControlExecutor())
    completion = adapter.start(_request(Points([[1.0, 2.0]], frame=STAGE), {"axes": ["Mock Y:Y", "Mock X:X"]}), None)
    assert completion.outcome == "failed" and completion.ended
    assert "ResourceReservedError" in completion.error and "Mock X" in completion.error


def test_a_stage_that_does_not_return_is_quarantined_and_the_action_has_not_ended(registry, positioners):
    positioners["Mock X"].delay_s = 0.5
    executor = ControlExecutor()
    adapter = MoveToAdapter(positioners, executor=executor)
    params = {"axes": ["Mock X:X"], "deadline_s": 0.05}
    completion = adapter.start(_request(Points([[7.0, 0.0]], frame=STAGE), params), None)
    assert completion.done and not completion.ended and completion.outcome == "failed"
    assert "did not finish" in completion.error
    assert adapter.quarantined == {positioner_key("Mock X")} and executor.is_quarantined(positioner_key("Mock X"))
    # a second command on the quarantined stage is refused at once
    again = adapter.start(_request(Points([[1.0, 0.0]], frame=STAGE), params), None)
    assert again.outcome == "failed" and "quarantined" in again.error
    assert executor.wait_released(positioner_key("Mock X"), 2.0)


# -- the run ---------------------------------------------------------------------------------

def _run(document, registry, positioners, *, mode="experiment", roles=None, executor=None, detectors=None):
    manager = detectors or FakeDetectorsManager([FakeDetector(shape=(64, 64), binning=1, frameStart=(0, 0))])
    source = LiveFrameSource(manager, "Camera")
    actions = live_backend(positioners, executor=executor or ControlExecutor())
    applied = roles if roles is not None else []

    def apply_mode(name):
        applied.append(name)
        return None if name in ("scout", "rest") else f"no setup mode {name!r}"

    run = PipelineRun(document, source=source, steps=FunctionStepRunner({"centroid": centroid}), actions=actions,
                      transforms=_transforms(), mode=mode, apply_mode=apply_mode, resource_registry=registry,
                      executor=executor or ControlExecutor(), owner="smart-pipeline:test")
    return run, source, manager, applied


def _feed(source, images, settle_s=0.3):
    for index, image in enumerate(images):
        source.on_image("Camera", image)
        time.sleep(0.05)
    time.sleep(settle_s)


def test_arm_reserves_applies_scouting_and_takes_the_lease_in_order(registry, positioners):
    roles = []
    run, source, manager, _ = _run(_document(), registry, positioners, roles=roles)
    run.arm()
    assert run.state == ARMED
    assert run.reserved == {positioner_key("Mock X"), positioner_key("Mock Y")} and run.token
    assert registry.holder(positioner_key("Mock X")).owner == "smart-pipeline:test"
    assert roles == ["scout"] and source.leased
    events = [e.event for e in run.log.events(step="pipeline")]
    assert events[:3] == ["reserved", "role", "armed"]

    run.start()
    assert run.state == RUNNING
    _feed(source, [_image(20, 20), _image(None, None), _image(40, 10)])
    run.stop()
    assert run.join(3) and run.state == STOPPED, run.error
    # the stage moved to the transformed points, with the token
    assert positioners["Mock Y"].position == {"Y": 20.0} and positioners["Mock X"].position == {"X": 5.0}
    assert all(call[3] == run.token or call[3] is not None for call in positioners["Mock X"].calls)
    # cleanup: idle applied, reservations released, lease released
    assert roles == ["scout", "rest"]
    assert registry.holder(positioner_key("Mock X")) is None and run.token is None
    assert not source.leased and manager.leases == []
    assert run.engine.summary.actions_completed == 2 and run.status()["state"] == STOPPED


def test_a_device_held_by_someone_else_refuses_the_whole_arm(registry, positioners):
    registry.reserve({positioner_key("Mock Y")}, "a script")
    roles = []
    run, source, manager, _ = _run(_document(), registry, positioners, roles=roles)
    with pytest.raises(ArmError, match="cannot reserve"):
        run.arm()
    assert run.state != ARMED and roles == [] and not source.leased and run.token is None
    assert registry.holder(positioner_key("Mock X")) is None


def test_a_missing_setup_mode_refuses_the_arm_and_releases_what_was_reserved(registry, positioners):
    document = _document()
    document.settings = PipelineSettings(roles={"scouting": "nope", "idle": "rest"})
    run, source, manager, applied = _run(document, registry, positioners)
    with pytest.raises(ArmError, match="no setup mode 'nope'"):
        run.arm()
    assert registry.holder(positioner_key("Mock X")) is None and not source.leased


def test_validate_mode_reserves_but_applies_no_role_and_moves_nothing(registry, positioners):
    roles = []
    run, source, manager, _ = _run(_document(), registry, positioners, mode="validate", roles=roles)
    run.arm()
    assert run.reserved and roles == []
    run.start()
    _feed(source, [_image(20, 20)])
    run.stop()
    assert run.join(3) and run.state == STOPPED
    assert positioners["Mock X"].calls == [] and run.log.events("would", step="mv")
    assert registry.holder(positioner_key("Mock X")) is None


def test_visualize_mode_reserves_nothing(registry, positioners):
    run, source, manager, applied = _run(_document(roles=False), registry, positioners, mode="visualize")
    run.arm()
    assert run.reserved == set() and run.token is None and applied == []
    run.stop()
    assert run.state == STOPPED and not source.leased


def test_a_lease_refusal_undoes_the_reservation_and_the_role(registry, positioners):
    roles = []
    manager = FakeDetectorsManager([FakeDetector(name="Other")])
    run, source, manager, _ = _run(_document(), registry, positioners, roles=roles, detectors=manager)
    with pytest.raises(KeyError):
        run.arm()
    assert registry.holder(positioner_key("Mock X")) is None and roles == ["scout", "rest"]


def test_a_quarantined_stage_stays_reserved_until_it_returns(registry, positioners):
    positioners["Mock X"].delay_s = 1.5          # the stage returns long after the 0.2 s deadline
    executor = ControlExecutor()
    run, source, manager, _ = _run(_document(), registry, positioners, executor=executor)
    run.arm()
    run.start()
    _feed(source, [_image(20, 20)], settle_s=0.4)
    assert run.join(3) and run.state == FAILED and "did not finish" in run.error
    assert run.status()["unfinished_action"] == "mv"
    key = positioner_key("Mock X")
    assert registry.holder(key) is not None, "the stuck stage must stay protected"
    assert registry.holder(positioner_key("Mock Y")) is None
    assert executor.wait_released(key, 3.0)
    deadline = time.monotonic() + 1.0
    while registry.holder(key) is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert registry.holder(key) is None


def test_stop_is_idempotent_and_safe_from_another_thread(registry, positioners):
    run, source, manager, _ = _run(_document(), registry, positioners)
    run.arm()
    run.start()
    threads = [threading.Thread(target=run.stop) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    assert run.join(3) and run.state == STOPPED and not source.leased
    run.stop()   # after the end: nothing happens
    assert run.state == STOPPED
