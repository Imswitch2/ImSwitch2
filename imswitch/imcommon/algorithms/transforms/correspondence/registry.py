"""Pluggable image localization and correspondence strategies.

A transform calibration has two independent choices: how image features are
localized/matched, and which transform model is fitted to those points.  The
single-image localization seam is optional so older correspondence handlers can
keep running unchanged, while richer strategies can localize Moving and
Reference independently and only pair them afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import numpy as np

from ..parameters import ParameterSpec, normalize_parameter_values, parameter_defaults
from .foci_grid import detect_spot_centers, estimate_grid_angle, order_grid_points

__all__ = [
    "LocalizationDiagnostics",
    "LocalizationFailure",
    "ImageLocalizationResult",
    "CorrespondenceResult",
    "CorrespondenceStrategySpec",
    "builtin_correspondence_specs",
]


def _points(value: Any, *, label: str, allow_empty: bool = True) -> np.ndarray:
    array = np.asarray(value, dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != 2:
        raise ValueError(f"{label} must have shape (N, 2), got {array.shape}")
    if not allow_empty and len(array) == 0:
        raise ValueError(f"{label} cannot be empty")
    return array


@dataclass(frozen=True)
class LocalizationDiagnostics:
    """Ephemeral point geometry used only for interactive inspection.

    These arrays deliberately do *not* belong to persisted transform provenance.
    They may be large and describe how localization was obtained, not the stable
    transform resource itself.
    """

    detected_points: np.ndarray | None = None
    predicted_points: np.ndarray | None = None
    refined_points: np.ndarray | None = None
    missing_points: np.ndarray | None = None
    logical_indices: np.ndarray | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("detected_points", "predicted_points", "refined_points", "missing_points"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _points(value, label=name))
        if self.logical_indices is not None:
            indices = np.asarray(self.logical_indices, dtype=np.int64)
            if indices.ndim != 2 or indices.shape[1] != 2:
                raise ValueError(
                    f"logical_indices must have shape (N, 2), got {indices.shape}"
                )
            object.__setattr__(self, "logical_indices", indices)


class LocalizationFailure(RuntimeError):
    """Localization error carrying ephemeral evidence for inspection.

    The diagnostics are intentionally runtime-only. They let a tuning UI show
    the crop/detections from a failed attempt without polluting saved transform
    provenance.
    """

    def __init__(self, message: str, diagnostics: LocalizationDiagnostics | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics


@dataclass(frozen=True)
class ImageLocalizationResult:
    """One image localized independently from any transform correspondence."""

    points: np.ndarray
    metadata: Mapping[str, Any] = field(default_factory=dict)
    diagnostics: LocalizationDiagnostics | None = field(default=None, compare=False)
    payload: Any = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "points", _points(self.points, label="points"))

    @property
    def point_count(self) -> int:
        return int(len(self.points))

    def summary(self) -> str:
        rows = self.metadata.get("n_rows")
        cols = self.metadata.get("n_cols")
        angle = self.metadata.get("angle_deg")
        stagger_axis = self.metadata.get("stagger_axis")
        stagger = self.metadata.get("stagger_fraction")
        parts: list[str] = []
        if rows is not None and cols is not None:
            parts.append(f"{int(rows)}x{int(cols)}")
            missing = int(self.metadata.get("missing_count", 0) or 0)
            matched = self.metadata.get("matched_count")
            if missing and matched is not None:
                parts.append(f"{int(matched)}/{int(rows) * int(cols)} measured")
        else:
            parts.append(f"{self.point_count} points")
        if angle is not None:
            parts.append(f"tilt {float(angle):.1f}°")
        if stagger_axis and stagger is not None and abs(float(stagger)) >= 0.05:
            parts.append(f"{stagger_axis} stagger {float(stagger):.2f}")
        return " · ".join(parts)


@dataclass(frozen=True)
class CorrespondenceResult:
    """Paired points plus persistable metadata and optional runtime evidence."""

    source_points: np.ndarray
    target_points: np.ndarray
    metadata: Mapping[str, Any] = field(default_factory=dict)
    source_localization: ImageLocalizationResult | None = field(
        default=None, compare=False, repr=False
    )
    target_localization: ImageLocalizationResult | None = field(
        default=None, compare=False, repr=False
    )

    def __post_init__(self) -> None:
        source = _points(self.source_points, label="source_points")
        target = _points(self.target_points, label="target_points")
        if target.shape != source.shape:
            raise ValueError(
                "target_points must have the same shape as source_points, got "
                f"{target.shape} and {source.shape}"
            )
        object.__setattr__(self, "source_points", source)
        object.__setattr__(self, "target_points", target)

    @property
    def pair_count(self) -> int:
        return int(self.source_points.shape[0])

    def summary(self) -> str:
        rows = self.metadata.get("n_rows")
        cols = self.metadata.get("n_cols")
        if rows is not None and cols is not None:
            return f"{self.pair_count} paired points · {int(rows)}x{int(cols)} grid"
        return f"{self.pair_count} paired points"


CorrespondenceHandler = Callable[
    [np.ndarray, np.ndarray, Mapping[str, Any]], CorrespondenceResult
]
LocalizationHandler = Callable[
    [np.ndarray, Mapping[str, Any]], ImageLocalizationResult
]
LocalizationMatcher = Callable[
    [ImageLocalizationResult, ImageLocalizationResult, Mapping[str, Any], Mapping[str, Any]],
    CorrespondenceResult,
]
LocalizationRecovery = Callable[
    [np.ndarray, ImageLocalizationResult, Mapping[str, Any]], ImageLocalizationResult
]
ParameterNormalizer = Callable[[Mapping[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class CorrespondenceStrategySpec:
    """One selectable localization/correspondence implementation."""

    name: str
    label: str
    description: str
    handler: CorrespondenceHandler | None = None
    parameter_specs: Mapping[str, ParameterSpec] = field(default_factory=dict)
    default_parameters: Mapping[str, Any] = field(default_factory=dict)
    normalize_parameters: ParameterNormalizer | None = None
    localize_handler: LocalizationHandler | None = None
    match_handler: LocalizationMatcher | None = None
    recovery_handler: LocalizationRecovery | None = None

    def __post_init__(self) -> None:
        if not str(self.name).strip():
            raise ValueError("correspondence strategy name cannot be empty")
        if self.handler is None and (
            self.localize_handler is None or self.match_handler is None
        ):
            raise ValueError(
                "strategy must provide either handler or both localize_handler/match_handler"
            )
        defaults = dict(self.default_parameters)
        for name, value in parameter_defaults(self.parameter_specs).items():
            defaults.setdefault(name, value)
        object.__setattr__(self, "default_parameters", defaults)

    @property
    def supports_independent_localization(self) -> bool:
        return self.localize_handler is not None and self.match_handler is not None

    @property
    def supports_missing_recovery(self) -> bool:
        return self.recovery_handler is not None

    def normalize(self, parameters: Mapping[str, Any] | None) -> dict[str, Any]:
        supplied = dict(self.default_parameters)
        supplied.update(dict(parameters or {}))
        if self.parameter_specs:
            supplied = normalize_parameter_values(self.parameter_specs, supplied)
        if self.normalize_parameters is not None:
            supplied = self.normalize_parameters(supplied)
        return supplied

    def localize(
        self, image: np.ndarray, parameters: Mapping[str, Any]
    ) -> ImageLocalizationResult:
        if self.localize_handler is None:
            raise RuntimeError(
                f"{self.label} does not support independent single-image localization"
            )
        result = self.localize_handler(image, self.normalize(parameters))
        if not isinstance(result, ImageLocalizationResult):
            raise TypeError(
                f"localization strategy {self.name!r} returned an invalid result"
            )
        return result


    def recover(
        self,
        image: np.ndarray,
        result: ImageLocalizationResult,
        parameters: Mapping[str, Any],
    ) -> ImageLocalizationResult:
        if self.recovery_handler is None:
            raise RuntimeError(f"{self.label} does not support missing-site recovery")
        recovered = self.recovery_handler(image, result, self.normalize(parameters))
        if not isinstance(recovered, ImageLocalizationResult):
            raise TypeError(
                f"localization strategy {self.name!r} returned an invalid recovery result"
            )
        return recovered

    def correspond(
        self,
        source: ImageLocalizationResult,
        target: ImageLocalizationResult,
        source_parameters: Mapping[str, Any],
        target_parameters: Mapping[str, Any] | None = None,
    ) -> CorrespondenceResult:
        if self.match_handler is None:
            raise RuntimeError(
                f"{self.label} does not support correspondence from independent localizations"
            )
        source_params = self.normalize(source_parameters)
        target_params = self.normalize(
            source_parameters if target_parameters is None else target_parameters
        )
        result = self.match_handler(source, target, source_params, target_params)
        if not isinstance(result, CorrespondenceResult):
            raise TypeError(f"strategy {self.name!r} returned an invalid correspondence")
        return result

    def run(
        self,
        source_image: np.ndarray,
        target_image: np.ndarray,
        parameters: Mapping[str, Any],
        target_parameters: Mapping[str, Any] | None = None,
    ) -> CorrespondenceResult:
        if self.supports_independent_localization:
            source_params = self.normalize(parameters)
            target_params = self.normalize(
                parameters if target_parameters is None else target_parameters
            )
            source = self.localize(source_image, source_params)
            target = self.localize(target_image, target_params)
            return self.correspond(source, target, source_params, target_params)
        if self.handler is None:
            raise RuntimeError(f"strategy {self.label} has no correspondence handler")
        if target_parameters is not None and self.normalize(target_parameters) != self.normalize(parameters):
            raise ValueError(
                f"{self.label} cannot use different Moving/Reference parameters"
            )
        result = self.handler(source_image, target_image, self.normalize(parameters))
        if not isinstance(result, CorrespondenceResult):
            try:
                source_points, target_points = result  # type: ignore[misc]
            except Exception as error:
                raise TypeError(
                    f"correspondence strategy {self.name!r} returned an invalid result"
                ) from error
            result = CorrespondenceResult(source_points, target_points)
        return result


def _normalize_foci_parameters(parameters: Mapping[str, Any]) -> dict[str, Any]:
    normalized = {
        "n_rows": int(parameters["n_rows"]),
        "n_cols": int(parameters["n_cols"]),
        "min_distance": int(parameters["min_distance"]),
        "threshold_rel": float(parameters["threshold_rel"]),
        "refine_radius": int(parameters["refine_radius"]),
        "gaussian_sigma": float(parameters["gaussian_sigma"]),
        "max_relative_tilt_deg": float(parameters["max_relative_tilt_deg"]),
    }
    if normalized["n_rows"] < 1 or normalized["n_cols"] < 1:
        raise ValueError("n_rows and n_cols must be at least 1")
    if normalized["min_distance"] < 1:
        raise ValueError("min_distance must be at least 1")
    if not 0.0 <= normalized["threshold_rel"] <= 1.0:
        raise ValueError("threshold_rel must be between 0 and 1")
    if normalized["refine_radius"] < 0:
        raise ValueError("refine_radius must be at least 0")
    if normalized["gaussian_sigma"] < 0:
        raise ValueError("gaussian_sigma must be at least 0")
    if not 0.0 < normalized["max_relative_tilt_deg"] <= 45.0:
        raise ValueError("max_relative_tilt_deg must be within (0, 45]")
    return normalized


def _localize_foci_grid(
    image: np.ndarray, parameters: Mapping[str, Any]
) -> ImageLocalizationResult:
    params = _normalize_foci_parameters(parameters)
    expected = params["n_rows"] * params["n_cols"]
    detected = detect_spot_centers(
        image,
        expected_n=expected,
        min_distance=params["min_distance"],
        threshold_rel=params["threshold_rel"],
        refine_radius=params["refine_radius"],
        gaussian_sigma=params["gaussian_sigma"],
    )
    ordered = order_grid_points(detected, params["n_rows"], params["n_cols"])
    angle = 0.0 if expected <= 1 else float(np.degrees(estimate_grid_angle(ordered)))
    return ImageLocalizationResult(
        points=ordered,
        metadata={
            "n_rows": params["n_rows"],
            "n_cols": params["n_cols"],
            "angle_deg": angle,
        },
        diagnostics=LocalizationDiagnostics(
            detected_points=detected,
            refined_points=ordered,
        ),
    )


def _match_foci_grid(
    source: ImageLocalizationResult,
    target: ImageLocalizationResult,
    source_parameters: Mapping[str, Any],
    target_parameters: Mapping[str, Any],
) -> CorrespondenceResult:
    source_rows = int(source.metadata["n_rows"])
    source_cols = int(source.metadata["n_cols"])
    target_rows = int(target.metadata["n_rows"])
    target_cols = int(target.metadata["n_cols"])
    if (source_rows, source_cols) != (target_rows, target_cols):
        raise ValueError(
            "moving and reference Foci Grid sizes differ: "
            f"{source_rows}x{source_cols} vs {target_rows}x{target_cols}"
        )
    difference = float(target.metadata.get("angle_deg", 0.0)) - float(
        source.metadata.get("angle_deg", 0.0)
    )
    difference = (difference + 45.0) % 90.0 - 45.0
    limit = min(
        float(source_parameters["max_relative_tilt_deg"]),
        float(target_parameters["max_relative_tilt_deg"]),
    )
    if abs(difference) > limit:
        raise ValueError(
            f"the two grids are tilted {abs(difference):.1f} degrees apart, "
            f"past the {limit:.1f} degree correspondence limit"
        )
    return CorrespondenceResult(
        source.points,
        target.points,
        metadata={
            "n_rows": source_rows,
            "n_cols": source_cols,
            "relative_tilt_deg": abs(difference),
        },
        source_localization=source,
        target_localization=target,
    )


def builtin_correspondence_specs() -> tuple[CorrespondenceStrategySpec, ...]:
    """Strategies that can run in :mod:`imcommon` without host dependencies."""
    specs = {
        "n_rows": ParameterSpec(
            10, int, "Grid rows",
            "Number of focus rows expected in this image.",
            minimum=1, step=1, group="Grid geometry",
        ),
        "n_cols": ParameterSpec(
            10, int, "Grid columns",
            "Number of focus columns expected in this image.",
            minimum=1, step=1, group="Grid geometry",
        ),
        "min_distance": ParameterSpec(
            10, int, "Minimum focus distance (px)",
            "Minimum separation used by peak detection. Increase it when one focus "
            "is detected more than once; decrease it when neighbours are merged.",
            minimum=1, step=1, group="Focus detection",
        ),
        "threshold_rel": ParameterSpec(
            0.2, float, "Focus threshold",
            "Relative peak-intensity threshold. Increase it to reject weak false "
            "detections; decrease it to include dim foci.",
            minimum=0.0, maximum=1.0, step=0.02, decimals=2,
            group="Focus detection",
        ),
        "refine_radius": ParameterSpec(
            5, int, "Center refinement radius (px)",
            "Radius used for sub-pixel center-of-mass refinement.",
            minimum=0, step=1, advanced=True, group="Focus refinement",
        ),
        "gaussian_sigma": ParameterSpec(
            1.0, float, "Detection smoothing (px)",
            "Gaussian smoothing applied before peak detection.",
            minimum=0.0, step=0.1, decimals=2, advanced=True,
            group="Focus detection",
        ),
        "max_relative_tilt_deg": ParameterSpec(
            30.0, float, "Maximum relative grid tilt (deg)",
            "Safety limit on the orientation difference used when pairing the two grids.",
            minimum=0.1, maximum=45.0, step=1.0, decimals=1,
            advanced=True, group="Correspondence",
        ),
    }
    return (
        CorrespondenceStrategySpec(
            name="foci_grid",
            label="Foci grid",
            description=(
                "Detect a known rectangular grid of bright foci independently in "
                "Moving and Reference, order each grid consistently, and pair sites."
            ),
            parameter_specs=specs,
            normalize_parameters=_normalize_foci_parameters,
            localize_handler=_localize_foci_grid,
            match_handler=_match_foci_grid,
        ),
    )
