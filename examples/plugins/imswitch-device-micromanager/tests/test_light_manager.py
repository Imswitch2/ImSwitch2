"""MicroManagerLightManager against the real MMCore and the DemoCamera adapters (no mocks).

Demo devices used (``MMConfig_demo.cfg`` plus what the tests load):

- ``LED Shutter``: a Shutter wrapping the State device ``LED``
  (labels ``Closed, 385nm, 470nm, 550nm, 635nm, State-5 ...``);
- ``White Light Shutter``: a ``DShutter`` Shutter with the property ``State`` ('0' / '1');
- ``DA`` (loaded by the tests from ``DemoCamera`` / ``D-DA``): SignalIO with the numeric
  property ``Voltage`` (limits 0 to 10 V) and the read-only ``Real Voltage``;
- ``Camera``: ``Binning`` (allowed '1', '2', '4', '8') and ``Gain`` (an integer, limits -5 to 8),
  used as the "same device for switch and power" case.
"""
import pytest

from imswitch.imcontrol.model.SetupInfo import LaserInfo, RS232Info
from imswitch.imcontrol.model.devices.graph import DeviceRelationKind, DeviceRole
from imswitch.imcontrol.model.devices.status import (
    DeviceConnectionState, DeviceNotConnectedError, DeviceRuntimeMode,
)
from imswitch.imcontrol.model.managers.lasers.LaserManager import RawDriveError
from imswitch_device_micromanager.core import MicroManagerCoreManager
from imswitch_device_micromanager.light import MicroManagerLightManager
from mm_helpers import DEMO_CONFIG

LED_SHUTTER, WHITE_SHUTTER, LED_STATE = "LED Shutter", "White Light Shutter", "LED"
DA = "DA"
#: The demo DA's ``Voltage`` limits (Micro-Manager's own, asserted below so a change shows up here).
DA_MIN_V, DA_MAX_V = 0.0, 10.0
SHUTTER = {"kind": "shutter"}
VOLTAGE = {"label": DA, "property": "Voltage", "units": "V", "decimals": 2}


# ------------------------------------------------------------------- helpers
def make_hub(adapter_dir, **properties):
    properties.setdefault("adapterPaths", [adapter_dir])
    properties.setdefault("configFile", DEMO_CONFIG)
    return MicroManagerCoreManager(
        RS232Info(managerName="micromanager.core", managerProperties=properties), "MMCore")


def laser_info(label=LED_SHUTTER, enable=SHUTTER, power=None, valueRangeMin=None, valueRangeMax=None,
               info_fields=None, **properties):
    properties = {"rs232device": "MMCore", "label": label, "enable": enable, **properties}
    if power is not None:
        properties["power"] = power
    return LaserInfo(managerName="micromanager.light", managerProperties=properties, wavelength=470,
                     valueRangeMin=valueRangeMin, valueRangeMax=valueRangeMax, **(info_fields or {}))


def make_light(hub, name="Light", **kwargs):
    return MicroManagerLightManager(laser_info(**kwargs), name, rs232sManager={"MMCore": hub})


def make_powered(hub, power=VOLTAGE, valueRangeMin=0.0, valueRangeMax=5.0, **kwargs):
    """A light whose output is the demo DA's ``Voltage``; the DA is loaded into the hub's core first."""
    if not hub.host.has_device(DA):
        hub.host.load_device(DA, "DemoCamera", "D-DA")
    return make_light(hub, power=power, valueRangeMin=valueRangeMin, valueRangeMax=valueRangeMax, **kwargs)


def core_of(hub):
    return hub.host.core


def shutter_open(hub, label=LED_SHUTTER):
    return core_of(hub).getShutterOpen(label)


@pytest.fixture
def hub(adapter_dir):
    h = make_hub(adapter_dir)
    yield h
    h.finalize()


@pytest.fixture
def led(hub):
    light = make_light(hub, name="LED")
    yield light
    light.finalize()


