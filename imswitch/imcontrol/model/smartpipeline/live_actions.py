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
from typing import Any, Callable, Protocol

from imswitch.imcommon.model.smartpipeline import ActionCatalog, ActionSpec, points_of
from imswitch.imcontrol.model.smartpipeline.catalog import LOG, MOVE_TO, SNAP, axis_targets, catalog
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


def live_backend(positionersManager, *, snap=None, marshal=None, executor=None, logger=None) -> LiveActionBackend:
    """The v1 backend: move-to, snap and log over the given managers."""
    adapters = [
        MoveToAdapter(positionersManager, executor=executor, logger=logger),
        SnapAdapter(snap, marshal),
        LogAdapter(),
    ]
    return LiveActionBackend(adapters, catalog=catalog())


__all__ = ["ActionAdapter", "LiveActionBackend", "LogAdapter", "MoveToAdapter", "SnapAdapter", "live_backend"]


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
