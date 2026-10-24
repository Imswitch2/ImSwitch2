"""The plugin is found through its entry point (needs `pip install -e` into the environment)."""
import json
import threading
import subprocess
import sys
from importlib import resources

import pytest


def _registry():
    try:
        from imswitch.imcontrol.model.plugins.registry import get_default_registry
        return get_default_registry()
    except Exception:
        return None


def _installed():
    registry = _registry()
    return registry is not None and registry.resolve("rs232", "micromanager.core") is not None


requires_installed = pytest.mark.skipif(
    not _installed(), reason="plugin not installed (entry point undiscoverable); run `pip install -e .`")


def _run_cli(*args):
    return subprocess.run([sys.executable, "-m", "imswitch.imcontrol.model.plugins", *args],
                          capture_output=True, text=True, timeout=120)


@requires_installed
def test_cli_list_names_the_plugin():
    result = _run_cli("list")
    assert result.returncode == 0, result.stderr
    assert any(line.startswith("micromanager.core") and "[rs232]" in line
               and "imswitch-device-micromanager" in line for line in result.stdout.splitlines())


@requires_installed
def test_registry_resolves_and_loads_the_hub_class():
    from imswitch_device_micromanager.core import MicroManagerCoreManager

    registry = _registry()
    contribution = registry.resolve("rs232", "micromanager.core")
    assert contribution is not None and contribution.plugin_name == "imswitch-device-micromanager"
    assert registry.load_manager_class("rs232", "micromanager.core") is MicroManagerCoreManager


@requires_installed
def test_multimanager_resolves_the_hub_through_the_real_loader():
    from imswitch.imcontrol.model.managers.MultiManager import MultiManager

    cls = MultiManager._resolveManagerClass(
        "imswitch.imcontrol.model.managers", "rs232", "rs232", "micromanager.core")
    assert cls.__name__ == "MicroManagerCoreManager"


@requires_installed
def test_validate_setup_passes_on_the_template(tmp_path):
    template = resources.files("imswitch_device_micromanager") / "setup_templates/demo_scope.json"
    result = _run_cli("validate-setup", str(template))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAILED" not in result.stdout


@requires_installed
def test_template_builds_a_connected_hub_through_rs232_manager_loading(adapter_dir):
    """The shipped template boots as a setup: SetupInfo -> RS232sManager -> a connected hub."""
    from imswitch.imcontrol.model.SetupInfo import SetupInfo
    from imswitch.imcontrol.model.managers.RS232sManager import RS232sManager

    raw = json.loads((resources.files("imswitch_device_micromanager")
                      / "setup_templates/demo_scope.json").read_text(encoding="utf-8"))
    raw["rs232devices"]["MMCore"]["managerProperties"]["adapterPaths"] = [adapter_dir]
    info = SetupInfo.from_json(json.dumps(raw), infer_missing=True)
    manager = RS232sManager(info.rs232devices)
    try:
        hub = manager["MMCore"]
        assert hub.isConnected and hub.host.has_device("Camera")
    finally:
        manager.finalize()


@requires_installed
def test_cli_list_names_the_camera_contribution():
    result = _run_cli("list")
    assert result.returncode == 0, result.stderr
    assert any(line.startswith("micromanager.camera") and "[detector]" in line
               and "imswitch-device-micromanager" in line for line in result.stdout.splitlines())


@requires_installed
def test_registry_loads_the_camera_class():
    from imswitch_device_micromanager.camera import MicroManagerCameraManager

    assert _registry().load_manager_class("detector", "micromanager.camera") is MicroManagerCameraManager


