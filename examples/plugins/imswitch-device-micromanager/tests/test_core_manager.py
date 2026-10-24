"""MicroManagerCoreManager: the rs232-kind hub. It must never raise out of its constructor."""
import sys

import pytest

from imswitch.imcontrol.model.SetupInfo import RS232Info
from imswitch.imcontrol.model.devices.graph import DeviceRole
from imswitch.imcontrol.model.devices.status import DeviceConnectionState, DeviceFailureKind
from imswitch_device_micromanager.core import MicroManagerCoreManager
from mm_helpers import DEMO_CONFIG, start


def make_manager(adapter_dir, **properties):
    properties.setdefault("adapterPaths", [adapter_dir])
    return MicroManagerCoreManager(RS232Info(managerName="micromanager.core", managerProperties=properties), "MMCore")


@pytest.fixture
def manager(adapter_dir):
    m = make_manager(adapter_dir, configFile=DEMO_CONFIG)
    yield m
    m.finalize()


def test_good_config_connects(manager):
    assert manager.connectionState is DeviceConnectionState.CONNECTED
    assert manager.isConnected and manager.host.is_connected
    assert manager.host.has_device("Camera")
    assert "Micro-Manager core started" in manager.connectionStatusSummary
    connection, mode, summary, details, failure = manager.statusSnapshot()
    assert connection is DeviceConnectionState.CONNECTED and failure is None


def test_bad_config_file_does_not_raise_and_reports_the_error(adapter_dir, tmp_path):
    manager = make_manager(adapter_dir, configFile=str(tmp_path / "nope.cfg"))
    assert manager.connectionState is DeviceConnectionState.ERROR
    assert manager.connectionFailureKind is DeviceFailureKind.DEVICE_NOT_FOUND
    assert "nope.cfg" in manager.connectionStatusDetails
    assert not manager.isConnected
    assert manager.host is not None                       # the object stays alive, host unconnected
    manager.finalize()                                    # still finalizes cleanly


def test_config_that_fails_to_load_reports_the_micromanager_message(adapter_dir, tmp_path):
    cfg = tmp_path / "broken.cfg"
    cfg.write_text("Device,Cam,NoSuchAdapter,NoSuchDevice\n")
    manager = make_manager(adapter_dir, configFile=str(cfg))
    assert manager.connectionState is DeviceConnectionState.ERROR
    assert manager.connectionFailureKind is DeviceFailureKind.CONNECTION_ERROR
    assert "NoSuchAdapter" in manager.connectionStatusDetails


def test_missing_adapters_report_device_not_found(tmp_path):
    manager = MicroManagerCoreManager(
        RS232Info("micromanager.core", {"adapterPaths": [str(tmp_path / "no-adapters")]}), "MMCore")
    assert manager.connectionState is DeviceConnectionState.ERROR
    assert manager.connectionFailureKind is DeviceFailureKind.DEVICE_NOT_FOUND
    assert not manager.isConnected


def test_missing_pymmcore_plus_reports_a_missing_dependency(adapter_dir, monkeypatch):
    monkeypatch.setitem(sys.modules, "pymmcore_plus", None)       # `import pymmcore_plus` now raises ImportError
    manager = make_manager(adapter_dir, configFile=DEMO_CONFIG)
    assert manager.connectionState is DeviceConnectionState.ERROR
    assert manager.connectionFailureKind is DeviceFailureKind.MISSING_DEPENDENCY


def test_reconnect_transport_reloads_the_core(manager):
    old_core = manager.host.core
    assert manager.reconnectTransport() is True
    assert manager.host.core is not old_core
    assert manager.host.has_device("Camera")
    assert manager.connectionState is DeviceConnectionState.CONNECTED
    assert "reconnected" in manager.connectionStatusSummary


def test_reconnect_stops_running_streams_first(manager):
    manager.host.load_device("Cam2", "DemoCamera", "DCam")
    manager.host.register_camera("Camera")
    start(manager.host, "Camera")
    assert manager.reconnectTransport() is True
    assert manager.host.stream("Camera").streaming is False
    assert manager.host.running_streams() == []


def test_failed_reconnect_returns_false_and_keeps_the_error(adapter_dir, tmp_path):
    cfg = tmp_path / "scope.cfg"
    cfg.write_text(open(adapter_dir + "/" + DEMO_CONFIG).read())
    manager = make_manager(adapter_dir, configFile=str(cfg))
    try:
        assert manager.isConnected
        cfg.unlink()
        assert manager.reconnectTransport() is False
        assert manager.connectionState is DeviceConnectionState.ERROR
        assert not manager.isConnected
        cfg.write_text(open(adapter_dir + "/" + DEMO_CONFIG).read())
        assert manager.reconnectTransport() is True            # and it recovers
    finally:
        manager.finalize()


def test_disconnect_transport_unloads_and_marks_disconnected(manager):
    core = manager.host.core
    manager.disconnectTransport()
    assert manager.connectionState is DeviceConnectionState.DISCONNECTED
    assert tuple(core.getLoadedDevices()) == ("Core",)
    assert not manager.isConnected


def test_finalize_unloads_everything(adapter_dir):
    manager = make_manager(adapter_dir, configFile=DEMO_CONFIG)
    core = manager.host.core
    manager.finalize()
    assert tuple(core.getLoadedDevices()) == ("Core",)
    assert manager.connectionState is DeviceConnectionState.DISCONNECTED
    assert not manager.isConnected


def test_descriptor_role_is_resource(manager):
    assert manager.getDeviceDescriptorSpec().role is DeviceRole.RESOURCE


def test_manager_properties_reach_the_host(adapter_dir, tmp_path):
    manager = make_manager(adapter_dir, circularBufferMB=40, logDir=str(tmp_path / "mm"), configFile=DEMO_CONFIG)
    try:
        assert manager.host.call("getCircularBufferMemoryFootprint") == 40
        assert (tmp_path / "mm").is_dir()
        assert manager.name == "MMCore"
    finally:
        manager.finalize()


def test_inline_mode_without_a_config_file(adapter_dir):
    manager = make_manager(adapter_dir)
    try:
        assert manager.isConnected
        assert tuple(manager.host.call("getLoadedDevices")) == ("Core",)
        manager.host.load_device("Cam", "DemoCamera", "DCam")
        assert manager.host.has_device("Cam")
    finally:
        manager.finalize()
