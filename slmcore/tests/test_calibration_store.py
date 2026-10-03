import json

import pytest

from slmcore.core.calibration import (
    SLMSectionCalibration,section_geometry_to_dict,
)
from slmcore.workspace import MappingDefinition,MappingStore,SLMCalibrationStore
from slmcore.core.engine.section import SectionGeometry
from slmcore import SLMIdentity,SLMStartupPreferences


def test_mapping_catalog_and_section_calibrations_are_separate(tmp_path):
    mappings = MappingStore(tmp_path)
    changes = []
    mappings.add_listener(lambda:changes.append(mappings.names))

    definition = mappings.add(MappingDefinition(
        name="100x",description="test",
    ))
    assert definition.name == "100x"
    assert mappings.names == ("100x",)
    payload = json.loads((tmp_path/"mappings.json").read_text())
    assert payload["mappings"][0]["name"] == "100x"
    assert "detector_name" not in payload["mappings"][0]
    assert "detector_pixel_size_um" not in payload["mappings"][0]

    store = SLMCalibrationStore(tmp_path/"calibrations")
    identity = SLMIdentity("slm","SN123",display_name="SLM")
    geometry = SectionGeometry("sec_0",0,0,64,32)
    saved = store.save_calibration(
        identity,"sec_0",definition.name,
        SLMSectionCalibration(
            kx_per_um=0.01,ky_per_um=0.02,
            section_geometry=section_geometry_to_dict(geometry),
        ),
    )
    assert saved.mapping_name == "100x"
    assert not hasattr(saved,"cam_px_size_um")

    loaded = store.load_calibration(identity,"sec_0","100x")
    assert loaded.kx_per_um == pytest.approx(0.01)
    assert loaded.ky_per_um == pytest.approx(0.02)
    assert loaded.mapping_name == "100x"
    assert loaded.section_geometry == section_geometry_to_dict(geometry)

    deleted_calibration = store.delete_calibration(identity,"sec_0","100x")
    assert deleted_calibration is not None
    assert not store.load_calibration(identity,"sec_0","100x").is_valid()
    assert mappings.has("100x")

    mappings.delete("100x")
    assert mappings.names == ()
    assert len(changes) == 2


def test_mapping_store_uses_clean_break_mappings_schema(tmp_path):
    (tmp_path/"planes.json").write_text(
        json.dumps({"version":1,"planes":[{"name":"legacy"}]}),
        encoding="utf-8",
    )

    mappings = MappingStore(tmp_path)

    assert mappings.names == ()
    mappings.add({"name":"100x"})
    assert (tmp_path/"mappings.json").is_file()
    assert mappings.names == ("100x",)


def test_startup_preferences_normalizes_empty_mapping_names():
    preferences = SLMStartupPreferences(
        default_mappings={"sec_0":"100x","sec_1":""},
    )

    assert preferences.default_mappings["sec_0"] == "100x"
    assert "sec_1" not in preferences.default_mappings
