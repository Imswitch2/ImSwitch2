"""Target timelapse workflow for scheduled target revisits.

Model-level workflow for multi-timepoint acquisition at discrete target positions.
Loops over enabled targets in order for each timepoint (A -> B -> C -> A pattern),
moves XY stage to each target, settles, calls an acquisition callback, and records
metadata including target id, timepoint, stage position, timestamps, success/failure,
and callback result.

Phases T2/T3 of the tiled-target-timelapse-smart-events plan. Event probing can
be supplied as the acquisition callback through the Phase T4 event-gated
workflow primitives.

Copyright (C) 2020-2026 ImSwitch developers
This file is part of ImSwitch.

ImSwitch is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

ImSwitch is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional, Tuple

if TYPE_CHECKING:
    from imswitch.imcontrol.model.workflows.facade import MicroscopeFacade
    from imswitch.imcontrol.model.workflows.target import Target, TargetList
    from imswitch.imcontrol.model.workflows.smart_mode_workflow import (
        SmartModeWorkflowAdapter,
        ModeTransition,
    )

logger = logging.getLogger(__name__)


@dataclass
class ModeConfig:
    """Role configuration for setup mode integration.

    Attributes:
        event_role: Role to apply before each target acquisition (e.g., 'event').
            Required when mode integration is enabled.
        resume_role: Optional role to apply after each target acquisition
            (e.g., 'resume' or 'scouting'). If None, no resume role is applied.
        idle_role: Optional role to apply at workflow completion or failure
            (e.g., 'idle'). If None, no idle role is applied.
        preflight_roles: List of roles to preflight before workflow starts.
            Defaults to [event_role] if not specified. Set to empty list to
            skip preflight.
    """

    event_role: str
    resume_role: Optional[str] = None
    idle_role: Optional[str] = None
    preflight_roles: Optional[list[str]] = None

    def __post_init__(self):
        if self.preflight_roles is None:
            self.preflight_roles = [self.event_role]


@dataclass
class TargetTimelapseParams:
    """Parameters for target timelapse workflow.

    Attributes:
        n_timepoints: Number of timepoints to acquire (must be >= 1).
        interval_s: Cycle interval in seconds measured between starts of cycles.
            A cycle visits all enabled targets once. Set to 0 for back-to-back cycles.
        settle_s: Settle time in seconds after stage movement before acquisition.
            Defaults to 0.
        failure_policy: Action on acquisition failure. ``"continue"`` proceeds to
            next target/timepoint; ``"abort"`` stops immediately. Defaults to ``"continue"``.
    """

    n_timepoints: int
    interval_s: float = 0.0
    settle_s: float = 0.0
    failure_policy: Literal["continue", "abort"] = "continue"


@dataclass
class TargetAcquisitionResult:
    """Metadata for one target acquisition attempt.

    Attributes:
        target_id: Unique target identifier.
        timepoint: Zero-based timepoint index.
        stage_xy: Actual stage position ``(x, y)`` in um at acquisition time.
        started_s: Start timestamp (time.time()).
        completed_s: Completion timestamp (time.time()).
        success: True if acquisition succeeded without exception.
        callback_result: Return value from acquisition_callback if success=True, else None.
        error: Exception message if success=False, else None.
        mode_transitions: List of ModeTransition records for this acquisition
            (event/resume roles). Empty when mode integration is not used.
        mode_failure: True when this failed because a required mode role did not
            apply cleanly.
    """

    target_id: int
    timepoint: int
    stage_xy: Tuple[float, float]
    started_s: float
    completed_s: float
    success: bool
    callback_result: Any = None
    error: Optional[str] = None
    mode_transitions: list["ModeTransition"] = field(default_factory=list)
    mode_failure: bool = False

    @property
    def elapsed_s(self) -> float:
        """Elapsed time in seconds for this acquisition."""
        return self.completed_s - self.started_s


@dataclass
class TargetTimelapseResult:
    """Overall result from target timelapse workflow run.

    Attributes:
        acquisitions: List of per-target-per-timepoint metadata in execution order.
        aborted: True if workflow was stopped early due to abort-on-failure.
        total_timepoints_completed: Number of full cycles (timepoints) completed.
        preflight_passed: True if mode preflight passed, False if it failed,
            None if no preflight was performed.
        preflight_messages: Messages from mode preflight. Empty if no preflight.
        mode_failure: True if a required mode apply failed and stopped the workflow.
        idle_transition: ModeTransition record for final idle role application,
            or None if no idle was applied.
    """

    acquisitions: list[TargetAcquisitionResult] = field(default_factory=list)
    aborted: bool = False
    total_timepoints_completed: int = 0
    preflight_passed: Optional[bool] = None
    preflight_messages: list[str] = field(default_factory=list)
    mode_failure: bool = False
    idle_transition: Optional["ModeTransition"] = None


class TargetTimelapseWorkflow:
    """Orchestrate multi-timepoint acquisition at discrete target positions.

    Loops over enabled targets in order for each timepoint, moves XY stage to
    each target, settles, calls an acquisition callback, and records metadata.

    Phase T3 extension: Optional setup mode integration through mode_adapter and
    mode_config. When supplied, the workflow preflights required roles before any
    stage motion, applies event role before each acquisition, optionally applies
    resume role after each acquisition, and applies idle role at completion or
    failure. If preflight or required role apply fails, stops before unsafe
    acquisition and records the failure. Preserves existing behavior when no
    mode_adapter/mode_config is supplied.

    Args:
        facade: MicroscopeFacade providing access to stage controller.
        targets: TargetList containing target positions. Only enabled targets
            are visited.
        params: TargetTimelapseParams configuration.
        acquisition_callback: Callable invoked once per enabled target per timepoint
            after stage move and settle. Signature: ``f(target, timepoint_idx) -> Any``.
            Return value is recorded in metadata. Should raise on failure.
        workflow_name: Workflow name for mode service role resolution. Required
            when mode_adapter and mode_config are supplied. Defaults to
            'TargetTimelapse'.
        mode_adapter: Optional SmartModeWorkflowAdapter for setup mode integration.
            If None, no mode transitions are performed.
        mode_config: Optional ModeConfig specifying roles to apply. Required when
            mode_adapter is supplied.
        sleep_fn: Optional sleep function for tests. Defaults to ``time.sleep``.
        time_fn: Optional time function for tests. Defaults to ``time.time``.
    """

    def __init__(
        self,
        facade: MicroscopeFacade,
        targets: TargetList,
        params: TargetTimelapseParams,
        acquisition_callback: Callable[[Any, int], Any],
        *,
        workflow_name: str = "TargetTimelapse",
        mode_adapter: Optional["SmartModeWorkflowAdapter"] = None,
        mode_config: Optional[ModeConfig] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
        time_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        if params.n_timepoints < 1:
            raise ValueError(f"n_timepoints must be >= 1, got {params.n_timepoints}")
        if params.interval_s < 0:
            raise ValueError(f"interval_s must be >= 0, got {params.interval_s}")
        if params.settle_s < 0:
            raise ValueError(f"settle_s must be >= 0, got {params.settle_s}")
        if params.failure_policy not in ("continue", "abort"):
            raise ValueError(
                f"failure_policy must be 'continue' or 'abort', got {params.failure_policy!r}"
            )

        if (mode_adapter is None) != (mode_config is None):
            raise ValueError(
                "mode_adapter and mode_config must both be supplied or both be None"
            )

        self.facade = facade
        self.targets = targets
        self.params = params
        self.acquisition_callback = acquisition_callback
        self.workflow_name = workflow_name
        self.mode_adapter = mode_adapter
        self.mode_config = mode_config
        self._sleep = sleep_fn if sleep_fn is not None else time.sleep
        self._time = time_fn if time_fn is not None else time.time

    def run(self) -> TargetTimelapseResult:
        """Execute the full target timelapse workflow.

        Returns:
            TargetTimelapseResult with per-acquisition metadata and completion status.
        """
        result = TargetTimelapseResult()
        enabled = self.targets.enabled_targets()

        if not enabled:
            logger.warning("No enabled targets - nothing to acquire")
            return result

        if self.mode_adapter and self.mode_config:
            if not self._preflight_modes(result):
                logger.error("Mode preflight failed - aborting before stage motion")
                result.mode_failure = True
                self._apply_idle_if_configured(result)
                return result

        logger.info(
            "Starting target timelapse: %d enabled targets, %d timepoints, %.2f s interval",
            len(enabled),
            self.params.n_timepoints,
            self.params.interval_s,
        )

        try:
            for tp_idx in range(self.params.n_timepoints):
                cycle_start = self._time()

                logger.info("Timepoint %d/%d", tp_idx + 1, self.params.n_timepoints)

                cycle_aborted = self._run_cycle(tp_idx, enabled, result)
                if cycle_aborted:
                    result.aborted = True
                    logger.warning("Workflow aborted at timepoint %d due to failure", tp_idx)
                    break

                result.total_timepoints_completed += 1

                if tp_idx < self.params.n_timepoints - 1:
                    self._wait_for_next_cycle(cycle_start)

            logger.info(
                "Target timelapse complete: %d acquisitions, %d/%d timepoints completed%s",
                len(result.acquisitions),
                result.total_timepoints_completed,
                self.params.n_timepoints,
                " (ABORTED)" if result.aborted else "",
            )
        finally:
            self._apply_idle_if_configured(result)

        return result

    def _run_cycle(
        self,
        tp_idx: int,
        enabled_targets: list["Target"],
        result: TargetTimelapseResult,
    ) -> bool:
        """Run one cycle visiting all enabled targets.

        Returns:
            True if cycle was aborted early, False if completed.
        """
        for target in enabled_targets:
            acq_result = self._acquire_target(target, tp_idx)
            result.acquisitions.append(acq_result)

            if not acq_result.success:
                if acq_result.mode_failure:
                    result.mode_failure = True
                    return True
                if self.params.failure_policy == "abort":
                    return True

        return False

    def _acquire_target(self, target: "Target", timepoint_idx: int) -> TargetAcquisitionResult:
        """Apply event role, move to target, call callback, and optionally resume.

        Args:
            target: Target instance from TargetList.
            timepoint_idx: Zero-based timepoint index.

        Returns:
            TargetAcquisitionResult with metadata.
        """
        started = self._time()
        mode_transitions = []

        if self.mode_adapter and self.mode_config:
            event_result = self.mode_adapter.applyRole(
                self.workflow_name, self.mode_config.event_role, required=True
            )
            mode_transitions.extend(self.mode_adapter.getTransitions()[-1:])
            if not event_result.ok:
                error_msg = f"Event role '{self.mode_config.event_role}' failed to apply"
                logger.error(
                    "Target %d [%s] timepoint %d: %s - aborting before stage motion",
                    target.id,
                    target.source,
                    timepoint_idx,
                    error_msg,
                )
                actual_xy = self.facade.stage_con.get_position()
                actual_xy = (float(actual_xy[0]), float(actual_xy[1]))
                completed = self._time()
                return TargetAcquisitionResult(
                    target_id=target.id,
                    timepoint=timepoint_idx,
                    stage_xy=actual_xy,
                    started_s=started,
                    completed_s=completed,
                    success=False,
                    callback_result=None,
                    error=error_msg,
                    mode_transitions=mode_transitions,
                    mode_failure=True,
                )

        self.facade.stage_con.move_to(*target.stage_xy)

        if self.params.settle_s > 0:
            self._sleep(self.params.settle_s)

        actual_xy = self.facade.stage_con.get_position()
        actual_xy = (float(actual_xy[0]), float(actual_xy[1]))

        success = False
        callback_result = None
        error_msg = None

        try:
            callback_result = self.acquisition_callback(target, timepoint_idx)
            success = True
            logger.info(
                "Target %d [%s] timepoint %d: success at (%.2f, %.2f)",
                target.id,
                target.source,
                timepoint_idx,
                actual_xy[0],
                actual_xy[1],
            )
        except Exception as exc:
            error_msg = str(exc)
            logger.warning(
                "Target %d [%s] timepoint %d: FAILED - %s",
                target.id,
                target.source,
                timepoint_idx,
                error_msg,
            )

        if self.mode_adapter and self.mode_config and self.mode_config.resume_role:
            self.mode_adapter.applyRole(
                self.workflow_name, self.mode_config.resume_role, required=False
            )
            mode_transitions.extend(self.mode_adapter.getTransitions()[-1:])

        completed = self._time()

        return TargetAcquisitionResult(
            target_id=target.id,
            timepoint=timepoint_idx,
            stage_xy=actual_xy,
            started_s=started,
            completed_s=completed,
            success=success,
            callback_result=callback_result,
            error=error_msg,
            mode_transitions=mode_transitions,
        )

    def _wait_for_next_cycle(self, cycle_start: float) -> None:
        """Wait until the next cycle should start based on interval_s.

        Args:
            cycle_start: Timestamp (from time_fn) when the cycle started.
        """
        if self.params.interval_s <= 0:
            return

        elapsed = self._time() - cycle_start
        remaining = self.params.interval_s - elapsed

        if remaining > 0:
            logger.debug("Waiting %.2f s for next cycle", remaining)
            self._sleep(remaining)
        else:
            logger.debug(
                "Cycle took %.2f s (interval %.2f s) - proceeding immediately",
                elapsed,
                self.params.interval_s,
            )

    def _preflight_modes(self, result: TargetTimelapseResult) -> bool:
        """Preflight required mode roles before starting workflow.

        Args:
            result: TargetTimelapseResult to record preflight status.

        Returns:
            True if preflight passed, False otherwise.
        """
        if not self.mode_adapter or not self.mode_config:
            return True
        if not self.mode_config.preflight_roles:
            return True

        preflight_result = self.mode_adapter.preflight(
            self.workflow_name, self.mode_config.preflight_roles
        )
        result.preflight_passed = preflight_result.ok
        result.preflight_messages = list(preflight_result.messages)

        if not preflight_result.ok:
            logger.error(
                "Mode preflight failed for workflow '%s': %s",
                self.workflow_name,
                "; ".join(preflight_result.messages),
            )

        return preflight_result.ok

    def _apply_idle_if_configured(self, result: TargetTimelapseResult) -> None:
        """Apply idle role if configured, recording the transition.

        Args:
            result: TargetTimelapseResult to record idle transition.
        """
        if not self.mode_adapter or not self.mode_config:
            return

        if self.mode_config.idle_role:
            self.mode_adapter.applyIdleFallback(
                self.workflow_name,
                self.mode_config.idle_role,
            )
            transitions = self.mode_adapter.getTransitions()
            if transitions:
                result.idle_transition = transitions[-1]
