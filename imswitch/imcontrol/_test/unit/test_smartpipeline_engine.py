"""The smart-pipeline engine against its mock ports: a replay of frames
through detection, a gate, a transform and an action chain, in every run
mode; busy, stale, timeout, failure and stop; the inbox; the event log."""
import csv
import json
import threading
import time

import numpy as np
import pytest

from imswitch.imcommon.model.smartpipeline import (
    Action,
    Event,
    Frame,
    FrameIdentity,
    Gate,
    Live,
    PipelineSettings,
    Points,
    ScaleFlip,
    Sink,
    SmartPipelineDocument,
    Transform,
    TransformRegistry,
    Value,
)
from imswitch.imcommon.model.workflow.steps import Process, Workflow
from imswitch.imcontrol.model.smartpipeline import (
    ArmError,
    CoalescingInbox,
    Engine,
    EngineError,
    EventLog,
    RunMode,
    SimpleCompletion,
)
from imswitch.imcontrol.model.smartpipeline.engine import DROPPED_BUSY, DROPPED_STALE, RAN, TICK_FAILED
from imswitch.imcontrol.model.smartpipeline.mock import (
    ArrayFrameSource,
    FunctionStepRunner,
    MockActions,
    sleep_clock,
)

CAMERA = "detector:Camera"
STAGE = "stage:XY"


# -- fixtures ------------------------------------------------------------------------

def _frame_with_disc(cy, cx, size=64, level=100.0):
    image = np.zeros((size, size), np.float32)
    if cy is not None:
        image[cy - 3:cy + 4, cx - 3:cx + 4] = level
    return image


def centroid(inputs, params, state):
    """A detection: the centroid of what is above ``level``, or no points."""
    image = inputs[0]
    mask = image > params.get("level", 50)
    state["calls"] = state.get("calls", 0) + 1
    if not mask.any():
        return Points(np.zeros((0, 2)), frame=CAMERA)
    yy, xx = np.nonzero(mask)
    return Points([[yy.mean(), xx.mean()]], frame=CAMERA)


def brightness(inputs, params, state):
    return Value(float(np.max(inputs[0])), unit="counts")


FUNCTIONS = {"centroid": centroid, "brightness": brightness}

FRAMES = [_frame_with_disc(20, 20), _frame_with_disc(None, None), _frame_with_disc(40, 10)]


def transforms():
    return TransformRegistry([ScaleFlip("cam2stage", CAMERA, STAGE, pixel_size=(0.5, 0.5))])


def pipeline(*, roles=True, extra=(), chain=("move", "snap"), gate=Gate("g", "condition", inputs=["c"])):
    steps = [
        Live("cam", detector="Camera"),
        Process("c", "centroid", {}, inputs=["cam"]),
        gate,
        Transform("tf", "cam2stage", inputs=["g"]),
    ]
    if "move" in chain:
        steps.append(Action("mv", "move-to", {"positioner": "XY", "frame": STAGE}, inputs=["tf"]))
    if "snap" in chain:
        steps.append(Action("sn", "snap", {"detector": "Camera"}, inputs=["mv"]))
    steps.append(Sink("log", "event-log", inputs=["g.fired"]))
    steps.extend(extra)
    document = SmartPipelineDocument(Workflow("find the cell", steps))
    if roles:
        document.settings = PipelineSettings(roles={"scouting": "widefield", "idle": "off"})
    return document


def engine(document=None, *, frames=FRAMES, mode="validate", actions=None, clock=None, **kw):
    document = document or pipeline()
    return Engine(
        document, source=ArrayFrameSource(frames), steps=FunctionStepRunner(FUNCTIONS),
        actions=actions if actions is not None else MockActions(), transforms=transforms(), mode=mode,
        clock=clock or time.monotonic, **kw,
    )


# -- the acceptance test of Phase 1 ----------------------------------------------------

