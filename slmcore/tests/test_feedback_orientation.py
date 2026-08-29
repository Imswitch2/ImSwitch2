from __future__ import annotations

import numpy as np

from slmcore.application.startup_preferences import StartupPreferencesState
from slmcore.core.cgh.feedback import (
    FeedbackOrientation,orient_localization,orientation_permutation,
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


def test_feedback_orientation_preferences_resolve_plane_then_default():
    saved = []
    state = StartupPreferencesState(SLMStartupPreferences(),saved.append)

    state.set_feedback_orientation_default("sec_0","flip_horizontal")
    assert state.feedback_orientation("sec_0") == "flip_horizontal"
    assert state.feedback_orientation("sec_0","sample") == "flip_horizontal"
    assert state.feedback_orientation_for_plane("sec_0","sample") is None

    state.set_feedback_orientation_for_plane("sec_0","sample","rotate_180")
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
