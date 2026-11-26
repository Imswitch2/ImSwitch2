"""Opt-in unified facade over the proven, separate periodic and finite paths.

This API does *not* replace the long-lived ``detect_lattice`` / ``localize_grid``
entry points. Their algorithms and default options are unchanged. Known pattern
information is optional, and the canonical result records how it was used.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Literal, Mapping

import numpy as np

from .adapters import from_finite_grid, from_periodic_localization
from .contracts import PatternLocalizationResult, PatternPrior
from .finite.ulenses import localize_grid
from .periodic.lattice import detect_lattice


def localize_pattern(
    image: np.ndarray,
    *,
    strategy: Literal["periodic", "finite"],
    prior: PatternPrior | None = None,
    options: Mapping[str, Any] | None = None,
) -> PatternLocalizationResult:
    """Localize with a proven backend and return a normalized result.

    ``options`` are the *existing* backend keyword arguments (periodic:
    ``detect_lattice``; finite: ``localize_grid``). Supplying no prior leaves
    the backend defaults untouched. A prior may constrain a backend only where
    that backend currently supports the information. Unsupported semantic
    metadata is recorded as ignored, not presented as an applied constraint.

    This facade does not perform ImProcess calibration-image selection,
    Gaussian focus fitting, modality matching, or finite-site reconstruction.
    """
    if strategy not in ("periodic", "finite"):
        raise ValueError("strategy must be 'periodic' or 'finite'")
    if prior is not None and not isinstance(prior, PatternPrior):
        raise TypeError("prior must be a PatternPrior or None")
    kwargs = dict(options or {})
    usage = {name: "ignored" for name in prior.provided_fields} if prior else {}

    def supply(key: str, value: Any, *source_fields: str) -> None:
        if key in kwargs:
            # A conflicting explicit algorithm setting must never be hidden
            # by a shared pattern hint; require the caller to resolve it.
            if kwargs[key] != value:
                raise ValueError(f"Conflicting {key!r} in options and PatternPrior")
        else:
            kwargs[key] = value
        for field in source_fields:
            usage[field] = "applied"

    if strategy == "periodic":
        if prior is not None:
            periods = [v for v in (
                prior.expected_period_x_px, prior.expected_period_y_px
            ) if v is not None]
            if periods:
                tolerance = (0.25 if prior.period_tolerance_fraction is None
                             else prior.period_tolerance_fraction)
                min_period = min(periods) * max(1e-6, 1.0 - tolerance)
                max_period = max(periods) * (1.0 + tolerance)
                if min_period >= max_period:
                    raise ValueError("PatternPrior periods produce an empty search band")
                source_fields = tuple(name for name in (
                    "expected_period_x_px", "expected_period_y_px"
                ) if getattr(prior, name) is not None)
                supply("min_period", min_period, *source_fields)
                supply("max_period", max_period, *source_fields)
                if prior.period_tolerance_fraction is not None:
                    usage["period_tolerance_fraction"] = "applied"
        result = from_periodic_localization(detect_lattice(image, **kwargs), prior=prior)
    else:
        if prior is not None:
            if prior.finite_site_indices is not None:
                # The uLenses finite strategy currently generates a full
                # rectangle. Silently replacing sparse provided sites with
                # absent ones would be incorrect.
                raise NotImplementedError(
                    "Exact finite_site_indices are not supported by the uLenses "
                    "grid algorithm; use the explicit finite registration API"
                )
            if prior.expected_period_x_px is not None:
                supply("xp_guess", prior.expected_period_x_px, "expected_period_x_px")
            if prior.expected_period_y_px is not None:
                supply("yp_guess", prior.expected_period_y_px, "expected_period_y_px")
            if (prior.expected_period_x_px is not None or
                    prior.expected_period_y_px is not None):
                supply("use_period_prior", True)
            if prior.period_tolerance_fraction is not None:
                supply("period_tolerance_fraction", prior.period_tolerance_fraction,
                       "period_tolerance_fraction")
            if prior.finite_shape_xy is not None:
                supply("n_cols", prior.finite_shape_xy[0], "finite_shape_xy")
                supply("n_rows", prior.finite_shape_xy[1], "finite_shape_xy")
            if prior.stagger_axis is not None and prior.stagger_fraction is not None:
                supply("stagger_mode", "manual", "stagger_axis", "stagger_fraction")
                supply("stagger_axis", {"x": "rows", "y": "columns"}[prior.stagger_axis],
                       "stagger_axis")
                supply("stagger_fraction", prior.stagger_fraction, "stagger_fraction")
        result = from_finite_grid(localize_grid(image, **kwargs), prior=prior)
    return replace(result, prior_usage=usage)
