"""
Unit tests for LeicaStandController unified state persistence (Phase 5b).

Tests the StatefulComponentMixin implementation on LeicaStandController:
- getComponentState returns {'mode': ...}
- SETUP_MODE_APPLY to 'FLUO' calls setFLUO then setILshutter(1); to 'CS' calls setCS
- STARTUP_RESTORE does NOT actuate (warns on a state diff)
- A manager whose setFLUO/setCS raises yields a returned warning (failure surfaced)
- getComponentStateHazards returns [] for both modes
- disconnected / None manager yields a warning
- LeicaStand is discovered by SetupModeController._getModeAwareControllers
  when the controller is a registered component
"""

import pytest
from unittest.mock import Mock

from imswitch.imcontrol.controller.controllers.LeicaStandController import (
    LeicaStandController,
)
from imswitch.imcontrol.controller.basecontrollers import (
    ComponentStateApplyMode,
    StatefulComponentMixin,
)


pytestmark = pytest.mark.nohardware


@pytest.fixture
def mock_manager():
    """Mock Leica stand _subManager with the 3 raw mock methods + isConnected."""
    manager = Mock()
    manager.isConnected = Mock(return_value=True)
    manager.setFLUO = Mock()
    manager.setCS = Mock()
    manager.setILshutter = Mock()
    return manager


@pytest.fixture
def mock_widget():
    """Mock LeicaStandWidget."""
    widget = Mock()
    widget.setConnected = Mock()
    widget.setMode = Mock()
    return widget


@pytest.fixture
def controller(mock_manager, mock_widget):
    """Lightweight LeicaStandController bound to the component-state methods.

    Mirrors the FlipMirror test harness: build a Mock(spec=...) and bind the
    real unbound methods so we exercise the actual logic without running
    ImConWidgetController.__init__.
    """
    ctrl = Mock(spec=LeicaStandController)
    ctrl._manager = mock_manager
    ctrl._widget = mock_widget
    ctrl._current_mode = "FLUO"
    ctrl.FLUO_SETTLE_SECONDS = 0.0  # don't actually sleep in tests
    ctrl._LeicaStandController__logger = Mock()

    # No-op the Qt-pumping settle so tests don't spin the event loop.
    ctrl._sleepPumpingEvents = lambda seconds: None

    ctrl.getComponentState = lambda: LeicaStandController.getComponentState(ctrl)
    ctrl.applyComponentState = lambda state, applyMode: LeicaStandController.applyComponentState(
        ctrl, state, applyMode=applyMode
    )
    ctrl.describeComponentState = lambda state: LeicaStandController.describeComponentState(
        ctrl, state
    )
    ctrl.getComponentStateHazards = lambda state, applyMode, context=None: LeicaStandController.getComponentStateHazards(
        ctrl, state, applyMode=applyMode, context=context
    )

    ctrl.componentName = 'LeicaStand'
    ctrl.stateSchemaVersion = 1
    ctrl.legacyStateNames = ()

    return ctrl


def test_leicastand_implements_stateful_component_mixin(controller):
    """LeicaStandController implements StatefulComponentMixin with correct attrs."""
    assert LeicaStandController.componentName == 'LeicaStand'
    assert LeicaStandController.stateSchemaVersion == 1
    assert LeicaStandController.legacyStateNames == ()
    assert issubclass(LeicaStandController, StatefulComponentMixin)

    assert hasattr(controller, 'getComponentState')
    assert hasattr(controller, 'applyComponentState')
    assert hasattr(controller, 'describeComponentState')
    assert hasattr(controller, 'getComponentStateHazards')


def test_get_component_state_returns_mode(controller):
    """getComponentState returns {'mode': self._current_mode}."""
    controller._current_mode = "CS"
    assert controller.getComponentState() == {"mode": "CS"}

    controller._current_mode = "FLUO"
    assert controller.getComponentState() == {"mode": "FLUO"}


