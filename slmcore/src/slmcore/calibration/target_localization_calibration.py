"""Diagonal calibration fitting from a target-localization result."""

from __future__ import annotations

from dataclasses import dataclass,field
from datetime import datetime
import math
from typing import Any,Mapping

import numpy as np

from .slm_section_calibration import SLMSectionCalibration

_FOURIER_REFERENCE_SIZE = 512.0


@dataclass(frozen=True)
class TargetLocalizationCalibrationCandidate:
    """Display-ready result of a simple target-localization calibration fit."""

    calibration: SLMSectionCalibration
    target_period_x_reference_px: float
    target_period_y_reference_px: float
    target_kx: float
    target_ky: float
    fitted_period_x_px: float
    fitted_period_y_px: float
    detector_pixel_size_um: float
    measured_period_x_um: float
    measured_period_y_um: float
    matched_count: int
    expected_count: int
    rms_residual_px: float | None = None
    warnings: tuple[str, ...] = ()
    metadata: Mapping[str,Any] = field(default_factory=dict)


def fit_target_localization_calibration(
    *,
    resolution: Any,
    localization: Any,
    detector_pixel_size_um: Any,
    plane: str | None=None,
    source: str="target_localization",
    metadata: Mapping[str, Any] | None=None,
    created_at: str | None=None,
) -> TargetLocalizationCalibrationCandidate:
    """Fit one diagonal SLM calibration from a resolved 2D target localization.

    The target period is read from the resolved target geometry attached to the
    supplied ``TargetResolution``.  Raw target parameters are deliberately not
    recalculated here because raster resolution may have selected different
    canonical/reference spacings.
    """

    geometry = getattr(resolution,"geometry",None)
    if not _is_lattice_target_geometry(geometry):
        raise ValueError(
            "Target localization calibration requires lattice target geometry."
        )

    target_period_x = _require_positive(
        getattr(geometry,"period_x_reference_px",None),
        "target period X",
    )
    target_period_y = _require_positive(
        getattr(geometry,"period_y_reference_px",None),
        "target period Y",
    )
    count_x = _require_int_at_least(
        getattr(geometry,"count_x",None),
        "target count X",
        2,
    )
    count_y = _require_int_at_least(
        getattr(geometry,"count_y",None),
        "target count Y",
        2,
    )

    fitted_period_x = _require_positive(
        getattr(localization,"period_x_px",None),
        "localized period X",
    )
    fitted_period_y = _require_positive(
        getattr(localization,"period_y_px",None),
        "localized period Y",
    )
    detector_scale = _require_positive(
        detector_pixel_size_um,
        "detector pixel size",
    )

    matched_count,expected_count = _validate_matched_2d_support(localization)

    target_kx = target_period_x / _FOURIER_REFERENCE_SIZE
    target_ky = target_period_y / _FOURIER_REFERENCE_SIZE
    measured_period_x = fitted_period_x * detector_scale
    measured_period_y = fitted_period_y * detector_scale

    warnings = []
    if math.isclose(
        target_period_x,target_period_y,rel_tol=0.0,abs_tol=1e-9,
    ) and count_x == count_y:
        warnings.append(
            "Symmetric target: X/Y axis assignment can be ambiguous. "
            "Prefer different X/Y periods or counts for calibration."
        )

    diagnostics = dict(getattr(localization,"diagnostics",{}) or {})
    rms_residual = _optional_finite_float(
        diagnostics.get("rms_residual_px",None)
    )

    fit_metadata = dict(metadata or {})
    fit_metadata.update(
        {
            "target_period_x_reference_px":target_period_x,
            "target_period_y_reference_px":target_period_y,
            "target_kx":target_kx,
            "target_ky":target_ky,
            "target_count_x":count_x,
            "target_count_y":count_y,
            "fitted_period_x_px":fitted_period_x,
            "fitted_period_y_px":fitted_period_y,
            "detector_pixel_size_um":detector_scale,
            "measured_period_x_um":measured_period_x,
            "measured_period_y_um":measured_period_y,
            "matched_count":matched_count,
            "expected_count":expected_count,
            "rms_residual_px":rms_residual,
            "warnings":tuple(warnings),
        }
    )

    calibration = SLMSectionCalibration(
        kx_per_um=target_kx / measured_period_x,
        ky_per_um=target_ky / measured_period_y,
        created_at=created_at or datetime.now().isoformat(),
        source=str(source or "target_localization"),
        plane=plane,
        cam_px_size_um=detector_scale,
        metadata=fit_metadata,
    )
    return TargetLocalizationCalibrationCandidate(
        calibration=calibration,
        target_period_x_reference_px=target_period_x,
        target_period_y_reference_px=target_period_y,
        target_kx=target_kx,
        target_ky=target_ky,
        fitted_period_x_px=fitted_period_x,
        fitted_period_y_px=fitted_period_y,
        detector_pixel_size_um=detector_scale,
        measured_period_x_um=measured_period_x,
        measured_period_y_um=measured_period_y,
        matched_count=matched_count,
        expected_count=expected_count,
        rms_residual_px=rms_residual,
        warnings=tuple(warnings),
        metadata=fit_metadata,
    )


def _validate_matched_2d_support(localization: Any) -> tuple[int, int]:
    lattice_indices = np.asarray(getattr(localization,"lattice_indices",None))
    if lattice_indices.ndim != 2 or lattice_indices.shape[0] != 2:
        raise ValueError(
            "Localization lattice indices must have shape (2, N)."
        )

    expected_count = int(lattice_indices.shape[1])
    diagnostics = dict(getattr(localization,"diagnostics",{}) or {})
    if "matched_mask" not in diagnostics:
        raise ValueError(
            "Localization result is missing matched-point information."
        )
    matched_mask = np.asarray(diagnostics["matched_mask"],dtype=bool)
    if matched_mask.shape != (expected_count,):
        raise ValueError(
            "Localization matched-point information does not match the "
            "lattice size."
        )

    matched_count = int(np.count_nonzero(matched_mask))
    if matched_count < 3:
        raise ValueError(
            "At least three matched localized spots are required for "
            "target-localization calibration."
        )
    matched_indices = lattice_indices[:,matched_mask]
    if np.unique(matched_indices[0]).size < 2:
        raise ValueError(
            "Matched localized spots must span at least two target X indices."
        )
    if np.unique(matched_indices[1]).size < 2:
        raise ValueError(
            "Matched localized spots must span at least two target Y indices."
        )
    return matched_count,expected_count


def _is_lattice_target_geometry(geometry: Any) -> bool:
    if getattr(geometry,"geometry_type",None) != "lattice":
        return False
    # Keep calibration import-light: slmcore imports calibration before the
    # built-in CGH target registry is loaded.
    return any(
        cls.__name__ == "LatticeTargetGeometry"
        for cls in type(geometry).__mro__
    )


def _require_positive(value: Any,name: str) -> float:
    try:
        result = float(value)
    except Exception as error:
        raise ValueError("%s must be a number." % name) from error
    if not math.isfinite(result) or result <= 0.0:
        raise ValueError("%s must be finite and > 0." % name)
    return result


def _require_int_at_least(value: Any,name: str,minimum: int) -> int:
    try:
        result = int(value)
    except Exception as error:
        raise ValueError("%s must be an integer." % name) from error
    if result < int(minimum):
        raise ValueError("%s must be >= %d." % (name,int(minimum)))
    return result


def _optional_finite_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except Exception:
        return None
    if not math.isfinite(result):
        return None
    return result


__all__ = [
    "TargetLocalizationCalibrationCandidate",
    "fit_target_localization_calibration",
]
