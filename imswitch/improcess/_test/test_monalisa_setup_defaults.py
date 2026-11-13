"""Detector-scoped setup defaults: explicit persistence and reproducibility."""

import json

import pytest

from imswitch.improcess.reconstructors.monalisa.setup_defaults import (
    FWHM_KEY, ORIENTATION_KEY, current_setup_path, positive_fwhm,
    read_detector_defaults, save_detector_default, valid_orientation,
)
from imswitch.improcess.reconstructors.monalisa.params import ORIENTATION_SETUP, ORIENTATION_RECORDED


def _setup(tmp_path):
    path = tmp_path / "hardware.json"
    path.write_text(json.dumps({
        "detectors": {
            "CAM": {"managerName": "MockDetector", "managerProperties": {
                "cameraPixelSizeUm": 0.077, "untouched": {"x": 1},
            }},
            "CAM2": {"managerName": "MockDetector", "managerProperties": {}},
        },
        "misc_user_field": {"a": [1, 2]},
    }), encoding="utf-8")
    return path


def test_explicit_save_round_trip_keeps_other_setup_content(tmp_path):
    path = _setup(tmp_path)
    old = json.loads(path.read_text())
    assert read_detector_defaults("CAM", path=path).fwhm_nm is None
    save_detector_default("CAM", FWHM_KEY, 212.5, path=path)
    save_detector_default("CAM", ORIENTATION_KEY, "-y+x", path=path)
    result = read_detector_defaults("CAM", path=path)
    assert result.detector == "CAM"
    assert result.fwhm_nm == pytest.approx(212.5)
    assert result.orientation == "-y+x"
    doc = json.loads(path.read_text())
    assert doc["misc_user_field"] == old["misc_user_field"]
    assert doc["detectors"]["CAM"]["managerProperties"]["untouched"] == {"x": 1}
    assert doc["detectors"] == old["detectors"]  # no hardware config changes
    assert doc["processing"]["monalisa"]["detectorDefaults"]["CAM"] == {
        "spotFwhmNm": 212.5, "scanOrientation": "-y+x",
    }
    assert doc["detectors"]["CAM2"] == old["detectors"]["CAM2"]


@pytest.mark.parametrize("detector,key,value", [
    ("missing", FWHM_KEY, 100), ("unknown", FWHM_KEY, 100),
    ("CAM", FWHM_KEY, -1), ("CAM", FWHM_KEY, float("nan")),
    ("CAM", ORIENTATION_KEY, "+x+x"), ("CAM", "otherKey", "a"),
])
def test_invalid_save_leaves_original_setup_untouched(tmp_path, detector, key, value):
    path = _setup(tmp_path)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        save_detector_default(detector, key, value, path=path)
    assert path.read_bytes() == before


def test_missing_or_invalid_defaults_do_not_create_a_false_calibration(tmp_path):
    path = _setup(tmp_path)
    doc = json.loads(path.read_text())
    doc["detectors"]["CAM"]["managerProperties"].update({
        FWHM_KEY: "bad", ORIENTATION_KEY: "not an orientation",
    })
    path.write_text(json.dumps(doc))
    assert read_detector_defaults("CAM", path=path).fwhm_nm is None
    assert read_detector_defaults("CAM", path=path).orientation is None
    assert read_detector_defaults("unknown", path=path).orientation is None
    assert positive_fwhm("inf") is None
    assert valid_orientation("+x+y") == "+x+y"


def test_setup_and_recorded_orientation_resolve_without_auto_detection():
    from imswitch.improcess.reconstructors.monalisa.reconstructor import MonalisaReconstructor

    for mode in (ORIENTATION_SETUP, ORIENTATION_RECORDED):
        assert MonalisaReconstructor._orientation_literal({
            "orientation_mode": mode, "orientation": "-y+x",
        }) == "-y+x"
    with pytest.raises(ValueError):
        MonalisaReconstructor._orientation_literal({
            "orientation_mode": ORIENTATION_SETUP, "orientation": "+x+x",
        })


