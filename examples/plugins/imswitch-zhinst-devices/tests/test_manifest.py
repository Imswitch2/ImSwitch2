import json
from importlib import resources


def _manifest():
    manifest_path = resources.files("imswitch_zhinst_devices") / "imswitch.json"
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def test_manifest_declares_lockin_detector():
    manifest = _manifest()

    managers = manifest["contributions"]["device_managers"]
    lockin = managers[0]

    assert lockin["id"] == "zhinst.lockin-demod"
    assert lockin["kind"] == "detector"
    assert lockin["python_name"].endswith(":ZhinstLockinDetectorManager")
    assert lockin["mock_python_name"].endswith(":MockZhinstLockinDetectorManager")
    assert lockin["manager_properties_schema"].endswith(".schema.json")


def test_setup_template_managerproperties_match_schema():
    import pytest

    jsonschema = pytest.importorskip("jsonschema")

    pkg = resources.files("imswitch_zhinst_devices")
    schema = json.loads(
        (pkg / "schemas/zhinst_lockin_detector.schema.json").read_text(encoding="utf-8")
    )
    setup = json.loads(
        (pkg / "setup_templates/zhinst_lockin_mock.json").read_text(encoding="utf-8")
    )
    props = setup["detectors"]["ZI Lock-in"]["managerProperties"]
    jsonschema.Draft202012Validator(schema).validate(props)
