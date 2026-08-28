import json

import pytest

from slmcore import (
    SLMDefinition,
    SLMGeometry,
    SLMHardwareConfig,
    SLMIdentity,
    SLMSectionsDefinition,
    SLMStartupPreferences,
    SectionSplitLayout,
    load_slm_setup_file,
    save_slm_startup_preferences,
)


def _definition():
    return SLMDefinition(
        identity=SLMIdentity("pupil_slm","SER-001","Pupil SLM"),
        geometry=SLMGeometry(width=10,height=6,pixel_size_um=8.0),
        sections=SLMSectionsDefinition(
            layout=SectionSplitLayout(n_sections=2,axis="x"),
            customizable=True,
        ),
    )


def _hardware():
    return SLMHardwareConfig(
        driver="example",
        options={"device_index":0},
    )


def test_identity_requires_stable_serial_number():
    with pytest.raises(ValueError,match="serial_number"):
        SLMIdentity("slm","")
    with pytest.raises(ValueError,match="serial_number"):
        SLMIdentity("slm",None)


def test_display_name_is_non_identifying():
    assert SLMIdentity("slm","SER","First") == SLMIdentity("slm","SER","Renamed")


def test_definition_derives_section_geometry_from_canonical_layout():
    definition = _definition()
    assert tuple(definition.section_geometries) == ("sec_0","sec_1")
    assert definition.section_geometries["sec_0"].width == 5
    assert definition.section_geometries["sec_1"].x == 5
    assert definition.section_count == 2


def test_definition_roundtrips_through_dict_without_hardware():
    definition = _definition()
    restored = SLMDefinition.from_dict(definition.to_dict())
    assert restored == definition
    assert restored.identity.display_name == "Pupil SLM"
    assert restored.section_geometries == definition.section_geometries
    assert "hardware" not in definition.to_dict()


def test_definition_from_dict_allows_host_key_override():
    data = _definition().to_dict()
    data["identity"].pop("key")
    restored = SLMDefinition.from_dict(data,key="imswitch_key")
    assert restored.identity.key == "imswitch_key"
    assert restored.identity.serial_number == "SER-001"


def test_hardware_config_roundtrips_independently():
    hardware = _hardware()
    restored = SLMHardwareConfig.from_dict(hardware.to_dict())
    assert restored == hardware
    assert dict(restored.options) == {"device_index":0}
    assert SLMHardwareConfig.from_dict(None) is None


def test_startup_preferences_roundtrip():
    preferences = SLMStartupPreferences(
        startup_config="default.h5",
        default_planes={"sec_0":"Sample"},
        section_display_mode="horizontal",
    )
    assert SLMStartupPreferences.from_dict(preferences.to_dict()) == preferences


def test_canonical_setup_file_load_and_preference_update(tmp_path):
    path = tmp_path / "slm.json"
    definition = _definition()
    hardware = _hardware()
    path.write_text(json.dumps({
        "schema_version":1,
        "definition":definition.to_dict(),
        "hardware":hardware.to_dict(),
        "startup_preferences":{
            "startup_config":None,
            "default_planes":{},
            "section_display_mode":"tabs",
        },
        "host_note":"preserved",
    }),encoding="utf-8")

    restored_definition,restored_hardware,preferences = load_slm_setup_file(path)
    assert restored_definition == definition
    assert restored_hardware == hardware
    assert preferences == SLMStartupPreferences()

    changed = SLMStartupPreferences(
        startup_config="startup.h5",
        default_planes={"sec_0":"Sample"},
        section_display_mode="horizontal",
    )
    save_slm_startup_preferences(path,changed)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["definition"] == definition.to_dict()
    assert data["hardware"] == hardware.to_dict()
    assert data["startup_preferences"] == changed.to_dict()
    assert data["host_note"] == "preserved"
