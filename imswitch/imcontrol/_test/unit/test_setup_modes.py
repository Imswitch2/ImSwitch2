import importlib.util
import sys
import types
from pathlib import Path


class DummySetupModeController:
    def __init__(self, name, state, calls):
        self.name = name
        self.state = state
        self.calls = calls

    def getSetupModeState(self):
        return dict(self.state)

    def applySetupModeState(self, state):
        self.calls.append(self.name)
        self.state = dict(state)
        return []


class UnsupportedController:
    pass


class DummyWidgetStatePersistence:
    """Mock registry for component state describe/hazard APIs."""
    
    def __init__(self):
        self._components = {}
    
    def register_component(self, name, describe_fn, hazard_fn):
        """Register component handlers."""
        self._components[name] = {
            "describe": describe_fn,
            "hazard": hazard_fn,
        }
    
    def isRegistered(self, component_name):
        """Check if component is registered."""
        return component_name in self._components
    
    def describeComponentState(self, component_name, state):
        """Delegate to component's describe function."""
        if component_name not in self._components:
            return [f"  {component_name}: no description available"]
        return self._components[component_name]["describe"](state)
    
    def getComponentStateHazards(self, component_name, state, apply_mode, context=None):
        """Delegate to component's hazard function."""
        if component_name not in self._components:
            return []
        return self._components[component_name]["hazard"](state, apply_mode, context or {})


def make_controller(tmp_path, monkeypatch, controllers, state_registry=None):
    setup_mode_module, setup_mode_mixin = load_setup_mode_module(monkeypatch)

    for controller in controllers.values():
        if isinstance(controller, DummySetupModeController):
            controller.__class__ = type(
                "ModeAwareDummySetupModeController",
                (setup_mode_mixin, DummySetupModeController),
                {}
            )

    dirtools = setup_mode_module.dirtools
    monkeypatch.setattr(dirtools.UserFileDirs, "Root", str(tmp_path))
    
    # Set the registry instance if provided
    if state_registry is not None:
        model_module = setup_mode_module.model
        model_module._RegistryRef.instance = state_registry
    
    mode_controller = setup_mode_module.SetupModeController(controllers)
    return mode_controller


def load_setup_mode_module(monkeypatch):
    from enum import Enum
    
    controller_dir = Path(__file__).resolve().parents[2] / "controller"
    package_name = "imswitch.imcontrol.controller"

    package_module = types.ModuleType(package_name)
    package_module.__path__ = [str(controller_dir)]
    monkeypatch.setitem(sys.modules, package_name, package_module)

    class ComponentStateApplyMode(Enum):
        """Distinguishes passive UI restore from active hardware application."""
        STARTUP_RESTORE = "startup_restore"
        SETUP_MODE_APPLY = "setup_mode_apply"

    class StatefulComponentMixin:
        pass

    class SetupModeMixin(StatefulComponentMixin):
        pass

    basecontrollers_module = types.ModuleType(f"{package_name}.basecontrollers")
    basecontrollers_module.StatefulComponentMixin = StatefulComponentMixin
    basecontrollers_module.SetupModeMixin = SetupModeMixin
    basecontrollers_module.ComponentStateApplyMode = ComponentStateApplyMode
    monkeypatch.setitem(sys.modules, f"{package_name}.basecontrollers", basecontrollers_module)

    # Create a minimal model module for getWidgetStatePersistence
    # This must be done BEFORE loading SetupModeController since it imports from model
    model_module = types.ModuleType("imswitch.imcontrol.model")
    
    # Create a mutable reference that can be updated later
    class _RegistryRef:
        instance = None
    
    def get_widget_state_persistence():
        return _RegistryRef.instance
    
    model_module.getWidgetStatePersistence = get_widget_state_persistence
    model_module._RegistryRef = _RegistryRef
    monkeypatch.setitem(sys.modules, "imswitch.imcontrol.model", model_module)

    # Create a stub for WidgetStatePersistence._StateInterface
    state_interface_module = types.ModuleType("imswitch.imcontrol.model.WidgetStatePersistence")
    state_interface_module._StateInterface = object  # Minimal stub
    monkeypatch.setitem(sys.modules, "imswitch.imcontrol.model.WidgetStatePersistence", state_interface_module)

    spec = importlib.util.spec_from_file_location(
        f"{package_name}.SetupModeController",
        controller_dir / "SetupModeController.py"
    )
    setup_mode_module = importlib.util.module_from_spec(spec)
    setup_mode_module.model = model_module  # Make it accessible to make_controller
    monkeypatch.setitem(sys.modules, spec.name, setup_mode_module)
    spec.loader.exec_module(setup_mode_module)

    return setup_mode_module, SetupModeMixin


