"""The plumbing in ``_bridge.MicroManagerDeviceMixin``, checked once for all four managers.

The camera, stage, light and flip-mirror managers share hub lookup, inline mode, label resolution and the
reconnect hook through the mixin, so these tests run the same scenario against each of them
(the per-manager modules keep their own tests of what the managers do with the device).
"""
import pytest

from imswitch.imcontrol.model.SetupInfo import DetectorInfo, FlipMirrorInfo, LaserInfo, PositionerInfo, RS232Info
from imswitch.imcontrol.model.devices.status import DeviceConnectionState, DeviceNotConnectedError
from imswitch_device_micromanager.camera import MicroManagerCameraManager
from imswitch_device_micromanager.core import MicroManagerCoreManager
from imswitch_device_micromanager.flipmirror import MicroManagerFlipMirrorManager
from imswitch_device_micromanager.light import MicroManagerLightManager
from imswitch_device_micromanager.stage import MicroManagerStageManager
from mm_helpers import DEMO_CONFIG


def _camera(properties):
    info = DetectorInfo(managerName="micromanager.camera", managerProperties=properties, forAcquisition=True)
    return lambda hubs: MicroManagerCameraManager(info, "Thing", rs232sManager=hubs)


def _stage(properties):
    info = PositionerInfo(managerName="micromanager.stage", managerProperties=properties, axes=["Z"],
                          forPositioning=True)
    return lambda hubs: MicroManagerStageManager(info, "Thing", rs232sManager=hubs)


def _light(properties):
    info = LaserInfo(managerName="micromanager.light", managerProperties={"enable": {"kind": "shutter"}, **properties},
                     wavelength=470, valueRangeMin=None, valueRangeMax=None)
    return lambda hubs: MicroManagerLightManager(info, "Thing", rs232sManager=hubs)


def _flip_mirror(properties):
    # FlipMirrorInfo is not a DeviceInfo: it has no transient / connectOnStartup, which the mixin must tolerate.
    info = FlipMirrorInfo(managerName="micromanager.flip-mirror", managerProperties=properties)
    return lambda hubs: MicroManagerFlipMirrorManager(info, "Thing", rs232sManager=hubs)


#: kind -> (factory, noun in the setup-file errors, a loaded label of that kind, device type text in the errors)
KINDS = {
    "camera": (_camera, "detector", "Camera", "camera"),
    "stage": (_stage, "positioner", "Z", "stage"),
    "light": (_light, "laser", "LED Shutter", "shutter"),
    "flip-mirror": (_flip_mirror, "flip_mirror", "White Light Shutter", "shutter"),
}


@pytest.fixture
def hub(adapter_dir):
    h = MicroManagerCoreManager(
        RS232Info(managerName="micromanager.core",
                  managerProperties={"adapterPaths": [adapter_dir], "configFile": DEMO_CONFIG}), "MMCore")
    yield h
    h.finalize()


@pytest.fixture(params=sorted(KINDS))
def kind(request):
    return request.param


def build(kind, hubs, **properties):
    factory, _noun, label, _type = KINDS[kind]
    return factory({"rs232device": "MMCore", "label": label, **properties})(hubs)


def test_the_manager_reads_the_hub_label_and_category_from_one_place(kind, hub):
    manager = build(kind, {"MMCore": hub})
    try:
        assert manager.hub is hub and manager.label == KINDS[kind][2]
        assert manager.getDeviceDescriptorSpec().category == KINDS[kind][1]
        assert manager.connectionState is DeviceConnectionState.CONNECTED and manager.backendIsReal
    finally:
        manager.finalize()


def test_a_missing_hub_key_or_label_is_refused_by_name(kind, hub):
    factory, noun, label, _type = KINDS[kind]
    manager_id = f"micromanager.{kind}"
    with pytest.raises(ValueError, match=f"{manager_id} needs managerProperties.rs232device"):
        factory({"label": label})({"MMCore": hub})
    with pytest.raises(ValueError, match=f"{manager_id} needs managerProperties.label"):
        factory({"rs232device": "MMCore"})({"MMCore": hub})
    with pytest.raises(ValueError, match=f"{manager_id} needs managerProperties.label"):
        factory({"rs232device": "MMCore", "label": ""})({"MMCore": hub})


def test_a_hub_name_without_an_rs232devices_entry_is_refused(kind, hub):
    _factory, noun, _label, _type = KINDS[kind]
    with pytest.raises(ValueError, match=rf"{noun} 'Thing': managerProperties.rs232device is 'MMCore', but no "
                                         r"rs232devices entry of that name exists"):
        build(kind, {"Other": hub})
    with pytest.raises(ValueError, match="no rs232devices entry"):
        build(kind, {})


def test_a_hub_name_that_is_not_a_micro_manager_hub_is_refused(kind):
    _factory, noun, _label, _type = KINDS[kind]
    with pytest.raises(ValueError, match=rf"{noun} 'Thing': rs232devices\['MMCore'\] is a object, not a "
                                         r"Micro-Manager hub"):
        build(kind, {"MMCore": object()})


def test_a_label_the_core_does_not_have_gets_a_did_you_mean_message(kind, hub):
    _factory, noun, label, type_text = KINDS[kind]
    typo = label + "x"
    with pytest.raises(ValueError) as excinfo:
        build(kind, {"MMCore": hub}, label=typo)
    message = str(excinfo.value)
    assert f"{noun} 'Thing' (managerProperties.label)" in message
    assert f"Did you mean {label!r}?" in message and "or give adapter and device to load it inline" in message


