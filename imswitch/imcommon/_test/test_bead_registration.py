"""Bead registration: camera pixels -> scan micrometres from two bead images.

Synthetic bead fields (``_bead_fields``) are rendered into a "camera" image
through a known camera-pixel -> µm map and into an "APD scan" image through
a scan geometry; the registration must recover the map without being told
anything about it, and refuse what it cannot trust."""
import json

import numpy as np
import pytest

from imswitch.imcommon._test import _bead_fields as bf
from imswitch.imcommon.algorithms.transforms import AcquisitionContext, fit_transform, io
from imswitch.imcommon.algorithms.transforms.bead_registration import (
    BeadRegistrationError,
    BeadRegistrationOptions,
    decompose_affine,
    register_bead_images,
    scan_geometry_affine,
)
from imswitch.imcommon.algorithms.transforms.correspondence import (
    builtin_correspondence_specs,
    detect_beads,
)

pytestmark = pytest.mark.nohardware

_CORNERS = np.array([[r, c] for r in (0.0, 200.0, 399.0) for c in (0.0, 240.0, 479.0)])


def _truth(matrix, points):
    return points @ matrix[:, :2].T + matrix[:, 2]


def _error_um(result, matrix):
    """Largest disagreement with the truth over the camera field, µm."""
    return float(np.abs(result.transform.map_points(_CORNERS) - _truth(matrix, _CORNERS)).max())


@pytest.mark.parametrize("kwargs", [
    dict(rotation_deg=17.0),
    dict(rotation_deg=90.4),
    dict(rotation_deg=180.0),
    dict(rotation_deg=-63.0, mirror=True),
    dict(rotation_deg=91.0, mirror=True),
    dict(rotation_deg=3.0, cam_scale=0.065, cam_sigma_px=2.2),        # another magnification
    dict(rotation_deg=40.0, fast="Y", slow="X"),                       # Y-fast scan
    dict(rotation_deg=-10.0, fast_step=0.08, slow_step=0.12, apd_counts=(220, 120)),   # non-square steps
    dict(rotation_deg=120.0, apd_sigma_px=1.8, cam_sigma_px=1.1, apd_amp=25, cam_amp=600),  # PSFs, SNR
    dict(rotation_deg=200.0, missing_fraction=0.15, spurious=5, seed=4),   # missing and spurious beads
])
def test_recovers_the_known_map(kwargs):
    cam, apd, geom, matrix, _ = bf.scene(**kwargs)
    result = register_bead_images(cam, apd, geom, BeadRegistrationOptions(target_axes=("Y", "X")))
    assert _error_um(result, matrix) < 0.06, result.report()
    g, t = decompose_affine(result.matrix), decompose_affine(np.vstack([matrix, [0, 0, 1]]))
    assert g["mirror"] == t["mirror"]
    assert abs((g["rotation_deg"] - t["rotation_deg"] + 180) % 360 - 180) < 0.3
    assert abs(g["scale"] / t["scale"] - 1) < 0.005
    assert result.rms < 0.05 and len(result.camera_points) >= 8


def test_the_scan_covers_only_part_of_the_camera_field():
    cam, apd, geom, matrix, _ = bf.scene(rotation_deg=33.0, apd_counts=(120, 100))
    result = register_bead_images(cam, apd, geom, BeadRegistrationOptions(target_axes=("Y", "X")))
    assert len(result.camera_detections) > 5 * len(result.scan_detections)
    assert _error_um(result, matrix) < 0.08


def test_a_fast_axis_shift_of_the_scan_image_is_absorbed_as_translation():
    """A phase delay moves the scan image along its fast axis: the fit maps
    camera pixels to where the scan *shows* the beads, with rotation and
    scale untouched, so a scan with the same phase delay lands on target."""
    shift_px = 4.0
    cam, apd, geom, matrix, _ = bf.scene(rotation_deg=12.0, fast_shift_px=shift_px)
    result = register_bead_images(cam, apd, geom, BeadRegistrationOptions(target_axes=("Y", "X")))
    fast_step = geom["axes"][0]["step_um"]
    expected = _truth(matrix, _CORNERS) + np.array([0.0, shift_px * fast_step])     # (Y, X), X fast
    assert np.abs(result.transform.map_points(_CORNERS) - expected).max() < 0.06
    g, t = decompose_affine(result.matrix), decompose_affine(np.vstack([matrix, [0, 0, 1]]))
    assert abs(g["rotation_deg"] - t["rotation_deg"]) < 0.3 and abs(g["scale"] / t["scale"] - 1) < 0.005
    assert result.transform.provenance["estimator"] == "similarity_reflect"


