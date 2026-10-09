import numpy as np

from slmcore.core.cgh.feedback import (
    ReferenceCorrespondence,resolve_reference_correspondence,
)
from slmcore.core.measurement.position_reference import (
    position_reference_target_from_localization,reference_geometry_from_localization,
    resolve_exact_lattice_correspondence,
)
from slmcore.core.cgh.localization import (
    DetectedSpots,LatticeRegistration,LocalizationResult,SpotOrdering,
    available_spot_orderings,canonicalize_registration,make_lattice_model,
    rectangular_lattice_indices,reorder_localization,spot_ordering_permutation,
)


def _registration(nx=5,ny=4,linear=None,translation=(80.0,60.0),stagger=0.0):
    indices = rectangular_lattice_indices(nx,ny)
    model = make_lattice_model(indices,stagger=stagger)
    if linear is None:
        linear = np.array([[10.0,0.0],[0.0,12.0]])
    linear = np.asarray(linear,dtype=np.float64)
    translation = np.asarray(translation,dtype=np.float64)
    expected = linear@model.logical_positions+translation[:,None]
    measured = np.array(expected,copy=True)
    detections = DetectedSpots(
        positions_px=measured,
        intensities=np.ones(indices.shape[1]),
        scores=np.ones(indices.shape[1]),
        cropped_image=np.zeros((160,180),dtype=np.float64),
        processed_image=np.zeros((160,180),dtype=np.float64),
        crop_coord=(0,160,0,180),
    )
    count = indices.shape[1]
    return LatticeRegistration(
        model=model,
        detections=detections,
        affine_linear=linear,
        affine_translation=translation,
        expected_positions_px=expected,
        measured_positions_px=measured,
        matched_mask=np.ones(count,dtype=bool),
        detection_indices=np.arange(count,dtype=np.int64),
        residuals_px=np.zeros_like(expected),
        rms_residual_px=0.0,
    )


def _localization(registration):
    diagnostics = dict(registration.diagnostics or {})
    diagnostics.update({
        "matched_mask":tuple(bool(x) for x in registration.matched_mask),
        "inferred_mask":tuple(False for _ in registration.matched_mask),
        "detection_indices":tuple(int(x) for x in registration.detection_indices),
        "residuals_px":tuple(tuple(float(x) for x in row) for row in registration.residuals_px),
        "affine_linear":tuple(tuple(float(x) for x in row) for row in registration.affine_linear),
        "affine_translation":tuple(float(x) for x in registration.affine_translation),
        "rms_residual_px":float(registration.rms_residual_px),
    })
    return LocalizationResult(
        target_type="multi_foci_vector",
        target_params={},
        parameters={},
        lattice_indices=registration.model.lattice_indices,
        crop_coord=registration.detections.crop_coord,
        cropped_image=registration.detections.cropped_image,
        expected_positions_px=registration.expected_positions_px,
        measured_positions_px=registration.measured_positions_px,
        period_x_px=registration.period_x_px,
        period_y_px=registration.period_y_px,
        offset_x_px=float(registration.affine_translation[0]),
        offset_y_px=float(registration.affine_translation[1]),
        diagnostics=diagnostics,
    )


def test_rectangular_grid_only_exposes_topology_preserving_orderings():
    registration = _registration(nx=5,ny=4)
    options = available_spot_orderings(
        registration.model.lattice_indices,
        registration.model.logical_positions,
    )
    assert options == (
        SpotOrdering.CANONICAL,
        SpotOrdering.FLIP_X,
        SpotOrdering.FLIP_Y,
        SpotOrdering.ROTATE_180,
    )
    assert SpotOrdering.TRANSPOSE not in options


def test_staggered_grid_rejects_symmetries_that_change_its_motif():
    registration = _registration(nx=5,ny=4,stagger=0.5)
    options = available_spot_orderings(
        registration.model.lattice_indices,
        registration.model.logical_positions,
    )
    assert options == (SpotOrdering.CANONICAL,SpotOrdering.ROTATE_180)


def test_canonicalization_only_reindexes_same_physical_fit():
    registration = _registration(
        linear=np.array([[-10.0,0.0],[0.0,-12.0]]),
    )
    original_points = {
        tuple(point) for point in registration.expected_positions_px.T
    }

    canonical = canonicalize_registration(registration)

    assert canonical.diagnostics["spot_ordering_mode"] == "auto"
    assert canonical.diagnostics["spot_ordering"] == "canonical"
    assert canonical.diagnostics["spot_ordering_registration_transform"] == "rotate_180"
    assert canonical.affine_linear[0,0] > 0.0
    assert canonical.affine_linear[1,1] > 0.0
    assert {
        tuple(point) for point in canonical.expected_positions_px.T
    } == original_points
    np.testing.assert_allclose(canonical.measured_positions_px,canonical.expected_positions_px)


