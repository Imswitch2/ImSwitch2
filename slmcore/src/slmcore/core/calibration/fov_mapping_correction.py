"""Persistent FOV correction fields layered on a named base mapping."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass,field,replace
from datetime import datetime
from types import MappingProxyType
from typing import Any,Mapping

import numpy as np
from scipy.spatial import ConvexHull,QhullError

from ..transform2d import (
    FittedTransform2D,
    TRANSFORM_POLYNOMIAL,
    fit_transform2d,
    fitted_polynomial_transform,
    normalize_transform_model,
)


def _freeze_points(value: Any,name: str) -> np.ndarray:
    array = np.asarray(value,dtype=np.float64)
    if array.ndim != 2 or array.shape[0] != 2:
        raise ValueError(f"{name} must have shape (2, N)")
    # FOV calibration deliberately retains a stronger workflow-level minimum
    # than some mathematical transform models require.  A persistent spatial
    # calibration should be backed by at least three sampled positions.
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


@dataclass(frozen=True)
class FOVMappingCorrection:
    """Persistent continuous correction layered on a named base mapping.

    The workflow-specific object owns provenance, sample coverage, and mapping
    compatibility.  The actual continuous mathematics is delegated to the
    unit-agnostic :class:`FittedTransform2D` shared with reference feedback.
    """

    name: str
    slm_serial: str
    section_key: str
    mapping_name: str
    base_mapping_signature: str
    sample_positions_kxy: np.ndarray
    sample_displacements_kxy: np.ndarray
    transform: FittedTransform2D
    detector_name: str | None = None
    provenance: Mapping[str,Any] = field(default_factory=dict)
    created_at: str = ""

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        serial = str(self.slm_serial or "").strip()
        section = str(self.section_key or "").strip()
        mapping = str(self.mapping_name or "").strip()
        if not name:
            raise ValueError("FOV mapping correction name cannot be empty")
        if not serial or not section or not mapping:
            raise ValueError("FOV mapping correction requires SLM serial, section and mapping")
        samples = _freeze_points(self.sample_positions_kxy,"sample_positions_kxy")
        displacements = _freeze_points(
            self.sample_displacements_kxy,"sample_displacements_kxy"
        )
        if samples.shape != displacements.shape:
            raise ValueError("FOV mapping correction sample arrays must have matching shapes")
        transform = self.transform
        if not isinstance(transform,FittedTransform2D):
            raise TypeError("FOV mapping correction transform must be a FittedTransform2D")

        object.__setattr__(self,"name",name)
        object.__setattr__(self,"slm_serial",serial)
        object.__setattr__(self,"section_key",section)
        object.__setattr__(self,"mapping_name",mapping)
        object.__setattr__(
            self,"base_mapping_signature",str(self.base_mapping_signature or "").strip()
        )
        object.__setattr__(self,"sample_positions_kxy",samples)
        object.__setattr__(self,"sample_displacements_kxy",displacements)
        object.__setattr__(self,"transform",transform)
        object.__setattr__(self,"detector_name",str(self.detector_name or "").strip() or None)
        object.__setattr__(self,"provenance",_freeze_mapping(self.provenance))
        object.__setattr__(self,"created_at",str(self.created_at or datetime.now().isoformat()))

    @classmethod
    def fit(
        cls,
        *,
        name: str,
        slm_serial: str,
        section_key: str,
        mapping_name: str,
        base_mapping_signature: str,
        ideal_positions_kxy: Any,
        total_displacements_kxy: Any,
        model: str="polynomial",
        degree: int=2,
        detector_name: str | None=None,
        provenance: Mapping[str,Any] | None=None,
    ) -> "FOVMappingCorrection":
        positions = _freeze_points(ideal_positions_kxy,"ideal_positions_kxy")
        displacements = _freeze_points(
            total_displacements_kxy,"total_displacements_kxy"
        )
        if positions.shape != displacements.shape:
            raise ValueError("FOV correction fit samples must have matching shapes")
        model = normalize_transform_model(model)
        transform = fit_transform2d(
            positions,displacements,model=model,polynomial_degree=int(degree),
        )
        return cls(
            name=name,slm_serial=slm_serial,section_key=section_key,
            mapping_name=mapping_name,base_mapping_signature=base_mapping_signature,
            sample_positions_kxy=positions,
            sample_displacements_kxy=displacements,
            transform=transform,detector_name=detector_name,
            provenance=dict(provenance or {}),
        )

    def with_name(self,name: str) -> "FOVMappingCorrection":
        return replace(self,name=str(name))

    @property
    def model(self) -> str:
        return self.transform.model

    @property
    def degree(self) -> int | None:
        """Polynomial degree, or ``None`` for non-polynomial models.

        Kept as a compatibility-facing property because existing FOV callers
        historically accessed ``correction.degree`` directly.
        """
        return self.transform.polynomial_degree

    @property
    def rms_residual_kxy(self) -> float:
        return self.transform.rms_residual

    @property
    def max_residual_kxy(self) -> float:
        return self.transform.max_residual

    @property
    def coefficients_kxy(self) -> np.ndarray | None:
        """Legacy compatibility accessor for polynomial corrections."""
        return self.transform.coefficients

    @property
    def normalization_center_kxy(self) -> np.ndarray | None:
        return self.transform.normalization_center

    @property
    def normalization_scale_kxy(self) -> np.ndarray | None:
        return self.transform.normalization_scale

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
        """Evaluate the fitted displacement field at ideal kxy positions."""
        return self.transform.displacement(ideal_positions_kxy)

    def corrected_positions(self,ideal_positions_kxy: Any) -> np.ndarray:
        return self.transform.apply(ideal_positions_kxy)

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
        """Serialize the current schema-v2 correction payload."""
        return {
            "name":self.name,
            "slm_serial":self.slm_serial,
            "section_key":self.section_key,
            "mapping_name":self.mapping_name,
            "base_mapping_signature":self.base_mapping_signature,
            "sample_positions_kxy":self.sample_positions_kxy.tolist(),
            "sample_displacements_kxy":self.sample_displacements_kxy.tolist(),
            "transform":self.transform.to_dict(),
            "detector_name":self.detector_name,
            "provenance":deepcopy(dict(self.provenance)),
            "created_at":self.created_at,
        }

    @classmethod
    def from_dict(cls,data: Mapping[str,Any]) -> "FOVMappingCorrection":
        if not isinstance(data,Mapping):
            raise TypeError("FOV mapping correction data must be a mapping")
        values = dict(data)
        values["transform"] = FittedTransform2D.from_dict(values["transform"])
        return cls(**values)

    @classmethod
    def from_v1_dict(cls,data: Mapping[str,Any]) -> "FOVMappingCorrection":
        """Load the former polynomial-only schema without rewriting the file.

        V1 stored polynomial coefficients directly on the FOV correction.  The
        migration simply wraps those exact coefficients/normalization values in
        the shared transform representation, preserving the saved field exactly.
        The next explicit save writes schema v2.
        """
        if not isinstance(data,Mapping):
            raise TypeError("FOV mapping correction data must be a mapping")
        values = dict(data)
        model = str(values.pop("model","polynomial") or "polynomial").strip().lower()
        if model != TRANSFORM_POLYNOMIAL:
            raise ValueError(f"Unsupported schema-v1 FOV model: {model}")
        degree = int(values.pop("degree",2))
        transform = fitted_polynomial_transform(
            degree=degree,
            coefficients=values.pop("coefficients_kxy"),
            normalization_center=values.pop("normalization_center_kxy"),
            normalization_scale=values.pop("normalization_scale_kxy"),
            rms_residual=float(values.pop("rms_residual_kxy",0.0)),
            max_residual=float(values.pop("max_residual_kxy",0.0)),
        )
        values["transform"] = transform
        return cls(**values)


__all__ = ["FOVMappingCorrection"]