# ---------------------------------------------------------------- demo facts
def test_the_demo_devices_are_what_these_tests_assume(hub):
    core = core_of(hub)
    assert core.getDeviceType(LED_SHUTTER).name == "ShutterDevice"
    assert core.getDeviceType(LED_STATE).name == "StateDevice"
    assert list(core.getStateLabels(LED_STATE))[:3] == ["Closed", "385nm", "470nm"]
    hub.host.load_device(DA, "DemoCamera", "D-DA")
    assert (core.getPropertyLowerLimit(DA, "Voltage"), core.getPropertyUpperLimit(DA, "Voltage")) == (DA_MIN_V, DA_MAX_V)


# ------------------------------------------------------------- shutter kind
def test_shutter_kind_is_a_binary_laser_that_opens_and_closes_the_shutter(hub):
    light = make_light(hub, name="LED")
    try:
        assert light.isBinary and not light.isModulated and not light.supportsRawDrive
        assert light.valueUnits == "" and light.valueDecimals == 0 and light.wavelength == 470
        assert light.name == "LED" and light.label == LED_SHUTTER and light.hub is hub
        assert light.connectionState is DeviceConnectionState.CONNECTED and light.backendIsReal
        assert not shutter_open(hub)
        assert light.setEnabled(True) is True and shutter_open(hub)
        assert light.setEnabled(False) is True and not shutter_open(hub)
        assert light.lastError is None
    finally:
        light.finalize()


def test_shutter_kind_on_a_plain_shutter_device(hub):
    light = make_light(hub, label=WHITE_SHUTTER)
    try:
        assert light.setEnabled(True) and shutter_open(hub, WHITE_SHUTTER)
        assert core_of(hub).getProperty(WHITE_SHUTTER, "State") == "1"
        assert light.setEnabled(False) and not shutter_open(hub, WHITE_SHUTTER)
    finally:
        light.finalize()


def test_creating_a_light_does_not_touch_the_hardware(hub):
    core_of(hub).setShutterOpen(WHITE_SHUTTER, True)
    light = make_light(hub, label=WHITE_SHUTTER)
    try:
        assert shutter_open(hub, WHITE_SHUTTER)             # the laser controller switches off at startup, not us
    finally:
        light.finalize()


def test_shutter_kind_refuses_a_state_device_naming_the_type(hub):
    with pytest.raises(ValueError, match=r"laser 'Light': Micro-Manager device 'LED' is a State, not a Shutter "
                                         r"\(managerProperties.enable.kind is 'shutter'\)"):
        make_light(hub, label=LED_STATE)
    with pytest.raises(ValueError, match="'Camera' is a Camera, not a Shutter"):
        make_light(hub, label="Camera")


# --------------------------------------------------------------- state kind
def test_state_kind_with_labels(hub):
    light = make_light(hub, label=LED_STATE, enable={"kind": "state", "on": "470nm", "off": "Closed"})
    try:
        core = core_of(hub)
        assert light.isBinary
        assert light.setEnabled(True) and core.getStateLabel(LED_STATE) == "470nm"
        assert light.setEnabled(False) and core.getStateLabel(LED_STATE) == "Closed"
    finally:
        light.finalize()


def test_state_kind_with_indices(hub):
    light = make_light(hub, label=LED_STATE, enable={"kind": "state", "on": 3, "off": 0})
    try:
        core = core_of(hub)
        assert light.setEnabled(True) and core.getState(LED_STATE) == 3 and core.getStateLabel(LED_STATE) == "550nm"
        assert light.setEnabled(False) and core.getState(LED_STATE) == 0
    finally:
        light.finalize()


def test_state_kind_refuses_an_unknown_label_listing_the_labels(hub):
    with pytest.raises(ValueError) as excinfo:
        make_light(hub, label=LED_STATE, enable={"kind": "state", "on": "470 nm", "off": "Closed"})
    message = str(excinfo.value)
    assert "no state labelled '470 nm' (enable.on)" in message and "Did you mean '470nm'?" in message
    assert "'385nm'" in message and "'Closed'" in message


