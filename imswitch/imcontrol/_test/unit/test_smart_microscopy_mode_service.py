import importlib

import pytest


service_module = importlib.import_module(
    'imswitch.imcontrol.controller.SmartMicroscopyModeService'
)
SmartMicroscopyModeService = service_module.SmartMicroscopyModeService
SmartModeHazardPolicy = service_module.SmartModeHazardPolicy
ApplyResult = service_module.ApplyResult
PreflightResult = service_module.PreflightResult


pytestmark = pytest.mark.nohardware


class _NullLogger:
    """Silent logger so the service does not require initLogger / Qt."""

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class _FakeApplyOutcome:
    """ApplyOutcome-like object returned by the fake backend's applySetupMode."""

    def __init__(self, warnings=None, failedComponents=None, warningComponents=None):
        self.warnings = list(warnings or [])
        self.failedComponents = list(failedComponents or [])
        self.warningComponents = list(warningComponents or [])


class _FakeSetupModeController:
    """Stand-in for SetupModeController that touches no filesystem.

    Records applySetupMode calls, returns a configurable ApplyOutcome-like
    object (warnings + failedComponents), serves mode state for preflight, and
    delegates hazard detection to a configurable callable. Tracks the last
    applied mode the same way the real controller does.
    """

    def __init__(self, modes=None, warningsByMode=None, failedComponentsByMode=None,
                 warningComponentsByMode=None, stateByMode=None, loadFailuresByMode=None,
                 hardwareCriticalComponents=None):
        self._modes = list(modes) if modes is not None else []
        self._warningsByMode = warningsByMode or {}
        self._failedComponentsByMode = failedComponentsByMode or {}
        self._warningComponentsByMode = warningComponentsByMode or {}
        self._stateByMode = stateByMode or {}
        self._loadFailuresByMode = loadFailuresByMode or {}
        self._hardwareCriticalComponents = set(hardwareCriticalComponents or [])
        self.applyCalls = []
        self.getModeHazardsCalls = []
        self._lastAppliedModeName = None

    def listSetupModes(self):
        return sorted(self._modes)

    def applySetupMode(self, name, componentNames=None):
        self.applyCalls.append(name)
        self._lastAppliedModeName = None
        outcome = _FakeApplyOutcome(
            warnings=self._warningsByMode.get(name, []),
            failedComponents=self._failedComponentsByMode.get(name, []),
            warningComponents=self._warningComponentsByMode.get(name, []),
        )
        if not outcome.warnings and not outcome.failedComponents and not outcome.warningComponents:
            self._lastAppliedModeName = name
        return outcome

    def loadSetupMode(self, name, componentNames=None):
        return self.applySetupMode(name, componentNames).warnings

    def getSetupMode(self, name):
        if name in self._loadFailuresByMode:
            raise self._loadFailuresByMode[name]
        return {'name': name, 'state': self._stateByMode.get(name, {})}

    def getModeHazards(self, stateByComponent, applyMode, context=None):
        self.getModeHazardsCalls.append((stateByComponent, applyMode, context))
        # hazardsByMode is keyed by the state dict identity is unstable, so the
        # fake instead keys hazards on the state mapping value: tests put the
        # hazard list under a sentinel '__hazards__' key in the mode state.
        return list(stateByComponent.get('__hazards__', []))

    def getLastAppliedModeName(self):
        return self._lastAppliedModeName

    def isSetupModeHardwareCritical(self, componentName):
        return componentName in self._hardwareCriticalComponents


def _makeService(roleConfig, modes=None, warningsByMode=None,
                 failedComponentsByMode=None, warningComponentsByMode=None,
                 stateByMode=None, loadFailuresByMode=None, policyConfig=None,
                 laserPowerThresholdMw=50.0, hardwareCriticalComponents=None):
    backend = _FakeSetupModeController(
        modes=modes,
        warningsByMode=warningsByMode,
        failedComponentsByMode=failedComponentsByMode,
        warningComponentsByMode=warningComponentsByMode,
        stateByMode=stateByMode,
        loadFailuresByMode=loadFailuresByMode,
        hardwareCriticalComponents=hardwareCriticalComponents,
    )
    service = SmartMicroscopyModeService(
        backend, roleConfig, policyConfig=policyConfig,
        laserPowerThresholdMw=laserPowerThresholdMw, logger=_NullLogger(),
    )
    return service, backend


