import json
from importlib import resources


def _manifest():
    path = resources.files("imswitch_device_thorlabs") / "imswitch.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_manifest_declares_tsi_camera():
    manager = _manifest()["contributions"]["device_managers"][0]
    assert manager["id"] == "thorlabs.tsi-camera"
    assert manager["kind"] == "detector"
    assert manager["python_name"].endswith(":ThorCamTSIManager")
    # Legacy in-tree class name kept as an alias for seamless migration.
    assert "ThorCamTSIManager" in manager["manager_name_aliases"]


def test_setup_template_properties_match_schema():
    import pytest

    jsonschema = pytest.importorskip("jsonschema")
    pkg = resources.files("imswitch_device_thorlabs")
    schema = json.loads(
        (pkg / "schemas/thorcam_tsi.schema.json").read_text(encoding="utf-8")
    )
    setup = json.loads(
        (pkg / "setup_templates/thorcam_tsi_mock.json").read_text(encoding="utf-8")
    )
    props = setup["detectors"]["ThorCam"]["managerProperties"]
    jsonschema.Draft202012Validator(schema).validate(props)