def test_state_kind_refuses_an_index_the_device_does_not_have(hub):
    with pytest.raises(ValueError, match=r"has 10 states \(0 to 9\); enable.on is 10"):
        make_light(hub, label=LED_STATE, enable={"kind": "state", "on": 10, "off": 0})


def test_state_kind_refuses_the_same_position_for_on_and_off(hub):
    with pytest.raises(ValueError, match=r"enable.on 'Closed' and enable.off 0 are both state 0"):
        make_light(hub, label=LED_STATE, enable={"kind": "state", "on": "Closed", "off": 0})


def test_state_kind_refuses_a_shutter_naming_the_type(hub):
    with pytest.raises(ValueError, match=r"'LED Shutter' is a Shutter, not a State device"):
        make_light(hub, enable={"kind": "state", "on": 1, "off": 0})


def test_a_state_light_is_dark_in_the_position_that_is_not_on(hub):
    """A position that is neither on nor off is not "emitting" either: switching off still goes to ``off``."""
    light = make_light(hub, label=LED_STATE, enable={"kind": "state", "on": "470nm", "off": "Closed"})
    try:
        core_of(hub).setState(LED_STATE, 4)                  # someone moved the wheel to 635nm
        assert light._read_enabled(core_of(hub)) is None
        assert light.setEnabled(False) and core_of(hub).getStateLabel(LED_STATE) == "Closed"
    finally:
        light.finalize()


# ----------------------------------------------------------- property kind
def test_property_kind_writes_the_value_pair(hub):
    light = make_light(hub, label=WHITE_SHUTTER, enable={"kind": "property", "name": "State", "on": "1", "off": "0"})
    try:
        assert light.isBinary
        assert light.setEnabled(True) and core_of(hub).getProperty(WHITE_SHUTTER, "State") == "1"
        assert shutter_open(hub, WHITE_SHUTTER)
        assert light.setEnabled(False) and core_of(hub).getProperty(WHITE_SHUTTER, "State") == "0"
    finally:
        light.finalize()


def test_property_kind_takes_numbers_for_an_allowed_value_list(hub):
    light = make_light(hub, label=WHITE_SHUTTER, enable={"kind": "property", "name": "State", "on": 1, "off": 0.0})
    try:
        assert light.setEnabled(True) and core_of(hub).getProperty(WHITE_SHUTTER, "State") == "1"
        assert light.setEnabled(False) and core_of(hub).getProperty(WHITE_SHUTTER, "State") == "0"
    finally:
        light.finalize()


def test_property_kind_refuses_a_value_the_property_does_not_allow(hub):
    with pytest.raises(ValueError, match=r"'5' \(enable.on\) is not an allowed value of property 'State' of "
                                         r"'White Light Shutter'; allowed: \['0', '1'\]"):
        make_light(hub, label=WHITE_SHUTTER, enable={"kind": "property", "name": "State", "on": "5", "off": "0"})


def test_property_kind_refuses_an_unknown_property_with_a_suggestion(hub):
    with pytest.raises(ValueError, match=r"no property 'Stat'\. Did you mean 'State'\?"):
        make_light(hub, label=WHITE_SHUTTER, enable={"kind": "property", "name": "Stat", "on": "1", "off": "0"})


def test_property_kind_refuses_a_read_only_property(hub):
    hub.host.load_device(DA, "DemoCamera", "D-DA")
    with pytest.raises(ValueError, match="'Real Voltage' of 'DA' is read-only"):
        make_light(hub, label=DA, enable={"kind": "property", "name": "Real Voltage", "on": 1, "off": 0})


def test_property_kind_refuses_the_same_value_for_on_and_off(hub):
    with pytest.raises(ValueError, match="enable.on and enable.off are both '1'"):
        make_light(hub, label=WHITE_SHUTTER, enable={"kind": "property", "name": "State", "on": "1", "off": 1})


def test_property_kind_on_a_device_of_any_type_lists_every_loaded_device_for_a_typo(hub):
    with pytest.raises(ValueError) as excinfo:
        make_light(hub, label="White Light Shuter", enable={"kind": "property", "name": "State", "on": "1", "off": "0"})
    message = str(excinfo.value)
    assert "no device 'White Light Shuter'" in message and "Did you mean 'White Light Shutter'?" in message
    assert "'Camera'" in message and "inline" in message


