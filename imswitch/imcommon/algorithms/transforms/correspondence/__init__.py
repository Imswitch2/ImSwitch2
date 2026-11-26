"""Ways of finding corresponding points between two image spaces.

Correspondence finding is pluggable; fitting is shared (:mod:`..estimate`).
Every strategy returns paired ``(N, 2)`` arrays in ``(row, col)`` order.  The
registry types let hosts register workflow-specific localizers without making
``imcommon`` depend on those hosts.
"""

from .beads import (
    BeadDetections,
    BeadMatch,
    BeadMatchError,
    bead_strategy_spec,
    detect_beads,
    match_point_sets,
)
from .foci_grid import (
    detect_spot_centers,
    estimate_grid_angle,
    find_grid_correspondence,
    order_grid_points,
)
from .grid_correspondence import (
    GridCorrespondence,
    GridCorrespondenceResolution,
    GridOrdering,
    available_grid_orderings,
    correspondence_options,
    grid_ordering_label,
    grid_ordering_permutation,
    resolve_grid_correspondence,
)
from .registry import (
    LocalizationDiagnostics,
    LocalizationFailure,
    ImageLocalizationResult,
    CorrespondenceResult,
    CorrespondenceStrategySpec,
    builtin_correspondence_specs,
)

__all__ = [
    "BeadDetections",
    "BeadMatch",
    "BeadMatchError",
    "bead_strategy_spec",
    "detect_beads",
    "match_point_sets",
    "GridCorrespondence",
    "GridCorrespondenceResolution",
    "GridOrdering",
    "available_grid_orderings",
    "correspondence_options",
    "grid_ordering_label",
    "grid_ordering_permutation",
    "resolve_grid_correspondence",
    "LocalizationDiagnostics",
    "LocalizationFailure",
    "ImageLocalizationResult",
    "CorrespondenceResult",
    "CorrespondenceStrategySpec",
    "builtin_correspondence_specs",
    "detect_spot_centers",
    "estimate_grid_angle",
    "find_grid_correspondence",
    "order_grid_points",
]
