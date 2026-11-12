"""UI-neutral settings shared by MoNaLISA localization front-ends.

The current MoNaLISA lattice localizer and the uLens localizer remain separate
algorithms.  These small value objects define the common vocabulary the UI can
use now so the algorithms can be converged later without another parameter/API
rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class PeriodPrior:
    """Expected lattice period(s), used to constrain rather than freeze a fit."""

    enabled: bool = False
    expected_x_px: float = 0.0
    expected_y_px: float = 0.0
    tolerance_fraction: float = 0.20

    def period_band_px(self) -> tuple[float, float] | None:
        if not self.enabled:
            return None
        expected = [float(v) for v in (self.expected_x_px, self.expected_y_px) if float(v) > 0]
        if not expected:
            return None
        tolerance = max(0.0, float(self.tolerance_fraction))
        low = min(expected) * max(0.0, 1.0 - tolerance)
        high = max(expected) * (1.0 + tolerance)
        if low <= 0 or high <= low:
            raise ValueError("Expected period prior must define a positive search band")
        return float(low), float(high)


@dataclass(frozen=True)
class LocalizationSettings:
    """Tuning parameters for automatic pattern localization."""

    min_period_px: float = 3.0
    max_period_px: float = 0.0  # 0 lets the localizer choose from the frame size
    num_candidate_peaks: int = 12
    min_peak_significance: float = 8.0
    period_prior: PeriodPrior = PeriodPrior()

    def period_band_px(self) -> tuple[float, float | None]:
        prior = self.period_prior.period_band_px()
        if prior is not None:
            return prior
        low = max(float(self.min_period_px), 1e-9)
        high = float(self.max_period_px)
        return low, (high if high > 0 else None)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict | None) -> "LocalizationSettings":
        value = dict(value or {})
        prior = value.pop("period_prior", None)
        if isinstance(prior, PeriodPrior):
            period_prior = prior
        else:
            period_prior = PeriodPrior(**dict(prior or {}))
        allowed = {
            "min_period_px",
            "max_period_px",
            "num_candidate_peaks",
            "min_peak_significance",
        }
        kwargs = {key: value[key] for key in allowed if key in value}
        return cls(period_prior=period_prior, **kwargs)


DEFAULT_LOCALIZATION_SETTINGS = LocalizationSettings()


__all__ = [
    "DEFAULT_LOCALIZATION_SETTINGS",
    "LocalizationSettings",
    "PeriodPrior",
]
