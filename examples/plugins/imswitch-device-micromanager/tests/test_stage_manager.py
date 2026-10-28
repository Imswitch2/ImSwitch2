"""MicroManagerStageManager against the real MMCore and the DemoCamera adapters (no mocks)."""
import inspect
import math
import threading

import pytest

from imswitch.imcontrol.model.SetupInfo import PositionerInfo, RS232Info
from imswitch.imcontrol.model.devices.graph import DeviceRelationKind, DeviceRole
from imswitch.imcontrol.model.devices.status import (
    DeviceConnectionState, DeviceFailureKind, DeviceNotConnectedError, DeviceRuntimeMode,
)
from imswitch_device_micromanager import stage as stage_module
from imswitch_device_micromanager._core import MMCoreHostError
from imswitch_device_micromanager.core import MicroManagerCoreManager
from imswitch_device_micromanager.stage import MicroManagerStageManager, UNKNOWN_POSITION
from mm_helpers import DEMO_CONFIG, wait_until

Z, XY = "Z", "XY"
#: The demo XY stage snaps to its step size (0.015 um); a read-back is within a step of the command.
XY_STEP_TOLERANCE = 0.02


# ------------------------------------------------------------------- helpers
def make_hub(adapter_dir, **properties):
    properties.setdefault("adapterPaths", [adapter_dir])
    properties.setdefault("configFile", DEMO_CONFIG)
    return MicroManagerCoreManager(
        RS232Info(managerName="micromanager.core", managerProperties=properties), "MMCore")


def positioner_info(axes, forPositioning=True, **fields):
    properties = {"rs232device": "MMCore", **fields.pop("properties", {})}
    return PositionerInfo(managerName="micromanager.stage", managerProperties=properties, axes=list(axes),
                          forPositioning=forPositioning, **fields)


def make_stage(hub, label=Z, axes=None, name=None, info_fields=None, **properties):
    """A stage manager for ``label``; axes default to what the demo device of that label needs."""
    if axes is None:
        axes = ["X", "Y"] if label == XY else ["Z"]
    info = positioner_info(axes, properties={"label": label, **properties}, **(info_fields or {}))
    return MicroManagerStageManager(info, name or f"Stage-{label}", rs232sManager={"MMCore": hub})


def core_of(hub):
    return hub.host.core


@pytest.fixture
def hub(adapter_dir):
    h = make_hub(adapter_dir)
    yield h
    h.finalize()


@pytest.fixture
def z(hub):
    s = make_stage(hub, Z)
    yield s
    s.finalize()


@pytest.fixture
def xy(hub):
    s = make_stage(hub, XY)
    yield s
    s.finalize()


# ------------------------------------------------------------------------ init
def test_z_init_reads_the_position_from_the_device_not_zero(hub):
    core = core_of(hub)
    core.setPosition(Z, 42.5)
    core.waitForDevice(Z)
    stage = make_stage(hub, Z)
    try:
        assert stage.axes == ["Z"]
        assert stage.position == {"Z": 42.5}
        assert stage.label == Z and stage.hub is hub
        assert stage.connectionState is DeviceConnectionState.CONNECTED and stage.backendIsReal
        assert stage.forPositioning and not stage.forScanning
    finally:
        stage.finalize()


def test_a_stage_axis_can_have_any_name(hub):
    stage = make_stage(hub, Z, axes=["Focus"])
    try:
        assert stage.axes == ["Focus"] and list(stage.position) == ["Focus"]
        stage.setPosition(7.0, "Focus")
        assert stage.position["Focus"] == 7.0 and core_of(hub).getPosition(Z) == 7.0
    finally:
        stage.finalize()


def test_xy_init_has_two_axes_read_from_the_device(hub):
    core = core_of(hub)
    core.setXYPosition(XY, 30.0, 40.0)
    core.waitForDevice(XY)
    x, y = core.getXYPosition(XY)
    stage = make_stage(hub, XY)
    try:
        assert stage.axes == ["X", "Y"]
        assert stage.position == {"X": x, "Y": y}
        assert (x, y) != (0.0, 0.0) and abs(x - 30.0) < XY_STEP_TOLERANCE
    finally:
        stage.finalize()


