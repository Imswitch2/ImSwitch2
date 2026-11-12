"""Opt-in shared contract adapters for uLenses/Transform finite localization.

The existing ``ulenses_localizer`` API, controls and error behavior remain
unchanged. This module does not run a fit or make inferred priors authoritative.
"""

from __future__ import annotations

from imswitch.imcommon.algorithms.grid_localization import (
    PatternPrior,
    PatternLocalizationResult,
    from_finite_grid,
    finite_grid_components,
)

from .ulenses_localizer import GridLocalizationResult


def prior_from_grid_controls(
    *, xp_guess: float = 10.0, yp_guess: float = 10.0,
    use_period_prior: bool = True, period_tolerance_fraction: float = 0.25,
    n_cols: int | None = None, n_rows: int | None = None,
    stagger_mode: str = "auto", stagger_axis: str = "auto",
    stagger_fraction: float = 0.5,
) -> PatternPrior:
    """Map only effective inputs from existing localize_grid controls.

    In auto stagger mode the configured fraction is *not* an expected stagger;
    in manual mode it is. ``n_cols/n_rows=None`` means inferred finite size.
    Validation of old UI inputs stays in ``localize_grid`` itself.
    """
    kwargs = {}
    if use_period_prior:
        kwargs.update(
            expected_period_x_px=float(xp_guess),
            expected_period_y_px=float(yp_guess),
            period_tolerance_fraction=float(period_tolerance_fraction),
        )
    if n_cols is not None and n_rows is not None:
        kwargs["finite_shape_xy"] = (int(n_cols), int(n_rows))
    if stagger_mode == "manual" and stagger_axis in ("rows", "columns"):
        kwargs.update(
            stagger_axis="x" if stagger_axis == "rows" else "y",
            stagger_fraction=float(stagger_fraction),
        )
    return PatternPrior(**kwargs)


def to_common(grid: GridLocalizationResult,
              *, prior: PatternPrior | None = None) -> PatternLocalizationResult:
    return from_finite_grid(grid, prior=prior)


def from_common(result: PatternLocalizationResult) -> GridLocalizationResult:
    return GridLocalizationResult(**finite_grid_components(result))
