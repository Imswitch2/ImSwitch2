from __future__ import annotations

import numpy as np

from slmcore import PositionReference
from slmcore.core.cgh.lattice_geometry import LatticeRepresentation
from slmcore.core.measurement.position_alignment import align_resolved_position_reference
from slmcore.core.measurement.position_reference import (
    PositionReferenceTarget,ReferenceGeometry,exact_lattice_correspondence_options,
    reference_geometry_from_position_reference,resolve_exact_lattice_correspondence,
)


def _indices(nx=5,ny=4):
    x,y=np.meshgrid(np.arange(nx),np.arange(ny),indexing="xy")
    return np.vstack([x.ravel(),y.ravel()]).astype(np.int64)


def _site_ids(indices):
    return tuple((int(x),int(y)) for x,y in np.asarray(indices).T)


def _detector_positions(lattice,shift=(0.0,0.0)):
    linear=np.array([[20.0,3.0],[-2.0,18.0]])
    offset=np.array([[100.0],[80.0]])+np.asarray(shift,dtype=np.float64).reshape(2,1)
    return linear@lattice.logical_positions+offset


def _target(indices,lattice,shift=(0.7,-0.4)):
    measured=_detector_positions(lattice,shift=shift)
    return PositionReferenceTarget(
        registered_positions_px=measured,
        measured_positions_px=measured,
        matched_mask=np.ones(indices.shape[1],dtype=bool),
        site_ids=_site_ids(indices),
        lattice_indices=indices,
        lattice=lattice,
    )


def test_exact_reference_uses_target_site_identity_for_signed_stagger():
    indices=_indices()
    source_lattice=LatticeRepresentation.alternating_rows(indices,+0.5)
    target_lattice=LatticeRepresentation.alternating_rows(indices,-0.5)
    # Exact-reference correspondence operates after both sides have been put in
    # target-site order. The motif difference is provenance/topology, not another
    # request to phase-shift or wrap finite site identities.
    base=_detector_positions(source_lattice)
    shift=np.array([[0.7],[-0.4]])
    source=ReferenceGeometry(
        positions_px=base,site_ids=_site_ids(indices),lattice=source_lattice,
    )
    target=PositionReferenceTarget(
        registered_positions_px=base+shift,measured_positions_px=base+shift,
        matched_mask=np.ones(indices.shape[1],dtype=bool),
        site_ids=_site_ids(indices),lattice_indices=indices,lattice=target_lattice,
    )

    assert exact_lattice_correspondence_options(target)==(
        "auto","canonical","flip_x","flip_y","rotate_180",
    )
    resolved=resolve_exact_lattice_correspondence(source,target,"canonical")
    np.testing.assert_array_equal(
        resolved.source_indices,np.arange(indices.shape[1],dtype=np.int64),
    )
    assert resolved.valid_count==indices.shape[1]
    assert resolved.correspondence["topology_aware"] is False

    aligned=align_resolved_position_reference(target,resolved,model="translation")
    np.testing.assert_allclose(
        aligned.correction_px,
        np.repeat(np.array([[-0.7],[0.4]]),indices.shape[1],axis=1),
        atol=1e-10,
    )


def test_stagger_auto_prefers_smallest_actual_motion():
    indices=_indices()
    source_lattice=LatticeRepresentation.alternating_rows(indices,+0.5)
    target_lattice=LatticeRepresentation.alternating_rows(indices,-0.5)
    base=_detector_positions(source_lattice)
    source=ReferenceGeometry(
        positions_px=base,site_ids=_site_ids(indices),lattice=source_lattice,
    )
    target=PositionReferenceTarget(
        registered_positions_px=base+np.array([[0.7],[-0.4]]),
        measured_positions_px=base+np.array([[0.7],[-0.4]]),
        matched_mask=np.ones(indices.shape[1],dtype=bool),
        site_ids=_site_ids(indices),lattice_indices=indices,lattice=target_lattice,
    )

    resolved=resolve_exact_lattice_correspondence(source,target,"auto")
    assert resolved.correspondence["resolved"]=="canonical"
    assert resolved.correspondence["fit_count"]==indices.shape[1]
    assert resolved.correspondence["median_displacement_px"]<1.0


def test_saved_reference_restores_persisted_lattice_topology():
    indices=_indices()
    lattice=LatticeRepresentation.alternating_rows(indices,-0.75)
    reference=PositionReference(
        name="staggered",observation_plane_name="sample",
        lattice_indices=indices,positions_px=_detector_positions(lattice),
        image_shape=(128,128),detector_name="cam",
        metadata={"lattice_representation":lattice.to_dict()},
    )

    restored=PositionReference.from_dict(reference.to_dict())
    geometry=reference_geometry_from_position_reference(restored)
    assert geometry.lattice is not None
    np.testing.assert_allclose(geometry.lattice.logical_positions,lattice.logical_positions)
    assert geometry.lattice.diagnostics["stagger"]==-0.75
    assert geometry.lattice.diagnostics["odd_basis_cell_shift_x"]==-1


