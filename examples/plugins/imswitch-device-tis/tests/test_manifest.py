import json
from importlib import resources

import pytest


def _manifest():
    path = resources.files("imswitch_device_tis") / "imswitch.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _managers_by_id():
    return {
        manager["id"]: manager
        for manager in _manifest()["contributions"]["device_managers"]
    }


def test_manifest_declares_ic4_camera():
    manager = _managers_by_id()["tis.camera-ic4"]
    assert manager["kind"] == "detector"
    assert manager["python_name"].endswith(":TISCameraIC4Manager")


def test_manifest_does_not_claim_the_legacy_tismanager_name():
    """Deliberate, and the opposite of the Thorlabs plugin's aliasing.

    That extraction was a pure move — same code, new home — so inheriting the
    legacy class name was safe. This is a rewrite onto a different SDK, and
    MultiManager._resolveManagerClass resolves registry contributions *ahead of*
    in-tree managers with only a log warning. An alias would therefore swap every
    existing TIS setup from IC3 to the unvalidated IC4 driver on `pip install`.

    Add the alias only once the rig has signed off and the in-tree TISManager is
    deleted, so this reads as one deliberate migration rather than a side effect
    of installing a package.
    """
    manager = _managers_by_id()["tis.camera-ic4"]
    assert "TISManager" not in manager["manager_name_aliases"]


def test_setup_template_properties_match_schema():
    jsonschema = pytest.importorskip("jsonschema")
    pkg = resources.files("imswitch_device_tis")
    schema = json.loads(
        (pkg / "schemas/tis_camera_ic4.schema.json").read_text(encoding="utf-8")
    )
    setup = json.loads(
        (pkg / "setup_templates/tis_camera_ic4_mock.json").read_text(encoding="utf-8")
    )
    props = setup["detectors"]["TISCam"]["managerProperties"]
    jsonschema.Draft202012Validator(schema).validate(props)


def test_setup_template_uses_the_mock_serial_convention():
    """The template must be runnable with no hardware and no vendor SDK."""
    pkg = resources.files("imswitch_device_tis")
    setup = json.loads(
        (pkg / "setup_templates/tis_camera_ic4_mock.json").read_text(encoding="utf-8")
    )
    serial = setup["detectors"]["TISCam"]["managerProperties"]["cameraSerial"]
    assert serial.startswith("MOCK_")


def _registry_resolves_plugin():
    """True when this plugin is installed (entry point discoverable)."""
    try:
        from imswitch.imcontrol.model.plugins.registry import build_default_registry
        return build_default_registry(discover=True).resolve(
            "detector", "tis.camera-ic4"
        ) is not None
    except Exception:
        return False


requires_installed = pytest.mark.skipif(
    not _registry_resolves_plugin(),
    reason="plugin not installed (entry point undiscoverable); run `pip install -e .`",
)


@requires_installed
def test_manager_resolves_through_the_real_loader():
    """Guards the path a setup file actually takes.

    The contribution id is namespaced with a hyphen, which is not a legal Python
    module path. MultiManager probes the legacy in-tree import path for every
    name it resolves, and that probe used to let joinModulePath's ValueError
    escape — so this plugin could be discovered and registered perfectly while
    still being impossible to name in a setup file. Registry-level checks all
    passed; only going through the real loader caught it.
    """
    from imswitch.imcontrol.model.managers.MultiManager import MultiManager

    cls = MultiManager._resolveManagerClass(
        "imswitch.imcontrol.model.managers", "detectors", "detector", "tis.camera-ic4"
    )

    assert cls.__name__ == "TISCameraIC4Manager"


@requires_installed
def test_setup_template_builds_a_working_detector_manager():
    """The shipped template must actually boot as a setup, not merely validate."""
    from imswitch.imcontrol.model.SetupInfo import SetupInfo
    from imswitch.imcontrol.model.managers.DetectorsManager import DetectorsManager

    raw = json.loads(
        (resources.files("imswitch_device_tis")
         / "setup_templates/tis_camera_ic4_mock.json").read_text(encoding="utf-8")
    )
    info = SetupInfo.from_json(json.dumps(raw), infer_missing=True)

    manager = DetectorsManager(info.detectors, updatePeriod=100)
    try:
        detector = manager["TISCam"]
        handle = manager.startAcquisition(liveView=False)
        detector._camera.simulate_hardware_trigger(4)

        frames = detector.readChunk("recording")

        assert len(frames) == 4
        assert len({f.tobytes() for f in frames}) == 4
        manager.stopAcquisition(handle, liveView=False)
    finally:
        manager.finalize()
