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

import threading
import time
from typing import Any, Callable, Protocol

from imswitch.imcommon.model.smartpipeline import ActionCatalog, ActionSpec, points_of
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

    def __init__(self, controllers, scan_workflow, *, marshal=None, capture=None, logger=None):
        self._controllers = controllers
        self._workflow = scan_workflow
        self._marshal = marshal if marshal is not None else (lambda fn: fn())
        self._capture = capture
        self._logger = logger

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

    def _prepare(self, request: ActionRequest, centre: dict | None = None) -> dict:
        """On the GUI thread: put the Scan panel's plan as the step says."""
        params = request.params
        source = str(params.get("source") or "Scan")
        controller = self._controller(source)
        mode = str(params.get("mode") or "acquisition").lower()
        lasers = [str(name) for name in (params.get("lasers") or [])]
        dwell = float(params.get("dwell_s") or 0.0) or None
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

    def _wait_idle(self, source: str, timeout_s: float = 5.0) -> None:
        """The panel releases its run a moment after the completion resolves;
        a plan set in that moment would be refused, so wait for it."""
        deadline = time.monotonic() + timeout_s
        while True:
            busy = self._marshal(lambda: bool(getattr(self._controller(source), "_isBusy", lambda: False)()))
            if not busy:
                return
            if time.monotonic() > deadline:
                raise RuntimeError(f"the Scan panel {source!r} stayed busy for {timeout_s:g} s")
            time.sleep(0.05)

    def _one_scan(self, request: ActionRequest, centre: dict | None, state: dict):
        """Prepare and run one scan; returns its request completion (the
        scan runs while the caller waits on it)."""
        self._wait_idle(str(request.params.get("source") or "Scan"))
        summary = self._marshal(lambda: self._prepare(request, centre))
        source = str(request.params.get("source") or "Scan")
        result = self._workflow.run_scan_prepared(True, False, notify_starting=True, preferred_source=source,
                                                  exact_completion=True)
        if not getattr(result, "accepted", False) or not result.acceptedCompletions:
            raise RuntimeError(f"refused: {getattr(result, 'rejectionMessage', '') or 'not accepted'}")
        owner, run_token, request_completion = result.acceptedCompletions[0]
        state["current"] = (owner, run_token)
        return summary, request_completion

    def start(self, request: ActionRequest, token: str | None) -> SimpleCompletion:
        completion = SimpleCompletion(request.epoch)
        mode = str(request.params.get("mode") or "acquisition").lower()
        centres = [None] if mode == "overview" else self._centres(request)
        state: dict = {"current": None, "abort": threading.Event()}
        capture = self._capture

        def run_all():
            scans = []
            try:
                for index, centre in enumerate(centres):
                    if state["abort"].is_set():
                        completion.cancel()
                        return
                    summary, request_completion = self._one_scan(request, centre, state)
                    request_completion.wait()
                    state["current"] = None
                    if not request_completion.successful:
                        completion.fail(f"run-scan: {request_completion.message or 'the scan failed'}"
                                        + (f" (scan {index + 1} of {len(centres)})" if len(centres) > 1 else ""))
                        return
                    captured = False
                    if capture is not None:
                        try:
                            captured = bool(capture(metadata={"scan": summary}))
                        except Exception as exc:  # noqa: BLE001 - reported in the result
                            captured = f"not captured: {exc}"
                    scans.append({**summary, "frame": captured})
                completion.complete({"scanned": True, "scans": len(scans), "mode": mode,
                                     **(scans[-1] if scans else {}), "all": scans})
            except Exception as exc:  # noqa: BLE001 - the action failed
                completion.fail(f"run-scan: {type(exc).__name__}: {exc}")

        def abort():
            state["abort"].set()
            current = state.get("current")
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
                                       capture=scan.get("capture"), logger=logger))
    return LiveActionBackend(adapters, catalog=catalog())


__all__ = ["ActionAdapter", "LiveActionBackend", "LogAdapter", "MoveToAdapter", "RunScanAdapter", "SnapAdapter",
           "WaitAdapter", "live_backend"]


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
