"""Total-variation scan-orientation detector for MoNaLISA reconstruction.

Ports the variance-minimization strategy from Mini_Recon's
``core/geometry.get_orientation``: try every fast/slow axis assignment and
every positive/negative direction combination, build the reassembled image
each time, and pick the orientation whose total variation
(``sum(|Δx|) + sum(|Δy|)``) is lowest.

The intuition is that the correctly-oriented reconstruction preserves
sample-side spatial correlation, so neighbouring scan positions yield
nearby pixel values — i.e. the smoothest, lowest-TV image.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Sequence

import numpy as np


__all__ = [
    "auto_detect_scan_orientation",
    "total_variation",
]


def total_variation(image: np.ndarray) -> float:
    """Return ``sum(|Δx|) + sum(|Δy|)`` over the last two axes of ``image``.

    Mirrors the scoring function in Mini_Recon. Operates on the spatial
    extent only; any leading T/Z axes are collapsed via ``image.reshape``.
    """
    arr = np.asarray(image, dtype=np.float64)
    if arr.ndim < 2:
        return 0.0
    arr = arr.reshape(-1, arr.shape[-2], arr.shape[-1])
    delta_x = np.abs(np.diff(arr, axis=-1))
    delta_y = np.abs(np.diff(arr, axis=-2))
    return float(delta_x.sum() + delta_y.sum())


def auto_detect_scan_orientation(
    coeffs_base0: np.ndarray,
    scan_params: dict,
    axis_labels: dict,
    *,
    candidates: Sequence[tuple[tuple[str, str], tuple[str, str]]] | None = None,
) -> tuple[dict, str, float]:
    """Pick the lowest-total-variation scan orientation for one base.

    Args:
        coeffs_base0: 3D coefficient slice for the signal base, shape
            ``(numFrames, gridRows, gridCols)`` — i.e. exactly what
            ``coeffs_to_image`` consumes.  Use base 0 (signal) for the score
            so the choice is driven by structure, not by the background.
        scan_params: Original scan_params dict (must contain ``dimensions``,
            ``directions``, ``steps``, ``unidirectional``).  The detector
            varies ``dimensions[0:2]`` and ``directions[0:2]``; ``dimensions[2]``
            and other entries are kept intact.
        axis_labels: Same axis-name map ``coeffs_to_image`` uses
            (``r_l_text``, ``u_d_text``, ``b_f_text``, ``timepoints_text``,
            ``p_text``, ``n_text``).
        candidates: Optional override for the (dimensions[0:2], directions[0:2])
            permutations to try. Defaults to all 8 of
            (Right-Left fast / Up-Down fast) × (pos/neg)².

    Returns:
        ``(best_scan_params, label, score)`` where
        ``best_scan_params`` is a deep copy of ``scan_params`` with the
        winning ``dimensions[0:2]`` and ``directions[0:2]`` written back,
        ``label`` is a readable string like ``"R-L+ / U-D-"``, and
        ``score`` is the winning total-variation value.
    """
    # Late import to avoid the circular import that would otherwise occur:
    # coeffs_to_image lives next to this file and this function is itself
    # imported from the reconstructor that owns coeffs_to_image.
    from .coeffs_to_image import coeffs_to_image

    rl = axis_labels['r_l_text']
    ud = axis_labels['u_d_text']
    pos = axis_labels['p_text']
    neg = axis_labels['n_text']

    if candidates is None:
        candidates = []
        for fast_dim, mid_dim in ((rl, ud), (ud, rl)):
            for fast_dir in (pos, neg):
                for mid_dir in (pos, neg):
                    candidates.append(((fast_dim, mid_dim), (fast_dir, mid_dir)))

    best_score = np.inf
    best_params = scan_params
    best_label = ''
    for dims_2, dirs_2 in candidates:
        trial = deepcopy(scan_params)
        # Patch fast (index 0) and mid (index 1) dims; preserve the slow Z
        # entry at dimensions[2] and the timepoint entry at dimensions[3].
        trial['dimensions'][0] = dims_2[0]
        trial['dimensions'][1] = dims_2[1]
        trial['directions'][0] = dirs_2[0]
        trial['directions'][1] = dirs_2[1]
        try:
            image = coeffs_to_image(coeffs_base0, trial, axis_labels)
        except Exception:
            # An invalid permutation (e.g. step counts that can't reshape)
            # is just disqualified — don't let it tank the whole detection.
            continue
        score = total_variation(image)
        if score < best_score:
            best_score = score
            best_params = trial
            best_label = (
                f"{_short(dims_2[0], rl, ud)}{_sign(dirs_2[0], pos)} / "
                f"{_short(dims_2[1], rl, ud)}{_sign(dirs_2[1], pos)}"
            )

    return best_params, best_label, float(best_score)


def _short(dim: str, rl: str, ud: str) -> str:
    if dim == rl:
        return 'R-L'
    if dim == ud:
        return 'U-D'
    return dim


def _sign(direction: str, pos: str) -> str:
    return '+' if direction == pos else '-'


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