# ------------------------------------------------------------ enable shape
@pytest.mark.parametrize("enable, message", [
    (None, "managerProperties.enable is required"),
    ({}, "enable.kind is None"),
    ({"kind": "relay"}, r"enable.kind is 'relay'; it must be one of \['shutter', 'state', 'property'\]"),
    ({"kind": "shutter", "on": 1}, r"does not take \['on'\]"),
    ({"kind": "state", "on": 1}, r"enable.off must be a state label"),
    ({"kind": "state", "on": True, "off": 0}, "enable.on must be a state label"),
    ({"kind": "state", "on": 1.5, "off": 0}, "enable.on must be a state label"),
    ({"kind": "state", "on": 1, "off": 0, "name": "State"}, r"does not take \['name'\]"),
    ({"kind": "property", "on": "1", "off": "0"}, "enable.name must be the property to write"),
    ({"kind": "property", "name": "State", "on": None, "off": "0"}, "enable.on must be a string or a number"),
    ({"kind": "property", "name": "State", "on": True, "off": "0"}, "enable.on must be a string or a number"),
])
def test_a_malformed_enable_block_is_refused_before_anything_loads(hub, enable, message):
    with pytest.raises(ValueError, match=message):
        make_light(hub, enable=enable)


# --------------------------------------------------------------------- power
def test_power_sets_the_property_with_the_configured_decimals(hub):
    light = make_powered(hub)
    try:
        core = core_of(hub)
        assert not light.isBinary and light.supportsRawDrive
        assert light.valueUnits == "V" and light.valueDecimals == 2
        assert (light.valueRangeMin, light.valueRangeMax) == (0.0, 5.0)
        assert light.setValue(3.5) is True
        assert float(core.getProperty(DA, "Voltage")) == 3.5
        assert light.setValue(3.14159) is True                      # written as '3.14'
        assert float(core.getProperty(DA, "Voltage")) == 3.14
        assert light.setValue(0) is True and float(core.getProperty(DA, "Voltage")) == 0.0
        assert not shutter_open(hub)                                # power is not emission
        assert light.setEnabled(True) and shutter_open(hub)
    finally:
        light.finalize()


def test_power_writes_exactly_the_formatted_text(hub):
    light = make_powered(hub, power={**VOLTAGE, "decimals": 0})
    try:
        assert light.valueDecimals == 0
        assert light.applyRawDrive(2.4) == 2.0                      # '2'
        assert light.applyRawDrive(2.6) == 3.0                      # '3'
    finally:
        light.finalize()


def test_power_on_the_same_device_as_the_switch_defaults_its_label(hub):
    """``power.label`` defaults to ``label``: here the Camera is both (a real device with an integer property)."""
    light = make_light(
        hub, label="Camera", enable={"kind": "property", "name": "Binning", "on": "2", "off": "1"},
        power={"property": "Gain", "units": "dB", "decimals": 0}, valueRangeMin=0, valueRangeMax=8)
    try:
        core = core_of(hub)
        assert light.setEnabled(True) and core.getProperty("Camera", "Binning") == "2"
        assert light.setValue(6) is True and core.getProperty("Camera", "Gain") == "6"
        assert light.applyEnabled(False) is True and core.getProperty("Camera", "Binning") == "1"
    finally:
        light.finalize()


def test_power_in_inline_mode_loads_only_the_switch_device(hub):
    hub.host.load_device(DA, "DemoCamera", "D-DA")
    light = make_light(
        hub, label="Shutter2", adapter="DemoCamera", device="DShutter", power=VOLTAGE,
        valueRangeMin=0.0, valueRangeMax=5.0)
    try:
        assert "Shutter2" in hub.host.call("getLoadedDevices")
        assert light.setEnabled(True) and shutter_open(hub, "Shutter2")
        assert light.setValue(1.25) and float(core_of(hub).getProperty(DA, "Voltage")) == 1.25
    finally:
        light.finalize()
    assert "Shutter2" not in hub.host.call("getLoadedDevices") and DA in hub.host.call("getLoadedDevices")


