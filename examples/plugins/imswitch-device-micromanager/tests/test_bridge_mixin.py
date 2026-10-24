"""The plumbing in ``_bridge.MicroManagerDeviceMixin``, checked once for all three managers.

The camera, stage and light managers share hub lookup, inline mode, label resolution and the
reconnect hook through the mixin, so these tests run the same scenario against each of them
(the per-manager modules keep their own tests of what the managers do with the device).
"""
import pytest

from imswitch.imcontrol.model.SetupInfo import DetectorInfo, LaserInfo, PositionerInfo, RS232Info
from imswitch.imcontrol.model.devices.status import DeviceConnectionState, DeviceNotConnectedError
from imswitch_device_micromanager.camera import MicroManagerCameraManager
from imswitch_device_micromanager.core import MicroManagerCoreManager
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


#: kind -> (factory, noun in the setup-file errors, a loaded label of that kind, device type text in the errors)
KINDS = {
    "camera": (_camera, "detector", "Camera", "camera"),
    "stage": (_stage, "positioner", "Z", "stage"),
    "light": (_light, "laser", "LED Shutter", "shutter"),
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
    device = {"camera": "DCam", "stage": "DStage", "light": "DShutter"}[kind]
    with pytest.raises(ValueError, match=rf"{noun} 'Thing': Micro-Manager device {label!r} is already loaded by the "
                                         r"hub's configuration"):
        build(kind, {"MMCore": hub}, adapter="DemoCamera", device=device)


def test_an_inline_device_of_the_wrong_type_is_refused_and_unloaded(kind, hub):
    _factory, noun, _label, _type = KINDS[kind]
    wrong = {"camera": "DStage", "stage": "DCam", "light": "DStage"}[kind]
    with pytest.raises(ValueError, match=rf"{noun} 'Thing': Micro-Manager device 'Inline' is a "):
        build(kind, {"MMCore": hub}, label="Inline", adapter="DemoCamera", device=wrong)
    assert "Inline" not in hub.host.call("getLoadedDevices")


def test_the_wrong_device_type_names_the_type_it_found(kind, hub):
    _factory, noun, _label, _type = KINDS[kind]
    other = {"camera": "XY", "stage": "Camera", "light": "Camera"}[kind]
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
