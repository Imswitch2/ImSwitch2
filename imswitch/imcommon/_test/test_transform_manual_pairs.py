"""Manual point pairs (clicks -> pairs -> fit -> residuals), snapping, the
degeneracy checks, and the bead detector's PSF-free default."""
import numpy as np
import pytest

from imswitch.imcommon._test import _bead_fields as bf
from imswitch.imcommon.algorithms.transforms import AffineTransform, fit_transform
from imswitch.imcommon.algorithms.transforms.calibration_report import (
    context_problems,
    describe_transform,
    qc_figure,
    verify_on_points,
)
from imswitch.imcommon.algorithms.transforms.correspondence import (
    DegeneratePointsError,
    PointPairEditor,
    check_point_geometry,
    detect_beads,
    estimate_spot_scale,
    fit_point_pairs,
    manual_correspondence,
    manual_strategy_spec,
    minimum_pairs,
    minimum_pairs_table,
    snap_to_local_maximum,
)
from imswitch.imcommon.algorithms.transforms import AcquisitionContext, SpatialTransform

pytestmark = pytest.mark.nohardware

TRUTH = AffineTransform.from_linear_offset(
    1.7 * np.array([[np.cos(0.4), -np.sin(0.4)], [np.sin(0.4), np.cos(0.4)]]) @ np.diag([1.0, -1.0]),
    [12.0, -5.0])
SOURCE = np.array([[10.0, 12.0], [80.0, 20.0], [30.0, 90.0], [70.0, 75.0], [50.0, 45.0], [15.0, 60.0]])


def test_minimum_pairs_per_model():
    table = minimum_pairs_table()
    assert table["translation"] == 1 and table["identity"] == 1 and table["rotation90"] == 1
    assert table["rigid"] == table["similarity"] == table["similarity_reflect"] == 2
    assert table["similarity_or_affine"] == 2
    assert table["affine"] == 3
    assert (minimum_pairs("polynomial", 2), minimum_pairs("polynomial", 3), minimum_pairs("polynomial", 4)) == (6, 10, 15)


def test_clicks_make_pairs_in_either_order_with_undo():
    editor = PointPairEditor()
    assert editor.next_role() == "source"
    editor.add_point("moving", (1, 2))
    assert editor.open_pair == 0 and editor.next_role() == "target"
    editor.add_point("moving", (1.5, 2.5))            # a second try replaces the open click
    assert len(editor) == 1 and editor.pairs[0].source == (1.5, 2.5)
    editor.add_point("reference", (3, 4))
    assert editor.open_pair is None and editor.pairs[0].complete
    editor.add_point("reference", (5, 6))              # Reference first starts a new pair
    assert editor.next_role() == "source"
    editor.add_point("source", (7, 8))
    editor.add_point("source", (9, 9))
    source, target, indices = editor.complete_pairs()
    assert source.tolist() == [[1.5, 2.5], [7, 8]] and target.tolist() == [[3, 4], [5, 6]]
    assert indices.tolist() == [0, 1] and len(editor) == 3
    assert editor.move(2, -2) == 0 and editor.pairs[0].source == (9.0, 9.0)
    editor.delete(0)
    assert len(editor) == 2
    assert editor.undo() and len(editor) == 3      # the delete
    assert editor.undo() and editor.pairs[2].source == (9.0, 9.0)   # the move
    editor.clear()
    assert len(editor) == 0 and editor.undo() and len(editor) == 3
    while editor.undo():
        pass
    assert len(editor) == 0 and not editor.can_undo


def test_a_fit_from_clicks_reports_each_pairs_residual_and_singles_out_a_bad_click():
    target = TRUTH.map_points(SOURCE)
    rng = np.random.default_rng(3)
    clicked = target + rng.normal(0, 0.2, target.shape)
    clicked[3] += (6.0, -4.0)                     # one bad click
    result = fit_point_pairs(SOURCE, clicked, "similarity_reflect")
    assert result.fit.inliers.all() and result.fit.residuals.n_points == 6   # least squares: nothing dropped
    assert len(result.residuals) == 6 and result.loo is not None
    assert result.worst == 3
    assert result.loo[3] > 5 and result.loo[3] > 2 * np.nanmax(np.delete(result.loo, 3))
    good = fit_point_pairs(np.delete(SOURCE, 3, 0), np.delete(clicked, 3, 0), "similarity_reflect")
    np.testing.assert_allclose(good.fit.transform.map_points(SOURCE), TRUTH.map_points(SOURCE), atol=0.5)
    assert good.fit.residuals.rms < 0.5


def test_with_exactly_the_minimum_the_fit_passes_through_every_click():
    target = TRUTH.map_points(SOURCE[:3])
    result = fit_point_pairs(SOURCE[:3], target, "affine")
    assert result.loo is None and result.fit.residuals.max < 1e-9


