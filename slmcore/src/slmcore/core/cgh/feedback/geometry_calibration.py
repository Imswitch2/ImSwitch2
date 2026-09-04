"""Persistent camera-to-target geometry-orientation calibration."""

from __future__ import annotations

from dataclasses import dataclass,field
from datetime import datetime
from types import MappingProxyType
from copy import deepcopy
from typing import Any,Mapping

import numpy as np

from .orientation import FeedbackOrientation,orientation_permutation
from ..localization import LocalizationResult
from ...measurement import ImageMeasurement


GEOMETRY_ORIENTATION_PROTOCOL = "geometry_orientation_v1"


@dataclass(frozen=True)
class GeometryOrientationCalibration:
    """One authoritative orientation calibration for an SLM/section/plane."""

    slm_serial: str
    section_key: str
    plane_name: str
    orientation: FeedbackOrientation | str
    detector_name: str | None = None
    grid_x: int = 5
    grid_y: int = 4
    period_x_px: float = 1.0
    period_y_px: float = 1.0
    protocol: str = GEOMETRY_ORIENTATION_PROTOCOL
    score: float = 0.0
    confidence_margin: float = 0.0
    matched_count: int = 0
    provenance: Mapping[str,Any] = field(default_factory=dict)
    created_at: str = ""

    def __post_init__(self) -> None:
        serial = str(self.slm_serial or "").strip()
        section = str(self.section_key or "").strip()
        plane = str(self.plane_name or "").strip()
        if not serial or not section or not plane:
            raise ValueError(
                "Geometry orientation calibration requires SLM serial, section and plane"
            )
        gx,gy = int(self.grid_x),int(self.grid_y)
        px,py = float(self.period_x_px),float(self.period_y_px)
        if gx < 2 or gy < 2:
            raise ValueError("Geometry calibration grid dimensions must be >= 2")
        if not np.isfinite(px) or not np.isfinite(py) or px <= 0 or py <= 0:
            raise ValueError("Geometry calibration periods must be finite and > 0")
        score = float(self.score)
        margin = float(self.confidence_margin)
        if not np.isfinite(score) or not np.isfinite(margin):
            raise ValueError("Geometry calibration scores must be finite")
        provenance = self.provenance or {}
        if not isinstance(provenance,Mapping):
            raise TypeError("provenance must be a mapping")
        object.__setattr__(self,"slm_serial",serial)
        object.__setattr__(self,"section_key",section)
        object.__setattr__(self,"plane_name",plane)
        object.__setattr__(self,"orientation",FeedbackOrientation.normalize(self.orientation))
        object.__setattr__(self,"detector_name",str(self.detector_name or "").strip() or None)
        object.__setattr__(self,"grid_x",gx)
        object.__setattr__(self,"grid_y",gy)
        object.__setattr__(self,"period_x_px",px)
        object.__setattr__(self,"period_y_px",py)
        object.__setattr__(self,"protocol",str(self.protocol or GEOMETRY_ORIENTATION_PROTOCOL))
        object.__setattr__(self,"score",score)
        object.__setattr__(self,"confidence_margin",margin)
        object.__setattr__(self,"matched_count",int(self.matched_count))
        object.__setattr__(self,"provenance",MappingProxyType(deepcopy(dict(provenance))))
        object.__setattr__(self,"created_at",str(self.created_at or datetime.now().isoformat()))

    def to_dict(self) -> dict[str,Any]:
        return {
            "slm_serial":self.slm_serial,
            "section_key":self.section_key,
            "plane_name":self.plane_name,
            "orientation":self.orientation.value,
            "detector_name":self.detector_name,
            "grid_x":self.grid_x,
            "grid_y":self.grid_y,
            "period_x_px":self.period_x_px,
            "period_y_px":self.period_y_px,
            "protocol":self.protocol,
            "score":self.score,
            "confidence_margin":self.confidence_margin,
            "matched_count":self.matched_count,
            "provenance":dict(self.provenance),
            "created_at":self.created_at,
        }

    @classmethod
    def from_dict(cls,data: Mapping[str,Any]) -> "GeometryOrientationCalibration":
        if not isinstance(data,Mapping):
            raise TypeError("Geometry orientation calibration must be a mapping")
        return cls(**dict(data))


@dataclass(frozen=True)
class GeometryOrientationAnalysis:
    orientation: FeedbackOrientation
    score: float
    second_score: float
    confidence_margin: float
    matched_count: int
    accepted: bool
    scores: Mapping[str,float]

    def __post_init__(self) -> None:
        object.__setattr__(self,"orientation",FeedbackOrientation.normalize(self.orientation))
        object.__setattr__(self,"score",float(self.score))
        object.__setattr__(self,"second_score",float(self.second_score))
        object.__setattr__(self,"confidence_margin",float(self.confidence_margin))
        object.__setattr__(self,"matched_count",int(self.matched_count))
        object.__setattr__(self,"accepted",bool(self.accepted))
        object.__setattr__(self,"scores",MappingProxyType({
            str(key):float(value) for key,value in dict(self.scores).items()
        }))


