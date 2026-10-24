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


# ------------------------------------------------------------------ camera
def _camera_schema():
    return json.loads((resources.files("imswitch_device_micromanager")
                       / "schemas/micromanager_camera.schema.json").read_text(encoding="utf-8"))


def test_manifest_declares_the_camera_as_a_detector_contribution():
    manager = _managers_by_id()["micromanager.camera"]
    assert manager["kind"] == "detector"
    assert manager["python_name"] == "imswitch_device_micromanager.camera:MicroManagerCameraManager"
    assert manager["manager_properties_schema"] == "schemas/micromanager_camera.schema.json"
    assert "setup_templates/demo_scope.json" in manager["setup_templates"]
    assert manager["manager_name_aliases"] == []


def test_setup_template_declares_a_camera_that_matches_its_schema():
    jsonschema = pytest.importorskip("jsonschema")
    package = resources.files("imswitch_device_micromanager")
    setup = json.loads((package / "setup_templates/demo_scope.json").read_text(encoding="utf-8"))
    detector = setup["detectors"]["Camera"]
    assert detector["managerName"] == "micromanager.camera" and detector["forAcquisition"] is True
    assert "cameraPixelSizeUm" not in detector["managerProperties"]       # MM's Res10x supplies it
    jsonschema.Draft202012Validator.check_schema(_camera_schema())
    jsonschema.Draft202012Validator(_camera_schema()).validate(detector["managerProperties"])
    assert detector["managerProperties"]["rs232device"] in setup["rs232devices"]


@pytest.mark.parametrize("properties", [
    {"rs232device": "MMCore", "label": "Camera"},
    {"rs232device": "MMCore", "label": "Camera", "cameraPixelSizeUm": 0.108},
    {"rs232device": "MMCore", "label": "Cam", "adapter": "DemoCamera", "device": "DCam"},
    {"rs232device": "MMCore", "label": "Cam", "adapter": "DemoCamera", "device": "DCam", "parent": "DHub",
     "preInitProperties": {"MaximumExposureMs": 5000, "Mode": "Noise", "Flag": True}},
])
def test_camera_schema_accepts_valid_properties(properties):
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator(_camera_schema()).validate(properties)


@pytest.mark.parametrize("properties", [
    {"label": "Camera"},                                                    # hub missing
    {"rs232device": "MMCore"},                                              # label missing
    {"rs232device": "MMCore", "label": ""},
    {"rs232device": "MMCore", "label": "Camera", "cameraPixelSizeUm": 0},
    {"rs232device": "MMCore", "label": "Camera", "cameraPixelSizeUm": -1},
    {"rs232device": "MMCore", "label": "Camera", "cameraPixelSizeUm": "0.1"},
    {"rs232device": "MMCore", "label": "Cam", "adapter": "DemoCamera"},      # adapter without device
    {"rs232device": "MMCore", "label": "Cam", "device": "DCam"},             # device without adapter
    {"rs232device": "MMCore", "label": "Cam", "parent": "DHub"},             # inline-only fields without adapter
    {"rs232device": "MMCore", "label": "Cam", "preInitProperties": {"a": 1}},
    {"rs232device": "MMCore", "label": "Cam", "adapter": "A", "device": "D", "preInitProperties": {"a": [1]}},
    {"rs232device": "MMCore", "label": "Camera", "waitForDevice": True},    # unknown key
])
def test_camera_schema_rejects_invalid_properties(properties):
    jsonschema = pytest.importorskip("jsonschema")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(_camera_schema()).validate(properties)


def test_camera_schema_documents_every_property():
    schema = _camera_schema()
    assert schema["additionalProperties"] is False and schema["required"] == ["rs232device", "label"]
    assert all(spec.get("description") for spec in schema["properties"].values())


# ------------------------------------------------------------------- stage
def _stage_schema():
    return json.loads((resources.files("imswitch_device_micromanager")
                       / "schemas/micromanager_stage.schema.json").read_text(encoding="utf-8"))


def test_manifest_declares_the_stage_as_a_positioner_contribution():
    manager = _managers_by_id()["micromanager.stage"]
    assert manager["kind"] == "positioner"
    assert manager["python_name"] == "imswitch_device_micromanager.stage:MicroManagerStageManager"
    assert manager["manager_properties_schema"] == "schemas/micromanager_stage.schema.json"
    assert "setup_templates/demo_scope.json" in manager["setup_templates"]
    assert manager["manager_name_aliases"] == []


