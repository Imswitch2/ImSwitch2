"""Tests for the shared spatial transform module (Step 0).

Two of these carry more weight than the rest:

* :func:`test_recovers_known_affine_from_synthetic_grid` -- the ground-truth
  test. Synthesize a known transform, build the moved image from it, recover it
  through the real calibration path, and assert the recovered matrix matches.
  It needs no rig, and it is the check that says the whole chain is correct
  rather than merely self-consistent.
* :func:`test_fast_path_matches_generic_path` -- every model-specific shortcut
  must agree with the generic ``map_coordinates`` route. A fast path that
  silently disagrees with the general one would be the worst possible bug in
  this module.
"""

import numpy as np
import pytest

from imswitch.imcommon.algorithms.roi import ROIRecord
from imswitch.imcommon.algorithms.transforms import (
    AcquisitionContext,
    AffineTransform,
    ComposedTransform,
    IdentityTransform,
    ResidualStats,
    Rotation90Transform,
    SpatialTransform,
    estimate_affine,
    estimator_names,
    estimator_specs,
    fit_transform,
    load,
    model_from_params,
    model_kinds,
    save,
    transform_image,
    transform_mask,
    transform_points,
    transform_roi,
)
from imswitch.imcommon.algorithms.transforms.conventions import (
    matrix_to_ndimage,
    pixel_matrix_to_world,
    points_xy_to_yx,
    world_matrix_to_pixel,
    xy_matrix_to_yx,
)
from imswitch.imcommon.algorithms.transforms.correspondence import (
    estimate_grid_angle,
    find_grid_correspondence,
    order_grid_points,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

IMAGE_SHAPE = (256, 256)
GRID_ROWS = 4
GRID_COLS = 4
GRID_START = 56.0
GRID_STEP = 48.0


def grid_positions():
    """The reference foci grid, as ``(N, 2)`` ``(row, col)`` centres."""
    rows = GRID_START + GRID_STEP * np.arange(GRID_ROWS, dtype=np.float64)
    cols = GRID_START + GRID_STEP * np.arange(GRID_COLS, dtype=np.float64)
    return np.array([[row, col] for row in rows for col in cols], dtype=np.float64)


def foci_image(points, shape=IMAGE_SHAPE, sigma=2.0, amplitude=1000.0):
    """Render Gaussian spots at ``points`` on a dim background."""
    rows = np.arange(shape[0], dtype=np.float64)[:, None]
    cols = np.arange(shape[1], dtype=np.float64)[None, :]
    picture = np.full(shape, 5.0, dtype=np.float64)
    for row, col in np.asarray(points, dtype=np.float64):
        picture += amplitude * np.exp(
            -(((rows - row) ** 2 + (cols - col) ** 2) / (2.0 * sigma**2))
        )
    return picture


def known_affine(angle_deg=5.0, scale=1.05, shift=(3.0, -2.0)):
    """A modest rotate/scale/translate about the image centre."""
    angle = np.deg2rad(angle_deg)
    linear = scale * np.array(
        [[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]],
        dtype=np.float64,
    )
    centre = np.array(
        [(IMAGE_SHAPE[0] - 1) / 2.0, (IMAGE_SHAPE[1] - 1) / 2.0], dtype=np.float64
    )
    offset = centre - linear @ centre + np.asarray(shift, dtype=np.float64)
    return AffineTransform.from_linear_offset(linear, offset)


DETECTION = {
    "n_rows": GRID_ROWS,
    "n_cols": GRID_COLS,
    "min_distance": 15,
    "threshold_rel": 0.2,
    "refine_radius": 6,
    "gaussian_sigma": 1.0,
}


def sample_models():
    """One instance of every model that has a warp fast path."""
    return [
        IdentityTransform(ndim=2),
        Rotation90Transform(k=1, input_shape=(37, 53)),
        Rotation90Transform(k=2, input_shape=(37, 53)),
        Rotation90Transform(k=3, input_shape=(37, 53)),
        AffineTransform.from_linear_offset(
            [[0.98, 0.11], [-0.09, 1.03]], [2.5, -1.75]
        ),
        AffineTransform.translation([4.0, -3.0]),
    ]


# ---------------------------------------------------------------------------
# The ground-truth test
# ---------------------------------------------------------------------------


def test_recovers_known_affine_from_synthetic_grid():
    """Build the moved grid from a known affine; the fit must recover it."""
    truth = known_affine()
    source_points = grid_positions()
    target_points = truth.map_points(source_points)

    source_image = foci_image(source_points)
    target_image = foci_image(target_points)

    detected_source, detected_target = find_grid_correspondence(
        source_image, target_image, **DETECTION
    )
    fit = estimate_affine(detected_source, detected_target)

    np.testing.assert_allclose(
        fit.transform.matrix[:2, :2], truth.matrix[:2, :2], atol=2e-3
    )
    np.testing.assert_allclose(
        fit.transform.matrix[:2, 2], truth.matrix[:2, 2], atol=0.3
    )
    assert fit.residuals.median < 0.1
    assert fit.residuals.n_inliers == GRID_ROWS * GRID_COLS


def test_recovers_known_affine_through_a_real_warp():
    """Same, but the moved image is produced by ``transform_image`` itself.

    This exercises the resampling path end to end, so interpolation error is
    included in what the fit has to see through. Tolerances are looser than the
    analytic case for exactly that reason.
    """
    truth = known_affine(angle_deg=4.0, scale=1.0, shift=(2.0, -3.0))
    source_points = grid_positions()
    source_image = foci_image(source_points)
    target_image = transform_image(source_image, truth, order=1)

    detected_source, detected_target = find_grid_correspondence(
        source_image, target_image, **DETECTION
    )
    fit = estimate_affine(detected_source, detected_target)

    np.testing.assert_allclose(
        fit.transform.matrix[:2, :2], truth.matrix[:2, :2], atol=5e-3
    )
    np.testing.assert_allclose(
        fit.transform.matrix[:2, 2], truth.matrix[:2, 2], atol=1.0
    )
    assert fit.residuals.median < 0.5


# ---------------------------------------------------------------------------
# Fast paths must equal the generic path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", sample_models(), ids=lambda m: repr(m)[:40])
def test_fast_path_matches_generic_path(model):
    rng = np.random.default_rng(0)
    image = rng.random((37, 53)) * 100.0
    out_shape = (
        model.output_shape
        if isinstance(model, Rotation90Transform)
        else image.shape
    )

    fast = transform_image(image, model, out_shape=out_shape, prefer_fast=True)
    generic = transform_image(image, model, out_shape=out_shape, prefer_fast=False)

    assert fast.shape == generic.shape
    np.testing.assert_allclose(fast, generic, atol=1e-9)


@pytest.mark.parametrize("k", [0, 1, 2, 3])
def test_rotation_matches_numpy_rot90(k):
    """The rotation matrix and ``np.rot90`` must agree pixel for pixel."""
    rng = np.random.default_rng(1)
    image = rng.random((7, 11))
    model = Rotation90Transform(k=k, input_shape=image.shape)

    expected = np.rot90(image, k, axes=(-2, -1))
    assert model.output_shape == expected.shape

    # Every source pixel must land where np.rot90 put its value.
    rows, cols = np.indices(image.shape)
    source = np.column_stack([rows.ravel(), cols.ravel()]).astype(np.float64)
    mapped = np.rint(model.map_points(source)).astype(int)
    np.testing.assert_array_equal(
        expected[mapped[:, 0], mapped[:, 1]], image[rows.ravel(), cols.ravel()]
    )


def test_rotation_stack_transforms_every_plane():
    rng = np.random.default_rng(2)
    stack = rng.random((3, 5, 8, 12))
    model = Rotation90Transform(k=1, input_shape=(8, 12))

    result = transform_image(stack, model, out_shape=model.output_shape)

    assert result.shape == (3, 5, 12, 8)
    np.testing.assert_allclose(result, np.rot90(stack, 1, axes=(-2, -1)))


# ---------------------------------------------------------------------------
# Model contract, parametrized over the registry
# ---------------------------------------------------------------------------


def test_registry_holds_the_step0_kinds():
    assert set(model_kinds()) >= {"identity", "rotation90", "affine", "composed"}


@pytest.mark.parametrize("model", sample_models(), ids=lambda m: m.kind)
def test_inverse_round_trips_points(model):
    rng = np.random.default_rng(3)
    points = rng.random((25, 2)) * 30.0
    round_tripped = model.inverse().map_points(model.map_points(points))
    np.testing.assert_allclose(round_tripped, points, atol=1e-9)


@pytest.mark.parametrize("model", sample_models(), ids=lambda m: m.kind)
def test_params_round_trip_through_the_registry(model):
    rebuilt = model_from_params(model.kind, model.to_params())
    rng = np.random.default_rng(4)
    points = rng.random((10, 2)) * 20.0
    np.testing.assert_allclose(rebuilt.map_points(points), model.map_points(points))


@pytest.mark.parametrize("model", sample_models(), ids=lambda m: m.kind)
def test_wrappers_never_branch_on_kind(model):
    """Every payload wrapper must accept every registered kind."""
    rng = np.random.default_rng(5)
    image = rng.random((37, 53))
    out_shape = (
        model.output_shape
        if isinstance(model, Rotation90Transform)
        else image.shape
    )

    assert transform_points(rng.random((6, 2)) * 10.0, model).shape == (6, 2)
    assert transform_image(image, model, out_shape=out_shape).shape == out_shape

    mask = rng.random((37, 53)) > 0.5
    warped_mask = transform_mask(mask, model, out_shape=out_shape)
    assert warped_mask.dtype == np.bool_
    assert warped_mask.shape == out_shape

    roi = transform_roi(
        ROIRecord(name="r", roi_type="rectangle", bounds=(2, 3, 20, 25)), model
    )
    assert isinstance(roi, ROIRecord)
    assert len(roi.bounds) == 4


def test_singular_affine_is_refused():
    with pytest.raises(ValueError, match="singular"):
        AffineTransform([[1.0, 2.0, 0.0], [2.0, 4.0, 0.0], [0.0, 0.0, 1.0]])


def test_affine_last_row_is_validated():
    with pytest.raises(ValueError, match="homogeneous"):
        AffineTransform([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.1, 0.0, 1.0]])


