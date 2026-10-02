"""ImControl-specific correspondence strategies for transform calibration."""

from __future__ import annotations

from typing import Any, Callable, Mapping

import numpy as np

from imswitch.imcommon.algorithms.transforms.correspondence import (
    CorrespondenceResult,
    CorrespondenceStrategySpec,
    find_grid_correspondence,
)
from .ulenses_localizer import LocalizationResult, localizer

__all__ = ["ulenses_grid_strategy_spec"]


def _normalize(parameters: Mapping[str, Any]) -> dict[str, Any]:
    normalized = {
        "use_ulenses_widget_guesses": bool(
            parameters.get("use_ulenses_widget_guesses", True)
        ),
        "xp_guess_px": float(parameters.get("xp_guess_px", 10.0)),
        "yp_guess_px": float(parameters.get("yp_guess_px", 10.0)),
        "threshold_rel": float(parameters.get("threshold_rel", 0.2)),
        "refine_radius": int(parameters.get("refine_radius", 5)),
        "gaussian_sigma": float(parameters.get("gaussian_sigma", 1.0)),
        "max_relative_tilt_deg": float(
            parameters.get("max_relative_tilt_deg", 30.0)
        ),
    }
    if normalized["xp_guess_px"] <= 0 or normalized["yp_guess_px"] <= 0:
        raise ValueError("uLens period guesses must be positive")
    if not 0.0 <= normalized["threshold_rel"] <= 1.0:
        raise ValueError("threshold_rel must be between 0 and 1")
    if normalized["refine_radius"] < 0:
        raise ValueError("refine_radius must be at least 0")
    if normalized["gaussian_sigma"] < 0:
        raise ValueError("gaussian_sigma must be at least 0")
    if not 0.0 < normalized["max_relative_tilt_deg"] <= 45.0:
        raise ValueError("max_relative_tilt_deg must be within (0, 45]")
    return normalized


def _localization_dict(result: LocalizationResult) -> dict[str, Any]:
    return {
        "x_period_px": float(result.xp),
        "x_offset_px": float(result.xo),
        "y_period_px": float(result.yp),
        "y_offset_px": float(result.yo),
        "n_cols": int(result.nx_c),
        "n_rows": int(result.ny_c),
    }


def ulenses_grid_strategy_spec(
    guess_provider: Callable[[], tuple[float, float] | None] | None = None,
) -> CorrespondenceStrategySpec:
    """Build the uLens-assisted regular-grid strategy.

    The legacy foci-affine workflow used the uLens localizer only to infer the
    grid dimensions and a safe spot-separation parameter.  Actual point
    detection/matching then ran through the foci-grid correspondence code.  We
    keep that proven split here while exposing it as one selectable strategy.
    """

    def run(
        source_image: np.ndarray,
        target_image: np.ndarray,
        parameters: Mapping[str, Any],
    ) -> CorrespondenceResult:
        params = _normalize(parameters)
        xp_guess = params["xp_guess_px"]
        yp_guess = params["yp_guess_px"]
        if params["use_ulenses_widget_guesses"] and guess_provider is not None:
            supplied = guess_provider()
            if supplied is not None:
                xp_guess, yp_guess = map(float, supplied)
                if xp_guess <= 0 or yp_guess <= 0:
                    raise ValueError("uLenses widget period guesses must be positive")

        source_loc = localizer(source_image, xp_guess=xp_guess, yp_guess=yp_guess)
        target_loc = localizer(target_image, xp_guess=xp_guess, yp_guess=yp_guess)

        source_shape = (int(source_loc.ny_c), int(source_loc.nx_c))
        target_shape = (int(target_loc.ny_c), int(target_loc.nx_c))
        if source_shape != target_shape:
            raise ValueError(
                "moving and reference uLens localization disagree: "
                f"moving {source_shape[0]}x{source_shape[1]}, "
                f"reference {target_shape[0]}x{target_shape[1]}"
            )
        n_rows, n_cols = source_shape
        if n_rows < 1 or n_cols < 1:
            raise ValueError(
                f"localized grid shape must be positive, got {n_rows}x{n_cols}"
            )

        periods = [source_loc.xp, source_loc.yp, target_loc.xp, target_loc.yp]
        if any(float(period) <= 0 for period in periods):
            raise ValueError("localized grid periods must be positive")
        min_distance = max(1, int(0.5 * min(float(period) for period in periods)))

        source_points, target_points = find_grid_correspondence(
            source_image,
            target_image,
            n_rows=n_rows,
            n_cols=n_cols,
            min_distance=min_distance,
            threshold_rel=params["threshold_rel"],
            refine_radius=params["refine_radius"],
            gaussian_sigma=params["gaussian_sigma"],
            max_relative_tilt_deg=params["max_relative_tilt_deg"],
        )
        return CorrespondenceResult(
            source_points=source_points,
            target_points=target_points,
            metadata={
                "n_rows": n_rows,
                "n_cols": n_cols,
                "detection": {
                    "min_distance": min_distance,
                    "threshold_rel": params["threshold_rel"],
                    "refine_radius": params["refine_radius"],
                    "gaussian_sigma": params["gaussian_sigma"],
                },
                "max_relative_tilt_deg": params["max_relative_tilt_deg"],
                "localization": {
                    "moving": _localization_dict(source_loc),
                    "reference": _localization_dict(target_loc),
                    "x_period_guess_px": xp_guess,
                    "y_period_guess_px": yp_guess,
                },
            },
        )

    return CorrespondenceStrategySpec(
        name="ulenses_grid",
        label="uLenses grid (auto)",
        description=(
            "Use the uLens periodic-grid localizer to infer the grid dimensions "
            "and spacing, then detect and pair the individual foci with the "
            "regular-grid matcher."
        ),
        handler=run,
        default_parameters={
            "use_ulenses_widget_guesses": True,
            "xp_guess_px": 10.0,
            "yp_guess_px": 10.0,
            "threshold_rel": 0.2,
            "refine_radius": 5,
            "gaussian_sigma": 1.0,
            "max_relative_tilt_deg": 30.0,
        },
        normalize_parameters=_normalize,
    )