def test_inline_mode_needs_adapter_and_device_together(kind, hub):
    _factory, noun, _label, _type = KINDS[kind]
    with pytest.raises(ValueError, match=rf"{noun} 'Thing': inline mode needs both managerProperties.adapter and "
                                         r"managerProperties.device"):
        build(kind, {"MMCore": hub}, adapter="DemoCamera")
    with pytest.raises(ValueError, match="inline mode needs both"):
        build(kind, {"MMCore": hub}, device="DCam")


def test_an_inline_label_the_cfg_already_loaded_is_refused(kind, hub):
    _factory, noun, label, _type = KINDS[kind]
    device = {"camera": "DCam", "stage": "DStage", "light": "DShutter", "flip-mirror": "DShutter"}[kind]
    with pytest.raises(ValueError, match=rf"{noun} 'Thing': Micro-Manager device {label!r} is already loaded by the "
                                         r"hub's configuration"):
        build(kind, {"MMCore": hub}, adapter="DemoCamera", device=device)


def test_an_inline_device_of_the_wrong_type_is_refused_and_unloaded(kind, hub):
    _factory, noun, _label, _type = KINDS[kind]
    wrong = {"camera": "DStage", "stage": "DCam", "light": "DStage", "flip-mirror": "DStage"}[kind]
    with pytest.raises(ValueError, match=rf"{noun} 'Thing': Micro-Manager device 'Inline' is a "):
        build(kind, {"MMCore": hub}, label="Inline", adapter="DemoCamera", device=wrong)
    assert "Inline" not in hub.host.call("getLoadedDevices")


def test_the_wrong_device_type_names_the_type_it_found(kind, hub):
    _factory, noun, _label, _type = KINDS[kind]
    other = {"camera": "XY", "stage": "Camera", "light": "Camera", "flip-mirror": "Camera"}[kind]
    with pytest.raises(ValueError, match=rf"{noun} 'Thing': Micro-Manager device {other!r} is a "):
        build(kind, {"MMCore": hub}, label=other)


def test_a_hub_that_is_down_leaves_the_manager_absent_with_the_hubs_message(kind, adapter_dir, tmp_path):
    hub = MicroManagerCoreManager(
        RS232Info(managerName="micromanager.core",
                  managerProperties={"adapterPaths": [adapter_dir], "configFile": str(tmp_path / "nope.cfg")}), "MMCore")
    try:
        manager = build(kind, {"MMCore": hub})
        assert manager.connectionState is DeviceConnectionState.ERROR and not manager.backendIsReal
        assert manager.connectionFailureKind is hub.connectionFailureKind and "nope.cfg" in manager.connectionStatusDetails
        assert manager.connectionStatusSummary == f"{KINDS[kind][2]}: Micro-Manager core MMCore is not connected"
        manager.finalize()
    finally:
        hub.finalize()


def test_a_transport_that_did_not_reconnect_is_reported_by_every_manager(kind, hub):
    manager = build(kind, {"MMCore": hub})
    try:
        errors = manager._onTransportReconnected(False)
        assert errors and manager.connectionState is DeviceConnectionState.ERROR
        assert manager.connectionStatusSummary == f"{KINDS[kind][2]}: Micro-Manager core did not reconnect"
    finally:
        manager.finalize()


def test_use_refuses_a_hub_that_went_away(kind, hub):
    manager = build(kind, {"MMCore": hub})
    try:
        assert manager._use("probe")[1] == KINDS[kind][2]
        hub.disconnectTransport()
        with pytest.raises(DeviceNotConnectedError, match="Micro-Manager core 'MMCore' is not connected"):
            manager._use("probe")
    finally:
        manager.finalize()


def test_a_manager_whose_info_has_no_transient_flags_is_not_transient(hub):
    """``FlipMirrorInfo`` has neither ``transient`` nor ``connectOnStartup``: the mixin uses DeviceInfo's defaults."""
    manager = build("flip-mirror", {"MMCore": hub})
    try:
        holder = manager.backendHolder
        assert not hasattr(FlipMirrorInfo(managerName="x"), "transient")
        assert holder.transient is False and holder.connect_on_startup is False and manager.backendIsReal
    finally:
        manager.finalize()


#: kind -> the inline adapter device that is of the manager's type
INLINE_DEVICE = {"camera": "DCam", "stage": "DStage", "light": "DShutter", "flip-mirror": "DShutter"}


def test_a_hub_reload_forgets_that_an_inline_device_was_loaded(kind, hub, tmp_path):
    """After ``reconnectTransport`` every device is gone, the inline one too. If the reloaded
    ``.cfg`` now loads the same label, re-adoption must refuse it ("already loaded by the hub's
    configuration") instead of treating that device as the one this manager loaded."""
    device = INLINE_DEVICE[kind]
    manager = build(kind, {"MMCore": hub}, label="Inline", adapter="DemoCamera", device=device)
    try:
        assert manager._inline_loaded and "Inline" in hub.host.call("getLoadedDevices")

        assert hub.reconnectTransport() is True                    # the same setup: Inline is gone again
        assert "Inline" not in hub.host.call("getLoadedDevices")
        assert manager._onTransportReconnected(True) == []         # loaded again, flag set again
        assert manager._inline_loaded and "Inline" in hub.host.call("getLoadedDevices")

        cfg = tmp_path / "now_loads_it.cfg"
        cfg.write_text(f"Device,Inline,DemoCamera,{device}\nProperty,Core,Initialize,1\n")
        hub.host.config_file = str(cfg)
        assert hub.reconnectTransport() is True
        errors = manager._onTransportReconnected(True)
        assert errors and "already loaded by the hub's configuration" in errors[0]
        assert not manager._inline_loaded                          # it is the configuration's device, not ours
        assert "Inline" in hub.host.call("getLoadedDevices")
    finally:
        manager.finalize()
        assert "Inline" in hub.host.call("getLoadedDevices")       # finalize did not unload the cfg's device