def test_rotation_needs_its_input_shape():
    with pytest.raises(ValueError, match="input_shape"):
        model_from_params("rotation90", {"k": 1})


# ---------------------------------------------------------------------------
# Non-affine models: the capability boundary
# ---------------------------------------------------------------------------


class _NonAffineModel(IdentityTransform):
    """Stand-in for a polynomial: no matrix, no analytic inverse."""

    kind = "test-nonaffine"

    def as_matrix(self):
        return None

    @property
    def has_analytic_inverse(self):
        return False


def test_non_affine_model_reports_no_matrix():
    model = _NonAffineModel()
    assert model.as_matrix() is None
    assert model.is_linear is False
    assert model.has_analytic_inverse is False


def test_composed_all_affine_collapses_to_a_matrix():
    first = SpatialTransform(
        model=AffineTransform.translation([2.0, 5.0]),
        source_frame="a",
        target_frame="b",
    )
    second = SpatialTransform(
        model=AffineTransform.from_linear_offset([[0.0, -1.0], [1.0, 0.0]], [0.0, 0.0]),
        source_frame="b",
        target_frame="c",
    )

    chained = first.then(second)

    assert chained.source_frame == "a"
    assert chained.target_frame == "c"
    assert chained.kind == "affine"
    assert chained.as_matrix() is not None
    assert chained.residuals is None

    points = np.array([[1.0, 2.0], [10.0, -4.0]])
    np.testing.assert_allclose(
        chained.map_points(points),
        second.map_points(first.map_points(points)),
    )


