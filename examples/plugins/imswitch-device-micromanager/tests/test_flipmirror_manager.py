"""MicroManagerFlipMirrorManager against the real MMCore and the DemoCamera adapters (no mocks).

Demo devices used (``MMConfig_demo.cfg`` plus what the tests load):

- ``White Light Shutter``: a ``DShutter`` Shutter, presented as a switch (closed 0, open 1). The demo
  config leaves it OPEN; the ``hub`` fixture closes it, so every test starts from a known state;
- ``Flip`` (loaded inline from ``DemoCamera`` / ``DStateDevice`` with the pre-init property
  ``Number of positions`` = 2): a State device with exactly two positions, labels ``State-0`` and
  ``State-1`` until a test defines its own;
- ``Dichroic`` (a ``DWheel`` with 10 positions, labels ``400DCLP, Q505LP, Q585LP, 89402bs, State-4 ...``),
  ``Path`` (a ``DLightPath`` with 3 positions, ``State-0`` .. ``State-2``) and ``LED`` (10 positions):
  State devices with more than two positions, presented as N-position selectors.
"""
import pytest

from imswitch.imcontrol.model.SetupInfo import FlipMirrorInfo, RS232Info
from imswitch.imcontrol.model.devices.graph import DeviceRelationKind, DeviceRole
from imswitch.imcontrol.model.devices.lifecycle import BackendLifecycle
from imswitch.imcontrol.model.devices.status import DeviceConnectionState, DeviceNotConnectedError
from imswitch_device_micromanager._core import MMCoreHostError
from imswitch_device_micromanager.core import MicroManagerCoreManager
from imswitch_device_micromanager.flipmirror import MicroManagerFlipMirrorManager
from mm_helpers import DEMO_CONFIG

SHUTTER = "White Light Shutter"
FLIP = "Flip"
INLINE_TWO = {"label": FLIP, "adapter": "DemoCamera", "device": "DStateDevice",
              "preInitProperties": {"Number of positions": 2}}


# ------------------------------------------------------------------- helpers
def make_hub(adapter_dir, **properties):
    properties.setdefault("adapterPaths", [adapter_dir])
    properties.setdefault("configFile", DEMO_CONFIG)
    return MicroManagerCoreManager(
        RS232Info(managerName="micromanager.core", managerProperties=properties), "MMCore")


def flip_info(label=SHUTTER, invert=False, initial_state=None, state_names=None, **properties):
    return FlipMirrorInfo(
        managerName="micromanager.flip-mirror", invert=invert, initial_state=initial_state,
        state_names=state_names or {}, managerProperties={"rs232device": "MMCore", "label": label, **properties})


def make_flip(hub, name="Mirror", invert=False, initial_state=None, state_names=None, **properties):
    return MicroManagerFlipMirrorManager(
        flip_info(invert=invert, initial_state=initial_state, state_names=state_names, **properties),
        name, rs232sManager={"MMCore": hub})


def make_two_position(hub, name="Mirror", **kwargs):
    return make_flip(hub, name=name, **{**INLINE_TWO, **kwargs})


def core_of(hub):
    return hub.host.core


def shutter_open(hub, label=SHUTTER):
    return core_of(hub).getShutterOpen(label)


def write_cfg(hub, tmp_path, text, name="other.cfg"):
    cfg = tmp_path / name
    cfg.write_text(text)
    hub.host.config_file = str(cfg)


@pytest.fixture
def hub(adapter_dir):
    h = make_hub(adapter_dir)
    core_of(h).setShutterOpen(SHUTTER, False)
    yield h
    h.finalize()


@pytest.fixture
def shutter(hub):
    mirror = make_flip(hub, name="WhiteLight")
    yield mirror
    mirror.finalize()


@pytest.fixture
def flip(hub):
    mirror = make_two_position(hub)
    yield mirror
    mirror.finalize()


# ---------------------------------------------------------------- demo facts
def test_the_demo_devices_are_what_these_tests_assume(hub):
    core = core_of(hub)
    assert core.getDeviceType(SHUTTER).name == "ShutterDevice"
    for label, count in (("Dichroic", 10), ("Path", 3), ("LED", 10)):
        assert core.getDeviceType(label).name == "StateDevice" and core.getNumberOfStates(label) == count
    hub.host.load_device(FLIP, "DemoCamera", "DStateDevice", pre_init={"Number of positions": 2})
    assert core.getNumberOfStates(FLIP) == 2 and list(core.getStateLabels(FLIP)) == ["State-0", "State-1"]
    assert core.isPropertyPreInit(FLIP, "Number of positions")      # why the inline device needs preInitProperties


