import numpy as np
from scipy.ndimage import gaussian_filter

from slmcore.core.calibration import SLMSectionCalibration
from slmcore.core.cgh.localization import (
    LOCALIZATION_PARAMS,
    localization_context,
    normalize_localization_parameter_input,
    resolve_localization_guidance,
)
from slmcore.core.cgh.localization import (
    DetectedSpots,
    LatticeRegistrationOptions,
    SpotDetectionOptions,
    localize_lattice,
    make_lattice_model,
    register_lattice,
)
from slmcore.core.cgh.signature import CGHSignature
from slmcore.core.cgh.targets.resolution import TargetResolution
from slmcore.core.cgh.pattern_geometry import LatticeTargetGeometry

def _defaults():
    return {key:spec.default for key,spec in LOCALIZATION_PARAMS.items()}

def test_legacy_crop_kernel_parameter_migrates_to_margin():
    values = normalize_localization_parameter_input({
        "crop_threshold":0.5,
        "dilation_kernel_size":5,
    })
    assert values["crop_threshold"] == 0.5
    assert values["crop_margin_px"] == 2
    assert "dilation_kernel_size" not in values

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
        pixel_size_um=0.1,
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
        pixel_size_um=0.1,
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


def test_signed_target_and_manual_stagger_are_preserved():
    indices = np.array([[0,1,0,1],[0,0,1,1]],dtype=np.int64)
    resolution = _resolution(indices,stagger=-0.25)
    params = _defaults()

    target = resolve_localization_guidance(
        target_params={"stagger":-0.25},
        resolution=resolution,
        calibration=None,
        parameters=params,
    )
    assert target.stagger == -0.25
    assert target.stagger_source == "target"

    manual = dict(params)
    manual.update({
        "stagger_prior_mode":"manual",
        "manual_stagger":-0.4,
    })
    hints = resolve_localization_guidance(
        target_params={"stagger":-0.25},
        resolution=resolution,
        calibration=None,
        parameters=manual,
    )
    assert hints.stagger == -0.4
    assert hints.stagger_source == "manual"

def test_finite_grid_registration_is_not_derailed_by_wrong_period_guidance():
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
    assert result.diagnostics["search_path"] == "finite_grid"
    assert result.diagnostics["fallback_used"] is False

def test_known_stagger_and_period_use_finite_grid_path():
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
    assert result.diagnostics["search_path"] == "finite_grid"
    assert result.diagnostics["fallback_used"] is False

def test_fft_registration_mode_forces_periodic_search_path():
    n = 11
    i,j = np.meshgrid(np.arange(n),np.arange(n),indexing="xy")
    indices = np.vstack([i.ravel(),j.ravel()])
    model = make_lattice_model(indices,stagger=0.0)
    linear = np.array([[9.4,0.5],[0.2,9.8]],dtype=np.float64)
    points = linear.dot(model.logical_positions) + np.array([[75.0],[75.0]])
    image = np.zeros((150,150),dtype=np.float64)
    for x,y in points.T:
        image[int(round(y)),int(round(x))] += 1.0
    image = gaussian_filter(image,0.9)

    result = localize_lattice(
        image,indices,stagger=0.0,
        detection_options=SpotDetectionOptions(
            crop_threshold=0.05,threshold_rel=0.05,refinement_window_px=2,
        ),
        registration_options=LatticeRegistrationOptions(
            registration_mode="fft_lattice",
            lattice_candidate_search_mode="fast",
        ),
    )
    assert result.diagnostics["search_path"].startswith("global_")

def test_finite_grid_mode_preserves_known_finite_topology():
    nx,ny = 5,4
    i,j = np.meshgrid(np.arange(nx),np.arange(ny),indexing="xy")
    indices = np.vstack([i.ravel(),j.ravel()])
    model = make_lattice_model(indices,stagger=0.0)
    linear = np.array([[47.4,0.05],[-0.18,47.2]],dtype=np.float64)
    points = linear.dot(model.logical_positions) + np.array([[110.0],[90.0]])
    image = np.zeros((190,220),dtype=np.float64)
    for x,y in points.T:
        image[int(round(y)),int(round(x))] += 1.0
    image = gaussian_filter(image,0.9)

    result = localize_lattice(
        image,indices,stagger=0.0,
        detection_options=SpotDetectionOptions(
            crop_threshold=0.05,threshold_rel=0.05,refinement_window_px=2,
        ),
        registration_options=LatticeRegistrationOptions(
            expected_period_px=(48.0,48.0),
            period_tolerance_fraction=0.0,
            registration_mode="finite_grid",
        ),
    )
    assert result.matched_count == nx*ny
    assert result.diagnostics["search_path"] == "finite_grid"
    assert np.allclose(
        np.linalg.norm(result.affine_linear,axis=0),(47.4,47.2),atol=0.8,
    )

def test_auto_keeps_partial_finite_grid_if_fft_fallback_is_weaker():
    nx,ny = 9,7
    i,j = np.meshgrid(np.arange(nx),np.arange(ny),indexing="xy")
    indices = np.vstack([i.ravel(),j.ravel()])
    model = make_lattice_model(indices,stagger=0.0)
    linear = np.array([[31.0,1.0],[0.2,29.0]],dtype=np.float64)
    points = linear.dot(model.logical_positions) + np.array([[170.0],[150.0]])

    # Leave two complete rows for topology initialization but remove enough
    # sites that Auto will also try the periodic fallback (<95% completeness).
    missing = np.array([0,10,20,30,40,50,51,52],dtype=np.int64)
    keep = np.ones(points.shape[1],dtype=bool)
    keep[missing] = False
    observed = points[:,keep]
    blank = np.zeros((340,340),dtype=np.float64)
    detections = DetectedSpots(
        positions_px=observed,
        intensities=np.ones(observed.shape[1]),
        scores=np.ones(observed.shape[1]),
        cropped_image=blank,
        processed_image=blank,
        crop_coord=(0,340,0,340),
    )

    result = register_lattice(
        detections,model,LatticeRegistrationOptions(registration_mode="auto"),
    )
    assert result.matched_count == observed.shape[1]
    assert result.diagnostics["search_path"] == "finite_grid"
    assert result.rms_residual_px < 1e-6
