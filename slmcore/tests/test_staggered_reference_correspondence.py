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


def test_signed_stagger_reference_uses_cell_phase_not_rectangular_permutation():
    indices=_indices()
    source_lattice=LatticeRepresentation.alternating_rows(indices,+0.5)
    target_lattice=LatticeRepresentation.alternating_rows(indices,-0.5)
    source=ReferenceGeometry(
        positions_px=_detector_positions(source_lattice),
        site_ids=_site_ids(indices),lattice=source_lattice,
    )
    target=_target(indices,target_lattice)

    # Reference correspondence exposes shape-compatible orientations even though
    # they are not self-symmetries of one fixed stagger motif.
    assert exact_lattice_correspondence_options(target)==(
        "auto","canonical","flip_x","flip_y","rotate_180",
    )

    resolved=resolve_exact_lattice_correspondence(source,target,"canonical")
    expected=np.array([
        0,1,2,3,4,
        -1,5,6,7,8,
        10,11,12,13,14,
        -1,15,16,17,18,
    ],dtype=np.int64)
    np.testing.assert_array_equal(resolved.source_indices,expected)
    assert resolved.valid_count==18
    assert resolved.correspondence["topology_aware"] is True
    assert resolved.correspondence["logical_translation"]==(0.0,0.0)

    # The smooth correction still evaluates at every target site, including the
    # two finite-edge sites that have no direct source counterpart.
    aligned=align_resolved_position_reference(target,resolved,model="translation")
    np.testing.assert_allclose(
        aligned.correction_px,
        np.repeat(np.array([[-0.7],[0.4]]),indices.shape[1],axis=1),
        atol=1e-10,
    )


def test_stagger_auto_prefers_correct_topology_phase():
    indices=_indices()
    source_lattice=LatticeRepresentation.alternating_rows(indices,+0.5)
    target_lattice=LatticeRepresentation.alternating_rows(indices,-0.5)
    source=ReferenceGeometry(
        positions_px=_detector_positions(source_lattice),
        site_ids=_site_ids(indices),lattice=source_lattice,
    )
    target=_target(indices,target_lattice)

    resolved=resolve_exact_lattice_correspondence(source,target,"auto")
    assert resolved.correspondence["resolved"]=="canonical"
    assert resolved.correspondence["fit_count"]==18
    assert abs(resolved.correspondence["relative_rotation_deg"])<1e-6
    assert resolved.correspondence["rms_px"]<1e-9


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


def test_detector_frame_normalization_makes_aligned_signed_stagger_canonical():
    indices=_indices(nx=5,ny=5)
    source_lattice=LatticeRepresentation.alternating_rows(indices,+0.5)
    target_lattice=LatticeRepresentation.alternating_rows(indices,-0.5)
    linear=np.array([[20.0,3.0],[-2.0,18.0]])

    def centered(lattice):
        logical=np.asarray(lattice.logical_positions,dtype=np.float64)
        return logical-np.mean(logical,axis=1,keepdims=True)

    # Reproduce the real failure mode: localization numbered the source in the
    # camera-canonical frame but the target in an equivalent 180-degree frame.
    # The physical finite lattices still coincide after the signed-stagger phase.
    source_positions=linear@centered(source_lattice)+np.array([[100.0],[80.0]])
    target_positions=linear@(-centered(target_lattice))+np.array([[100.7],[79.6]])
    source=ReferenceGeometry(
        positions_px=source_positions,site_ids=_site_ids(indices),lattice=source_lattice,
    )
    target=PositionReferenceTarget(
        registered_positions_px=target_positions,measured_positions_px=target_positions,
        matched_mask=np.ones(indices.shape[1],dtype=bool),site_ids=_site_ids(indices),
        lattice_indices=indices,lattice=target_lattice,
    )

    resolved=resolve_exact_lattice_correspondence(source,target,"canonical")
    automatic=resolve_exact_lattice_correspondence(source,target,"auto")

    assert resolved.valid_count==indices.shape[1]
    assert resolved.correspondence["source_detector_frame"]=="canonical"
    assert resolved.correspondence["target_detector_frame"]=="rotate_180"
    assert automatic.correspondence["resolved"]=="canonical"
    np.testing.assert_array_equal(automatic.source_indices,resolved.source_indices)
    displacement=np.linalg.norm(
        resolved.desired_positions_px-target.measured_positions_px,axis=0,
    )
    assert float(np.median(displacement))<1.0


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
    assert float(np.max(displacement))<1.0
    np.testing.assert_allclose(displacement,np.hypot(0.7,0.4),atol=1e-10)