def test_axes_that_fit_neither_device_type_are_refused_before_anything_loads(hub):
    for axes in (["X", "Y", "Z"], ["Y", "X"], ["A", "B"], []):
        with pytest.raises(ValueError, match="do not fit a Micro-Manager stage"):
            make_stage(hub, Z, axes=axes)


def test_wrong_axes_for_the_device_type_are_refused_naming_the_type(hub):
    with pytest.raises(ValueError, match=r"'Z' is a Stage \(one axis\), but axes is \['X', 'Y'\]"):
        make_stage(hub, Z, axes=["X", "Y"])
    with pytest.raises(ValueError, match=r"'XY' is an XYStage, which needs axes \['X', 'Y'\], but axes is \['Z'\]"):
        make_stage(hub, XY, axes=["Z"])


def test_a_label_that_is_not_a_stage_is_refused_naming_its_type(hub):
    with pytest.raises(ValueError, match=r"'Camera' is a Camera, not a Stage or XYStage"):
        make_stage(hub, "Camera", axes=["Z"])


def test_missing_label_error_lists_the_stage_labels(hub):
    with pytest.raises(ValueError) as caught:
        make_stage(hub, "XYs", axes=["X", "Y"])
    message = str(caught.value)
    assert "'XYs'" in message and "Did you mean 'XY'" in message
    assert "Loaded Stage devices: ['Z']" in message and "loaded XYStage devices: ['XY']" in message
    assert "Camera" not in message.split("Loaded Stage devices")[1]


def test_inline_mode_loads_the_device_and_finalize_unloads_it(hub):
    stage = make_stage(hub, "Focus2", axes=["Z"], adapter="DemoCamera", device="DStage")
    try:
        assert "Focus2" in hub.host.call("getLoadedDevices")
        assert stage.position == {"Z": 0.0}                 # a freshly loaded DemoStage really is at 0
        stage.setPosition(5.0, "Z")
        assert hub.host.call("getPosition", "Focus2") == 5.0
    finally:
        stage.finalize()
    assert "Focus2" not in hub.host.call("getLoadedDevices")
    assert stage.connectionState is DeviceConnectionState.DISCONNECTED


def test_inline_xy_stage(hub):
    stage = make_stage(hub, "Table", axes=["X", "Y"], adapter="DemoCamera", device="DXYStage")
    try:
        assert stage.axes == ["X", "Y"] and "Table" in hub.host.call("getLoadedDevices")
    finally:
        stage.finalize()
    assert "Table" not in hub.host.call("getLoadedDevices")


def test_inline_device_that_the_cfg_already_loaded_is_refused(hub):
    with pytest.raises(ValueError, match="already loaded"):
        make_stage(hub, Z, adapter="DemoCamera", device="DStage")


def test_inline_needs_adapter_and_device_together(hub):
    with pytest.raises(ValueError, match="both"):
        make_stage(hub, "Focus2", adapter="DemoCamera")


def test_an_inline_device_of_the_wrong_type_or_axes_is_refused_and_unloaded(hub):
    with pytest.raises(ValueError, match="not a Stage or XYStage"):
        make_stage(hub, "Cam3", adapter="DemoCamera", device="DCam")
    with pytest.raises(ValueError, match="is a Stage"):
        make_stage(hub, "Focus2", axes=["X", "Y"], adapter="DemoCamera", device="DStage")
    loaded = hub.host.call("getLoadedDevices")
    assert "Cam3" not in loaded and "Focus2" not in loaded


def test_a_failure_after_the_inline_load_unloads_the_device(hub):
    info = positioner_info(["Z"], forPositioning=False,        # PositionerManager refuses "neither"
                           properties={"label": "Focus2", "adapter": "DemoCamera", "device": "DStage"})
    with pytest.raises(ValueError, match="forPositioning"):
        MicroManagerStageManager(info, "Stage", rs232sManager={"MMCore": hub})
    assert "Focus2" not in hub.host.call("getLoadedDevices")


def test_for_scanning_is_refused_and_nothing_is_loaded(hub):
    info = PositionerInfo(managerName="micromanager.stage", axes=["Z"], forPositioning=True, forScanning=True,
                          managerProperties={"rs232device": "MMCore", "label": "Focus2",
                                             "adapter": "DemoCamera", "device": "DStage"})
    with pytest.raises(ValueError, match="forScanning must be false"):
        MicroManagerStageManager(info, "Stage", rs232sManager={"MMCore": hub})
    assert "Focus2" not in hub.host.call("getLoadedDevices")


