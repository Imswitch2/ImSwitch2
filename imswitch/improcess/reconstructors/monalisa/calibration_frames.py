"""Choose representative scan frames for MoNaLISA calibration.

Localization and spot-width calibration should see one in-plane scan of the
illumination cell.  Mixing Z planes, timepoints or conditions into one temporal
variance image makes specimen/defocus changes look like scan modulation and can
bias the measured spot width.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np


def modulation_score(frames: np.ndarray) -> float:
    """Mean temporal variance of one in-plane scan stack."""
    stack = np.asarray(frames)
    if stack.ndim < 3 or stack.shape[0] < 2:
        return 0.0
    return float(stack.var(axis=0, dtype=np.float64).mean())


def most_modulated_indices(
    frames: np.ndarray,
    groups: Iterable[np.ndarray],
) -> np.ndarray:
    """Indices of the scan stack with the strongest in-plane modulation.

    ``groups`` are the frame indices belonging to individual scans of one
    illumination cell (for example one Z plane of a YZ recording).  Empty
    groups are ignored.  Ties are resolved in input order.
    """
    data = np.asarray(frames)
    candidates = [np.asarray(group, dtype=int).ravel() for group in groups]
    candidates = [group for group in candidates if group.size]
    if not candidates:
        return np.arange(data.shape[0], dtype=int)
    scores = [modulation_score(data[group]) for group in candidates]
    return candidates[int(np.argmax(scores))]