# ------------------------------------------------------------------ shutter
def test_a_shutter_is_a_switch_closed_is_0_and_open_is_1(hub):
    mirror = make_flip(hub, name="WhiteLight")
    try:
        assert mirror.name == "WhiteLight" and mirror.label == SHUTTER and mirror.hub is hub
        assert mirror.is_connected() and mirror.connectionState is DeviceConnectionState.CONNECTED
        assert mirror.get_state_names() == {0: "Closed", 1: "Open"}
        assert mirror.get_state() == 0 and mirror.get_cached_state() == 0 and not shutter_open(hub)
        assert mirror.move_to(1) is None and shutter_open(hub)
        assert mirror.get_state() == 1 and mirror.get_cached_state() == 1
        assert core_of(hub).getProperty(SHUTTER, "State") == "1"
        mirror.move_to(0)
        assert not shutter_open(hub) and mirror.get_state() == 0
        assert mirror.get_last_error() is None
    finally:
        mirror.finalize()


def test_state_reads_come_from_the_device_not_from_the_cache(shutter, hub):
    shutter.move_to(1)
    core_of(hub).setShutterOpen(SHUTTER, False)                       # somebody else closed it
    assert shutter.get_cached_state() == 1
    assert shutter.get_state() == 0 and shutter.get_cached_state() == 0


def test_invert_swaps_logical_and_hardware_states_and_the_names_follow_the_hardware(hub):
    mirror = make_flip(hub, invert=True)
    try:
        assert mirror.invert is True
        assert mirror.get_state_names() == {0: "Open", 1: "Closed"}   # logical 0 is the open shutter
        assert mirror.get_state() == 1                                # closed shutter = logical 1
        mirror.move_to(0)
        assert shutter_open(hub) and mirror.get_state() == 0
        mirror.move_to(1)
        assert not shutter_open(hub) and mirror.get_state() == 1
    finally:
        mirror.finalize()


def test_setup_state_names_win_per_state_and_the_other_keeps_micro_managers_word(hub):
    both = make_flip(hub, state_names={"0": "Dark", "1": "Bright"})
    try:
        assert both.get_state_names() == {0: "Dark", 1: "Bright"}
    finally:
        both.finalize()
    one = make_flip(hub, state_names={"1": "Bright"})
    try:
        assert one.get_state_names() == {0: "Closed", 1: "Bright"}
    finally:
        one.finalize()


def test_state_names_with_a_key_beyond_the_positions_are_refused(hub):
    with pytest.raises(ValueError, match=r"state_names has the key '2'; 'White Light Shutter' has 2 positions, "
                                         r"the states \['0', '1'\]"):
        make_flip(hub, state_names={"0": "a", "2": "b"})


@pytest.mark.parametrize("key", ["left", "-1", "1.5", True, None])
def test_state_names_keys_that_are_not_state_numbers_are_refused(hub, key):
    with pytest.raises(ValueError, match="the keys are state numbers"):
        make_flip(hub, state_names={key: "a"})


def test_creating_a_flip_mirror_does_not_move_it(hub):
    core_of(hub).setShutterOpen(SHUTTER, True)
    mirror = make_flip(hub)
    try:
        assert shutter_open(hub) and mirror.get_cached_state() == 1   # it reads where the device is
    finally:
        mirror.finalize()


def test_a_shutter_refuses_the_states_property(hub):
    with pytest.raises(ValueError, match=r"managerProperties.states is for State devices; 'White Light Shutter' is "
                                         r"a Shutter .*use invert"):
        make_flip(hub, states=[0, 1])


