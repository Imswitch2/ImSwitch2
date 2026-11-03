"""Representative-stack selection for MoNaLISA Auto calibration."""

import numpy as np

from imswitch.improcess.reconstructors.monalisa.calibration_frames import (
    modulation_score,
    most_modulated_indices,
)


def test_most_modulated_stack_is_selected_without_mixing_planes():
    rng = np.random.default_rng(2)
    frames = np.zeros((12, 16, 16), dtype=float)
    groups = [np.arange(0, 4), np.arange(4, 8), np.arange(8, 12)]

    # Static plane-to-plane offsets are deliberately large: combining all
    # planes would make them part of a global temporal variance.  Only the
    # middle in-plane scan contains the strong moving/modulated signal.
    frames[groups[0]] += 100.0
    frames[groups[1]] += 300.0
    frames[groups[2]] += 500.0
    pattern = rng.normal(size=(4, 16, 16))
    frames[groups[1]] += 20.0 * pattern
    frames[groups[0]] += rng.normal(scale=0.2, size=(4, 16, 16))
    frames[groups[2]] += rng.normal(scale=0.2, size=(4, 16, 16))

    chosen = most_modulated_indices(frames, groups)

    np.testing.assert_array_equal(chosen, groups[1])
    assert modulation_score(frames[chosen]) > modulation_score(frames[groups[0]])
    assert modulation_score(frames[chosen]) > modulation_score(frames[groups[2]])


def test_empty_group_list_falls_back_to_all_frames():
    frames = np.zeros((5, 3, 3), dtype=float)
    np.testing.assert_array_equal(
        most_modulated_indices(frames, []), np.arange(frames.shape[0])
    )
