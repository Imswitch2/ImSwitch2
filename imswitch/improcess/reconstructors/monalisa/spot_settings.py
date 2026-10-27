"""Shared MoNaLISA spot-width settings and unit conversion."""

from __future__ import annotations

import math

GAUSSIAN_FWHM_TO_SIGMA = 2.3548200450309493
DEFAULT_SPOT_FWHM_NM = 220.0


def fwhm_to_sigma_px(value: float, unit: str, pixel_size_nm: float | None) -> float:
    """Convert a user-facing Gaussian FWHM to the extractor's sigma in pixels."""
    fwhm = float(value)
    if not math.isfinite(fwhm) or fwhm <= 0:
        raise ValueError("Spot FWHM must be finite and > 0")
    unit = str(unit).strip().lower()
    if unit == "px":
        return fwhm / GAUSSIAN_FWHM_TO_SIGMA
    if unit == "nm":
        if pixel_size_nm is None or not math.isfinite(float(pixel_size_nm)) or float(pixel_size_nm) <= 0:
            raise ValueError("Spot FWHM in nm needs a positive camera pixel size")
        return fwhm / (GAUSSIAN_FWHM_TO_SIGMA * float(pixel_size_nm))
    raise ValueError(f"Unknown spot FWHM unit {unit!r}; expected 'nm' or 'px'")


def sigma_px_to_fwhm(sigma_px: float, unit: str, pixel_size_nm: float | None) -> float:
    sigma = float(sigma_px)
    unit = str(unit).strip().lower()
    if unit == "px":
        return sigma * GAUSSIAN_FWHM_TO_SIGMA
    if unit == "nm":
        if pixel_size_nm is None or float(pixel_size_nm) <= 0:
            raise ValueError("Spot FWHM in nm needs a positive camera pixel size")
        return sigma * GAUSSIAN_FWHM_TO_SIGMA * float(pixel_size_nm)
    raise ValueError(f"Unknown spot FWHM unit {unit!r}")


__all__ = [
    "DEFAULT_SPOT_FWHM_NM",
    "GAUSSIAN_FWHM_TO_SIGMA",
    "fwhm_to_sigma_px",
    "sigma_px_to_fwhm",
]
