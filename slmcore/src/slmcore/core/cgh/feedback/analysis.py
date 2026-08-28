"""Feedback-specific intensity and position analyses."""

from __future__ import annotations

from typing import Any,Mapping

import numpy as np

from ..localization import LocalizationResult
from .model import (
    FeedbackMeasurement,
    PositionAnalysis,
)


def analyze_position(
    measurement: FeedbackMeasurement,
    *,
    ideal_positions_kxy: np.ndarray,
    calibration: Any=None,
    parameters: Mapping[str,Any],
) -> PositionAnalysis:
    """Derive one non-cumulative correction relative to ideal target positions.

    Position correction itself does not require a section calibration.
    The localized target provides the detector-pixel -> target-kxy mapping.

    When a valid calibration with detector pixel size is available, physical
    position errors in micrometres are also calculated for diagnostics.
    """
    localization = measurement.localization
    if localization is None:
        raise RuntimeError(
            "Localize the acquired image before applying position correction"
        )

    _require_complete_localization(
        localization,
        "Position correction",
    )

    expected = np.asarray(
        localization.expected_positions_px,
        dtype=np.float64,
    )
    measured = np.asarray(
        localization.measured_positions_px,
        dtype=np.float64,
    )
    ideal = np.asarray(
        ideal_positions_kxy,
        dtype=np.float64,
    )

    if ideal.shape != expected.shape:
        raise ValueError(
            "Localized spot count does not match target ideal positions"
        )

    # Local residual around the globally registered lattice.
    error_px = expected - measured

    # Fit the detector-pixel -> target-kxy mapping directly from the
    # registered expected lattice and the corresponding ideal target
    # positions:
    #
    #     ideal_kxy = linear_px_to_kxy @ expected_px + translation
    #
    # For displacement vectors the translation cancels, so only the
    # linear part is needed.
    linear_px_to_kxy, _translation = _fit_affine(
        expected,
        ideal,
    )

    correction_kxy = (
        linear_px_to_kxy @ error_px
    )

    corrected = ideal + correction_kxy

    # Physical units are diagnostic only. They are available when the
    # section calibration carries a valid detector pixel size, but are
    # not required to calculate the correction.
    error_um = None

    if calibration is not None:
        validator = getattr(calibration, "is_valid", None)
        pixel_size_um = getattr(
            calibration,
            "cam_px_size_um",
            None,
        )

        if (
            callable(validator)
            and validator()
            and pixel_size_um is not None
            and float(pixel_size_um) > 0
        ):
            error_um = (
                error_px * float(pixel_size_um)
            )

    return PositionAnalysis(
        parameters=parameters,
        position_errors_px=error_px,
        position_errors_um=error_um,
        correction_kxy=correction_kxy,
        corrected_positions_kxy=corrected,
    )


def _fit_affine(source: np.ndarray,target: np.ndarray):
    source = np.asarray(source,dtype=np.float64)
    target = np.asarray(target,dtype=np.float64)
    if source.shape != target.shape or source.ndim != 2 or source.shape[0] != 2:
        raise ValueError("Affine point arrays must share shape (2, N)")
    if source.shape[1] < 3:
        raise ValueError("At least three points are required for an affine fit")
    design = np.column_stack([
        source.T,np.ones(source.shape[1],dtype=np.float64),
    ])
    coefficients,_,rank,_ = np.linalg.lstsq(design,target.T,rcond=None)
    if int(rank) < 3:
        raise ValueError("Affine point set is rank-deficient")
    linear = coefficients[:2,:].T
    translation = coefficients[2,:]
    if abs(float(np.linalg.det(linear))) < 1e-12:
        raise ValueError("Affine mapping is singular")
    return linear,translation


def _require_complete_localization(
    localization: LocalizationResult,purpose: str,
) -> None:
    matched = dict(localization.diagnostics or {}).get("matched_mask")
    if matched is None:
        # Old localization results were complete by construction.
        return
    matched = np.asarray(matched,dtype=bool)
    if matched.shape != (localization.lattice_indices.shape[1],):
        raise RuntimeError("Localization matched-mask shape is inconsistent")
    missing = int(np.count_nonzero(~matched))
    if missing:
        raise RuntimeError(
            "%s requires a complete target localization; %d of %d target spots "
            "were not matched. Inspect the localization or adjust its settings."
            % (purpose,missing,matched.size)
        )


def _sum_around(
    array: np.ndarray,
    coord_x: float,
    coord_y: float,
    window_size: int,
    *,
    zero_out: bool=False,
):
    """Sum a clipped square region and optionally zero it in the preview."""
    h,w = array.shape
    size = int(window_size)
    if size <= 0:
        raise ValueError("window_size must be > 0")
    half = size // 2
    off_x = int(round(float(coord_x) - half))
    off_y = int(round(float(coord_y) - half))
    sx,sy = np.meshgrid(np.arange(size),np.arange(size))
    sx = np.clip(sx + off_x,0,w - 1)
    sy = np.clip(sy + off_y,0,h - 1)
    sampled_sum = float(np.sum(array[sy,sx]))
    if zero_out:
        array[sy,sx] = 0
    return sampled_sum,array
