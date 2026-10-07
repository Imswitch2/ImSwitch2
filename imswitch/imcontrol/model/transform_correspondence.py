"""ImControl-specific correspondence strategies for transform calibration."""

from __future__ import annotations

from typing import Any, Callable, Mapping

import numpy as np

from imswitch.imcommon.algorithms.transforms import ParameterSpec
from imswitch.imcommon.algorithms.grid_localization import (
    LOCALIZATION_DEFAULTS, LOCALIZATION_PARAMETER_INFO,
)
from imswitch.imcommon.algorithms.transforms.correspondence import (
    CorrespondenceResult,
    CorrespondenceStrategySpec,
    ImageLocalizationResult,
    LocalizationDiagnostics,
    LocalizationFailure,
)
from .ulenses_localizer import (
    GridLocalizationResult,
    GridLocalizationError,
    localize_grid,
    search_missing_grid_foci,
)

__all__ = ["ulenses_grid_strategy_spec"]


def _normalize(parameters: Mapping[str, Any]) -> dict[str, Any]:
    supplied = dict(parameters or {})
    # Compatibility aliases from the first ImControl grid-localizer passes.
    aliases = {
        "spot_threshold_rel": "grid_threshold_rel",
        "spot_blur_sigma": "grid_gaussian_sigma",
        "spot_min_distance_fraction": "grid_min_distance_fraction",
        "focus_search_window_px": "focus_refine_radius",
    }
    normalized = {
        "use_ulenses_widget_guesses": bool(supplied.get("use_ulenses_widget_guesses", True)),
        "xp_guess_px": float(supplied.get("xp_guess_px", 10.0)),
        "yp_guess_px": float(supplied.get("yp_guess_px", 10.0)),
        "grid_size_mode": str(supplied.get("grid_size_mode", "auto")),
        "n_cols": int(supplied.get("n_cols", 10)),
        "n_rows": int(supplied.get("n_rows", 10)),
        "stagger_mode": str(supplied.get("stagger_mode", "auto")),
        "stagger_axis": str(supplied.get("stagger_axis", "auto")),
        "stagger_fraction": float(supplied.get("stagger_fraction", 0.5)),
        "max_relative_tilt_deg": float(supplied.get("max_relative_tilt_deg", 30.0)),
    }
    for name, default in LOCALIZATION_DEFAULTS.items():
        legacy = aliases.get(name)
        raw = supplied.get(name, supplied.get(legacy, default) if legacy else default)
        value_type = type(default)
        normalized[name] = value_type(raw)

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
    if not 0.0 <= normalized["stagger_fraction"] <= 1.0:
        raise ValueError("stagger_fraction must be within [0, 1]")
    if normalized["stagger_mode"] == "manual" and normalized["stagger_axis"] == "auto":
        raise ValueError("manual stagger requires rows or columns")
    if not 0.0 < normalized["max_relative_tilt_deg"] <= 45.0:
        raise ValueError("max_relative_tilt_deg must be within (0, 45]")
    return normalized


def _backend_parameter_specs() -> dict[str, ParameterSpec]:
    specs: dict[str, ParameterSpec] = {}
    for name, default in LOCALIZATION_DEFAULTS.items():
        info = dict(LOCALIZATION_PARAMETER_INFO[name])
        specs[name] = ParameterSpec(
            default,
            type(default),
            info.pop("label"),
            info.pop("tooltip", ""),
            minimum=info.pop("minimum", None),
            maximum=info.pop("maximum", None),
            step=info.pop("step", None),
            decimals=info.pop("decimals", None),
            choices=tuple(info.pop("choices")) if "choices" in info else None,
            advanced=bool(info.pop("advanced", False)),
            group=info.pop("group", None),
        )
    return specs

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


def _localization_failure(error: GridLocalizationError) -> LocalizationFailure:
    metadata = dict(error.diagnostics)
    if error.crop_coord is not None:
        metadata["crop_coord"] = tuple(int(v) for v in error.crop_coord)
    return LocalizationFailure(
        str(error),
        diagnostics=LocalizationDiagnostics(
            detected_points=error.detected_points_rc,
            metadata=metadata,
        ),
    )