def test_setup_mode_roundtrip(tmp_path, monkeypatch):
    calls = []
    laser = DummySetupModeController("Laser", {"power": 10}, calls)
    setup_modes = make_controller(
        tmp_path,
        monkeypatch,
        {
            "Laser": laser,
            "Unsupported": UnsupportedController(),
        }
    )

    result = setup_modes.saveSetupMode(
        "test mode",
        componentNames=["Laser", "Unsupported"],
        description="laser only",
        shortcut="F3"
    )

    assert result["warnings"] == ['Setup mode component "Unsupported" is not available.']
    assert result["mode"]["includedComponents"] == ["Laser"]
    assert result["mode"]["description"] == "laser only"
    assert result["mode"]["shortcut"] == "F3"
    assert setup_modes.listSetupModes() == ["test mode"]

    laser.state = {"power": 0}
    warnings = setup_modes.loadSetupMode("test mode")

    assert warnings == []
    assert laser.state == {"power": 10}
    assert calls == ["Laser"]


def test_setup_mode_metadata_rename_duplicate(tmp_path, monkeypatch):
    calls = []
    setup_modes = make_controller(
        tmp_path,
        monkeypatch,
        {
            "Laser": DummySetupModeController("Laser", {"power": 10}, calls),
        }
    )

    setup_modes.saveSetupMode(
        "original",
        componentNames=["Laser"],
        description="starting point",
        shortcut="F3"
    )

    updated = setup_modes.updateSetupModeMetadata(
        "original", description="updated", shortcut=""
    )
    assert updated["description"] == "updated"
    assert updated["shortcut"] is None

    renamed = setup_modes.renameSetupMode("original", "renamed")
    assert renamed["name"] == "renamed"
    assert setup_modes.listSetupModes() == ["renamed"]

    duplicate = setup_modes.duplicateSetupMode("renamed", "copy", shortcut="F4")
    assert duplicate["name"] == "copy"
    assert duplicate["description"] == "updated"
    assert duplicate["shortcut"] == "F4"
    assert setup_modes.listSetupModes() == ["copy", "renamed"]


def test_setup_mode_apply_order(tmp_path, monkeypatch):
    calls = []
    setup_modes = make_controller(
        tmp_path,
        monkeypatch,
        {
            "Laser": DummySetupModeController("Laser", {"enabled": True}, calls),
            "Scan": DummySetupModeController("Scan", {"size": 1}, calls),
            "Settings": DummySetupModeController("Settings", {"roi": [0, 0, 10, 10]}, calls),
        }
    )

    setup_modes.saveSetupMode("ordered", componentNames=["Laser", "Scan", "Settings"])
    setup_modes.loadSetupMode("ordered")

    assert calls == ["Settings", "Scan", "Laser"]


def test_describe_mode_component(tmp_path, monkeypatch):
    """Test describeModeComponent delegator."""
    registry = DummyWidgetStatePersistence()
    registry.register_component(
        "Laser",
        describe_fn=lambda state: [
            f"  power: {state.get('power')}",
            f"  enabled: {state.get('enabled')}"
        ],
        hazard_fn=lambda state, mode, ctx: []
    )
    
    setup_modes = make_controller(
        tmp_path,
        monkeypatch,
        {"Laser": DummySetupModeController("Laser", {"power": 100, "enabled": True}, [])},
        state_registry=registry
    )
    
    description = setup_modes.describeModeComponent("Laser", {"power": 100, "enabled": True})
    assert description == ["  power: 100", "  enabled: True"]


