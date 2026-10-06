"""ImControl-specific correspondence strategies for transform calibration."""

from __future__ import annotations

from typing import Any, Callable, Mapping

import numpy as np

from imswitch.imcommon.algorithms.transforms import ParameterSpec
from imswitch.imcommon.algorithms.transforms.correspondence import (
    CorrespondenceResult,
    CorrespondenceStrategySpec,
)
from .ulenses_localizer import (
    GridLocalizationResult,
    localize_grid,
    refine_grid_foci,
)

__all__ = ["ulenses_grid_strategy_spec"]


def _normalize(parameters: Mapping[str, Any]) -> dict[str, Any]:
    normalized = {
        "use_ulenses_widget_guesses": bool(
            parameters.get("use_ulenses_widget_guesses", True)
        ),
        "xp_guess_px": float(parameters.get("xp_guess_px", 10.0)),
        "yp_guess_px": float(parameters.get("yp_guess_px", 10.0)),
        "grid_size_mode": str(parameters.get("grid_size_mode", "auto")),
        "n_cols": int(parameters.get("n_cols", 10)),
        "n_rows": int(parameters.get("n_rows", 10)),
        "stagger_mode": str(parameters.get("stagger_mode", "auto")),
        "stagger_axis": str(parameters.get("stagger_axis", "auto")),
        "stagger_fraction": float(parameters.get("stagger_fraction", 0.5)),
        "grid_threshold_rel": float(
            parameters.get("grid_threshold_rel", parameters.get("threshold_rel", 0.10))
        ),
        "grid_gaussian_sigma": float(
            parameters.get("grid_gaussian_sigma", 1.0)
        ),
        "grid_min_distance_fraction": float(
            parameters.get("grid_min_distance_fraction", 0.35)
        ),
        "focus_refine_radius": int(
            parameters.get("focus_refine_radius", parameters.get("refine_radius", 5))
        ),
        "focus_gaussian_sigma": float(
            parameters.get("focus_gaussian_sigma", parameters.get("gaussian_sigma", 1.0))
        ),
        "max_relative_tilt_deg": float(
            parameters.get("max_relative_tilt_deg", 30.0)
        ),
    }
    if normalized["xp_guess_px"] <= 0 or normalized["yp_guess_px"] <= 0:
        raise ValueError("uLens period guesses must be positive")
    if normalized["grid_size_mode"] not in ("auto", "manual"):
        raise ValueError("grid_size_mode must be 'auto' or 'manual'")
    if normalized["n_cols"] < 2 or normalized["n_rows"] < 2:
        raise ValueError("n_cols and n_rows must be at least 2")
    if normalized["stagger_mode"] not in ("auto", "none", "manual"):
        raise ValueError("stagger_mode must be 'auto', 'none', or 'manual'")
    if normalized["stagger_axis"] not in ("auto", "rows", "columns"):
        raise ValueError("stagger_axis must be 'auto', 'rows', or 'columns'")
    if normalized["stagger_mode"] == "manual" and normalized["stagger_axis"] == "auto":
        raise ValueError("manual stagger requires Rows or Columns stagger axis")
    if not -1.0 <= normalized["stagger_fraction"] <= 1.0:
        raise ValueError("stagger_fraction must be within [-1, 1]")
    if not 0.0 <= normalized["grid_threshold_rel"] <= 1.0:
        raise ValueError("grid_threshold_rel must be between 0 and 1")
    if normalized["grid_gaussian_sigma"] < 0:
        raise ValueError("grid_gaussian_sigma must be non-negative")
    if not 0.05 <= normalized["grid_min_distance_fraction"] <= 1.0:
        raise ValueError("grid_min_distance_fraction must be within [0.05, 1]")
    if normalized["focus_refine_radius"] < 0:
        raise ValueError("focus_refine_radius must be at least 0")
    if normalized["focus_gaussian_sigma"] < 0:
        raise ValueError("focus_gaussian_sigma must be at least 0")
    if not 0.0 < normalized["max_relative_tilt_deg"] <= 45.0:
        raise ValueError("max_relative_tilt_deg must be within (0, 45]")
    return normalized


def _grid_localization_dict(result: GridLocalizationResult) -> dict[str, Any]:
    return {
        "origin_rc": result.origin_rc.tolist(),
        "basis_x_rc": result.basis_x_rc.tolist(),
        "basis_y_rc": result.basis_y_rc.tolist(),
        "x_period_px": result.x_period_px,
        "y_period_px": result.y_period_px,
        "angle_deg": result.angle_deg,
        "n_cols": int(result.n_cols),
        "n_rows": int(result.n_rows),
        "stagger_axis": result.stagger_axis,
        "stagger_fraction": float(result.stagger_fraction),
        "diagnostics": dict(result.diagnostics),
    }