def test_composed_heterogeneous_chain_keeps_working_without_a_matrix():
    chain = ComposedTransform([AffineTransform.translation([1.0, 2.0]), _NonAffineModel()])
    assert chain.as_matrix() is None
    points = np.array([[3.0, 4.0]])
    np.testing.assert_allclose(chain.map_points(points), [[4.0, 6.0]])
    np.testing.assert_allclose(chain.inverse().map_points([[4.0, 6.0]]), points)


def test_chaining_refuses_frames_that_do_not_meet():
    first = SpatialTransform(
        model=IdentityTransform(), source_frame="a", target_frame="b"
    )
    second = SpatialTransform(
        model=IdentityTransform(), source_frame="c", target_frame="d"
    )
    with pytest.raises(ValueError, match="frames do not meet"):
        first.then(second)


def test_record_refuses_identical_frames():
    with pytest.raises(ValueError, match="must differ"):
        SpatialTransform(
            model=IdentityTransform(), source_frame="cam", target_frame="cam"
        )


# ---------------------------------------------------------------------------
# Conventions
# ---------------------------------------------------------------------------


def test_axis_swap_is_an_involution():
    matrix = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [0.0, 0.0, 1.0]])
    np.testing.assert_allclose(xy_matrix_to_yx(xy_matrix_to_yx(matrix)), matrix)


