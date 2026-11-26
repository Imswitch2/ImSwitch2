"""Backward-compatible ImControl uLenses/Transform localization API.

The numerical finite-grid and legacy 1-D implementations have moved into
``imcommon``. The original objects, signatures, exception type, and semantics
remain available here as aliases (including for historical saved results).
"""

from imswitch.imcommon.algorithms.grid_localization.finite.ulenses import (
    GridLocalizationError, GridLocalizationResult, localize_grid as _numerical_localize_grid,
    refine_grid_foci, search_missing_grid_foci,
    _as_grid_image, _backend_options, _full_xy, _grid_from_registration,
    _run_slmcore_compatible, _candidate_score, _refine_focus_sites,
)
from functools import wraps
from inspect import signature

from imswitch.imcommon.algorithms.grid_localization import localize_pattern
from imswitch.imcommon.algorithms.grid_localization.legacy.ulenses import (
    LocalizationResult, localizer,
    _find_best_peak_index, _estimate_period, _estimate_offset, _optimize_parameters,
)

__all__ = [
    "LocalizationResult", "GridLocalizationResult", "GridLocalizationError",
    "localizer", "localize_grid", "refine_grid_foci", "search_missing_grid_foci",
]


@wraps(_numerical_localize_grid)
def localize_grid(img_data, **kwargs) -> GridLocalizationResult:
    """Compatibility API: shared finite dispatch, original result and errors.

    Both uLenses and Transform import this entry point. All optional numeric
    controls keep their previous defaults, keyword-only validation, and their
    original values; the PatternPrior reports only the effective known inputs.
    The proven finite topology-first/FFT-fallback engine runs unchanged.
    """
    # Validate the old call signature *before* any prior translation. Use the
    # bound defaults only for constructing the optional common prior; pass
    # the actual kwargs unchanged to retain the old backend's default policy.
    bound = signature(_numerical_localize_grid).bind(img_data, **kwargs)
    bound.apply_defaults()
    resolved = bound.arguments
    from .grid_localization_common_adapter import (
        prior_from_grid_controls, from_common,
    )
    try:
        prior = prior_from_grid_controls(
            xp_guess=resolved["xp_guess"],
            yp_guess=resolved["yp_guess"],
            use_period_prior=resolved["use_period_prior"],
            period_tolerance_fraction=resolved["period_tolerance_fraction"],
            n_cols=resolved["n_cols"],
            n_rows=resolved["n_rows"],
            stagger_mode=resolved["stagger_mode"],
            stagger_axis=resolved["stagger_axis"],
            stagger_fraction=resolved["stagger_fraction"],
        )
    except (TypeError, ValueError):
        # Invalid input remains the responsibility of the old finite backend,
        # preserving its public error type/wording as far as possible.
        prior = None
    common = localize_pattern(img_data, strategy="finite", prior=prior,
                              options=kwargs)
    return from_common(common)