def test_get_mode_hazards_high_laser_power(tmp_path, monkeypatch):
    """Test getModeHazards returns high_laser_power hazard above threshold."""
    from enum import Enum
    
    class ComponentStateApplyMode(Enum):
        STARTUP_RESTORE = "startup_restore"
        SETUP_MODE_APPLY = "setup_mode_apply"
    
    def laser_hazard_fn(state, apply_mode, context):
        """Mock laser hazard function."""
        hazards = []
        threshold = context.get("laserPowerThresholdMw", 100)
        
        if apply_mode == ComponentStateApplyMode.STARTUP_RESTORE:
            return []
        
        lasers = state.get("lasers", {})
        for laser_name, laser_state in lasers.items():
            if not laser_state.get("enabled"):
                continue
            power = laser_state.get("value", 0)
            if power > threshold:
                hazards.append({
                    "kind": "high_laser_power",
                    "componentName": "Laser",
                    "details": {
                        "laserName": laser_name,
                        "value": power,
                        "units": "mW",
                    }
                })
        return hazards
    
    registry = DummyWidgetStatePersistence()
    registry.register_component(
        "Laser",
        describe_fn=lambda state: ["  lasers configured"],
        hazard_fn=laser_hazard_fn
    )
    
    setup_modes = make_controller(
        tmp_path,
        monkeypatch,
        {"Laser": DummySetupModeController("Laser", {}, [])},
        state_registry=registry
    )
    
    # Test high power above threshold
    high_power_state = {
        "Laser": {
            "lasers": {
                "488nm": {"enabled": True, "value": 200},
            }
        }
    }
    hazards = setup_modes.getModeHazards(
        high_power_state,
        ComponentStateApplyMode.SETUP_MODE_APPLY,
        context={"laserPowerThresholdMw": 100}
    )
    assert len(hazards) == 1
    assert hazards[0]["kind"] == "high_laser_power"
    assert hazards[0]["details"]["laserName"] == "488nm"
    assert hazards[0]["details"]["value"] == 200
    
    # Test below threshold
    low_power_state = {
        "Laser": {
            "lasers": {
                "488nm": {"enabled": True, "value": 50},
            }
        }
    }
    hazards = setup_modes.getModeHazards(
        low_power_state,
        ComponentStateApplyMode.SETUP_MODE_APPLY,
        context={"laserPowerThresholdMw": 100}
    )
    assert len(hazards) == 0
    
    # Test STARTUP_RESTORE mode returns no hazards
    hazards = setup_modes.getModeHazards(
        high_power_state,
        ComponentStateApplyMode.STARTUP_RESTORE,
        context={"laserPowerThresholdMw": 100}
    )
    assert len(hazards) == 0


def test_diff_mode_components(tmp_path, monkeypatch):
    """Test diffModeComponents delegator."""
    registry = DummyWidgetStatePersistence()
    registry.register_component(
        "Laser",
        describe_fn=lambda state: [f"  power: {state.get('power', 0)}"],
        hazard_fn=lambda state, mode, ctx: []
    )
    registry.register_component(
        "Scan",
        describe_fn=lambda state: [f"  size: {state.get('size', 0)}"],
        hazard_fn=lambda state, mode, ctx: []
    )
    
    setup_modes = make_controller(
        tmp_path,
        monkeypatch,
        {
            "Laser": DummySetupModeController("Laser", {}, []),
            "Scan": DummySetupModeController("Scan", {}, []),
        },
        state_registry=registry
    )
    
    old_state = {
        "Laser": {"power": 100},
        "Scan": {"size": 10},
    }
    new_state = {
        "Laser": {"power": 200},
        "Scan": {"size": 10},
    }
    
    diff = setup_modes.diffModeComponents(old_state, new_state)
    
    # Laser changed
    assert "Laser" in diff
    assert len(diff["Laser"]) > 0
    
    # Scan unchanged (should not be in diff or empty)
    assert "Scan" not in diff or len(diff["Scan"]) == 0
