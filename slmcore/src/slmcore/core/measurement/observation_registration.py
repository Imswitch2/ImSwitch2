"""Observation-plane coordinate registration primitives.

Observation registration is a measurement-space calibration.  It maps the
native full-detector coordinates of one SLM section into an explicit canonical
coordinate frame for one observation plane.  It deliberately does not know
about CGH generation, FOV mapping, Qt, or persistence.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass,field
from datetime import datetime
from types import MappingProxyType
from typing import Any,Mapping

import math
import numpy as np

from ..transform2d import (
    FittedTransform2D,TRANSFORM_POLYNOMIAL,fit_transform2d,
    minimum_points_for_transform,normalize_polynomial_degree,normalize_transform_model,
)


def _normalize_optional_text(value: Any) -> str | None:
    text=str(value or "").strip()
    return text or None


def _normalize_shape(value: Any) -> tuple[int,int] | None:
    if value is None:
        return None
    shape=tuple(int(item) for item in value)
    if len(shape)!=2 or shape[0]<=0 or shape[1]<=0:
        raise ValueError("image_shape must contain positive (height, width)")
    return shape


def _freeze_points(value: Any,name: str,*,minimum: int=1) -> np.ndarray:
    array=np.asarray(value,dtype=np.float64)
    if array.ndim!=2 or array.shape[0]!=2:
        raise ValueError(f"{name} must have shape (2, N)")
    if array.shape[1]<int(minimum):
        raise ValueError(f"{name} must contain at least {int(minimum)} points")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} contains non-finite values")
    result=np.array(array,copy=True)
    result.setflags(write=False)
    return result


def _freeze_mapping(value: Mapping[str,Any] | None) -> Mapping[str,Any]:
    if value is None:
        value={}
    if not isinstance(value,Mapping):
        raise TypeError("provenance must be a mapping")
    return MappingProxyType(deepcopy(dict(value)))


@dataclass(frozen=True)
class ObservationFrame:
    """One native detector-coordinate frame for an observation plane.

    Coordinates are always full-detector pixel coordinates.  Detector geometry
    fields are provenance/compatibility data; ``None`` means that the caller
    does not know that property, not that it should differ from a saved value.
    """

    slm_serial: str
    section_key: str
    observation_plane_name: str
    detector_name: str | None=None
    image_shape: tuple[int,int] | None=None
    pixel_size_um: float | None=None

    def __post_init__(self) -> None:
        serial=str(self.slm_serial or "").strip()
        section=str(self.section_key or "").strip()
        plane=str(self.observation_plane_name or "").strip()
        if not serial or not section or not plane:
            raise ValueError(
                "Observation frame requires SLM serial, section and observation plane"
            )
        pixel_size=self.pixel_size_um
        if pixel_size is not None:
            pixel_size=float(pixel_size)
            if not math.isfinite(pixel_size) or pixel_size<=0:
                raise ValueError("pixel_size_um must be a positive finite value")
        object.__setattr__(self,"slm_serial",serial)
        object.__setattr__(self,"section_key",section)
        object.__setattr__(self,"observation_plane_name",plane)
        object.__setattr__(self,"detector_name",_normalize_optional_text(self.detector_name))
        object.__setattr__(self,"image_shape",_normalize_shape(self.image_shape))
        object.__setattr__(self,"pixel_size_um",pixel_size)

    @property
    def scope(self) -> tuple[str,str,str]:
        return self.slm_serial,self.section_key,self.observation_plane_name

    def compatibility_issues(self,other: "ObservationFrame") -> tuple[str,...]:
        """Return concrete contradictions between two descriptions of one frame.

        Unknown optional geometry on either side is tolerated.  This lets older
        or partially described measurements participate while still detecting
        clearly stale detector names, shapes, or pixel sizes when available.
        """
        if not isinstance(other,ObservationFrame):
            raise TypeError("other must be an ObservationFrame")
        issues=[]
        if self.slm_serial!=other.slm_serial:
            issues.append("SLM serial")
        if self.section_key!=other.section_key:
            issues.append("section")
        if self.observation_plane_name!=other.observation_plane_name:
            issues.append("observation plane")
        if (
            self.detector_name is not None and other.detector_name is not None
            and self.detector_name!=other.detector_name
        ):
            issues.append("detector")
        if (
            self.image_shape is not None and other.image_shape is not None
            and self.image_shape!=other.image_shape
        ):
            issues.append("detector image shape")
        if self.pixel_size_um is not None and other.pixel_size_um is not None:
            if not math.isclose(
                self.pixel_size_um,other.pixel_size_um,rel_tol=1e-9,abs_tol=1e-12,
            ):
                issues.append("pixel size")
        return tuple(issues)

    def is_compatible_with(self,other: "ObservationFrame") -> bool:
        return not self.compatibility_issues(other)

    def require_compatible(self,other: "ObservationFrame",*,label: str="Observation frame") -> None:
        issues=self.compatibility_issues(other)
        if issues:
            raise ValueError(f"{label} is incompatible: {', '.join(issues)}")

    def to_dict(self) -> dict[str,Any]:
        return {
            "slm_serial":self.slm_serial,
            "section_key":self.section_key,
            "observation_plane_name":self.observation_plane_name,
            "detector_name":self.detector_name,
            "image_shape":None if self.image_shape is None else list(self.image_shape),
            "pixel_size_um":self.pixel_size_um,
        }

    @classmethod
    def from_dict(cls,data: Mapping[str,Any]) -> "ObservationFrame":
        if not isinstance(data,Mapping):
            raise TypeError("Observation-frame data must be a mapping")
        return cls(**dict(data))


@dataclass(frozen=True)
class ObservationRegistrationCalibration:
    """Persistent native <-> canonical registration for one observation frame."""

    native_frame: ObservationFrame
    canonical_frame: ObservationFrame
    forward_transform: FittedTransform2D
    reverse_transform: FittedTransform2D
    native_points_px: np.ndarray
    canonical_points_px: np.ndarray
    transform_model: str | None=None
    polynomial_degree: int | None=None
    provenance: Mapping[str,Any]=field(default_factory=dict)
    created_at: str=""

    def __post_init__(self) -> None:
        if not isinstance(self.native_frame,ObservationFrame):
            raise TypeError("native_frame must be an ObservationFrame")
        if not isinstance(self.canonical_frame,ObservationFrame):
            raise TypeError("canonical_frame must be an ObservationFrame")
        if not isinstance(self.forward_transform,FittedTransform2D):
            raise TypeError("forward_transform must be a FittedTransform2D")
        if not isinstance(self.reverse_transform,FittedTransform2D):
            raise TypeError("reverse_transform must be a FittedTransform2D")
        if (
            self.native_frame.observation_plane_name
            != self.canonical_frame.observation_plane_name
        ):
            raise ValueError("Native and canonical frames must use the same observation plane")

        model=normalize_transform_model(
            self.forward_transform.model if self.transform_model is None else self.transform_model
        )
        if self.forward_transform.model!=model or self.reverse_transform.model!=model:
            raise ValueError("Forward/reverse registration transforms must use the saved model")
        if model==TRANSFORM_POLYNOMIAL:
            degree=normalize_polynomial_degree(
                self.forward_transform.polynomial_degree
                if self.polynomial_degree is None else self.polynomial_degree,
                allow_legacy_linear=True,
            )
            if (
                self.forward_transform.polynomial_degree!=degree
                or self.reverse_transform.polynomial_degree!=degree
            ):
                raise ValueError("Forward/reverse polynomial registrations must use the saved degree")
        else:
            if self.polynomial_degree is not None:
                raise ValueError("polynomial_degree is only valid for polynomial registration")
            degree=None

        minimum=minimum_points_for_transform(model,2 if degree is None else degree)
        native=_freeze_points(self.native_points_px,"native_points_px",minimum=minimum)
        canonical=_freeze_points(
            self.canonical_points_px,"canonical_points_px",minimum=minimum,
        )
        if native.shape!=canonical.shape:
            raise ValueError("Native and canonical calibration points must have matching shapes")

        object.__setattr__(self,"native_points_px",native)
        object.__setattr__(self,"canonical_points_px",canonical)
        object.__setattr__(self,"transform_model",model)
        object.__setattr__(self,"polynomial_degree",degree)
        object.__setattr__(self,"provenance",_freeze_mapping(self.provenance))
        object.__setattr__(self,"created_at",str(self.created_at or datetime.now().isoformat()))

    @property
    def point_count(self) -> int:
        return int(self.native_points_px.shape[1])

    @property
    def forward_rms_residual(self) -> float:
        return float(self.forward_transform.rms_residual)

    @property
    def reverse_rms_residual(self) -> float:
        return float(self.reverse_transform.rms_residual)

    def native_to_canonical(self,positions_px: Any) -> np.ndarray:
        return self.forward_transform.apply(positions_px)

    def canonical_to_native(self,positions_px: Any) -> np.ndarray:
        return self.reverse_transform.apply(positions_px)

    def validate_native_frame(self,frame: ObservationFrame) -> None:
        self.native_frame.require_compatible(frame,label="Native observation frame")

    def validate_canonical_frame(self,frame: ObservationFrame) -> None:
        self.canonical_frame.require_compatible(frame,label="Canonical observation frame")

    def to_dict(self) -> dict[str,Any]:
        return {
            "native_frame":self.native_frame.to_dict(),
            "canonical_frame":self.canonical_frame.to_dict(),
            "transform_model":self.transform_model,
            "polynomial_degree":self.polynomial_degree,
            "forward_transform":self.forward_transform.to_dict(),
            "reverse_transform":self.reverse_transform.to_dict(),
            "native_points_px":self.native_points_px.tolist(),
            "canonical_points_px":self.canonical_points_px.tolist(),
            "provenance":deepcopy(dict(self.provenance)),
            "created_at":self.created_at,
        }

    @classmethod
    def from_dict(cls,data: Mapping[str,Any]) -> "ObservationRegistrationCalibration":
        if not isinstance(data,Mapping):
            raise TypeError("Observation-registration data must be a mapping")
        values=dict(data)
        values["native_frame"]=ObservationFrame.from_dict(values["native_frame"])
        values["canonical_frame"]=ObservationFrame.from_dict(values["canonical_frame"])
        values["forward_transform"]=FittedTransform2D.from_dict(values["forward_transform"])
        values["reverse_transform"]=FittedTransform2D.from_dict(values["reverse_transform"])
        return cls(**values)


def fit_observation_registration(
    native_frame: ObservationFrame,
    canonical_frame: ObservationFrame,
    native_points_px: Any,
    canonical_points_px: Any,
    *,
    model: str,
    polynomial_degree: int=2,
    weights: Any=None,
    provenance: Mapping[str,Any] | None=None,
) -> ObservationRegistrationCalibration:
    """Fit forward and reverse observation registration from correspondences."""
    if not isinstance(native_frame,ObservationFrame) or not isinstance(canonical_frame,ObservationFrame):
        raise TypeError("native_frame and canonical_frame must be ObservationFrame values")
    if native_frame.observation_plane_name!=canonical_frame.observation_plane_name:
        raise ValueError("Native and canonical frames must use the same observation plane")
    model=normalize_transform_model(model)
    degree=(
        normalize_polynomial_degree(polynomial_degree,allow_legacy_linear=True)
        if model==TRANSFORM_POLYNOMIAL else 2
    )
    minimum=minimum_points_for_transform(model,degree)
    native=_freeze_points(native_points_px,"native_points_px",minimum=minimum)
    canonical=_freeze_points(canonical_points_px,"canonical_points_px",minimum=minimum)
    if native.shape!=canonical.shape:
        raise ValueError("Native and canonical calibration points must have matching shapes")

    forward=fit_transform2d(
        native,canonical-native,model=model,polynomial_degree=degree,weights=weights,
    )
    reverse=fit_transform2d(
        canonical,native-canonical,model=model,polynomial_degree=degree,weights=weights,
    )
    return ObservationRegistrationCalibration(
        native_frame=native_frame,
        canonical_frame=canonical_frame,
        forward_transform=forward,
        reverse_transform=reverse,
        native_points_px=native,
        canonical_points_px=canonical,
        transform_model=model,
        polynomial_degree=degree if model==TRANSFORM_POLYNOMIAL else None,
        provenance={} if provenance is None else provenance,
    )


__all__=[
    "ObservationFrame","ObservationRegistrationCalibration",
    "fit_observation_registration",
]
