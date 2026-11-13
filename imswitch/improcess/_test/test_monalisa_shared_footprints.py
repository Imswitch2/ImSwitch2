"""Shared Local/Joint footprint contract and migration of historical settings."""

import json

import numpy as np
import pytest

from imswitch.improcess.reconstructors.monalisa.calibration import PatternLocalization
from imswitch.improcess.reconstructors.monalisa.extraction import ExtractionOperator
from imswitch.improcess.reconstructors.monalisa.footprint_config import (
    FitFootprint, footprint_from_params, migrate_footprint_params,
)
from imswitch.improcess.reconstructors.monalisa.local_extraction import LocalExtractionOperator
from imswitch.improcess.reconstructors.monalisa.pipeline import PipelineParams, prepare_geometry
from imswitch.improcess.reconstructors.monalisa.setup_defaults import (
    read_detector_defaults, save_detector_footprint,
)


@pytest.mark.parametrize("old,expected", [
    ({"estimator": "joint", "joint_footprint_mode": "auto"}, ("circle", "fwhm", 2.5)),
    ({"estimator": "joint", "joint_footprint_mode": "fixed",
      "joint_footprint_radius_px": 5.25}, ("circle", "fixed", 10.5)),
    ({"estimator": "joint", "joint_footprint_mode": "lattice"}, ("lattice", "from_lattice", 2.5)),
    ({"estimator": "local", "local_footprint_shape": "square",
      "local_footprint_mode": "fixed", "local_footprint_size": 7}, ("square", "fixed", 2.5)),
])
def test_legacy_migration_is_estimator_specific(old, expected):
    fit = footprint_from_params(old)
    assert (fit.shape, fit.mode) == expected[:2]
    if old["estimator"] == "joint" and fit.mode == "fixed":
        assert fit.size_px == expected[2]
    if fit.mode == "fwhm":
        assert fit.sigma_factor == expected[2]
    migrated = migrate_footprint_params(old)
    assert footprint_from_params(migrated) == fit
    assert old.items() <= migrated.items()


def test_explicit_shared_parameters_override_old_estimator_fields():
    raw = {"estimator": "joint", "joint_footprint_mode": "auto",
           **FitFootprint("square", "fixed", 9).to_dict()}
    assert footprint_from_params(raw) == FitFootprint("square", "fixed", 9)
    raw["estimator"] = "local"
    assert footprint_from_params(raw) == FitFootprint("square", "fixed", 9)


@pytest.mark.parametrize("fit", [
    FitFootprint("square", "from_lattice"),
    FitFootprint("circle", "from_lattice"),
    FitFootprint("square", "fixed", 7),
    FitFootprint("circle", "fixed", 10.5),
    FitFootprint("circle", "fwhm", sigma_factor=2.5),
    FitFootprint("square", "fwhm", sigma_factor=2.5),
    FitFootprint("lattice", "from_lattice"),
])
def test_shared_footprint_resolves_in_both_estimators(fit):
    loc = PatternLocalization.manual_grid(12, 12, 5.2, 5.5)
    geom = {}
    for estimator in ("local", "joint"):
        params = PipelineParams(
            estimator=estimator, spot_sigma_px=1.6,
            variance_weighting=False, reassignment="off", frame_gain="off",
            localization_refinement="fast", local_sampling="camera_pixels",
            **fit.to_dict(),
        )
        geom[estimator] = prepare_geometry(
            np.ones((52, 53)), np.eye(2), np.array([[0, 0]]), params,
            localization=loc,
        )
        diag = geom[estimator].diagnostics
        assert diag["footprint_shape"] == fit.shape
        assert diag["footprint_mode"] == fit.mode
        assert diag["resolved_footprint_size_px"] == fit.resolved_size(loc, 1.6)
    local = geom["local"].operator
    joint = geom["joint"].operator
    assert isinstance(local, LocalExtractionOperator)
    assert isinstance(joint, ExtractionOperator)
    # For an interior focus the fit-pixel counts are the same in both
    # estimators; Joint's different Gaussian model does not change support.
    centre = np.where((geom["joint"].spot_model.x > 18)
                      & (geom["joint"].spot_model.x < 34)
                      & (geom["joint"].spot_model.y > 18)
                      & (geom["joint"].spot_model.y < 34))[0]
    assert len(centre)
    focus = centre[0]
    assert local.valid[focus] and joint.valid[focus]
    assert local.pixels_per_focus[focus] == joint.pixels_per_focus[focus]
    # Camera-pixel sampling must select the exact same sensor coordinates,
    # not merely the same number of samples.  Solvers remain independent.
    rows, cols = local.frame_shape
    count = int(local.pixels_per_focus[focus])
    native = np.asarray(local._native_pixel_index[focus], dtype=int)
    if fit.shape == "circle":
        centre_x = float(geom["local"].spot_model.x[focus])
        centre_y = float(geom["local"].spot_model.y[focus])
        yy, xx = np.divmod(native, cols)
        inside = ((xx - centre_x) ** 2 + (yy - centre_y) ** 2
                  <= (geom["local"].diagnostics["resolved_footprint_size_px"] / 2) ** 2)
        local_pixels = set(native[inside].tolist())
    else:
        local_pixels = set(native[:count].tolist())
    row_ids = joint.footprints.row[joint.footprints.focus == focus]
    joint_pixels = set(joint._row_pixel[row_ids].tolist())
    assert local_pixels == joint_pixels


def test_shared_lattice_rejects_staggered_in_both_estimators():
    loc = PatternLocalization.manual_grid(12, 10, 5, 5, stagger_axis="x", stagger=.5)
    for estimator in ("local", "joint"):
        with pytest.raises(ValueError, match="axis-aligned|single-motif|DLL grid"):
            prepare_geometry(
                np.zeros((50, 50)), np.eye(2), np.array([[0, 0]]),
                PipelineParams(estimator=estimator, spot_sigma_px=1.5,
                               variance_weighting=False, reassignment="off",
                               **FitFootprint("lattice").to_dict()),
                localization=loc,
            )


def test_setup_saves_shared_preferences_and_reads_old_local_format(tmp_path):
    path = tmp_path / "hardware.json"
    path.write_text(json.dumps({"detectors": {"CAM": {"managerProperties": {
        "preserve_me": True,
        "monalisaLocalFootprintMode": "fixed",
        "monalisaLocalFootprintShape": "square",
        "monalisaLocalFootprintSizePx": 7,
    }}}}))
    old = read_detector_defaults("CAM", path=path)
    assert (old.footprint_mode, old.footprint_shape, old.footprint_size) == ("fixed", "square", 7)
    save_detector_footprint("CAM", "fwhm", "circle", 9,
                            sigma_factor=3.25, path=path)
    loaded = read_detector_defaults("CAM", path=path)
    assert (loaded.footprint_mode, loaded.footprint_shape, loaded.footprint_sigma_factor) == (
        "fwhm", "circle", 3.25)
    doc = json.loads(path.read_text())
    props = doc["detectors"]["CAM"]["managerProperties"]
    assert props["preserve_me"] is True
    assert props["monalisaLocalFootprintSizePx"] == 7  # read-only legacy migration
    assert doc["processing"]["monalisa"]["detectorDefaults"]["CAM"]["footprint"] == {
        "shape": "circle", "mode": "fwhm", "sizePx": 9.0, "sigmaFactor": 3.25,
    }


def test_invalid_fixed_square_is_rejected_early():
    with pytest.raises(ValueError, match="odd integer"):
        FitFootprint("square", "fixed", 8).validate()
