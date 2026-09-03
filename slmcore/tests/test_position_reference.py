from __future__ import annotations

import numpy as np

from slmcore import ImageMeasurement,PositionReference
from slmcore.core.cgh.feedback import (
    EditablePositionReferenceGeometry,
    fit_center_reference_geometry,
    editable_position_reference_geometry,
    editable_reference_positions,
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
    design = np.column_stack([base.T,np.ones(base.shape[1])])
    coefficients,_,_,_ = np.linalg.lstsq(design,measured.T,rcond=None)
    expected = (design@coefficients).T
    affine_linear = coefficients[:2,:].T
    affine_translation = coefficients[2,:]
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
        diagnostics={
            "matched_mask":tuple(True for _ in range(25)),
            "affine_linear":tuple(tuple(float(v) for v in row) for row in affine_linear),
            "affine_translation":tuple(float(v) for v in affine_translation),
        },
    )


def test_fit_center_geometry_favors_central_spots_over_global_fit():
    localization = _localization()
    global_reference = np.asarray(localization.expected_positions_px)
    measured = np.asarray(localization.measured_positions_px)
    geometry = fit_center_reference_geometry(localization)
    fitted = editable_reference_positions(localization,geometry)

    # Fit center is deliberately the previous maximum-emphasis behavior, now
    # exposed as one discrete editable-lattice operation.
    center = np.mean(global_reference,axis=1,keepdims=True)
    radius = np.linalg.norm(global_reference-center,axis=0)
    weights = np.exp(-4.0*np.square(radius/np.max(radius)))
    design = np.column_stack([
        global_reference.T,np.ones(global_reference.shape[1]),
    ])
    root_weights = np.sqrt(weights)[:,None]
    coefficients,_,_,_ = np.linalg.lstsq(
        design*root_weights,measured.T*root_weights,rcond=None,
    )
    previous_max_emphasis = (design@coefficients).T
    np.testing.assert_allclose(fitted,previous_max_emphasis,atol=1e-10)

    center = np.mean(global_reference,axis=1,keepdims=True)
    radius = np.linalg.norm(global_reference-center,axis=0)
    central = radius <= 10.1
    outer = radius >= np.max(radius)-1e-6

    global_center_error = np.mean(
        np.linalg.norm(global_reference[:,central]-measured[:,central],axis=0)
    )
    fitted_center_error = np.mean(
        np.linalg.norm(fitted[:,central]-measured[:,central],axis=0)
    )
    global_outer_error = np.mean(
        np.linalg.norm(global_reference[:,outer]-measured[:,outer],axis=0)
    )
    fitted_outer_error = np.mean(
        np.linalg.norm(fitted[:,outer]-measured[:,outer],axis=0)
    )

    assert fitted_center_error < global_center_error
    assert fitted_outer_error > global_outer_error


def test_fit_center_respects_rotation_and_lattice_angle_locks():
    localization = _localization()
    base = editable_position_reference_geometry(localization)
    constrained = EditablePositionReferenceGeometry(
        period_x_px=base.period_x_px,
        period_y_px=base.period_y_px,
        rotation_deg=30.0,
        lattice_angle_deg=90.0,
        offset_x_px=base.offset_x_px,
        offset_y_px=base.offset_y_px,
        handedness=base.handedness,
    )

    fitted = fit_center_reference_geometry(
        localization,
        geometry=constrained,
        locks={"rotation_deg":True,"lattice_angle_deg":True},
    )

    assert fitted.rotation_deg == 30.0
    assert fitted.lattice_angle_deg == 90.0
    # Translation remains free so Fit center can still move the constrained grid.
    assert abs(fitted.offset_x_px)+abs(fitted.offset_y_px) > 0.0


def test_fit_center_respects_period_lock_and_refits_unlocked_geometry():
    localization = _localization()
    base = editable_position_reference_geometry(localization)
    constrained = EditablePositionReferenceGeometry(
        period_x_px=1.25*base.period_x_px,
        period_y_px=base.period_y_px,
        rotation_deg=base.rotation_deg,
        lattice_angle_deg=base.lattice_angle_deg,
        handedness=base.handedness,
    )

    fitted = fit_center_reference_geometry(
        localization,geometry=constrained,locks={"period_x_px":True},
    )

    assert fitted.period_x_px == constrained.period_x_px
    assert np.isfinite(fitted.period_y_px)
    assert np.isfinite(fitted.rotation_deg)
    assert np.isfinite(fitted.lattice_angle_deg)


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


