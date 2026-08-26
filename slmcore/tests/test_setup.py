import json

import pytest

from slmcore import (
    SLMGeometry,
    SLMHardwareSetup,
    SLMIdentity,
    SLMSectionsSetup,
    SLMSetup,
    SLMStartupPreferences,
    SectionSplitLayout,
    load_slm_setup_file,
    save_slm_startup_preferences,
)


def _setup():
    return SLMSetup(
        identity=SLMIdentity("pupil_slm","SER-001","Pupil SLM"),
        geometry=SLMGeometry(width=10,height=6,pixel_size_um=8.0),
        sections=SLMSectionsSetup(
            layout=SectionSplitLayout(n_sections=2,axis="x"),
            customizable=True,
        ),
        hardware=SLMHardwareSetup(
            driver="example",
            options={"device_index":0},
        ),
    )


def test_identity_requires_stable_serial_number():
    with pytest.raises(ValueError,match="serial_number"):
        SLMIdentity("slm","")
    with pytest.raises(ValueError,match="serial_number"):
        SLMIdentity("slm",None)


def test_display_name_is_non_identifying():
    assert SLMIdentity("slm","SER","First") == SLMIdentity("slm","SER","Renamed")


def test_setup_derives_section_geometry_from_canonical_layout():
    setup = _setup()
    assert tuple(setup.section_geometries) == ("sec_0","sec_1")
    assert setup.section_geometries["sec_0"].width == 5
    assert setup.section_geometries["sec_1"].x == 5
    assert setup.section_count == 2


def test_setup_roundtrips_through_dict():
    setup = _setup()
    restored = SLMSetup.from_dict(setup.to_dict())
    assert restored == setup
    assert restored.identity.display_name == "Pupil SLM"
    assert dict(restored.hardware.options) == {"device_index":0}
    assert restored.section_geometries == setup.section_geometries


def test_setup_from_dict_allows_host_key_override():
    data = _setup().to_dict()
    data["identity"].pop("key")
    restored = SLMSetup.from_dict(data,key="imswitch_key")
    assert restored.identity.key == "imswitch_key"
    assert restored.identity.serial_number == "SER-001"


def test_startup_preferences_roundtrip():
    preferences = SLMStartupPreferences(
        startup_config="default.h5",
        default_planes={"sec_0":"Sample"},
        section_display_mode="horizontal",
    )
    assert SLMStartupPreferences.from_dict(preferences.to_dict()) == preferences


def test_canonical_setup_file_load_and_preference_update(tmp_path):
    path = tmp_path / "slm.json"
    setup = _setup()
    path.write_text(json.dumps({
        "schema_version":1,
        "setup":setup.to_dict(),
        "startup_preferences":{
            "startup_config":None,
            "default_planes":{},
            "section_display_mode":"tabs",
        },
        "host_note":"preserved",
    }),encoding="utf-8")

    restored,preferences = load_slm_setup_file(path)
    assert restored == setup
    assert preferences == SLMStartupPreferences()

    changed = SLMStartupPreferences(
        startup_config="startup.h5",
        default_planes={"sec_0":"Sample"},
        section_display_mode="horizontal",
    )
    save_slm_startup_preferences(path,changed)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["startup_preferences"] == changed.to_dict()
    assert data["host_note"] == "preserved"
