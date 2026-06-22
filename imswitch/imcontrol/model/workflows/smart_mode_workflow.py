"""Workflow-facing adapter for SmartMicroscopyModeService.

Provides a model-level facade around SmartMicroscopyModeService for use in
automated workflows. Delegates preflight and role application, records
transition metadata, and provides safe idle fallback without hardcoding setup
mode names or importing GUI widgets.

Phase T3 of the tiled-target-timelapse-smart-events plan.

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
from typing import Callable, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# Runtime roles a setup mode may declare for a smart-microscopy workflow. These
# are model-level workflow roles, not UI/controller concepts.
SMART_MICROSCOPY_ROLES = frozenset(
    {'scouting', 'event', 'resume', 'idle', 'validation'}
)
SMART_MICROSCOPY_POLICIES = frozenset({'allow', 'warnOnly', 'blockOnHazard'})


@dataclass(frozen=True)
class ApplyResult:
    """Structured result of applying a workflow mode role.

    The camelCase field names match the existing smart-mode service contract and
    are intentionally kept stable for controller and workflow callers.
    """

    applied: bool
    ok: bool
    modeName: Optional[str]
    warnings: list[str] = field(default_factory=list)
    failedComponents: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PreflightResult:
    """Structured result of non-interactive mode-role preflight."""

    ok: bool
    hazards: list[dict] = field(default_factory=list)
    missingModes: list[str] = field(default_factory=list)
    failedModes: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


@runtime_checkable
class ModeRoleApplier(Protocol):
    """Model-layer protocol for objects that apply smart-microscopy roles."""

    def resolveMode(self, workflowName: str, role: str) -> Optional[str]:
        """Resolve a workflow role to a setup mode name, or None when unmapped."""
        ...

    def preflight(
        self, workflowName: str, roles: Optional[list[str]] = None
    ) -> PreflightResult:
        """Preflight one workflow's configured mode roles."""
        ...

    def applyRole(self, workflowName: str, role: str) -> ApplyResult:
        """Apply one workflow role and return the structured result."""
        ...


def validate_smart_mode_config(config, available_modes):
    """Validate workflow role mappings against available setup mode names."""
    problems = []
    available_mode_set = set(available_modes)
    modes = config.get('modes', {})

    for workflow_name, workflow_modes in modes.items():
        for role, mode_name in workflow_modes.items():
            if mode_name and mode_name not in available_mode_set:
                problems.append(
                    f'Workflow "{workflow_name}" role "{role}" points at setup mode '
                    f'"{mode_name}", which does not exist.'
                )

    return problems


def normalize_smart_mode_role_config(modes):
    """Normalize role-to-mode mappings loaded from setup smart-mode config."""
    normalized = {}
    for workflow_name, workflow_modes in (modes or {}).items():
        workflow_name = str(workflow_name).strip()
        if not workflow_name or not isinstance(workflow_modes, dict):
            continue

        normalized_roles = {}
        for role, mode_name in workflow_modes.items():
            role = str(role).strip()
            mode_name = str(mode_name).strip() if mode_name is not None else ''
            if role in SMART_MICROSCOPY_ROLES and mode_name:
                normalized_roles[role] = mode_name
        if normalized_roles:
            normalized[workflow_name] = normalized_roles
    return normalized


def normalize_smart_mode_policy_config(policies):
    """Normalize workflow hazard policies, omitting default block-on-hazard."""
    normalized = {}
    for workflow_name, policy_name in (policies or {}).items():
        workflow_name = str(workflow_name).strip()
        policy_name = str(policy_name).strip() if policy_name is not None else ''
        if (
            workflow_name
            and policy_name in SMART_MICROSCOPY_POLICIES
            and policy_name != 'blockOnHazard'
        ):
            normalized[workflow_name] = policy_name
    return normalized


def normalize_smart_mode_enabled_config(enabled):
    """Normalize workflow smart-mode enablement flags."""
    normalized = {}
    for workflow_name, is_enabled in (enabled or {}).items():
        workflow_name = str(workflow_name).strip()
        if workflow_name and bool(is_enabled):
            normalized[workflow_name] = True
    return normalized


@dataclass
class ModeTransition:
    """Metadata for one mode role transition.

    Attributes:
        workflow_name: Name of the workflow requesting the transition.
        role: Requested role (e.g., 'scouting', 'event', 'resume', 'idle').
        timestamp_s: Transition timestamp (time.time()).
        applied: Whether a mode was actually applied.
        ok: Whether the transition succeeded without hardware failures.
        mode_name: Resolved setup mode name, or None if unmapped.
        warnings: Warning messages from the mode service.
        failed_components: Hardware components that failed during apply.
    """

    workflow_name: str
    role: str
    timestamp_s: float
    applied: bool
    ok: bool
    mode_name: Optional[str]
    warnings: list[str] = field(default_factory=list)
    failed_components: list[str] = field(default_factory=list)