def test_hub_lookup_and_property_errors_name_the_setup_fields(hub):
    with pytest.raises(ValueError, match=r"rs232device.*'Nope'.*rs232devices"):
        MicroManagerStageManager(positioner_info(["Z"], properties={"rs232device": "Nope", "label": Z}), "S",
                                 rs232sManager={"MMCore": hub})
    with pytest.raises(ValueError, match=r"rs232device.*not a Micro-Manager hub"):
        MicroManagerStageManager(positioner_info(["Z"], properties={"label": Z}), "S",
                                 rs232sManager={"MMCore": object()})
    with pytest.raises(ValueError, match="rs232device"):
        MicroManagerStageManager(positioner_info(["Z"], properties={"label": Z}), "S")
    with pytest.raises(ValueError, match="managerProperties.label"):
        MicroManagerStageManager(positioner_info(["Z"]), "S", rs232sManager={"MMCore": hub})
    with pytest.raises(ValueError, match="homeable must be true or false"):
        make_stage(hub, Z, homeable="yes")
    with pytest.raises(ValueError, match="waitForDevice must be true or false"):
        make_stage(hub, Z, waitForDevice=1)


# ---------------------------------------------------------------------- moves
def test_set_position_moves_the_stage_and_tracks_the_read_back(z, hub):
    result = z.setPosition(100.0, "Z")
    assert z.position["Z"] == 100.0 and core_of(hub).getPosition(Z) == 100.0
    assert result == {"Z": 100.0}


def test_move_is_relative(z, hub):
    z.setPosition(100.0, "Z")
    z.move(-25.0, "Z")
    assert z.position["Z"] == 75.0 and core_of(hub).getPosition(Z) == 75.0


def test_xy_set_position_leaves_the_other_axis_where_the_device_has_it(xy, hub):
    core = core_of(hub)
    xy.setPosition(50.0, "Y")
    _, y_before = core.getXYPosition(XY)
    xy.setPosition(10.0, "X")
    x, y = core.getXYPosition(XY)
    assert y == y_before
    assert abs(x - 10.0) < XY_STEP_TOLERANCE
    assert xy.position == {"X": x, "Y": y}                 # the read-back, not the command


def test_xy_move_is_relative_on_one_axis(xy, hub):
    core = core_of(hub)
    xy.setPosition(20.0, "X")
    xy.setPosition(20.0, "Y")
    x0, y0 = core.getXYPosition(XY)
    xy.move(5.0, "Y")
    x1, y1 = core.getXYPosition(XY)
    assert x1 == x0 and abs((y1 - y0) - 5.0) < XY_STEP_TOLERANCE
    assert xy.position == {"X": x1, "Y": y1}


def test_the_manager_follows_the_hardware_not_the_command(xy, hub):
    """The demo XY stage snaps to its step size: the tracked value is what the stage says."""
    xy.setPosition(1.0, "X")
    x, _ = core_of(hub).getXYPosition(XY)
    assert x != 1.0 and xy.position["X"] == x


def test_an_unknown_axis_is_refused_before_anything_moves(z, xy, hub):
    with pytest.raises(ValueError, match="Axis X not available"):
        z.setPosition(1.0, "X")
    with pytest.raises(ValueError, match="Axis Z not available"):
        xy.move(1.0, "Z")


def test_an_out_of_range_move_raises_and_the_position_stays(z, hub):
    z.setPosition(10.0, "Z")
    with pytest.raises(RuntimeError, match="out of range"):
        z.setPosition(1e9, "Z")
    assert z.position["Z"] == 10.0 and core_of(hub).getPosition(Z) == 10.0


# ------------------------------------------------------------------- events
def test_wait_for_device_false_still_ends_with_a_truthful_position_via_the_event(hub):
    stage = make_stage(hub, Z, waitForDevice=False)
    try:
        stage.setPosition(100.0, "Z")
        wait_until(lambda: stage.position["Z"] == 100.0, message="the stage position event")
        assert core_of(hub).getPosition(Z) == 100.0
        stage.move(-40.0, "Z")
        wait_until(lambda: stage.position["Z"] == 60.0, message="the relative move's event")
    finally:
        stage.finalize()


