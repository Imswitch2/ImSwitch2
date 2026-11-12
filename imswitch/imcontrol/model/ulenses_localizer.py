"""Backward-compatible ImControl uLenses/Transform localization API.

The numerical finite-grid and legacy 1-D implementations have moved into
``imcommon``. The original objects, signatures, exception type, and semantics
remain available here as aliases (including for historical saved results).
"""

from imswitch.imcommon.algorithms.grid_localization.finite.ulenses import (
    GridLocalizationError, GridLocalizationResult, localize_grid,
    refine_grid_foci, search_missing_grid_foci,
    _as_grid_image, _backend_options, _full_xy, _grid_from_registration,
    _run_slmcore_compatible, _candidate_score, _refine_focus_sites,
)
from imswitch.imcommon.algorithms.grid_localization.legacy.ulenses import (
    LocalizationResult, localizer,
    _find_best_peak_index, _estimate_period, _estimate_offset, _optimize_parameters,
)

__all__ = [
    "LocalizationResult", "GridLocalizationResult", "GridLocalizationError",
    "localizer", "localize_grid", "refine_grid_foci", "search_missing_grid_foci",
]