def test_a_setup_range_above_micro_managers_limit_is_refused_with_both_ranges(hub):
    with pytest.raises(ValueError) as excinfo:
        make_powered(hub, valueRangeMin=0.0, valueRangeMax=12.0)
    message = str(excinfo.value)
    assert "accepts [0, 10] in Micro-Manager" in message and "[0, 12]" in message and "'Voltage'" in message


def test_a_setup_range_below_micro_managers_limit_is_refused(hub):
    with pytest.raises(ValueError, match=r"accepts \[0, 10\] in Micro-Manager.*\[-1, 5\]"):
        make_powered(hub, valueRangeMin=-1.0, valueRangeMax=5.0)


def test_the_setup_range_may_be_narrower_than_micro_managers_and_is_enforced(hub):
    light = make_powered(hub, valueRangeMin=1.0, valueRangeMax=5.0)
    try:
        core = core_of(hub)
        assert light.setValue(2.0)
        for outside in (0.5, 5.5, 9.0, float("nan"), float("inf"), "3"):
            assert light.setValue(outside) is False                 # Micro-Manager would take 5.5 and 9: we do not
            assert "outside the setup range" in light.lastError or "not a finite number" in light.lastError
        assert float(core.getProperty(DA, "Voltage")) == 2.0        # nothing was written
        with pytest.raises(RawDriveError, match="outside the setup range"):
            light.applyRawDrive(5.5)
        assert float(core.getProperty(DA, "Voltage")) == 2.0
    finally:
        light.finalize()


@pytest.mark.parametrize("lo, hi", [(None, None), (0.0, None), (None, 5.0), (5.0, 5.0), (6.0, 5.0), ("0", "5")])
def test_power_needs_a_numeric_setup_range(hub, lo, hi):
    with pytest.raises(ValueError, match="needs numeric valueRangeMin < valueRangeMax"):
        make_powered(hub, valueRangeMin=lo, valueRangeMax=hi)


@pytest.mark.parametrize("power, message", [
    ({"property": "Voltage", "decimals": 2}, "power.units is required"),
    ({"property": "Voltage", "units": "", "decimals": 2}, "power.units is required"),
    ({"property": "Voltage", "units": "V"}, "power.decimals is required"),
    ({"property": "Voltage", "units": "V", "decimals": -1}, "power.decimals is required"),
    ({"property": "Voltage", "units": "V", "decimals": 1.5}, "power.decimals is required"),
    ({"property": "Voltage", "units": "V", "decimals": True}, "power.decimals is required"),
    ({"units": "V", "decimals": 2}, "power.property must be the numeric property"),
    ({"property": "Voltage", "units": "V", "decimals": 2, "scale": 2}, r"does not take \['scale'\]"),
])
def test_units_and_decimals_are_required_with_power_and_never_guessed(hub, power, message):
    with pytest.raises(ValueError, match=message):
        make_powered(hub, power={"label": DA, **power})


def test_power_property_must_exist_be_numeric_and_writable(hub):
    hub.host.load_device(DA, "DemoCamera", "D-DA")
    cases = [
        ({"label": DA, "property": "Volts", "units": "V", "decimals": 2}, r"no property 'Volts'\. Did you mean 'Voltage'\?"),
        ({"label": DA, "property": "Real Voltage", "units": "V", "decimals": 2}, "is read-only"),
        ({"label": "Camera", "property": "PixelType", "units": "bits", "decimals": 0}, "is not numeric"),
        ({"label": "Nope", "property": "Voltage", "units": "V", "decimals": 2}, "no device 'Nope'"),
        ({"label": "Camera", "property": "Gain", "units": "dB", "decimals": 1}, "integer property; power.decimals must be 0"),
    ]
    for power, message in cases:
        with pytest.raises(ValueError, match=message):
            make_light(hub, power=power, valueRangeMin=0, valueRangeMax=5)