def test_update_position_reads_the_device_for_adapters_without_events(hub):
    stage = make_stage(hub, Z, waitForDevice=False)
    try:
        stage._disconnect_events()                          # an adapter that never reports a position change
        stage.setPosition(33.0, "Z")
        core_of(hub).waitForDevice(Z)
        assert stage.position["Z"] != 33.0                 # nothing told the manager
        stage.updatePosition()
        assert stage.position["Z"] == 33.0
    finally:
        stage.finalize()


def test_a_move_made_outside_the_manager_is_followed(z, hub):
    core_of(hub).setPosition(Z, 77.0)                       # a front panel, a joystick, another client
    wait_until(lambda: z.position["Z"] == 77.0, message="the event of the foreign move")


def test_events_of_other_stages_are_ignored(z, xy, hub):
    z.setPosition(5.0, "Z")
    xy.setPosition(80.0, "X")
    xy.setPosition(90.0, "Y")
    assert z.position["Z"] == 5.0
    x, y = core_of(hub).getXYPosition(XY)
    assert xy.position == {"X": x, "Y": y}


def test_xy_events_update_both_axes(hub):
    stage = make_stage(hub, XY, waitForDevice=False)
    try:
        core_of(hub).setXYPosition(XY, 60.0, 70.0)
        # The demo XY stage sends its final notification ~50 ms after the move; polling the core
        # meanwhile would only contend for the device, so the predicate looks at the manager alone.
        wait_until(lambda: abs(stage.position["X"] - 60.0) < XY_STEP_TOLERANCE
                   and abs(stage.position["Y"] - 70.0) < XY_STEP_TOLERANCE, message="the XY stage position event")
        assert stage.position == dict(zip("XY", core_of(hub).getXYPosition(XY)))
    finally:
        stage.finalize()


def test_the_event_handlers_never_wait_for_the_hub_lock(z, hub):
    """MMCore delivers events on its own thread while a move holds the hub lock and waits for the
    device: a handler that took that lock would deadlock the move."""
    with hub.host.lock:
        core_of(hub).setPosition(Z, 9.0)
        wait_until(lambda: z.position["Z"] == 9.0, message="an event while the hub lock is held")


def test_finalize_stops_following_the_device(hub):
    stage = make_stage(hub, Z)
    stage.setPosition(5.0, "Z")
    stage.finalize()
    core_of(hub).setPosition(Z, 3.0)
    core_of(hub).waitForDevice(Z)
    threading.Event().wait(0.05)                            # an event, if one were still wired, arrives at once
    assert stage.position["Z"] == 5.0
    stage.finalize()                                        # safe twice
    assert stage.connectionState is DeviceConnectionState.DISCONNECTED


# ------------------------------------------------------------ reference / homing
def test_without_homeable_nothing_is_referenceable_and_home_is_never_called(hub, monkeypatch):
    calls = []
    monkeypatch.setattr(core_of(hub), "home", lambda *a: calls.append(a))
    for label in (Z, XY):
        stage = make_stage(hub, label)
        try:
            assert stage.requiresReference is False and stage.isReferenceActionable is False
            assert stage.isReferenced and not stage.homeable
            before = dict(stage.position)
            stage.reference()
            assert stage.position == before
        finally:
            stage.finalize()
    assert calls == []


def test_requires_reference_is_set_per_instance(hub):
    homeable = make_stage(hub, XY, homeable=True)
    try:
        plain = make_stage(hub, Z)
        try:
            assert homeable.requiresReference is True and plain.requiresReference is False
            assert MicroManagerStageManager.requiresReference is False       # the class attribute stays
        finally:
            plain.finalize()
    finally:
        homeable.finalize()


def test_homeable_starts_unreferenced_and_never_homes_at_init(hub, monkeypatch):
    calls = []
    monkeypatch.setattr(core_of(hub), "home", lambda *a: calls.append(a))
    stage = make_stage(hub, XY, homeable=True)
    try:
        assert calls == []
        assert stage.requiresReference and stage.isReferenceActionable
        assert not stage.isReferenced and not stage.isAxisReferenced("X")
    finally:
        stage.finalize()