# ------------------------------------------------------------ two positions
def test_a_two_position_state_device_is_a_switch_named_by_its_state_labels(hub):
    hub.host.load_device(FLIP, "DemoCamera", "DStateDevice", pre_init={"Number of positions": 2})
    core = core_of(hub)
    core.defineStateLabel(FLIP, 0, "Eyepiece")
    core.defineStateLabel(FLIP, 1, "Camera")
    mirror = make_flip(hub, label=FLIP)
    try:
        assert mirror.get_state_names() == {0: "Eyepiece", 1: "Camera"}
        assert mirror.move_to(1) is None and core.getState(FLIP) == 1 and core.getStateLabel(FLIP) == "Camera"
        assert mirror.get_state() == 1
        mirror.move_to(0)
        assert core.getState(FLIP) == 0 and mirror.get_state() == 0
    finally:
        mirror.finalize()


def test_inline_two_position_device_is_loaded_and_unloaded_by_the_manager(hub):
    mirror = make_two_position(hub)
    assert FLIP in hub.host.call("getLoadedDevices") and mirror.get_state_names() == {0: "State-0", 1: "State-1"}
    mirror.move_to(1)
    mirror.finalize()
    assert FLIP not in hub.host.call("getLoadedDevices")


def test_invert_on_a_state_device(hub):
    mirror = make_two_position(hub, invert=True)
    try:
        assert mirror.get_state_names() == {0: "State-1", 1: "State-0"}
        mirror.move_to(0)
        assert core_of(hub).getState(FLIP) == 1 and mirror.get_state() == 0
    finally:
        mirror.finalize()


def test_states_orders_the_two_positions_by_label_or_index(hub):
    hub.host.load_device(FLIP, "DemoCamera", "DStateDevice", pre_init={"Number of positions": 2})
    core = core_of(hub)
    core.defineStateLabel(FLIP, 0, "A")
    core.defineStateLabel(FLIP, 1, "B")
    for states in (["B", "A"], [1, 0], ["B", 0]):
        mirror = make_flip(hub, label=FLIP, states=states)
        try:
            assert mirror.get_state_names() == {0: "B", 1: "A"}
            mirror.move_to(0)
            assert core.getState(FLIP) == 1 and mirror.get_state() == 0
            mirror.move_to(1)
            assert core.getState(FLIP) == 0 and mirror.get_state() == 1
        finally:
            mirror.finalize()


def test_states_and_invert_compose(hub):
    hub.host.load_device(FLIP, "DemoCamera", "DStateDevice", pre_init={"Number of positions": 2})
    mirror = make_flip(hub, label=FLIP, states=[1, 0], invert=True)    # swapped twice: the identity
    try:
        mirror.move_to(0)
        assert core_of(hub).getState(FLIP) == 0 and mirror.get_state_names() == {0: "State-0", 1: "State-1"}
    finally:
        mirror.finalize()


def test_states_refuses_an_unknown_label_with_a_suggestion_and_the_labels(hub):
    hub.host.load_device(FLIP, "DemoCamera", "DStateDevice", pre_init={"Number of positions": 2})
    with pytest.raises(ValueError) as excinfo:
        make_flip(hub, label=FLIP, states=["State-1", "State 0"])
    message = str(excinfo.value)
    assert "no state labelled 'State 0'" in message and "Did you mean 'State-0'?" in message and "'State-1'" in message


def test_states_refuses_an_index_the_device_does_not_have(hub):
    hub.host.load_device(FLIP, "DemoCamera", "DStateDevice", pre_init={"Number of positions": 2})
    with pytest.raises(ValueError, match=r"has 2 positions \(0 to 1\); managerProperties.states has 2"):
        make_flip(hub, label=FLIP, states=[0, 2])


def test_states_naming_one_position_twice_is_refused(hub):
    hub.host.load_device(FLIP, "DemoCamera", "DStateDevice", pre_init={"Number of positions": 2})
    with pytest.raises(ValueError, match=r"are both position 0 of 'Flip'"):
        make_flip(hub, label=FLIP, states=["State-0", 0])
    with pytest.raises(ValueError, match="names the same position twice"):
        make_flip(hub, label=FLIP, states=[0, 0])


@pytest.mark.parametrize("states, message", [
    ([0], "exactly 2 Micro-Manager positions"),
    ([0, 1, 0], "exactly 2 Micro-Manager positions"),
    ("01", "exactly 2 Micro-Manager positions"),
    ([0, -1], "entries must be a state label"),
    ([0, 1.5], "entries must be a state label"),
    ([True, 0], "entries must be a state label"),
    ([0, ""], "entries must be a state label"),
])
def test_a_malformed_states_property_is_refused_before_anything_loads(hub, states, message):
    with pytest.raises(ValueError, match=message):
        make_two_position(hub, states=states)
    assert FLIP not in hub.host.call("getLoadedDevices")


