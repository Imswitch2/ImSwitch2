from __future__ import annotations

import numpy as np

from slmcore.application.startup_preferences import StartupPreferencesState
from slmcore.core.cgh.feedback import (
    FeedbackOrientation,orient_localization,orientation_permutation,
    orientation_reindex_permutation,
)
from slmcore.core.cgh.localization import LocalizationResult
from slmcore.setup import SLMStartupPreferences


def _localization():
    indices = np.array([[0,1,0,1],[0,0,1,1]],dtype=np.int64)
    positions = np.array([
        [10.0,20.0,10.0,20.0],
        [10.0,10.0,20.0,20.0],
    ])
    return LocalizationResult(
        target_type="multi_foci",
        target_params={},
        parameters={},
        lattice_indices=indices,
        crop_coord=(0,32,0,32),
        cropped_image=np.zeros((32,32)),
        expected_positions_px=positions,
        measured_positions_px=positions + np.array([[1,2,3,4],[5,6,7,8]]),
        period_x_px=10,
        period_y_px=10,
        offset_x_px=10,
        offset_y_px=10,
        diagnostics={
            "matched_mask":(True,False,True,False),
            "inferred_mask":(False,True,False,False),
            "detection_indices":(0,1,2,3),
        },
    )


def test_horizontal_feedback_orientation_reindexes_correspondence_not_image():
    localization = _localization()
    oriented = orient_localization(
        localization,FeedbackOrientation.FLIP_HORIZONTAL,
    )
    np.testing.assert_array_equal(
        orientation_permutation(
            localization.lattice_indices,FeedbackOrientation.FLIP_HORIZONTAL,
        ),
        [1,0,3,2],
    )
    np.testing.assert_array_equal(
        oriented.measured_positions_px,
        localization.measured_positions_px[:,[1,0,3,2]],
    )
    np.testing.assert_array_equal(oriented.cropped_image,localization.cropped_image)
    assert oriented.diagnostics["matched_mask"] == (False,True,False,True)
    assert oriented.diagnostics["inferred_mask"] == (True,False,False,False)
    assert oriented.diagnostics["feedback_orientation"] == "flip_horizontal"


def test_all_feedback_orientations_are_bijections_for_square_lattice():
    localization = _localization()
    for orientation in FeedbackOrientation:
        permutation = orientation_permutation(
            localization.lattice_indices,orientation,
        )
        assert sorted(permutation.tolist()) == [0,1,2,3]


def test_orientation_reindex_permutation_converts_between_section_conventions():
    indices = np.array([
        [0,1,2,0,1,2],
        [0,0,0,1,1,1],
    ],dtype=np.int64)
    raw = np.arange(12,dtype=np.float64).reshape(2,6)

    for source_orientation in FeedbackOrientation:
        source_permutation = orientation_permutation(indices,source_orientation)
        source_ordered = raw[:,source_permutation]
        for target_orientation in FeedbackOrientation:
            target_permutation = orientation_permutation(indices,target_orientation)
            reindex = orientation_reindex_permutation(
                indices,source_orientation,target_orientation,
            )
            np.testing.assert_array_equal(
                source_ordered[:,reindex],raw[:,target_permutation],
            )


def test_feedback_orientation_preferences_resolve_plane_then_default():
    saved = []
    state = StartupPreferencesState(SLMStartupPreferences(),saved.append)

    state.set_feedback_orientation_default("sec_0","flip_horizontal")
    assert state.feedback_orientation("sec_0") == "flip_horizontal"
    assert state.feedback_orientation("sec_0","sample") == "flip_horizontal"
    assert state.feedback_orientation_for_observation_plane("sec_0","sample") is None

    state.set_feedback_orientation_for_observation_plane("sec_0","sample","rotate_180")
    assert state.feedback_orientation("sec_0","sample") == "rotate_180"
    assert state.feedback_orientation("sec_0","fourier") == "flip_horizontal"

    restored = SLMStartupPreferences.from_dict(state.value.to_dict())
    settings = restored.feedback_orientations["sec_0"]
    assert settings.default == "flip_horizontal"
    assert dict(settings.planes) == {"sample":"rotate_180"}
    assert saved[-1] == state.value


def test_feedback_orientation_pre_release_section_value_migrates_to_default():
    restored = SLMStartupPreferences.from_dict({
        "feedback_orientations":{"sec_0":"flip_horizontal"},
    })
    settings = restored.feedback_orientations["sec_0"]
    assert settings.default == "flip_horizontal"
    assert dict(settings.planes) == {}
    assert restored.to_dict()["feedback_orientations"] == {
        "sec_0":{
            "default":"flip_horizontal",
            "planes":{},
        },
    }


