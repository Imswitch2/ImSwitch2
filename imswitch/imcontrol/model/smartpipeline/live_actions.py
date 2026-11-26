"""The live action backend: hand-written, owner-aware adapters.

Each adapter is one catalogue action done for real: it says which
resources it needs reserved (:meth:`ActionAdapter.resources`, resource
keys of :mod:`imswitch.imcontrol.model.resources`) and performs the action
with the pipeline's reservation token on the manager-level call
(``setPosition(..., owner=token)``), so a device the pipeline holds refuses
the GUI and scripts while the pipeline's own moves go through. A move runs
through the process-wide control executor with a deadline: a stage that
does not return is *quarantined* -- its completion is done (failed) but not
ended, the engine reports the action as unfinished, and the run keeps that
resource reserved until the executor says it returned.

Nothing here is generated from the scripting API: the forms come from the
specs in :mod:`.catalog`, the execution is curated (plan §9, decision 6).
The backend is Qt-free; what must happen on the GUI thread (a snap through
the recording controller) is handed in as a ``marshal`` callable that runs
a function there and returns its result.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Protocol

import numpy as np

from imswitch.imcommon.model.smartpipeline import ActionCatalog, ActionSpec, points_of
from imswitch.imcontrol.model.smartpipeline.boxes import BOX_COLUMNS, SOURCE_BOX_COLUMNS, box_of_row, plan_box_scan
from imswitch.imcontrol.model.smartpipeline.catalog import LOG, MOVE_TO, RUN_SCAN, SNAP, WAIT, axis_targets, catalog
from imswitch.imcontrol.model.smartpipeline.ports import ActionRequest, SimpleCompletion


class ActionAdapter(Protocol):
    spec: ActionSpec

    def resources(self, params: dict) -> set[str]:
        """The resource keys the action needs reserved for these params."""

    def start(self, request: ActionRequest, token: str | None) -> SimpleCompletion: ...


def _result_for_log(request: ActionRequest) -> dict:
    return {"action": request.action, "tick": request.tick}


class LogAdapter:
    spec = LOG

    def resources(self, params: dict) -> set[str]:
        return set()

    def start(self, request: ActionRequest, token: str | None) -> SimpleCompletion:
        message = str(request.params.get("message") or "")
        return SimpleCompletion(request.epoch, result={"logged": True, "message": message}, done=True)


class WaitAdapter:
    """``wait``: a completion that settles after ``seconds``; abort ends it early."""

    spec = WAIT

    def resources(self, params: dict) -> set[str]:
        return set()

    def start(self, request: ActionRequest, token: str | None) -> SimpleCompletion:
        completion = SimpleCompletion(request.epoch)
        seconds = max(0.0, float(request.params.get("seconds") or 0.0))
        timer = threading.Timer(seconds, lambda: completion.complete({"waited": seconds}))
        timer.daemon = True
        completion.abort_hook = lambda: (timer.cancel(), True)[1]
        timer.start()
        return completion


class SnapAdapter:
    """One image through whatever ``snap(detector)`` does on the GUI thread
    (the recording controller's snap), marshalled."""

    spec = SNAP

    def __init__(self, snap: Callable[[str], Any] | None, marshal: Callable[[Callable[[], Any]], Any] | None = None):
        self._snap = snap
        self._marshal = marshal if marshal is not None else (lambda fn: fn())

    def resources(self, params: dict) -> set[str]:
        return set()

    def start(self, request: ActionRequest, token: str | None) -> SimpleCompletion:
        completion = SimpleCompletion(request.epoch)
        if self._snap is None:
            completion.fail("snap: no recording controller is available")
            return completion
        detector = str(request.params.get("detector") or "")
        try:
            result = self._marshal(lambda: self._snap(detector))
        except Exception as exc:
            completion.fail(f"snap: {type(exc).__name__}: {exc}")
            return completion
        completion.complete({"snapped": True, "detector": detector, "result": result if isinstance(result, (str, int, float, bool)) else None})
        return completion


class MoveToAdapter:
    """``move-to`` on the positioners manager, one axis per coordinate column."""

    spec = MOVE_TO

    def __init__(self, positionersManager, *, executor=None, logger=None):
        self._positioners = positionersManager
        self._executor = executor
        self._logger = logger
        self.quarantined: set[str] = set()
        self._lock = threading.Lock()

    @staticmethod
    def _key(positioner: str) -> str:
        from imswitch.imcontrol.model.resources import positioner_key

        return positioner_key(positioner)

    def resources(self, params: dict) -> set[str]:
        return {self._key(positioner) for positioner, _axis in axis_targets(params)}

    def start(self, request: ActionRequest, token: str | None) -> SimpleCompletion:
        completion = SimpleCompletion(request.epoch)
        params = request.params
        try:
            targets = axis_targets(params)
        except ValueError as exc:
            completion.fail(str(exc))
            return completion
        coordinates = points_of(request.inputs[0]) if request.inputs else None
        if coordinates is None or coordinates.shape[0] == 0:
            completion.complete({"moved": False, "reason": "no points"})
            return completion
        point = [float(v) for v in coordinates[0]]
        if len(point) < len(targets):
            completion.fail(f"move-to: {len(targets)} axes for a {len(point)}-column point")
            return completion
        relative = bool(params.get("relative", False))
        deadline = float(params.get("deadline_s") or MOVE_TO.params["deadline_s"])
        # one bounded step per positioner, in column order
        by_positioner: dict[str, list[tuple[str, float]]] = {}
        for (positioner, axis), value in zip(targets, point):
            by_positioner.setdefault(positioner, []).append((axis, value))
        moved: dict[str, dict[str, float]] = {}
        for positioner, moves in by_positioner.items():
            resource = self._key(positioner)

            def step(positioner=positioner, moves=moves):
                manager = self._positioners[positioner]
                for axis, value in moves:
                    if relative:
                        manager.move(value, axis, owner=token)
                    else:
                        manager.setPosition(value, axis, owner=token)

            ok, cause = self._run(resource, f"move-to {positioner}", step, deadline)
            if not ok:
                unfinished = "did not finish" in cause
                if unfinished:
                    with self._lock:
                        self.quarantined.add(resource)
                completion.fail(f"move-to {positioner}: {cause}", ended=not unfinished)
                return completion
            moved[positioner] = {axis: value for axis, value in moves}
        completion.complete({"moved": True, "relative": relative, "position": moved})
        return completion

    def _run(self, resource: str, label: str, fn, deadline_s: float) -> tuple[bool, str]:
        executor = self._executor
        if executor is None:
            from imswitch.imcontrol.model.measurement.controls import get_control_executor

            executor = get_control_executor()
        return executor.run_step(resource, label, fn, deadline_s)

    def released(self, resource: str) -> None:
        with self._lock:
            self.quarantined.discard(resource)


class RunScanAdapter:
    """``run-scan`` through the Scan panel's controller (the SimplePointScan
    plan) and the scan workflow service's exact completion.

    ``controllers()`` is the comm channel's controller registry (the scan
    controller is found under the ``source`` parameter, ``Scan`` by
    default); ``scan_workflow`` the service whose ``run_scan_prepared``
    dispatches to the UI thread itself; ``marshal`` runs a function on the
    GUI thread (the plan setters are widget code); ``capture()`` is the
    live source's :meth:`~.live_source.LiveFrameSource.capture_latest`,
    called when a scan completed so its frame becomes the next tick.
    """

    spec = RUN_SCAN

    def __init__(self, controllers, scan_workflow, *, marshal=None, capture=None, logger=None, recorder=None):
        self._controllers = controllers
        self._workflow = scan_workflow
        self._marshal = marshal if marshal is not None else (lambda fn: fn())
        self._capture = capture
        self._logger = logger
        #: A :class:`~.event_recording.TriggeredScanRecorder`: in Experiment,
        #: with ``save`` on, each scan is started as a scan-once recording of
        #: it (the scan is saved), not as a bare scan.
        self.recorder = recorder

    def _records(self, request: ActionRequest) -> bool:
        return (self.recorder is not None and request.mode == "experiment"
                and bool(request.params.get("save", True)))

    def resources(self, params: dict) -> set[str]:
        return set()   # scans admit the waveform output themselves (finding 1)

    def _controller(self, source: str):
        registry = self._controllers() if callable(self._controllers) else self._controllers
        controller = (registry or {}).get(source)
        if controller is None:
            raise RuntimeError(f"run-scan: no scan panel {source!r} in this setup")
        return controller

    def _centres(self, request: ActionRequest) -> list[dict]:
        """One scan centre (axis -> µm) per input point: all of them, or the
        first, as ``points`` says; at most ``max_points`` when above 0."""
        params = request.params
        coordinates = points_of(request.inputs[0]) if request.inputs else None
        if coordinates is None or coordinates.shape[0] == 0:
            return [{}]
        axes = [str(a) for a in (params.get("axes") or RUN_SCAN.params["axes"])]
        rows = coordinates if str(params.get("points", "all")).lower() == "all" else coordinates[:1]
        limit = int(params.get("max_points") or 0)
        if limit > 0:
            rows = rows[:limit]
        return [{axis: float(value) for axis, value in zip(axes, row)} for row in rows]

    def _prepare_advanced(self, controller, request: ActionRequest, centre: dict | None, mode: str,
                          lasers: list, dwell, plan: dict | None = None) -> dict:
        """The Advanced scan panel: its fields, through the controller's
        setters and its widget's. The scanned axes are the step's ``axes``
        (the positioners' names); an overview is ``field_um`` around 0, an
        acquisition ``length_um`` at ``pixel_um`` around the centre given. With
        a box ``plan`` (:func:`~.boxes.plan_box_scan`) the centre and the size
        are per axis, and only those are set (the panel keeps its step unless
        ``pixel_um`` is given)."""
        params = request.params
        axes = [str(a) for a in (params.get("axes") or RUN_SCAN.params["axes"])]
        widget = getattr(controller, "_widget", None)
        sizes: dict = {}
        steps: dict = {}
        if plan is not None:
            centre = dict(plan["centre"])
            sizes = dict(plan["size"])
            steps = dict(plan.get("steps") or {})
            size = None
            step = None
        elif mode == "overview":
            size = float(params.get("field_um") or 0.0) or None
            step = float(params.get("overview_pixel_um") or 0.0) or None
            centre = {axis: 0.0 for axis in axes}
        else:
            size = float(params.get("length_um") or 0.0) or None
            step = float(params.get("pixel_um") or 0.0) or None
        for axis in axes:
            if centre and axis in centre:
                controller.changeScanCenterPos(axis, float(centre[axis]))
            if plan is not None:
                if axis in sizes:
                    controller.changeScanSize(axis, float(sizes[axis]))
                if axis in steps and params.get("pixel_um") and widget is not None and hasattr(widget, "setScanStepSize"):
                    widget.setScanStepSize(axis, float(steps[axis]))
                continue
            if size:
                controller.changeScanSize(axis, size)
            if step and widget is not None and hasattr(widget, "setScanStepSize"):
                widget.setScanStepSize(axis, step)
        if dwell and widget is not None and hasattr(widget, "setSeqTimePar"):
            widget.setSeqTimePar(dwell)
        applied_lasers = []
        if lasers and widget is not None and hasattr(widget, "setLineStepEnabled"):
            known = list(getattr(getattr(controller, "_setupInfo", None), "lasers", {}) or {})
            for name in known:
                try:
                    widget.setLineStepEnabled(name, 0, name in lasers)
                    if name in lasers:
                        applied_lasers.append(name)
                except Exception:  # noqa: BLE001 - a laser the panel does not list
                    pass
        return {"mode": mode, "centre_um": dict(centre or {}), "size_um": (sizes or size), "pixel_um": (steps or step),
                "lasers": applied_lasers or lasers, "panel": "advanced"}

    # -- boxes (the Advanced panel) -----------------------------------------------

    @staticmethod
    def _is_advanced(controller) -> bool:
        return not hasattr(controller, "applyAcquisitionPlan")

    def _scanner_ranges(self, controller, axes: list, params: dict) -> dict:
        """``{axis: (low, high)}`` in micrometres the scanner of each axis can reach: the step's own
        ``range_um`` where given, else the positioner's ``minVolt``/``maxVolt`` times its
        ``conversionFactor`` (what the scan designer's voltage check compares against), narrowed
        by ``range_inset_um`` on both ends. An axis with no limit known is absent."""
        given = params.get("range_um") or {}
        inset = float(params.get("range_inset_um") or 0.0)
        positioners = getattr(getattr(controller, "_setupInfo", None), "positioners", None) or {}
        ranges: dict = {}
        for axis in axes:
            if axis in given and given[axis] is not None:
                low, high = (float(v) for v in given[axis])
            else:
                props = getattr(positioners.get(axis), "managerProperties", None) or {}
                lo_v, hi_v = props.get("minVolt"), props.get("maxVolt")
                if lo_v is None or hi_v is None:
                    continue
                conversion = float(props.get("conversionFactor", 1) or 1)
                low, high = sorted((float(lo_v) * conversion, float(hi_v) * conversion))
            ranges[axis] = (low + inset, high - inset)
        return ranges

    def _row_info(self, request: ActionRequest, row: int) -> dict:
        """What the trigger's row says, for the log: its label, dff, F, F0, the box in the camera's and
        the scan's coordinates, and the transform the points went through."""
        points = request.inputs[0]
        properties = getattr(points, "properties", None) or {}
        info: dict = {}
        for key in ("label", "dff", "F", "F0", "area", *SOURCE_BOX_COLUMNS, *BOX_COLUMNS):
            if key in properties:
                value = properties[key][row]
                info[key] = value.item() if hasattr(value, "item") else value
        coordinates = points_of(points)
        info["point"] = [float(v) for v in coordinates[row]]
        meta = getattr(points, "metadata", None) or {}
        if isinstance(meta, dict) and meta.get("transform"):
            info["transform"] = meta["transform"]
        return info

    def _box_targets(self, request: ActionRequest) -> list | None:
        """Per input point (as ``points``/``max_points`` select them) the scan the box asks for:
        ``{"row", "info", "plan"}``; None when the action is not box-driven (no box columns, the Advanced
        panel absent, ``box: off``)."""
        params = request.params
        if str(params.get("box") or "auto").lower() == "off" or not request.inputs:
            return None
        points = request.inputs[0]
        properties = getattr(points, "properties", None) or {}
        coordinates = points_of(points)
        if coordinates is None or not all(name in properties for name in BOX_COLUMNS):
            return None
        source = str(params.get("source") or "Scan")
        controller = self._controller(source)
        if not self._is_advanced(controller):
            return None
        axes = [str(a) for a in (params.get("axes") or RUN_SCAN.params["axes"])]
        rows = list(range(coordinates.shape[0]))
        if str(params.get("points", "all")).lower() != "all":
            rows = rows[:1]
        limit = int(params.get("max_points") or 0)
        if limit > 0:
            rows = rows[:limit]
        widget = getattr(controller, "_widget", None)

        def read():
            return {axis: (float(params.get("pixel_um") or 0.0) or float(widget.getScanStepSize(axis)))
                    for axis in axes[:2]}

        steps = self._marshal(read)
        ranges = self._scanner_ranges(controller, axes[:2], params)
        targets = []
        for row in rows:
            box = box_of_row(properties, row, axes)
            plan = plan_box_scan(
                box, steps=steps, margin_um=float(params.get("margin_um") or 0.0),
                margin_frac=float(params.get("margin_frac") or 0.0), min_size_um=float(params.get("min_size_um") or 0.0),
                max_size_um=float(params.get("max_size_um") or 0.0), ranges=ranges,
                min_overlap=float(params.get("min_overlap", 0.5)))
            plan["steps"] = steps
            plan["ranges"] = ranges
            targets.append({"row": row, "info": self._row_info(request, row), "plan": plan})
        return targets

    def plan(self, request: ActionRequest) -> dict:
        """What this request would scan, without touching the panel's fields or starting anything: the
        dry run's report (the box in scan micrometres, the padded and clamped rectangle, or why the trigger
        is skipped), and where an Experiment run would save it."""
        targets = self._box_targets(request)
        saving = {}
        if self.recorder is not None and bool(request.params.get("save", True)):
            saving = {"would_save_to": self.recorder.folder, "would_save_detectors": list(self.recorder.detectors)}
        if targets is None:
            return saving
        return {"scan_plan": [_plan_report(t) for t in targets],
                "message": "; ".join(_plan_sentence(t) for t in targets), **saving}

    def _snapshot_advanced(self, controller, axes: list) -> dict | None:
        """The panel's scan fields now (None when its widget has no getters: nothing to put back)."""
        widget = getattr(controller, "_widget", None)
        snap: dict = {"centre": {}, "size": {}, "step": {}, "dwell": None, "lines": {}}
        if widget is None or not hasattr(widget, "getScanCenterPos"):
            return None
        for axis in axes:
            snap["centre"][axis] = float(widget.getScanCenterPos(axis))
            snap["size"][axis] = float(widget.getScanSize(axis))
            snap["step"][axis] = float(widget.getScanStepSize(axis))
        if hasattr(widget, "getSeqTimePar"):
            snap["dwell"] = float(widget.getSeqTimePar())
        if hasattr(widget, "getLineStepEnabled"):
            for name in list(getattr(getattr(controller, "_setupInfo", None), "lasers", {}) or {}):
                snap["lines"][name] = bool(widget.getLineStepEnabled(name, 0))
        return snap

    def _restore_advanced(self, controller, snap: dict) -> None:
        widget = getattr(controller, "_widget", None)
        for axis, value in snap.get("centre", {}).items():
            controller.changeScanCenterPos(axis, value)
        for axis, value in snap.get("size", {}).items():
            controller.changeScanSize(axis, value)
        if widget is not None:
            for axis, value in snap.get("step", {}).items():
                widget.setScanStepSize(axis, value)
            if snap.get("dwell") is not None and hasattr(widget, "setSeqTimePar"):
                widget.setSeqTimePar(snap["dwell"])
            if hasattr(widget, "setLineStepEnabled"):
                for name, enabled in snap.get("lines", {}).items():
                    widget.setLineStepEnabled(name, 0, enabled)

    def _prepare(self, request: ActionRequest, centre: dict | None = None, plan: dict | None = None) -> dict:
        """On the GUI thread: put the Scan panel's plan as the step says."""
        params = request.params
        source = str(params.get("source") or "Scan")
        controller = self._controller(source)
        mode = str(params.get("mode") or "acquisition").lower()
        lasers = [str(name) for name in (params.get("lasers") or [])]
        dwell = float(params.get("dwell_s") or 0.0) or None
        if not hasattr(controller, "applyAcquisitionPlan"):
            return self._prepare_advanced(controller, request, centre, mode, lasers, dwell, plan)
        if mode == "overview":
            field = float(params.get("field_um") or 0.0)
            controller.applyOverviewPlan(field_um=field or None, lasers=lasers or None)
            return {"mode": mode, "field_um": field}
        centre = dict(centre or {})
        controller.applyAcquisitionPlan(
            centres_um=centre, length_um=float(params.get("length_um") or 0.0) or None,
            step_um=float(params.get("pixel_um") or 0.0) or None, dwell_s=dwell, lasers=lasers or None,
        )
        return {"mode": mode, "centre_um": centre, "length_um": params.get("length_um"),
                "pixel_um": params.get("pixel_um"), "lasers": lasers}

    def _busy_reason(self, source: str, timeout_s: float = 0.0) -> str:
        """Why the scan panel ``source`` cannot take a scan now ('' when it
        can), after waiting up to ``timeout_s`` for it to become free."""
        deadline = time.monotonic() + timeout_s
        while True:
            reason = self._marshal(lambda: scan_panel_busy(self._controller(source)))
            if not reason or time.monotonic() > deadline:
                return reason
            time.sleep(0.05)

    def _wait_idle(self, source: str, timeout_s: float = 5.0) -> None:
        """The panel releases its run a moment after the completion resolves;
        a plan set in that moment would be refused, so wait for it."""
        reason = self._busy_reason(source, timeout_s)
        if reason:
            raise RuntimeError(f"the Scan panel {source!r} stayed busy for {timeout_s:g} s ({reason})")

    def _one_scan(self, request: ActionRequest, centre: dict | None, state: dict, plan: dict | None = None,
                  info: dict | None = None):
        """Prepare and run one scan; returns its request completion (the
        scan runs while the caller waits on it). When the scan is saved
        (:meth:`_records`) it is started by its recording instead, and the
        completion is that recording's."""
        t0 = time.perf_counter()
        self._wait_idle(str(request.params.get("source") or "Scan"))
        t1 = time.perf_counter()
        summary = self._marshal(lambda: self._prepare(request, centre, plan))
        t2 = time.perf_counter()
        source = str(request.params.get("source") or "Scan")
        if self._records(request):
            recording = self._start_recording(request, source, summary, info, state)
            state["current"] = ("recording", recording)
            t3 = time.perf_counter()
            state["latency"] = {"wait_idle": round(t1 - t0, 4), "set_parameters": round(t2 - t1, 4),
                                "start_request": round(t3 - t2, 4)}
            return summary, recording
        result = self._workflow.run_scan_prepared(True, False, notify_starting=True, preferred_source=source,
                                                  exact_completion=True)
        if not getattr(result, "accepted", False) or not result.acceptedCompletions:
            raise RuntimeError(f"refused: {getattr(result, 'rejectionMessage', '') or 'not accepted'}")
        owner, run_token, request_completion = result.acceptedCompletions[0]
        state["current"] = (owner, run_token)
        t3 = time.perf_counter()
        state["latency"] = {"wait_idle": round(t1 - t0, 4), "set_parameters": round(t2 - t1, 4),
                            "start_request": round(t3 - t2, 4)}
        return summary, request_completion

    # -- saving the triggered scan ----------------------------------------------------

    def _event_fields(self, request: ActionRequest, summary: dict, info: dict | None, event: int) -> dict:
        """What the event's file records about it, before the scan."""
        info = dict(info or {})
        recorder = self.recorder
        context = dict(getattr(request, "context", {}) or {})
        label = info.get("label")
        fields = {
            "event": event, "region": recorder.region,
            "roi_label": label, "roi_id": None if label is None else f"R{recorder.region}-{int(float(label))}",
            "dff": info.get("dff"), "F": info.get("F"), "F0": info.get("F0"), "area_px": info.get("area"),
            "point": info.get("point"),
            "bbox_camera_px": ([info.get(k) for k in SOURCE_BOX_COLUMNS]
                               if all(k in info for k in SOURCE_BOX_COLUMNS) else None),
            "bbox_scan_um": [info.get(k) for k in BOX_COLUMNS] if all(k in info for k in BOX_COLUMNS) else None,
            "scan_centre_um": summary.get("centre_um"), "scan_size_um": summary.get("size_um"),
            "scan_pixel_um": summary.get("pixel_um"), "lasers": summary.get("lasers"),
            "frame_sequence": context.get("frame_sequence"), "frame_wall": context.get("frame_wall"),
            "decision_wall": context.get("decision_wall"), "tick": request.tick, "step": request.step_id,
            "stage_position_um": recorder.stage_position(),
        }
        # The leaves the etSTED route writes too (``EtSTED:<leaf>``), same names
        # and formats, so one reader handles the files of both routes.
        from imswitch.imcontrol.model.smartpipeline.event_recording import utc_iso

        axes = [str(a) for a in (request.params.get("axes") or RUN_SCAN.params["axes"])][:2]
        centre = summary.get("centre_um") or {}
        fields.update(route="smart-pipeline", event_index=event,
                      session_id=os.path.basename(os.path.normpath(recorder.folder)),
                      pipeline_name=str(recorder.base_attrs.get("SmartPipeline:pipeline", "")),
                      scan_axes=",".join(axes),
                      scan_center_um=(np.asarray([float(centre[a]) for a in axes], dtype=float)
                                      if all(a in centre for a in axes) else None),
                      trigger_frame_time_utc=(utc_iso(context["frame_wall"])
                                              if context.get("frame_wall") else None))
        if fields.get("bbox_camera_px"):
            y0, x0, y1, x1 = (float(v) for v in fields["bbox_camera_px"])
            fields.update(event_coords_px=np.asarray([(x0 + x1) / 2, (y0 + y1) / 2], dtype=float),
                          event_coords_order="column,row")
        transform = info.get("transform")
        if isinstance(transform, dict):
            fields["transform_id"] = transform.get("id")
            fields["transform_name"] = transform.get("id")
            fields["transform"] = transform
            for key in ("residual_rms_um", "residual_um", "residual"):
                if transform.get(key) is not None:
                    fields["transform_residual"] = transform.get(key)
                    break
        return fields

    def _start_recording(self, request: ActionRequest, source: str, summary: dict, info: dict | None,
                         state: dict | None = None):
        from imswitch.imcontrol.model.smartpipeline.event_recording import event_attrs, event_file_name, utc_iso

        recorder = self.recorder
        event = recorder.next_event()
        fields = self._event_fields(request, summary, info, event)
        fields["scan_start_wall"] = time.time()
        fields["scan_start_time_utc"] = utc_iso(fields["scan_start_wall"])
        name = event_file_name(event, fields.get("roi_label"), recorder.region)
        summary["event"] = event
        summary["file_base"] = name
        try:
            return recorder.start(source, file_name=name, attrs=event_attrs(fields), event=event, fields=fields,
                                  pre=state.get("pre") if state is not None else None)
        except Exception as exc:
            raise RuntimeError(f"the triggered scan was not started because it could not be saved: {exc}") from exc

    def start(self, request: ActionRequest, token: str | None) -> SimpleCompletion:
        completion = SimpleCompletion(request.epoch)
        mode = str(request.params.get("mode") or "acquisition").lower()
        state: dict = {"current": None, "abort": threading.Event()}
        capture = self._capture
        t_request = time.perf_counter()
        try:
            targets = None if mode == "overview" else self._box_targets(request)
        except Exception as exc:  # noqa: BLE001 - reported as the action's failure
            completion.fail(f"run-scan: {type(exc).__name__}: {exc}")
            return completion
        if targets is not None:
            jobs = [{"centre": None, "plan": t["plan"], "info": t["info"], "row": t["row"]} for t in targets]
        else:
            jobs = [{"centre": c, "plan": None, "info": None, "row": None}
                    for c in ([None] if mode == "overview" else self._centres(request))]
        restore = bool(request.params.get("restore", True))
        source = str(request.params.get("source") or "Scan")
        # The camera frames before the trigger, taken now, on the engine's
        # thread at the decision (references): later they would be gone.
        archive = getattr(self.recorder, "archive", None) if self._records(request) else None
        state["pre"] = (archive.frames_before((request.context or {}).get("frame_sequence"))
                        if archive is not None else None)
        axes = [str(a) for a in (request.params.get("axes") or RUN_SCAN.params["axes"])]

        def run_all():
            scans, skipped = [], []
            snapshot = None
            controller = None
            try:
                runnable = [j for j in jobs if j["plan"] is None or not j["plan"]["skip"]]
                skipped = [{"row": j["row"], "reason": j["plan"]["skip"], "trigger": j["info"]}
                           for j in jobs if j["plan"] is not None and j["plan"]["skip"]]
                # A panel that is scanning or stopping (the user's scan, or the
                # end of the previous one that has not let go yet) is not
                # started into: the trigger is skipped, with the reason.
                busy = self._busy_reason(source, BUSY_WAIT_S) if runnable else ""
                if busy:
                    skipped += [{"row": j["row"], "reason": f"the scan panel {source!r} is busy: {busy}",
                                 "trigger": j["info"]} for j in runnable]
                    runnable = []
                if restore and runnable and self._is_advanced(self._controller(source)):
                    controller = self._controller(source)
                    self._wait_idle(source)
                    snapshot = self._marshal(lambda: self._snapshot_advanced(controller, axes))
                for index, job in enumerate(runnable):
                    if state["abort"].is_set():
                        completion.cancel()
                        return
                    summary, request_completion = self._one_scan(request, job["centre"], state, job["plan"],
                                                                 job["info"])
                    if job["plan"] is not None:
                        summary["trigger"] = job["info"]
                        summary["notes"] = job["plan"]["notes"]
                        summary["pixels"] = job["plan"]["pixels"]
                        state["latency"]["request_to_start"] = round(time.perf_counter() - t_request, 4)
                    request_completion.wait()
                    state["current"] = None
                    recording = request_completion if summary.get("file_base") is not None else None
                    if not request_completion.successful:
                        what = "the triggered scan was not saved" if recording is not None else "the scan failed"
                        if recording is not None:
                            self.recorder.failed(recording, request_completion.message or "the recording failed")
                        completion.fail(f"run-scan: {what}: {request_completion.message or 'no reason given'}"
                                        + (f" (scan {index + 1} of {len(runnable)})" if len(runnable) > 1 else ""))
                        return
                    if recording is not None:
                        from imswitch.imcontrol.model.smartpipeline.event_recording import utc_iso

                        end = {"scan_end_wall": recording.ended_wall, "latency_s": dict(state.get("latency") or {}),
                               "scan_end_time_utc": utc_iso(recording.ended_wall) if recording.ended_wall else None}
                        warnings = self.recorder.finish(recording, end)
                        summary["saved"] = list(recording.paths)
                        if warnings:
                            summary["save_warnings"] = warnings
                    captured = False
                    if capture is not None:
                        try:
                            captured = bool(capture(metadata={"scan": summary}))
                        except Exception as exc:  # noqa: BLE001 - reported in the result
                            captured = f"not captured: {exc}"
                    scans.append({**summary, "frame": captured, **({"latency_s": state["latency"]} if job["plan"] else {})})
                if archive is not None:
                    for skip in skipped:
                        info = dict(skip.get("trigger") or {})
                        archive.add_row({**{k: info.get(k) for k in ("dff", "F", "F0", "point")},
                                         "roi_label": info.get("label"), "status": "skipped",
                                         "reason": skip.get("reason"), "tick": request.tick,
                                         "stage_position_um": self.recorder.stage_position()})
                completion.complete({"scanned": bool(scans), "scans": len(scans), "mode": mode, "skipped": skipped,
                                     **(scans[-1] if scans else {}), "all": scans})
            except Exception as exc:  # noqa: BLE001 - the action failed
                completion.fail(f"run-scan: {type(exc).__name__}: {exc}")
            finally:
                if snapshot is not None:
                    try:
                        self._wait_idle(source)
                        self._marshal(lambda: self._restore_advanced(controller, snapshot))
                    except Exception as exc:  # noqa: BLE001 - say that the panel was not put back
                        if self._logger is not None:
                            self._logger.warning("run-scan: the panel's scan parameters were not restored: %s", exc)

        def abort():
            state["abort"].set()
            current = state.get("current")
            if current and current[0] == "recording":
                return current[1].abort()
            return self._abort(*current) if current else True

        completion.abort_hook = abort
        threading.Thread(target=run_all, name="run-scan", daemon=True).start()
        return completion

    def _abort(self, owner, run_token) -> bool:
        try:
            self._workflow.abort_scan_from(owner, run_token)
            return True
        except Exception:
            if self._logger is not None:
                self._logger.warning("run-scan: abort failed", exc_info=True)
            return False


#: How long a trigger waits for a busy scan panel to let go before it is skipped, s.
BUSY_WAIT_S = 1.0


def scan_panel_busy(controller) -> str:
    """Why a scan panel's controller is busy -- a scan running, a stop still
    taking the devices down, a run held between iterations -- or ''."""
    state = getattr(controller, "__dict__", {})
    if state.get("_stopInFlight", False):
        return "its scan is stopping"
    if bool(getattr(controller, "isRunning", False)):
        return "a scan is running"
    busy = getattr(controller, "_isBusy", None)
    if callable(busy) and busy():
        return "a scan is running"
    if state.get("_scanRunToken") is not None:
        return "a scan run is still open"
    if state.get("_repeatPending", False):
        return "a repeated scan is pending"
    holds = getattr(controller, "_holdsScanRunBetweenIterations", None)
    if callable(holds):
        try:
            if holds():
                return "a scan run is held between iterations"
        except Exception:  # noqa: BLE001 - an unreadable state is not a reason to skip
            pass
    return ""


def _plan_report(target: dict) -> dict:
    plan = target["plan"]
    return {"row": target["row"], "trigger": target["info"], "skip": plan["skip"],
            "padded_um": {a: [round(v, 4) for v in lh] for a, lh in plan["padded"].items()},
            "centre_um": plan["centre"], "size_um": plan["size"], "pixels": plan["pixels"],
            "range_um": {a: list(r) for a, r in plan["ranges"].items()}, "notes": plan["notes"]}


def _plan_sentence(target: dict) -> str:
    plan, info = target["plan"], target["info"]
    who = f"ROI {info.get('label', '?')} (dff {info.get('dff', float('nan')):.3g})"
    if plan["skip"]:
        return f"would SKIP {who}: {plan['skip']}"
    where = ", ".join(f"{a}={plan['centre'][a]:.3f}+/-{plan['size'][a] / 2:.3f}" for a in plan["centre"])
    note = f" [{'; '.join(plan['notes'])}]" if plan["notes"] else ""
    return f"would scan {who}: centre/half-size um {where}{note}"


class LiveActionBackend:
    """The engine's action backend over the adapters; ``token`` is the
    pipeline's reservation token, set by the run after reserving."""

    def __init__(self, adapters, *, catalog: ActionCatalog | None = None):
        self._adapters: dict[str, ActionAdapter] = {adapter.spec.id: adapter for adapter in adapters}
        self.catalog = catalog if catalog is not None else ActionCatalog(adapter.spec for adapter in adapters)
        self.token: str | None = None
        self.started: list[ActionRequest] = []

    def adapter(self, action_id: str) -> ActionAdapter | None:
        return self._adapters.get(str(action_id))

    def resources_for(self, workflow) -> set[str]:
        """Every resource key the pipeline's action steps need."""
        from imswitch.imcommon.model.smartpipeline import action_chain

        keys: set[str] = set()
        for step in action_chain(workflow):
            adapter = self.adapter(step.action)
            if adapter is None:
                continue
            spec = adapter.spec
            keys |= adapter.resources({**spec.params, **step.params})
        return keys

    def quarantined(self) -> set[str]:
        found: set[str] = set()
        for adapter in self._adapters.values():
            found |= set(getattr(adapter, "quarantined", ()))
        return found

    def plan(self, request: ActionRequest) -> dict:
        """What the action would do, as a report (the dry run's log line); {} when the
        adapter has nothing to say."""
        adapter = self.adapter(request.action)
        planner = getattr(adapter, "plan", None)
        return planner(request) if callable(planner) else {}

    def start(self, request: ActionRequest) -> SimpleCompletion:
        adapter = self.adapter(request.action)
        if adapter is None:
            raise RuntimeError(f"no adapter for action {request.action!r}")
        self.started.append(request)
        return adapter.start(request, self.token)


def live_backend(positionersManager, *, snap=None, marshal=None, executor=None, logger=None, scan=None) -> LiveActionBackend:
    """The v1 backend: move-to, snap, log and -- with ``scan`` (a dict of
    ``controllers``, ``workflow``, ``capture``) -- run-scan."""
    adapters = [
        MoveToAdapter(positionersManager, executor=executor, logger=logger),
        SnapAdapter(snap, marshal),
        LogAdapter(),
        WaitAdapter(),
    ]
    if scan is not None:
        adapters.append(RunScanAdapter(scan.get("controllers"), scan.get("workflow"), marshal=marshal,
                                       capture=scan.get("capture"), logger=logger,
                                       recorder=scan.get("recorder")))
    return LiveActionBackend(adapters, catalog=catalog())


__all__ = ["BUSY_WAIT_S", "ActionAdapter", "LiveActionBackend", "LogAdapter", "MoveToAdapter", "RunScanAdapter",
           "SnapAdapter", "WaitAdapter", "live_backend", "scan_panel_busy"]


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