class SmartModeWorkflowAdapter:
    """Workflow-facing adapter for SmartMicroscopyModeService.

    Wraps SmartMicroscopyModeService to provide a simple, workflow-safe interface
    for mode transitions. Does not import GUI widgets and does not hardcode setup
    mode names. Records all transitions for workflow metadata and provides safe
    idle fallback on failures.

    Args:
        mode_service: SmartMicroscopyModeService instance to delegate to.
        logger_instance: Optional logger. If omitted, one is created.
    """

    def __init__(
        self,
        mode_service: ModeRoleApplier,
        logger_instance: Optional[logging.Logger] = None,
        time_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self._mode_service = mode_service
        self._logger = logger_instance if logger_instance is not None else logger
        self._time = time_fn if time_fn is not None else time.time
        self._transitions: list[ModeTransition] = []

    def preflight(
        self, workflow_name: str, roles: Optional[list[str]] = None
    ) -> PreflightResult:
        """Preflight workflow roles before arming.

        Delegates to SmartMicroscopyModeService.preflight(). Does not touch
        hardware and does not open dialogs.

        Args:
            workflow_name: Workflow whose role modes to preflight.
            roles: Optional list of role names to preflight. Defaults to all
                configured roles for the workflow.

        Returns:
            PreflightResult with ok, hazards, missingModes, failedModes, messages.
        """
        self._logger.info(
            "Preflighting workflow '%s' roles: %s", workflow_name, roles or "all"
        )
        result = self._mode_service.preflight(workflow_name, roles)

        if not result.ok:
            self._logger.error(
                "Preflight failed for workflow '%s': %s",
                workflow_name,
                "; ".join(result.messages),
            )
        else:
            self._logger.info("Preflight passed for workflow '%s'", workflow_name)

        return result

    def applyRole(
        self, workflow_name: str, role: str, required: bool = True
    ) -> ApplyResult:
        """Apply a workflow role through the mode service.

        Delegates to SmartMicroscopyModeService.applyRole() and records the
        transition. If the role is required and the apply fails, logs an error.

        Args:
            workflow_name: Workflow requesting the role.
            role: Role name (e.g., 'scouting', 'event', 'resume', 'idle').
            required: If True, log an error when apply fails. If False, log a
                warning. Defaults to True.

        Returns:
            ApplyResult with applied, ok, modeName, warnings, failedComponents.
        """
        self._logger.info("Applying role '%s' for workflow '%s'", role, workflow_name)

        result = self._mode_service.applyRole(workflow_name, role)
        transition = ModeTransition(
            workflow_name=workflow_name,
            role=role,
            timestamp_s=self._time(),
            applied=result.applied,
            ok=result.ok,
            mode_name=result.modeName,
            warnings=list(result.warnings) if result.warnings else [],
            failed_components=list(result.failedComponents)
            if result.failedComponents
            else [],
        )
        self._transitions.append(transition)

        if not result.ok:
            log_fn = self._logger.error if required else self._logger.warning
            log_fn(
                "Role '%s' for workflow '%s' did not apply cleanly: %s",
                role,
                workflow_name,
                "; ".join(result.warnings) if result.warnings else "unknown error",
            )
        elif result.applied:
            self._logger.info(
                "Role '%s' applied mode '%s' for workflow '%s'",
                role,
                result.modeName,
                workflow_name,
            )
        else:
            self._logger.debug(
                "Role '%s' for workflow '%s' was a no-op or unmapped",
                role,
                workflow_name,
            )

        return result

    def applyIdleFallback(
        self,
        workflow_name: str,
        role: str = "idle",
    ) -> ApplyResult:
        """Apply idle role as a safe fallback after failure or completion.

        Convenience method that applies the configured idle role with
        required=False, so failures are logged as warnings rather than errors.

        Args:
            workflow_name: Workflow requesting the idle fallback.
            role: Role to apply for the fallback. Defaults to ``"idle"`` for
                callers that do not need a custom role name.

        Returns:
            ApplyResult from the idle role application.
        """
        self._logger.info(
            "Applying idle fallback role '%s' for workflow '%s'",
            role,
            workflow_name,
        )
        return self.applyRole(workflow_name, role, required=False)

    def getTransitions(self) -> list[ModeTransition]:
        """Get all recorded mode transitions in chronological order.

        Returns:
            List of ModeTransition records.
        """
        return list(self._transitions)

    def clearTransitions(self) -> None:
        """Clear recorded transition history."""
        self._transitions.clear()
