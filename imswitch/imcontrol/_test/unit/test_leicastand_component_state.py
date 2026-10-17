"""
Unit tests for LeicaStandController unified state persistence.

The setup-mode path must use the same high-level Leica FLUO/CS transitions as
normal widget interaction while preserving startup non-actuation and backwards
compatibility with older mode-only snapshots.
"""

from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest

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
    manager = Mock()
    manager.isConnected = Mock(return_value=True)
    manager.setFLUO = Mock()  # raw setup-mode path must no longer use this
    manager.setCS = Mock()
    manager.setCube = Mock()
    manager.setILFieldDiaphragm = Mock()
    manager.setCameraPort = Mock()
    manager.setMagnScan = Mock()
    manager.setMagn1 = Mock()
    manager.setILshutter = Mock()
    return manager


@pytest.fixture
def mock_widget():
    widget = Mock()
    widget.setConnected = Mock()
    widget.setMode = Mock()
    widget.setCurrentCube = Mock()
    widget.setCurrentPortSide = Mock()
    return widget


@pytest.fixture
def controller(mock_manager, mock_widget):
    """Lightweight controller using the real LeicaStandController methods."""
    ctrl = SimpleNamespace(
        _manager=mock_manager,
        _widget=mock_widget,
        _current_mode="FLUO",
        _current_port_side="Left",
        _last_selected_fluo_cube_name="FITC",
        _cube_slot_to_name={1: "DAPI", 2: "FITC", 6: "EMP_BF"},
        _cube_name_to_slot={"DAPI": 1, "FITC": 2, "EMP_BF": 6},
        _LeicaStandController__logger=Mock(),
        FLUO_SHUTTER_DELAY_MS=LeicaStandController.FLUO_SHUTTER_DELAY_MS,
    )

    method_names = (
        "getComponentState",
        "applyComponentState",
        "describeComponentState",
        "getComponentStateHazards",
        "_safe_call",
        "_get_selected_fluo_cube_name",
        "_performFluoModeTransition",
        "setFluoMode",
        "_finishSetFluoMode",
        "_performCSModeTransition",
        "setCSMode",
        "setCubeByName",
        "setPortSideByName",
    )
    for name in method_names:
        setattr(ctrl, name, MethodType(getattr(LeicaStandController, name), ctrl))

    # Setup-mode FLUO waits for the same 800 ms shutter delay. Tests only need
    # the sequencing, so avoid sleeping/spinning the Qt event loop.
    ctrl._sleepPumpingEvents = lambda seconds: None
    return ctrl


def _hardware_events(manager):
    events = []
    manager.setCS.side_effect = lambda: events.append(("setCS",))
    manager.setCube.side_effect = lambda slot: events.append(("setCube", slot))
    manager.setILFieldDiaphragm.side_effect = (
        lambda value: events.append(("setILFieldDiaphragm", value))
    )
    manager.setCameraPort.side_effect = lambda: events.append(("setCameraPort",))
    manager.setMagnScan.side_effect = lambda: events.append(("setMagnScan",))
    manager.setMagn1.side_effect = lambda: events.append(("setMagn1",))
    manager.setILshutter.side_effect = (
        lambda value: events.append(("setILshutter", value))
    )
    return events


def test_leicastand_implements_stateful_component_mixin(controller):
    assert LeicaStandController.componentName == "LeicaStand"
    assert LeicaStandController.stateSchemaVersion == 1
    assert LeicaStandController.legacyStateNames == ()
    assert issubclass(LeicaStandController, StatefulComponentMixin)

    assert hasattr(controller, "getComponentState")
    assert hasattr(controller, "applyComponentState")
    assert hasattr(controller, "describeComponentState")
    assert hasattr(controller, "getComponentStateHazards")


def test_get_component_state_includes_mode_cube_and_port(controller):
    controller._current_mode = "CS"
    controller._last_selected_fluo_cube_name = "DAPI"
    controller._current_port_side = "Right"

    assert controller.getComponentState() == {
        "mode": "CS",
        "lastSelectedFluoCubeName": "DAPI",
        "portSide": "Right",
    }


