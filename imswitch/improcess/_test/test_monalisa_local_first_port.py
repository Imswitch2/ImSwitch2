"""First-port coverage for Local Gaussian and conservative footprint suggestions."""

import numpy as np
import pytest

from imswitch.improcess.reconstructors.monalisa.calibration import PatternLocalization
from imswitch.improcess.reconstructors.monalisa.extraction import ExtractionOperator
from imswitch.improcess.reconstructors.monalisa.footprint_suggestion import (
    suggest_local_footprint_size,
)
from imswitch.improcess.reconstructors.monalisa.local_extraction import LocalExtractionOperator
from imswitch.improcess.reconstructors.monalisa.params import (
    ESTIMATOR_JOINT,
    ESTIMATOR_LOCAL,
    PRESET_FAST,
    PRESET_FULL,
    apply_preset,
    default_params,
)
from imswitch.improcess.reconstructors.monalisa.lattice import Lattice
from imswitch.improcess.reconstructors.monalisa.pipeline import PipelineParams, prepare_geometry


def test_normal_and_full_share_the_same_local_fit():
    normal = apply_preset(default_params(), PRESET_FAST)
    full = apply_preset(default_params(), PRESET_FULL)
    assert normal["estimator"] == ESTIMATOR_LOCAL
    assert normal["footprint_shape"] == "square"
    assert normal["local_sampling"] == "camera_pixels"
    assert full["estimator"] == ESTIMATOR_LOCAL


@pytest.mark.parametrize("period,expected", [(10.0, 9), (11.05, 11), (8.0, 7)])
def test_suggestion_square_period(period, expected):
    loc = PatternLocalization.manual_grid(period, period)
    assert suggest_local_footprint_size(loc, "square") == expected
    assert suggest_local_footprint_size(loc, "circle") == expected


def test_suggestion_considers_stagger_and_rotation():
    hexagon = PatternLocalization.manual_grid(12.0, 10.0, stagger_axis="x", stagger=0.5)
    assert suggest_local_footprint_size(hexagon, "square") == 9
    assert suggest_local_footprint_size(hexagon, "circle") == 11
    rotated = PatternLocalization.from_lattice(Lattice(
        a1=(10 / np.sqrt(2), 10 / np.sqrt(2)),
        a2=(-10 / np.sqrt(2), 10 / np.sqrt(2)),
    ))
    assert suggest_local_footprint_size(rotated, "square") == 7
    assert suggest_local_footprint_size(rotated, "circle") == 9


def test_local_native_square_and_circle_fit_isolated_gaussian():
    shape = (31, 31)
    yy, xx = np.mgrid[:shape[0], :shape[1]]
    x, y = np.array([15.2]), np.array([14.7])
    sigma = 1.7
    image = 140 * np.exp(-((xx-x[0])**2+(yy-y[0])**2)/(2*sigma**2)) + 12
    for footprint in ("rectangular", "circular"):
        op = LocalExtractionOperator.build(
            x, y, sigma, shape, background="constant", footprint_shape=footprint,
            footprint_size=9, pinhole_radius_px=4.5, sampling="camera_pixels",
        )
        out = op.apply_device(image, "CPU")
        np.testing.assert_allclose(out.amplitude[0, 0], 140, rtol=1e-11)
        np.testing.assert_allclose(out.background[0, 0], 12, rtol=1e-11)
        assert op.valid.tolist() == [True]


def test_local_supports_edge_clipping_without_out_of_frame_reads():
    shape = (21, 21)
    op = LocalExtractionOperator.build(
        [0.25, 10.0], [0.1, 10.0], 1.5, shape,
        footprint_shape="rectangular", footprint_size=7,
        sampling="camera_pixels", background="constant",
    )
    assert op.pixels_per_focus[0] < op.pixels_per_focus[1]
    assert np.isfinite(op.apply_device(np.ones(shape), "CPU").amplitude[0, 1])


def test_pipeline_dispatch_respects_explicit_method_and_footprint():
    loc = PatternLocalization.manual_grid(11, 11, 4, 4)
    params = PipelineParams(
        estimator="local", local_footprint_shape="square",
        local_footprint_size=9, local_sampling="camera_pixels",
        spot_sigma_px=1.8, localization_refinement="fast", reassignment="off",
        frame_gain="off", cell_offsets="off", variance_weighting=False,
    )
    # Resolved calibration, no automatic spot recalibration required.
    frame = np.zeros((48, 48), dtype=float)
    geo = prepare_geometry(frame, np.eye(2), np.array([[0, 0]]), params, localization=loc)
    assert isinstance(geo.operator, LocalExtractionOperator)
    assert geo.diagnostics["estimator"] == "local"
    geo_joint = prepare_geometry(
        frame, np.eye(2), np.array([[0, 0]]),
        PipelineParams(spot_sigma_px=1.8, localization_refinement="fast"),
        localization=loc,
    )
    assert isinstance(geo_joint.operator, ExtractionOperator)


def test_local_reassignment_keeps_local_estimator():
    loc = PatternLocalization.manual_grid(11, 11, 4, 4)
    params = PipelineParams(
        estimator="local", spot_sigma_px=1.8, localization_refinement="fast",
        reassignment="auto", variance_weighting=False,
    )
    geometry = prepare_geometry(
        np.zeros((48, 48)), np.eye(2), np.array([[0, 0]]), params,
        localization=loc,
    )
    assert isinstance(geometry.operator, LocalExtractionOperator)
    assert geometry.operator.footprints.focus.size > 0
