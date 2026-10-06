"""ImControl-specific correspondence strategies for transform calibration."""

from __future__ import annotations

from typing import Any, Callable, Mapping

import numpy as np

from imswitch.imcommon.algorithms.transforms import ParameterSpec
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
        parameter_specs={
            "use_ulenses_widget_guesses": ParameterSpec(
                True, bool, "Use uLenses widget period guesses",
                "When enabled, initialize the periodic localizer from the current "
                "uLenses widget X/Y period values. Disable it to use the explicit "
                "period guesses below.",
            ),
            "xp_guess_px": ParameterSpec(
                10.0, float, "X period guess (px)",
                "Initial horizontal period supplied to the current axis-aligned "
                "uLens periodic localizer. Used only when widget guesses are disabled "
                "or unavailable.",
                minimum=0.001, step=0.5, decimals=3,
            ),
            "yp_guess_px": ParameterSpec(
                10.0, float, "Y period guess (px)",
                "Initial vertical period supplied to the current axis-aligned "
                "uLens periodic localizer. Used only when widget guesses are disabled "
                "or unavailable.",
                minimum=0.001, step=0.5, decimals=3,
            ),
            "threshold_rel": ParameterSpec(
                0.2, float, "Focus threshold",
                "Relative peak threshold used by the individual-focus matcher after "
                "the periodic grid geometry has been estimated. Increase it to reject "
                "weak false detections; decrease it to include dim foci.",
                minimum=0.0, maximum=1.0, step=0.02, decimals=2,
            ),
            "refine_radius": ParameterSpec(
                5, int, "Center refinement radius (px)",
                "Radius used for sub-pixel refinement of each matched focus center.",
                minimum=0, step=1, advanced=True,
            ),
            "gaussian_sigma": ParameterSpec(
                1.0, float, "Focus detection smoothing (px)",
                "Gaussian smoothing used by the individual-focus detection stage.",
                minimum=0.0, step=0.1, decimals=2, advanced=True,
            ),
            "max_relative_tilt_deg": ParameterSpec(
                30.0, float, "Maximum relative grid tilt (deg)",
                "Safety limit used by the current focus-ordering matcher. This does "
                "not make the uLens periodic localizer itself rotation-aware; that is "
                "the next algorithmic pass.",
                minimum=0.1, maximum=45.0, step=1.0, decimals=1, advanced=True,
            ),
        },
        normalize_parameters=_normalize,
    )
