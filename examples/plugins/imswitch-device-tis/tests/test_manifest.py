import json
from importlib import resources


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
    import pytest

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