def _localized_result_from_grid(
    image: np.ndarray, grid: GridLocalizationResult, params: Mapping[str, Any],
    *, xp_guess: float, yp_guess: float,
) -> ImageLocalizationResult:
    matched = np.asarray(grid.matched_mask, dtype=bool)
    # The shared SLMcore-compatible detector already supplies the subpixel
    # measured coordinates used by registration. Reuse those directly so
    # Transform and uLenses are inspecting the same localization evidence.
    refined = np.asarray(grid.measured_points_rc[matched], dtype=np.float64)
    measured_indices = np.asarray(grid.logical_indices[matched], dtype=np.int64)
    metadata = _grid_localization_dict(grid)
    metadata.update({
        "x_period_guess_px": float(xp_guess),
        "y_period_guess_px": float(yp_guess),
        "grid_size_mode": params["grid_size_mode"],
        "matched_count": int(grid.matched_count),
        "missing_count": int(grid.missing_count),
        "spot_center_method": params["focus_localization_method"],
        "spot_center_window_px": params["focus_search_window_px"],
    })
    return ImageLocalizationResult(
        points=refined,
        metadata=metadata,
        diagnostics=LocalizationDiagnostics(
            detected_points=grid.detected_points_rc,
            predicted_points=grid.grid_points_rc,
            refined_points=refined,
            missing_points=grid.missing_points_rc,
            logical_indices=measured_indices,
            metadata=dict(grid.diagnostics),
        ),
        payload=grid,
    )


