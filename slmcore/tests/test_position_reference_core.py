from __future__ import annotations

import numpy as np
import pytest

from slmcore import PositionReference
from slmcore.core.cgh.localization import LocalizationResult
from slmcore.core.measurement import (
    PositionReferenceTarget,ReferenceGeometry,
    align_resolved_position_reference,fit_position_alignment,
    position_reference_target_from_localization,
    reference_geometry_from_global_fit,reference_geometry_from_current_target,
    reference_geometry_from_localization,
    reference_geometry_from_position_reference,
    resolve_exact_lattice,
)


def _ids(count):
    return tuple((index,0) for index in range(count))


def test_exact_lattice_resolver_reorders_masks_weights_and_leaves_missing_sites_neutral():
    reference=ReferenceGeometry(
        positions_px=np.array([[20.0,10.0,30.0],[2.0,1.0,3.0]]),
        site_ids=((1,0),(0,0),(2,0)),
        valid_mask=(True,False,True),
        weights=(2.0,3.0,4.0),
        metadata={"name":"source"},
    )
    target=PositionReferenceTarget(
        registered_positions_px=np.array([[0.0,1.0,2.0],[0.0,0.0,0.0]]),
        measured_positions_px=np.array([[1.0,2.0,3.0],[4.0,5.0,6.0]]),
        site_ids=((0,0),(1,0),(9,0)),
    )

    resolved=resolve_exact_lattice(reference,target)

    np.testing.assert_array_equal(resolved.source_indices,np.array([1,0,-1]))
    np.testing.assert_array_equal(resolved.valid_mask,np.array([False,True,False]))
    np.testing.assert_allclose(
        resolved.desired_positions_px,
        np.array([[10.0,20.0,3.0],[1.0,2.0,6.0]]),
    )
    np.testing.assert_allclose(resolved.weights,np.array([3.0,2.0,0.0]))
    assert resolved.diagnostics["matched_site_count"]==2
    assert resolved.diagnostics["valid_site_count"]==1
    assert resolved.diagnostics["missing_target_site_ids"]==((9,0),)
    assert resolved.diagnostics["unused_source_site_ids"]==((2,0),)


def test_exact_lattice_requires_site_identity_but_not_identical_order():
    reference=ReferenceGeometry(
        positions_px=np.array([[0.0,1.0],[0.0,0.0]]),
    )
    target=PositionReferenceTarget(
        registered_positions_px=np.array([[0.0,1.0],[0.0,0.0]]),
        measured_positions_px=np.array([[0.0,1.0],[0.0,0.0]]),
    )
    with pytest.raises(ValueError,match="reference site IDs"):
        resolve_exact_lattice(reference,target)


def test_per_spot_alignment_applies_only_valid_weighted_correspondences():
    registered=np.array([[0.0,1.0,2.0],[0.0,0.0,0.0]])
    measured=np.array([[0.1,1.1,2.1],[0.0,0.0,0.0]])
    desired=np.array([[0.5,1.6,9.0],[0.2,0.2,9.0]])
    result=fit_position_alignment(
        registered,measured,desired,
        valid_mask=(True,True,True),weights=(1.0,0.0,1.0),model="per_spot",
    )

    np.testing.assert_array_equal(result.fit_mask,np.array([True,False,True]))
    np.testing.assert_allclose(result.predicted_positions_px[:,0],desired[:,0])
    np.testing.assert_allclose(result.predicted_positions_px[:,1],measured[:,1])
    np.testing.assert_allclose(result.predicted_positions_px[:,2],desired[:,2])
    np.testing.assert_allclose(result.correction_px,result.predicted_positions_px-measured)
    np.testing.assert_allclose(result.residual_px,desired-result.predicted_positions_px)
    assert result.transform is None


def test_continuous_alignment_uses_valid_intersection_and_predicts_complete_target():
    x,y=np.meshgrid(np.arange(3.0),np.arange(3.0),indexing="xy")
    registered=np.vstack([x.ravel(),y.ravel()])
    measured=registered+np.array([[0.15],[-0.08]])
    linear=np.array([[0.04,0.02],[-0.01,0.03]])
    displacement=linear@registered+np.array([[0.2],[-0.1]])
    desired=measured+displacement
    reference=ReferenceGeometry(
        positions_px=desired,
        site_ids=_ids(registered.shape[1]),
        weights=np.ones(registered.shape[1]),
    )
    matched=np.ones(registered.shape[1],dtype=bool)
    matched[-1]=False
    target=PositionReferenceTarget(
        registered_positions_px=registered,
        measured_positions_px=measured,
        matched_mask=matched,
        site_ids=_ids(registered.shape[1]),
    )
    resolved=resolve_exact_lattice(reference,target)

    result=align_resolved_position_reference(target,resolved,model="affine")

    assert result.transform is not None
    assert result.model=="affine"
    assert result.diagnostics["fit_count"]==registered.shape[1]-1
    np.testing.assert_allclose(result.registered_positions_px,registered)
    np.testing.assert_allclose(result.measured_positions_px,measured)
    np.testing.assert_allclose(result.desired_positions_px,desired)
    np.testing.assert_allclose(result.fit_sample_positions_px,registered[:,matched])
    np.testing.assert_allclose(
        result.fit_sample_displacements_px,(desired-measured)[:,matched],
    )
    np.testing.assert_allclose(result.predicted_positions_px,desired,atol=1e-12)
    np.testing.assert_allclose(result.correction_px,displacement,atol=1e-12)
    assert result.rms_residual<1e-12
    assert result.max_residual<1e-12