def test_a_replay_logs_would_move_to_per_detected_event():
    eng = engine(mode="validate")
    summary = eng.run()

    assert summary.ok and summary.ticks == 3 and summary.ran == 3
    would = eng.log.events("would", step="mv")
    assert [e.detail["message"] for e in would] == [
        f"would move-to XY to (y=10.000, x=10.000) in {STAGE}",
        f"would move-to XY to (y=20.000, x=5.000) in {STAGE}",
    ]
    # the dark frame: the gate closed, nothing downstream ran, the sink saw the closed event
    closed = eng.log.events("closed", step="g")
    assert len(closed) == 1 and closed[0].tick == 2
    logged = eng.log.events("log", step="log")
    assert [e.detail["value"]["fired"] for e in logged] == [True, False, True]
    # the chain ran move -> snap twice, nothing was dispatched
    assert summary.actions_started == 4 and summary.actions_completed == 4 and summary.gates_fired == 2
    assert eng.actions.requests == []
    assert eng.log.events("finished")[0].detail["mode"] == "validate"


def test_visualize_skips_actions_and_experiment_dispatches_them():
    eng = engine(mode="visualize")
    eng.run()
    assert len(eng.log.events("skipped")) == 4 and eng.actions.requests == []

    eng = engine(mode="experiment")
    summary = eng.run()
    assert summary.ok
    assert [r.action for r in eng.actions.requests] == ["move-to", "snap", "move-to", "snap"]
    assert eng.actions.position == {"XY": {"Y": 20.0, "X": 5.0}} and eng.actions.snaps == ["Camera", "Camera"]
    # the snap started after the move completed, with the move's done event
    assert eng.actions.requests[1].inputs[0].fired and eng.actions.requests[1].inputs[0].payload["moved"] is True
    events = [(e.step, e.event) for e in eng.log.events(step="mv") + eng.log.events(step="sn") if e.tick == 1]
    assert ("mv", "done") in events and ("sn", "started") in events


def test_run_modes_parse():
    assert RunMode.of("Validate") is RunMode.VALIDATE and RunMode.of(RunMode.EXPERIMENT) is RunMode.EXPERIMENT
    with pytest.raises(ValueError, match="unknown run mode"):
        RunMode.of("dry")


# -- arming --------------------------------------------------------------------------

def test_hardware_actions_need_roles_in_experiment_mode():
    bare = pipeline(roles=False)
    with pytest.raises(ArmError, match="'scouting' and 'idle' roles"):
        engine(bare, mode="experiment").arm()
    for mode in ("visualize", "validate"):   # nothing is applied: no roles needed
        eng = engine(bare, mode=mode)
        eng.arm()
        assert eng.armed and eng.arm_problems() == []


def test_points_without_a_transform_are_refused_at_arm():
    document = SmartPipelineDocument(Workflow("p", [
        Live("cam", detector="Camera"),
        Process("c", "centroid", {}, inputs=["cam"]),
        Action("mv", "move-to", {"positioner": "XY", "frame": STAGE}, inputs=["c"]),
    ]))
    with pytest.raises(ArmError, match="without a transform step"):
        engine(document, mode="visualize").arm()


def test_a_transform_into_the_wrong_frame_is_refused_at_arm():
    document = pipeline()
    document.workflow.step("mv").params["frame"] = "stage:Other"
    with pytest.raises(ArmError, match="writes 'stage:XY', but 'move-to' takes 'stage:Other'"):
        engine(document, mode="visualize").arm()


def test_a_tick_before_arming_is_refused():
    eng = engine()
    with pytest.raises(EngineError):
        eng.tick(Frame(FRAMES[0], "Camera", 0, 0.0))


def test_arming_again_resets_state_gates_and_the_epoch():
    document = pipeline(gate=Gate("g", "once", inputs=["c"]))
    eng = engine(document)
    eng.run()
    assert eng.summary.gates_fired == 1 and eng.epoch == 1
    eng.source = ArrayFrameSource(FRAMES)
    eng.arm()
    assert eng.epoch == 2 and eng.summary.ticks == 0
    eng.run()
    assert eng.summary.gates_fired == 1   # the once gate opened again after re-arming


# -- busy, stale, failure, timeout ----------------------------------------------------