def test_reference_homes_the_xy_stage_and_marks_it_referenced(hub):
    stage = make_stage(hub, XY, homeable=True)
    try:
        stage.setPosition(50.0, "X")
        stage.reference()
        assert stage.isReferenced
        x, y = core_of(hub).getXYPosition(XY)
        assert stage.position == {"X": x, "Y": y}           # re-read after the home
    finally:
        stage.finalize()


def test_reference_calls_home_with_the_label_and_waits(hub, monkeypatch):
    core = core_of(hub)
    order = []
    real_home, real_busy = core.home, core.deviceBusy
    monkeypatch.setattr(core, "home", lambda label: (order.append(("home", label)), real_home(label))[1])
    # The wait polls deviceBusy outside the hub lock (not core.waitForDevice, which would hold it).
    monkeypatch.setattr(core, "deviceBusy", lambda label: (order.append(("busy", label)), real_busy(label))[1])
    stage = make_stage(hub, XY, homeable=True, waitForDevice=False)     # homing waits whatever waitForDevice says
    try:
        stage.reference("X")
        assert order[0] == ("home", XY) and ("busy", XY) in order[1:]
        assert stage.isAxisReferenced("X") and stage.isAxisReferenced("Y")        # MM homes the whole device
    finally:
        stage.finalize()


def test_reference_on_an_adapter_without_home_raises_micromanagers_error_not_at_init(hub):
    stage = make_stage(hub, Z, homeable=True)               # DemoStage has no home command: init must not care
    try:
        assert stage.connectionState is DeviceConnectionState.CONNECTED and not stage.isReferenced
        with pytest.raises(RuntimeError, match="Unsupported device command"):
            stage.reference()
        assert not stage.isReferenced
    finally:
        stage.finalize()


def test_reference_refuses_a_reference_position_and_an_unknown_axis(hub, monkeypatch):
    calls = []
    monkeypatch.setattr(core_of(hub), "home", lambda *a: calls.append(a))
    stage = make_stage(hub, XY, homeable=True)
    try:
        with pytest.raises(ValueError, match="reference position"):
            stage.reference(position=0.0)
        with pytest.raises(ValueError, match="Axis Z not available"):
            stage.reference("Z")
        assert calls == [] and not stage.isReferenced
    finally:
        stage.finalize()


# ------------------------------------------------------------- lifecycle / graph
def test_descriptor_depends_on_the_hub_transport(z):
    spec = z.getDeviceDescriptorSpec()
    assert spec.role is DeviceRole.PRIMARY and spec.category == "positioner"
    (dependency,) = spec.dependencies
    assert dependency.kind is DeviceRelationKind.USES_TRANSPORT
    assert (dependency.target.kind, dependency.target.name) == ("rs232", "MMCore")


def test_safe_state_moves_nothing(z, hub):
    z.setPosition(12.0, "Z")
    assert z._lifecycleSafeState(verified=False) == [] and z._lifecycleSafeState(verified=True) == []
    assert core_of(hub).getPosition(Z) == 12.0


def test_reconnect_rereads_the_position_and_follows_the_new_core(z, hub):
    z.setPosition(100.0, "Z")
    assert hub.reconnectTransport() is True
    core = core_of(hub)
    assert core.getPosition(Z) == 0.0 and z.position["Z"] == 100.0       # stale until the hook ran
    core.setPosition(Z, 12.0)
    core.waitForDevice(Z)
    assert z._onTransportReconnected(True) == []
    assert z.position["Z"] == 12.0 and z.connectionState is DeviceConnectionState.CONNECTED
    core.setPosition(Z, 31.0)                                             # the events come from the new core
    wait_until(lambda: z.position["Z"] == 31.0, message="an event of the reloaded core")
    z.setPosition(8.0, "Z")
    assert core.getPosition(Z) == 8.0


def test_reconnect_rereads_an_xy_stage(xy, hub):
    xy.setPosition(40.0, "X")
    assert hub.reconnectTransport() is True
    core = core_of(hub)
    core.setXYPosition(XY, 3.0, 4.0)
    core.waitForDevice(XY)
    assert xy._onTransportReconnected(True) == []
    assert xy.position == dict(zip("XY", core.getXYPosition(XY)))