def test_setup_template_declares_stages_that_match_the_schema():
    jsonschema = pytest.importorskip("jsonschema")
    package = resources.files("imswitch_device_micromanager")
    setup = json.loads((package / "setup_templates/demo_scope.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(_stage_schema())
    positioners = setup["positioners"]
    assert positioners["Z"]["axes"] == ["Z"] and positioners["XY"]["axes"] == ["X", "Y"]
    for positioner in positioners.values():
        assert positioner["managerName"] == "micromanager.stage" and positioner["forPositioning"] is True
        assert not positioner.get("forScanning")
        jsonschema.Draft202012Validator(_stage_schema()).validate(positioner["managerProperties"])
        assert positioner["managerProperties"]["rs232device"] in setup["rs232devices"]
    assert positioners["XY"]["managerProperties"]["homeable"] is True      # DemoXYStage homes, DemoStage does not
    assert "homeable" not in positioners["Z"]["managerProperties"]


@pytest.mark.parametrize("properties", [
    {"rs232device": "MMCore", "label": "Z"},
    {"rs232device": "MMCore", "label": "XY", "homeable": True, "waitForDevice": False},
    {"rs232device": "MMCore", "label": "Focus", "adapter": "DemoCamera", "device": "DStage"},
    {"rs232device": "MMCore", "label": "Focus", "adapter": "DemoCamera", "device": "DStage", "parent": "DHub",
     "preInitProperties": {"Mode": "fast", "Steps": 3, "Flag": True}},
])
def test_stage_schema_accepts_valid_properties(properties):
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator(_stage_schema()).validate(properties)


@pytest.mark.parametrize("properties", [
    {"label": "Z"},                                                         # hub missing
    {"rs232device": "MMCore"},                                              # label missing
    {"rs232device": "MMCore", "label": ""},
    {"rs232device": "MMCore", "label": "Z", "homeable": "yes"},
    {"rs232device": "MMCore", "label": "Z", "homeable": 1},
    {"rs232device": "MMCore", "label": "Z", "waitForDevice": "no"},
    {"rs232device": "MMCore", "label": "Z", "adapter": "DemoCamera"},        # adapter without device
    {"rs232device": "MMCore", "label": "Z", "device": "DStage"},             # device without adapter
    {"rs232device": "MMCore", "label": "Z", "parent": "DHub"},               # inline-only fields without adapter
    {"rs232device": "MMCore", "label": "Z", "preInitProperties": {"a": 1}},
    {"rs232device": "MMCore", "label": "Z", "axisNames": ["Z"]},             # axes come from PositionerInfo.axes
    {"rs232device": "MMCore", "label": "Z", "cameraPixelSizeUm": 0.1},       # a camera key
])
def test_stage_schema_rejects_invalid_properties(properties):
    jsonschema = pytest.importorskip("jsonschema")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(_stage_schema()).validate(properties)


def test_stage_schema_documents_every_property_and_its_defaults():
    schema = _stage_schema()
    assert schema["additionalProperties"] is False and schema["required"] == ["rs232device", "label"]
    assert all(spec.get("description") for spec in schema["properties"].values())
    assert schema["properties"]["waitForDevice"]["default"] is True
    assert schema["properties"]["homeable"]["default"] is False


# ------------------------------------------------------------------- light
SHUTTER = {"kind": "shutter"}


def _light_schema():
    return json.loads((resources.files("imswitch_device_micromanager")
                       / "schemas/micromanager_light.schema.json").read_text(encoding="utf-8"))


def test_manifest_declares_the_light_as_a_laser_contribution():
    manager = _managers_by_id()["micromanager.light"]
    assert manager["kind"] == "laser"
    assert manager["python_name"] == "imswitch_device_micromanager.light:MicroManagerLightManager"
    assert manager["manager_properties_schema"] == "schemas/micromanager_light.schema.json"
    assert "setup_templates/demo_scope.json" in manager["setup_templates"]
    assert manager["manager_name_aliases"] == []


def test_setup_template_declares_a_light_that_matches_its_schema():
    jsonschema = pytest.importorskip("jsonschema")
    package = resources.files("imswitch_device_micromanager")
    setup = json.loads((package / "setup_templates/demo_scope.json").read_text(encoding="utf-8"))
    laser = setup["lasers"]["LED"]
    assert laser["managerName"] == "micromanager.light" and laser["wavelength"] == 470
    assert laser["valueRangeMin"] is None and laser["valueRangeMax"] is None      # binary: no power block
    assert "power" not in laser["managerProperties"]
    jsonschema.Draft202012Validator(_light_schema()).validate(laser["managerProperties"])
    assert laser["managerProperties"]["rs232device"] in setup["rs232devices"]
    assert "Laser" in setup["availableWidgets"]

def _schema():
    return json.loads((resources.files("imswitch_device_micromanager")
                       / "schemas/micromanager_light.schema.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("properties", [
    {"rs232device": "MMCore", "label": "LED Shutter", "enable": {"kind": "shutter"}},
    {"rs232device": "MMCore", "label": "LED", "enable": {"kind": "state", "on": "470nm", "off": "Closed"}},
    {"rs232device": "MMCore", "label": "LED", "enable": {"kind": "state", "on": 2, "off": 0}},
    {"rs232device": "MMCore", "label": "S", "enable": {"kind": "property", "name": "State", "on": "1", "off": "0"}},
    {"rs232device": "MMCore", "label": "S", "enable": {"kind": "property", "name": "State", "on": 1, "off": 0.5}},
    {"rs232device": "MMCore", "label": "S", "enable": SHUTTER,
     "power": {"property": "Voltage", "units": "V", "decimals": 2}},
    {"rs232device": "MMCore", "label": "S", "enable": SHUTTER,
     "power": {"label": "DA", "property": "Voltage", "units": "mW", "decimals": 0}},
    {"rs232device": "MMCore", "label": "S", "adapter": "DemoCamera", "device": "DShutter", "parent": "DHub",
     "preInitProperties": {"Mode": "fast", "Steps": 3, "Flag": True}, "enable": SHUTTER},
])
def test_light_schema_accepts_valid_properties(properties):
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator.check_schema(_schema())
    jsonschema.Draft202012Validator(_schema()).validate(properties)


@pytest.mark.parametrize("properties", [
    {"label": "S", "enable": SHUTTER},                                              # hub missing
    {"rs232device": "MMCore", "enable": SHUTTER},                                   # label missing
    {"rs232device": "MMCore", "label": ""},                                         # enable missing
    {"rs232device": "MMCore", "label": "S", "enable": {}},
    {"rs232device": "MMCore", "label": "S", "enable": {"kind": "relay"}},
    {"rs232device": "MMCore", "label": "S", "enable": {"kind": "shutter", "on": 1}},
    {"rs232device": "MMCore", "label": "S", "enable": {"kind": "state", "on": 1}},
    {"rs232device": "MMCore", "label": "S", "enable": {"kind": "state", "on": -1, "off": 0}},
    {"rs232device": "MMCore", "label": "S", "enable": {"kind": "state", "on": True, "off": 0}},
    {"rs232device": "MMCore", "label": "S", "enable": {"kind": "state", "on": 1.5, "off": 0}},
    {"rs232device": "MMCore", "label": "S", "enable": {"kind": "state", "on": 1, "off": 0, "name": "x"}},
    {"rs232device": "MMCore", "label": "S", "enable": {"kind": "property", "on": "1", "off": "0"}},
    {"rs232device": "MMCore", "label": "S", "enable": {"kind": "property", "name": "", "on": "1", "off": "0"}},
    {"rs232device": "MMCore", "label": "S", "enable": {"kind": "property", "name": "S", "on": True, "off": "0"}},
    {"rs232device": "MMCore", "label": "S", "enable": SHUTTER, "power": {"property": "V", "decimals": 2}},         # no units
    {"rs232device": "MMCore", "label": "S", "enable": SHUTTER, "power": {"property": "V", "units": "V"}},          # no decimals
    {"rs232device": "MMCore", "label": "S", "enable": SHUTTER, "power": {"units": "V", "decimals": 2}},           # no property
    {"rs232device": "MMCore", "label": "S", "enable": SHUTTER, "power": {"property": "V", "units": "", "decimals": 2}},
    {"rs232device": "MMCore", "label": "S", "enable": SHUTTER, "power": {"property": "V", "units": "V", "decimals": -1}},
    {"rs232device": "MMCore", "label": "S", "enable": SHUTTER, "power": {"property": "V", "units": "V", "decimals": 1.5}},
    {"rs232device": "MMCore", "label": "S", "enable": SHUTTER, "power": {"property": "V", "units": "V", "decimals": 2, "x": 1}},
    {"rs232device": "MMCore", "label": "S", "enable": SHUTTER, "adapter": "A"},                                    # adapter without device
    {"rs232device": "MMCore", "label": "S", "enable": SHUTTER, "parent": "H"},
    {"rs232device": "MMCore", "label": "S", "enable": SHUTTER, "wavelength": 470},                                 # a LaserInfo field
    {"rs232device": "MMCore", "label": "S", "enable": SHUTTER, "calibCsvPath": "x.csv"},                            # no calibration lookup
])
def test_light_schema_rejects_invalid_properties(properties):
    jsonschema = pytest.importorskip("jsonschema")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(_schema()).validate(properties)


def test_light_schema_documents_every_property_and_has_no_defaults_for_units_and_decimals():
    schema = _schema()
    assert schema["additionalProperties"] is False and schema["required"] == ["rs232device", "label", "enable"]
    assert all(spec.get("description") for spec in schema["properties"].values())
    power = schema["properties"]["power"]
    assert power["required"] == ["property", "units", "decimals"] and power["additionalProperties"] is False
    assert all(spec.get("description") for spec in power["properties"].values())
    assert "default" not in power["properties"]["units"] and "default" not in power["properties"]["decimals"]


# --------------------------------------------------------------- flip mirror
def _flip_schema():
    return json.loads((resources.files("imswitch_device_micromanager")
                       / "schemas/micromanager_flip_mirror.schema.json").read_text(encoding="utf-8"))


def test_manifest_declares_the_flip_mirror_as_a_flip_mirror_contribution():
    manager = _managers_by_id()["micromanager.flip-mirror"]
    assert manager["kind"] == "flip_mirror"
    assert manager["python_name"] == "imswitch_device_micromanager.flipmirror:MicroManagerFlipMirrorManager"
    assert manager["manager_properties_schema"] == "schemas/micromanager_flip_mirror.schema.json"
    assert "setup_templates/demo_scope.json" in manager["setup_templates"]
    assert manager["manager_name_aliases"] == []


def test_setup_template_declares_a_flip_mirror_that_matches_its_schema():
    jsonschema = pytest.importorskip("jsonschema")
    package = resources.files("imswitch_device_micromanager")
    setup = json.loads((package / "setup_templates/demo_scope.json").read_text(encoding="utf-8"))
    mirror = setup["flipMirrors"]["WhiteLight"]
    assert mirror["managerName"] == "micromanager.flip-mirror"
    assert mirror["state_names"] == {"0": "Closed", "1": "Open"}
    assert mirror["managerProperties"] == {"rs232device": "MMCore", "label": "White Light Shutter"}
    jsonschema.Draft202012Validator.check_schema(_flip_schema())
    jsonschema.Draft202012Validator(_flip_schema()).validate(mirror["managerProperties"])
    assert mirror["managerProperties"]["rs232device"] in setup["rs232devices"]
    assert "FlipMirror" in setup["availableWidgets"]


@pytest.mark.parametrize("properties", [
    {"rs232device": "MMCore", "label": "White Light Shutter"},
    {"rs232device": "MMCore", "label": "Flip", "states": ["Out", "In"]},
    {"rs232device": "MMCore", "label": "Flip", "states": [1, 0]},
    {"rs232device": "MMCore", "label": "Flip", "states": ["Out", 1]},
    {"rs232device": "MMCore", "label": "Flip", "adapter": "DemoCamera", "device": "DStateDevice",
     "preInitProperties": {"Number of positions": 2}},
    {"rs232device": "MMCore", "label": "S", "adapter": "DemoCamera", "device": "DShutter", "parent": "DHub"},
])
def test_flip_mirror_schema_accepts_valid_properties(properties):
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator(_flip_schema()).validate(properties)


@pytest.mark.parametrize("properties", [
    {"label": "S"},                                                          # hub missing
    {"rs232device": "MMCore"},                                               # label missing
    {"rs232device": "MMCore", "label": ""},
    {"rs232device": "MMCore", "label": "S", "states": ["a"]},
    {"rs232device": "MMCore", "label": "S", "states": ["a", "b", "c"]},
    {"rs232device": "MMCore", "label": "S", "states": ["a", "a"]},
    {"rs232device": "MMCore", "label": "S", "states": [0, -1]},
    {"rs232device": "MMCore", "label": "S", "states": [0, 1.5]},
    {"rs232device": "MMCore", "label": "S", "states": ["", "b"]},
    {"rs232device": "MMCore", "label": "S", "states": "ab"},
    {"rs232device": "MMCore", "label": "S", "invert": True},                 # a FlipMirrorInfo field
    {"rs232device": "MMCore", "label": "S", "initial_state": 0},             # a FlipMirrorInfo field
    {"rs232device": "MMCore", "label": "S", "state_names": {"0": "a"}},      # a FlipMirrorInfo field
    {"rs232device": "MMCore", "label": "S", "adapter": "A"},                 # adapter without device
    {"rs232device": "MMCore", "label": "S", "device": "D"},                  # device without adapter
    {"rs232device": "MMCore", "label": "S", "parent": "H"},                  # inline-only without adapter
    {"rs232device": "MMCore", "label": "S", "preInitProperties": {"a": 1}},
    {"rs232device": "MMCore", "label": "S", "enable": {"kind": "shutter"}},  # a light key
])
def test_flip_mirror_schema_rejects_invalid_properties(properties):
    jsonschema = pytest.importorskip("jsonschema")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(_flip_schema()).validate(properties)


def test_flip_mirror_schema_documents_every_property():
    schema = _flip_schema()
    assert schema["additionalProperties"] is False and schema["required"] == ["rs232device", "label"]
    assert all(spec.get("description") for spec in schema["properties"].values())