def test_exact_reference_does_not_reinfer_detector_frame_from_stagger_geometry():
    indices=_indices(nx=5,ny=5)
    source_lattice=LatticeRepresentation.alternating_rows(indices,+0.5)
    target_lattice=LatticeRepresentation.alternating_rows(indices,-0.5)
    base=_detector_positions(source_lattice)
    shift=np.array([[0.7],[-0.4]])
    source=ReferenceGeometry(
        positions_px=base,site_ids=_site_ids(indices),lattice=source_lattice,
    )
    target=PositionReferenceTarget(
        registered_positions_px=base+shift,measured_positions_px=base+shift,
        matched_mask=np.ones(indices.shape[1],dtype=bool),site_ids=_site_ids(indices),
        lattice_indices=indices,lattice=target_lattice,
    )

    resolved=resolve_exact_lattice_correspondence(source,target,"canonical")
    automatic=resolve_exact_lattice_correspondence(source,target,"auto")

    assert resolved.valid_count==indices.shape[1]
    np.testing.assert_array_equal(
        resolved.source_indices,np.arange(indices.shape[1],dtype=np.int64),
    )
    assert automatic.correspondence["resolved"]=="canonical"
    np.testing.assert_array_equal(automatic.source_indices,resolved.source_indices)
    assert resolved.correspondence["source_detector_frame"]=="target_indexed"


def test_staggered_reference_feedback_flip_keeps_edge_sites_local():
    from slmcore.core.cgh.feedback import FeedbackOrientation,orient_localization,orientation_permutation
    from slmcore.core.cgh.localization import LocalizationResult
    from slmcore.core.measurement.position_reference import (
        position_reference_target_from_localization,reference_geometry_from_localization,
    )

    indices=_indices(nx=5,ny=5)
    source_lattice=LatticeRepresentation.alternating_rows(indices,+0.5)
    target_lattice=LatticeRepresentation.alternating_rows(indices,-0.5)
    source_positions=_detector_positions(source_lattice)
    permutation=orientation_permutation(indices,FeedbackOrientation.FLIP_HORIZONTAL)
    inverse=np.empty_like(permutation); inverse[permutation]=np.arange(permutation.size)
    shift=np.array([[0.7],[-0.4]])
    target_raw_positions=(source_positions+shift)[:,inverse]
    matched=tuple(True for _ in range(indices.shape[1]))

    source_localization=LocalizationResult(
        target_type="multi_foci_vector",target_params={"stagger":+0.5},parameters={},
        lattice_indices=indices,crop_coord=(0,256,0,256),cropped_image=np.zeros((256,256)),
        expected_positions_px=source_positions,measured_positions_px=source_positions,
        period_x_px=20.0,period_y_px=18.0,offset_x_px=100.0,offset_y_px=80.0,
        diagnostics={"matched_mask":matched,"resolved_stagger":+0.5,
                     "lattice_representation":source_lattice.to_dict()},
    )
    target_localization=LocalizationResult(
        target_type="multi_foci_vector",target_params={"stagger":-0.5},parameters={},
        lattice_indices=indices,crop_coord=(0,256,0,256),cropped_image=np.zeros((256,256)),
        expected_positions_px=target_raw_positions,measured_positions_px=target_raw_positions,
        period_x_px=20.0,period_y_px=18.0,offset_x_px=100.0,offset_y_px=80.0,
        diagnostics={"matched_mask":matched,"resolved_stagger":-0.5,
                     "lattice_representation":target_lattice.to_dict()},
    )
    target_localization=orient_localization(
        target_localization,FeedbackOrientation.FLIP_HORIZONTAL,
    )
    source=reference_geometry_from_localization(source_localization,full_image=False)
    target=position_reference_target_from_localization(target_localization,full_image=False)
    resolved=resolve_exact_lattice_correspondence(source,target,"canonical")
    displacement=np.linalg.norm(
        resolved.desired_positions_px-target.measured_positions_px,axis=0,
    )

    assert resolved.valid_count==indices.shape[1]
    assert resolved.correspondence["resolved"]=="canonical"
    assert float(np.max(displacement))<1.0
    np.testing.assert_allclose(displacement,np.hypot(0.7,0.4),atol=1e-10)