def test_reconnect_reloads_an_inline_device(hub):
    stage = make_stage(hub, "Focus2", axes=["Z"], adapter="DemoCamera", device="DStage")
    try:
        assert hub.reconnectTransport() is True
        assert "Focus2" not in hub.host.call("getLoadedDevices")
        assert stage._onTransportReconnected(True) == []
        assert "Focus2" in hub.host.call("getLoadedDevices")
        stage.setPosition(2.0, "Z")
        assert hub.host.call("getPosition", "Focus2") == 2.0
    finally:
        stage.finalize()


def test_reconnect_without_a_real_transport_returns_an_error(z):
    errors = z._onTransportReconnected(False)
    assert errors and isinstance(errors[0], str)
    assert z.connectionState is DeviceConnectionState.ERROR
    with pytest.raises(DeviceNotConnectedError):
        z.setPosition(1.0, "Z")


def test_reconnect_reports_a_label_the_reloaded_core_no_longer_has(z, hub, tmp_path):
    cfg = tmp_path / "other.cfg"
    cfg.write_text("Property,Core,Initialize,0\nDevice,Other,DemoCamera,DStage\nProperty,Core,Initialize,1\n")
    hub.host.config_file = str(cfg)
    assert hub.reconnectTransport() is True
    errors = z._onTransportReconnected(True)
    assert errors and "no Stage or XYStage device 'Z'" in errors[0] and "Other" in errors[0]
    assert z.connectionState is DeviceConnectionState.ERROR


def test_reconnect_reports_a_label_that_changed_type(z, hub, tmp_path):
    cfg = tmp_path / "changed.cfg"
    cfg.write_text("Property,Core,Initialize,0\nDevice,Z,DemoCamera,DXYStage\nProperty,Core,Initialize,1\n")
    hub.host.config_file = str(cfg)
    assert hub.reconnectTransport() is True
    errors = z._onTransportReconnected(True)
    assert errors and "is an XYStage" in errors[0]


def test_lifecycle_reinitialise_raises_what_the_hook_would_return(z, hub, tmp_path):
    cfg = tmp_path / "other.cfg"
    cfg.write_text("Property,Core,Initialize,0\nDevice,Other,DemoCamera,DStage\nProperty,Core,Initialize,1\n")
    hub.host.config_file = str(cfg)
    assert hub.reconnectTransport() is True
    with pytest.raises(RuntimeError, match="no Stage or XYStage device"):
        z._lifecycleReinitialise()


# --------------------------------------------------------------- hub is down
def test_a_stage_is_built_absent_when_the_hub_is_not_connected(adapter_dir, tmp_path):
    hub = make_hub(adapter_dir, configFile=str(tmp_path / "nope.cfg"))
    try:
        stage = make_stage(hub, XY, homeable=True)
        assert stage.connectionState is DeviceConnectionState.ERROR
        assert stage.connectionFailureKind is DeviceFailureKind.DEVICE_NOT_FOUND     # the hub's kind
        assert "nope.cfg" in stage.connectionStatusDetails
        assert not stage.backendIsReal and not stage.isUsable
        assert stage.axes == ["X", "Y"] and set(stage.position) == {"X", "Y"}
        assert all(math.isnan(value) for value in stage.position.values())          # unknown, never a zero
        assert math.isnan(UNKNOWN_POSITION)
        for call in (lambda: stage.setPosition(1.0, "X"), lambda: stage.move(1.0, "Y"),
                     stage.reference, stage.updatePosition):
            with pytest.raises(DeviceNotConnectedError):
                call()
        stage.finalize()
    finally:
        hub.finalize()


def test_a_stage_built_while_the_hub_was_down_adopts_the_device_on_reconnect(adapter_dir, tmp_path):
    cfg = tmp_path / "late.cfg"
    hub = make_hub(adapter_dir, configFile=str(cfg))
    try:
        stage = make_stage(hub, Z)
        assert stage.connectionState is DeviceConnectionState.ERROR
        cfg.write_text(open(adapter_dir + "/" + DEMO_CONFIG).read())
        assert hub.reconnectTransport() is True
        core_of(hub).setPosition(Z, 21.0)
        core_of(hub).waitForDevice(Z)
        assert stage._onTransportReconnected(True) == []
        assert stage.connectionState is DeviceConnectionState.CONNECTED and stage.backendIsReal
        assert stage.position == {"Z": 21.0}
        stage.setPosition(22.0, "Z")
        assert core_of(hub).getPosition(Z) == 22.0
        core_of(hub).setPosition(Z, 23.0)
        wait_until(lambda: stage.position["Z"] == 23.0, message="an event after the late adoption")
        stage.finalize()
    finally:
        hub.finalize()


