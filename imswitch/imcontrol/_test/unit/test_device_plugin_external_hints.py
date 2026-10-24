"""Tests for the Phase 8 extraction safety net: install hints for managers that
live in (or were extracted to) external plugin packages."""

import pytest

from imswitch.imcontrol.model.plugins.external import (
    ExternalManagerHint,
    lookup_external_hint,
)
from imswitch.imcontrol.model.plugins.registry import build_default_registry


def test_install_command_with_and_without_extra():
    assert ExternalManagerHint("imswitch-device-x").install_command() == (
        "pip install imswitch-device-x"
    )
    assert ExternalManagerHint(
        "imswitch-device-x", extra="hardware"
    ).install_command() == "pip install imswitch-device-x[hardware]"


def test_a_plugin_that_is_not_on_pypi_installs_from_its_source_folder():
    hint = ExternalManagerHint(
        "imswitch-device-x", extra="hardware", source="examples/plugins/imswitch-device-x"
    )
    assert hint.install_command() == 'pip install "./examples/plugins/imswitch-device-x[hardware]"'
    assert ExternalManagerHint(
        "imswitch-device-x", source="examples/plugins/imswitch-device-x"
    ).install_command() == 'pip install "./examples/plugins/imswitch-device-x"'


def test_every_bundled_plugin_hint_points_at_a_folder_that_exists():
    from pathlib import Path

    from imswitch.imcontrol.model.plugins.external import KNOWN_EXTERNAL_MANAGERS

    repo = Path(__file__).resolve().parents[4]
    for hint in set(KNOWN_EXTERNAL_MANAGERS.values()):
        assert hint.source, f"{hint.package} is not on PyPI; give it a source folder"
        assert (repo / hint.source / "pyproject.toml").is_file(), hint.source


def test_lookup_known_and_unknown():
    hint = lookup_external_hint("detector", "zhinst.lockin-demod")
    assert hint is not None
    assert hint.package == "imswitch-zhinst-devices"
    # Legacy class-name and alias resolve to the same package.
    assert lookup_external_hint("detector", "ZhinstLockinDetectorManager") is hint
    assert lookup_external_hint("detector", "ZurichLockinDetectorManager") is hint
    # Wrong kind / unknown name -> no hint.
    assert lookup_external_hint("laser", "zhinst.lockin-demod") is None
    assert lookup_external_hint("detector", "NoSuchManager") is None


def test_thorlabs_package_backs_camera_and_stage_across_kinds():
    """One plugin can provide managers of several device kinds; the camera
    (detector) and Kinesis stage (positioner) resolve to the same package."""
    camera = lookup_external_hint("detector", "thorlabs.tsi-camera")
    stage = lookup_external_hint("positioner", "thorlabs.kinesis-stage")
    assert camera is not None and stage is not None
    assert camera.package == stage.package == "imswitch-device-thorlabs"
    # Legacy in-tree class name resolves too.
    assert lookup_external_hint("positioner", "KinesisStageManager") is stage
    # Kind is part of the key: the stage name under the wrong kind misses.
    assert lookup_external_hint("detector", "thorlabs.kinesis-stage") is None


def test_resolution_error_includes_install_hint_for_known_external():
    registry = build_default_registry(discover=False)
    message = registry.format_resolution_error("detector", "zhinst.lockin-demod")
    assert "imswitch-zhinst-devices" in message
    assert "not published on PyPI" in message
    assert 'pip install "./examples/plugins/imswitch-zhinst-devices[hardware]"' in message


def test_resolution_error_includes_install_hint_for_extracted_positioner():
    registry = build_default_registry(discover=False)
    message = registry.format_resolution_error("positioner", "KinesisStageManager")
    assert 'pip install "./examples/plugins/imswitch-device-thorlabs[hardware]"' in message


def test_resolution_error_has_no_hint_for_unknown_manager():
    registry = build_default_registry(discover=False)
    message = registry.format_resolution_error("detector", "TotallyUnknownCam")
    assert "pip install" not in message
    # Still lists installed managers and the generic guidance.
    assert "Installed detector managers:" in message


# --- Micro-Manager bridge (imswitch-device-micromanager) -----------------------

MICROMANAGER_KEYS = [
    ("rs232", "micromanager.core", "rs232"),
    ("detector", "micromanager.camera", "detectors"),
    ("positioner", "micromanager.stage", "positioners"),
    ("laser", "micromanager.light", "lasers"),
    ("flip_mirror", "micromanager.flip-mirror", "flipMirrors"),
]


def test_micromanager_plugin_backs_hub_camera_and_stage_across_kinds():
    hints = {lookup_external_hint(kind, name) for kind, name, _ in MICROMANAGER_KEYS}
    assert len(hints) == 1
    hint = hints.pop()
    assert hint is not None and hint.package == "imswitch-device-micromanager"
    assert hint.extra is None
    assert hint.install_command() == 'pip install "./examples/plugins/imswitch-device-micromanager"'
    assert "Python >= 3.11" in hint.note
    assert "camera, stages, lights, flip mirrors" in hint.note
    # Kind is part of the key, and nothing in-tree is redirected: only the plugin's own ids are hinted.
    assert lookup_external_hint("detector", "micromanager.core") is None
    assert lookup_external_hint("detector", "MicroManagerCameraManager") is None


@pytest.mark.parametrize("kind, managerName, package", MICROMANAGER_KEYS)
def test_unresolved_micromanager_manager_tells_the_user_how_to_install_the_plugin(
        monkeypatch, kind, managerName, package):
    """The real resolution path (MultiManager) with the plugin not installed: the error carries the hint."""
    import sys

    import imswitch.imcontrol.model.managers.MultiManager  # noqa: F401
    from imswitch.imcontrol.model.managers.MultiManager import MultiManager
    from imswitch.imcontrol.model.plugins.registry import (
        DevicePluginRegistry,
        UnknownDeviceManagerError,
    )

    # managers/__init__.py binds "MultiManager" to the class; the module object is in sys.modules.
    monkeypatch.setattr(sys.modules["imswitch.imcontrol.model.managers.MultiManager"],
                        "get_default_registry", lambda: DevicePluginRegistry())
    with pytest.raises(UnknownDeviceManagerError) as excinfo:
        MultiManager._resolveManagerClass("imswitch.imcontrol.model.managers", package, kind, managerName)
    message = str(excinfo.value)
    assert managerName in message
    assert "imswitch-device-micromanager" in message
    assert 'pip install "./examples/plugins/imswitch-device-micromanager"' in message
    assert "not published on PyPI" in message
