import json
from importlib import resources


def _manifest():
    path = resources.files("imswitch_device_thorlabs") / "imswitch.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _managers_by_id():
    return {
        manager["id"]: manager
        for manager in _manifest()["contributions"]["device_managers"]
    }


def test_manifest_declares_tsi_camera():
    manager = _managers_by_id()["thorlabs.tsi-camera"]
    assert manager["kind"] == "detector"
    assert manager["python_name"].endswith(":ThorCamTSIManager")
    # Legacy in-tree class name kept as an alias for seamless migration.
    assert "ThorCamTSIManager" in manager["manager_name_aliases"]


def test_manifest_declares_kinesis_stage():
    manager = _managers_by_id()["thorlabs.kinesis-stage"]
    assert manager["kind"] == "positioner"
    assert manager["python_name"].endswith(":KinesisStageManager")
    # Legacy in-tree class name kept as an alias for seamless migration.
    assert "KinesisStageManager" in manager["manager_name_aliases"]


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


def test_kinesis_setup_template_properties_match_schema():
    import pytest

    jsonschema = pytest.importorskip("jsonschema")
    pkg = resources.files("imswitch_device_thorlabs")
    schema = json.loads(
        (pkg / "schemas/kinesis_stage.schema.json").read_text(encoding="utf-8")
    )
    setup = json.loads(
        (pkg / "setup_templates/kinesis_stage_mock.json").read_text(encoding="utf-8")
    )
    props = setup["positioners"]["XY"]["managerProperties"]
    jsonschema.Draft202012Validator(schema).validate(props)
