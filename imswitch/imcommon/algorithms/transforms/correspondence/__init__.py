"""Ways of finding corresponding points between two image spaces.

Correspondence finding is pluggable; fitting is shared (:mod:`..estimate`).
Every strategy returns paired ``(N, 2)`` arrays in ``(row, col)`` order.  The
registry types let hosts register workflow-specific localizers without making
``imcommon`` depend on those hosts.
"""

from .foci_grid import (
    detect_spot_centers,
    estimate_grid_angle,
    find_grid_correspondence,
    order_grid_points,
)
from .registry import (
    CorrespondenceResult,
    CorrespondenceStrategySpec,
    builtin_correspondence_specs,
)

__all__ = [
    "CorrespondenceResult",
    "CorrespondenceStrategySpec",
    "builtin_correspondence_specs",
    "detect_spot_centers",
    "estimate_grid_angle",
    "find_grid_correspondence",
    "order_grid_points",
]
