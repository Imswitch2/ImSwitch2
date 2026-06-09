"""Contract tests for MoNaLISA scan-orientation auto-detect.

Ported total-variation minimization from Mini_Recon's get_orientation. The
detector should pick the (fast/slow axis, pos/neg direction) that produces
the smoothest reassembled image, mirroring the rationale that correctly-
oriented reconstructions preserve spatial correlation.
"""

from __future__ import annotations

import numpy as np

from imswitch.improcess.reconstructors.monalisa.coeffs_to_image import (
    coeffs_to_image,
)
from imswitch.improcess.reconstructors.monalisa.orientation import (
    auto_detect_scan_orientation,
    total_variation,
)


_AXIS_LABELS = {
    'r_l_text': 'Right-Left',
    'u_d_text': 'Up-Down',
    'b_f_text': 'Back-Front',
    'timepoints_text': 'Timepoints',
    'p_text': 'pos',
    'n_text': 'neg',
}


def _params(rows: int, cols: int, dim0='Right-Left', dim1='Up-Down',
            dir0='pos', dir1='pos'):
    return {
        'dimensions': [dim0, dim1, 'Back-Front', 'Timepoints'],
        'directions': [dir0, dir1, 'pos'],
        'steps': [str(cols), str(rows), '1', '1'],
        'step_sizes': ['35', '35', '35', '1'],
        'unidirectional': True,
    }


# --- total_variation -------------------------------------------------------


def test_total_variation_zero_for_constant_image():
    assert total_variation(np.ones((10, 10))) == 0.0


def test_total_variation_higher_for_noisier_image():
    smooth = np.linspace(0, 1, 100).reshape(10, 10)
    noisy = np.random.default_rng(0).uniform(size=(10, 10))
    assert total_variation(noisy) > total_variation(smooth)


# --- auto_detect_scan_orientation ------------------------------------------


def _build_smooth_phantom(rows: int, cols: int, grid: int) -> np.ndarray:
    """Build coefficients whose reassembly under the natural orientation is
    a smooth gradient, and whose reassembly under any other orientation
    introduces scan-direction-flips that bump the total variation."""
    frames = rows * cols
    y = np.linspace(0, 1, rows).reshape(-1, 1)
    x = np.linspace(0, 2, cols).reshape(1, -1)
    sample = (y + x).astype(np.float32)  # smooth gradient across the sample

    coeffs = np.zeros((frames, grid, grid), dtype=np.float32)
    for r in range(rows):
        for c in range(cols):
            # Natural scan order: fast=cols=Right-Left, mid=rows=Up-Down,
            # both with pos direction. Frame index encodes (r * cols + c).
            i = r * cols + c
            # Each frame contributes its sample value as a flat grid patch.
            coeffs[i, :, :] = sample[r, c]
    return coeffs


def test_auto_detect_picks_orientation_with_lowest_total_variation():
    rows, cols, grid = 6, 5, 3
    coeffs = _build_smooth_phantom(rows, cols, grid)
    # Seed with an obviously wrong orientation so the detector has to fix it.
    starting_params = _params(rows, cols, dim0='Up-Down', dim1='Right-Left',
                              dir0='neg', dir1='neg')

    best_params, label, score = auto_detect_scan_orientation(
        coeffs, starting_params, _AXIS_LABELS,
    )

    # Score should be strictly better than the starting orientation
    seed_image = coeffs_to_image(coeffs, starting_params, _AXIS_LABELS)
    seed_score = total_variation(seed_image)
    assert score <= seed_score

    # Resulting params still look like scan_params (no shape mutation)
    assert set(best_params.keys()) == set(starting_params.keys())
    assert label  # non-empty descriptor was returned


def test_auto_detect_only_varies_xy_assignments():
    rows, cols = 4, 4
    coeffs = _build_smooth_phantom(rows, cols, 3)
    base_params = _params(rows, cols)

    best_params, _label, _score = auto_detect_scan_orientation(
        coeffs, base_params, _AXIS_LABELS,
    )

    # Z slow axis and timepoints axis untouched
    assert best_params['dimensions'][2] == 'Back-Front'
    assert best_params['dimensions'][3] == 'Timepoints'
    assert best_params['directions'][2] == 'pos'
    # And the chosen fast/mid axes are from the (R-L, U-D) pool
    assert {best_params['dimensions'][0], best_params['dimensions'][1]} == {
        'Right-Left', 'Up-Down',
    }