def _wrapped_grid_angle_difference(first_deg: float, second_deg: float) -> float:
    # Grid axes are equivalent modulo 90 degrees. Keep the smallest difference.
    difference = float(second_deg) - float(first_deg)
    return (difference + 45.0) % 90.0 - 45.0


def _check_same_finite_grid(
    source: GridLocalizationResult,
    target: GridLocalizationResult,
) -> None:
    if source.shape != target.shape:
        raise ValueError(
            "moving and reference grid localization disagree: "
            f"moving {source.n_rows}x{source.n_cols}, "
            f"reference {target.n_rows}x{target.n_cols}. "
            "For now transform calibration requires the same finite focus set."
        )
    source_axis = source.stagger_axis
    target_axis = target.stagger_axis
    source_stagger = abs(float(source.stagger_fraction))
    target_stagger = abs(float(target.stagger_fraction))
    # A numerically tiny inferred stagger is effectively non-staggered.
    if source_stagger < 0.05:
        source_axis = None
    if target_stagger < 0.05:
        target_axis = None
    if source_axis != target_axis:
        raise ValueError(
            "moving and reference grid localization disagree on stagger axis: "
            f"{source_axis or 'none'} vs {target_axis or 'none'}"
        )
    if source_axis is not None and abs(source_stagger - target_stagger) > 0.15:
        raise ValueError(
            "moving and reference grid localization disagree on stagger fraction: "
            f"{source.stagger_fraction:.3f} vs {target.stagger_fraction:.3f}"
        )


