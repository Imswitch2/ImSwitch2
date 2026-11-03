"""The engine: runs a smart pipeline tick by tick against its ports.

**The loop.** A frame from the source is a tick. The engine runs the steps
in file order: the source port carries the frame (as the step runner's
image result), processing steps run through the step runner, a transform
through the transform registry, a gate decides, a sink logs. A step whose
input is missing -- because a gate upstream did not open, or because the
step that made it failed -- is *suppressed* for the tick. Then the **action
chain**: the first action whose inputs are all there starts; the tick ends;
the pipeline is *busy* until that action's completion settles, and then the
next action of the chain starts with the values the tick had when the chain
started, and so on until the chain is done. While busy, a tick runs its
processing and sinks as usual (overlays keep drawing), a ``busy`` gate
closes, and an action that would start is counted as dropped. A failed,
cancelled or timed-out action stops the pipeline: failure enters cleanup.

**Freshness.** After an action chain completes, frames whose sequence is
not above the last one seen while the chain ran are stale (taken before
the hardware settled) and are dropped, counted. A frame whose pixel grid
(:class:`~imswitch.imcommon.model.smartpipeline.FrameIdentity`) differs from
the one the pipeline was armed on stops the pipeline: the calibration no
longer applies.

**Run modes** (the effects table is :data:`MODE_EFFECTS`): in *Visualize*
no action is dispatched (logged as skipped, the chain continues at once);
in *Validate* every action is checked -- its catalogue entry, parameters,
the coordinate contract -- and logged as "would …", nothing is dispatched;
in *Experiment* it is dispatched to the backend. Roles, reservations and
the live source are the run's (:mod:`.run`); what this engine does when
armed in Experiment is refuse a pipeline with hardware actions and no
``scouting``/``idle`` roles, as the plan says (Validate applies no role, so
it needs none).

**Clock and state.** Gates keep their clocks on the engine's ``clock``
(injectable, so a replay is deterministic); every step's state, the gate
counters and the chain are reset by :meth:`Engine.arm`. :meth:`Engine.stop`
is thread-safe and idempotent: it stops the source, aborts a pending
completion, and :meth:`Engine.run` then drains -- waits, bounded, for the
completion to end -- and writes the log footer. A completion from an
earlier arm (its ``epoch``) is ignored.
"""

from __future__ import annotations

import csv
import json
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import numpy as np

from imswitch.imcommon.model.smartpipeline import (
    Action,
    CoordinateError,
    Event,
    Frame,
    Gate,
    Live,
    SmartPipelineDocument,
    Sink,
    Transform,
    TransformRegistry,
    action_chain,
    apply_transform,
    check_points,
    fired_of,
    frame_of,
    kind_of,
    pipeline_settings,
    points_of,
    source_step_of,
    validate_pipeline,
    value_of,
)
from imswitch.imcommon.model.smartpipeline.frames import finite_value
from imswitch.imcommon.model.smartpipeline.steps import DEFAULT_COUNT, DEFAULT_INTERVAL_S, DONE_PORT, FIRED_PORT
from imswitch.imcommon.model.workflow.steps import DEFAULT_PORT, SOURCE_PORT, Ref, Save, Source, Workflow
from imswitch.imcontrol.model.smartpipeline.ports import (
    CANCELLED,
    FAILED,
    OK,
    PENDING,
    TIMEOUT,
    ActionRequest,
    SimpleCompletion,
)


class RunMode(Enum):
    VISUALIZE = "visualize"
    VALIDATE = "validate"
    EXPERIMENT = "experiment"

    @classmethod
    def of(cls, value) -> "RunMode":
        if isinstance(value, RunMode):
            return value
        text = str(value or "").strip().lower()
        for mode in cls:
            if mode.value == text or mode.name.lower() == text:
                return mode
        raise ValueError(f"unknown run mode {value!r}; expected one of {[m.value for m in cls]}")