# ------------------------------------------------------------ N positions
DICHROIC_LABELS = ["400DCLP", "Q505LP", "Q585LP", "89402bs"] + [f"State-{i}" for i in range(4, 10)]


def test_a_ten_position_wheel_exposes_every_position_named_by_micro_managers_labels(hub):
    mirror = make_flip(hub, label="Dichroic")
    try:
        names = mirror.get_state_names()
        assert names == dict(enumerate(DICHROIC_LABELS)) and len(names) == 10
        assert mirror.get_state() == 0 and mirror.get_cached_state() == 0
        assert mirror.move_to(3) is None
        assert core_of(hub).getState("Dichroic") == 3 and core_of(hub).getStateLabel("Dichroic") == "89402bs"
        assert mirror.get_state() == 3 and mirror.get_cached_state() == 3 and mirror.get_last_error() is None
        mirror.move_to(9)
        assert core_of(hub).getState("Dichroic") == 9 and mirror.get_state() == 9
        mirror.move_to("0")                                           # numeric text like the other flip mirrors
        assert core_of(hub).getState("Dichroic") == 0
    finally:
        mirror.finalize()


def test_a_three_position_light_path_and_a_ten_position_led_are_selectors_too(hub):
    for label, count in (("Path", 3), ("LED", 10)):
        mirror = make_flip(hub, label=label)
        try:
            assert list(mirror.get_state_names()) == list(range(count))
            mirror.move_to(count - 1)
            assert core_of(hub).getState(label) == count - 1 and mirror.get_state() == count - 1
        finally:
            mirror.finalize()
    path = make_flip(hub, label="Path")
    try:
        assert path.get_state_names() == {0: "State-0", 1: "State-1", 2: "State-2"}
    finally:
        path.finalize()


def test_state_names_override_per_position_and_the_rest_keep_micro_managers_labels(hub):
    mirror = make_flip(hub, label="Dichroic", state_names={"0": "Open", "9": "Dark"})
    try:
        names = mirror.get_state_names()
        assert names[0] == "Open" and names[9] == "Dark" and names[1] == "Q505LP" and names[8] == "State-8"
    finally:
        mirror.finalize()


def test_a_state_number_beyond_the_positions_is_refused_and_nothing_moves(hub):
    mirror = make_flip(hub, label="Dichroic")
    try:
        mirror.move_to(2)
        for state in (10, -1, "x", None):
            with pytest.raises((ValueError, TypeError)):
                mirror.move_to(state)
        assert core_of(hub).getState("Dichroic") == 2
        with pytest.raises(ValueError, match=r"a position 0 to 9, got 10"):
            mirror.move_to(10)
    finally:
        mirror.finalize()


def test_a_wheel_move_the_device_does_not_show_is_a_failed_move(hub, monkeypatch):
    mirror = make_flip(hub, label="Dichroic")
    try:
        monkeypatch.setattr(mirror, "_read_slot", lambda core: 0)
        with pytest.raises(RuntimeError, match=r"was told to move to state 4 but shows state 0"):
            mirror.move_to(4)
        assert mirror.get_cached_state() == 0
    finally:
        mirror.finalize()


def test_an_inline_three_position_device_is_loaded_exposed_and_unloaded(hub):
    mirror = make_flip(hub, **{**INLINE_TWO, "preInitProperties": {"Number of positions": 3}})
    try:
        assert FLIP in hub.host.call("getLoadedDevices")
        assert mirror.get_state_names() == {0: "State-0", 1: "State-1", 2: "State-2"}
        mirror.move_to(2)
        assert core_of(hub).getState(FLIP) == 2
    finally:
        mirror.finalize()
    assert FLIP not in hub.host.call("getLoadedDevices")


def test_invert_is_refused_for_more_than_two_positions(hub):
    with pytest.raises(ValueError, match=r"invert swaps the two states of a two-state device; 'Dichroic' has 10 positions"):
        make_flip(hub, label="Dichroic", invert=True)
    with pytest.raises(ValueError, match="invert swaps the two states"):
        make_flip(hub, **{**INLINE_TWO, "preInitProperties": {"Number of positions": 3}}, invert=True)
    assert FLIP not in hub.host.call("getLoadedDevices")             # the inline device is unloaded again