def test_a_failed_construction_leaves_an_inline_device_unloaded(hub):
    with pytest.raises(ValueError, match="power.units is required"):
        make_light(hub, label="Shutter2", adapter="DemoCamera", device="DShutter",
                   power={"label": DA, "property": "Voltage", "decimals": 2}, valueRangeMin=0, valueRangeMax=5)
    hub.host.load_device(DA, "DemoCamera", "D-DA")
    with pytest.raises(ValueError, match="accepts"):                 # fails after the inline load
        make_light(hub, label="Shutter2", adapter="DemoCamera", device="DShutter", power=VOLTAGE,
                   valueRangeMin=0, valueRangeMax=50)
    assert "Shutter2" not in hub.host.call("getLoadedDevices")


# ------------------------------------------------------------ checked commands
def test_apply_commands_return_what_the_device_shows(hub):
    light = make_powered(hub)
    try:
        assert light.applyRawDrive(3.5) == 3.5
        assert light.applyValue(4.25) == 4.25
        assert light.applyEnabled(True) is True and shutter_open(hub)
        assert light.applyEnabled(False) is True and not shutter_open(hub)
    finally:
        light.finalize()


def test_apply_raw_drive_raises_on_a_value_the_laser_refuses(hub):
    light = make_powered(hub)
    try:
        for bad in (6.0, -0.1, float("nan"), None, "x"):
            with pytest.raises(RawDriveError):
                light.applyRawDrive(bad)
        assert light.lastError
    finally:
        light.finalize()


def test_a_command_micro_manager_refuses_is_logged_and_false_but_raises_when_checked(hub, caplog):
    light = make_powered(hub)
    try:
        hub.host.unload_device(DA)                                    # the device is gone under the manager
        assert light.setValue(1.0) is False
        assert "RuntimeError" in light.lastError or "Error" in light.lastError
        with pytest.raises(RawDriveError, match="power 1.0 failed"):
            light.applyRawDrive(1.0)
        hub.host.unload_device(LED_SHUTTER)
        assert light.setEnabled(True) is False
        with pytest.raises(RawDriveError, match="switching on failed"):
            light.applyEnabled(True)
    finally:
        light.finalize()


def test_a_binary_light_has_no_power_command(led):
    assert not led.supportsRawDrive
    # The laser convention: setValue on a binary light is a no-op (the controller calls
    # setValue(0) on every laser at startup), while the checked raw-drive path refuses.
    assert led.setValue(1.0) is True and led.lastError is None
    with pytest.raises(RawDriveError, match="no power property"):
        led.applyRawDrive(1.0)


def test_apply_enabled_raises_when_the_device_does_not_show_the_new_state(hub, monkeypatch):
    """The demo devices always obey, so the "accepted but not shown" path is driven by a read-back that disagrees."""
    light = make_light(hub)
    try:
        monkeypatch.setattr(light, "_read_enabled", lambda core: None)
        with pytest.raises(RawDriveError, match="shows nothing definite"):
            light.applyEnabled(True)
        assert light.setEnabled(True) is False
    finally:
        monkeypatch.undo()
        light.finalize()


def test_the_laser_commands_are_guarded_by_the_resource_registry(led):
    for method in ("setEnabled", "setValue", "applyEnabled", "applyValue", "applyRawDrive"):
        assert getattr(type(led), method).__resource_guarded__


def test_a_light_whose_hub_went_away_refuses_commands(led, hub):
    hub.disconnectTransport()
    with pytest.raises(DeviceNotConnectedError):
        led.setEnabled(True)
    with pytest.raises(RawDriveError, match="refused"):
        led.applyEnabled(True)


# ------------------------------------------------------------------ lifecycle
def test_safe_state_turns_emission_off(hub):
    light = make_light(hub)
    try:
        light.setEnabled(True)
        assert light._lifecycleSafeState(verified=False) == [] and not shutter_open(hub)
        light.setEnabled(True)
        assert light._lifecycleSafeState(verified=True) == [] and not shutter_open(hub)
    finally:
        light.finalize()