def test_axis_swap_agrees_with_swapping_the_points():
    matrix_yx = np.array([[0.9, 0.1, 4.0], [-0.2, 1.1, -3.0], [0.0, 0.0, 1.0]])
    points_yx = np.array([[5.0, 7.0], [11.0, 2.0]])
    mapped_yx = AffineTransform(matrix_yx).map_points(points_yx)

    matrix_xy = xy_matrix_to_yx(matrix_yx)
    points_xy = points_xy_to_yx(points_yx)
    mapped_xy = AffineTransform(matrix_xy).map_points(points_xy)

    np.testing.assert_allclose(points_xy_to_yx(mapped_xy), mapped_yx)


def test_pixel_world_conversion_round_trips():
    matrix = np.array([[0.98, 0.03, 5.0], [-0.02, 1.01, -4.0], [0.0, 0.0, 1.0]])
    scale = (0.16, 0.08)
    world = pixel_matrix_to_world(matrix, scale)
    np.testing.assert_allclose(world_matrix_to_pixel(world, scale), matrix)


def test_pixel_world_conversion_scales_the_translation():
    """A shift of N pixels is N * pixel_size in world units."""
    matrix = np.array([[1.0, 0.0, 10.0], [0.0, 1.0, 20.0], [0.0, 0.0, 1.0]])
    world = pixel_matrix_to_world(matrix, (0.5, 0.25))
    np.testing.assert_allclose(world[:2, 2], [5.0, 5.0])


def test_matrix_to_ndimage_returns_the_inverse_mapping():
    model = AffineTransform.from_linear_offset([[1.0, 0.0], [0.0, 1.0]], [3.0, 7.0])
    linear, offset = matrix_to_ndimage(model.matrix)
    np.testing.assert_allclose(linear, np.eye(2))
    np.testing.assert_allclose(offset, [-3.0, -7.0])


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _record():
    return SpatialTransform(
        model=known_affine(),
        source_frame="moving",
        target_frame="reference",
        units="px",
        source_context=AcquisitionContext(
            detector="CamA", image_shape=IMAGE_SHAPE, binning=1, pixel_size_um=(0.1, 0.1)
        ),
        target_context=AcquisitionContext(detector="CamB", image_shape=IMAGE_SHAPE),
        residuals=ResidualStats.from_residuals([0.1, 0.2, 0.15], threshold=2.0),
        provenance={"strategy": "foci_grid", "n_rows": 4, "n_cols": 4},
    )


@pytest.mark.parametrize("suffix", [".json", ".h5"])
def test_save_load_round_trip(tmp_path, suffix):
    original = _record()
    path = save(tmp_path / f"calibration{suffix}", original)
    restored = load(path)

    assert restored.source_frame == original.source_frame
    assert restored.target_frame == original.target_frame
    assert restored.units == original.units
    assert restored.kind == original.kind
    assert restored.created_at == original.created_at
    np.testing.assert_allclose(restored.as_matrix(), original.as_matrix())

    assert restored.residuals is not None
    assert restored.residuals.to_dict() == original.residuals.to_dict()
    assert restored.source_context.detector == "CamA"
    assert restored.source_context.pixel_size_um == (0.1, 0.1)
    assert restored.target_context.detector == "CamB"
    assert restored.provenance["strategy"] == "foci_grid"


