"""Patch-1 bridge from MoNaLISA's existing localization API to ImCommon.

These functions are opt-in. Reconstruction and the existing JSON-based session
calibration format continue to use PatternLocalization and PeriodPrior directly.
"""

from __future__ import annotations

from imswitch.imcommon.algorithms.grid_localization import (
    PatternPrior,
    PatternLocalizationResult,
    from_periodic_localization,
    periodic_components,
)

from .calibration import PatternLocalization
from .lattice import Lattice
from .localization_settings import LocalizationSettings


def prior_from_settings(settings: LocalizationSettings) -> PatternPrior:
    """Translate only an *enabled*, effective MoNaLISA period prior.

    Disabled or empty expected periods have no effect on the old detector.
    The old ``PeriodPrior.period_band_px()`` still owns search-band semantics:
    the shared contract is not a replacement for its numeric settings.
    """
    old = settings.period_prior
    if not old.enabled or old.period_band_px() is None:
        return PatternPrior()
    return PatternPrior(
        expected_period_x_px=float(old.expected_x_px) if old.expected_x_px > 0 else None,
        expected_period_y_px=float(old.expected_y_px) if old.expected_y_px > 0 else None,
        period_tolerance_fraction=max(0.0, float(old.tolerance_fraction)),
    )


def to_common(localization: PatternLocalization | Lattice,
              *, prior: PatternPrior | None = None) -> PatternLocalizationResult:
    """Take a non-mutating view of an existing periodic calibration."""
    return from_periodic_localization(localization, prior=prior)


def from_common(result: PatternLocalizationResult) -> PatternLocalization:
    """Restore the exact native pattern geometry/motif/provenance contract."""
    components = periodic_components(result)
    return PatternLocalization(
        lattice=Lattice(
            a1=components["a1"], a2=components["a2"],
            offset=components["offset"],
        ),
        motif=components["motif"],
        provenance=components["provenance"],
    )