def test_manual_ordering_is_reversible_without_reregistering():
    canonical_registration = canonicalize_registration(_registration())
    canonical = _localization(canonical_registration)

    flipped = reorder_localization(canonical,SpotOrdering.FLIP_X)
    assert flipped.diagnostics["spot_ordering_mode"] == "manual"
    assert flipped.diagnostics["spot_ordering"] == "flip_x"
    assert not np.array_equal(flipped.measured_positions_px,canonical.measured_positions_px)

    restored = reorder_localization(
        flipped,SpotOrdering.CANONICAL,automatic=True,
    )
    assert restored.diagnostics["spot_ordering_mode"] == "auto"
    assert restored.diagnostics["spot_ordering"] == "canonical"
    np.testing.assert_allclose(
        restored.measured_positions_px,canonical.measured_positions_px,
    )
    np.testing.assert_allclose(
        restored.expected_positions_px,canonical.expected_positions_px,
    )


def test_reference_auto_correspondence_handles_rotation_and_wrong_index_symmetry():
    base_registration = canonicalize_registration(_registration(nx=5,ny=4))
    base = _localization(base_registration)
    reference = np.asarray(base.measured_positions_px,dtype=np.float64)

    # Current 405 pattern is physically rotated by +5 degrees relative to the
    # saved 488 reference, while the saved columns arrive with an additional
    # Flip-X identity convention. Auto must recover the identity before position
    # error vectors are computed.
    theta = np.deg2rad(5.0)
    rotation = np.array([
        [np.cos(theta),-np.sin(theta)],
        [np.sin(theta), np.cos(theta)],
    ])
    center = np.mean(reference,axis=1,keepdims=True)
    current_positions = rotation@(reference-center)+center+np.array([[2.0],[-3.0]])
    current = LocalizationResult(
        target_type=base.target_type,
        target_params=base.target_params,
        parameters=base.parameters,
        lattice_indices=base.lattice_indices,
        crop_coord=base.crop_coord,
        cropped_image=base.cropped_image,
        expected_positions_px=current_positions,
        measured_positions_px=current_positions,
        period_x_px=base.period_x_px,
        period_y_px=base.period_y_px,
        offset_x_px=base.offset_x_px,
        offset_y_px=base.offset_y_px,
        diagnostics=base.diagnostics,
    )
    flip = spot_ordering_permutation(base.lattice_indices,SpotOrdering.FLIP_X)
    wrongly_ordered_reference = reference[:,flip]

    resolved,resolution = resolve_reference_correspondence(
        current,wrongly_ordered_reference,ReferenceCorrespondence.AUTO,
    )

    assert resolution.resolved is SpotOrdering.FLIP_X
    assert not resolution.reflection
    assert abs(abs(resolution.relative_rotation_deg)-5.0) < 1e-6
    np.testing.assert_allclose(resolved,reference,atol=1e-10)


def test_reference_correspondence_manual_override_is_respected():
    registration = canonicalize_registration(_registration(nx=5,ny=4))
    localization = _localization(registration)
    reference = np.asarray(localization.measured_positions_px,dtype=np.float64)

    resolved,resolution = resolve_reference_correspondence(
        localization,reference,ReferenceCorrespondence.ROTATE_180,
    )
    permutation = spot_ordering_permutation(
        localization.lattice_indices,SpotOrdering.ROTATE_180,
    )

    assert resolution.requested is ReferenceCorrespondence.ROTATE_180
    assert resolution.resolved is SpotOrdering.ROTATE_180
    np.testing.assert_allclose(resolved,reference[:,permutation])


def test_neutral_exact_lattice_auto_correspondence_matches_legacy_helper():
    base_registration=canonicalize_registration(_registration(nx=5,ny=4))
    base=_localization(base_registration)
    reference=np.asarray(base.measured_positions_px,dtype=np.float64)
    theta=np.deg2rad(5.0)
    rotation=np.array([[np.cos(theta),-np.sin(theta)],[np.sin(theta),np.cos(theta)]])
    center=np.mean(reference,axis=1,keepdims=True)
    current_positions=rotation@(reference-center)+center+np.array([[2.0],[-3.0]])
    current=LocalizationResult(
        target_type=base.target_type,target_params=base.target_params,parameters=base.parameters,
        lattice_indices=base.lattice_indices,crop_coord=base.crop_coord,
        cropped_image=base.cropped_image,expected_positions_px=current_positions,
        measured_positions_px=current_positions,period_x_px=base.period_x_px,
        period_y_px=base.period_y_px,offset_x_px=base.offset_x_px,offset_y_px=base.offset_y_px,
        diagnostics=base.diagnostics,
    )
    flip=spot_ordering_permutation(base.lattice_indices,SpotOrdering.FLIP_X)
    wrong=reference[:,flip]
    legacy,legacy_resolution=resolve_reference_correspondence(
        current,wrong,ReferenceCorrespondence.AUTO,
    )
    target=position_reference_target_from_localization(current,full_image=False)
    source=reference_geometry_from_localization(base,full_image=False)
    source=type(source)(
        positions_px=wrong,site_ids=target.site_ids,valid_mask=np.ones(wrong.shape[1],dtype=bool),
    )
    resolved=resolve_exact_lattice_correspondence(source,target,"auto")

    assert resolved.correspondence["resolved"]==legacy_resolution.resolved.value
    np.testing.assert_allclose(resolved.desired_positions_px,legacy,atol=1e-10)