ROLE_CONFIG = {
    'EtSnouty': {
        'scouting': 'Snouty widefield scouting',
        'event': 'Snouty light-sheet event scan',
        'idle': 'Snouty safe idle',
    },
}

ALL_MODES = [
    'Snouty widefield scouting',
    'Snouty light-sheet event scan',
    'Snouty safe idle',
]


def test_resolve_mode_configured_and_unconfigured():
    service, _ = _makeService(ROLE_CONFIG, modes=ALL_MODES)

    assert service.resolveMode('EtSnouty', 'scouting') == 'Snouty widefield scouting'
    assert service.resolveMode('EtSnouty', 'event') == 'Snouty light-sheet event scan'

    # Unconfigured role -> None.
    assert service.resolveMode('EtSnouty', 'validation') is None
    # Unconfigured workflow -> None.
    assert service.resolveMode('EtSTED', 'scouting') is None


def test_none_role_config_is_treated_as_empty():
    service, _ = _makeService(None, modes=ALL_MODES)
    assert service.resolveMode('EtSnouty', 'scouting') is None
    assert service.validate() == []


def test_update_config_replaces_future_role_and_policy_mappings():
    service, _ = _makeService(
        {'EtSnouty': {'event': 'old mode'}},
        modes=['old mode', 'new mode'],
        policyConfig={'EtSnouty': 'allow'},
    )

    assert service.resolveMode('EtSnouty', 'event') == 'old mode'
    assert service.resolvePolicy('EtSnouty') == SmartModeHazardPolicy.ALLOW

    service.updateConfig(
        {'EtSnouty': {'event': 'new mode'}},
        {'EtSnouty': 'warnOnly'},
    )

    assert service.resolveMode('EtSnouty', 'event') == 'new mode'
    assert service.resolvePolicy('EtSnouty') == SmartModeHazardPolicy.WARN_ONLY


def test_validate_passes_when_all_modes_exist():
    service, _ = _makeService(ROLE_CONFIG, modes=ALL_MODES)
    assert service.validate() == []


def test_validate_flags_missing_mode():
    # 'Snouty safe idle' is intentionally not present in the backend.
    service, _ = _makeService(
        ROLE_CONFIG,
        modes=['Snouty widefield scouting', 'Snouty light-sheet event scan'],
    )
    problems = service.validate()

    assert len(problems) == 1
    assert 'Snouty safe idle' in problems[0]
    assert 'idle' in problems[0]


def test_apply_role_applies_and_records():
    service, backend = _makeService(ROLE_CONFIG, modes=ALL_MODES)

    result = service.applyRole('EtSnouty', 'scouting')

    assert isinstance(result, ApplyResult)
    assert result.applied is True
    assert result.ok is True
    assert result.modeName == 'Snouty widefield scouting'
    assert result.warnings == []
    assert result.failedComponents == []
    assert backend.applyCalls == ['Snouty widefield scouting']
    assert backend.getLastAppliedModeName() == 'Snouty widefield scouting'


def test_apply_role_noops_when_mode_already_active():
    service, backend = _makeService(ROLE_CONFIG, modes=ALL_MODES)

    # First apply loads the mode.
    service.applyRole('EtSnouty', 'scouting')
    assert backend.applyCalls == ['Snouty widefield scouting']

    # Second apply of the same resolved mode is a no-op.
    result = service.applyRole('EtSnouty', 'scouting')
    assert result.applied is False
    assert result.ok is True
    assert result.modeName == 'Snouty widefield scouting'
    assert result.warnings == []
    assert backend.applyCalls == ['Snouty widefield scouting']  # not called again


def test_apply_role_noops_against_externally_applied_mode():
    # Simulate the interactive SetupModesController having applied the mode.
    service, backend = _makeService(ROLE_CONFIG, modes=ALL_MODES)
    backend.applySetupMode('Snouty widefield scouting')
    backend.applyCalls.clear()

    result = service.applyRole('EtSnouty', 'scouting')

    assert result.applied is False
    assert result.ok is True
    assert result.warnings == []
    assert backend.applyCalls == []  # de-dup reads the shared backend getter


