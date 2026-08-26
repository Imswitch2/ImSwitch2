import numpy as np
from scipy.ndimage import gaussian_filter

from slmcore.calibration import SLMSectionCalibration
from slmcore.cgh.localization import (
    LOCALIZATION_PARAMS,
    localization_context,
    resolve_localization_guidance,
)
from slmcore.cgh.localization import (
    LatticeRegistrationOptions,
    SpotDetectionOptions,
    localize_lattice,
    make_lattice_model,
)
from slmcore.cgh.signature import CGHSignature
from slmcore.cgh.targets.resolution import TargetResolution
from slmcore.cgh.pattern_geometry import LatticeTargetGeometry


def _defaults():
    return {key:spec.default for key,spec in LOCALIZATION_PARAMS.items()}


def _resolution(indices, *, period_x=6.0, period_y=8.0, stagger=0.5):
    n = indices.shape[1]
    zeros = np.zeros((2,n),dtype=np.float64)
    params = {
        "period_x_px":period_x,
        "period_y_px":period_y,
        "stagger":stagger,
    }
    return TargetResolution(
        section_shape=(64,64),
        target_signature=CGHSignature("test-target"),
        canonical_params=params,
        effective_params=params,
        adjustments=(),
        lattice_indices=indices,
        ideal_spot_positions_kxy=zeros,
        spot_positions_kxy=zeros,
        spot_intensities=np.ones(n,dtype=np.float64),
        preview=np.ones((2,2),dtype=np.float64),
        geometry=LatticeTargetGeometry(
            period_x_reference_px=period_x,
            period_y_reference_px=period_y,
            stagger=stagger,
            count_x=int(np.unique(indices[0]).size),
            count_y=int(np.unique(indices[1]).size),
        ),
    )


def test_hint_resolution_target_manual_none_and_safe_period_only():
    indices = np.array([[0,1,0,1],[0,0,1,1]],dtype=np.int64)
    resolution = _resolution(indices)
    calibration = SLMSectionCalibration(
        kx_per_um=0.01,
        ky_per_um=0.02,
        cam_px_size_um=0.1,
    )

    params = _defaults()
    hints = resolve_localization_guidance(
        target_params={
            "stagger":0.5,
            "rotation_deg":37.0,
            "skew_deg":19.0,
        },
        resolution=resolution,
        calibration=calibration,
        parameters=params,
    )
    assert hints.stagger == 0.5
    assert hints.stagger_source == "target"
    assert np.allclose(hints.expected_period_px,(11.71875,7.8125))
    assert hints.period_source == "target_calibration"
    assert (hints.count_x,hints.count_y) == (2,2)
    assert hints.count_source == "target"

    context = localization_context(
        target_type="test",
        target_params={
            "stagger":0.5,
            "rotation_deg":37.0,
            "skew_deg":19.0,
        },
        resolution=resolution,
        calibration=calibration,
    )
    assert context["target_geometry_type"] == "lattice"
    assert context["target_lattice_count"] == (2,2)
    assert context["target_stagger"] == 0.5
    assert np.allclose(context["target_expected_period_px"],(11.71875,7.8125))
    assert "target_expected_rotation_deg" not in context

    manual = dict(params)
    manual.update({
        "stagger_prior_mode":"manual",
        "manual_stagger":0.25,
        "period_prior_mode":"manual",
        "expected_period_x_px":10.0,
        "expected_period_y_px":12.0,
        "lattice_size_prior_mode":"manual",
        "manual_lattice_count_x":3,
        "manual_lattice_count_y":4,
    })
    hints = resolve_localization_guidance(
        target_params={"stagger":0.5},
        resolution=resolution,
        calibration=calibration,
        parameters=manual,
    )
    assert hints.stagger == 0.25
    assert hints.stagger_source == "manual"
    assert hints.expected_period_px == (10.0,12.0)
    assert hints.period_source == "manual"
    assert (hints.count_x,hints.count_y) == (3,4)
    assert hints.count_source == "manual"

    none = dict(params)
    none.update({
        "stagger_prior_mode":"none",
        "period_prior_mode":"none",
        "lattice_size_prior_mode":"none",
    })
    hints = resolve_localization_guidance(
        target_params={"stagger":0.5},
        resolution=resolution,
        calibration=calibration,
        parameters=none,
    )
    assert hints.stagger is None
    assert hints.stagger_source == "auto"
    assert hints.expected_period_px is None
    assert hints.period_source == "auto"
    assert hints.count_x is None and hints.count_y is None
    assert hints.count_source == "auto"


def test_wrong_period_guidance_falls_back_to_global_fast():
    n = 13
    i,j = np.meshgrid(np.arange(n),np.arange(n),indexing="xy")
    indices = np.vstack([i.ravel(),j.ravel()])
    model = make_lattice_model(indices,stagger=0.0)
    linear = np.array([[9.5,0.7],[0.4,10.2]],dtype=np.float64)
    points = linear.dot(model.logical_positions) + np.array([[90.0],[90.0]])

    image = np.zeros((180,180),dtype=np.float64)
    for x,y in points.T:
        image[int(round(y)),int(round(x))] += 1.0
    image = gaussian_filter(image,0.9)

    result = localize_lattice(
        image,
        indices,
        stagger=0.0,
        detection_options=SpotDetectionOptions(
            crop_threshold=0.05,
            threshold_rel=0.05,
            refinement_window_px=2,
        ),
        registration_options=LatticeRegistrationOptions(
            expected_period_px=(5.0,5.0),
            period_tolerance_fraction=0.10,
            lattice_candidate_search_mode="fast",
        ),
    )

    assert result.matched_count == indices.shape[1]
    assert result.diagnostics["search_path"] in ("global_fast","global_full")
    assert result.diagnostics["fallback_used"] is True


def test_known_stagger_and_period_use_guided_path():
    n = 13
    i,j = np.meshgrid(np.arange(n),np.arange(n),indexing="xy")
    indices = np.vstack([i.ravel(),j.ravel()])
    model = make_lattice_model(indices,stagger=0.5)
    linear = np.array([[9.0,1.2],[0.6,8.4]],dtype=np.float64)
    points = linear.dot(model.logical_positions) + np.array([[90.0],[90.0]])

    image = np.zeros((180,180),dtype=np.float64)
    for x,y in points.T:
        image[int(round(y)),int(round(x))] += 1.0
    image = gaussian_filter(image,0.9)

    expected = tuple(float(v) for v in np.linalg.norm(linear,axis=0))
    result = localize_lattice(
        image,
        indices,
        stagger=0.5,
        detection_options=SpotDetectionOptions(
            crop_threshold=0.05,
            threshold_rel=0.05,
            refinement_window_px=2,
        ),
        registration_options=LatticeRegistrationOptions(
            expected_period_px=expected,
            period_tolerance_fraction=0.20,
            lattice_candidate_search_mode="fast",
        ),
    )

    assert result.matched_count == indices.shape[1]
    assert result.diagnostics["search_path"] == "period_guided"
    assert result.diagnostics["fallback_used"] is False
