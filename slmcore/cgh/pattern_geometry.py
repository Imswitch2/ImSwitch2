"""Reusable geometry metadata exposed by resolved CGH targets.

Target geometry describes what was requested in target/reference coordinates.
It is deliberately separate from detector-space localization geometry, which
may be supplied manually or inferred from an acquisition.
"""

from __future__ import annotations

from dataclasses import dataclass
import math


class PatternGeometry:
    """Base class for geometry metadata attached to a ``TargetResolution``."""

    geometry_type = "unknown"


@dataclass(frozen=True)
class LatticeTargetGeometry(PatternGeometry):
    """Resolved finite lattice geometry in target/reference coordinates.

    Periods are expressed in the canonical Fourier-reference pixel convention
    used by CGH targets.  ``stagger`` and ``count_x/count_y`` are structural
    target information and are therefore safe to expose independently from
    target rotation/skew correction parameters.
    """

    period_x_reference_px: float
    period_y_reference_px: float
    stagger: float
    count_x: int
    count_y: int

    geometry_type = "lattice"

    def __post_init__(self) -> None:
        px = float(self.period_x_reference_px)
        py = float(self.period_y_reference_px)
        stagger = float(self.stagger)
        count_x = int(self.count_x)
        count_y = int(self.count_y)

        if not math.isfinite(px) or px <= 0:
            raise ValueError("period_x_reference_px must be finite and > 0")
        if not math.isfinite(py) or py <= 0:
            raise ValueError("period_y_reference_px must be finite and > 0")
        if not math.isfinite(stagger) or not 0.0 <= stagger <= 1.0:
            raise ValueError("stagger must be finite and in [0, 1]")
        if count_x <= 0 or count_y <= 0:
            raise ValueError("count_x/count_y must be > 0")

        object.__setattr__(self,"period_x_reference_px",px)
        object.__setattr__(self,"period_y_reference_px",py)
        object.__setattr__(self,"stagger",stagger)
        object.__setattr__(self,"count_x",count_x)
        object.__setattr__(self,"count_y",count_y)

    @property
    def spot_count(self) -> int:
        return int(self.count_x * self.count_y)


__all__ = ["PatternGeometry","LatticeTargetGeometry"]