def test_initial_state_is_checked_against_the_position_count(hub):
    with pytest.raises(ValueError, match=r"initial_state 10 is not a position of 'Dichroic', which has 10 positions \(0 to 9\)"):
        make_flip(hub, label="Dichroic", initial_state=10)
    with pytest.raises(ValueError, match=r"initial_state 2 is not a position of 'White Light Shutter', which has 2 positions"):
        make_flip(hub, initial_state=2)
    mirror = make_flip(hub, label="Dichroic", initial_state=9)
    try:
        assert core_of(hub).getState("Dichroic") == 9 and mirror.get_cached_state() == 9
    finally:
        mirror.finalize()


def test_state_names_beyond_the_wheel_are_refused(hub):
    with pytest.raises(ValueError, match=r"state_names has the key '10'; 'Dichroic' has 10 positions"):
        make_flip(hub, label="Dichroic", state_names={"10": "x"})


def test_states_is_refused_for_a_device_with_more_than_two_positions(hub):
    for states in ([0, 1], ["400DCLP", "Q505LP"]):
        with pytest.raises(ValueError) as excinfo:
            make_flip(hub, label="Dichroic", states=states)
        message = str(excinfo.value)
        assert "only orders the two positions of a two-position device" in message
        assert "'Dichroic' has 10 positions and every position is exposed" in message
    with pytest.raises(ValueError, match="every position is exposed"):
        make_flip(hub, **{**INLINE_TWO, "preInitProperties": {"Number of positions": 3}}, states=[0, 1])
    assert FLIP not in hub.host.call("getLoadedDevices")


def test_a_one_position_device_is_refused_with_its_count(hub):
    with pytest.raises(ValueError, match=r"'Flip' has 1 positions; a flip mirror needs at least 2"):
        make_flip(hub, **{**INLINE_TWO, "preInitProperties": {"Number of positions": 1}})
    assert FLIP not in hub.host.call("getLoadedDevices")             # refused after the load: unloaded again


# ------------------------------------------------------------------ refusals
def test_a_device_of_another_type_is_refused_naming_its_type(hub):
    with pytest.raises(ValueError, match=r"flip_mirror 'Mirror': Micro-Manager device 'Camera' is a Camera, "
                                         r"not a State or Shutter"):
        make_flip(hub, label="Camera")
    with pytest.raises(ValueError, match="'Z' is a Stage, not a State or Shutter"):
        make_flip(hub, label="Z")


def test_a_label_typo_lists_the_loaded_state_and_shutter_devices(hub):
    with pytest.raises(ValueError) as excinfo:
        make_flip(hub, label="White Light Shuter")
    message = str(excinfo.value)
    assert "no State or Shutter device 'White Light Shuter'" in message and "Did you mean 'White Light Shutter'?" in message
    assert "'LED'" in message and "'LED Shutter'" in message and "'Camera'" not in message


# --------------------------------------------------------------- initial state
def test_initial_state_moves_the_mirror_at_startup(hub):
    mirror = make_flip(hub, initial_state=1)
    try:
        assert shutter_open(hub) and mirror.get_cached_state() == 1
    finally:
        mirror.finalize()


def test_initial_state_is_a_logical_state_so_invert_applies(hub):
    mirror = make_flip(hub, initial_state=0, invert=True)
    try:
        assert shutter_open(hub)                                        # logical 0 of an inverted shutter is open
    finally:
        mirror.finalize()


def test_no_initial_state_leaves_the_mirror_where_it_is(hub):
    core_of(hub).setShutterOpen(SHUTTER, True)
    mirror = make_flip(hub, initial_state=None)
    mirror.finalize()
    assert shutter_open(hub)


@pytest.mark.parametrize("value", [-1, 1.5, "1", True])
def test_an_initial_state_that_is_not_a_state_number_is_refused(hub, value):
    with pytest.raises(ValueError, match=r"initial_state must be a state number \(0, 1, ...\) or null"):
        make_flip(hub, initial_state=value)