def test_staggered_feedback_orientation_keeps_topology_paired_with_positions():
    from slmcore.core.cgh.lattice_geometry import LatticeRepresentation

    x,y=np.meshgrid(np.arange(5),np.arange(4),indexing="xy")
    indices=np.vstack([x.ravel(),y.ravel()]).astype(np.int64)
    lattice=LatticeRepresentation.alternating_rows(indices,-0.5)
    logical=np.asarray(lattice.logical_positions,dtype=np.float64)
    linear=np.array([[18.0,1.5],[-0.7,17.0]])
    positions=linear@logical+np.array([[100.0],[80.0]])
    localization=LocalizationResult(
        target_type="multi_foci_vector",target_params={"stagger":-0.5},parameters={},
        lattice_indices=indices,crop_coord=(0,256,0,256),cropped_image=np.zeros((256,256)),
        expected_positions_px=positions,measured_positions_px=positions,
        period_x_px=18.0,period_y_px=17.0,offset_x_px=100.0,offset_y_px=80.0,
        diagnostics={
            "matched_mask":tuple(True for _ in range(indices.shape[1])),
            "resolved_stagger":-0.5,
            "lattice_representation":lattice.to_dict(),
        },
    )
    permutation=orientation_permutation(indices,FeedbackOrientation.FLIP_HORIZONTAL)
    oriented=orient_localization(localization,FeedbackOrientation.FLIP_HORIZONTAL)
    oriented_lattice=LatticeRepresentation.from_mapping(
        oriented.diagnostics["lattice_representation"]
    )

    # Orientation establishes target-site order in the detector arrays.  The
    # attached lattice is the authoritative target topology, not a permuted copy
    # of the source-frame motif.
    np.testing.assert_allclose(
        oriented_lattice.logical_positions,lattice.logical_positions,
    )
    np.testing.assert_allclose(
        oriented.measured_positions_px,localization.measured_positions_px[:,permutation],
    )
    assert oriented.diagnostics["feedback_effective_orientation"]=="flip_horizontal"


def test_staggered_target_order_drives_position_feedback_sign_consistently():
    """Regression for the 405-style signed-stagger + horizontal-flip case.

    Localization is left in a residual Rotate-180 logical frame because the
    staggered finite motif cannot use every rectangular self-symmetry.  Feedback
    orientation must first restore true target-site order; analyze_position then
    needs no special stagger/sign handling.
    """
    from slmcore.core.cgh.feedback import FeedbackMeasurement
    from slmcore.core.cgh.feedback.analysis import analyze_position
    from slmcore.core.cgh.lattice_geometry import LatticeRepresentation
    from slmcore.core.measurement import ImageMeasurement

    nx=ny=5
    x,y=np.meshgrid(np.arange(nx),np.arange(ny),indexing="xy")
    indices=np.vstack([x.ravel(),y.ravel()]).astype(np.int64)
    lattice=LatticeRepresentation.alternating_rows(indices,-0.5)
    logical=np.asarray(lattice.centered_logical_positions,dtype=np.float64)

    feedback_linear=np.array([[-1.0,0.0],[0.0,1.0]])
    detector_linear=np.array([[20.0,2.0],[-1.0,18.0]])
    physical_target_positions=(
        detector_linear@(feedback_linear@logical)+np.array([[120.0],[100.0]])
    )

    # Camera-canonical localization columns are in a residual Rotate-180 frame.
    # For this odd-sized alternating-row lattice, composing Rotate-180 with the
    # configured horizontal feedback flip is a vertical target-site reversal.
    target_to_source=np.flipud(np.arange(nx*ny).reshape(ny,nx)).ravel()
    inverse=np.empty_like(target_to_source)
    inverse[target_to_source]=np.arange(target_to_source.size)
    raw_positions=physical_target_positions[:,inverse]
    localization=LocalizationResult(
        target_type="multi_foci_vector",
        target_params={
            "period_x_px":20.0,"period_y_px":18.0,
            "n_foci_x":nx,"n_foci_y":ny,
            "rotation_deg":0.0,"skew_deg":0.0,"stagger":-0.5,
        },
        parameters={},lattice_indices=indices,
        crop_coord=(0,256,0,256),cropped_image=np.zeros((256,256)),
        expected_positions_px=raw_positions,measured_positions_px=raw_positions,
        period_x_px=20.0,period_y_px=18.0,offset_x_px=120.0,offset_y_px=100.0,
        diagnostics={
            "matched_mask":tuple(True for _ in range(indices.shape[1])),
            "resolved_stagger":-0.5,
            "lattice_representation":lattice.to_dict(),
            "spot_ordering_detector_frame":"rotate_180",
        },
    )

    oriented=orient_localization(
        localization,FeedbackOrientation.FLIP_HORIZONTAL,target_lattice=lattice,
    )
    np.testing.assert_allclose(
        oriented.expected_positions_px,physical_target_positions,atol=1e-12,
    )
    assert oriented.diagnostics["feedback_orientation_topology_aware"] is False
    assert oriented.diagnostics["feedback_source_detector_frame"]=="rotate_180"
    assert oriented.diagnostics["feedback_effective_orientation"]=="flip_vertical"

    # Use an independent ideal-kxy affine so the sign check is not tautological.
    ideal_linear=np.array([[0.045,0.006],[-0.004,0.052]])
    ideal=ideal_linear@logical
    desired_kxy_shift=np.array([[0.003],[-0.002]])
    detector_per_kxy=(
        detector_linear@feedback_linear@np.linalg.inv(ideal_linear)
    )
    detector_error=detector_per_kxy@desired_kxy_shift
    reference=physical_target_positions+detector_error

    measurement=FeedbackMeasurement(
        acquisition=ImageMeasurement(
            image=np.zeros((256,256),dtype=np.float64),source="test",
        ),
        localization=oriented,
    )
    analysis=analyze_position(
        measurement,ideal_positions_kxy=ideal,
        reference_positions_px=reference,parameters={},
    )
    np.testing.assert_allclose(
        analysis.correction_kxy,
        np.repeat(desired_kxy_shift,indices.shape[1],axis=1),
        atol=1e-12,
    )
