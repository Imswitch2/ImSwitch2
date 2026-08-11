"""Ways of finding corresponding points between two image spaces.

Correspondence finding is pluggable; fitting is shared (:mod:`..estimate`).
The tree already contains four distinct strategies -- a regular foci grid,
RANSAC bead matching (etSTED auto-calibration and the multicolor
``descriptor_3d`` mode), FFT phase correlation, and pystackreg intensity
registration -- and they differ only in how they produce paired points.

Every strategy returns ``(source_points, target_points)`` as two ``(N, 2)``
arrays in ``(row, col)`` order, ready for :func:`..estimate.estimate_affine`.

``foci_grid`` ships at Step 0. Note that ``pystackreg`` is not a core
dependency of ImSwitch; any strategy built on it must import it lazily and fail
with an installation hint, as ``improcess.analysis.multicolor`` already does.
"""

from .foci_grid import (
    detect_spot_centers,
    estimate_grid_angle,
    find_grid_correspondence,
    order_grid_points,
)

__all__ = [
    "detect_spot_centers",
    "estimate_grid_angle",
    "find_grid_correspondence",
    "order_grid_points",
]