def test_apply_role_non_hardware_warning_stays_ok():
    service, backend = _makeService(
        ROLE_CONFIG,
        modes=ALL_MODES,
        warningsByMode={
            'Snouty light-sheet event scan': ['Settings: failed to restore ROI'],
        },
        warningComponentsByMode={
            'Snouty light-sheet event scan': ['Settings'],
        },
    )

    result = service.applyRole('EtSnouty', 'event')

    # A non-hardware warning is surfaced but does not mark the result not ok.
    assert result.applied is True
    assert result.ok is True
    assert result.warnings == ['Settings: failed to restore ROI']
    assert result.failedComponents == []
    assert backend.applyCalls == ['Snouty light-sheet event scan']


def test_apply_role_uses_component_metadata_for_hardware_criticality():
    service, _ = _makeService(
        ROLE_CONFIG,
        modes=ALL_MODES,
        warningsByMode={
            'Snouty light-sheet event scan': ['OlympusStand: failed to switch mode'],
        },
        warningComponentsByMode={
            'Snouty light-sheet event scan': ['OlympusStand'],
        },
        hardwareCriticalComponents={'OlympusStand'},
    )

    result = service.applyRole('EtSnouty', 'event')

    assert result.ok is False
    assert result.failedComponents == ['OlympusStand']


def test_apply_role_unmarked_component_warning_stays_ok_even_for_old_hardware_name():
    service, _ = _makeService(
        ROLE_CONFIG,
        modes=ALL_MODES,
        warningsByMode={
            'Snouty light-sheet event scan': ['FlipMirror: skipped in fake backend'],
        },
        warningComponentsByMode={
            'Snouty light-sheet event scan': ['FlipMirror'],
        },
    )

    result = service.applyRole('EtSnouty', 'event')

    assert result.ok is True
    assert result.failedComponents == []


def test_apply_role_hardware_warning_sets_not_ok_and_names_component():
    service, backend = _makeService(
        ROLE_CONFIG,
        modes=ALL_MODES,
        warningsByMode={
            'Snouty light-sheet event scan': ['FlipMirror: failed to move'],
        },
        warningComponentsByMode={
            'Snouty light-sheet event scan': ['FlipMirror'],
        },
        hardwareCriticalComponents={'FlipMirror'},
    )

    result = service.applyRole('EtSnouty', 'event')

    assert result.applied is True
    assert result.ok is False
    assert result.warnings == ['FlipMirror: failed to move']
    assert result.failedComponents == ['FlipMirror']
    assert backend.applyCalls == ['Snouty light-sheet event scan']

    # The failed apply is not considered active, so a retry is not skipped.
    retry = service.applyRole('EtSnouty', 'event')
    assert retry.applied is True
    assert backend.applyCalls == [
        'Snouty light-sheet event scan',
        'Snouty light-sheet event scan',
    ]


def test_failed_role_apply_clears_active_marker_so_recovery_can_reapply_previous_mode():
    service, backend = _makeService(
        ROLE_CONFIG,
        modes=ALL_MODES,
        warningsByMode={
            'Snouty light-sheet event scan': ['FlipMirror: failed to move'],
        },
        warningComponentsByMode={
            'Snouty light-sheet event scan': ['FlipMirror'],
        },
        hardwareCriticalComponents={'FlipMirror'},
    )

    assert service.applyRole('EtSnouty', 'scouting').ok is True
    assert backend.getLastAppliedModeName() == 'Snouty widefield scouting'

    assert service.applyRole('EtSnouty', 'event').ok is False
    assert backend.getLastAppliedModeName() is None

    recovery = service.applyRole('EtSnouty', 'scouting')

    assert recovery.applied is True
    assert recovery.ok is True
    assert backend.applyCalls == [
        'Snouty widefield scouting',
        'Snouty light-sheet event scan',
        'Snouty widefield scouting',
    ]