def test_editable_reference_reconstructs_fit_and_changes_geometry_about_same_center():
    localization = _localization(crop=(0,80,0,80))
    fit = editable_position_reference_geometry(localization)
    reconstructed = editable_reference_positions(localization,fit)
    np.testing.assert_allclose(
        reconstructed,localization.expected_positions_px,atol=1e-10,
    )

    edited = EditablePositionReferenceGeometry(
        period_x_px=fit.period_x_px,
        period_y_px=fit.period_y_px,
        rotation_deg=0.0,
        lattice_angle_deg=90.0,
        offset_x_px=2.5,
        offset_y_px=-1.5,
        handedness=fit.handedness,
    )
    positions = editable_reference_positions(localization,edited)
    assert positions.shape == localization.expected_positions_px.shape
    assert np.all(np.isfinite(positions))
    # Translation changes only by the explicit detector-space offset; the
    # logical lattice itself remains indexed point-for-point.
    diagnostics = dict(localization.diagnostics)
    fit_center = np.asarray(diagnostics["affine_translation"],dtype=np.float64)
    linear = np.asarray(diagnostics["affine_linear"],dtype=np.float64)
    logical = np.linalg.solve(
        linear,
        np.asarray(localization.expected_positions_px)-fit_center[:,None],
    )
    assert logical.shape == positions.shape
    np.testing.assert_allclose(
        np.mean(positions-edited.offset_x_px*np.array([[1.0],[0.0]])
                       -edited.offset_y_px*np.array([[0.0],[1.0]]),axis=1),
        np.mean(editable_reference_positions(
            localization,
            EditablePositionReferenceGeometry(
                period_x_px=fit.period_x_px,period_y_px=fit.period_y_px,
                rotation_deg=0.0,lattice_angle_deg=90.0,
                handedness=fit.handedness,
            ),
        ),axis=1),
        atol=1e-10,
    )


def test_editable_reference_preserves_nonrectangular_logical_structure():
    logical = np.asarray([
        [-1.0,0.0,1.0,-0.5,0.5,1.5],
        [-0.5,-0.5,-0.5,0.5,0.5,0.5],
    ])
    linear = np.asarray([[11.0,2.5],[1.5,9.0]])
    translation = np.asarray([42.0,37.0])
    expected = linear@logical+translation[:,None]
    localization = LocalizationResult(
        target_type="test",target_params={},parameters={},
        lattice_indices=np.asarray([[0,1,2,0,1,2],[0,0,0,1,1,1]]),
        crop_coord=(0,100,0,100),cropped_image=np.zeros((100,100)),
        expected_positions_px=expected,measured_positions_px=expected,
        period_x_px=float(np.linalg.norm(linear[:,0])),
        period_y_px=float(np.linalg.norm(linear[:,1])),
        offset_x_px=translation[0],offset_y_px=translation[1],
        diagnostics={
            "affine_linear":tuple(tuple(v for v in row) for row in linear),
            "affine_translation":tuple(translation),
            "matched_mask":tuple(True for _ in range(expected.shape[1])),
        },
    )
    fit = editable_position_reference_geometry(localization)
    edited = EditablePositionReferenceGeometry(
        period_x_px=12.0,period_y_px=10.0,rotation_deg=5.0,
        lattice_angle_deg=90.0,handedness=fit.handedness,
    )
    positions = editable_reference_positions(localization,edited)
    theta = np.radians(5.0)
    phi = theta+fit.handedness*np.radians(90.0)
    edited_linear = np.column_stack((
        12.0*np.array([np.cos(theta),np.sin(theta)]),
        10.0*np.array([np.cos(phi),np.sin(phi)]),
    ))
    np.testing.assert_allclose(
        positions,edited_linear@logical+translation[:,None],atol=1e-10,
    )