def test_an_anisotropic_scanner_is_fitted_as_an_affine_when_auto():
    """One galvo's conversion factor 6 % off: the scan's micrometres are
    stretched along one axis, which a similarity cannot describe."""
    cam, apd, geom, matrix, beads = bf.scene(rotation_deg=25.0)
    stretched = dict(geom)
    stretched["axes"] = [dict(geom["axes"][0], step_um=geom["axes"][0]["step_um"] * 1.06,
                              first_um=geom["axes"][0]["first_um"] * 1.06), geom["axes"][1]]
    result = register_bead_images(cam, apd, stretched, BeadRegistrationOptions(target_axes=("Y", "X")))
    assert result.transform.provenance["estimator"] == "affine"
    expected = _truth(matrix, _CORNERS) * np.array([1.0, 1.06])
    assert np.abs(result.transform.map_points(_CORNERS) - expected).max() < 0.08


def test_target_axes_order_and_y_fast_scans():
    cam, apd, geom, matrix, _ = bf.scene(rotation_deg=-20.0, fast="Y", slow="X")
    a = register_bead_images(cam, apd, geom, BeadRegistrationOptions(target_axes=("Y", "X")))
    b = register_bead_images(cam, apd, geom)          # default: (slow, fast) = (X, Y)
    assert b.target_axes == ("X", "Y")
    np.testing.assert_allclose(a.transform.map_points(_CORNERS), b.transform.map_points(_CORNERS)[:, ::-1],
                               atol=1e-6)


def test_scan_geometry_affine_follows_the_scan_geometry_convention():
    geom = {"axes": [{"device": "X", "step_um": 0.2, "first_um": -3.0},
                     {"device": "Y", "step_um": 0.1, "first_um": 5.0}]}
    affine, axes = scan_geometry_affine(geom)
    assert axes == ("Y", "X")
    np.testing.assert_allclose(affine.map_points([[0, 0], [10, 20]]), [[5.0, -3.0], [6.0, 1.0]])
    affine, axes = scan_geometry_affine(geom, ("X", "Y"))
    np.testing.assert_allclose(affine.map_points([[10, 20]]), [[1.0, 6.0]])
    with pytest.raises(BeadRegistrationError):
        scan_geometry_affine(geom, ("Z", "X"))


def test_too_few_beads_is_refused():
    cam, apd, geom, _, _ = bf.scene(n_beads=12)
    with pytest.raises(BeadRegistrationError, match="too few beads"):
        register_bead_images(cam, apd, geom)


def test_a_regular_grid_is_refused_as_ambiguous():
    grid = np.array([[y, x] for y in np.arange(-40, 40, 2.5) for x in np.arange(-40, 40, 2.5)])
    cam, apd, geom, _, _ = bf.scene(beads_um=grid + [0.3, -0.2])
    with pytest.raises(BeadRegistrationError, match="ambiguous"):
        register_bead_images(cam, apd, geom)


def test_different_bead_fields_do_not_pair():
    cam, _, _, _, _ = bf.scene(seed=1)
    _, apd, geom, _, _ = bf.scene(seed=2)
    with pytest.raises(BeadRegistrationError):
        register_bead_images(cam, apd, geom)


def test_residuals_above_the_limit_are_refused():
    cam, apd, geom, _, _ = bf.scene(rotation_deg=8.0)
    with pytest.raises(BeadRegistrationError, match="residuals too large"):
        register_bead_images(cam, apd, geom, BeadRegistrationOptions(max_residual=0.001))