def ulenses_grid_strategy_spec(
    guess_provider: Callable[[], tuple[float, float] | None] | None = None,
) -> CorrespondenceStrategySpec:
    """Build the tilted/stagger-aware finite-grid correspondence strategy.

    Global geometry and precise correspondence are intentionally separate:
    :func:`localize_grid` first estimates finite lattice geometry from coarse
    spots, then :func:`refine_grid_foci` refines the predicted sites locally.
    The moving/reference focus sets are paired by their common finite logical
    indices.  No transform-specific assumptions leak back into the grid
    localizer itself.
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

        if params["grid_size_mode"] == "manual":
            n_cols = params["n_cols"]
            n_rows = params["n_rows"]
        else:
            n_cols = None
            n_rows = None

        grid_kwargs = {
            "xp_guess": xp_guess,
            "yp_guess": yp_guess,
            "n_cols": n_cols,
            "n_rows": n_rows,
            "threshold_rel": params["grid_threshold_rel"],
            "gaussian_sigma": params["grid_gaussian_sigma"],
            "min_distance_fraction": params["grid_min_distance_fraction"],
            "stagger_mode": params["stagger_mode"],
            "stagger_axis": params["stagger_axis"],
            "stagger_fraction": params["stagger_fraction"],
        }
        source_grid = localize_grid(source_image, **grid_kwargs)
        target_grid = localize_grid(target_image, **grid_kwargs)
        _check_same_finite_grid(source_grid, target_grid)

        tilt = abs(_wrapped_grid_angle_difference(source_grid.angle_deg, target_grid.angle_deg))
        if tilt > params["max_relative_tilt_deg"]:
            raise ValueError(
                f"the two localized grids differ in orientation by {tilt:.1f} degrees, "
                f"past the {params['max_relative_tilt_deg']:.1f} degree correspondence "
                "limit. The finite grid axes may be transposed."
            )

        source_points = refine_grid_foci(
            source_image,
            source_grid,
            search_radius_px=params["focus_refine_radius"],
            gaussian_sigma=params["focus_gaussian_sigma"],
        )
        target_points = refine_grid_foci(
            target_image,
            target_grid,
            search_radius_px=params["focus_refine_radius"],
            gaussian_sigma=params["focus_gaussian_sigma"],
        )
        if source_points.shape != target_points.shape:
            raise RuntimeError("refined finite focus sets do not have matching shapes")

        return CorrespondenceResult(
            source_points=source_points,
            target_points=target_points,
            metadata={
                "n_rows": int(source_grid.n_rows),
                "n_cols": int(source_grid.n_cols),
                "grid_size_mode": params["grid_size_mode"],
                "relative_tilt_deg": float(tilt),
                "grid_localization": {
                    "moving": _grid_localization_dict(source_grid),
                    "reference": _grid_localization_dict(target_grid),
                    "x_period_guess_px": float(xp_guess),
                    "y_period_guess_px": float(yp_guess),
                },
                "focus_refinement": {
                    "search_radius_px": params["focus_refine_radius"],
                    "gaussian_sigma": params["focus_gaussian_sigma"],
                },
            },
        )

    return CorrespondenceStrategySpec(
        name="ulenses_grid",
        label="uLenses grid",
        description=(
            "Localize a finite 2-D grid with arbitrary tilt and optional alternating "
            "row/column stagger, then refine and pair the individual foci by their "
            "logical grid indices. Auto grid size can be overridden with explicit "
            "rows/columns when needed."
        ),
        handler=run,
        parameter_specs={
            "use_ulenses_widget_guesses": ParameterSpec(
                True, bool, "Use uLenses widget period guesses",
                "Use the current uLenses widget X/Y periodicities as strong initial "
                "period guidance. Disable this to use the explicit guesses below.",
            ),
            "xp_guess_px": ParameterSpec(
                10.0, float, "X period guess (px)",
                "Expected logical X period in camera pixels. It guides coarse spot "
                "separation and finite-grid selection but does not fix the final fit.",
                minimum=0.001, step=0.5, decimals=3,
            ),
            "yp_guess_px": ParameterSpec(
                10.0, float, "Y period guess (px)",
                "Expected logical Y period in camera pixels. It guides localization "
                "without constraining the final measured tilted basis.",
                minimum=0.001, step=0.5, decimals=3,
            ),
            "grid_size_mode": ParameterSpec(
                "auto", str, "Grid size",
                "Auto infers the finite n_rows × n_cols grid from detected line "
                "topology. If Auto fails or is ambiguous, choose Manual and enter "
                "the known finite grid size below.",
                choices=("auto", "manual"),
            ),
            "n_cols": ParameterSpec(
                10, int, "Grid columns (manual)",
                "Known number of logical X sites. In Manual mode this is a strong "
                "finite-topology prior and is used directly.",
                minimum=2, step=1,
            ),
            "n_rows": ParameterSpec(
                10, int, "Grid rows (manual)",
                "Known number of logical Y sites. In Manual mode this is a strong "
                "finite-topology prior and is used directly.",
                minimum=2, step=1,
            ),
            "stagger_mode": ParameterSpec(
                "auto", str, "Stagger",
                "Auto compares non-staggered, alternating-row and alternating-column "
                "models. None disables stagger. Manual uses the axis/fraction below.",
                choices=("auto", "none", "manual"),
            ),
            "stagger_axis": ParameterSpec(
                "auto", str, "Stagger axis",
                "Rows means odd rows shift along logical X; Columns means odd columns "
                "shift along logical Y. Auto lets localization choose the better model.",
                choices=("auto", "rows", "columns"),
            ),
            "stagger_fraction": ParameterSpec(
                0.5, float, "Stagger fraction (manual)",
                "Alternating shift as a fraction of the corresponding lattice period. "
                "For example 0.5 is a half-period stagger.",
                minimum=-1.0, maximum=1.0, step=0.05, decimals=3,
            ),
            "grid_threshold_rel": ParameterSpec(
                0.10, float, "Grid spot threshold",
                "Relative threshold for the coarse spots used only to infer global "
                "grid geometry. Increase to reject weak false detections; decrease "
                "to retain dim grid sites.",
                minimum=0.0, maximum=1.0, step=0.02, decimals=2,
            ),
            "grid_gaussian_sigma": ParameterSpec(
                1.0, float, "Grid detection smoothing (px)",
                "Gaussian smoothing before coarse global-grid spot detection.",
                minimum=0.0, step=0.1, decimals=2, advanced=True,
            ),
            "grid_min_distance_fraction": ParameterSpec(
                0.35, float, "Coarse spot distance / period",
                "Minimum coarse-peak separation as a fraction of the smaller period "
                "guess. This affects global grid localization, not final focus centers.",
                minimum=0.05, maximum=1.0, step=0.05, decimals=2, advanced=True,
            ),
            "focus_refine_radius": ParameterSpec(
                5, int, "Focus refinement radius (px)",
                "Local search radius around each predicted finite-grid site. This is "
                "the precise per-focus correspondence stage used for transform fitting.",
                minimum=0, step=1,
            ),
            "focus_gaussian_sigma": ParameterSpec(
                1.0, float, "Focus search smoothing (px)",
                "Smoothing used only while selecting the local focus maximum before "
                "sub-pixel center-of-mass refinement.",
                minimum=0.0, step=0.1, decimals=2, advanced=True,
            ),
            "max_relative_tilt_deg": ParameterSpec(
                30.0, float, "Maximum relative grid tilt (deg)",
                "Safety limit between moving and reference grid orientations. The "
                "global localizer itself supports tilted grids; this only guards the "
                "finite-axis identity used for correspondence.",
                minimum=0.1, maximum=45.0, step=1.0, decimals=1, advanced=True,
            ),
        },
        normalize_parameters=_normalize,
    )