def test_json_is_readable_and_carries_the_schema(tmp_path):
    import json

    path = save(tmp_path / "calibration.json", _record())
    document = json.loads(path.read_text())

    assert document["schema"] == "imswitch.transform/1"
    assert document["kind"] == "affine"
    assert document["source_frame"] == "moving"
    assert document["residuals"]["n_points"] == 3
    assert document["units"] == "px"


def test_unknown_schema_is_refused(tmp_path):
    path = tmp_path / "calibration.json"
    path.write_text('{"schema": "something/else", "kind": "identity"}')
    with pytest.raises(ValueError, match="unsupported transform schema"):
        load(path)


def test_unknown_kind_names_the_known_ones(tmp_path):
    with pytest.raises(ValueError, match="unknown transform kind"):
        model_from_params("displacement-field", {})


def test_unsupported_suffix_is_refused(tmp_path):
    with pytest.raises(ValueError, match="unsupported transform file suffix"):
        save(tmp_path / "calibration.npz", _record())


def test_h5_group_can_sit_next_to_other_data(tmp_path):
    """Transforms must be attachable to an existing recording, not replace it."""
    import h5py

    path = tmp_path / "recording.h5"
    with h5py.File(path, "w") as handle:
        handle.create_dataset("data", data=np.arange(12).reshape(3, 4))

    save(path, _record())

    with h5py.File(path, "r") as handle:
        assert "data" in handle
        assert "transform" in handle
        np.testing.assert_array_equal(handle["data"][:], np.arange(12).reshape(3, 4))

    assert load(path).kind == "affine"


# ---------------------------------------------------------------------------
# Correspondence
# ---------------------------------------------------------------------------


def test_order_grid_points_is_row_major():
    points = grid_positions()
    shuffled = points[np.random.default_rng(6).permutation(len(points))]
    np.testing.assert_allclose(
        order_grid_points(shuffled, GRID_ROWS, GRID_COLS), points
    )


def test_order_grid_points_refuses_a_partial_grid():
    points = grid_positions()[:-1]
    with pytest.raises(ValueError, match="expected exactly 16 points"):
        order_grid_points(points, GRID_ROWS, GRID_COLS)


@pytest.mark.parametrize("tilt_deg", [0.0, 3.0, -7.5, 20.0, -33.0])
def test_estimate_grid_angle_recovers_the_tilt(tilt_deg):
    angle = np.deg2rad(tilt_deg)
    rotation = AffineTransform.from_linear_offset(
        [[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]], [0.0, 0.0]
    )
    tilted = rotation.map_points(grid_positions())
    assert np.degrees(estimate_grid_angle(tilted)) == pytest.approx(tilt_deg, abs=1e-6)


def test_square_grid_ordering_survives_isotropic_spread():
    """Regression: a square grid carries no orientation in its covariance.

    Both singular values of a regular NxN grid are exactly equal, so the SVD
    axes the ImSwitch1 original used are arbitrary and can differ between two
    images of the same grid -- transposing the pairing and producing a
    confidently wrong transform. The tell is that only the *second* column of
    the recovered matrix is wrong.
    """
    points = grid_positions()
    centred = points - points.mean(axis=0)
    singular_values = np.linalg.svd(centred, compute_uv=False)
    assert singular_values[0] == pytest.approx(singular_values[1])

    truth = known_affine(angle_deg=12.0)
    ordered_source = order_grid_points(
        np.random.default_rng(7).permutation(points), GRID_ROWS, GRID_COLS
    )
    ordered_target = order_grid_points(
        np.random.default_rng(8).permutation(truth.map_points(points)),
        GRID_ROWS,
        GRID_COLS,
    )
    fit = estimate_affine(ordered_source, ordered_target)

    np.testing.assert_allclose(fit.transform.matrix, truth.matrix, atol=1e-6)


def test_refuses_grids_tilted_past_the_ambiguity_limit():
    """A near-quarter-turn cannot be ordered; it must refuse, not transpose."""
    source_points = grid_positions()
    angle = np.deg2rad(40.0)
    rotation = AffineTransform.from_linear_offset(
        [[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]],
        [0.0, 0.0],
    )
    centre = np.array([(IMAGE_SHAPE[0] - 1) / 2.0, (IMAGE_SHAPE[1] - 1) / 2.0])
    tilted = rotation.map_points(source_points - centre) + centre

    with pytest.raises(ValueError, match="tilted"):
        find_grid_correspondence(
            foci_image(source_points), foci_image(tilted), **DETECTION
        )


