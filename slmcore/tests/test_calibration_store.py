from types import SimpleNamespace

import pytest

from slmcore.calibration import (
    SLMCalibrationStore,SLMSectionCalibration,section_geometry_to_dict,
)
from slmcore.engine.section import SectionGeometry
from slmcore import SLMStartupPreferences


def test_calibration_store_owns_plane_catalog_and_section_files(tmp_path):
    store = SLMCalibrationStore(tmp_path)
    changes = []
    store.add_listener(lambda:changes.append(store.plane_names))

    plane = store.add_plane({
        "name":"Sample plane",
        "detector_name":"Camera",
        "detector_pixel_size_um":0.0777,
        "description":"test",
    })
    assert plane == "Sample plane"
    assert store.plane_names == ("Sample plane",)
    assert store.plane_definition(plane)["detector_name"] == "Camera"

    identity = SimpleNamespace(key="slm",serial_number="SN123")
    geometry = SectionGeometry("sec_0",0,0,64,32)
    saved = store.save_calibration(
        identity,"SLM", "sec_0",plane,
        SLMSectionCalibration(
            kx_per_um=0.01,ky_per_um=0.02,
            section_geometry=section_geometry_to_dict(geometry),
        ),
    )
    assert saved.plane == plane
    assert saved.cam_px_size_um == pytest.approx(0.0777)

    loaded = store.load_calibration(identity,"sec_0",plane)
    assert loaded.kx_per_um == pytest.approx(0.01)
    assert loaded.ky_per_um == pytest.approx(0.02)
    assert loaded.plane == plane
    assert loaded.cam_px_size_um == pytest.approx(0.0777)
    assert loaded.section_geometry == section_geometry_to_dict(geometry)

    deleted = store.delete_plane(plane)
    assert store.plane_names == ()
    assert len(deleted) == 1
    assert len(changes) == 2


def test_startup_preferences_normalizes_empty_plane_names():
    preferences = SLMStartupPreferences(
        default_planes={"sec_0":"Plane A","sec_1":""},
    )

    assert preferences.default_planes["sec_0"] == "Plane A"
    assert "sec_1" not in preferences.default_planes
