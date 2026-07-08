"""Pure-function detection preview computation for SMLM localizer UI."""

from __future__ import annotations

import numpy as np

from .detection import detect_spots


def compute_detection_preview(
    image: np.ndarray | None,
    threshold: float,
    roi: int,
    sigma: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Compute scatter x/y coordinates for detected spots preview.

    Runs the net-gradient detection (no fitting) on the given 2D frame and
    returns scatter coordinates suitable for pyqtgraph display. The coordinate
    convention matches the DataFrame image view: detect_spots returns (row, col)
    and the scatter is plotted as x=col, y=row so a spot at image (r, c) lands
    on the correct pixel on screen.

    Parameters
    ----------
    image:
        2D frame to detect on. None or non-2D inputs return empty arrays.
    threshold:
        Net-gradient threshold for detection.
    roi:
        ROI size in pixels (used for edge exclusion).
    sigma:
        Gaussian smoothing sigma.

    Returns
    -------
    tuple[np.ndarray, np.ndarray]
        (x, y) coordinate arrays. x = column coords, y = row coords.
        Both are 1D float arrays of the same length. Empty when no spots
        detected or input is invalid.
    """
    if image is None:
        return np.array([], dtype=np.float32), np.array([], dtype=np.float32)

    image = np.asarray(image)
    if image.ndim != 2:
        return np.array([], dtype=np.float32), np.array([], dtype=np.float32)

    try:
        coords = detect_spots(image, threshold=threshold, roi=roi, sigma=sigma)
    except Exception:
        return np.array([], dtype=np.float32), np.array([], dtype=np.float32)

    if coords.size == 0:
        return np.array([], dtype=np.float32), np.array([], dtype=np.float32)

    rows, cols = coords[:, 0], coords[:, 1]
    x = cols.astype(np.float32)
    y = rows.astype(np.float32)
    return x, y


__all__ = ["compute_detection_preview"]
