"""Estimator-independent choice of camera pixels used for Gaussian fitting.

Stored configurations written before the shared footprint controls are still
understood: Local used ``local_footprint_*`` and Joint used
``joint_footprint_*``.  Explicit shared keys take precedence.  This module
is UI-independent and deliberately does not select the estimator.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from .footprint_suggestion import suggest_local_footprint_size

SHAPES = ("square", "circle", "lattice")
MODES = ("from_lattice", "fixed", "fwhm")


@dataclass(frozen=True)
class FitFootprint:
    shape: str = "square"
    mode: str = "from_lattice"
    size_px: float = 9.0  # side for a square, diameter for a circle
    sigma_factor: float = 2.5  # radius / spot sigma in fwhm mode

    def validate(self) -> "FitFootprint":
        if self.shape not in SHAPES:
            raise ValueError(f"Unknown fitting footprint shape {self.shape!r}")
        if self.mode not in MODES:
            raise ValueError(f"Unknown fitting footprint sizing {self.mode!r}")
        if not math.isfinite(self.size_px) or self.size_px <= 0:
            raise ValueError("Fixed footprint size must be positive and finite")
        if not math.isfinite(self.sigma_factor) or self.sigma_factor <= 0:
            raise ValueError("FWHM-relative footprint factor must be positive and finite")
        if self.shape == "square" and self.mode == "fixed":
            if self.size_px < 3 or self.size_px > 255 or self.size_px % 2 != 1:
                raise ValueError("Fixed square footprint must have an odd integer side from 3 to 255 px")
        return self

    def resolved_size(self, localization, sigma_px: float) -> float | None:
        """Return side/diameter in px, or None for true lattice-cell ownership."""
        self.validate()
        if self.shape == "lattice":
            return None
        if self.mode == "fixed":
            return float(self.size_px)
        if self.mode == "from_lattice":
            return float(suggest_local_footprint_size(localization, self.shape))
        if not math.isfinite(sigma_px) or sigma_px <= 0:
            raise ValueError("FWHM-relative footprint requires a positive shared spot sigma")
        diameter = 2.0 * self.sigma_factor * sigma_px
        if self.shape == "circle":
            return diameter  # preserve the Joint radius with no quantization
        # Local square stencils use odd integer side lengths; a shared policy
        # must yield the same camera-pixel support in either estimator.
        side = max(3, int(math.ceil(diameter)))
        return float(min(255, side if side % 2 else side + 1))

    def to_dict(self) -> dict:
        return {"footprint_shape": self.shape, "footprint_mode": self.mode,
                "footprint_size_px": self.size_px,
                "footprint_sigma_factor": self.sigma_factor}


def footprint_from_params(params, *, estimator: str | None = None) -> FitFootprint:
    """Resolve new common fields or *old estimator-specific* saved settings.

    Presence of ``footprint_shape`` distinguishes the new contract from old
    saved jobs.  Do not merge defaults first: that would mask the old format.
    """
    if estimator is None:
        estimator = str(params.get("estimator", "joint"))
    if params.get("footprint_shape") is not None:
        return FitFootprint(
            shape=str(params["footprint_shape"]),
            mode=str(params.get("footprint_mode", "from_lattice")),
            size_px=float(params.get("footprint_size_px", 9.0)),
            sigma_factor=float(params.get("footprint_sigma_factor", 2.5)),
        ).validate()
    if estimator == "joint":
        old_mode = str(params.get("joint_footprint_mode", "auto"))
        if old_mode == "lattice":
            return FitFootprint("lattice", "from_lattice")
        if old_mode == "fixed":
            return FitFootprint("circle", "fixed",
                                2.0 * float(params.get("joint_footprint_radius_px", 4.5))).validate()
        if old_mode != "auto":
            raise ValueError(f"Unknown old Joint footprint mode {old_mode!r}")
        return FitFootprint("circle", "fwhm",
                            sigma_factor=float(params.get("reach_sigma", 2.5))).validate()
    old_shape = str(params.get("local_footprint_shape", "square"))
    return FitFootprint(
        old_shape,
        str(params.get("local_footprint_mode", "fixed")),
        float(params.get("local_footprint_size", 9)),
    ).validate()


def migrate_footprint_params(params: dict, *, estimator: str | None = None) -> dict:
    """Add common fields while retaining historical entries for old readers."""
    original = dict(params)
    return {**original, **footprint_from_params(original, estimator=estimator).to_dict()}