def test_while_an_action_runs_the_next_tick_drops_its_action_and_a_busy_gate_closes():
    document = SmartPipelineDocument(Workflow("p", [
        Live("cam", detector="Camera"),
        Process("c", "centroid", {}, inputs=["cam"]),
        Gate("g", "condition", inputs=["c"]),
        Transform("tf", "cam2stage", inputs=["g"]),
        Gate("b", "busy", inputs=["c"]),          # before the chain: idle on the first tick
        Action("mv", "move-to", {"positioner": "XY", "frame": STAGE}, inputs=["tf"]),
        Action("sn", "snap", {"detector": "Camera"}, inputs=["mv"]),
        Sink("busylog", "event-log", inputs=["b.fired"]),
    ]))
    document.settings = PipelineSettings(roles={"scouting": "widefield", "idle": "off"})
    actions = MockActions(auto_complete=False)
    eng = engine(document, mode="experiment", actions=actions, wait_actions=False)
    eng.arm()
    first = eng.tick(Frame(FRAMES[0], "Camera", 0, 0.0))
    assert first.started == "mv" and eng.busy
    second = eng.tick(Frame(FRAMES[2], "Camera", 1, 1.0))
    assert second.outcome == RAN and second.dropped_action == "mv" and second.started is None
    assert eng.summary.dropped_busy == 1
    busy_events = [e.detail["why"] for e in eng.log.events(step="b")]
    assert busy_events == ["idle", "busy with mv"]
    actions.settle_all()           # the move completes: the chain goes on to snap, which also waits
    third = eng.tick(Frame(FRAMES[2], "Camera", 2, 2.0))
    assert eng.busy and eng.summary.actions_completed == 1 and third.dropped_action == "mv"
    actions.settle_all()
    eng.tick(Frame(FRAMES[2], "Camera", 3, 3.0))
    assert eng.summary.actions_completed == 2 and eng.log.events("chain-done")


def test_frames_taken_before_the_chain_ended_are_stale():
    eng = engine(mode="validate", wait_actions=False)
    eng.arm()
    assert eng.tick(Frame(FRAMES[0], "Camera", 5, 0.0)).outcome == RAN   # chain ran to completion at once
    again = eng.tick(Frame(FRAMES[0], "Camera", 5, 0.1))
    assert again.outcome == DROPPED_STALE and eng.summary.dropped_stale == 1
    assert eng.tick(Frame(FRAMES[0], "Camera", 6, 0.2)).outcome == RAN


def test_a_frame_on_another_grid_stops_the_pipeline():
    eng = engine(wait_actions=False)
    eng.arm()
    eng.tick(Frame(FRAMES[0], "Camera", 0, 0.0))
    cropped = eng.tick(Frame(FRAMES[0][:32, :32], "Camera", 1, 1.0))
    assert cropped.outcome == TICK_FAILED and "grid changed" in eng.summary.error
    assert "shape (64, 64) -> (32, 32)" in eng.summary.error


def test_a_failing_action_stops_the_run_with_the_reason():
    actions = MockActions(fail_on={"move-to"})
    eng = engine(mode="experiment", actions=actions)
    summary = eng.run()
    assert not summary.ok and "action 'move-to' failed" in summary.error
    assert summary.ticks == 1 and summary.actions_failed == 1
    assert eng.log.events("failed", step="mv")
    assert eng.log.events("stopped") or eng.log.events("finished")


def test_a_backend_refusal_is_a_failure_too():
    eng = engine(mode="experiment", actions=MockActions(refuse_on={"snap"}))
    summary = eng.run()
    assert not summary.ok and "sn: RuntimeError: snap: refused" in summary.error


def test_an_action_that_never_completes_times_out_and_is_aborted():
    document = pipeline(chain=("move",))
    document.workflow.step("mv").timeout_s = 0.05
    actions = MockActions(auto_complete=False)
    eng = engine(document, mode="experiment", actions=actions)
    summary = eng.run()
    assert not summary.ok and "timeout" in summary.error
    assert actions.pending[0].abort_requested and actions.pending[0].outcome == "cancelled"
    assert eng.log.events("timeout", step="mv")


def test_a_completion_from_an_earlier_arm_is_ignored():
    actions = MockActions(auto_complete=False)
    eng = engine(pipeline(chain=("move",)), mode="experiment", actions=actions, wait_actions=False)
    eng.arm()
    eng.tick(Frame(FRAMES[0], "Camera", 0, 0.0))
    stale = actions.pending[0]
    eng.arm()                          # a new epoch; the old completion is still pending
    eng.source = ArrayFrameSource(FRAMES)
    assert not eng.busy
    stale.complete({"moved": True})
    eng.tick(Frame(FRAMES[0], "Camera", 0, 0.0))
    assert eng.summary.actions_completed == 0 and len(actions.pending) == 2