def ulenses_grid_strategy_spec(
    guess_provider: Callable[[], tuple[float, float] | None] | None = None,
) -> CorrespondenceStrategySpec:
    """Build the tilted/stagger-aware finite-grid correspondence strategy.

    Moving and Reference are localized independently.  Each image may therefore
    use different detection/refinement parameters while correspondence is still
    established from the common finite logical grid.
    """

    def localize(
        image: np.ndarray,
        parameters: Mapping[str, Any],
    ) -> ImageLocalizationResult:
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

        try:
            backend_parameters = {
                name: params[name] for name in LOCALIZATION_DEFAULTS
            }
            grid = localize_grid(
                image,
                xp_guess=xp_guess,
                yp_guess=yp_guess,
                n_cols=n_cols,
                n_rows=n_rows,
                **backend_parameters,
                stagger_mode=params["stagger_mode"],
                stagger_axis=params["stagger_axis"],
                stagger_fraction=params["stagger_fraction"],
            )
        except GridLocalizationError as error:
            raise _localization_failure(error) from error
        return _localized_result_from_grid(
            image, grid, params, xp_guess=xp_guess, yp_guess=yp_guess
        )

    def recover_missing(
        image: np.ndarray,
        current: ImageLocalizationResult,
        parameters: Mapping[str, Any],
    ) -> ImageLocalizationResult:
        params = _normalize(parameters)
        grid = current.payload
        if not isinstance(grid, GridLocalizationResult):
            raise TypeError("uLenses missing-site recovery requires a grid payload")
        xp_guess = float(current.metadata.get("x_period_guess_px", params["xp_guess_px"]))
        yp_guess = float(current.metadata.get("y_period_guess_px", params["yp_guess_px"]))
        recovered = search_missing_grid_foci(
            image,
            grid,
            search_radius_px=params["focus_search_window_px"],
            gaussian_sigma=params["spot_blur_sigma"],
            threshold_rel=max(0.01, 0.5 * params["spot_threshold_rel"]),
        )
        return _localized_result_from_grid(
            image, recovered, params, xp_guess=xp_guess, yp_guess=yp_guess
        )

    def match(
        source: ImageLocalizationResult,
        target: ImageLocalizationResult,
        source_parameters: Mapping[str, Any],
        target_parameters: Mapping[str, Any],
    ) -> CorrespondenceResult:
        source_grid = source.payload
        target_grid = target.payload
        if not isinstance(source_grid, GridLocalizationResult) or not isinstance(
            target_grid, GridLocalizationResult
        ):
            raise TypeError("uLenses correspondence requires grid-localization payloads")
        _check_same_finite_grid(source_grid, target_grid)

        tilt = abs(
            _wrapped_grid_angle_difference(source_grid.angle_deg, target_grid.angle_deg)
        )
        tilt_limit = min(
            float(source_parameters["max_relative_tilt_deg"]),
            float(target_parameters["max_relative_tilt_deg"]),
        )
        if tilt > tilt_limit:
            raise ValueError(
                f"the two localized grids differ in orientation by {tilt:.1f} degrees, "
                f"past the {tilt_limit:.1f} degree correspondence limit. "
                "The finite grid axes may be transposed."
            )
        source_indices = (
            None if source.diagnostics is None else source.diagnostics.logical_indices
        )
        target_indices = (
            None if target.diagnostics is None else target.diagnostics.logical_indices
        )
        if source_indices is None or target_indices is None:
            raise RuntimeError("uLenses localization is missing logical focus indices")
        source_lookup = {tuple(map(int, index)): pos for pos, index in enumerate(source_indices)}
        target_lookup = {tuple(map(int, index)): pos for pos, index in enumerate(target_indices)}
        common = sorted(set(source_lookup) & set(target_lookup), key=lambda item: (item[1], item[0]))
        if len(common) < 3:
            raise RuntimeError(
                "fewer than three measured grid sites are shared by Moving and Reference"
            )
        source_points = np.asarray([source.points[source_lookup[index]] for index in common])
        target_points = np.asarray([target.points[target_lookup[index]] for index in common])

        return CorrespondenceResult(
            source_points=source_points,
            target_points=target_points,
            metadata={
                "n_rows": int(source_grid.n_rows),
                "n_cols": int(source_grid.n_cols),
                "relative_tilt_deg": float(tilt),
                "measured_pair_count": int(len(common)),
                "moving_missing_count": int(source_grid.missing_count),
                "reference_missing_count": int(target_grid.missing_count),
                "grid_localization": {
                    "moving": dict(source.metadata),
                    "reference": dict(target.metadata),
                },
                "spot_refinement": {
                    "moving": {
                        "method": source_parameters["focus_localization_method"],
                        "window_px": source_parameters["focus_search_window_px"],
                    },
                    "reference": {
                        "method": target_parameters["focus_localization_method"],
                        "window_px": target_parameters["focus_search_window_px"],
                    },
                },
            },
            source_localization=source,
            target_localization=target,
        )

    return CorrespondenceStrategySpec(
        name="ulenses_grid",
        label="uLenses grid",
        description=(
            "Localize a finite 2-D grid with the SLMcore-compatible crop, spot "
            "detection, stagger inference and robust registration pipeline, then "
            "pair measured foci by logical index."
        ),
        localize_handler=localize,
        match_handler=match,
        recovery_handler=recover_missing,
        parameter_specs={
            "use_ulenses_widget_guesses": ParameterSpec(
                True, bool, "Use uLenses widget period guesses",
                "Use the current uLenses widget X/Y periodicities as SLMcore-style "
                "period guidance. Disable this to use the explicit guesses below.",
                group="Grid geometry",
            ),
            "xp_guess_px": ParameterSpec(
                10.0, float, "Expected period X (camera px)",
                "Expected camera-space length of the logical X lattice period.",
                minimum=0.001, step=0.5, decimals=3, group="Grid geometry",
                enabled_when=("use_ulenses_widget_guesses", False),
            ),
            "yp_guess_px": ParameterSpec(
                10.0, float, "Expected period Y (camera px)",
                "Expected camera-space length of the logical Y lattice period.",
                minimum=0.001, step=0.5, decimals=3, group="Grid geometry",
                enabled_when=("use_ulenses_widget_guesses", False),
            ),
            "grid_size_mode": ParameterSpec(
                "auto", str, "Lattice size source",
                "Auto infers finite X/Y point counts from the detected lattice; "
                "Manual imposes the counts below.",
                choices=("auto", "manual"), group="Grid geometry",
            ),
            "n_cols": ParameterSpec(
                10, int, "Lattice points X",
                "Number of finite target sites along the logical X lattice axis.",
                minimum=2, step=1, group="Grid geometry",
                enabled_when=("grid_size_mode", "manual"),
            ),
            "n_rows": ParameterSpec(
                10, int, "Lattice points Y",
                "Number of finite target sites along the logical Y lattice axis.",
                minimum=2, step=1, group="Grid geometry",
                enabled_when=("grid_size_mode", "manual"),
            ),
            "stagger_mode": ParameterSpec(
                "auto", str, "Stagger source",
                "Auto infers alternating stagger from the image. None forces zero "
                "stagger. Manual imposes the value and axis below.",
                choices=("auto", "none", "manual"), group="Grid geometry",
            ),
            "stagger_axis": ParameterSpec(
                "auto", str, "Stagger axis",
                "Rows matches SLMcore's canonical alternating-row model; Columns "
                "uses the same backend on a transposed image for compatibility.",
                choices=("auto", "rows", "columns"), group="Grid geometry",
                enabled_when=("stagger_mode", "manual"),
            ),
            "stagger_fraction": ParameterSpec(
                0.5, float, "Stagger",
                "Odd-row/column shift as a fraction of the corresponding lattice period.",
                minimum=0.0, maximum=1.0, step=0.05, decimals=3,
                group="Grid geometry", enabled_when=("stagger_mode", "manual"),
            ),
            **_backend_parameter_specs(),
            "max_relative_tilt_deg": ParameterSpec(
                30.0, float, "Maximum relative grid tilt (deg)",
                "Safety limit between Moving and Reference grid orientations.",
                minimum=0.1, maximum=45.0, step=1.0, decimals=1, advanced=True,
                group="Correspondence",
            ),
        },
        normalize_parameters=_normalize,
    )