#: What each run mode is allowed to do. ``actions``: ``skip`` (never
#: dispatched), ``log`` (checked and logged as "would"), ``dispatch``.
#: ``roles``/``reserve``: whether arming applies setup-mode roles and
#: reserves devices (Phase 2); listed here so the rule is in one place.
MODE_EFFECTS = {
    RunMode.VISUALIZE: {"actions": "skip", "roles": False, "reserve": False},
    RunMode.VALIDATE: {"actions": "log", "roles": False, "reserve": True},
    RunMode.EXPERIMENT: {"actions": "dispatch", "roles": True, "reserve": True},
}


class ArmError(RuntimeError):
    """The pipeline cannot be armed as asked; the message says why."""


class EngineError(RuntimeError):
    """The engine was used out of order (a tick before arming)."""


# --------------------------------------------------------------------------
# the event log
# --------------------------------------------------------------------------

@dataclass
class LogEntry:
    tick: int
    t: float
    step: str
    event: str
    detail: dict = field(default_factory=dict)

    def __str__(self) -> str:
        detail = " ".join(f"{k}={_short(v)}" for k, v in self.detail.items())
        return f"[{self.tick:5d} {self.t:9.3f}] {self.step}: {self.event}" + (f" ({detail})" if detail else "")


class EventLog:
    """What happened, tick by tick, as data; written as CSV or JSON."""

    COLUMNS = ("tick", "t", "step", "event", "detail")

    def __init__(self, *, keep: int | None = None, sink: Callable[[LogEntry], None] | None = None):
        self.entries: list[LogEntry] = []
        self.footer: dict = {}
        self._keep = keep
        self._sink = sink
        self._lock = threading.Lock()

    def append(self, tick: int, t: float, step: str, event: str, **detail) -> LogEntry:
        entry = LogEntry(tick, float(t), str(step), str(event), {k: _plain(v) for k, v in detail.items()})
        with self._lock:
            self.entries.append(entry)
            if self._keep is not None and len(self.entries) > self._keep:
                del self.entries[: len(self.entries) - self._keep]
        if self._sink is not None:
            try:
                self._sink(entry)
            except Exception:
                pass
        return entry

    def events(self, event: str | None = None, step: str | None = None) -> list[LogEntry]:
        return [e for e in self.entries if (event is None or e.event == event) and (step is None or e.step == step)]

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self):
        return iter(list(self.entries))

    def write_csv(self, path) -> Path:
        path = Path(path)
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(self.COLUMNS)
            for entry in self.entries:
                writer.writerow([entry.tick, f"{entry.t:.6f}", entry.step, entry.event,
                                 json.dumps(entry.detail, default=str, ensure_ascii=False)])
            if self.footer:
                writer.writerow(["", "", "", "footer", json.dumps(self.footer, default=str, ensure_ascii=False)])
        return path

    def write_json(self, path) -> Path:
        path = Path(path)
        payload = {"entries": [{"tick": e.tick, "t": e.t, "step": e.step, "event": e.event, "detail": e.detail}
                               for e in self.entries], "footer": dict(self.footer)}
        path.write_text(json.dumps(payload, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
        return path


def _plain(value):
    """``value`` as something JSON can write and a reader can understand."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    return describe(value)


def _short(value) -> str:
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "..."


def describe(value) -> Any:
    """A readable summary of what a port carries, for the log."""
    kind = kind_of(value)
    if kind == "points":
        coordinates = points_of(value)
        summary: dict[str, Any] = {"kind": "points", "count": int(coordinates.shape[0]), "frame": frame_of(value)}
        if coordinates.shape[0]:
            summary["first"] = [round(float(v), 3) for v in coordinates[0]]
        return summary
    if kind == "value":
        return {"kind": "value", "value": value_of(value), "unit": str(getattr(value, "unit", "") or "")}
    if kind == "event":
        payload = getattr(value, "payload", None)
        summary = {"kind": "event", "fired": bool(fired_of(value))}
        if isinstance(payload, dict) and payload:
            summary["payload"] = _plain(payload)
        return summary
    name = getattr(value, "name", None)
    shape = getattr(getattr(value, "data", value), "shape", None)
    summary = {"kind": kind}
    if name:
        summary["name"] = str(name)
    if shape is not None:
        summary["shape"] = [int(s) for s in shape]
    return summary


# --------------------------------------------------------------------------
# results of a tick and of a run
# --------------------------------------------------------------------------

RAN = "ran"
DROPPED_STALE = "dropped-stale"
DROPPED_BUSY = "dropped-busy"
TICK_FAILED = "failed"


@dataclass
class TickResult:
    tick: int
    sequence: int
    outcome: str
    values: dict = field(default_factory=dict)          # "step.port" -> value
    default_ports: dict = field(default_factory=dict, repr=False)   # step id -> its first port
    suppressed: list[str] = field(default_factory=list)
    started: str | None = None                          # the action step started this tick
    dropped_action: str | None = None                   # the action not started because busy
    error: str | None = None

    def value(self, ref) -> Any:
        """The value on ``ref`` (``"step"`` or ``"step.port"``)."""
        return self.values[_key(_ref(ref), self.values, self.default_ports)]


@dataclass
class RunSummary:
    ticks: int = 0
    ran: int = 0
    dropped_stale: int = 0
    dropped_busy: int = 0
    actions_started: int = 0
    actions_completed: int = 0
    actions_failed: int = 0
    gates_fired: int = 0
    stopped: bool = False
    error: str | None = None
    unfinished_action: str | None = None
    mode: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def _ref(text) -> Ref:
    from imswitch.imcommon.model.workflow.steps import parse_ref

    return parse_ref(text)


def _key(ref: Ref, values: dict, default_ports: dict | None = None) -> str:
    """The ``"step.port"`` key ``ref`` means: its port, else the step's first
    declared port (``default_ports``; a gate's ``out``, never its ``fired``),
    else ``out``, else the first port the step produced (a Python step's
    named output)."""
    if ref.port is not None:
        return f"{ref.step}.{ref.port}"
    declared = (default_ports or {}).get(ref.step)
    if declared is not None:
        return f"{ref.step}.{declared}"
    for port in (DEFAULT_PORT, SOURCE_PORT):
        if f"{ref.step}.{port}" in values:
            return f"{ref.step}.{port}"
    for key in values:
        if key.startswith(ref.step + "."):
            return key
    return f"{ref.step}.{DEFAULT_PORT}"


# --------------------------------------------------------------------------
# the engine
# --------------------------------------------------------------------------

@dataclass
class _Pending:
    step: Action
    completion: Any
    started_at: float
    tick: int
    values: dict            # the chain's values, captured when it started
    chain_index: int        # index of ``step`` in the chain


@dataclass
class _GateState:
    fired: int = 0
    last_t: float | None = None
    #: A delay gate: when its trigger arrived, while it waits.
    triggered_t: float | None = None


class Engine:
    """Runs one pipeline. Make it, :meth:`arm` it, then :meth:`run` it (or
    :meth:`tick` it frame by frame), and :meth:`stop` it from any thread."""

    def __init__(self, document, *, source, steps, actions, transforms: TransformRegistry | None = None,
                 mode=RunMode.VALIDATE, clock: Callable[[], float] = time.monotonic, log: EventLog | None = None,
                 logger=None, wait_actions: bool = True, cleanup_budget_s: float = 5.0,
                 on_overlay: Callable[[str, Any], None] | None = None, registry=None):
        self.workflow: Workflow = document.workflow if isinstance(document, SmartPipelineDocument) else document
        self.settings = pipeline_settings(self.workflow)
        self.source = source
        self.steps = steps
        self.actions = actions
        self.transforms = transforms if transforms is not None else TransformRegistry()
        self.mode = RunMode.of(mode)
        self.clock = clock
        self.log = log if log is not None else EventLog()
        self.logger = logger
        self.wait_actions = bool(wait_actions)
        self.cleanup_budget_s = float(cleanup_budget_s)
        self.on_overlay = on_overlay
        self.registry = registry
        self.summary = RunSummary(mode=self.mode.value)
        self.armed = False
        self._epoch = 0
        self._tick = 0
        self._stop = threading.Event()
        self._state: dict[str, dict] = {}
        self._gates: dict[str, _GateState] = {}
        self._pending: _Pending | None = None
        self._chain: list[Action] = action_chain(self.workflow)
        self._armed_identity = None
        self._last_sequence: int | None = None
        self._fresh_after: int | None = None
        self._source_step = source_step_of(self.workflow)
        self._default_ports = {step.id: ports[0] for step in self.workflow.steps
                               if (ports := getattr(step, "ports", None))}
        self._lock = threading.RLock()

    def _key(self, ref: Ref, values: dict) -> str:
        return _key(ref, values, self._default_ports)

    # -- arming ---------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._pending is not None

    @property
    def epoch(self) -> int:
        return self._epoch

    def arm_problems(self) -> list[str]:
        """Why the pipeline cannot be armed in this mode, as reasons; empty
        when it can."""
        problems = [str(issue) for issue in validate_pipeline(
            self.workflow, registry=self.registry, actions=getattr(self.actions, "catalog", None),
            transforms=self.transforms,
        )]
        if self._source_step is None:
            return problems or ["no source step"]
        effects = MODE_EFFECTS[self.mode]
        hardware = [step for step in self._chain if self._hardware(step)]
        if effects["roles"] and hardware and not self.settings.arms_live:
            problems.append(
                f"{self.mode.value} mode with hardware actions ({', '.join(s.id for s in hardware)}) needs the "
                "pipeline to name its 'scouting' and 'idle' roles; without them it arms in visualize or "
                "validate mode only"
            )
        return problems

    def _hardware(self, step: Action) -> bool:
        catalog = getattr(self.actions, "catalog", None)
        spec = catalog.get(step.action) if catalog is not None else None
        return True if spec is None else bool(spec.hardware)

    def arm(self) -> None:
        """Preflight, then reset: every step's state, the gates, the chain.
        Raises :class:`ArmError` with every reason when the pipeline cannot
        be armed as it is."""
        problems = self.arm_problems()
        if problems:
            raise ArmError("the pipeline cannot be armed:\n  " + "\n  ".join(problems))
        with self._lock:
            self._epoch += 1
            self._tick = 0
            self._stop.clear()
            self._state = {}
            self._gates = {step.id: _GateState() for step in self.workflow.steps if isinstance(step, Gate)}
            self._pending = None
            self._armed_identity = None
            self._last_sequence = None
            self._fresh_after = None
            self.summary = RunSummary(mode=self.mode.value)
            self.armed = True
        self.log.append(0, self.clock(), "pipeline", "armed", mode=self.mode.value, epoch=self._epoch,
                        steps=[s.id for s in self.workflow.steps])

    # -- running --------------------------------------------------------------

    def run(self, max_ticks: int | None = None) -> RunSummary:
        """Tick until the source ends, :meth:`stop` is called, ``max_ticks``
        is reached or an action fails; then drain and return the summary."""
        if not self.armed:
            self.arm()
        try:
            self._kickoff()
            if self.wait_actions and self._pending is not None:
                self._wait_chain()
            for frame in self.source.frames():
                if self._stop.is_set():
                    break
                result = self.tick(frame)
                if result.outcome == TICK_FAILED and self.summary.error:
                    break
                if self.wait_actions and self._pending is not None:
                    self._wait_chain()
                    if self.summary.error:
                        break
                if max_ticks is not None and self._tick >= max_ticks:
                    break
        except Exception as exc:  # the source itself failed
            self._fail("source", f"{type(exc).__name__}: {exc}")
        finally:
            self._drain()
        return self.summary

    def _kickoff(self) -> None:
        """Start the first action marked ``on_arm``, with no inputs, before
        any frame: how a scan-driven pipeline takes its first picture."""
        for index, step in enumerate(self._chain):
            if not getattr(step, "on_arm", False):
                continue
            try:
                self._start_action(step, [], {}, 0, index)
            except Exception as exc:
                self._fail(step.id, str(exc) if isinstance(exc, CoordinateError) else f"{type(exc).__name__}: {exc}")
            return

    def _wait_chain(self) -> None:
        """Offline: let the chain complete before the next frame, so a
        replay is deterministic; a completion that never settles is a
        timeout after the step's ``timeout_s`` (or the cleanup budget)."""
        while self._pending is not None and not self._stop.is_set():
            pending = self._pending
            timeout = pending.step.timeout_s or self.cleanup_budget_s
            pending.completion.wait(timeout)
            self._poll_completion(force_timeout=not pending.completion.done)

    def stop(self) -> None:
        """Ask the run to end; safe from any thread, idempotent."""
        self._stop.set()
        self.summary.stopped = True
        try:
            self.source.stop()
        except Exception:
            pass
        pending = self._pending   # read without the lock: a tick may be running
        if pending is not None:
            try:
                pending.completion.abort()
            except Exception:
                pass

    def _drain(self) -> None:
        """After the loop: wait, bounded, for a pending action to end, write
        the footer, close the source, disarm."""
        with self._lock:
            pending, self._pending = self._pending, None
        if pending is not None:
            completion = pending.completion
            try:
                completion.abort()
            except Exception:
                pass
            deadline = self.clock() + self.cleanup_budget_s
            wait_ended = getattr(completion, "wait_ended", None)
            if callable(wait_ended):
                wait_ended(self.cleanup_budget_s)
            else:
                while not getattr(completion, "ended", False) and self.clock() < deadline:
                    time.sleep(0.01)
            if not getattr(completion, "ended", False):
                self.summary.unfinished_action = pending.step.id
                self.log.append(self._tick, self.clock(), pending.step.id, "still-running",
                                outcome=getattr(completion, "outcome", PENDING))
            else:
                self.log.append(self._tick, self.clock(), pending.step.id, "aborted",
                                outcome=getattr(completion, "outcome", PENDING))
        self.log.footer = {**self.summary.to_dict(), "epoch": self._epoch}
        self.log.append(self._tick, self.clock(), "pipeline", "stopped" if self.summary.stopped else "finished",
                        **self.summary.to_dict())
        try:
            self.source.close()
        except Exception:
            pass
        self.armed = False

    def _fail(self, step_id: str, message: str) -> None:
        if self.summary.error is None:
            self.summary.error = f"{step_id}: {message}"
        self.log.append(self._tick, self.clock(), step_id, "failed", error=message)
        self._stop.set()
        try:
            self.source.stop()
        except Exception:
            pass

    # -- one tick -------------------------------------------------------------

    def tick(self, frame: Frame) -> TickResult:
        """Run one frame through the pipeline."""
        if not self.armed:
            raise EngineError("the pipeline is not armed")
        with self._lock:
            self._tick += 1
            tick = self._tick
            self.summary.ticks += 1
            now = self.clock()
            self._poll_completion()
            if self.summary.error:
                return TickResult(tick, frame.sequence, TICK_FAILED, error=self.summary.error)
            # the grid this detector's points are measured on
            scan_driven = bool((frame.metadata or {}).get("scan_driven"))
            if scan_driven:
                pass   # a scan-driven detector's grid is the scan's; the transform reads it per frame
            elif self._armed_identity is None:
                self._armed_identity = frame.identity
            elif frame.identity is not None and self._armed_identity.differs_from(frame.identity):
                reasons = "; ".join(self._armed_identity.differs_from(frame.identity))
                self._fail(self._source_step.id, f"the frame grid changed since arming: {reasons}")
                return TickResult(tick, frame.sequence, TICK_FAILED, error=self.summary.error)
            if self._fresh_after is not None and frame.sequence <= self._fresh_after:
                self.summary.dropped_stale += 1
                self.log.append(tick, now, self._source_step.id, "dropped-stale", sequence=frame.sequence,
                                fresh_after=self._fresh_after)
                return TickResult(tick, frame.sequence, DROPPED_STALE)
            self._last_sequence = frame.sequence
            result = TickResult(tick, frame.sequence, RAN, default_ports=self._default_ports)
            values = result.values
            try:
                values[f"{self._source_step.id}.{SOURCE_PORT}"] = self.steps.image_result(frame)
            except Exception as exc:
                self._fail(self._source_step.id, f"{type(exc).__name__}: {exc}")
                result.outcome, result.error = TICK_FAILED, self.summary.error
                return result
            chain_started = False
            for step in self.workflow.steps:
                if step is self._source_step or isinstance(step, (Source, Live, Save)):
                    continue
                refs = list(getattr(step, "inputs", []) or [])
                missing = [str(ref) for ref in refs if self._key(ref, values) not in values]
                if missing:
                    result.suppressed.append(step.id)
                    continue
                inputs = [values[self._key(ref, values)] for ref in refs]
                try:
                    if isinstance(step, Action):
                        if chain_started:
                            # Later actions run from the chain, on completion.
                            continue
                        # The first action, in file order, whose inputs the
                        # tick has starts the chain (a suppressed one is passed).
                        chain_started = True
                        if self._pending is not None:
                            self.summary.dropped_busy += 1
                            result.dropped_action = step.id
                            self.log.append(tick, now, step.id, "dropped-busy", pending=self._pending.step.id)
                            continue
                        # The chain keeps the tick's values: a later action
                        # reads them, and the tick's result shows ``done``.
                        self._start_action(step, inputs, values, tick, self._chain.index(step))
                        result.started = step.id
                        continue
                    produced = self._run_step(step, inputs, frame, tick, now)
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}" if not isinstance(exc, (CoordinateError,)) else str(exc)
                    if isinstance(step, Action):
                        self._fail(step.id, message)
                        result.outcome, result.error = TICK_FAILED, self.summary.error
                        return result
                    # A processing step that fails suppresses what follows,
                    # and the tick goes on (the overlay still draws).
                    self.log.append(tick, now, step.id, "step-failed", error=message)
                    result.suppressed.append(step.id)
                    continue
                for port, value in produced.items():
                    values[f"{step.id}.{port}"] = value
            self.summary.ran += 1
            return result

    def _run_step(self, step, inputs: list, frame: Frame, tick: int, now: float) -> dict[str, Any]:
        if isinstance(step, Transform):
            transform = self.transforms.get(step.transform)
            if transform is None:
                raise CoordinateError(f"unknown transform {step.transform!r}")
            moved = apply_transform(inputs[0], transform, {"frame": frame, "params": step.params}, name=step.id)
            return {DEFAULT_PORT: moved}
        if isinstance(step, Gate):
            return self._gate(step, inputs[0], tick, now)
        if isinstance(step, Sink):
            self._sink(step, inputs[0], tick, now)
            return {}
        state = self._state.setdefault(step.id, {})
        produced = self.steps.run_step(step, inputs, state=state, frame=frame)
        return dict(produced or {})

    # -- gates ----------------------------------------------------------------

    def _gate(self, step: Gate, value, tick: int, now: float) -> dict[str, Any]:
        state = self._gates.setdefault(step.id, _GateState())
        params = step.params
        reason = ""
        if step.rule == "condition":
            kind = kind_of(value)
            if kind == "points":
                count = int(points_of(value).shape[0])
                need = int(params.get("min_count", 1) or 0)
                open_ = count >= need
                reason = f"{count} point(s), need {need}"
            elif kind == "value":
                number = value_of(value)
                low, high = params.get("min"), params.get("max")
                open_ = (low is None or number >= low) and (high is None or number <= high)
                reason = f"{number:g} in [{low}, {high}]"
            elif kind == "event":
                open_ = bool(fired_of(value))
                reason = "fired" if open_ else "not fired"
            else:
                open_ = bool(value is not None)
                reason = kind
            if params.get("invert"):
                open_ = not open_
                reason = f"inverted: {reason}"
        elif step.rule == "min-interval":
            seconds = float(params.get("seconds", DEFAULT_INTERVAL_S))
            open_ = state.last_t is None or now - state.last_t >= seconds
            reason = f"{now - state.last_t:.3f} s since last" if state.last_t is not None else "first"
        elif step.rule == "once":
            open_ = state.fired == 0
            reason = "first" if open_ else "already fired"
        elif step.rule == "count-limit":
            limit = int(params.get("count", DEFAULT_COUNT))
            open_ = state.fired < limit
            reason = f"{state.fired}/{limit}"
        elif step.rule == "busy":
            open_ = self._pending is None
            reason = "idle" if open_ else f"busy with {self._pending.step.id}"
        elif step.rule == "delay":
            seconds = float(params.get("seconds", DEFAULT_INTERVAL_S))
            kind = kind_of(value)
            triggered = (bool(fired_of(value)) if kind == "event" else
                         int(points_of(value).shape[0]) > 0 if kind == "points" else value is not None)
            if state.triggered_t is None:
                if triggered:
                    state.triggered_t = now
                    reason = f"triggered, opens in {seconds:g} s"
                else:
                    reason = "no trigger"
                open_ = False
            else:
                waited = now - state.triggered_t
                open_ = waited >= seconds
                reason = f"{waited:.3f} s after the trigger" if open_ else f"{waited:.3f} of {seconds:g} s"
                if open_:
                    state.triggered_t = None
        else:
            raise CoordinateError(f"unknown gate rule {step.rule!r}")
        if open_:
            state.fired += 1
            state.last_t = now
            self.summary.gates_fired += 1
        self.log.append(tick, now, step.id, "open" if open_ else "closed", rule=step.rule, why=reason)
        event = Event(open_, {"rule": step.rule, "why": reason}, name=step.id)
        return {DEFAULT_PORT: value, FIRED_PORT: event} if open_ else {FIRED_PORT: event}

    # -- sinks ----------------------------------------------------------------

    def _sink(self, step: Sink, value, tick: int, now: float) -> None:
        if step.sink == "event-log":
            self.log.append(tick, now, step.id, "log", value=describe(value))
        elif step.sink == "overlay" and self.on_overlay is not None:
            try:
                self.on_overlay(step.id, value)
            except Exception as exc:
                self.log.append(tick, now, step.id, "overlay-failed", error=str(exc))

    # -- the action chain ------------------------------------------------------

    def _spec(self, step: Action):
        catalog = getattr(self.actions, "catalog", None)
        spec = catalog.get(step.action) if catalog is not None else None
        if spec is None:
            raise CoordinateError(f"unknown action {step.action!r}")
        return spec

    def _checked_inputs(self, step: Action, spec, inputs: list) -> list:
        """The inputs as the action may use them: the coordinate contract on
        points, finite numbers on values."""
        frame = spec.frame_of(step.params)
        checked = []
        for index, value in enumerate(inputs):
            accepted = spec.accepted_kind(index)
            kind = kind_of(value)
            if accepted == "points" or (accepted == "any" and kind == "points"):
                check_points(value, frame=frame if accepted == "points" else None, what=f"{step.id} input {index + 1}")
            elif accepted == "value" or (accepted == "any" and kind == "value"):
                finite_value(value, what=f"{step.id} input {index + 1}")
            elif accepted == "event" and fired_of(value) is None:
                raise CoordinateError(f"{step.id} input {index + 1}: not an event (got {type(value).__name__})")
            checked.append(value)
        return checked

    def _start_action(self, step: Action, inputs: list, values: dict, tick: int, chain_index: int) -> None:
        now = self.clock()
        spec = self._spec(step)
        params = {**spec.params, **step.params}
        inputs = self._checked_inputs(step, spec, inputs)
        request = ActionRequest(step_id=step.id, action=step.action, params=params, inputs=inputs,
                                mode=self.mode.value, tick=tick, epoch=self._epoch, timeout_s=step.timeout_s)
        effect = MODE_EFFECTS[self.mode]["actions"]
        summary = {"action": step.action, "params": params, "inputs": [describe(v) for v in inputs]}
        if effect == "skip":
            self.log.append(tick, now, step.id, "skipped", **summary)
            completion = SimpleCompletion(self._epoch, result={"skipped": True}, done=True)
        elif effect == "log":
            self.log.append(tick, now, step.id, "would", **summary, **_would(step.action, params, inputs))
            completion = SimpleCompletion(self._epoch, result={"would": True}, done=True)
        else:
            self.log.append(tick, now, step.id, "started", **summary)
            completion = self.actions.start(request)
        self.summary.actions_started += 1
        self._pending = _Pending(step, completion, now, tick, values, chain_index)
        self._poll_completion()

    def _poll_completion(self, *, force_timeout: bool = False) -> None:
        """Settle a pending completion that is done (or timed out) and
        continue the chain."""
        pending = self._pending
        if pending is None:
            return
        completion = pending.completion
        now = self.clock()
        if getattr(completion, "epoch", self._epoch) != self._epoch:
            self._pending = None   # a completion of an earlier arm: nothing to act on
            return
        timeout = pending.step.timeout_s
        timed_out = force_timeout or (timeout is not None and now - pending.started_at > timeout)
        if not completion.done and not timed_out:
            return
        if not completion.done:
            try:
                completion.abort()
            except Exception:
                pass
            outcome, error = TIMEOUT, f"no completion after {timeout or self.cleanup_budget_s:g} s"
        else:
            outcome, error = completion.outcome, completion.error
        self._pending = None
        if outcome != OK:
            self.summary.actions_failed += 1
            self.log.append(pending.tick, now, pending.step.id, outcome, error=error or outcome,
                            seconds=now - pending.started_at)
            if not getattr(completion, "ended", True):
                # Settled, but the hardware has not stopped (a quarantined
                # stage): the run says so, and keeps the device protected.
                self.summary.unfinished_action = pending.step.id
                self.log.append(pending.tick, now, pending.step.id, "still-running", outcome=outcome)
            self._fail(pending.step.id, f"action {pending.step.action!r} {outcome}: {error or outcome}")
            return
        self.summary.actions_completed += 1
        result = completion.result
        self.log.append(pending.tick, now, pending.step.id, "done", seconds=now - pending.started_at,
                        result=_plain(result) if not isinstance(result, dict) else _plain(result))
        values = pending.values
        values[f"{pending.step.id}.{DONE_PORT}"] = Event(True, dict(result) if isinstance(result, dict) else
                                                          {"result": _plain(result)}, name=pending.step.id)
        # the next action of the chain whose inputs the chain has
        for index in range(pending.chain_index + 1, len(self._chain)):
            step = self._chain[index]
            refs = list(step.inputs or [])
            if any(self._key(ref, values) not in values for ref in refs):
                continue
            inputs = [values[self._key(ref, values)] for ref in refs]
            try:
                self._start_action(step, inputs, values, pending.tick, index)
            except Exception as exc:
                self._fail(step.id, str(exc) if isinstance(exc, CoordinateError) else f"{type(exc).__name__}: {exc}")
            return
        # chain complete: what was taken before now is stale
        self._fresh_after = self._last_sequence
        self.log.append(pending.tick, now, "pipeline", "chain-done", fresh_after=self._fresh_after)


def _would(action: str, params: dict, inputs: list) -> dict:
    """The "would …" wording of a validated action, for the log."""
    if inputs and kind_of(inputs[0]) == "points":
        coordinates = points_of(inputs[0])
        if coordinates.shape[0]:
            first = coordinates[0]
            labels = ("y", "x", "z")[: coordinates.shape[1]] if coordinates.shape[1] <= 3 else ()
            where = ", ".join(f"{label}={float(v):.3f}" for label, v in zip(labels, first))
            target = params.get("positioner") or params.get("detector") or ""
            return {"message": f"would {action} {target} to ({where}) in {frame_of(inputs[0])}".replace("  ", " ")}
        return {"message": f"would {action}: no points"}
    if inputs and kind_of(inputs[0]) == "value":
        return {"message": f"would {action} with {value_of(inputs[0]):g}"}
    return {"message": f"would {action}"}


__all__ = [
    "DROPPED_BUSY",
    "DROPPED_STALE",
    "MODE_EFFECTS",
    "RAN",
    "TICK_FAILED",
    "ArmError",
    "Engine",
    "EngineError",
    "EventLog",
    "LogEntry",
    "RunMode",
    "RunSummary",
    "TickResult",
    "describe",
]


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