@pytest.mark.parametrize("model, source, message", [
    ("affine", [[0, 0], [10, 10], [20, 20], [35, 35]], "lie on a line"),
    ("polynomial", [[0, 0], [1, 0], [2, 0], [3, 0], [4, 0], [5, 0]], "lie on a line"),
    ("similarity", [[5, 5], [5, 5]], "same spot"),
    ("affine", [[0, 0], [10, 0]], "at least 3"),
    ("rigid", [[1, 1]], "at least 2"),
])
def test_degenerate_sets_are_refused_with_a_reason(model, source, message):
    source = np.asarray(source, float)
    with pytest.raises(DegeneratePointsError, match=message):
        check_point_geometry(source, source * 2 + 1, model)


def test_collinear_points_are_fine_for_a_similarity():
    source = np.array([[0, 0], [10, 10], [20, 20.0]])
    result = fit_point_pairs(source, TRUTH.map_points(source), "similarity_reflect")
    assert result.fit.residuals.max < 1e-6


def test_snapping_moves_a_click_onto_the_spot_centre():
    image = bf.render((60, 60), [(20.3, 31.6)], 1.6, [1000], 20, np.random.default_rng(0))
    snapped = snap_to_local_maximum(image, (22, 30), radius=3)
    assert np.hypot(snapped[0] - 20.3, snapped[1] - 31.6) < 0.3
    assert snap_to_local_maximum(image, (50, 10), radius=0) == (50.0, 10.0)        # off
    flat = np.zeros((10, 10))
    assert snap_to_local_maximum(flat, (4, 5), radius=3) == (4.0, 5.0)


def test_manual_pairs_are_a_correspondence_strategy():
    result = manual_correspondence(SOURCE, TRUTH.map_points(SOURCE))
    assert result.pair_count == 6 and result.metadata["strategy"] == "manual"
    spec = manual_strategy_spec()
    assert spec.name == "manual"
    with pytest.raises(ValueError, match="clicked"):
        spec.run(np.zeros((4, 4)), np.zeros((4, 4)), {})


def test_auto_model_keeps_a_similarity_unless_the_affine_is_clearly_better():
    source = np.random.default_rng(0).uniform(0, 100, (30, 2))
    target = TRUTH.map_points(source) + np.random.default_rng(1).normal(0, 0.05, (30, 2))
    fit = fit_transform("similarity_or_affine", source, target)
    assert fit.estimator == "similarity_reflect" and "not significantly better" in fit.selection["rule"]
    sheared = AffineTransform.from_linear_offset([[1.0, 0.3], [0.0, 1.2]], [3.0, 4.0])
    fit = fit_transform("similarity_or_affine", source, sheared.map_points(source))
    assert fit.estimator == "affine" and "lowers the RMS" in fit.selection["rule"]
    few = fit_transform("similarity_or_affine", source[:5], sheared.map_points(source[:5]))
    assert few.estimator == "similarity_reflect" and "fewer than 10" in few.selection["rule"]


def test_bead_detection_does_not_need_a_psf_by_default():
    cam, _apd, _geom, _matrix, _ = bf.scene(rotation_deg=10.0)
    default = detect_beads(cam)
    measured = detect_beads(cam, measure_psf=True)
    assert default.scale_source == "estimated" and measured.scale_source == "measured"
    assert detect_beads(cam, sigma_px=1.5).scale_source == "given"
    assert len(default) >= 0.9 * len(measured)
    # The same beads, at the same sub-pixel positions.
    from scipy import spatial
    distance, _ = spatial.cKDTree(default.positions).query(measured.positions)
    assert np.median(distance) < 0.02
    # Nothing to fit, nothing to fail: an empty image gives a scale and no beads.
    empty = np.random.default_rng(0).poisson(100, (64, 64)).astype(np.uint16)
    assert estimate_spot_scale(empty) > 0
    assert len(detect_beads(empty)) == 0


def test_estimated_spot_scale_follows_the_spot_size():
    for sigma in (1.2, 2.0, 3.0):
        positions = [(20 + 25 * i, 20 + 25 * j) for i in range(4) for j in range(4)]
        image = bf.render((130, 130), positions, sigma, [900] * len(positions), 30, np.random.default_rng(1))
        assert estimate_spot_scale(image) == pytest.approx(sigma, rel=0.2)


def test_report_verify_context_and_figure(tmp_path):
    target = TRUTH.map_points(SOURCE)
    pairs = fit_point_pairs(SOURCE, target, "similarity_reflect")
    transform = SpatialTransform(pairs.fit.transform, "moving", "reference", residuals=pairs.fit.residuals,
                                 source_context=AcquisitionContext(image_shape=(100, 100), binning=1))
    text = "\n".join(describe_transform(transform, pairs.fit))
    assert "similarity_reflect" in text and "MIRRORED" in text and "Scale 1.70000" in text
    check = verify_on_points(transform, SOURCE, target + 0.1, tolerance=1.0)
    assert check["matched"] == 6 and check["rms"] == pytest.approx(np.hypot(0.1, 0.1), abs=1e-6)
    assert context_problems(transform.source_context, AcquisitionContext(image_shape=(100, 100))) == []
    assert context_problems(transform.source_context, AcquisitionContext(image_shape=(50, 50), binning=2))
    image = np.random.default_rng(0).random((100, 100))
    path = qc_figure(image, image, SOURCE, target, pairs.fit.mapped_points, path=tmp_path / "qc.png")
    assert path.exists() and path.stat().st_size > 1000