def test_stop_from_another_thread_ends_a_blocking_source_and_aborts_the_action():
    inbox = CoalescingInbox()
    actions = MockActions(auto_complete=False)
    eng = Engine(pipeline(chain=("move",)), source=inbox, steps=FunctionStepRunner(FUNCTIONS), actions=actions,
                 transforms=transforms(), mode="experiment", wait_actions=False, cleanup_budget_s=0.5)
    eng.arm()
    done = threading.Event()
    summary = {}

    def worker():
        summary["run"] = eng.run()
        done.set()

    threading.Thread(target=worker, daemon=True).start()
    inbox.push(Frame(FRAMES[0], "Camera", 0, 0.0))
    deadline = time.monotonic() + 2
    while not actions.pending and time.monotonic() < deadline:
        time.sleep(0.01)
    assert actions.pending, "the action should have started"
    eng.stop()
    assert done.wait(2), "run() did not return after stop()"
    assert summary["run"].stopped and actions.pending[0].outcome == "cancelled"
    assert eng.log.events("aborted", step="mv") and not eng.armed


def test_a_completion_that_does_not_end_is_reported():
    class Lingering(SimpleCompletion):
        def abort(self):
            self.cancel(ended=False)   # cancelled, but the hardware is still moving
            return True

    class Backend(MockActions):
        def start(self, request):
            self.requests.append(request)
            completion = Lingering(request.epoch)
            self.pending.append(completion)
            return completion

    eng = engine(pipeline(chain=("move",)), mode="experiment", actions=Backend(), wait_actions=False,
                 cleanup_budget_s=0.05)
    eng.arm()
    eng.tick(Frame(FRAMES[0], "Camera", 0, 0.0))
    eng.stop()
    summary = eng.run()
    assert summary.unfinished_action == "mv" and eng.log.events("still-running", step="mv")


# -- gates ---------------------------------------------------------------------------

def test_min_interval_once_and_count_limit_gates_use_the_engine_clock():
    clock = sleep_clock()
    frames = [FRAMES[0]] * 5

    document = pipeline(gate=Gate("g", "min-interval", {"seconds": 2.0}, inputs=["c"]), chain=("move",))
    eng = engine(document, frames=frames, clock=clock)
    eng.arm()
    opened = []
    for index, image in enumerate(frames):
        eng.tick(Frame(image, "Camera", index, float(index)))
        opened.append(eng.log.events(step="g")[-1].event == "open")
        clock.advance(1.0)
    assert opened == [True, False, True, False, True]

    document = pipeline(gate=Gate("g", "once", inputs=["c"]), chain=("move",))
    eng = engine(document, frames=frames)
    assert eng.run().gates_fired == 1

    document = pipeline(gate=Gate("g", "count-limit", {"count": 3}, inputs=["c"]), chain=("move",))
    eng = engine(document, frames=frames)
    assert eng.run().gates_fired == 3 and len(eng.log.events("would", step="mv")) == 3


def test_condition_gates_on_values_and_events():
    document = SmartPipelineDocument(Workflow("p", [
        Live("cam", detector="Camera"),
        Process("b", "brightness", {}, inputs=["cam"]),
        Gate("bright", "condition", {"min": 50.0}, inputs=["b"]),
        Gate("again", "condition", inputs=["bright.fired"]),
        Sink("log", "event-log", inputs=["again.fired"]),
    ]))
    eng = engine(document, mode="visualize")
    eng.run()
    assert [e.event for e in eng.log.events(step="bright")] == ["open", "closed", "open"]
    assert [e.event for e in eng.log.events(step="again")] == ["open", "closed", "open"]


