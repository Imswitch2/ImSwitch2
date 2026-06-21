"""Shared smart-microscopy role-switching glue for event-triggered controllers.

Event-triggered workflows (EtSnouty, EtSTED, EtMonalisa, ...) all need the same
small amount of glue around :class:`SmartMicroscopyModeService`:

  * decide whether smart-mode switching is enabled for this workflow (a
    per-workflow rollout flag on ``setupInfo``);
  * preflight the configured role modes before arming;
  * apply a runtime role (``scouting``/``event``/``resume``/``idle``) and turn
    the structured :class:`ApplyResult` into a pass/fail decision the caller can
    branch on, logging mode name / warnings / failed hardware components on the
    way.

That glue is workflow-agnostic: it only needs the workflow name and the set of
required roles, both declared as class attributes. :class:`SmartModeRoleMixin`
hoists it out of the individual controllers so EtSnouty (still a standalone
controller) and the ``EventTriggeredControllerBase`` subclasses reach the *same*
service by the *same* method calls (Design Principle 1).

The mixin relies on three members that every host controller provides:

  * ``self._setupInfo`` — carries ``smartMicroscopyModeSwitchingEnabled``;
  * ``self._logger`` — for human-readable logging;
  * ``self.setDetLogLine(key, val, *args)`` — for the per-event detector log.
"""