def test_scale_discrepancy_warns():
    cam, apd, geom, _, _ = bf.scene(rotation_deg=8.0)
    result = register_bead_images(cam, apd, geom, BeadRegistrationOptions(camera_pixel_um=0.216))
    assert any("configured pixel size" in w for w in result.warnings)
    quiet = register_bead_images(cam, apd, geom, BeadRegistrationOptions(camera_pixel_um=0.108))
    assert not quiet.warnings


def test_saturated_and_merged_beads_are_rejected():
    rng = np.random.default_rng(0)
    positions = [(30, 30), (30, 34), (60, 60), (90, 30)]          # a merged pair, two singles
    image = bf.render((120, 120), positions, 1.5, [800, 800, 70000, 800], 50, rng)
    found = detect_beads(image)
    assert found.rejected["saturated"] >= 1
    assert found.rejected["merged"] >= 2 or found.rejected["width"] >= 1
    assert len(found) == 1
    np.testing.assert_allclose(found.positions[0], (90, 30), atol=0.15)


def test_the_record_round_trips_through_the_module_io(tmp_path):
    cam, apd, geom, matrix, _ = bf.scene(rotation_deg=71.0, mirror=True)
    context = AcquisitionContext(detector="Camera", image_shape=cam.shape, roi=(0, 0, cam.shape[1], cam.shape[0]),
                                 binning=1, pixel_size_um=(0.108, 0.108))
    result = register_bead_images(cam, apd, geom, BeadRegistrationOptions(target_axes=("Y", "X")),
                                  source_frame="detector:Camera", target_frame="scanner:galvo",
                                  camera_context=context, provenance={"files": ["a.h5", "b.h5"]})
    path = io.save_json(tmp_path / "t.json", result.transform)
    loaded = io.load_json(path)
    np.testing.assert_allclose(loaded.map_points(_CORNERS), result.transform.map_points(_CORNERS))
    document = json.loads(path.read_text())
    assert document["units"] == "um" and document["kind"] == "affine"
    assert document["target_context"]["axes"] == ["Y", "X"]
    assert document["source_context"]["roi"] == [0, 0, 480, 400]
    assert document["provenance"]["beads_used"] >= 8 and document["provenance"]["files"] == ["a.h5", "b.h5"]
    assert document["provenance"]["mirror"] is True
    assert document["residuals"]["unit"] == "um"
    inverse = loaded.inverse()
    np.testing.assert_allclose(inverse.map_points(loaded.map_points(_CORNERS)), _CORNERS, atol=1e-9)


def test_the_bead_strategy_is_registered_and_pairs_through_the_registry():
    spec = {s.name: s for s in builtin_correspondence_specs()}["beads"]
    cam, apd, geom, matrix, _ = bf.scene(rotation_deg=-35.0, mirror=True)
    step = geom["axes"][0]["step_um"]
    result = spec.run(cam, apd, {}, target_parameters={"pixel_size_row": step, "pixel_size_col": step})
    assert result.pair_count >= 8 and result.metadata["mirror"] is True
    # Pixel -> pixel, then the module's fitter: a mirrored similarity fits it.
    fit = fit_transform("similarity_reflect", result.source_points, result.target_points, residual_threshold=1.0)
    assert fit.residuals.rms < 0.3
    assert np.linalg.det(fit.transform.as_matrix()[:2, :2]) < 0


def test_similarity_reflect_recovers_a_mirrored_similarity_and_similarity_does_not():
    rng = np.random.default_rng(1)
    source = rng.uniform(0, 100, (30, 2))
    truth = bf.similarity_matrix(0.7, 37.0, True, (5.0, -2.0))
    target = _truth(truth, source)
    fit = fit_transform("similarity_reflect", source, target)
    np.testing.assert_allclose(fit.transform.as_matrix()[:2], truth, atol=1e-9)
    assert fit_transform("similarity", source, target).residuals.rms > 1.0
    proper = bf.similarity_matrix(0.7, 37.0, False, (5.0, -2.0))
    fit = fit_transform("similarity_reflect", source, _truth(proper, source))
    np.testing.assert_allclose(fit.transform.as_matrix()[:2], proper, atol=1e-9)
