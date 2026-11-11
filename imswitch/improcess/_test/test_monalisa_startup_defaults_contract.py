"""Active-setup selection and current MoNaLISA parameter contract (no Qt)."""

import json

import pytest

from imswitch.improcess.reconstructors.monalisa.setup_defaults import (
    read_detector_defaults, startup_detector_id,
)
from imswitch.improcess.reconstructors.monalisa.params import default_params
from imswitch.improcess.reconstructors.monalisa.footprint_config import (
    footprint_from_params, migrate_footprint_params,
)


def _setup(tmp_path, detectors):
    path = tmp_path / "setup.json"
    path.write_text(json.dumps({"detectors": detectors}), encoding="utf-8")
    return path


def test_startup_selects_sole_detector_and_reads_fwhm(tmp_path):
    path = _setup(tmp_path, {"CAM": {"managerProperties": {"monalisaSpotFwhmNm": 226.0}}})
    assert startup_detector_id(path=path) == "CAM"
    assert read_detector_defaults("CAM", path=path).fwhm_nm == pytest.approx(226.0)


def test_startup_finds_unique_configured_monalisa_detector(tmp_path):
    path = _setup(tmp_path, {
        "OTHER": {"managerProperties": {}},
        "CAM": {"managerProperties": {"monalisaSpotFwhmNm": 243.5}},
    })
    assert startup_detector_id(path=path) == "CAM"


def test_startup_unknown_when_multiple_detectors_have_defaults(tmp_path):
    path = _setup(tmp_path, {
        "CAM_A": {"managerProperties": {"monalisaSpotFwhmNm": 230}},
        "CAM_B": {"managerProperties": {"monalisaSpotFwhmNm": 250}},
    })
    assert startup_detector_id(path=path) == "unknown"


def test_startup_without_fwhm_chooses_only_single_detector(tmp_path):
    path = _setup(tmp_path, {"CAM": {"managerProperties": {}}})
    assert startup_detector_id(path=path) == "CAM"
    assert read_detector_defaults("CAM", path=path).fwhm_nm is None
    path = _setup(tmp_path, {"CAM": {"managerProperties": {}}, "OTHER": {"managerProperties": {}}})
    assert startup_detector_id(path=path) == "unknown"


def test_startup_bad_setup_fails_closed(tmp_path):
    path = tmp_path / "not_a_setup.json"
    path.write_text("[]", encoding="utf-8")
    assert startup_detector_id(path=path) == "unknown"
    path.write_text("{broken", encoding="utf-8")
    assert startup_detector_id(path=path) == "unknown"
    assert startup_detector_id(path=tmp_path / "missing.json") == "unknown"


def test_current_parameter_contract_has_shared_footprint_only():
    values = default_params()
    for obsolete in ("joint_footprint_mode", "joint_footprint_radius_px", "local_footprint_mode",
                     "local_footprint_shape", "local_footprint_size"):
        assert obsolete not in values
    assert values["footprint_shape"] == "square"
    assert values["footprint_mode"] == "from_lattice"
    old = {"estimator": "joint", "joint_footprint_mode": "fixed", "joint_footprint_radius_px": 5.25}
    migrated = migrate_footprint_params(old)
    assert footprint_from_params(migrated).size_px == pytest.approx(10.5)
    assert migrated["joint_footprint_mode"] == "fixed"


def test_startup_uses_new_processing_defaults_without_legacy_hardware_keys(tmp_path):
    path = tmp_path / "setup.json"
    path.write_text(json.dumps({
        "detectors": {"CAM": {"managerProperties": {}}, "OTHER": {"managerProperties": {}}},
        "processing": {"monalisa": {"detectorDefaults": {
            "CAM": {"spotFwhmNm": 242.0, "scanOrientation": "-x+y"},
        }}},
    }), encoding="utf-8")
    assert startup_detector_id(path=path) == "CAM"
    defaults = read_detector_defaults("CAM", path=path)
    assert defaults.fwhm_nm == pytest.approx(242.0)
    assert defaults.orientation == "-x+y"
    assert read_detector_defaults("OTHER", path=path).fwhm_nm is None