def test_a_failed_initial_move_is_a_connection_error_not_a_crash(hub, monkeypatch):
    monkeypatch.setattr(MicroManagerFlipMirrorManager, "_read_slot", lambda self, core: 0)   # never shows the move
    mirror = make_flip(hub, initial_state=1)
    try:
        assert mirror.connectionState is DeviceConnectionState.ERROR and not mirror.is_connected()
        assert "initial_state 1 could not be applied" in mirror.connectionStatusSummary
        assert "shows state 0" in mirror.get_last_error()
    finally:
        mirror.finalize()


# ------------------------------------------------------------------ commands
@pytest.mark.parametrize("state", [2, -1, "x", None])
def test_a_state_beyond_a_two_state_device_is_refused_and_nothing_moves(shutter, hub, state):
    with pytest.raises((ValueError, TypeError)):
        shutter.move_to(state)
    assert not shutter_open(hub)


def test_move_to_accepts_numeric_text_like_the_other_flip_mirrors(shutter, hub):
    shutter.move_to("1")
    assert shutter_open(hub)


def test_a_move_the_device_does_not_show_is_a_failed_move(shutter, hub, monkeypatch):
    monkeypatch.setattr(shutter, "_read_slot", lambda core: 0)
    with pytest.raises(RuntimeError, match=r"'White Light Shutter' was told to move to state 1 but shows state 0"):
        shutter.move_to(1)
    assert "told to move to state 1" in shutter.get_last_error()
    assert shutter.get_cached_state() == 0                            # the failed move did not update the cache
    assert shutter.is_connected()                                     # a refused command is not a lost connection


def test_a_failed_command_sets_the_last_error_and_the_next_good_one_clears_it(hub):
    mirror = make_two_position(hub)
    try:
        hub.host.unload_device(FLIP)                                  # the device is gone under the manager
        with pytest.raises(Exception):
            mirror.move_to(1)
        assert mirror.get_last_error() and mirror.is_connected()
        hub.host.load_device(FLIP, "DemoCamera", "DStateDevice", pre_init={"Number of positions": 2})
        mirror.move_to(1)
        assert mirror.get_last_error() is None
    finally:
        mirror.finalize()


def test_a_flip_mirror_whose_hub_went_away_refuses_commands(shutter, hub):
    hub.disconnectTransport()
    assert not shutter.is_connected()
    with pytest.raises(DeviceNotConnectedError):
        shutter.move_to(1)
    with pytest.raises(DeviceNotConnectedError):
        shutter.get_state()
    assert shutter.get_cached_state() == 0                            # the cache stays readable


def test_waiting_for_a_busy_device_polls_outside_the_hub_lock_and_times_out(shutter, hub, monkeypatch):
    core = core_of(hub)
    core.setTimeoutMs(50)
    calls = []

    def always_busy(label):
        calls.append(label)
        return True

    monkeypatch.setattr(core, "deviceBusy", always_busy)
    with pytest.raises(MMCoreHostError, match=r"flip_mirror 'WhiteLight': device 'White Light Shutter' still busy after 0\.1 s"):
        shutter.move_to(1)
    assert len(calls) >= 2


# ------------------------------------------------------- startup without a hub
def test_a_hub_that_is_down_leaves_the_flip_mirror_absent_but_buildable(adapter_dir, tmp_path):
    hub = make_hub(adapter_dir, configFile=str(tmp_path / "nope.cfg"))
    try:
        mirror = make_flip(hub, state_names={"1": "Bright"})
        assert not mirror.is_connected() and mirror.connectionState is DeviceConnectionState.ERROR
        assert mirror.get_cached_state() is None
        assert mirror.get_state_names() == {0: "0", 1: "Bright"}      # unknown labels are not invented
        assert "nope.cfg" in mirror.get_last_error()
        with pytest.raises(DeviceNotConnectedError):
            mirror.move_to(1)
        mirror.finalize()
    finally:
        hub.finalize()


# ------------------------------------------------------------------ lifecycle
def test_safe_state_moves_nothing(shutter, hub):
    shutter.move_to(1)
    assert shutter._lifecycleSafeState(verified=False) == [] and shutter._lifecycleSafeState(verified=True) == []
    assert shutter_open(hub)