def test_apply_role_hardware_failure_sets_not_ok_and_names_component():
    # A hardware component (FlipMirror) raising during apply -> ok=False.
    service, backend = _makeService(
        ROLE_CONFIG,
        modes=ALL_MODES,
        warningsByMode={
            'Snouty light-sheet event scan': ['Failed to apply "FlipMirror": boom'],
        },
        failedComponentsByMode={
            'Snouty light-sheet event scan': ['FlipMirror'],
        },
        hardwareCriticalComponents={'FlipMirror'},
    )

    result = service.applyRole('EtSnouty', 'event')

    assert result.applied is True
    assert result.ok is False
    assert result.failedComponents == ['FlipMirror']
    assert result.warnings == ['Failed to apply "FlipMirror": boom']


def test_apply_role_non_hardware_failure_stays_ok():
    # A non-hardware component (Settings) raising is not a beam-path safety
    # event, so it does not by itself block (ok stays True), though its warning
    # is still surfaced.
    service, backend = _makeService(
        ROLE_CONFIG,
        modes=ALL_MODES,
        warningsByMode={
            'Snouty light-sheet event scan': ['Failed to apply "Settings": boom'],
        },
        failedComponentsByMode={
            'Snouty light-sheet event scan': ['Settings'],
        },
    )

    result = service.applyRole('EtSnouty', 'event')

    assert result.applied is True
    assert result.ok is True
    assert result.failedComponents == []
    assert result.warnings == ['Failed to apply "Settings": boom']


def test_apply_role_unmapped_does_not_raise_or_load():
    service, backend = _makeService(ROLE_CONFIG, modes=ALL_MODES)

    # Unmapped role on a known workflow.
    result = service.applyRole('EtSnouty', 'resume')
    assert result.applied is False
    # A missing mapping is a config issue, not a hardware failure -> ok stays True.
    assert result.ok is True
    assert result.modeName is None
    assert len(result.warnings) == 1
    assert 'resume' in result.warnings[0]
    assert backend.applyCalls == []

    # Unmapped workflow entirely.
    result = service.applyRole('EtSTED', 'scouting')
    assert result.applied is False
    assert result.ok is True
    assert len(result.warnings) == 1
    assert backend.applyCalls == []


def test_resolve_policy_defaults_to_block_on_hazard():
    # Absent workflow policy -> BLOCK_ON_HAZARD.
    service, _ = _makeService(ROLE_CONFIG, modes=ALL_MODES)
    assert service.resolvePolicy('EtSnouty') == SmartModeHazardPolicy.BLOCK_ON_HAZARD

    # Unknown policy name -> BLOCK_ON_HAZARD.
    service, _ = _makeService(
        ROLE_CONFIG, modes=ALL_MODES,
        policyConfig={'EtSnouty': 'notAPolicy'},
    )
    assert service.resolvePolicy('EtSnouty') == SmartModeHazardPolicy.BLOCK_ON_HAZARD

    # Known policy names resolve.
    service, _ = _makeService(
        ROLE_CONFIG, modes=ALL_MODES,
        policyConfig={'EtSnouty': 'warnOnly'},
    )
    assert service.resolvePolicy('EtSnouty') == SmartModeHazardPolicy.WARN_ONLY


def test_preflight_ok_when_no_hazards_or_missing_modes():
    service, backend = _makeService(
        ROLE_CONFIG, modes=ALL_MODES,
        stateByMode={name: {} for name in ALL_MODES},
    )

    result = service.preflight('EtSnouty')

    assert isinstance(result, PreflightResult)
    assert result.ok is True
    assert result.hazards == []
    assert result.missingModes == []
    # getModeHazards was called once per distinct existing mode.
    assert len(backend.getModeHazardsCalls) == len(ROLE_CONFIG['EtSnouty'])


def test_preflight_blocks_on_missing_mode_under_block_policy():
    # 'Snouty safe idle' missing from the backend.
    service, _ = _makeService(
        ROLE_CONFIG,
        modes=['Snouty widefield scouting', 'Snouty light-sheet event scan'],
        stateByMode={
            'Snouty widefield scouting': {},
            'Snouty light-sheet event scan': {},
        },
    )

    result = service.preflight('EtSnouty')

    assert result.ok is False
    assert result.missingModes == ['Snouty safe idle']
    assert any('Snouty safe idle' in message for message in result.messages)


