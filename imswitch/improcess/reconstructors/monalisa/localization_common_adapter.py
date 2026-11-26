"""Patch-1 bridge from MoNaLISA's existing localization API to ImCommon.

These functions are opt-in. Reconstruction and the existing JSON-based session
calibration format continue to use PatternLocalization and PeriodPrior directly.
"""

from __future__ import annotations

from dataclasses import replace

from imswitch.imcommon.algorithms.grid_localization import (
    PatternPrior,
    PatternLocalizationResult,
    from_periodic_localization,
    periodic_components,
    localize_pattern,
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


def detect_lattice_shared(
    image,
    *,
    settings: LocalizationSettings,
    band: tuple[float, float | None],
) -> Lattice:
    """Dispatch the *unchanged* periodic FFT detector via the shared facade.

    ``band`` is the previously resolved MoNaLISA setting (including explicit
    ``PipelineParams.period_band_px`` overrides). It remains authoritative.
    Use the shared prior as an algorithm hint only when it describes precisely
    that same band; otherwise preserve the old numerical kwargs and report
    the prior as metadata, not a newly applied constraint.

    This function intentionally returns the historical Lattice for existing
    calibration, spot fitting, FOV enumeration and saved-job contracts.
    """
    prior = prior_from_settings(settings)
    options = dict(
        min_period=band[0], max_period=band[1],
        num_candidate_peaks=int(settings.num_candidate_peaks),
        min_peak_significance=float(settings.min_peak_significance),
    )
    # The shared API computes period bands from PatternPrior. It differs
    # from the existing ImProcess rule for extreme tolerances (>= 1) and
    # cannot represent a deliberate explicit band override. Preserve exact
    # old kwargs instead of broadening or narrowing the Fourier search.
    can_supply_prior = False
    if prior.provided_fields:
        periods = [v for v in (
            prior.expected_period_x_px, prior.expected_period_y_px
        ) if v is not None]
        tolerance = prior.period_tolerance_fraction
        if periods and tolerance is not None and tolerance < 1.0:
            same_band = (
                min(periods) * (1.0 - tolerance),
                max(periods) * (1.0 + tolerance),
            )
            can_supply_prior = same_band == band
    if can_supply_prior:
        # Period bounds are added by the facade, using the exact same
        # algorithm and numeric values as the historical settings.
        options.pop("min_period")
        options.pop("max_period")
        common = localize_pattern(image, strategy="periodic", prior=prior,
                                  options=options)
    else:
        common = localize_pattern(image, strategy="periodic", options=options)
        if prior.provided_fields:
            common = replace(
                common, prior=prior,
                prior_usage={key: "not_evaluated" for key in prior.provided_fields},
            )
    return from_common(common).lattice
