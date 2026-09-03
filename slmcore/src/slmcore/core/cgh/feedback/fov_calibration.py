"""Persistent smooth FOV position-calibration fields."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass,field,replace
from datetime import datetime
from types import MappingProxyType
from typing import Any,Mapping

import numpy as np
from scipy.spatial import ConvexHull,QhullError


def _freeze_points(value: Any,name: str) -> np.ndarray:
    array = np.asarray(value,dtype=np.float64)
    if array.ndim != 2 or array.shape[0] != 2:
        raise ValueError(f"{name} must have shape (2, N)")
    if array.shape[1] < 3:
        raise ValueError(f"{name} must contain at least three samples")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} contains non-finite values")
    array = np.array(array,copy=True)
    array.setflags(write=False)
    return array


def _freeze_mapping(value: Mapping[str,Any] | None) -> Mapping[str,Any]:
    if value is None:
        value = {}
    if not isinstance(value,Mapping):
        raise TypeError("metadata must be a mapping")
    return MappingProxyType(deepcopy(dict(value)))


def _polynomial_terms(x: np.ndarray,y: np.ndarray,degree: int) -> np.ndarray:
    columns = [np.ones_like(x),x,y]
    if degree >= 2:
        columns.extend((x*x,x*y,y*y))
    if degree >= 3:
        for total in range(3,degree+1):
            for px in range(total,-1,-1):
                py = total-px
                columns.append((x**px)*(y**py))
    return np.column_stack(columns)


@dataclass(frozen=True)
class FOVPositionCalibration:
    """Smooth correction field sampled from position-feedback measurements.

    The field maps ideal normalized Fourier coordinates to the persistent
    displacement that should be applied before transient position feedback.
    Source samples are retained so saved calibrations can be inspected/refit.
    """

    name: str
    slm_serial: str
    section_key: str
    plane_name: str
    sample_positions_kxy: np.ndarray
    sample_displacements_kxy: np.ndarray
    coefficients_kxy: np.ndarray
    normalization_center_kxy: np.ndarray
    normalization_scale_kxy: np.ndarray
    model: str = "polynomial"
    degree: int = 2
    detector_name: str | None = None
    rms_residual_kxy: float = 0.0
    max_residual_kxy: float = 0.0
    provenance: Mapping[str,Any] = field(default_factory=dict)
    created_at: str = ""

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        serial = str(self.slm_serial or "").strip()
        section = str(self.section_key or "").strip()
        plane = str(self.plane_name or "").strip()
        model = str(self.model or "polynomial").strip().lower()
        degree = int(self.degree)
        if not name:
            raise ValueError("FOV calibration name cannot be empty")
        if not serial or not section or not plane:
            raise ValueError("FOV calibration requires SLM serial, section and plane")
        if model != "polynomial":
            raise ValueError(f"Unsupported FOV calibration model: {model}")
        if degree < 1 or degree > 3:
            raise ValueError("Polynomial degree must be between 1 and 3")

        samples = _freeze_points(self.sample_positions_kxy,"sample_positions_kxy")
        displacements = _freeze_points(
            self.sample_displacements_kxy,"sample_displacements_kxy"
        )
        if samples.shape != displacements.shape:
            raise ValueError("FOV calibration sample arrays must have matching shapes")
        center = np.asarray(self.normalization_center_kxy,dtype=np.float64).reshape(-1)
        scale = np.asarray(self.normalization_scale_kxy,dtype=np.float64).reshape(-1)
        if center.shape != (2,) or scale.shape != (2,):
            raise ValueError("FOV normalization center/scale must have shape (2,)")
        if not np.all(np.isfinite(center)) or not np.all(np.isfinite(scale)):
            raise ValueError("FOV normalization contains non-finite values")
        if np.any(scale <= 0):
            raise ValueError("FOV normalization scale must be > 0")
        center = np.array(center,copy=True); center.setflags(write=False)
        scale = np.array(scale,copy=True); scale.setflags(write=False)

        coeff = np.asarray(self.coefficients_kxy,dtype=np.float64)
        expected_terms = _polynomial_terms(np.zeros(1),np.zeros(1),degree).shape[1]
        if coeff.shape != (2,expected_terms):
            raise ValueError(
                "FOV polynomial coefficients must have shape (2, %d)" % expected_terms
            )
        if not np.all(np.isfinite(coeff)):
            raise ValueError("FOV coefficients contain non-finite values")
        coeff = np.array(coeff,copy=True); coeff.setflags(write=False)
        rms = float(self.rms_residual_kxy)
        maximum = float(self.max_residual_kxy)
        if not np.isfinite(rms) or rms < 0 or not np.isfinite(maximum) or maximum < 0:
            raise ValueError("FOV fit residuals must be finite and >= 0")

        object.__setattr__(self,"name",name)
        object.__setattr__(self,"slm_serial",serial)
        object.__setattr__(self,"section_key",section)
        object.__setattr__(self,"plane_name",plane)
        object.__setattr__(self,"sample_positions_kxy",samples)
        object.__setattr__(self,"sample_displacements_kxy",displacements)
        object.__setattr__(self,"coefficients_kxy",coeff)
        object.__setattr__(self,"normalization_center_kxy",center)
        object.__setattr__(self,"normalization_scale_kxy",scale)
        object.__setattr__(self,"model",model)
        object.__setattr__(self,"degree",degree)
        object.__setattr__(self,"detector_name",str(self.detector_name or "").strip() or None)
        object.__setattr__(self,"rms_residual_kxy",rms)
        object.__setattr__(self,"max_residual_kxy",maximum)
        object.__setattr__(self,"provenance",_freeze_mapping(self.provenance))
        object.__setattr__(self,"created_at",str(self.created_at or datetime.now().isoformat()))

    @classmethod
    def fit(
        cls,
        *,
        name: str,
        slm_serial: str,
        section_key: str,
        plane_name: str,
        ideal_positions_kxy: Any,
        total_displacements_kxy: Any,
        model: str="polynomial",
        degree: int=2,
        detector_name: str | None=None,
        provenance: Mapping[str,Any] | None=None,
    ) -> "FOVPositionCalibration":
        positions = _freeze_points(ideal_positions_kxy,"ideal_positions_kxy")
        displacements = _freeze_points(
            total_displacements_kxy,"total_displacements_kxy"
        )
        if positions.shape != displacements.shape:
            raise ValueError("FOV fit samples must have matching shapes")
        degree = int(degree)
        model = str(model or "polynomial").strip().lower()
        if model != "polynomial":
            raise ValueError(f"Unsupported FOV calibration model: {model}")
        center = np.mean(positions,axis=1)
        span = np.ptp(positions,axis=1)
        scale = np.where(span > 1e-12,0.5*span,1.0)
        normalized = (positions-center[:,None])/scale[:,None]
        design = _polynomial_terms(normalized[0],normalized[1],degree)
        if design.shape[0] < design.shape[1]:
            raise ValueError(
                "Not enough position samples for polynomial degree %d: need at least %d"
                % (degree,design.shape[1])
            )
        coefficients,_,rank,_ = np.linalg.lstsq(design,displacements.T,rcond=None)
        if int(rank) < design.shape[1]:
            raise ValueError("FOV calibration samples are rank-deficient for this fit")
        coefficients = coefficients.T
        fitted = (design @ coefficients.T).T
        residual = displacements-fitted
        magnitude = np.linalg.norm(residual,axis=0)
        return cls(
            name=name,slm_serial=slm_serial,section_key=section_key,
            plane_name=plane_name,sample_positions_kxy=positions,
            sample_displacements_kxy=displacements,
            coefficients_kxy=coefficients,
            normalization_center_kxy=center,
            normalization_scale_kxy=scale,
            model=model,degree=degree,detector_name=detector_name,
            rms_residual_kxy=float(np.sqrt(np.mean(magnitude*magnitude))),
            max_residual_kxy=float(np.max(magnitude)),
            provenance=dict(provenance or {}),
        )

    def with_name(self,name: str) -> "FOVPositionCalibration":
        return replace(self,name=str(name))

    @property
    def sample_count(self) -> int:
        return int(self.sample_positions_kxy.shape[1])

    @property
    def coverage_area(self) -> float:
        try:
            hull = ConvexHull(self.sample_positions_kxy.T)
        except QhullError:
            return 0.0
        return float(hull.volume)  # 2D ConvexHull.volume is polygon area.

    def evaluate(self,ideal_positions_kxy: Any) -> np.ndarray:
        positions = np.asarray(ideal_positions_kxy,dtype=np.float64)
        if positions.ndim != 2 or positions.shape[0] != 2:
            raise ValueError("ideal_positions_kxy must have shape (2, N)")
        if not np.all(np.isfinite(positions)):
            raise ValueError("ideal_positions_kxy contains non-finite values")
        normalized = (
            positions-self.normalization_center_kxy[:,None]
        )/self.normalization_scale_kxy[:,None]
        design = _polynomial_terms(normalized[0],normalized[1],self.degree)
        return (design @ self.coefficients_kxy.T).T

    def corrected_positions(self,ideal_positions_kxy: Any) -> np.ndarray:
        ideal = np.asarray(ideal_positions_kxy,dtype=np.float64)
        return ideal+self.evaluate(ideal)

    def coverage_extrapolation(
        self,ideal_positions_kxy: Any,*,tolerance_fraction: float=0.005,
    ) -> tuple[np.ndarray,float,float]:
        """Return outside mask, maximum hull violation, and numerical tolerance."""
        positions = np.asarray(ideal_positions_kxy,dtype=np.float64)
        if positions.ndim != 2 or positions.shape[0] != 2:
            raise ValueError("ideal_positions_kxy must have shape (2, N)")
        try:
            hull = ConvexHull(self.sample_positions_kxy.T)
        except QhullError:
            return np.ones(positions.shape[1],dtype=bool),float("inf"),0.0
        equations = np.asarray(hull.equations,dtype=np.float64)
        # ConvexHull facet equations are normal*x + offset <= 0 inside.
        violations = equations[:,:2] @ positions + equations[:,2,None]
        per_point = np.max(violations,axis=0)
        diameter = float(np.linalg.norm(np.ptp(self.sample_positions_kxy,axis=1)))
        tolerance = max(1e-10,float(tolerance_fraction)*max(diameter,1e-8))
        outside = per_point > tolerance
        maximum = max(0.0,float(np.max(per_point))) if per_point.size else 0.0
        return outside,maximum,tolerance

    def to_dict(self) -> dict[str,Any]:
        return {
            "name":self.name,
            "slm_serial":self.slm_serial,
            "section_key":self.section_key,
            "plane_name":self.plane_name,
            "sample_positions_kxy":self.sample_positions_kxy.tolist(),
            "sample_displacements_kxy":self.sample_displacements_kxy.tolist(),
            "coefficients_kxy":self.coefficients_kxy.tolist(),
            "normalization_center_kxy":self.normalization_center_kxy.tolist(),
            "normalization_scale_kxy":self.normalization_scale_kxy.tolist(),
            "model":self.model,
            "degree":self.degree,
            "detector_name":self.detector_name,
            "rms_residual_kxy":self.rms_residual_kxy,
            "max_residual_kxy":self.max_residual_kxy,
            "provenance":deepcopy(dict(self.provenance)),
            "created_at":self.created_at,
        }

    @classmethod
    def from_dict(cls,data: Mapping[str,Any]) -> "FOVPositionCalibration":
        if not isinstance(data,Mapping):
            raise TypeError("FOV calibration data must be a mapping")
        return cls(**dict(data))


__all__ = ["FOVPositionCalibration"]
