from slmcore import DEFAULT_REGISTRIES,SLMGeometry,SLMIdentity,SLMRuntime
from slmcore.core.calibration import (
    SLMSectionCalibration,
    attach_calibration_geometry,
    calibration_geometry_matches,
    calibration_geometry_mismatches,
    config_calibration_geometry_mismatches,
    section_geometry_to_dict,
)
from slmcore.core.engine.section import SectionGeometry,split_slm_geometry


def _calibration(geometry):
    return attach_calibration_geometry(
        SLMSectionCalibration(
            kx_per_um=0.01,
            ky_per_um=0.02,
            mapping_name="100x",
        ),
        geometry,
    )


def test_calibration_persists_explicit_section_geometry_and_detects_mismatch():
    original = SectionGeometry("sec_0",0,0,32,32)
    changed = SectionGeometry("sec_0",0,0,30,32)
    calibration = _calibration(original)

    assert calibration.section_geometry == section_geometry_to_dict(original)
    assert calibration_geometry_matches(calibration,original)
    assert not calibration_geometry_matches(calibration,changed)

    mismatches = calibration_geometry_mismatches((
        ("sec_0",changed,calibration),
    ))
    assert len(mismatches) == 1
    assert mismatches[0].calibration_geometry == section_geometry_to_dict(original)
    assert mismatches[0].section_geometry == section_geometry_to_dict(changed)


def test_config_mismatch_detection_compares_calibration_to_config_section_geometry():
    geometry = SLMGeometry(width=64,height=32,pixel_size_um=1.0)
    runtime = SLMRuntime(
        identity=SLMIdentity("slm","SER123"),
        geometry=geometry,
        section_geometries=split_slm_geometry(geometry,2),
        registries=DEFAULT_REGISTRIES,
    )
    original = runtime.get_section_geometry("sec_0")
    runtime.set_section_calibration("sec_0",_calibration(original))
    config = runtime.create_config()

    assert config_calibration_geometry_mismatches(config) == ()

    # Keep the section geometry but deliberately record a calibration measured
    # against another physical section geometry.
    clone = config.sections["sec_0"].clone(DEFAULT_REGISTRIES)
    clone.calibration.section_geometry = section_geometry_to_dict(
        SectionGeometry("sec_0",0,0,30,32)
    )
    config.sections["sec_0"] = clone

    mismatches = config_calibration_geometry_mismatches(config)
    assert len(mismatches) == 1
    assert mismatches[0].section_key == "sec_0"


def test_diagonal_calibration_conversion_and_serialized_matrix_are_equivalent():
    calibration = SLMSectionCalibration(kx_per_um=0.0125, ky_per_um=-0.02)

    assert calibration.um_to_kxy(8.0, -3.0) == (0.1, 0.06)
    x_um, y_um = calibration.kxy_to_um(0.1, 0.06)
    assert x_um == 8.0
    assert y_um == -3.0

    payload = calibration.to_dict()
    assert payload["mapping"] == "diagonal"
    assert payload["matrix_2x2"] == [[0.0125, 0.0], [0.0, -0.02]]
    restored = SLMSectionCalibration.from_dict(payload)
    assert restored.um_to_kxy(8.0, -3.0) == (0.1, 0.06)