def test_a_processing_failure_suppresses_what_follows_but_the_tick_goes_on():
    def broken(inputs, params, state):
        raise ValueError("no cell today")

    runner = FunctionStepRunner({**FUNCTIONS, "centroid": broken})
    eng = Engine(pipeline(), source=ArrayFrameSource(FRAMES[:1]), steps=runner, actions=MockActions(),
                 transforms=transforms(), mode="validate")
    summary = eng.run()
    assert summary.ok and summary.ran == 1
    failed = eng.log.events("step-failed", step="c")
    assert len(failed) == 1 and "no cell today" in failed[0].detail["error"]
    assert not eng.log.events("would")


def test_step_state_survives_between_ticks_and_is_reset_by_arm():
    eng = engine(mode="visualize")
    eng.run()
    assert eng._state["c"]["calls"] == 3
    eng.source = ArrayFrameSource(FRAMES)
    eng.arm()
    assert eng._state == {}


def test_overlay_sinks_hand_the_value_to_the_callback():
    seen = []
    document = pipeline(extra=(Sink("ov", "overlay", inputs=["c"]),), chain=())
    eng = Engine(document, source=ArrayFrameSource(FRAMES), steps=FunctionStepRunner(FUNCTIONS),
                 actions=MockActions(), transforms=transforms(), mode="visualize",
                 on_overlay=lambda step, value: seen.append((step, value.count)))
    eng.run()
    assert seen == [("ov", 1), ("ov", 0), ("ov", 1)]


def test_a_tick_result_reads_its_values_by_reference():
    eng = engine(wait_actions=False)
    eng.arm()
    result = eng.tick(Frame(FRAMES[0], "Camera", 0, 0.0))
    assert result.value("c").count == 1 and result.value("g.fired").fired
    assert result.value("tf").frame == STAGE and result.value("mv.done").fired


# -- the inbox -----------------------------------------------------------------------

def test_the_inbox_keeps_the_latest_frame_and_counts_what_it_drops():
    clock = sleep_clock()
    inbox = CoalescingInbox(max_age_s=1.0, clock=clock)
    assert inbox.push(Frame(FRAMES[0], "Camera", 0, 0.0))
    assert inbox.push(Frame(FRAMES[1], "Camera", 1, 0.1))        # replaces the first
    assert not inbox.push(Frame(FRAMES[1], "Camera", 1, 0.1))    # a duplicate sequence
    assert not inbox.push(Frame(FRAMES[1], "Camera", 0, 0.2))    # late
    taken = inbox.take(timeout=0)
    assert taken.sequence == 1 and inbox.stats.replaced == 1 and inbox.stats.duplicates == 2
    inbox.push(Frame(FRAMES[2], "Camera", 2, 0.3))
    clock.advance(5.0)
    assert inbox.take(timeout=0) is None and inbox.stats.aged_out == 1
    inbox.stop()
    assert inbox.take() is None and list(inbox.frames()) == []


def test_the_inbox_hands_frames_to_a_waiting_consumer():
    inbox = CoalescingInbox()
    got = []

    def consume():
        for frame in inbox.frames():
            got.append(frame.sequence)

    thread = threading.Thread(target=consume, daemon=True)
    thread.start()
    for index in range(3):
        inbox.push(Frame(FRAMES[0], "Camera", index, float(index)))
        time.sleep(0.02)
    inbox.stop()
    thread.join(2)
    assert not thread.is_alive() and got and got[-1] == 2


# -- the event log -------------------------------------------------------------------

def test_the_event_log_writes_csv_and_json_with_a_footer(tmp_path):
    eng = engine()
    eng.run()
    csv_path = eng.log.write_csv(tmp_path / "events.csv")
    with open(csv_path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["event"] == "armed" and rows[-1]["event"] == "footer"
    footer = json.loads(rows[-1]["detail"])
    assert footer["ticks"] == 3 and footer["actions_completed"] == 4
    payload = json.loads(eng.log.write_json(tmp_path / "events.json").read_text(encoding="utf-8"))
    assert payload["footer"]["mode"] == "validate" and len(payload["entries"]) == len(eng.log)


def test_the_event_log_can_keep_a_window_and_feed_a_sink():
    seen = []
    log = EventLog(keep=2, sink=seen.append)
    for index in range(4):
        log.append(index, 0.0, "s", "e", n=index)
    assert [e.detail["n"] for e in log] == [2, 3] and len(seen) == 4
    assert str(seen[0]).startswith("[    0     0.000] s: e (n=0)")
