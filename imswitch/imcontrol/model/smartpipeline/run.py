"""A live run: arm = preflight, reserve, apply the scouting role, take the
lease; run the engine on one worker thread; stop = idempotent cleanup in
the plan's order.

Arming happens in this order, and every refusal arms nothing: the document
is checked (the engine's ``arm_problems``), the devices the action steps
name are reserved for the pipeline (a device another owner holds refuses
the whole run; while the pipeline holds them, the GUI and scripts are
refused), the ``scouting`` setup mode is applied, the EVENT_STREAM lease is
taken, the engine is armed. Cleanup, after the engine has drained: the
``idle`` setup mode, release of every reservation except one whose device is
still executing (quarantined: it stays protected until the executor says it
returned), the lease. Roles and reservations happen at arm and stop only,
never per frame.

What the modes allow comes from the engine's ``MODE_EFFECTS``: Visualize
reserves nothing and applies no role; Validate reserves (so a dry run
finds the refusals a real run would) but applies no role; Experiment does
both.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from imswitch.imcommon.model.smartpipeline import Transform, pipeline_settings
from imswitch.imcontrol.model.smartpipeline.engine import MODE_EFFECTS, ArmError, Engine, EventLog, RunMode

IDLE = "idle"
ARMED = "armed"
RUNNING = "running"
STOPPING = "stopping"
STOPPED = "stopped"
FAILED = "failed"


class PipelineRun:
    """One armed pipeline, from arm to cleanup.

    ``apply_mode(modeName)`` applies a setup mode (on the GUI thread; the
    controller marshals) and returns ``None`` or a problem text;
    ``resource_registry`` is the reservations registry (``get_resource_registry()``
    by default); ``executor`` the control executor whose quarantine
    listener frees a stuck device's reservation when it returns.
    """

    def __init__(self, document, *, source, steps, actions, transforms=None, mode=RunMode.VALIDATE,
                 apply_mode: Callable[[str], str | None] | None = None, resource_registry=None, executor=None,
                 owner: str = "smart-pipeline", log: EventLog | None = None, clock=time.monotonic, logger=None,
                 registry=None, on_finished: Callable[["PipelineRun"], None] | None = None,
                 on_overlay=None, reserve_deadline_s: float = 5.0):
        self.document = document
        self.settings = pipeline_settings(document.workflow)
        self.source = source
        self.actions = actions
        self.mode = RunMode.of(mode)
        self.owner = owner
        self.logger = logger
        self._apply_mode = apply_mode
        self._registry = resource_registry
        self._executor = executor
        self._on_finished = on_finished
        self._clock = clock
        self.log = log if log is not None else EventLog()
        self.engine = Engine(document, source=source, steps=steps, actions=actions, transforms=transforms,
                             mode=self.mode, clock=clock, log=self.log, logger=logger, wait_actions=False,
                             registry=registry, on_overlay=on_overlay)
        self.state = IDLE
        self.error: str | None = None
        self.token: str | None = None
        self.reserved: set[str] = set()
        self.roles_applied: list[str] = []
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stop_requested = False
        self._cleaned = False
        self._reserve_deadline_s = float(reserve_deadline_s)

    # -- reservations -------------------------------------------------------------

    def _resource_registry(self):
        if self._registry is None:
            from imswitch.imcontrol.model.resources import get_resource_registry

            self._registry = get_resource_registry()
        return self._registry

    def _control_executor(self):
        if self._executor is None:
            from imswitch.imcontrol.model.measurement.controls import get_control_executor

            self._executor = get_control_executor()
        return self._executor

    def _reserve(self) -> None:
        keys = set(getattr(self.actions, "resources_for", lambda workflow: set())(self.document.workflow))
        if not keys:
            return
        registry = self._resource_registry()
        try:
            reservation = registry.reserve(sorted(keys), self.owner, deadline_s=self._reserve_deadline_s)
        except Exception as exc:
            raise ArmError(f"cannot reserve the pipeline's devices: {exc}") from exc
        self.token = getattr(reservation, "token", reservation)
        self.reserved = set(keys)
        if hasattr(self.actions, "token"):
            self.actions.token = self.token
        self.log.append(0, self._clock(), "pipeline", "reserved", owner=self.owner, resources=sorted(keys))

    def _release(self) -> None:
        if self.token is None:
            return
        quarantined = set(getattr(self.actions, "quarantined", lambda: set())()) & self.reserved
        registry = self._resource_registry()
        to_release = self.reserved - quarantined
        try:
            if quarantined:
                registry.release(self.token, to_release)
            else:
                registry.release(self.token)
        except Exception:
            pass   # the manager's shutdown may have ended it already
        if quarantined:
            self.log.append(self.engine._tick, self._clock(), "pipeline", "kept-reserved", resources=sorted(quarantined))
            token = self.token

            def freed(resource: str, now_quarantined: bool) -> None:
                if not now_quarantined and resource in quarantined:
                    try:
                        registry.release(token, {resource})
                    except Exception:
                        pass

            self._control_executor().add_listener(freed)
        self.reserved = set()
        self.token = None
        if hasattr(self.actions, "token"):
            self.actions.token = None

    # -- roles -------------------------------------------------------------------

    def _apply_role(self, role: str, *, required: bool) -> None:
        mode_name = self.settings.roles.get(role)
        if not mode_name:
            return
        if self._apply_mode is None:
            if required:
                raise ArmError(f"role {role!r} names setup mode {mode_name!r}, but setup modes are not available")
            return
        problem = self._apply_mode(mode_name)
        if problem:
            if required:
                raise ArmError(f"role {role!r} ({mode_name!r}): {problem}")
            self.log.append(self.engine._tick, self._clock(), "pipeline", "role-failed", role=role, mode=mode_name,
                            error=problem)
            return
        self.roles_applied.append(role)
        self.log.append(self.engine._tick, self._clock(), "pipeline", "role", role=role, mode=mode_name)

    # -- arm / run / stop ----------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def arm(self) -> None:
        """Preflight, reserve, apply ``scouting``, take the lease, arm the
        engine; :class:`ArmError` with the reason, arming nothing."""
        if self.state not in (IDLE, STOPPED, FAILED):
            raise ArmError(f"the pipeline is {self.state}")
        problems = self.engine.arm_problems()
        if problems:
            raise ArmError("the pipeline cannot be armed:\n  " + "\n  ".join(problems))
        effects = MODE_EFFECTS[self.mode]
        self.error = None
        self._stop_requested = False
        self._cleaned = False
        self.roles_applied = []
        try:
            if effects["reserve"]:
                self._reserve()
            if effects["roles"]:
                self._apply_role("scouting", required=True)
            self.source.start()
            problems = self._grid_problems()
            if problems:
                raise ArmError("the pipeline cannot be armed:\n  " + "\n  ".join(problems))
            self.engine.arm()
        except Exception:
            self._cleanup(after_engine=False)
            raise
        self.state = ARMED

    def _grid_problems(self) -> list[str]:
        """The transforms the pipeline uses, against the grid the source
        delivers (finding 3): a calibration made on another ROI or binning
        does not apply."""
        from imswitch.imcontrol.model.smartpipeline.calibrations import identity_problems

        identity = getattr(self.source, "identity", None)
        if identity is None:
            return []
        problems: list[str] = []
        for step in self.document.workflow.steps:
            if not isinstance(step, Transform):
                continue
            transform = self.engine.transforms.get(step.transform)
            if transform is not None:
                problems.extend(f"{step.id}: {reason}" for reason in identity_problems(transform, identity))
        return problems

    def start(self) -> None:
        """Run the engine on a worker thread (after :meth:`arm`)."""
        if self.state != ARMED:
            raise ArmError(f"arm the pipeline first (it is {self.state})")
        self.state = RUNNING
        self._thread = threading.Thread(target=self._run, name="smart-pipeline", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            summary = self.engine.run()
            self.error = summary.error
        except Exception as exc:  # the engine never raises; belt and braces
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self._cleanup(after_engine=True)
            self.state = FAILED if self.error else STOPPED
            if self._on_finished is not None:
                try:
                    self._on_finished(self)
                except Exception:
                    if self.logger is not None:
                        self.logger.exception("Smart pipeline: on_finished failed")

    def stop(self) -> None:
        """Ask the run to end; any thread, idempotent."""
        with self._lock:
            if self._stop_requested:
                return
            self._stop_requested = True
        if self.state == RUNNING:
            self.state = STOPPING
        self.engine.stop()
        if self.state == ARMED:
            # armed but never started: nothing is running, clean up now
            self._cleanup(after_engine=False)
            self.state = STOPPED

    def join(self, timeout: float | None = None) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _cleanup(self, *, after_engine: bool) -> None:
        with self._lock:
            if self._cleaned:
                return
            self._cleaned = True
        effects = MODE_EFFECTS[self.mode]
        if not after_engine:
            try:
                self.source.stop()
            except Exception:
                pass
        if effects["roles"] and "scouting" in self.roles_applied:
            try:
                self._apply_role("idle", required=False)
            except Exception as exc:
                self.log.append(self.engine._tick, self._clock(), "pipeline", "role-failed", role="idle", error=str(exc))
        self._release()
        try:
            self.source.close()
        except Exception as exc:
            self.log.append(self.engine._tick, self._clock(), "pipeline", "lease-release-failed", error=str(exc))

    # -- status -------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        summary = self.engine.summary
        stats = getattr(self.source, "stats", None)
        status = {
            "state": self.state, "mode": self.mode.value, "ticks": summary.ticks, "ran": summary.ran,
            "dropped_busy": summary.dropped_busy, "dropped_stale": summary.dropped_stale,
            "actions_started": summary.actions_started, "actions_completed": summary.actions_completed,
            "busy": self.engine.busy, "error": self.error, "unfinished_action": summary.unfinished_action,
            "reserved": sorted(self.reserved),
        }
        if stats is not None:
            status["inbox"] = {"pushed": stats.pushed, "taken": stats.taken, "replaced": stats.replaced,
                               "duplicates": stats.duplicates, "aged_out": stats.aged_out}
        return status


__all__ = ["ARMED", "FAILED", "IDLE", "RUNNING", "STOPPED", "STOPPING", "PipelineRun"]


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