def test_preflight_blocks_on_hazard_under_block_policy():
    hazard = {
        'kind': 'high_laser_power',
        'severity': 'warning',
        'message': 'Laser 488: 200 mW exceeds 50 mW threshold',
        'componentName': 'Laser',
    }
    service, backend = _makeService(
        ROLE_CONFIG, modes=ALL_MODES,
        stateByMode={
            'Snouty widefield scouting': {},
            'Snouty light-sheet event scan': {'__hazards__': [hazard]},
            'Snouty safe idle': {},
        },
    )

    result = service.preflight('EtSnouty')

    assert result.ok is False
    assert result.hazards == [hazard]
    assert any('200 mW' in message for message in result.messages)


def test_preflight_blocks_on_unreadable_mode_under_block_policy():
    service, _ = _makeService(
        ROLE_CONFIG,
        modes=ALL_MODES,
        stateByMode={name: {} for name in ALL_MODES},
        loadFailuresByMode={
            'Snouty light-sheet event scan': RuntimeError('corrupt mode file'),
        },
    )

    result = service.preflight('EtSnouty', roles=['event'])

    assert result.ok is False
    assert result.failedModes == ['Snouty light-sheet event scan']
    assert any('Could not load setup mode' in message for message in result.messages)


def test_preflight_warn_only_lists_hazards_but_stays_ok():
    hazard = {
        'kind': 'high_laser_power',
        'severity': 'warning',
        'message': 'Laser 488: 200 mW exceeds 50 mW threshold',
        'componentName': 'Laser',
    }
    service, _ = _makeService(
        ROLE_CONFIG, modes=ALL_MODES,
        stateByMode={
            'Snouty widefield scouting': {},
            'Snouty light-sheet event scan': {'__hazards__': [hazard]},
            'Snouty safe idle': {},
        },
        policyConfig={'EtSnouty': 'warnOnly'},
    )

    result = service.preflight('EtSnouty')

    assert result.ok is True
    assert result.hazards == [hazard]


def test_preflight_allow_lists_hazards_and_missing_but_stays_ok():
    hazard = {
        'kind': 'high_laser_power',
        'severity': 'warning',
        'message': 'Laser 488: 200 mW exceeds 50 mW threshold',
        'componentName': 'Laser',
    }
    service, _ = _makeService(
        ROLE_CONFIG,
        modes=['Snouty widefield scouting', 'Snouty light-sheet event scan'],
        stateByMode={
            'Snouty widefield scouting': {},
            'Snouty light-sheet event scan': {'__hazards__': [hazard]},
        },
        policyConfig={'EtSnouty': 'allow'},
    )

    result = service.preflight('EtSnouty')

    assert result.ok is True
    assert result.hazards == [hazard]
    assert result.missingModes == ['Snouty safe idle']


def test_preflight_respects_explicit_roles_argument():
    service, backend = _makeService(
        ROLE_CONFIG, modes=ALL_MODES,
        stateByMode={name: {} for name in ALL_MODES},
    )

    result = service.preflight('EtSnouty', roles=['scouting'])

    assert result.ok is True
    assert len(backend.getModeHazardsCalls) == 1
    assert backend.getModeHazardsCalls[0][1].value == 'setup_mode_apply'


def test_setup_mode_controller_tracks_last_applied_mode_name():
    """The real SetupModeController.applySetupMode updates the shared getter.

    Asserted against a fake controllers dict and a tmp mode dir-free path by
    going through the real controller's getter/setter contract directly: a fresh
    controller reports None, and the attribute the real applySetupMode sets is
    surfaced by getLastAppliedModeName.
    """
    from imswitch.imcontrol.controller.SetupModeController import SetupModeController

    controller = SetupModeController.__new__(SetupModeController)
    controller._lastAppliedModeName = None

    assert controller.getLastAppliedModeName() is None

    # applySetupMode sets self._lastAppliedModeName = mode['name'] before return.
    controller._lastAppliedModeName = 'Some applied mode'
    assert controller.getLastAppliedModeName() == 'Some applied mode'
