"""Phase-1 conversions tested against both native localization contracts."""

import numpy as np
import pytest

from imswitch.imcommon.algorithms.grid_localization import (
    DetectedSpots, LatticeModel, LatticeRegistration, PatternPrior,
    from_finite_grid, from_lattice_registration, from_periodic_localization,
    finite_grid_components, periodic_components,
)
from imswitch.imcommon.algorithms.grid_localization.geometry import LatticeRepresentation
from imswitch.imcommon.algorithms.grid_localization.lattice import rectangular_lattice_indices
from imswitch.imcontrol.model.ulenses_localizer import GridLocalizationResult
from imswitch.improcess.reconstructors.monalisa.calibration import PatternLocalization
from imswitch.improcess.reconstructors.monalisa.lattice import Lattice


def _finite_grid(axis=None, stagger=0.0):
    indices = np.asarray([(i, j) for j in range(4) for i in range(5)], dtype=np.int64)
    origin = np.array([27.5, 41.0])  # row, col in the FULL image
    basis_x = np.array([1.0, 14.0])  # row, col
    basis_y = np.array([13.0, -0.5])
    points = []
    for i, j in indices:
        p = origin + i * basis_x + j * basis_y
        if axis == "rows":
            p = p + (j % 2) * stagger * basis_x
        elif axis == "columns":
            p = p + (i % 2) * stagger * basis_y
        points.append(p)
    points = np.array(points)
    matched = np.ones(len(points), dtype=bool)
    matched[[3, 14]] = False
    measured = points.copy() + [0.02, -0.01]
    measured[~matched] = np.nan
    return GridLocalizationResult(
        origin_rc=origin, basis_x_rc=basis_x, basis_y_rc=basis_y,
        n_cols=5, n_rows=4, stagger_axis=axis, stagger_fraction=stagger,
        grid_points_rc=points, logical_indices=indices,
        detected_points_rc=measured[matched].copy(),
        matched_mask=matched, measured_points_rc=measured,
        num_cols=5, num_rows=4,
        diagnostics={"grid_size_source": "manual", "crop_coord": (17, 140, 28, 180)},
    )


@pytest.mark.parametrize("axis,fraction", [(None, 0.), ("rows", 0.4), ("columns", 0.35)])
def test_finite_grid_roundtrip_preserves_logical_ids_masks_and_xy(axis, fraction):
    source = _finite_grid(axis, fraction)
    common = from_finite_grid(source, prior=PatternPrior(finite_shape_xy=(5, 4)))
    assert common.strategy == "finite"
    np.testing.assert_array_equal(common.geometry.site_indices_ij, source.logical_indices)
    np.testing.assert_allclose(common.predicted_positions_xy, source.grid_points_rc[:, ::-1])
    np.testing.assert_array_equal(common.matched_mask, source.matched_mask)
    assert common.geometry.stagger_axis == {None: None, "rows": "x", "columns": "y"}[axis]
    assert common.prior_usage["finite_shape_xy"] == "not_evaluated"
    restored = GridLocalizationResult(**finite_grid_components(common))
    assert restored.stagger_axis == source.stagger_axis
    assert restored.stagger_fraction == source.stagger_fraction
    np.testing.assert_allclose(restored.grid_points_rc, source.grid_points_rc)
    np.testing.assert_array_equal(restored.logical_indices, source.logical_indices)
    np.testing.assert_array_equal(restored.matched_mask, source.matched_mask)
    np.testing.assert_allclose(restored.measured_points_rc, source.measured_points_rc, equal_nan=True)
    np.testing.assert_allclose(restored.detected_points_rc, source.detected_points_rc)
    assert restored.diagnostics == source.diagnostics


@pytest.mark.parametrize("pattern", [
    "rectangular", "rotated_hexagonal", "half_period_bravais", "non_half_motif",
])
def test_periodic_roundtrip_keeps_basis_motif_and_serialized_schema(pattern):
    if pattern == "rectangular":
        native = PatternLocalization.from_lattice(Lattice.rectangular(10, 12, 1.3, 2.1), method="auto")
    elif pattern == "rotated_hexagonal":
        native = PatternLocalization.from_lattice(Lattice.hexagonal(10, angle_rad=0.19), method="auto")
    elif pattern == "half_period_bravais":
        native = PatternLocalization.manual_grid(10, 12, stagger_axis="x", stagger=-0.5)
    else:
        native = PatternLocalization.manual_grid(10, 12, stagger_axis="y", stagger=0.3)
    common = from_periodic_localization(native)
    args = periodic_components(common)
    roundtrip = PatternLocalization(
        lattice=Lattice(a1=args["a1"], a2=args["a2"], offset=args["offset"]),
        motif=args["motif"], provenance=args["provenance"],
    )
    assert roundtrip.to_dict() == native.to_dict()
    assert PatternLocalization.from_dict(roundtrip.to_dict()).to_dict() == native.to_dict()
    for shape in ((100, 130), (180, 230)):
        xy = native.points_in_frame(*shape)
        repro = roundtrip.points_in_frame(*shape)
        np.testing.assert_allclose(repro[0], xy[0], rtol=0, atol=0)
        np.testing.assert_allclose(repro[1], xy[1], rtol=0, atol=0)
    assert common.predicted_positions_xy is None  # no premature FOV enumeration


def test_finite_registration_crop_conversion_and_motif_survive():
    indices = rectangular_lattice_indices(3, 3)
    representation = LatticeRepresentation.alternating_rows(indices, 0.6)
    logical = representation.centered_logical_positions
    model = LatticeModel(lattice_indices=indices, logical_positions=logical,
                         representation=representation,
                         diagnostics={"lattice_size_source": "provided"})
    matrix = np.array([[11., 1.], [0.5, 13.]])
    translation = np.array([17., 25.])  # position relative to cropped image
    expected = matrix @ logical + translation[:, None]
    detected = DetectedSpots(
        positions_px=expected, intensities=np.ones(expected.shape[1]),
        scores=np.ones(expected.shape[1]),
        cropped_image=np.zeros((80, 90)), processed_image=np.zeros((80, 90)),
        crop_coord=(21, 101, 30, 120),
    )
    registration = LatticeRegistration(
        model=model, detections=detected,
        affine_linear=matrix, affine_translation=translation,
        expected_positions_px=expected, measured_positions_px=expected,
        matched_mask=np.ones(expected.shape[1], dtype=bool),
        detection_indices=np.arange(expected.shape[1]),
        residuals_px=np.zeros_like(expected), rms_residual_px=0.0,
    )
    common = from_lattice_registration(registration)
    assert common.geometry.representation is representation
    np.testing.assert_array_equal(common.geometry.site_indices_ij, indices.T)
    np.testing.assert_allclose(common.predicted_positions_xy, expected.T + [30, 21])
    np.testing.assert_allclose(common.measured_positions_xy, expected.T + [30, 21])
    assert common.diagnostics["crop_coord"] == (21, 101, 30, 120)
    assert common.diagnostics["finite_indices_source"] == "provided"
    assert common.geometry.stagger_fraction == pytest.approx(0.6)