def test_the_default_backend_lifecycle_can_reconnect(shutter):
    lifecycle = shutter.getDeviceLifecycle()
    assert isinstance(lifecycle, BackendLifecycle) and lifecycle.capabilities.reconnect
    assert not lifecycle.capabilities.connect                         # not a transient device


def test_reconnect_rereads_where_the_mirror_is_and_follows_the_new_core(shutter, hub):
    shutter.move_to(1)
    assert hub.reconnectTransport() is True
    core = core_of(hub)
    core.setShutterOpen(SHUTTER, False)                               # the reloaded core came up its own way
    assert shutter.get_cached_state() == 1                            # stale until the hook ran
    assert shutter._onTransportReconnected(True) == []
    assert shutter.get_cached_state() == 0 and shutter.connectionState is DeviceConnectionState.CONNECTED
    assert shutter.is_connected()
    shutter.move_to(1)                                                # the new core is the one in use
    assert core.getShutterOpen(SHUTTER)


def test_reconnect_does_not_replay_initial_state(hub):
    mirror = make_flip(hub, initial_state=1)
    try:
        assert hub.reconnectTransport() is True
        core_of(hub).setShutterOpen(SHUTTER, False)
        assert mirror._onTransportReconnected(True) == []
        assert not shutter_open(hub) and mirror.get_cached_state() == 0     # observed, not moved
    finally:
        mirror.finalize()


def test_reconnect_reloads_an_inline_device(hub):
    mirror = make_two_position(hub)
    try:
        mirror.move_to(1)
        assert hub.reconnectTransport() is True
        assert FLIP not in hub.host.call("getLoadedDevices")
        assert mirror._onTransportReconnected(True) == []
        assert FLIP in hub.host.call("getLoadedDevices") and mirror.get_cached_state() == 0
        mirror.move_to(1)
        assert core_of(hub).getState(FLIP) == 1
    finally:
        mirror.finalize()


def test_reconnect_without_a_real_transport_returns_an_error(shutter):
    errors = shutter._onTransportReconnected(False)
    assert errors and isinstance(errors[0], str)
    assert shutter.connectionState is DeviceConnectionState.ERROR and not shutter.is_connected()
    assert shutter.get_last_error()
    with pytest.raises(DeviceNotConnectedError):
        shutter.move_to(1)


def test_reconnect_reports_a_label_the_reloaded_core_no_longer_has(shutter, hub, tmp_path):
    write_cfg(hub, tmp_path, "Property,Core,Initialize,0\nDevice,Other,DemoCamera,DShutter\nProperty,Core,Initialize,1\n")
    assert hub.reconnectTransport() is True
    errors = shutter._onTransportReconnected(True)
    assert errors and "no State or Shutter device 'White Light Shutter'" in errors[0] and "Other" in errors[0]
    assert shutter.connectionState is DeviceConnectionState.ERROR


def test_reconnect_reports_a_device_that_changed_type(shutter, hub, tmp_path):
    write_cfg(hub, tmp_path, "Property,Core,Initialize,0\nDevice,White Light Shutter,DemoCamera,DStage\nProperty,Core,Initialize,1\n")
    assert hub.reconnectTransport() is True
    errors = shutter._onTransportReconnected(True)
    assert errors and "is a Stage, not a State or Shutter" in errors[0]


def test_reconnect_follows_a_state_device_whose_position_count_changed(hub, tmp_path):
    """The count is the device's now (was: an error telling the user to restart): the names are read
    again and the controller's lifecycle refresh rebuilds the widget row."""
    write_cfg(hub, tmp_path, "Property,Core,Initialize,0\nDevice,Flip,DemoCamera,DStateDevice\n"
                             "Property,Flip,Number of positions,2\nProperty,Core,Initialize,1\n")
    assert hub.reconnectTransport() is True
    mirror = make_flip(hub, label=FLIP)
    try:
        assert mirror.get_state_names() == {0: "State-0", 1: "State-1"}
        write_cfg(hub, tmp_path, "Property,Core,Initialize,0\nDevice,Flip,DemoCamera,DStateDevice\n"
                                 "Property,Flip,Number of positions,3\nProperty,Core,Initialize,1\n", name="three.cfg")
        assert hub.reconnectTransport() is True
        assert mirror._onTransportReconnected(True) == []
        assert mirror.get_state_names() == {0: "State-0", 1: "State-1", 2: "State-2"}
        assert mirror.connectionState is DeviceConnectionState.CONNECTED
        mirror.move_to(2)
        assert mirror.get_state() == 2
    finally:
        mirror.finalize()


