import enum

from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.workflows.smart_mode_workflow import (
    ApplyResult,
    PreflightResult,
)

from .basecontrollers import ComponentStateApplyMode


class SmartModeHazardPolicy(enum.Enum):
    """Non-interactive hazard policy applied at preflight time.

    The interactive ``SetupModesController`` resolves hazards with a modal
    dialog; the non-interactive service cannot, so each workflow declares one of
    these policies instead (see plan §2):

    - ``ALLOW``: never block on hazards or missing modes.
    - ``WARN_ONLY``: surface hazards/missing modes but never block.
    - ``BLOCK_ON_HAZARD`` (default): block arming if any hazard or missing role
      mode is present.
    """

    ALLOW = 'allow'
    WARN_ONLY = 'warnOnly'
    BLOCK_ON_HAZARD = 'blockOnHazard'


class SmartMicroscopyModeService:
    """Maps runtime roles to setup modes and applies them through the backend.

    A plain backend object (not an ImConWidgetController and not a Qt widget)
    that sits next to :class:`SetupModeController`. Event-triggered workflows ask
    it to apply a runtime *role* (``scouting``, ``event``, ``resume``, ``idle``,
    ``validation``) without knowing which mirrors, lasers, stand modes, SLMs,
    cameras, or detector settings are involved.

    Role application is a synchronous call that returns an :class:`ApplyResult`;
    it never emits a Qt signal, so callers can make safety decisions inline
    (Design Principle 2). The service applies modes by calling
    :meth:`SetupModeController.applySetupMode` directly and must not route through
    the interactive ``SetupModesController`` (Design Principle 3). It holds no
    widget and never opens a dialog.
    """

    def __init__(self, setupModeController, roleConfig, policyConfig=None,
                 laserPowerThresholdMw=50.0, logger=None):
        """Construct the service.

        Args:
            setupModeController: The backend :class:`SetupModeController`. The
                service uses its ``listSetupModes``, ``applySetupMode``,
                ``getSetupMode``, ``getModeHazards``, and
                ``getLastAppliedModeName`` primitives.
            roleConfig: The parsed ``smartMicroscopyModes`` mapping of
                ``workflowName -> {role: setupModeName}``. ``None`` is treated as
                an empty configuration.
            policyConfig: The parsed ``smartMicroscopyModePolicies`` mapping of
                ``workflowName -> policyName``. ``None`` is treated as an empty
                configuration. Unknown or absent policy names default to
                :attr:`SmartModeHazardPolicy.BLOCK_ON_HAZARD`.
            laserPowerThresholdMw: High-power laser threshold (mW) passed to
                ``getModeHazards`` during preflight. Defaults to ``50.0``.
            logger: Optional logger. If omitted, one is created.
        """
        self._setupModeController = setupModeController
        self._roleConfig = roleConfig or {}
        self._policyConfig = policyConfig or {}
        self._laserPowerThresholdMw = laserPowerThresholdMw
        self._logger = logger if logger is not None else initLogger(self)

    def updateConfig(self, roleConfig, policyConfig=None):
        """Replace role and policy mappings used by future role operations."""
        self._roleConfig = roleConfig or {}
        self._policyConfig = policyConfig or {}

    def resolveMode(self, workflowName, role):
        """Resolve ``(workflowName, role)`` to a configured setup mode name.

        Returns:
            The configured setup mode name, or ``None`` if the workflow or role
            is not configured.
        """
        workflowModes = self._roleConfig.get(workflowName)
        if not workflowModes:
            return None
        return workflowModes.get(role)

    def resolvePolicy(self, workflowName):
        """Resolve a workflow's non-interactive hazard policy.

        Unknown or absent workflow policies default to
        :attr:`SmartModeHazardPolicy.BLOCK_ON_HAZARD` -- the safest choice.

        Returns:
            SmartModeHazardPolicy: The policy for ``workflowName``.
        """
        policyName = self._policyConfig.get(workflowName)
        if policyName is None:
            return SmartModeHazardPolicy.BLOCK_ON_HAZARD
        try:
            return SmartModeHazardPolicy(policyName)
        except ValueError:
            self._logger.warning(
                f'Unknown smart microscopy hazard policy "{policyName}" for workflow '
                f'"{workflowName}"; defaulting to "{SmartModeHazardPolicy.BLOCK_ON_HAZARD.value}".'
            )
            return SmartModeHazardPolicy.BLOCK_ON_HAZARD

    def validate(self):
        """Validate that every configured role mode exists in the backend.

        Returns:
            list[str]: One human-readable problem per configured role whose mode
            name is not present in ``setupModeController.listSetupModes()``. An
            empty list means every configured role resolves to an existing mode.
            A controller calls this before arming an experiment.
        """
        problems = []
        try:
            availableModes = set(self._setupModeController.listSetupModes())
        except Exception as error:
            self._logger.error(f'Failed to list setup modes during validation: {error}')
            return [f'Could not list available setup modes: {error}']

        for workflowName, workflowModes in self._roleConfig.items():
            if not workflowModes:
                continue
            for role, modeName in workflowModes.items():
                if modeName not in availableModes:
                    problems.append(
                        f'Smart microscopy workflow "{workflowName}" role "{role}" '
                        f'points at setup mode "{modeName}", which does not exist.'
                    )

        return problems

    def applyRole(self, workflowName, role):
        """Apply the setup mode configured for ``(workflowName, role)``.

        Resolves the role to a setup mode name and applies it through
        ``SetupModeController.applySetupMode`` directly, returning an
        :class:`ApplyResult` synchronously. Behavior:

        - If the role is not mapped, no mode is applied and the result carries a
          single warning with ``ok=True`` and ``applied=False``. A missing
          mapping is a configuration issue, not a hardware failure: it is
          surfaced for logging but does not by itself mark the result not ok.
        - If the resolved mode equals the backend's most recently cleanly
          applied mode (``getLastAppliedModeName``), this is a no-op with
          ``applied=False``, ``ok=True`` (Design Principle 4). De-dup reads the
          backend getter so a clean apply done by the interactive
          ``SetupModesController`` is also reflected.
        - Otherwise the mode is applied: ``failedComponents`` is the set of
          *hardware* components that did not apply cleanly, ``ok`` is ``True``
          iff that set is empty, ``warnings`` are the backend's warnings, and
          ``applied=True``.

        Returns:
            ApplyResult: The structured outcome of the role application.
        """
        modeName = self.resolveMode(workflowName, role)
        if modeName is None:
            warning = (
                f'No setup mode configured for smart microscopy workflow '
                f'"{workflowName}" role "{role}".'
            )
            self._logger.warning(warning)
            return ApplyResult(
                applied=False, ok=True, modeName=None,
                warnings=[warning], failedComponents=[],
            )

        if modeName == self._setupModeController.getLastAppliedModeName():
            self._logger.debug(
                f'Setup mode "{modeName}" was already cleanly applied; skipping '
                f'reapplication for workflow "{workflowName}" role "{role}".'
            )
            return ApplyResult(
                applied=False, ok=True, modeName=modeName,
                warnings=[], failedComponents=[],
            )

        self._logger.info(
            f'Applying setup mode "{modeName}" for workflow "{workflowName}" '
            f'role "{role}".'
        )
        outcome = self._setupModeController.applySetupMode(modeName)
        warnings = list(outcome.warnings) if outcome.warnings else []
        failedComponents = self._hardwareFailedComponents(outcome)
        ok = not failedComponents
        if not ok:
            self._logger.error(
                f'Setup mode "{modeName}" did not cleanly apply hardware components '
                f'{failedComponents} for workflow "{workflowName}" role "{role}".'
            )
        return ApplyResult(
            applied=True, ok=ok, modeName=modeName,
            warnings=warnings, failedComponents=failedComponents,
        )

    def _hardwareFailedComponents(self, outcome):
        failedComponents = []
        for componentName in (
            list(getattr(outcome, 'failedComponents', []) or [])
            + list(getattr(outcome, 'warningComponents', []) or [])
        ):
            if (
                self._isHardwareCriticalComponent(componentName)
                and componentName not in failedComponents
            ):
                failedComponents.append(componentName)
        return failedComponents

    def _isHardwareCriticalComponent(self, componentName):
        try:
            return self._setupModeController.isSetupModeHardwareCritical(componentName)
        except AttributeError:
            self._logger.warning(
                'SetupModeController does not expose hardware-critical component '
                'metadata; treating "%s" as non-critical.',
                componentName,
            )
        except Exception as error:
            self._logger.warning(
                'Failed to read hardware-critical metadata for "%s": %s',
                componentName,
                error,
            )
        return False

    def preflight(self, workflowName, roles=None):
        """Non-interactive hazard preflight before arming (plan §2).

        Resolves the workflow's role modes (all configured roles when ``roles``
        is ``None``), checks each resolved mode exists, aggregates hazards from
        ``SetupModeController.getModeHazards`` for the modes that do exist, and
        applies the workflow's policy:

        - ``ALLOW`` / ``WARN_ONLY``: ``ok=True`` regardless of findings.
        - ``BLOCK_ON_HAZARD`` (default): ``ok=False`` if any hazard or missing
          mode is found, else ``ok=True``.

        This never touches hardware and never opens a dialog.

        Args:
            workflowName: Workflow whose role modes to preflight.
            roles: Optional iterable of role names to preflight. Defaults to all
                configured roles for the workflow.

        Returns:
            PreflightResult: ``ok``, ``hazards``, ``missingModes``,
            ``failedModes``, ``messages``.
        """
        if roles is None:
            workflowModes = self._roleConfig.get(workflowName) or {}
            roles = list(workflowModes.keys())

        try:
            availableModes = set(self._setupModeController.listSetupModes())
        except Exception as error:
            self._logger.error(f'Failed to list setup modes during preflight: {error}')
            message = f'Could not list available setup modes: {error}'
            return PreflightResult(
                ok=False, hazards=[], missingModes=[], failedModes=[], messages=[message],
            )

        hazards = []
        missingModes = []
        failedModes = []
        messages = []
        # Track resolved names to avoid duplicate hazard aggregation when several
        # roles point at the same mode (one hardware mode is one hazard set).
        checkedModeNames = set()

        for role in roles:
            modeName = self.resolveMode(workflowName, role)
            if modeName is None:
                messages.append(
                    f'No setup mode configured for workflow "{workflowName}" role "{role}".'
                )
                continue

            if modeName not in availableModes:
                if modeName not in missingModes:
                    missingModes.append(modeName)
                    messages.append(
                        f'Role "{role}" points at setup mode "{modeName}", which does not exist.'
                    )
                continue

            if modeName in checkedModeNames:
                continue
            checkedModeNames.add(modeName)

            try:
                mode = self._setupModeController.getSetupMode(modeName)
            except Exception as error:
                self._logger.error(f'Failed to load setup mode "{modeName}" during preflight: {error}')
                if modeName not in failedModes:
                    failedModes.append(modeName)
                messages.append(f'Could not load setup mode "{modeName}": {error}')
                continue

            state = mode.get('state', {}) if isinstance(mode, dict) else {}
            try:
                modeHazards = self._setupModeController.getModeHazards(
                    state,
                    ComponentStateApplyMode.SETUP_MODE_APPLY,
                    {
                        'laserPowerThresholdMw': self._laserPowerThresholdMw,
                        'applySource': 'smart_microscopy',
                    },
                )
            except Exception as error:
                self._logger.error(
                    f'Failed to inspect setup mode "{modeName}" hazards during preflight: {error}'
                )
                if modeName not in failedModes:
                    failedModes.append(modeName)
                messages.append(f'Could not inspect setup mode "{modeName}" hazards: {error}')
                continue

            for hazard in modeHazards:
                hazards.append(hazard)
                message = hazard.get('message') if isinstance(hazard, dict) else None
                if message:
                    messages.append(f'Mode "{modeName}": {message}')
                else:
                    messages.append(f'Mode "{modeName}" has a hazard: {hazard}')

        policy = self.resolvePolicy(workflowName)
        if policy in (SmartModeHazardPolicy.ALLOW, SmartModeHazardPolicy.WARN_ONLY):
            ok = True
        else:  # BLOCK_ON_HAZARD
            ok = not (hazards or missingModes or failedModes)

        return PreflightResult(
            ok=ok, hazards=hazards, missingModes=missingModes,
            failedModes=failedModes, messages=messages,
        )


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