def _localization():
    indices=np.array([[0,1,0,1],[0,0,1,1]],dtype=np.int64)
    expected=np.array([[10.0,20.0,10.0,20.0],[10.0,10.0,20.0,20.0]])
    measured=expected+np.array([[0.5],[-0.25]])
    return LocalizationResult(
        target_type="multi_foci_vector",
        target_params={},parameters={},lattice_indices=indices,
        crop_coord=(5,35,7,37),cropped_image=np.zeros((30,30)),
        expected_positions_px=expected,measured_positions_px=measured,
        period_x_px=10.0,period_y_px=10.0,
        offset_x_px=10.0,offset_y_px=10.0,
        diagnostics={
            "matched_mask":(True,False,True,True),
            "inferred_mask":(False,True,False,False),
            "affine_linear":((10.0,0.0),(0.0,10.0)),
            "affine_translation":(10.0,10.0),
        },
    )


def test_source_adapters_keep_full_detector_frame_and_genuine_target_fit_mask():
    localization=_localization()
    measured=reference_geometry_from_localization(localization)
    global_fit=reference_geometry_from_global_fit(localization)
    target=position_reference_target_from_localization(localization)

    offset=np.array([[7.0],[5.0]])
    np.testing.assert_allclose(
        measured.positions_px,localization.measured_positions_px+offset,
    )
    np.testing.assert_array_equal(measured.valid_mask,np.ones(4,dtype=bool))
    np.testing.assert_allclose(
        global_fit.positions_px,localization.expected_positions_px+offset,
    )
    np.testing.assert_array_equal(
        target.matched_mask,np.array([True,False,True,True]),
    )
    np.testing.assert_allclose(
        target.registered_positions_px,localization.expected_positions_px+offset,
    )


def test_saved_reference_adapter_preserves_full_positions_and_provenance():
    localization=_localization()
    positions=localization.measured_positions_px+np.array([[7.0],[5.0]])
    saved=PositionReference(
        name="main alignment",observation_plane_name="Main",
        lattice_indices=localization.lattice_indices,
        positions_px=positions,image_shape=(100,120),detector_name="cam",
        metadata={"source_section":"488"},created_at="2026-10-08T12:00:00",
    )
    geometry=reference_geometry_from_position_reference(saved)

    np.testing.assert_allclose(geometry.positions_px,positions)
    assert geometry.site_ids==((0,0),(1,0),(0,1),(1,1))
    assert geometry.metadata["coordinate_frame"]=="full_detector"
    assert geometry.metadata["observation_plane_name"]=="Main"
    assert geometry.metadata["reference_metadata"]=={"source_section":"488"}


def test_self_current_reference_factory_starts_with_zero_correction():
    localization=_localization()
    target=position_reference_target_from_localization(localization)
    source=reference_geometry_from_current_target(target)
    resolved=resolve_exact_lattice(source,target)
    result=align_resolved_position_reference(target,resolved,model="per_spot")

    np.testing.assert_allclose(resolved.desired_positions_px,target.measured_positions_px)
    np.testing.assert_allclose(result.predicted_positions_px,target.measured_positions_px)
    np.testing.assert_allclose(result.correction_px,0.0)


def test_selection_facade_separates_source_and_correction_state():
    from slmcore import (
        PositionCorrectionSelection,PositionReferenceSelection,
        PositionReferenceSourceSelection,
    )

    selection=PositionReferenceSelection(
        mode="reference",reference_name="ref",correspondence="flip_x",
        correction_mode="polynomial",polynomial_degree=3,
    )
    assert isinstance(selection.source,PositionReferenceSourceSelection)
    assert isinstance(selection.correction,PositionCorrectionSelection)
    assert selection.mode.value=="reference"
    assert selection.reference_name=="ref"
    assert selection.correspondence.value=="flip_x"
    assert selection.correction_mode=="polynomial"
    assert selection.polynomial_degree==3

def test_localization_adapters_retain_staggered_lattice_topology():
    from dataclasses import replace

    localization=_localization()
    diagnostics=dict(localization.diagnostics)
    diagnostics["resolved_stagger"]=0.75
    localization=replace(
        localization,target_params={"stagger":0.75},diagnostics=diagnostics,
    )

    target=position_reference_target_from_localization(localization)
    source=reference_geometry_from_localization(localization)

    assert target.lattice is not None
    assert source.lattice is not None
    assert target.lattice.diagnostics["stagger"]==pytest.approx(0.75)
    assert target.lattice.diagnostics["canonical_stagger"]==pytest.approx(-0.25)
    np.testing.assert_array_equal(
        target.lattice.basis_indices,np.array([0,0,1,1]),
    )
    np.testing.assert_array_equal(
        target.lattice.cell_indices,np.array([[0,1,1,2],[0,0,0,0]]),
    )
    np.testing.assert_allclose(source.lattice.logical_positions,target.lattice.logical_positions)


def test_selection_facade_accepts_generic_manual_polynomial_transform():
    from slmcore import PositionReferenceSelection
    from slmcore.core.transform2d import FittedTransform2D

    transform=FittedTransform2D(
        model="polynomial",polynomial_degree=2,
        coefficients=np.zeros((2,6),dtype=np.float64),
        normalization_center=np.array([20.0,30.0]),
        normalization_scale=np.array([10.0,12.0]),
    )
    selection=PositionReferenceSelection(
        mode="current",correction_mode="polynomial",polynomial_degree=2,
        manual_transform=transform.to_dict(),
    )

    assert selection.mode.value=="current"
    assert selection.manual_transform is not None
    assert selection.manual_transform.model=="polynomial"
    assert selection.affine_transform is None