def test_correspondence_error_names_which_image_failed():
    good = foci_image(grid_positions())
    empty = np.full(IMAGE_SHAPE, 5.0)
    with pytest.raises(ValueError, match="target image"):
        find_grid_correspondence(good, empty, **DETECTION)


# ---------------------------------------------------------------------------
# Choosing the model class
# ---------------------------------------------------------------------------


def _similarity(angle_deg, scale, shift):
    angle = np.deg2rad(angle_deg)
    return AffineTransform.from_linear_offset(
        scale
        * np.array(
            [[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]]
        ),
        shift,
    )


def test_every_advertised_estimator_is_fittable():
    names = {spec.name for spec in estimator_specs()}
    assert names == set(estimator_names())
    assert names == {
        "affine",
        "similarity",
        "rigid",
        "translation",
        "rotation90",
        "identity",
    }


@pytest.mark.parametrize(
    "kind, truth",
    [
        ("translation", AffineTransform.translation([6.0, -4.0])),
        ("rigid", _similarity(9.0, 1.0, [3.0, 2.0])),
        ("similarity", _similarity(9.0, 1.2, [3.0, 2.0])),
        ("affine", known_affine()),
    ],
)
def test_each_estimator_recovers_its_own_model_exactly(kind, truth):
    source = grid_positions()
    fit = fit_transform(kind, source, truth.map_points(source))

    assert fit.estimator == kind
    np.testing.assert_allclose(fit.transform.as_matrix(), truth.matrix, atol=1e-9)
    assert fit.residuals.max < 1e-9