def test_safe_state_reports_a_light_it_could_not_verify_off_only_when_verified(hub):
    light = make_light(hub)
    try:
        hub.host.unload_device(LED_SHUTTER)
        assert light._lifecycleSafeState(verified=False) == []
        errors = light._lifecycleSafeState(verified=True)
        assert errors and "emission could not be verified off" in errors[0]
    finally:
        light.finalize()


def test_safe_state_is_not_blocked_by_a_reservation(hub):
    from imswitch.imcontrol.model.resources import ResourceReservedError, get_resource_registry, laser_key

    light = make_light(hub, name="Reserved")
    try:
        light.setEnabled(True)
        registry = get_resource_registry()
        reservation = registry.reserve([laser_key("Reserved")], "calibration")
        try:
            with pytest.raises(ResourceReservedError):
                light.setEnabled(False)                                # the guard admits the owner only
            assert shutter_open(hub)
            assert light._lifecycleSafeState(verified=True) == [] and not shutter_open(hub)
        finally:
            registry.release(reservation.token)
    finally:
        light.finalize()


def test_reconnect_switches_emission_off_and_verifies_it(led, hub):
    led.setEnabled(True)
    assert hub.reconnectTransport() is True
    core_of(hub).setShutterOpen(LED_SHUTTER, True)                    # the reloaded core came up with the light on
    assert led._onTransportReconnected(True) == []
    assert not shutter_open(hub) and led.connectionState is DeviceConnectionState.CONNECTED
    assert led.setEnabled(True) and shutter_open(hub)                 # the new core is the one in use


def test_reconnect_with_a_light_that_does_not_read_back_off_is_an_error(led, hub, monkeypatch):
    assert hub.reconnectTransport() is True
    monkeypatch.setattr(led, "_read_enabled", lambda core: True)
    errors = led._onTransportReconnected(True)
    assert errors and "shows on" in errors[0]
    assert led.connectionState is DeviceConnectionState.ERROR


def test_reconnect_without_a_real_transport_returns_an_error(led):
    errors = led._onTransportReconnected(False)
    assert errors and isinstance(errors[0], str)
    assert led.connectionState is DeviceConnectionState.ERROR
    with pytest.raises(DeviceNotConnectedError):
        led.setEnabled(True)


def test_reconnect_reloads_an_inline_device(hub):
    light = make_light(hub, label="Shutter2", adapter="DemoCamera", device="DShutter")
    try:
        assert hub.reconnectTransport() is True
        assert "Shutter2" not in hub.host.call("getLoadedDevices")
        assert light._onTransportReconnected(True) == []
        assert "Shutter2" in hub.host.call("getLoadedDevices") and not shutter_open(hub, "Shutter2")
        assert light.setEnabled(True) and shutter_open(hub, "Shutter2")
    finally:
        light.finalize()


def test_reconnect_reverifies_the_enable_block_and_power_against_the_reloaded_core(hub, tmp_path):
    light = make_powered(hub)
    try:
        cfg = tmp_path / "other.cfg"
        cfg.write_text("Property,Core,Initialize,0\nDevice,Other,DemoCamera,DShutter\nProperty,Core,Initialize,1\n")
        hub.host.config_file = str(cfg)
        assert hub.reconnectTransport() is True
        errors = light._onTransportReconnected(True)
        assert errors and "no shutter device 'LED Shutter'" in errors[0] and "Other" in errors[0]
        assert light.connectionState is DeviceConnectionState.ERROR
        with pytest.raises(RuntimeError, match="no shutter device"):
            light._lifecycleReinitialise()
    finally:
        light.finalize()


def test_reconnect_reports_a_power_property_that_changed_under_it(hub):
    light = make_powered(hub)
    try:
        assert hub.reconnectTransport() is True                       # the DA was loaded by the test: gone now
        errors = light._onTransportReconnected(True)
        assert errors and "no device 'DA'" in errors[0]
    finally:
        light.finalize()