def test_setup_mode_fluo_uses_same_hardware_sequence_as_widget(
    controller, mock_manager, monkeypatch
):
    """Setup-mode FLUO and widget FLUO drive the same high-level stand calls."""
    import importlib

    leica_module = importlib.import_module(
        "imswitch.imcontrol.controller.controllers.LeicaStandController"
    )
    monkeypatch.setattr(
        leica_module.QtCore,
        "QTimer",
        SimpleNamespace(singleShot=lambda _delay, callback: callback()),
    )

    widget_events = _hardware_events(mock_manager)
    controller.setFluoMode()
    widget_events = list(widget_events)

    mock_manager.reset_mock()
    setup_events = _hardware_events(mock_manager)
    controller._current_mode = "CS"
    warnings = controller.applyComponentState(
        {"mode": "FLUO"},
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    assert warnings == []
    assert setup_events == widget_events == [
        ("setCS",),
        ("setCube", 2),
        ("setILFieldDiaphragm", 12),
        ("setCameraPort",),
        ("setILshutter", 1),
    ]
    mock_manager.setFLUO.assert_not_called()
    assert controller._current_mode == "FLUO"


def test_setup_mode_cs_uses_same_hardware_sequence_as_widget(
    controller, mock_manager
):
    widget_events = _hardware_events(mock_manager)
    controller.setCSMode()
    widget_events = list(widget_events)

    mock_manager.reset_mock()
    setup_events = _hardware_events(mock_manager)
    controller._current_mode = "FLUO"
    warnings = controller.applyComponentState(
        {"mode": "CS"},
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    assert warnings == []
    assert setup_events == widget_events == [
        ("setCS",),
        ("setCube", 6),
        ("setCameraPort",),
        ("setMagnScan",),
    ]
    mock_manager.setFLUO.assert_not_called()
    assert controller._current_mode == "CS"
    assert controller._current_port_side == "Left"


def test_setup_mode_restores_saved_fluo_cube(controller, mock_manager):
    controller._last_selected_fluo_cube_name = "FITC"

    warnings = controller.applyComponentState(
        {
            "mode": "FLUO",
            "lastSelectedFluoCubeName": "DAPI",
        },
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    assert warnings == []
    assert controller._last_selected_fluo_cube_name == "DAPI"
    controller._widget.setCurrentCube.assert_called_with("DAPI")
    mock_manager.setCube.assert_called_once_with(1)


def test_setup_mode_restores_port_after_cs_normalizes_left(controller, mock_manager):
    warnings = controller.applyComponentState(
        {
            "mode": "CS",
            "portSide": "Right",
        },
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    assert warnings == []
    # CS's normal transition first selects the scan/Left magnification, then the
    # saved independent port selection is restored.
    assert mock_manager.setMagnScan.call_count == 1
    mock_manager.setMagn1.assert_called_once_with()
    assert controller._current_port_side == "Right"
    controller._widget.setCurrentPortSide.assert_called_with("Right")


def test_old_mode_only_snapshot_still_loads(controller, mock_manager):
    controller._last_selected_fluo_cube_name = "FITC"
    controller._current_port_side = "Right"

    warnings = controller.applyComponentState(
        {"mode": "FLUO"},
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    assert warnings == []
    mock_manager.setCube.assert_called_once_with(2)
    # Missing portSide means no extra port restore is imposed on old snapshots.
    mock_manager.setMagn1.assert_not_called()
    mock_manager.setMagnScan.assert_not_called()


def test_startup_restore_updates_metadata_without_actuation(controller, mock_manager):
    controller._current_mode = "FLUO"

    warnings = controller.applyComponentState(
        {
            "mode": "CS",
            "lastSelectedFluoCubeName": "DAPI",
            "portSide": "Right",
        },
        applyMode=ComponentStateApplyMode.STARTUP_RESTORE,
    )

    assert controller._current_mode == "FLUO"
    assert controller._last_selected_fluo_cube_name == "DAPI"
    assert controller._current_port_side == "Right"
    controller._widget.setCurrentCube.assert_called_with("DAPI")
    controller._widget.setCurrentPortSide.assert_called_with("Right")

    mock_manager.setCS.assert_not_called()
    mock_manager.setCube.assert_not_called()
    mock_manager.setMagn1.assert_not_called()
    mock_manager.setILshutter.assert_not_called()

    assert len(warnings) == 1
    assert "not switched at startup" in warnings[0]


def test_startup_restore_no_warning_when_modes_match(controller, mock_manager):
    warnings = controller.applyComponentState(
        {"mode": "FLUO"},
        applyMode=ComponentStateApplyMode.STARTUP_RESTORE,
    )

    mock_manager.setCS.assert_not_called()
    assert warnings == []


def test_setup_mode_surfaces_transition_failure_without_advancing_mode(
    controller, mock_manager
):
    mock_manager.setCameraPort.side_effect = RuntimeError("comm error")
    controller._current_mode = "CS"

    warnings = controller.applyComponentState(
        {"mode": "FLUO"},
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    assert len(warnings) == 1
    assert "Failed to switch Leica stand to FLUO" in warnings[0]
    assert "comm error" in warnings[0]
    assert controller._current_mode == "CS"
    mock_manager.setILshutter.assert_not_called()


def test_setup_mode_surfaces_cs_failure_without_advancing_mode(
    controller, mock_manager
):
    mock_manager.setCube.side_effect = RuntimeError("stand stuck")
    controller._current_mode = "FLUO"

    warnings = controller.applyComponentState(
        {"mode": "CS"},
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    assert len(warnings) == 1
    assert "Failed to switch Leica stand to CS" in warnings[0]
    assert "stand stuck" in warnings[0]
    assert controller._current_mode == "FLUO"


def test_invalid_saved_cube_warns_but_uses_current_valid_selection(
    controller, mock_manager
):
    warnings = controller.applyComponentState(
        {
            "mode": "FLUO",
            "lastSelectedFluoCubeName": "MISSING",
        },
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    assert len(warnings) == 1
    assert "is not available" in warnings[0]
    mock_manager.setCube.assert_called_once_with(2)


def test_invalid_saved_port_warns_without_extra_port_actuation(
    controller, mock_manager
):
    warnings = controller.applyComponentState(
        {"mode": "FLUO", "portSide": "Top"},
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    assert len(warnings) == 1
    assert "Unknown Leica stand port side" in warnings[0]
    mock_manager.setMagn1.assert_not_called()
    mock_manager.setMagnScan.assert_not_called()


def test_unknown_mode_warns_no_actuation(controller, mock_manager):
    warnings = controller.applyComponentState(
        {"mode": "BOGUS"},
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    mock_manager.setCS.assert_not_called()
    mock_manager.setCube.assert_not_called()
    assert len(warnings) == 1
    assert "Unknown Leica stand mode" in warnings[0]


def test_none_manager_yields_warning(controller):
    controller._manager = None

    warnings = controller.applyComponentState(
        {"mode": "FLUO"},
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    assert warnings == ["Leica stand is not available."]


def test_disconnected_manager_yields_warning(controller, mock_manager):
    mock_manager.isConnected.return_value = False

    warnings = controller.applyComponentState(
        {"mode": "FLUO"},
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    mock_manager.setCS.assert_not_called()
    assert warnings == ["Leica stand is not connected."]


def test_describe_component_state(controller):
    assert controller.describeComponentState({"mode": "FLUO"}) == ["Stand: FLUO"]
    assert controller.describeComponentState(
        {
            "mode": "CS",
            "lastSelectedFluoCubeName": "DAPI",
            "portSide": "Right",
        }
    ) == ["Stand: CS", "Fluorescence cube: DAPI", "Port: Right"]
    assert controller.describeComponentState({}) == ["Stand: unknown"]


def test_get_component_state_hazards_empty_for_both_modes(controller):
    state = {"mode": "FLUO"}

    assert controller.getComponentStateHazards(
        state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE
    ) == []
    assert controller.getComponentStateHazards(
        state, applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
    ) == []


def test_round_trip_state(controller, mock_manager):
    controller._current_mode = "CS"
    controller._last_selected_fluo_cube_name = "DAPI"
    controller._current_port_side = "Right"
    saved = controller.getComponentState()

    controller._current_mode = "FLUO"
    controller._last_selected_fluo_cube_name = "FITC"
    controller._current_port_side = "Left"

    warnings = controller.applyComponentState(
        saved,
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY,
    )

    assert warnings == []
    assert controller._current_mode == "CS"
    assert controller._last_selected_fluo_cube_name == "DAPI"
    assert controller._current_port_side == "Right"
    mock_manager.setMagn1.assert_called_once_with()

def test_registration_in_init_only_with_usable_manager():
    """__init__ registers LeicaStand only on the usable-manager path."""
    import inspect

    source = inspect.getsource(LeicaStandController.__init__)

    # Registration call is present.
    assert "getWidgetStatePersistence().register('LeicaStand', self)" in source

    # It comes after the early-return guards, so it is skipped when no usable
    # real stand manager is available.
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