def test_gaussian_fitting_default_saves_estimator_and_footprint_atomically(tmp_path):
    from imswitch.improcess.reconstructors.monalisa.setup_defaults import (
        save_detector_fitting, SHARED_FOOTPRINT_MODE_KEY, ESTIMATOR_KEY,
    )

    path = _setup(tmp_path)
    save_detector_fitting("CAM", "joint", "fixed", "circle", 10.5, path=path)
    saved = read_detector_defaults("CAM", path=path)
    assert (saved.estimator, saved.footprint_mode, saved.footprint_shape) == (
        "joint", "fixed", "circle",
    )
    assert saved.footprint_size == pytest.approx(10.5)
    doc = json.loads(path.read_text())
    prefs = doc["processing"]["monalisa"]["detectorDefaults"]["CAM"]
    assert prefs["estimator"] == "joint"
    assert prefs["footprint"] == {
        "shape": "circle", "mode": "fixed", "sizePx": 10.5, "sigmaFactor": 2.5,
    }
    assert doc["detectors"]["CAM"]["managerProperties"] == {
        "cameraPixelSizeUm": 0.077, "untouched": {"x": 1},
    }

    before = path.read_bytes()
    with pytest.raises(ValueError):
        save_detector_fitting("CAM", "invalid", "fixed", "circle", 10.5, path=path)
    assert path.read_bytes() == before


def test_older_footprint_default_has_no_implicit_estimator(tmp_path):
    from imswitch.improcess.reconstructors.monalisa.setup_defaults import save_detector_footprint

    path = _setup(tmp_path)
    save_detector_footprint("CAM", "from_lattice", "square", 9.0, path=path)
    defaults = read_detector_defaults("CAM", path=path)
    assert defaults.estimator is None
    assert defaults.footprint_mode == "from_lattice"


def test_processing_defaults_override_legacy_per_field_and_detectors_are_isolated(tmp_path):
    path = _setup(tmp_path)
    doc = json.loads(path.read_text())
    doc["detectors"]["CAM"]["managerProperties"].update({
        FWHM_KEY: 200.0, ORIENTATION_KEY: "+x+y", "monalisaFitEstimator": "joint",
    })
    doc["processing"] = {"other": {"preserve": True}, "monalisa": {
        "detectorDefaults": {"CAM": {"spotFwhmNm": 240.0, "custom": 17}}
    }}
    path.write_text(json.dumps(doc))
    defaults = read_detector_defaults("CAM", path=path)
    assert (defaults.fwhm_nm, defaults.orientation, defaults.estimator) == (240., "+x+y", "joint")
    assert read_detector_defaults("CAM2", path=path).fwhm_nm is None

    save_detector_default("CAM", ORIENTATION_KEY, "-y+x", path=path)
    after = json.loads(path.read_text())
    prefs = after["processing"]["monalisa"]["detectorDefaults"]["CAM"]
    assert prefs == {"spotFwhmNm": 240., "scanOrientation": "-y+x",
                     "estimator": "joint", "custom": 17}
    assert after["processing"]["other"] == {"preserve": True}
    assert after["detectors"] == doc["detectors"]  # read-only migration
    assert "CAM2" not in after["processing"]["monalisa"]["detectorDefaults"]


def test_invalid_processing_structure_fails_closed_without_modifying_setup(tmp_path):
    path = _setup(tmp_path)
    doc = json.loads(path.read_text())
    doc["processing"] = {"monalisa": {"detectorDefaults": ["not-a-map"]}}
    path.write_text(json.dumps(doc))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="detectorDefaults"):
        save_detector_default("CAM", FWHM_KEY, 230, path=path)
    assert path.read_bytes() == before


def test_migrates_old_fitting_preference_while_preserving_hardware(tmp_path):
    from imswitch.improcess.reconstructors.monalisa.setup_defaults import save_detector_default

    path = _setup(tmp_path)
    doc = json.loads(path.read_text())
    doc["detectors"]["CAM"]["managerProperties"].update({
        "monalisaFitEstimator": "local",
        "monalisaFitFootprintShape": "circle",
        "monalisaFitFootprintMode": "fwhm",
        "monalisaFitFootprintSizePx": 9.0,
        "monalisaFitFootprintSigmaFactor": 2.7,
    })
    path.write_text(json.dumps(doc))
    save_detector_default("CAM", FWHM_KEY, 215, path=path)
    after = json.loads(path.read_text())
    assert after["detectors"] == doc["detectors"]
    prefs = after["processing"]["monalisa"]["detectorDefaults"]["CAM"]
    assert prefs["estimator"] == "local"
    assert prefs["footprint"] == {
        "shape": "circle", "mode": "fwhm", "sizePx": 9.0, "sigmaFactor": 2.7,
    }
    assert read_detector_defaults("CAM", path=path).footprint_sigma_factor == 2.7