@pytest.fixture
def template_managers(adapter_dir):
    """The shipped template as a running setup: RS232sManager (hub) + DetectorsManager (camera)."""
    from imswitch.imcontrol.model.SetupInfo import SetupInfo
    from imswitch.imcontrol.model.managers.DetectorsManager import DetectorsManager
    from imswitch.imcontrol.model.managers.RS232sManager import RS232sManager

    raw = json.loads((resources.files("imswitch_device_micromanager")
                      / "setup_templates/demo_scope.json").read_text(encoding="utf-8"))
    raw["rs232devices"]["MMCore"]["managerProperties"]["adapterPaths"] = [adapter_dir]
    info = SetupInfo.from_json(json.dumps(raw), infer_missing=True)
    rs232 = RS232sManager(info.rs232devices)
    detectors = DetectorsManager(info.detectors, updatePeriod=20, rs232sManager=rs232)
    try:
        yield rs232, detectors
    finally:
        for name in detectors.getAllDeviceNames():
            detectors[name].finalize()
        rs232.finalize()


@requires_installed
def test_template_builds_the_camera_through_detectors_manager_loading(template_managers):
    _, detectors = template_managers
    camera = detectors["Camera"]
    assert type(camera).__name__ == "MicroManagerCameraManager"
    assert camera.model == "DemoCamera/DCam" and camera.fullShape == (512, 512)
    assert camera.pixelSizeUm[1] == 1.0                    # the template omits it: MM's Res10x supplies it


@requires_installed
def test_a_live_view_lease_streams_and_releasing_it_stops_the_camera(template_managers):
    from mm_helpers import wait_until

    rs232, detectors = template_managers
    camera = detectors["Camera"]
    camera.setParameter("Exposure", 5)
    handle = detectors.startAcquisition(liveView=True)
    try:
        wait_until(lambda: camera.image.size and camera.image.any(), message="live view frames")
        assert camera.image.shape == (512, 512)
    finally:
        detectors.stopAcquisition(handle, liveView=True)
    assert not rs232["MMCore"].host.running_streams()
    assert not detectors.isDetectorFaulted("Camera")


@requires_installed
def test_a_lost_frame_stream_faults_the_detector_through_the_lease_table(adapter_dir):
    """stopAcquisition raising FramesLostError is what the lease table turns into the hardware fault."""
    from imswitch.imcontrol.model.SetupInfo import SetupInfo
    from imswitch.imcontrol.model.managers.DetectorsManager import DetectorsManager
    from imswitch.imcontrol.model.managers.RS232sManager import RS232sManager
    from mm_helpers import wait_until

    raw = json.loads((resources.files("imswitch_device_micromanager")
                      / "setup_templates/demo_scope.json").read_text(encoding="utf-8"))
    properties = raw["rs232devices"]["MMCore"]["managerProperties"]
    properties.update(adapterPaths=[adapter_dir], circularBufferMB=1)
    info = SetupInfo.from_json(json.dumps(raw), infer_missing=True)
    rs232 = RS232sManager(info.rs232devices)
    detectors = DetectorsManager(info.detectors, updatePeriod=20, rs232sManager=rs232)
    try:
        camera = detectors["Camera"]
        camera.setParameter("Exposure", 1)
        handle = detectors.acquire(["Camera"], _recording_purpose())
        host = rs232["MMCore"].host
        with host.lock:                                     # the consumer stalls: MMCore's 1 MB buffer overflows
            threading.Event().wait(0.2)
        wait_until(lambda: host.snapshot()["streams"]["Camera"]["fault"], message="overflow fault")
        detectors.release(handle)
        assert detectors.isDetectorFaulted("Camera")        # quarantined, not silently "stopped"
        camera.flushBuffers()
        camera.setParameter("Exposure", 100)                # slow enough not to overflow again
        detectors.retryStop("Camera")                       # the fault was consumed by the raising stop
        assert not detectors.isDetectorFaulted("Camera")
    finally:
        for name in detectors.getAllDeviceNames():
            detectors[name].finalize()
        rs232.finalize()


def _recording_purpose():
    from imswitch.imcontrol.model.managers._acquisition_leases import LeasePurpose
    return LeasePurpose.RECORDING
