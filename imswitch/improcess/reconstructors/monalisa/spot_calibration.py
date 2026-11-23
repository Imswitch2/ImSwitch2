"""Standalone shared-width estimate using one representative MoNaLISA scan.

Independent of Gaussian extraction, variance-weighted placement and reassignment.
"""

from __future__ import annotations

import numpy as np

from .calibration import PatternLocalization
from .localization_settings import LocalizationSettings
from .pipeline import (
    PipelineParams, choose_calibration_image, find_localization,
)
from .spot_model import calibrate_spot_model


def estimate_shared_width(
    frames: np.ndarray, *, localization: PatternLocalization | None = None,
    localization_settings: LocalizationSettings | None = None,
) -> tuple[float, str, PatternLocalization | None]:
    """Return measured sigma (camera px), status, and an optional new localization.

    If a localization is already selected, use its actual motif points without
    changing its geometry. Otherwise detect the lattice with full calibration.
    """
    frames = np.asarray(frames)
    if frames.ndim != 3 or frames.shape[0] < 2:
        raise ValueError("FWHM estimation needs at least two frames of one in-plane scan")
    mean = frames.mean(axis=0, dtype=np.float64)
    modulation = frames.var(axis=0, dtype=np.float64)
    settings = localization_settings or LocalizationSettings()
    params = PipelineParams(
        spot_sigma_px=None, localization_refinement="full", localization_settings=settings,
    )
    if localization is None:
        finding = find_localization(mean, modulation, params)
        return (
            float(finding.spot.shared_sigma), finding.describe(),
            PatternLocalization.from_lattice(finding.lattice, source="estimated-width"),
        )
    calibration = choose_calibration_image(mean, modulation, params, lattice=localization)
    x, y = localization.points_in_frame(*mean.shape)
    if x.size == 0:
        raise ValueError("Selected localization has no foci in the camera frame")
    spot = calibrate_spot_model(
        calibration.image, x, y, None, mode="shared",
        modulation=calibration.name == "variance",
    )
    sigma = float(spot.shared_sigma)
    if not np.isfinite(sigma) or sigma <= 0:
        raise ValueError("FWHM estimation did not produce a finite positive width")
    return sigma, f"Estimated from {calibration.name} image using {len(x)} selected foci", None
