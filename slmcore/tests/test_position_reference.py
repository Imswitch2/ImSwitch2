from __future__ import annotations

import numpy as np

from slmcore import ImageMeasurement,PositionReference
from slmcore.core.cgh.feedback import (
    center_weighted_reference_positions,
    localization_positions_full_px,
    reference_positions_for_localization,
)
from slmcore.core.cgh.feedback.analysis import analyze_position
from slmcore.core.cgh.feedback.model import FeedbackMeasurement
from slmcore.core.cgh.localization import LocalizationResult


def _affine_fit(source,target):
    design = np.column_stack([source.T,np.ones(source.shape[1])])
    coefficients,_,_,_ = np.linalg.lstsq(design,target.T,rcond=None)
    return (design@coefficients).T


def _localization(*,crop=(10,90,20,100)):
    xx,yy = np.meshgrid(
        np.arange(5,dtype=np.float64)*10.0 + 20.0,
        np.arange(5,dtype=np.float64)*10.0 + 20.0,
        indexing="xy",
    )
    base = np.vstack([xx.ravel(),yy.ravel()])
    center = np.mean(base,axis=1,keepdims=True)
    radius = np.linalg.norm(base-center,axis=0)
    normalized = radius/np.max(radius)

    # Deliberately non-affine distortion: an ordinary affine registration is
    # pulled toward the outer spots while the central spots remain closer to
    # the undistorted lattice.
    measured = np.array(base,copy=True)
    measured[0] += 6.0*np.square(normalized)
    measured[1] += 2.0*np.square(normalized)
    expected = _affine_fit(base,measured)
    lattice_indices = np.vstack([
        np.tile(np.arange(5,dtype=np.int64),5),
        np.repeat(np.arange(5,dtype=np.int64),5),
    ])
    return LocalizationResult(
        target_type="multi_foci_vector",
        target_params={},
        parameters={},
        lattice_indices=lattice_indices,
        crop_coord=crop,
        cropped_image=np.zeros((80,80),dtype=np.float64),
        expected_positions_px=expected,
        measured_positions_px=measured,
        period_x_px=10.0,
        period_y_px=10.0,
        offset_x_px=20.0,
        offset_y_px=20.0,
        diagnostics={"matched_mask":tuple(True for _ in range(25))},
    )


def test_center_weighted_reference_preserves_global_fit_at_zero_and_favors_center():
    localization = _localization()
    global_reference = np.asarray(localization.expected_positions_px)
    zero = center_weighted_reference_positions(localization,emphasis=0)
    weighted = center_weighted_reference_positions(localization,emphasis=100)

    np.testing.assert_allclose(zero,global_reference,atol=1e-10)

    center = np.mean(global_reference,axis=1,keepdims=True)
    radius = np.linalg.norm(global_reference-center,axis=0)
    central = radius <= 10.1
    outer = radius >= np.max(radius)-1e-6
    measured = np.asarray(localization.measured_positions_px)

    global_center_error = np.mean(
        np.linalg.norm(global_reference[:,central]-measured[:,central],axis=0)
    )
    weighted_center_error = np.mean(
        np.linalg.norm(weighted[:,central]-measured[:,central],axis=0)
    )
    global_outer_error = np.mean(
        np.linalg.norm(global_reference[:,outer]-measured[:,outer],axis=0)
    )
    weighted_outer_error = np.mean(
        np.linalg.norm(weighted[:,outer]-measured[:,outer],axis=0)
    )

    assert weighted_center_error < global_center_error
    assert weighted_outer_error > global_outer_error


def test_saved_reference_uses_full_detector_coordinates_and_resolves_new_crop():
    localization = _localization(crop=(10,90,20,100))
    full = localization_positions_full_px(localization)
    np.testing.assert_allclose(
        full,
        localization.measured_positions_px + np.array([[20.0],[10.0]]),
    )

    reference = PositionReference(
        name="alignment",
        plane_name="sample",
        lattice_indices=localization.lattice_indices,
        positions_px=full,
        image_shape=(120,140),
    )
    shifted_crop = LocalizationResult(
        target_type=localization.target_type,
        target_params=localization.target_params,
        parameters=localization.parameters,
        lattice_indices=localization.lattice_indices,
        crop_coord=(5,85,7,87),
        cropped_image=localization.cropped_image,
        expected_positions_px=localization.expected_positions_px,
        measured_positions_px=localization.measured_positions_px,
        period_x_px=localization.period_x_px,
        period_y_px=localization.period_y_px,
        offset_x_px=localization.offset_x_px,
        offset_y_px=localization.offset_y_px,
        diagnostics=localization.diagnostics,
    )
    resolved = reference_positions_for_localization(reference,shifted_crop)
    np.testing.assert_allclose(resolved,full-np.array([[7.0],[5.0]]))


def test_analyze_position_without_reference_keeps_legacy_result_exactly():
    localization = _localization(crop=(0,80,0,80))
    acquisition = ImageMeasurement(
        image=np.zeros((80,80),dtype=np.float64),source="test",
    )
    measurement = FeedbackMeasurement(
        acquisition=acquisition,localization=localization,
    )
    ideal = np.asarray(localization.lattice_indices,dtype=np.float64)*0.05

    legacy = analyze_position(
        measurement,ideal_positions_kxy=ideal,parameters={},
    )
    explicit = analyze_position(
        measurement,
        ideal_positions_kxy=ideal,
        reference_positions_px=localization.expected_positions_px,
        parameters={},
    )

    np.testing.assert_array_equal(
        legacy.position_errors_px,explicit.position_errors_px,
    )
    np.testing.assert_array_equal(legacy.correction_kxy,explicit.correction_kxy)
    np.testing.assert_array_equal(
        legacy.corrected_positions_kxy,explicit.corrected_positions_kxy,
    )


def test_saved_reference_span_does_not_rescale_pixel_to_kxy_mapping():
    localization = _localization(crop=(0,80,0,80))
    acquisition = ImageMeasurement(
        image=np.zeros((80,80),dtype=np.float64),source="test",
    )
    measurement = FeedbackMeasurement(
        acquisition=acquisition,localization=localization,
    )
    ideal = np.asarray(localization.lattice_indices,dtype=np.float64)*0.05

    registered = np.asarray(localization.expected_positions_px,dtype=np.float64)
    center = np.mean(registered,axis=1,keepdims=True)
    reference = center + 1.8*(registered-center)
    error_px = reference-np.asarray(localization.measured_positions_px)

    design = np.column_stack([
        registered.T,np.ones(registered.shape[1],dtype=np.float64),
    ])
    coefficients,_,_,_ = np.linalg.lstsq(design,ideal.T,rcond=None)
    current_linear_px_to_kxy = coefficients[:2,:].T

    analysis = analyze_position(
        measurement,
        ideal_positions_kxy=ideal,
        reference_positions_px=reference,
        parameters={},
    )

    np.testing.assert_allclose(
        analysis.correction_kxy,
        current_linear_px_to_kxy@error_px,
        atol=1e-12,
    )
