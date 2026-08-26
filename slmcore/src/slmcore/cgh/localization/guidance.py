"""Resolve optional target/manual priors for generic lattice localization.

This module is the boundary between target semantics and the numerical image
localizer.  It converts whatever geometric knowledge is available into a
:class:`LocalizationGuidance` value.  The registration code consumes those
resolved values without depending on :class:`TargetResolution` or calibration
objects directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any,Mapping

import numpy as np

from ..coordinates import reference_px_to_k
from ..pattern_geometry import LatticeTargetGeometry
from ..targets.resolution import TargetResolution

@dataclass(frozen=True)
class LocalizationGuidance:
    """Resolved geometric priors supplied to the generic lattice localizer.

    Guidance records *what is known* about the expected image lattice, not
    why it is known. Values may originate from a target, manual user input
    or image-derived/automatic inference. This keeps the numerical
    registration engine independent of CGH target semantics.
    """

    geometry_type: str
    stagger: float | None
    stagger_source: str
    expected_period_px: tuple[float, float] | None
    period_source: str
    count_x: int | None
    count_y: int | None
    count_source: str

def localization_context(
    *,
    target_type: str,
    target_params: Mapping[str,Any],
    resolution: TargetResolution,
    calibration: Any=None,
) -> Mapping[str,Any]:
    """Resolve target-derived hints that are safe to expose to localization.

    Stagger is structural and can be used directly. Target rotation/skew are
    deliberately not projected into detector space because they may themselves
    be correction terms. Calibration is used only to predict detector-space
    primitive period lengths.
    """
    lattice_indices = _validated_lattice_indices(resolution)

    target_geometry = getattr(resolution,"geometry",None)
    geometry_type = (
        str(target_geometry.geometry_type)
        if target_geometry is not None
        else "lattice"
    )
    target_stagger = _target_stagger_hint(target_params,resolution)
    target_period = _target_detector_period_hint(resolution,calibration)
    target_count = _target_lattice_size_hint(resolution)

    return {
        "target_type":str(target_type),
        "target_geometry_type":geometry_type,
        "target_spot_count":int(lattice_indices.shape[1]),
        "target_stagger":target_stagger,
        "target_stagger_source":(
            "target" if target_stagger is not None else "unavailable"
        ),
        "target_expected_period_px":target_period,
        "target_period_source":(
            "target_calibration" if target_period is not None else "unavailable"
        ),
        "target_lattice_count":target_count,
        "target_lattice_count_source":(
            "target" if target_count is not None else "unavailable"
        ),
    }

def _validated_lattice_indices(resolution: TargetResolution) -> np.ndarray:
    if not isinstance(resolution,TargetResolution):
        raise TypeError("resolution must be a TargetResolution")
    lattice_indices = np.asarray(resolution.lattice_indices)
    if lattice_indices.ndim != 2 or lattice_indices.shape[0] != 2:
        raise ValueError("Target localization requires lattice_indices shape (2, N)")
    return lattice_indices

def resolve_localization_guidance(
    *,
    target_params: Mapping[str,Any],
    resolution: TargetResolution,
    calibration: Any,
    parameters: Mapping[str,Any],
) -> LocalizationGuidance:
    """Resolve target/manual/auto inputs into detector-space lattice guidance.

    Target rotation and skew are intentionally not treated as camera-space
    priors. They may themselves encode optical correction. Structural
    information such as stagger and finite lattice size is safe to reuse,
    while target period is projected into detector pixels only when a valid
    calibration is explicitly supplied.
    """
    geometry_type = str(
        parameters.get("pattern_geometry_type","lattice")
    ).strip().lower()
    if geometry_type != "lattice":
        raise ValueError("pattern_geometry_type must currently be 'lattice'")

    stagger_mode = _normalize_geometry_source(
        parameters.get("stagger_prior_mode","target"),
        "stagger_prior_mode",
    )
    if stagger_mode == "manual":
        stagger = float(parameters["manual_stagger"])
        if not np.isfinite(stagger) or not 0.0 <= stagger <= 1.0:
            raise ValueError("manual_stagger must be in [0, 1]")
        stagger_source = "manual"
    elif stagger_mode == "target":
        stagger = _target_stagger_hint(target_params,resolution)
        stagger_source = "target" if stagger is not None else "target_unavailable"
    else:
        stagger = None
        stagger_source = "auto"

    period_mode = _normalize_geometry_source(
        parameters.get("period_prior_mode","target"),
        "period_prior_mode",
    )
    if period_mode == "manual":
        expected_period = (
            float(parameters["expected_period_x_px"]),
            float(parameters["expected_period_y_px"]),
        )
        if (
            not np.all(np.isfinite(expected_period))
            or expected_period[0] <= 0
            or expected_period[1] <= 0
        ):
            raise ValueError("Manual expected periods must be finite and > 0")
        period_source = "manual"
    elif period_mode == "target":
        expected_period = _target_detector_period_hint(resolution,calibration)
        period_source = (
            "target_calibration"
            if expected_period is not None
            else "target_unavailable"
        )
    else:
        expected_period = None
        period_source = "auto"

    count_mode = _normalize_geometry_source(
        parameters.get("lattice_size_prior_mode","target"),
        "lattice_size_prior_mode",
    )
    if count_mode == "manual":
        count_x = int(parameters["manual_lattice_count_x"])
        count_y = int(parameters["manual_lattice_count_y"])
        if count_x <= 0 or count_y <= 0:
            raise ValueError("Manual lattice point counts must be > 0")
        count_source = "manual"
    elif count_mode == "target":
        target_count = _target_lattice_size_hint(resolution)
        if target_count is None:
            count_x = None
            count_y = None
            count_source = "target_unavailable"
        else:
            count_x,count_y = target_count
            count_source = "target"
    else:
        count_x = None
        count_y = None
        count_source = "auto"

    return LocalizationGuidance(
        geometry_type=geometry_type,
        stagger=stagger,
        stagger_source=stagger_source,
        expected_period_px=expected_period,
        period_source=period_source,
        count_x=count_x,
        count_y=count_y,
        count_source=count_source,
    )

def _normalize_geometry_source(value,name):
    mode = str(value).strip().lower()
    if mode == "none":
        mode = "auto"
    if mode not in ("target","manual","auto"):
        raise ValueError("%s must be 'target', 'manual' or 'auto'" % name)
    return mode

def _target_stagger_hint(
    target_params: Mapping[str,Any],
    resolution: TargetResolution,
) -> float | None:
    """Return exact structural stagger exposed by the resolved target."""
    geometry = getattr(resolution,"geometry",None)
    if isinstance(geometry,LatticeTargetGeometry):
        return float(geometry.stagger)

    sources = (
        target_params,
        getattr(resolution,"canonical_params",{}),
        getattr(resolution,"effective_params",{}),
    )
    for source in sources:
        if source is None or "stagger" not in source:
            continue
        try:
            value = float(source["stagger"])
        except Exception:
            continue
        if np.isfinite(value) and 0.0 <= value <= 1.0:
            return value
    return None

def _target_lattice_size_hint(
    resolution: TargetResolution,
) -> tuple[int, int] | None:
    """Return finite target X/Y lattice counts when available."""
    geometry = getattr(resolution,"geometry",None)
    if isinstance(geometry,LatticeTargetGeometry):
        return int(geometry.count_x),int(geometry.count_y)

    indices = np.asarray(getattr(resolution,"lattice_indices",()))
    if indices.ndim != 2 or indices.shape[0] != 2 or indices.shape[1] == 0:
        return None
    rounded = np.rint(indices).astype(np.int64)
    if not np.allclose(indices,rounded,rtol=0.0,atol=1e-9):
        return None
    count_x = int(np.unique(rounded[0]).size)
    count_y = int(np.unique(rounded[1]).size)
    if count_x*count_y != rounded.shape[1]:
        return None
    return count_x,count_y

def _target_detector_period_hint(
    resolution: TargetResolution,
    calibration: Any,
) -> tuple[float, float] | None:
    """Predict camera-space primitive period lengths without rotation/skew."""
    if calibration is None:
        return None
    validator = getattr(calibration,"is_valid",None)
    if not callable(validator) or not validator():
        return None
    pixel_size_um = getattr(calibration,"cam_px_size_um",None)
    if pixel_size_um is None or float(pixel_size_um) <= 0:
        return None

    geometry = getattr(resolution,"geometry",None)
    if isinstance(geometry,LatticeTargetGeometry):
        period_x_ref = float(geometry.period_x_reference_px)
        period_y_ref = float(geometry.period_y_reference_px)
    else:
        params = dict(getattr(resolution,"effective_params",{}) or {})
        if "period_x_px" not in params or "period_y_px" not in params:
            params = dict(getattr(resolution,"canonical_params",{}) or {})
        try:
            period_x_ref = float(params["period_x_px"])
            period_y_ref = float(params["period_y_px"])
        except Exception:
            return None
    if period_x_ref <= 0 or period_y_ref <= 0:
        return None

    kx = reference_px_to_k(period_x_ref)
    ky = reference_px_to_k(period_y_ref)
    try:
        x_dx_um,x_dy_um = calibration.kxy_to_um(kx,0.0)
        y_dx_um,y_dy_um = calibration.kxy_to_um(0.0,ky)
    except Exception:
        return None

    px_size = float(pixel_size_um)
    period_x_px = float(np.hypot(x_dx_um,x_dy_um) / px_size)
    period_y_px = float(np.hypot(y_dx_um,y_dy_um) / px_size)
    if not (
        np.isfinite(period_x_px)
        and np.isfinite(period_y_px)
        and period_x_px > 0
        and period_y_px > 0
    ):
        return None
    return (period_x_px,period_y_px)

__all__ = [
    "LocalizationGuidance",
    "localization_context",
    "resolve_localization_guidance",
]
