"""Neutral editable lattice geometry used by measurement calibration workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any,Mapping

import numpy as np
from scipy.optimize import least_squares

from ..cgh.localization import LocalizationResult


@dataclass(frozen=True)
class EditablePositionReferenceGeometry:
    """Transient affine detector-space geometry for an editable lattice."""

    period_x_px: float
    period_y_px: float
    rotation_deg: float
    lattice_angle_deg: float
    offset_x_px: float=0.0
    offset_y_px: float=0.0
    handedness: int=1

    def __post_init__(self) -> None:
        values=(
            self.period_x_px,self.period_y_px,self.rotation_deg,
            self.lattice_angle_deg,self.offset_x_px,self.offset_y_px,
        )
        if not all(np.isfinite(float(value)) for value in values):
            raise ValueError("Editable reference geometry contains non-finite values")
        if float(self.period_x_px)<=0 or float(self.period_y_px)<=0:
            raise ValueError("Editable reference periods must be > 0")
        angle=float(self.lattice_angle_deg)
        if not 0.1<=angle<=179.9:
            raise ValueError("Editable lattice angle must be between 0.1 and 179.9 degrees")
        object.__setattr__(self,"period_x_px",float(self.period_x_px))
        object.__setattr__(self,"period_y_px",float(self.period_y_px))
        object.__setattr__(self,"rotation_deg",float(self.rotation_deg))
        object.__setattr__(self,"lattice_angle_deg",angle)
        object.__setattr__(self,"offset_x_px",float(self.offset_x_px))
        object.__setattr__(self,"offset_y_px",float(self.offset_y_px))
        object.__setattr__(self,"handedness",1 if int(self.handedness)>=0 else -1)

    def to_dict(self) -> dict[str,Any]:
        return {
            "period_x_px":self.period_x_px,
            "period_y_px":self.period_y_px,
            "rotation_deg":self.rotation_deg,
            "lattice_angle_deg":self.lattice_angle_deg,
            "offset_x_px":self.offset_x_px,
            "offset_y_px":self.offset_y_px,
            "handedness":self.handedness,
        }

    @classmethod
    def from_mapping(cls,value: Mapping[str,Any]) -> "EditablePositionReferenceGeometry":
        if not isinstance(value,Mapping):
            raise TypeError("Editable reference geometry must be a mapping")
        return cls(
            period_x_px=value["period_x_px"],
            period_y_px=value["period_y_px"],
            rotation_deg=value["rotation_deg"],
            lattice_angle_deg=value["lattice_angle_deg"],
            offset_x_px=value.get("offset_x_px",0.0),
            offset_y_px=value.get("offset_y_px",0.0),
            handedness=value.get("handedness",1),
        )


def editable_position_reference_geometry(
    localization: LocalizationResult,
) -> EditablePositionReferenceGeometry:
    linear,translation=_localization_affine(localization)
    return _editable_geometry_from_affine(
        linear,translation,base_translation=translation,
    )


def fit_center_reference_geometry(
    localization: LocalizationResult,
    *,
    geometry: EditablePositionReferenceGeometry | Mapping[str,Any] | None=None,
    locks: Mapping[str,Any] | None=None,
) -> EditablePositionReferenceGeometry:
    expected=np.asarray(localization.expected_positions_px,dtype=np.float64)
    measured=np.asarray(localization.measured_positions_px,dtype=np.float64)
    if expected.shape!=measured.shape or expected.shape[1]<3:
        raise ValueError("Center fit requires at least three localized spots")
    fit_linear,fit_translation=_localization_affine(localization)
    logical=np.linalg.solve(fit_linear,expected-fit_translation[:,None])
    center=np.mean(expected,axis=1,keepdims=True)
    radius=np.linalg.norm(expected-center,axis=0)
    maximum=float(np.max(radius)) if radius.size else 0.0
    if maximum<=0.0:
        return editable_position_reference_geometry(localization)
    weights=np.exp(-4.0*np.square(radius/maximum))
    lock_names={name for name,value in dict(locks or {}).items() if bool(value)}
    allowed={"period_x_px","period_y_px","rotation_deg","lattice_angle_deg"}
    unknown=lock_names-allowed
    if unknown:
        raise ValueError("Unknown editable reference geometry lock(s): %s" % ", ".join(sorted(unknown)))
    if not lock_names:
        linear,translation=_fit_weighted_affine(logical,measured,weights)
        return _editable_geometry_from_affine(
            linear,translation,base_translation=fit_translation,
        )
    current=editable_position_reference_geometry(localization) if geometry is None else geometry
    if not isinstance(current,EditablePositionReferenceGeometry):
        current=EditablePositionReferenceGeometry.from_mapping(current)
    names=("period_x_px","period_y_px","rotation_deg","lattice_angle_deg")
    free=tuple(name for name in names if name not in lock_names)
    values=current.to_dict(); parameter_names=free+("offset_x_px","offset_y_px")
    x0=np.asarray([values[name] for name in parameter_names],dtype=np.float64)
    lower=[]; upper=[]
    for name in parameter_names:
        if name in ("period_x_px","period_y_px"):
            lower.append(1e-6); upper.append(np.inf)
        elif name=="lattice_angle_deg":
            lower.append(0.1); upper.append(179.9)
        elif name=="rotation_deg":
            lower.append(-720.0); upper.append(720.0)
        else:
            lower.append(-np.inf); upper.append(np.inf)
    root_weights=np.sqrt(weights)[None,:]
    def geometry_from_parameters(parameters):
        candidate=dict(values)
        for name,value in zip(parameter_names,parameters): candidate[name]=float(value)
        return EditablePositionReferenceGeometry.from_mapping(candidate)
    def residual(parameters):
        candidate=geometry_from_parameters(parameters)
        positions=_editable_reference_positions_from_logical(logical,fit_translation,candidate)
        return ((positions-measured)*root_weights).ravel()
    result=least_squares(residual,x0,bounds=(np.asarray(lower),np.asarray(upper)),method="trf")
    if not result.success or not np.all(np.isfinite(result.x)):
        raise RuntimeError("Constrained center fit did not converge")
    return geometry_from_parameters(result.x)


def editable_reference_positions(
    localization: LocalizationResult,
    geometry: EditablePositionReferenceGeometry | Mapping[str,Any],
) -> np.ndarray:
    if not isinstance(geometry,EditablePositionReferenceGeometry):
        geometry=EditablePositionReferenceGeometry.from_mapping(geometry)
    fit_linear,fit_translation=_localization_affine(localization)
    expected=np.asarray(localization.expected_positions_px,dtype=np.float64)
    logical=np.linalg.solve(fit_linear,expected-fit_translation[:,None])
    return _editable_reference_positions_from_logical(logical,fit_translation,geometry)


def editable_reference_center_px(
    localization: LocalizationResult,
    geometry: EditablePositionReferenceGeometry | Mapping[str,Any],
) -> np.ndarray:
    if not isinstance(geometry,EditablePositionReferenceGeometry):
        geometry=EditablePositionReferenceGeometry.from_mapping(geometry)
    _linear,translation=_localization_affine(localization)
    return translation+np.asarray([geometry.offset_x_px,geometry.offset_y_px],dtype=np.float64)


def _editable_geometry_from_affine(linear,translation,*,base_translation):
    linear=np.asarray(linear,dtype=np.float64); translation=np.asarray(translation,dtype=np.float64); base_translation=np.asarray(base_translation,dtype=np.float64)
    a=linear[:,0]; b=linear[:,1]
    period_x=float(np.linalg.norm(a)); period_y=float(np.linalg.norm(b))
    if period_x<=0 or period_y<=0: raise ValueError("Reference affine basis has zero-length vectors")
    rotation=float(np.degrees(np.arctan2(a[1],a[0])))
    cosine=float(np.clip(np.dot(a,b)/(period_x*period_y),-1.0,1.0))
    lattice_angle=float(np.degrees(np.arccos(cosine)))
    handedness=1 if float(a[0]*b[1]-a[1]*b[0])>=0 else -1
    offset=translation-base_translation
    return EditablePositionReferenceGeometry(
        period_x_px=period_x,period_y_px=period_y,rotation_deg=rotation,
        lattice_angle_deg=lattice_angle,offset_x_px=float(offset[0]),
        offset_y_px=float(offset[1]),handedness=handedness,
    )


def _editable_reference_positions_from_logical(logical,fit_translation,geometry):
    theta=np.radians(geometry.rotation_deg)
    phi=theta+geometry.handedness*np.radians(geometry.lattice_angle_deg)
    linear=np.column_stack((
        geometry.period_x_px*np.asarray([np.cos(theta),np.sin(theta)]),
        geometry.period_y_px*np.asarray([np.cos(phi),np.sin(phi)]),
    ))
    translation=np.asarray(fit_translation,dtype=np.float64)+np.asarray([geometry.offset_x_px,geometry.offset_y_px],dtype=np.float64)
    return linear@np.asarray(logical,dtype=np.float64)+translation[:,None]


def _localization_affine(localization):
    diagnostics=dict(localization.diagnostics or {})
    linear=np.asarray(diagnostics.get("affine_linear",()),dtype=np.float64)
    translation=np.asarray(diagnostics.get("affine_translation",()),dtype=np.float64)
    if linear.shape!=(2,2) or translation.shape!=(2,):
        raise ValueError("Editable position references require affine localization diagnostics")
    if not np.all(np.isfinite(linear)) or not np.all(np.isfinite(translation)):
        raise ValueError("Localization affine diagnostics contain non-finite values")
    if abs(float(np.linalg.det(linear)))<1e-12:
        raise ValueError("Localization affine mapping is singular")
    return linear,translation


def _fit_weighted_affine(source,target,weights):
    source=np.asarray(source,dtype=np.float64); target=np.asarray(target,dtype=np.float64); weights=np.asarray(weights,dtype=np.float64)
    if source.shape!=target.shape or source.ndim!=2 or source.shape[0]!=2: raise ValueError("Affine point arrays must share shape (2, N)")
    if weights.shape!=(source.shape[1],): raise ValueError("Affine weights must have shape (N,)")
    if source.shape[1]<3 or np.any(weights<=0) or not np.all(np.isfinite(weights)): raise ValueError("Weighted affine fit requires positive weights and 3+ points")
    design=np.column_stack([source.T,np.ones(source.shape[1],dtype=np.float64)])
    root=np.sqrt(weights)[:,None]
    coefficients,_,rank,_=np.linalg.lstsq(design*root,target.T*root,rcond=None)
    if int(rank)<3: raise ValueError("Affine point set is rank-deficient")
    linear=coefficients[:2,:].T; translation=coefficients[2,:]
    if abs(float(np.linalg.det(linear)))<1e-12: raise ValueError("Affine mapping is singular")
    return linear,translation


__all__=[
    "EditablePositionReferenceGeometry",
    "editable_position_reference_geometry",
    "fit_center_reference_geometry",
    "editable_reference_positions",
    "editable_reference_center_px",
]
