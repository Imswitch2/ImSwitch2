import json
from importlib import resources

import pytest


def _manifest():
    path = resources.files("imswitch_device_micromanager") / "imswitch.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _managers_by_id():
    return {m["id"]: m for m in _manifest()["contributions"]["device_managers"]}


def test_manifest_declares_the_core_hub_as_an_rs232_contribution():
    manager = _managers_by_id()["micromanager.core"]
    assert manager["kind"] == "rs232"
    assert manager["python_name"] == "imswitch_device_micromanager.core:MicroManagerCoreManager"
    assert "setup_templates/demo_scope.json" in manager["setup_templates"]


def test_manifest_claims_no_in_tree_manager_name():
    """An alias would silently re-route existing setups on `pip install` (the TIS lesson)."""
    for manager in _managers_by_id().values():
        assert manager["manager_name_aliases"] == []
        assert manager["id"].startswith("micromanager.")


def test_manifest_has_a_minimum_imswitch_version():
    assert _manifest()["imswitch_min_version"] == "0.1"


def test_every_contribution_names_a_schema_and_template_that_exist():
    package = resources.files("imswitch_device_micromanager")
    for manager in _managers_by_id().values():
        assert (package / manager["manager_properties_schema"]).is_file()
        for template in manager["setup_templates"]:
            assert (package / template).is_file()


def test_setup_template_properties_match_schema():
    jsonschema = pytest.importorskip("jsonschema")
    package = resources.files("imswitch_device_micromanager")
    schema = json.loads((package / "schemas/micromanager_core.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    setup = json.loads((package / "setup_templates/demo_scope.json").read_text(encoding="utf-8"))
    properties = setup["rs232devices"]["MMCore"]["managerProperties"]
    jsonschema.Draft202012Validator(schema).validate(properties)


@pytest.mark.parametrize("properties", [
    {"configFile": "a.cfg", "adapterPaths": ["/x"], "circularBufferMB": 64, "logDir": None,
     "adoptExistingCore": True},
    {},
    {"circularBufferMB": None},
])
def test_schema_accepts_valid_properties(properties):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((resources.files("imswitch_device_micromanager")
                         / "schemas/micromanager_core.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(properties)


@pytest.mark.parametrize("properties", [
    {"circularBufferMB": 0},
    {"circularBufferMB": 1.5},
    {"adapterPaths": "/x"},
    {"adoptExistingCore": "yes"},
    {"configFile": 3},
    {"unknownKey": 1},
])
def test_schema_rejects_invalid_properties(properties):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((resources.files("imswitch_device_micromanager")
                         / "schemas/micromanager_core.schema.json").read_text(encoding="utf-8"))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(properties)
