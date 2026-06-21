"""Phase 4 no-hardware integration test for smart-microscopy mode switching.

Drives the *real* chain end to end without any event controller running:

    SmartMicroscopyModeService.applyRole
        -> SetupModeController.applySetupMode
            -> registry.applyComponentState
                -> FlipMirror component.applyComponentState
                    -> MockThorlabsMFF.move_to

It saves two setup modes ("scouting" with the mock flip mirrors at state 0 and
"event" with them at state 1), then asserts that applying the corresponding
runtime roles physically switches the mock flip-mirror beam path. This is the
"setup mode apply can switch the relevant flip mirrors without EtSnouty running"
acceptance criterion for Phase 4.
"""

from types import SimpleNamespace

import pytest

import imswitch.imcontrol.controller.SetupModeController as setup_mode_module
from imswitch.imcontrol.controller.SetupModeController import SetupModeController
from imswitch.imcontrol.controller.SmartMicroscopyModeService import (
    SmartMicroscopyModeService,
)
from imswitch.imcontrol.controller.basecontrollers import StatefulComponentMixin
from imswitch.imcontrol.model.managers.flipMirrors.ThorlabsMFF_mock import (
    MockThorlabsMFF,
)


pytestmark = pytest.mark.nohardware


class _FlipMirrorComponent(StatefulComponentMixin):
    """Minimal FlipMirror setup-mode component backed by real mock managers.

    Mirrors the essentials of FlipMirrorController's component-state contract:
    snapshot reads each mock's state; apply moves each mock to the saved state
    and returns a warning (without raising) if a move fails.
    """

    componentName = 'FlipMirror'
    stateSchemaVersion = 1
    legacyStateNames = ()

    def __init__(self, mirrors):
        self._mirrors = mirrors

    def getComponentState(self):
        return {
            'mirrors': {
                name: {'state': mirror.get_state(), 'connected': mirror.is_connected()}
                for name, mirror in self._mirrors.items()
            },
            'links': {},
        }

    def applyComponentState(self, state, *, applyMode):
        warnings = []
        for name, mirrorState in (state.get('mirrors') or {}).items():
            target = mirrorState.get('state') if isinstance(mirrorState, dict) else mirrorState
            if target is None:
                continue
            if name not in self._mirrors:
                warnings.append(f'Flip mirror "{name}" is not available.')
                continue
            try:
                self._mirrors[name].move_to(int(target))
            except Exception as error:
                warnings.append(f'Failed to move flip mirror "{name}" to {target}: {error}.')
        return warnings

    def describeComponentState(self, state):
        return []

    def getComponentStateHazards(self, state, *, applyMode, context=None):
        return []


class _FakeRegistry:
    """Unified-registry stand-in delegating to the registered components."""

    def __init__(self, controllers):
        self._controllers = controllers

    def isRegistered(self, name):
        return name in self._controllers

    def snapshotComponent(self, name):
        controller = self._controllers.get(name)
        return controller.getComponentState() if controller is not None else None

    def applyComponentState(self, name, state, apply_mode):
        controller = self._controllers.get(name)
        if controller is None:
            return []
        return controller.applyComponentState(state, applyMode=apply_mode)

    def describeComponentState(self, name, state):
        return []

    def getComponentStateHazards(self, name, state, apply_mode, context=None):
        return []


def _make_mock_mirror(name, initial_state=0):
    deviceInfo = SimpleNamespace(
        managerProperties={
            'initial_state': initial_state,
            'state_names': {'0': 'Straight', '1': 'Tilted'},
        }
    )
    return MockThorlabsMFF(deviceInfo, name)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    mirrors = {
        'Illumination': _make_mock_mirror('Illumination'),
        'DetectionTilt': _make_mock_mirror('DetectionTilt'),
    }
    component = _FlipMirrorComponent(mirrors)
    controllers = {'FlipMirror': component}

    registry = _FakeRegistry(controllers)
    monkeypatch.setattr(setup_mode_module, 'getWidgetStatePersistence', lambda: registry)
    monkeypatch.setattr(setup_mode_module.dirtools.UserFileDirs, 'Root', str(tmp_path))

    setupModeController = SetupModeController(controllers)

    # Author the two modes from live mock state, exactly like a user would.
    for mirror in mirrors.values():
        mirror.move_to(0)
    setupModeController.saveSetupMode(
        'Snouty widefield scouting', componentNames=['FlipMirror']
    )
    for mirror in mirrors.values():
        mirror.move_to(1)
    setupModeController.saveSetupMode(
        'Snouty light-sheet event scan', componentNames=['FlipMirror']
    )
    # Leave hardware in the scouting state before the service drives it.
    for mirror in mirrors.values():
        mirror.move_to(0)

    service = SmartMicroscopyModeService(
        setupModeController,
        {
            'EtSnouty': {
                'scouting': 'Snouty widefield scouting',
                'event': 'Snouty light-sheet event scan',
            }
        },
    )
    return SimpleNamespace(mirrors=mirrors, service=service, controller=setupModeController)


def _states(mirrors):
    return {name: mirror.get_state() for name, mirror in mirrors.items()}


def test_apply_event_role_switches_flip_mirrors(harness):
    assert _states(harness.mirrors) == {'Illumination': 0, 'DetectionTilt': 0}

    result = harness.service.applyRole('EtSnouty', 'event')

    assert result.ok
    assert result.applied
    assert result.failedComponents == []
    assert _states(harness.mirrors) == {'Illumination': 1, 'DetectionTilt': 1}


def test_apply_scouting_role_switches_flip_mirrors_back(harness):
    harness.service.applyRole('EtSnouty', 'event')

    result = harness.service.applyRole('EtSnouty', 'scouting')

    assert result.ok
    assert result.applied
    assert _states(harness.mirrors) == {'Illumination': 0, 'DetectionTilt': 0}


def test_reapplying_active_role_is_a_noop(harness):
    harness.service.applyRole('EtSnouty', 'scouting')

    # The mirrors are already in the scouting state; re-applying must not run.
    for mirror in harness.mirrors.values():
        mirror.move_to(1)  # tamper to prove no re-apply occurs
    result = harness.service.applyRole('EtSnouty', 'scouting')

    assert not result.applied
    assert _states(harness.mirrors) == {'Illumination': 1, 'DetectionTilt': 1}


def test_setup_mode_apply_is_independent_of_any_event_controller(harness):
    # No EtSnouty / event loop exists in this test; the service alone drives the
    # beam path purely through the setup-mode backend.
    harness.service.applyRole('EtSnouty', 'event')
    assert harness.controller.getLastAppliedModeName() == 'Snouty light-sheet event scan'