def test_a_late_adoption_of_the_wrong_device_type_is_an_error(adapter_dir, tmp_path):
    cfg = tmp_path / "late.cfg"
    hub = make_hub(adapter_dir, configFile=str(cfg))
    try:
        stage = make_stage(hub, "Camera", axes=["Z"])
        cfg.write_text(open(adapter_dir + "/" + DEMO_CONFIG).read())
        assert hub.reconnectTransport() is True
        errors = stage._onTransportReconnected(True)
        assert errors and "not a Stage or XYStage" in errors[0]
        assert stage.connectionState is DeviceConnectionState.ERROR
    finally:
        hub.finalize()


def test_a_transient_stage_stays_absent_until_connected(hub):
    stage = make_stage(hub, Z, info_fields={"transient": True})
    try:
        assert stage.runtimeMode is DeviceRuntimeMode.ABSENT and not stage.backendIsReal
        assert math.isnan(stage.position["Z"])
        result = stage.getDeviceLifecycle().connect()
        assert result.success, result
        assert stage.backendIsReal and stage.position["Z"] == core_of(hub).getPosition(Z)
        stage.setPosition(4.0, "Z")
        assert core_of(hub).getPosition(Z) == 4.0
    finally:
        stage.finalize()


def test_a_stage_whose_hub_went_away_refuses_moves(z, hub):
    hub.disconnectTransport()
    with pytest.raises(DeviceNotConnectedError):
        z.setPosition(1.0, "Z")


# ---------------------------------------------------------------- source rules
def test_stage_module_has_no_forbidden_calls_and_homes_only_in_reference():
    source = open(stage_module.__file__, encoding="utf-8").read()
    for forbidden in ("time.sleep", ".instance()", "_hardwareFaulted", "initialPosition={axis: 0"):
        assert forbidden not in source, forbidden
    assert source.count("home(") == 1
    assert "home(" in inspect.getsource(MicroManagerStageManager.reference)


def test_a_blocking_move_does_not_hold_the_hub_lock_while_the_device_is_busy(z, hub, monkeypatch):
    """Plan section 4.5: a multi-second stage move must not stall the camera drain thread.
    The device is made to report busy for a while; the hub lock must be acquirable meanwhile."""
    core = hub.host.core
    polls = {"busy": 6}
    real_busy = core.deviceBusy

    def slow_busy(label):
        if polls["busy"] > 0:
            polls["busy"] -= 1
            return True
        return real_busy(label)

    monkeypatch.setattr(core, "deviceBusy", slow_busy)
    acquired_during_wait = []

    def poke():
        wait_until(lambda: polls["busy"] < 6, message="the move to start polling")
        acquired_during_wait.append(hub.host.lock.acquire(timeout=1.0))
        if acquired_during_wait[-1]:
            hub.host.lock.release()

    poker = threading.Thread(target=poke)
    poker.start()
    z.setPosition(42.0, "Z")
    poker.join(timeout=5)
    assert acquired_during_wait == [True]
    assert polls["busy"] == 0                      # the wait really polled through the busy phase
    assert z.position["Z"] == pytest.approx(42.0)


def test_a_device_that_never_goes_idle_fails_at_micromanagers_timeout(z, hub, monkeypatch):
    core = hub.host.core
    monkeypatch.setattr(core, "deviceBusy", lambda label: True)
    monkeypatch.setattr(core, "getTimeoutMs", lambda: 50)
    with pytest.raises(MMCoreHostError, match="still busy"):
        z.setPosition(1.0, "Z")


def test_a_hub_reload_makes_a_homed_stage_unreferenced_again(hub):
    stage = make_stage(hub, XY, homeable=True)
    try:
        assert not stage.isReferenced
        stage.reference()
        assert stage.isReferenced
        assert hub.reconnectTransport()
        assert stage._onTransportReconnected(True) == []
        assert not stage.isReferenced                  # the reloaded core's homed state is unknown
    finally:
        stage.finalize()