def test_apply_setup_mode_fluo_calls_fluo_then_shutter(controller, mock_manager):
    """SETUP_MODE_APPLY to FLUO calls setFLUO() then setILshutter(1)."""
    controller._current_mode = "CS"

    warnings = controller.applyComponentState(
        {"mode": "FLUO"},
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    mock_manager.setFLUO.assert_called_once_with()
    mock_manager.setILshutter.assert_called_once_with(1)
    mock_manager.setCS.assert_not_called()
    assert warnings == []
    assert controller._current_mode == "FLUO"
    controller._widget.setMode.assert_called_with("FLUO")


def test_apply_setup_mode_cs_calls_cs(controller, mock_manager):
    """SETUP_MODE_APPLY to CS calls setCS() and not the FLUO sequence."""
    controller._current_mode = "FLUO"

    warnings = controller.applyComponentState(
        {"mode": "CS"},
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    mock_manager.setCS.assert_called_once_with()
    mock_manager.setFLUO.assert_not_called()
    mock_manager.setILshutter.assert_not_called()
    assert warnings == []
    assert controller._current_mode == "CS"
    controller._widget.setMode.assert_called_with("CS")


def test_apply_startup_restore_does_not_actuate(controller, mock_manager):
    """STARTUP_RESTORE does NOT call any actuation, and warns on a state diff."""
    controller._current_mode = "FLUO"

    warnings = controller.applyComponentState(
        {"mode": "CS"},  # differs from current FLUO
        applyMode=ComponentStateApplyMode.STARTUP_RESTORE,
    )

    mock_manager.setFLUO.assert_not_called()
    mock_manager.setCS.assert_not_called()
    mock_manager.setILshutter.assert_not_called()

    # current mode unchanged (no actuation)
    assert controller._current_mode == "FLUO"

    # warns about not switching
    assert len(warnings) == 1
    assert 'not switched at startup' in warnings[0]
    assert 'CS' in warnings[0]


def test_apply_startup_restore_no_warning_when_modes_match(controller, mock_manager):
    """STARTUP_RESTORE with a matching mode actuates nothing and warns nothing."""
    controller._current_mode = "FLUO"

    warnings = controller.applyComponentState(
        {"mode": "FLUO"},
        applyMode=ComponentStateApplyMode.STARTUP_RESTORE,
    )

    mock_manager.setFLUO.assert_not_called()
    mock_manager.setCS.assert_not_called()
    assert warnings == []


def test_apply_setup_mode_surfaces_failure_for_fluo(controller, mock_manager):
    """A setFLUO that raises yields a returned warning (failure surfaced)."""
    mock_manager.setFLUO.side_effect = RuntimeError("comm error")
    controller._current_mode = "CS"

    warnings = controller.applyComponentState(
        {"mode": "FLUO"},
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    assert len(warnings) == 1
    assert 'Failed to switch Leica stand to FLUO' in warnings[0]
    assert 'comm error' in warnings[0]
    # mode is NOT advanced on failure
    assert controller._current_mode == "CS"


def test_apply_setup_mode_surfaces_failure_for_cs(controller, mock_manager):
    """A setCS that raises yields a returned warning (not swallowed)."""
    mock_manager.setCS.side_effect = RuntimeError("stand stuck")
    controller._current_mode = "FLUO"

    warnings = controller.applyComponentState(
        {"mode": "CS"},
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    assert len(warnings) == 1
    assert 'Failed to switch Leica stand to CS' in warnings[0]
    assert 'stand stuck' in warnings[0]
    assert controller._current_mode == "FLUO"


def test_apply_unknown_mode_warns_no_actuation(controller, mock_manager):
    """An unknown saved mode warns and actuates nothing."""
    warnings = controller.applyComponentState(
        {"mode": "BOGUS"},
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    mock_manager.setFLUO.assert_not_called()
    mock_manager.setCS.assert_not_called()
    assert len(warnings) == 1
    assert 'Unknown Leica stand mode' in warnings[0]


def test_apply_none_manager_yields_warning(controller, mock_manager):
    """A None manager yields a 'not available' warning, no raise."""
    controller._manager = None

    warnings = controller.applyComponentState(
        {"mode": "FLUO"},
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    assert len(warnings) == 1
    assert 'not available' in warnings[0]


def test_apply_disconnected_manager_yields_warning(controller, mock_manager):
    """A disconnected manager yields a 'not connected' warning, no actuation."""
    mock_manager.isConnected.return_value = False

    warnings = controller.applyComponentState(
        {"mode": "FLUO"},
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    mock_manager.setFLUO.assert_not_called()
    assert len(warnings) == 1
    assert 'not connected' in warnings[0]


def test_describe_component_state(controller):
    """describeComponentState returns a readable summary."""
    assert controller.describeComponentState({"mode": "FLUO"}) == ["Stand: FLUO"]
    assert controller.describeComponentState({"mode": "CS"}) == ["Stand: CS"]
    assert controller.describeComponentState({}) == ["Stand: unknown"]


def test_get_component_state_hazards_empty_for_both_modes(controller):
    """getComponentStateHazards returns [] for both modes."""
    state = {"mode": "FLUO"}

    assert controller.getComponentStateHazards(
        state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE
    ) == []
    assert controller.getComponentStateHazards(
        state, applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
    ) == []


def test_round_trip_state(controller, mock_manager):
    """Round-trip: snapshot CS, switch back to FLUO via SETUP_MODE_APPLY."""
    controller._current_mode = "CS"
    saved = controller.getComponentState()
    assert saved == {"mode": "CS"}

    controller._current_mode = "FLUO"
    warnings = controller.applyComponentState(
        saved, applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
    )

    assert warnings == []
    mock_manager.setCS.assert_called_once_with()
    assert controller._current_mode == "CS"


def test_registration_in_init_only_with_usable_manager():
    """__init__ registers LeicaStand only on the usable-manager path."""
    import inspect

    source = inspect.getsource(LeicaStandController.__init__)

    # Registration call is present.
    assert "getWidgetStatePersistence().register('LeicaStand', self)" in source

    # It comes after all three early-return guards (stand_manager None, mocker,
    # _subManager None), so it is skipped on the mock/disconnected paths.
    lines = source.split('\n')
    register_idx = next(
        i for i, ln in enumerate(lines)
        if "register('LeicaStand'" in ln
    )
    last_return_idx = max(
        i for i, ln in enumerate(lines) if 'return' in ln
    )
    assert register_idx > last_return_idx, (
        "Registration must follow the early-return guards"
    )


def test_leicastand_discovered_in_setup_mode_controller():
    """SetupModeController._getModeAwareControllers discovers a LeicaStand component.

    Discovery is over the controllers dict and keys on isinstance(controller,
    StatefulComponentMixin), so a registered LeicaStandController is picked up.
    """
    from imswitch.imcontrol.controller.SetupModeController import SetupModeController

    fake_stand = Mock(spec=LeicaStandController)
    # Ensure it is recognized as a StatefulComponentMixin instance.
    fake_stand.__class__ = LeicaStandController

    smc = SetupModeController.__new__(SetupModeController)
    smc._controllers = {'LeicaStand': fake_stand}

    discovered = SetupModeController._getModeAwareControllers(smc)

    assert 'LeicaStand' in discovered
    assert discovered['LeicaStand'] is fake_stand


def test_leicastand_apply_ordering_and_hardware_components():
    """LeicaStand orders and hardware criticality are declared on the controller.

    The working-tree SetupModeController no longer keeps a component-name
    ``applyOrder`` list (it is ``()``); ordering is declared per-component via
    ``setupModeApplyPriority``. LeicaStand declares the MICROSCOPE_STAND band and
    marks itself as hardware-critical for smart-mode failure handling.
    """
    from imswitch.imcontrol.controller.basecontrollers import SetupModeApplyPriority

    assert (
        LeicaStandController.setupModeApplyPriority
        == SetupModeApplyPriority.MICROSCOPE_STAND
    )
    assert LeicaStandController.setupModeHardwareCritical is True