def geometry_orientation_code(lattice_indices: np.ndarray) -> np.ndarray:
    """Return the fixed v1 asymmetric relative-intensity code."""
    indices = np.asarray(lattice_indices)
    if indices.ndim != 2 or indices.shape[0] != 2:
        raise ValueError("lattice_indices must have shape (2, N)")
    x_values = np.unique(indices[0])
    y_values = np.unique(indices[1])
    if x_values.size * y_values.size != indices.shape[1]:
        raise ValueError("Geometry calibration requires a complete rectangular lattice")
    x_rank = {int(value):rank for rank,value in enumerate(x_values)}
    y_rank = {int(value):rank for rank,value in enumerate(y_values)}
    nx=max(1,int(x_values.size)-1); ny=max(1,int(y_values.size)-1)
    values=[]
    for x,y in zip(indices[0],indices[1]):
        xr=float(x_rank[int(x)])/nx
        yr=float(y_rank[int(y)])/ny
        # Smooth but deliberately non-separable code.  All eight D4 mappings
        # produce distinct rank patterns while keeping every focus measurable.
        value=0.30+0.28*xr+0.34*yr+0.08*xr*yr
        values.append(value)
    result=np.asarray(values,dtype=np.float64)
    result/=float(np.max(result))
    return result


def analyze_geometry_orientation(
    measurement: ImageMeasurement,
    localization: LocalizationResult,
    target_intensities: np.ndarray,
    *,
    integration_size_px: int=5,
    min_match_fraction: float=0.60,
    min_score: float=0.65,
    min_margin: float=0.12,
) -> GeometryOrientationAnalysis:
    """Score all eight discrete mappings from localized spot powers."""
    if not isinstance(measurement,ImageMeasurement):
        raise TypeError("measurement must be an ImageMeasurement")
    if not isinstance(localization,LocalizationResult):
        raise TypeError("localization must be a LocalizationResult")
    target=np.asarray(target_intensities,dtype=np.float64)
    count=int(localization.lattice_indices.shape[1])
    if target.shape != (count,):
        raise ValueError("target_intensities must align with localization lattice")
    size=int(integration_size_px)
    if size <= 0:
        raise ValueError("integration_size_px must be > 0")

    diagnostics=dict(localization.diagnostics or {})
    matched=np.asarray(diagnostics.get("matched_mask",np.ones(count,dtype=bool)),dtype=bool)
    if matched.shape != (count,):
        raise RuntimeError("Localization matched-mask shape is inconsistent")
    matched_count=int(np.count_nonzero(matched))
    required=max(4,int(np.ceil(float(min_match_fraction)*count)))
    if matched_count < required:
        raise RuntimeError(
            "Geometry calibration needs at least %d matched spots; localization has %d"
            % (required,matched_count)
        )

    image=np.asarray(localization.cropped_image,dtype=np.float64)
    positions=np.asarray(localization.measured_positions_px,dtype=np.float64)
    scores={}
    for orientation in FeedbackOrientation:
        permutation=orientation_permutation(localization.lattice_indices,orientation)
        oriented_positions=positions[:,permutation]
        oriented_matched=matched[permutation]
        powers=np.asarray([
            _sum_around(image,oriented_positions[0,index],oriented_positions[1,index],size)
            for index in range(count)
        ],dtype=np.float64)
        usable=oriented_matched & np.isfinite(powers) & (powers >= 0)
        if int(np.count_nonzero(usable)) < required:
            scores[orientation.value]=-1.0
            continue
        scores[orientation.value]=_rank_correlation(powers[usable],target[usable])

    ordered=sorted(
        ((float(score),FeedbackOrientation.normalize(name)) for name,score in scores.items()),
        key=lambda item:item[0],reverse=True,
    )
    best_score,best_orientation=ordered[0]
    second_score=ordered[1][0] if len(ordered)>1 else -1.0
    margin=float(best_score-second_score)
    return GeometryOrientationAnalysis(
        orientation=best_orientation,
        score=best_score,
        second_score=second_score,
        confidence_margin=margin,
        matched_count=matched_count,
        accepted=bool(best_score >= float(min_score) and margin >= float(min_margin)),
        scores=scores,
    )


def _rank_correlation(a: np.ndarray,b: np.ndarray) -> float:
    ar=_ranks(np.asarray(a,dtype=np.float64))
    br=_ranks(np.asarray(b,dtype=np.float64))
    ar-=np.mean(ar); br-=np.mean(br)
    denom=float(np.linalg.norm(ar)*np.linalg.norm(br))
    return 0.0 if denom <= 1e-12 else float(np.dot(ar,br)/denom)


def _ranks(values: np.ndarray) -> np.ndarray:
    order=np.argsort(values,kind="mergesort")
    ranks=np.empty(values.size,dtype=np.float64)
    ranks[order]=np.arange(values.size,dtype=np.float64)
    return ranks


def _sum_around(image: np.ndarray,x: float,y: float,size: int) -> float:
    half=int(size)//2
    x0=int(round(float(x)))-half; y0=int(round(float(y)))-half
    x1=x0+int(size); y1=y0+int(size)
    x0=max(0,x0); y0=max(0,y0); x1=min(image.shape[1],x1); y1=min(image.shape[0],y1)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return float(np.sum(image[y0:y1,x0:x1]))


__all__=[
    "GEOMETRY_ORIENTATION_PROTOCOL",
    "GeometryOrientationAnalysis",
    "GeometryOrientationCalibration",
    "analyze_geometry_orientation",
    "geometry_orientation_code",
]