def test_finalize_switches_emission_off_and_unloads_an_inline_device(hub):
    light = make_light(hub, label="Shutter2", adapter="DemoCamera", device="DShutter")
    light.setEnabled(True)
    core = core_of(hub)
    assert core.getShutterOpen("Shutter2")
    light.finalize()
    assert "Shutter2" not in core.getLoadedDevices()
    assert light.connectionState is DeviceConnectionState.DISCONNECTED

    light = make_light(hub, label=WHITE_SHUTTER)
    light.setEnabled(True)
    light.finalize()
    assert not shutter_open(hub, WHITE_SHUTTER)                       # system-config device stays loaded, dark


def test_finalize_after_the_hub_is_gone_does_not_raise(hub):
    light = make_light(hub)
    hub.disconnectTransport()
    light.finalize()


# --------------------------------------------------------------- hub is down
def test_a_light_is_built_absent_when_the_hub_is_not_connected_and_adopts_it_later(adapter_dir, tmp_path):
    cfg = tmp_path / "late.cfg"
    hub = make_hub(adapter_dir, configFile=str(cfg))
    try:
        light = make_light(hub)
        assert light.connectionState is DeviceConnectionState.ERROR and not light.isUsable
        assert not light.backendIsReal and light.isBinary
        with pytest.raises(DeviceNotConnectedError):
            light.setEnabled(True)
        cfg.write_text(open(adapter_dir + "/" + DEMO_CONFIG).read())
        assert hub.reconnectTransport() is True
        assert light._onTransportReconnected(True) == []
        assert light.connectionState is DeviceConnectionState.CONNECTED and light.backendIsReal
        assert light.setEnabled(True) and shutter_open(hub)
        light.finalize()
    finally:
        hub.finalize()


def test_a_light_built_while_the_hub_was_down_refuses_the_wrong_device_type_when_it_adopts(adapter_dir, tmp_path):
    cfg = tmp_path / "late.cfg"
    hub = make_hub(adapter_dir, configFile=str(cfg))
    try:
        light = make_light(hub, label="Camera")
        cfg.write_text(open(adapter_dir + "/" + DEMO_CONFIG).read())
        assert hub.reconnectTransport() is True
        errors = light._onTransportReconnected(True)
        assert errors and "is a Camera, not a Shutter" in errors[0]
        assert light.connectionState is DeviceConnectionState.ERROR
    finally:
        hub.finalize()


def test_a_transient_light_stays_absent_until_connected(hub):
    light = make_light(hub, info_fields={"transient": True})
    try:
        assert light.runtimeMode is DeviceRuntimeMode.ABSENT and not light.backendIsReal
        with pytest.raises(DeviceNotConnectedError):
            light.setEnabled(True)
        result = light.getDeviceLifecycle().connect()
        assert result.success, result
        assert light.backendIsReal and light.setEnabled(True) and shutter_open(hub)
    finally:
        light.finalize()


# ------------------------------------------------------------------ descriptor
def test_descriptor_spec_is_a_laser_using_the_hub_transport(led):
    spec = led.getDeviceDescriptorSpec()
    assert spec.role is DeviceRole.PRIMARY and spec.category == "laser"
    assert spec.display_name == f"LED (Micro-Manager {LED_SHUTTER})"
    (dependency,) = spec.dependencies
    assert dependency.kind is DeviceRelationKind.USES_TRANSPORT
    assert (dependency.target.kind, dependency.target.name) == ("rs232", "MMCore")


def test_set_value_on_a_binary_light_is_a_no_op_not_an_error(hub, caplog):
    """LaserController.setValue(0) hits every laser at startup; a shutter-only light must not
    log an error for it (seen as 'setting power 0 failed: no power property' in the e2e log)."""
    import logging
    light = make_light(hub, label="LED Shutter", enable={"kind": "shutter"})
    try:
        with caplog.at_level(logging.ERROR):
            assert light.setValue(0) is True
        assert "setting power" not in caplog.text
        assert light.lastError is None
    finally:
        light.finalize()
