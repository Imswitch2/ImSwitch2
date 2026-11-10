"""Joint fit-support modes ported from the MoNaLISA WIP investigation branch."""

import numpy as np
import pytest

from imswitch.improcess.reconstructors.monalisa.calibration import PatternLocalization
from imswitch.improcess.reconstructors.monalisa.extraction import ExtractionOperator
from imswitch.improcess.reconstructors.monalisa.lattice import Lattice
from imswitch.improcess.reconstructors.monalisa.params import default_params
from imswitch.improcess.reconstructors.monalisa.pipeline import PipelineParams, prepare_geometry


def _spots(shape, x, y, sigma, amplitudes, background=0.0):
    yy, xx = np.indices(shape)
    image = np.full(shape, background, dtype=float)
    for cx, cy, amplitude in zip(x, y, amplitudes):
        image += amplitude * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma**2))
    return image


def test_joint_default_matches_historical_sigma_scaled_radius():
    x, y = np.array([12.2, 25.1]), np.array([14., 25.8])
    old = ExtractionOperator.build(x, y, 1.7, (40, 41))
    new = ExtractionOperator.build(x, y, 1.7, (40, 41), footprint_mode="auto")
    np.testing.assert_array_equal(new._row_pixel, old._row_pixel)
    np.testing.assert_array_equal(new.pixels_per_focus, old.pixels_per_focus)
    frame = _spots((40, 41), x, y, 1.7, [87., 110.], background=4.)
    np.testing.assert_allclose(new.apply_fast_device(frame, "CPU").amplitude,
                               old.apply_fast_device(frame, "CPU").amplitude)
    # The old Joint auto policy remains understood for historical records,
    # but new configurations declare only the shared footprint fields.
    assert "joint_footprint_mode" not in default_params()
    assert default_params()["footprint_mode"] == "from_lattice"


def test_fixed_radius_changes_fit_pixel_support_and_recovers_amplitude():
    shape = (56, 60)
    x, y = np.array([12., 31.]), np.array([13., 31.])
    small = ExtractionOperator.build(x, y, 1.6, shape, footprint_mode="fixed",
                                     fit_radius_px=2.6, min_pixels=1)
    large = ExtractionOperator.build(x, y, 1.6, shape, footprint_mode="fixed",
                                     fit_radius_px=5.0, min_pixels=1)
    assert large.num_fit_pixels > small.num_fit_pixels
    frame = _spots(shape, x, y, 1.6, [100., 140.], background=9.)
    for op in (small, large):
        output = op.apply_fast_device(frame, "CPU")
        np.testing.assert_allclose(output.amplitude[0, op.valid],
                                   np.array([100., 140.])[op.valid], rtol=2e-3)


@pytest.mark.parametrize("period,offset", [(10., 5.2), (11., 5.2), (10.3, 4.7)])
def test_joint_lattice_owns_one_pixel_once_including_even_period(period, offset):
    shape = (54, 59)
    lattice = Lattice.rectangular(period, period, offset, offset - .2)
    x, y = lattice.points_in_frame(*shape, margin=period)
    x, y = x + .15, y - .1  # Fitted centres need not coincide with DLL grid
    op = ExtractionOperator.build(x, y, 1.35, shape, footprint_mode="lattice",
                                  lattice=lattice, min_pixels=1)
    assert op.num_fit_pixels == len(op._row_pixel)
    np.testing.assert_array_equal(np.unique(op._row_pixel), op._row_pixel)
    # Footprints omit invalid border foci, but no fit pixel has >1 owner.
    assert np.all(np.bincount(op.footprints.row, minlength=op.num_fit_pixels) <= 1)
    if period == 10.:
        assert 100 in op.pixels_per_focus[op.valid]


@pytest.mark.parametrize("background", ["none", "constant", "constant+haze"])
def test_lattice_joint_support_preserves_background_models(background):
    shape = (58, 62)
    lattice = Lattice.rectangular(10., 10., 5.2, 4.7)
    x, y = lattice.points_in_frame(*shape, margin=12.)
    image = _spots(shape, x, y, 2., np.full(len(x), 75.), background=0. if background == "none" else 11.)
    op = ExtractionOperator.build(x, y, 2., shape, footprint_mode="lattice",
                                  lattice=lattice, background=background,
                                  haze_sigma=6. if background == "constant+haze" else None)
    assert np.isfinite(op.apply_fast_device(image, "CPU").amplitude[0, op.valid]).all()


def test_pipeline_routes_selected_support_and_reports_provenance():
    loc = PatternLocalization.from_lattice(Lattice.rectangular(10., 10., 5.2, 4.7))
    frame = np.zeros((58, 58), dtype=float)
    index = np.array([[0, 0], [1, 0], [0, 1], [1, 1]])
    for mode in ("auto", "fixed", "lattice"):
        geo = prepare_geometry(
            frame, np.eye(2), index,
            PipelineParams(estimator="joint", joint_footprint_mode=mode,
                           joint_fit_radius_px=4.5, spot_sigma_px=1.5,
                           localization_refinement="fast", reassignment="off"),
            localization=loc,
        )
        assert geo.diagnostics["joint_footprint_mode"] == mode
        assert geo.diagnostics["joint_fit_radius_px"] == (4.5 if mode == "fixed" else None)
        assert geo.diagnostics["joint_fit_pixel_count"] == geo.operator.num_fit_pixels


def test_joint_lattice_rejects_incompatible_patterns():
    x = np.array([8., 18.])
    y = np.array([8., 18.])
    with pytest.raises(ValueError, match="require a lattice"):
        ExtractionOperator.build(x, y, 1.3, (30, 30), footprint_mode="lattice")
    rotated = Lattice(a1=(10., .8), a2=(-.8, 10.), offset=(5., 5.))
    with pytest.raises(ValueError, match="axis-aligned"):
        ExtractionOperator.build(x, y, 1.3, (30, 30), footprint_mode="lattice", lattice=rotated)
    stagger = PatternLocalization.manual_grid(10., 10., 5., 5., stagger_axis="x", stagger=.3)
    with pytest.raises(ValueError, match="single-motif"):
        prepare_geometry(np.zeros((50, 50)), np.eye(2), np.array([[0, 0]]),
                         PipelineParams(estimator="joint", joint_footprint_mode="lattice",
                                        spot_sigma_px=1.4), localization=stagger)


@pytest.mark.parametrize("kwargs,match", [
    ({"footprint_mode": "bad"}, "footprint"),
    ({"footprint_mode": "fixed"}, "requires a fit radius"),
    ({"footprint_mode": "fixed", "fit_radius_px": -1}, "finite and > 0"),
])
def test_invalid_joint_support_fails_clearly(kwargs, match):
    with pytest.raises(ValueError, match=match):
        ExtractionOperator.build([10.], [10.], 1.5, (25, 25), **kwargs)
