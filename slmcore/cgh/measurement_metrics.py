"""Experimental intensity analysis derived from localized image measurements."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any,Mapping

import numpy as np

from ..measurement import ImageMeasurement
from ..engine.parameters import EditorKind,ParamSpec
from .intensity_metrics import evaluate_relative_intensity_statistics
from .localization import LocalizationResult
from .targets.resolution import TargetResolution


INTENSITY_ANALYSIS_PARAMS = MappingProxyType({
    "integration_size_px":ParamSpec(
        5,int,min_value=1,editor=EditorKind.SPIN_BOX,
        label="Integration size (px)",
        tooltip="Square integration window centered on each localized focus.",
    ),
})

_DEFAULT_INTEGRATION_SIZE_PX = int(
    INTENSITY_ANALYSIS_PARAMS["integration_size_px"].default
)


@dataclass(frozen=True)
class IntensityAnalysis:
    """Authoritative integrated spot powers and measured intensity metrics."""

    geometry_type: str
    parameters: Mapping[str,Any]
    spot_powers: np.ndarray
    efficiency: float
    uniformity: float
    normalized_std: float
    integration_preview: np.ndarray
    matched_count: int = 0
    total_count: int = 0

    def __post_init__(self) -> None:
        parameters = MappingProxyType(dict(self.parameters or {}))
        powers = _freeze_array(self.spot_powers,"spot_powers",1)
        preview = _freeze_array(self.integration_preview,"integration_preview",2)
        if np.any(powers < 0):
            raise ValueError("spot_powers cannot contain negative values")
        object.__setattr__(self,"geometry_type",str(self.geometry_type or "unknown"))
        object.__setattr__(self,"parameters",parameters)
        object.__setattr__(self,"spot_powers",powers)
        object.__setattr__(self,"integration_preview",preview)
        efficiency = float(self.efficiency)
        uniformity = float(self.uniformity)
        normalized_std = float(self.normalized_std)
        if not all(np.isfinite(value) for value in (
            efficiency,uniformity,normalized_std,
        )):
            raise ValueError("Intensity analysis metrics contain non-finite values")
        matched_count = int(self.matched_count)
        total_count = int(self.total_count)
        if matched_count != powers.size:
            raise ValueError("matched_count must equal the number of spot powers")
        if total_count < matched_count:
            raise ValueError("total_count cannot be smaller than matched_count")
        object.__setattr__(self,"efficiency",efficiency)
        object.__setattr__(self,"uniformity",uniformity)
        object.__setattr__(self,"normalized_std",normalized_std)
        object.__setattr__(self,"matched_count",matched_count)
        object.__setattr__(self,"total_count",total_count)

    @property
    def values(self) -> Mapping[str,float]:
        return MappingProxyType({
            "uniformity":self.uniformity,
            "efficiency":self.efficiency,
            "normalized_std":self.normalized_std,
        })


@dataclass(frozen=True)
class MeasurementMetrics:
    """Compatibility view of measured metrics without spot-analysis payloads."""

    geometry_type: str
    values: Mapping[str,float]
    matched_count: int = 0
    total_count: int = 0

    def __post_init__(self) -> None:
        values = {str(key):float(value) for key,value in dict(self.values).items()}
        if not all(np.isfinite(value) for value in values.values()):
            raise ValueError("Measurement metrics contain non-finite values")
        object.__setattr__(self,"geometry_type",str(self.geometry_type or "unknown"))
        object.__setattr__(self,"values",MappingProxyType(values))
        object.__setattr__(self,"matched_count",int(self.matched_count))
        object.__setattr__(self,"total_count",int(self.total_count))

    @classmethod
    def from_analysis(cls,analysis: IntensityAnalysis) -> "MeasurementMetrics":
        return cls(
            geometry_type=analysis.geometry_type,
            values={
                "uniformity":analysis.uniformity,
                "efficiency":analysis.efficiency,
                "normalized_std":analysis.normalized_std,
            },
            matched_count=analysis.matched_count,
            total_count=analysis.total_count,
        )


def analyze_measurement_intensity(
    measurement: ImageMeasurement,
    localization: LocalizationResult,
    resolution: TargetResolution,
    parameters: Mapping[str,Any],
) -> IntensityAnalysis:
    """Integrate localized spot powers and evaluate experimental metrics once."""
    geometry = resolution.geometry
    geometry_type = (
        "unknown" if geometry is None
        else str(getattr(geometry,"geometry_type","unknown") or "unknown")
    )
    if geometry_type != "lattice":
        raise RuntimeError(
            "Experimental intensity analysis is currently defined for lattice targets"
        )

    diagnostics = dict(localization.diagnostics or {})
    measurement_id = diagnostics.get("measurement_id")
    if (
        measurement_id is not None
        and str(measurement_id) != str(measurement.measurement_id)
    ):
        raise RuntimeError("Localization belongs to a different measurement")

    image = np.asarray(localization.cropped_image,dtype=np.float64)
    positions = np.asarray(localization.measured_positions_px,dtype=np.float64)
    if positions.ndim != 2 or positions.shape[0] != 2:
        raise ValueError("Localized positions must have shape (2, N)")

    total_count = int(positions.shape[1])
    matched = diagnostics.get("matched_mask")
    if matched is None:
        matched_mask = np.ones(total_count,dtype=bool)
    else:
        matched_mask = np.asarray(matched,dtype=bool)
        if matched_mask.shape != (total_count,):
            raise RuntimeError("Localization matched-mask shape is inconsistent")

    positions = positions[:,matched_mask]
    matched_count = int(positions.shape[1])
    if matched_count <= 0:
        raise RuntimeError("No localized lattice spots are available for metrics")

    integration_size = int(parameters["integration_size_px"])
    if integration_size <= 0:
        raise ValueError("integration_size_px must be > 0")

    preview = np.array(image,dtype=np.float64,copy=True)
    powers = np.empty(matched_count,dtype=np.float64)
    for index in range(matched_count):
        powers[index] = _sum_around(
            image,
            positions[0,index],
            positions[1,index],
            integration_size,
        )
        _zero_around(
            preview,
            positions[0,index],
            positions[1,index],
            integration_size,
        )

    if not np.any(powers > 0):
        raise ValueError("No positive lattice-spot power was measured")

    image_total = float(np.sum(image))
    efficiency = float(np.sum(powers)) / image_total if image_total > 0 else 0.0
    statistics = evaluate_relative_intensity_statistics(
        powers,np.ones_like(powers),
    )
    return IntensityAnalysis(
        geometry_type=geometry_type,
        parameters=dict(parameters),
        spot_powers=powers,
        efficiency=efficiency,
        uniformity=statistics.uniformity,
        normalized_std=statistics.normalized_std,
        integration_preview=preview,
        matched_count=matched_count,
        total_count=total_count,
    )


def analyze_measurement_metrics(
    measurement: ImageMeasurement,
    localization: LocalizationResult,
    resolution: TargetResolution,
    parameters: Mapping[str, Any] | None=None,
) -> MeasurementMetrics:
    """Compatibility wrapper returning the compact metrics view."""
    geometry = resolution.geometry
    geometry_type = (
        "unknown" if geometry is None
        else str(getattr(geometry,"geometry_type","unknown") or "unknown")
    )
    if geometry_type != "lattice":
        return MeasurementMetrics(geometry_type=geometry_type,values={})
    analysis = analyze_measurement_intensity(
        measurement,
        localization,
        resolution,
        {"integration_size_px":_DEFAULT_INTEGRATION_SIZE_PX}
        if parameters is None else parameters,
    )
    return MeasurementMetrics.from_analysis(analysis)


def _window_bounds(
    image: np.ndarray,coord_x: float,coord_y: float,window_size: int,
):
    height,width = image.shape
    size = int(window_size)
    half = size // 2
    x0 = int(round(float(coord_x))) - half
    y0 = int(round(float(coord_y))) - half
    x1 = x0 + size
    y1 = y0 + size
    return max(0,x0),max(0,y0),min(width,x1),min(height,y1)


def _sum_around(
    image: np.ndarray,coord_x: float,coord_y: float,window_size: int,
) -> float:
    x0,y0,x1,y1 = _window_bounds(image,coord_x,coord_y,window_size)
    return float(np.sum(image[y0:y1,x0:x1]))


def _zero_around(
    image: np.ndarray,coord_x: float,coord_y: float,window_size: int,
) -> None:
    x0,y0,x1,y1 = _window_bounds(image,coord_x,coord_y,window_size)
    image[y0:y1,x0:x1] = 0.0


def _freeze_array(value: Any,name: str,ndim: int) -> np.ndarray:
    array = np.asarray(value,dtype=np.float64)
    if array.ndim != ndim:
        raise ValueError(f"{name} must be {ndim}D, got shape {array.shape}")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} contains non-finite values")
    array = np.array(array,copy=True)
    array.setflags(write=False)
    return array


__all__ = [
    "INTENSITY_ANALYSIS_PARAMS",
    "IntensityAnalysis",
    "MeasurementMetrics",
    "analyze_measurement_intensity",
    "analyze_measurement_metrics",
]