class SmartModeRoleMixin:
    """Workflow-agnostic smart-microscopy role-switching glue.

    Subclasses set :attr:`SMART_MODE_WORKFLOW` (the workflow name used to look
    up role modes and the rollout flag) and :attr:`SMART_MODE_REQUIRED_ROLES`
    (roles that must be mapped before an experiment may arm). When
    :attr:`SMART_MODE_WORKFLOW` is ``None`` the mixin is inert -- every guard
    returns "disabled" so the host controller's behavior is unchanged.
    """

    #: Workflow name this controller switches modes for, or ``None`` to disable.
    SMART_MODE_WORKFLOW = None
    #: Roles that must be mapped before arming (configuration error otherwise).
    SMART_MODE_REQUIRED_ROLES = ()

    #: Smart-mode service handle; injected via :meth:`setSmartModeService`. Set
    #: as a class attribute so the mixin is safe even when a host controller has
    #: not initialized it in ``__init__`` yet.
    _smartModeService = None

    def setSmartModeService(self, smartModeService) -> None:
        """Inject the :class:`SmartMicroscopyModeService` handle."""
        self._smartModeService = smartModeService

    # ── Enablement ──────────────────────────────────────────────────────── #

    def _smartModeSwitchingEnabled(self) -> bool:
        """Whether smart-mode switching is active for this workflow.

        True only when this controller declares a workflow, a service has been
        injected, and ``setupInfo.smartMicroscopyModeSwitchingEnabled`` is
        truthy for the workflow.
        """
        if self.SMART_MODE_WORKFLOW is None:
            return False
        if self._smartModeService is None:
            return False
        enabledByWorkflow = (
            getattr(self._setupInfo, 'smartMicroscopyModeSwitchingEnabled', None) or {}
        )
        return bool(enabledByWorkflow.get(self.SMART_MODE_WORKFLOW, False))

    # ── Preflight ───────────────────────────────────────────────────────── #

    def _preflightSmartModeRoles(self, roles=None) -> bool:
        """Preflight the configured role modes before arming.

        When disabled this is a no-op returning ``True``. Otherwise it resolves
        the roles to preflight (the required roles plus any configured optional
        ``resume``/``idle`` roles when ``roles`` is ``None``), runs the service
        preflight, logs every message via ``setDetLogLine`` and ``self._logger``,
        and returns ``result.ok``. A missing service or an unmapped required
        role records a problem and returns ``False`` before the service is even
        consulted.
        """
        if not self._smartModeSwitchingEnabled():
            return True

        if self._smartModeService is None:
            self._recordSmartModeProblem(
                'service_missing',
                'Smart microscopy mode service is unavailable.',
            )
            return False

        missingRequiredRoles = [
            role for role in self.SMART_MODE_REQUIRED_ROLES
            if self._smartModeService.resolveMode(self.SMART_MODE_WORKFLOW, role) is None
        ]
        if missingRequiredRoles:
            self._recordSmartModeProblem(
                'missing_required_roles',
                f'Missing required smart microscopy roles: {", ".join(missingRequiredRoles)}.',
            )
            return False

        if roles is None:
            roles = list(self.SMART_MODE_REQUIRED_ROLES)
            for optionalRole in ('resume', 'idle'):
                if (
                    self._smartModeService.resolveMode(
                        self.SMART_MODE_WORKFLOW, optionalRole
                    )
                    is not None
                ):
                    roles.append(optionalRole)
        else:
            roles = list(roles)

        result = self._smartModeService.preflight(self.SMART_MODE_WORKFLOW, roles=roles)
        for i, message in enumerate(result.messages):
            self.setDetLogLine('smart_mode_preflight_message_', message, i)
            self._logger.warning(f'Smart microscopy preflight: {message}')

        if not result.ok:
            self._recordSmartModeProblem(
                'preflight_failed',
                f'Smart microscopy mode preflight failed for roles: {", ".join(roles)}.',
            )
            return False

        return True

    # ── Role application ────────────────────────────────────────────────── #

    def _applySmartModeRole(self, role, *, required) -> bool:
        """Apply a single runtime role and turn the result into pass/fail.

        Returns ``False`` (recording a problem) when the service is missing,
        ``applyRole`` raises, the role is ``required`` but unmapped, or the apply
        did not cleanly touch hardware (``result.ok`` is ``False`` -- the failed
        hardware components are named in the recorded problem). A non-required,
        unmapped role logs its warning and returns ``True``.
        """
        if self._smartModeService is None:
            self._recordSmartModeProblem(
                role, 'Smart microscopy mode service is unavailable.'
            )
            return False

        try:
            result = self._smartModeService.applyRole(self.SMART_MODE_WORKFLOW, role)
        except Exception as error:
            self._recordSmartModeProblem(
                role,
                f'Failed to apply smart microscopy role "{role}": {error}',
            )
            return False

        modeName = result.modeName or ''
        self.setDetLogLine(f'smart_mode_{role}', modeName)
        self.setDetLogLine(f'smart_mode_{role}_applied', result.applied)
        self.setDetLogLine(f'smart_mode_{role}_ok', result.ok)
        for i, warning in enumerate(result.warnings):
            self.setDetLogLine(f'smart_mode_{role}_warning_', warning, i)
            self._logger.warning(f'Smart microscopy role "{role}" warning: {warning}')

        if required and result.modeName is None:
            self._recordSmartModeProblem(
                role,
                f'Required smart microscopy role "{role}" is not configured.',
            )
            return False

        if not result.ok:
            failed = ', '.join(result.failedComponents) or 'unknown component'
            self._recordSmartModeProblem(
                role,
                f'Smart microscopy role "{role}" failed to apply hardware component(s): {failed}.',
            )
            return False

        self._logger.info(
            f'Smart microscopy role "{role}" applied via setup mode "{modeName}".'
        )
        return True

    def _resumeSmartModeRole(self) -> str:
        """Role to apply when resuming detection after a slow scan.

        Prefers an explicit ``resume`` mode when one is configured for this
        workflow, otherwise falls back to ``scouting``.
        """
        if (
            self._smartModeSwitchingEnabled()
            and self._smartModeService is not None
            and self._smartModeService.resolveMode(self.SMART_MODE_WORKFLOW, 'resume')
            is not None
        ):
            return 'resume'
        return 'scouting'

    def _applySmartModeRoleIfConfigured(self, role) -> bool:
        """Apply an optional role, no-opping when disabled or unconfigured.

        Returns ``True`` without touching hardware when smart-mode switching is
        disabled, no service is set, or the role is unmapped for this workflow;
        otherwise applies the role as a non-required best-effort transition.
        """
        if not self._smartModeSwitchingEnabled() or self._smartModeService is None:
            return True
        if self._smartModeService.resolveMode(self.SMART_MODE_WORKFLOW, role) is None:
            return True
        return self._applySmartModeRole(role, required=False)

    # ── Problem recording ───────────────────────────────────────────────── #

    def _recordSmartModeProblem(self, key, message) -> None:
        """Log a smart-mode problem and stamp it into the detector log."""
        self._logger.error(message)
        self.setDetLogLine(f'smart_mode_{key}_error', message)


# Copyright (C) 2026 ImSwitch developers
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