def test_a_reconnect_that_leaves_the_setup_inconsistent_with_the_new_count_is_still_an_error(hub, tmp_path):
    write_cfg(hub, tmp_path, "Property,Core,Initialize,0\nDevice,Flip,DemoCamera,DStateDevice\n"
                             "Property,Flip,Number of positions,3\nProperty,Core,Initialize,1\n")
    assert hub.reconnectTransport() is True
    mirror = make_flip(hub, label=FLIP, initial_state=2)
    try:
        write_cfg(hub, tmp_path, "Property,Core,Initialize,0\nDevice,Flip,DemoCamera,DStateDevice\n"
                                 "Property,Flip,Number of positions,2\nProperty,Core,Initialize,1\n", name="two.cfg")
        assert hub.reconnectTransport() is True
        errors = mirror._onTransportReconnected(True)
        assert errors and "initial_state 2 is not a position of 'Flip', which has 2 positions" in errors[0]
        assert mirror.connectionState is DeviceConnectionState.ERROR
    finally:
        mirror.finalize()


def test_a_wheel_built_while_the_hub_was_down_names_its_ten_positions_once_adopted(adapter_dir, tmp_path):
    """Absent at startup the mirror has the two placeholder positions; adoption gives the wheel's ten
    (the controller then rebuilds the row: see test_flipmirror_controller_positions)."""
    cfg = tmp_path / "late.cfg"
    hub = make_hub(adapter_dir, configFile=str(cfg))
    try:
        wheel = make_flip(hub, label="Dichroic")
        assert not wheel.is_connected() and wheel.get_state_names() == {0: "0", 1: "1"}
        cfg.write_text(open(adapter_dir + "/" + DEMO_CONFIG).read())
        assert hub.reconnectTransport() is True
        assert wheel._onTransportReconnected(True) == []
        names = wheel.get_state_names()
        assert len(names) == 10 and names[0] == "400DCLP" and names[9] == "State-9"
        wheel.move_to(4)
        assert wheel.get_state() == 4 and wheel.is_connected()
        wheel.finalize()
    finally:
        hub.finalize()


def test_lifecycle_reinitialise_raises_what_the_hook_would_return(shutter, hub, tmp_path):
    write_cfg(hub, tmp_path, "Property,Core,Initialize,0\nDevice,Other,DemoCamera,DShutter\nProperty,Core,Initialize,1\n")
    assert hub.reconnectTransport() is True
    with pytest.raises(RuntimeError, match="no State or Shutter device"):
        shutter._lifecycleReinitialise()


def test_finalize_unloads_an_inline_device_leaves_the_mirror_and_records_the_shutdown(hub):
    mirror = make_flip(hub, label="Shutter2", adapter="DemoCamera", device="DShutter")
    mirror.move_to(1)
    mirror.finalize()
    assert "Shutter2" not in hub.host.call("getLoadedDevices")
    assert mirror.connectionState is DeviceConnectionState.DISCONNECTED and not mirror.is_connected()
    plain = make_flip(hub)
    plain.move_to(1)
    plain.finalize()
    assert shutter_open(hub)                                          # a flip mirror stays where it is


# ------------------------------------------------------------------ descriptor
def test_descriptor_spec_is_a_flip_mirror_using_the_hub_transport(shutter):
    spec = shutter.getDeviceDescriptorSpec()
    assert spec.role is DeviceRole.PRIMARY and spec.category == "flip_mirror"
    assert spec.display_name == f"WhiteLight (Micro-Manager {SHUTTER})"
    (dependency,) = spec.dependencies
    assert dependency.kind is DeviceRelationKind.USES_TRANSPORT
    assert (dependency.target.kind, dependency.target.name) == ("rs232", "MMCore")


def test_a_failed_construction_leaves_an_inline_device_unloaded(hub):
    with pytest.raises(ValueError, match="managerProperties.states is for State devices"):
        make_flip(hub, label="Shutter2", adapter="DemoCamera", device="DShutter", states=[0, 1])
    assert "Shutter2" not in hub.host.call("getLoadedDevices")
