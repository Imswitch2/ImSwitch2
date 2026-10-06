"""Pluggable image-to-point correspondence strategies.

A transform calibration has two independent choices: how corresponding points
are found, and which transform model is fitted to those points.  This module
keeps the first choice small and dependency-free so hosts such as ImControl can
register hardware/workflow-specific localizers without making :mod:`imcommon`
import those hosts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import numpy as np

from ..parameters import ParameterSpec, normalize_parameter_values, parameter_defaults
from .foci_grid import find_grid_correspondence

__all__ = [
    "CorrespondenceResult",
    "CorrespondenceStrategySpec",
    "builtin_correspondence_specs",
]


@dataclass(frozen=True)
class CorrespondenceResult:
    """Paired points plus strategy-specific diagnostic/provenance metadata."""

    source_points: np.ndarray
    target_points: np.ndarray
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        source = np.asarray(self.source_points, dtype=np.float64)
        target = np.asarray(self.target_points, dtype=np.float64)
        if source.ndim != 2 or source.shape[1] != 2:
            raise ValueError(
                f"source_points must have shape (N, 2), got {source.shape}"
            )
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


CorrespondenceHandler = Callable[[np.ndarray, np.ndarray, Mapping[str, Any]], CorrespondenceResult]
ParameterNormalizer = Callable[[Mapping[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class CorrespondenceStrategySpec:
    """One selectable way of obtaining paired points from two images.

    ``parameter_specs`` is the authoritative description of the localization
    controls. ``default_parameters`` remains available for compatibility with
    the first transform-calibration pass and host strategies written against
    it; when specs are supplied their defaults are merged automatically.
    """

    name: str
    label: str
    description: str
    handler: CorrespondenceHandler
    parameter_specs: Mapping[str, ParameterSpec] = field(default_factory=dict)
    default_parameters: Mapping[str, Any] = field(default_factory=dict)
    normalize_parameters: ParameterNormalizer | None = None

    def __post_init__(self) -> None:
        defaults = parameter_defaults(self.parameter_specs)
        defaults.update(dict(self.default_parameters))
        object.__setattr__(self, "default_parameters", defaults)

    def normalize(self, parameters: Mapping[str, Any]) -> dict[str, Any]:
        values = (
            normalize_parameter_values(self.parameter_specs, parameters)
            if self.parameter_specs
            else dict(parameters)
        )
        if self.normalize_parameters is not None:
            values = self.normalize_parameters(values)
        return values

    def run(
        self,
        source_image: np.ndarray,
        target_image: np.ndarray,
        parameters: Mapping[str, Any],
    ) -> CorrespondenceResult:
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


def _foci_grid_strategy(
    source_image: np.ndarray,
    target_image: np.ndarray,
    parameters: Mapping[str, Any],
) -> CorrespondenceResult:
    params = _normalize_foci_parameters(parameters)
    source_points, target_points = find_grid_correspondence(
        source_image,
        target_image,
        n_rows=params["n_rows"],
        n_cols=params["n_cols"],
        min_distance=params["min_distance"],
        threshold_rel=params["threshold_rel"],
        refine_radius=params["refine_radius"],
        gaussian_sigma=params["gaussian_sigma"],
        max_relative_tilt_deg=params["max_relative_tilt_deg"],
    )
    return CorrespondenceResult(
        source_points=source_points,
        target_points=target_points,
        metadata={
            "n_rows": params["n_rows"],
            "n_cols": params["n_cols"],
            "detection": {
                key: params[key]
                for key in (
                    "min_distance",
                    "threshold_rel",
                    "refine_radius",
                    "gaussian_sigma",
                )
            },
            "max_relative_tilt_deg": params["max_relative_tilt_deg"],
        },
    )


def builtin_correspondence_specs() -> tuple[CorrespondenceStrategySpec, ...]:
    """Strategies that can run in :mod:`imcommon` without host dependencies."""
    return (
        CorrespondenceStrategySpec(
            name="foci_grid",
            label="Foci grid",
            description=(
                "Detect a known rectangular grid of bright foci in both images, "
                "order the grids consistently, and pair corresponding sites."
            ),
            handler=_foci_grid_strategy,
            parameter_specs={
                "n_rows": ParameterSpec(
                    10, int, "Grid rows",
                    "Number of focus rows expected in each image. The current "
                    "grid matcher requires exactly rows × columns detections.",
                    minimum=1, step=1,
                ),
                "n_cols": ParameterSpec(
                    10, int, "Grid columns",
                    "Number of focus columns expected in each image.",
                    minimum=1, step=1,
                ),
                "min_distance": ParameterSpec(
                    10, int, "Minimum focus distance (px)",
                    "Minimum separation used by peak detection. Increase it when "
                    "one focus is detected more than once; decrease it when valid "
                    "neighbouring foci are being merged.",
                    minimum=1, step=1,
                ),
                "threshold_rel": ParameterSpec(
                    0.2, float, "Focus threshold",
                    "Relative peak-intensity threshold after normalization and "
                    "smoothing. Increase it to reject weak false detections; "
                    "decrease it to include dim foci.",
                    minimum=0.0, maximum=1.0, step=0.02, decimals=2,
                ),
                "refine_radius": ParameterSpec(
                    5, int, "Center refinement radius (px)",
                    "Radius of the local patch used for sub-pixel center-of-mass "
                    "refinement around each detected focus.",
                    minimum=0, step=1, advanced=True,
                ),
                "gaussian_sigma": ParameterSpec(
                    1.0, float, "Detection smoothing (px)",
                    "Gaussian smoothing applied before peak detection. Increase "
                    "for noisy images; too much smoothing can merge close foci.",
                    minimum=0.0, step=0.1, decimals=2, advanced=True,
                ),
                "max_relative_tilt_deg": ParameterSpec(
                    30.0, float, "Maximum relative grid tilt (deg)",
                    "Safety limit on the orientation difference between moving "
                    "and reference grids. Beyond this, row/column ordering can be "
                    "ambiguous and the current matcher refuses the correspondence.",
                    minimum=0.1, maximum=45.0, step=1.0, decimals=1,
                    advanced=True,
                ),
            },
            normalize_parameters=_normalize_foci_parameters,
        ),
    )