def test_constrained_estimators_refuse_to_absorb_shear():
    """A rigid fit of a sheared pair must show the error, not hide it.

    This is the point of offering the choice: a free affine will fit anything,
    including distortion that a constrained model would have flagged.
    """
    source = grid_positions()
    sheared = AffineTransform([[1.0, 0.35, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    target = sheared.map_points(source)

    rigid = fit_transform("rigid", source, target, residual_threshold=1e6)
    affine = fit_transform("affine", source, target, residual_threshold=1e6)

    assert rigid.residuals.rms > 5.0
    assert affine.residuals.rms < 1e-9


def test_constrained_fit_beats_affine_on_few_noisy_points():
    """Fewer degrees of freedom means less room to fit the noise."""
    rng = np.random.default_rng(11)
    truth = AffineTransform.translation([7.0, -5.0])
    source = grid_positions()[:4]
    target = truth.map_points(source) + rng.normal(scale=0.6, size=(4, 2))

    translation = fit_transform("translation", source, target, residual_threshold=1e6)
    affine = fit_transform("affine", source, target, residual_threshold=1e6)

    error_translation = np.abs(
        translation.transform.as_matrix()[:2, 2] - truth.matrix[:2, 2]
    ).max()
    error_affine = np.abs(affine.transform.as_matrix()[:2, 2] - truth.matrix[:2, 2]).max()
    assert error_translation < error_affine


def test_rigid_fit_never_mirrors():
    """The reflection guard: a mirrored 'rotation' fits but is physically wrong."""
    source = grid_positions()
    mirrored = AffineTransform([[1.0, 0.0, 0.0], [0.0, -1.0, 255.0], [0.0, 0.0, 1.0]])
    fit = fit_transform("rigid", source, mirrored.map_points(source))

    linear = fit.transform.as_matrix()[:2, :2]
    assert np.linalg.det(linear) > 0


@pytest.mark.parametrize("k", [0, 1, 2, 3])
def test_rotation90_estimator_picks_the_right_quarter_turn(k):
    shape = (256, 256)
    truth = Rotation90Transform(k=k, input_shape=shape)
    source = grid_positions()

    fit = fit_transform(
        "rotation90", source, truth.map_points(source), input_shape=shape
    )

    assert isinstance(fit.transform, Rotation90Transform)
    assert fit.transform.k == k
    assert fit.residuals.max < 1e-9


def test_rotation90_estimator_requires_the_input_shape():
    source = grid_positions()
    with pytest.raises(ValueError, match="needs input_shape"):
        fit_transform("rotation90", source, source)


def test_identity_estimator_measures_the_gap_without_fitting():
    source = grid_positions()
    target = source + np.array([3.0, 4.0])

    fit = fit_transform("identity", source, target)

    assert isinstance(fit.transform, IdentityTransform)
    assert fit.residuals.mean == pytest.approx(5.0)
    assert fit.residuals.n_inliers == 0


def test_unknown_estimator_names_the_known_ones():
    source = grid_positions()
    with pytest.raises(ValueError, match="unknown estimator"):
        fit_transform("projective", source, source)


@pytest.mark.parametrize(
    "kind, truth",
    [
        ("translation", AffineTransform.translation([4.0, -2.0])),
        ("rigid", _similarity(6.0, 1.0, [4.0, -2.0])),
        ("similarity", _similarity(6.0, 1.15, [4.0, -2.0])),
        ("affine", known_affine()),
    ],
)
def test_every_continuous_estimator_rejects_an_outlier(kind, truth):
    """Each model is given a ground truth it can actually represent."""
    source = grid_positions()
    target = truth.map_points(source)
    target[5] += np.array([50.0, -45.0])

    fit = fit_transform(kind, source, target, residual_threshold=2.0)

    assert not fit.inliers[5]
    assert fit.inliers.sum() == len(source) - 1


@pytest.mark.parametrize("spec", estimator_specs(), ids=lambda s: s.name)
def test_estimators_refuse_too_few_pairs(spec):
    if spec.min_samples < 2:
        pytest.skip("nothing is fewer than one pair")
    points = np.zeros((spec.min_samples - 1, 2))
    with pytest.raises(ValueError, match="point pair"):
        fit_transform(spec.name, points, points, input_shape=(16, 16))


def test_estimate_affine_still_delegates_to_the_affine_estimator():
    source = grid_positions()
    truth = known_affine()
    target = truth.map_points(source)
    np.testing.assert_allclose(
        estimate_affine(source, target).transform.matrix,
        fit_transform("affine", source, target).transform.as_matrix(),
    )


def test_estimate_affine_needs_three_pairs():
    points = np.array([[0.0, 0.0], [1.0, 1.0]])
    with pytest.raises(ValueError, match="at least 3 point pairs"):
        estimate_affine(points, points)


def test_estimate_affine_rejects_an_outlier():
    """A single bad pair must not drag the fit; RANSAC should exclude it."""
    truth = known_affine()
    source = grid_positions()
    target = truth.map_points(source)
    target[7] += np.array([40.0, -35.0])

    fit = estimate_affine(source, target, residual_threshold=2.0)

    assert not fit.inliers[7]
    assert fit.inliers.sum() == len(source) - 1
    np.testing.assert_allclose(
        fit.transform.matrix[:2, :2], truth.matrix[:2, :2], atol=1e-6
    )


# ---------------------------------------------------------------------------
# ROI / mask payloads
# ---------------------------------------------------------------------------


def test_transform_roi_translates_bounds():
    roi = ROIRecord(name="cell", roi_type="rectangle", bounds=(10, 20, 30, 40))
    moved = transform_roi(roi, AffineTransform.translation([5.0, -3.0]))
    assert moved.bounds == (15, 17, 35, 37)
    assert moved.name == "cell"


def test_transform_roi_bounding_box_grows_under_rotation():
    """A rotated rectangle is not a rectangle; the bbox is honestly larger."""
    roi = ROIRecord(name="r", roi_type="rectangle", bounds=(0, 0, 10, 40))
    angle = np.deg2rad(30.0)
    model = AffineTransform.from_linear_offset(
        [[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]], [0.0, 0.0]
    )
    rotated = transform_roi(roi, model)
    assert (rotated.bounds[2] - rotated.bounds[0]) > 10


def test_transform_mask_keeps_labels_intact():
    mask = np.zeros((20, 20), dtype=np.uint8)
    mask[5:10, 5:10] = 3
    moved = transform_mask(mask, AffineTransform.translation([2.0, 2.0]))
    assert moved.dtype == np.uint8
    assert set(np.unique(moved)) <= {0, 3}
    assert moved[7, 7] == 3
