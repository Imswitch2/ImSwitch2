"""Neutral detector-space position-reference alignment.

This module owns the distinction between per-site corrections and one shared
continuous transform.  It has no dependency on feedback state, reference
persistence, SLM calibration, or Qt.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass,field
from types import MappingProxyType
from typing import Any,Mapping

import numpy as np

from ..transform2d import (
    CONTINUOUS_TRANSFORM_MODELS,FittedTransform2D,TRANSFORM_POLYNOMIAL,
    fit_transform2d,normalize_polynomial_degree,normalize_transform_model,
)
from .position_reference import PositionReferenceTarget,ResolvedPositionReference


ALIGNMENT_PER_SPOT="per_spot"
POSITION_ALIGNMENT_MODELS=(ALIGNMENT_PER_SPOT,)+CONTINUOUS_TRANSFORM_MODELS


def normalize_position_alignment_model(value: Any) -> str:
    model=str(value or ALIGNMENT_PER_SPOT).strip().lower()
    if model not in POSITION_ALIGNMENT_MODELS:
        raise ValueError(
            "Position-alignment model must be one of: %s"
            % ", ".join(POSITION_ALIGNMENT_MODELS)
        )
    return model


def _points(value: Any,name: str) -> np.ndarray:
    array=np.asarray(value,dtype=np.float64)
    if array.ndim!=2 or array.shape[0]!=2 or array.shape[1]<1:
        raise ValueError(f"{name} must have shape (2, N), N >= 1")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} contains non-finite values")
    return array


def _mask(value: Any,*,count: int) -> np.ndarray:
    if value is None:
        return np.ones(count,dtype=bool)
    array=np.asarray(value,dtype=bool)
    if array.shape!=(count,):
        raise ValueError(f"valid_mask must have shape ({count},)")
    return array


def _weights(value: Any,*,count: int) -> np.ndarray | None:
    if value is None:
        return None
    array=np.asarray(value,dtype=np.float64)
    if array.shape!=(count,):
        raise ValueError(f"weights must have shape ({count},)")
    if not np.all(np.isfinite(array)) or np.any(array<0):
        raise ValueError("weights must be finite and >= 0")
    return array


def _freeze_array(value: Any,dtype=np.float64) -> np.ndarray:
    result=np.array(value,dtype=dtype,copy=True)
    result.setflags(write=False)
    return result


def _freeze_mapping(value: Mapping[str,Any] | None) -> Mapping[str,Any]:
    return MappingProxyType(deepcopy(dict(value or {})))


def _residual_statistics(
    residual: np.ndarray,fit_mask: np.ndarray,weights: np.ndarray | None,
) -> tuple[float,float]:
    magnitude=np.linalg.norm(np.asarray(residual,dtype=np.float64)[:,fit_mask],axis=0)
    if magnitude.size==0:
        raise ValueError("Position alignment requires at least one valid correspondence")
    if weights is None:
        rms=float(np.sqrt(np.mean(magnitude*magnitude)))
    else:
        selected=np.asarray(weights,dtype=np.float64)[fit_mask]
        total=float(np.sum(selected))
        if total<=0:
            raise ValueError("Position alignment requires a positive total weight")
        rms=float(np.sqrt(np.sum(selected*magnitude*magnitude)/total))
    return rms,float(np.max(magnitude))


@dataclass(frozen=True)
class PositionAlignmentProblem:
    """Measured/desired detector geometry before choosing a correction model."""

    registered_positions_px: np.ndarray
    measured_positions_px: np.ndarray
    desired_positions_px: np.ndarray
    fit_mask: np.ndarray | None=None
    weights: np.ndarray | None=None

    def __post_init__(self) -> None:
        registered=_points(self.registered_positions_px,"registered_positions_px")
        measured=_points(self.measured_positions_px,"measured_positions_px")
        desired=_points(self.desired_positions_px,"desired_positions_px")
        if registered.shape!=measured.shape or registered.shape!=desired.shape:
            raise ValueError("Position-alignment point arrays must share shape (2, N)")
        count=registered.shape[1]
        fit_mask=_mask(self.fit_mask,count=count)
        weights=_weights(self.weights,count=count)
        object.__setattr__(self,"registered_positions_px",_freeze_array(registered))
        object.__setattr__(self,"measured_positions_px",_freeze_array(measured))
        object.__setattr__(self,"desired_positions_px",_freeze_array(desired))
        object.__setattr__(self,"fit_mask",_freeze_array(fit_mask,dtype=bool))
        object.__setattr__(self,"weights",None if weights is None else _freeze_array(weights))


@dataclass(frozen=True)
class PositionAlignmentResult:
    """Predicted detector positions plus the samples that produced the field."""

    model: str
    registered_positions_px: np.ndarray
    measured_positions_px: np.ndarray
    desired_positions_px: np.ndarray
    predicted_positions_px: np.ndarray
    correction_px: np.ndarray
    residual_px: np.ndarray
    fit_mask: np.ndarray
    fit_sample_positions_px: np.ndarray
    fit_sample_displacements_px: np.ndarray
    weights: np.ndarray | None=None
    transform: FittedTransform2D | None=None
    rms_residual: float=0.0
    max_residual: float=0.0
    diagnostics: Mapping[str,Any]=field(default_factory=dict)

    def __post_init__(self) -> None:
        model=normalize_position_alignment_model(self.model)
        registered=_points(self.registered_positions_px,"registered_positions_px")
        measured=_points(self.measured_positions_px,"measured_positions_px")
        desired=_points(self.desired_positions_px,"desired_positions_px")
        predicted=_points(self.predicted_positions_px,"predicted_positions_px")
        correction=_points(self.correction_px,"correction_px")
        residual=_points(self.residual_px,"residual_px")
        if not (
            registered.shape==measured.shape==desired.shape==predicted.shape
            ==correction.shape==residual.shape
        ):
            raise ValueError("Position-alignment arrays must share shape (2, N)")
        count=predicted.shape[1]
        fit_mask=_mask(self.fit_mask,count=count)
        weights=_weights(self.weights,count=count)
        fit_positions=_points(self.fit_sample_positions_px,"fit_sample_positions_px")
        fit_displacements=_points(
            self.fit_sample_displacements_px,"fit_sample_displacements_px",
        )
        fit_count=int(np.count_nonzero(fit_mask))
        if fit_positions.shape!=(2,fit_count) or fit_displacements.shape!=(2,fit_count):
            raise ValueError("Alignment fit samples must match the fit-mask count")
        transform=self.transform
        if model==ALIGNMENT_PER_SPOT:
            if transform is not None:
                raise ValueError("Per-spot alignment does not use a fitted transform")
        else:
            if not isinstance(transform,FittedTransform2D):
                raise TypeError("Continuous alignment requires a FittedTransform2D")
            if transform.model!=model:
                raise ValueError("Alignment model and fitted transform disagree")
        rms=float(self.rms_residual); maximum=float(self.max_residual)
        if not np.isfinite(rms) or rms<0 or not np.isfinite(maximum) or maximum<0:
            raise ValueError("Alignment residuals must be finite and >= 0")
        object.__setattr__(self,"model",model)
        object.__setattr__(self,"registered_positions_px",_freeze_array(registered))
        object.__setattr__(self,"measured_positions_px",_freeze_array(measured))
        object.__setattr__(self,"desired_positions_px",_freeze_array(desired))
        object.__setattr__(self,"predicted_positions_px",_freeze_array(predicted))
        object.__setattr__(self,"correction_px",_freeze_array(correction))
        object.__setattr__(self,"residual_px",_freeze_array(residual))
        object.__setattr__(self,"fit_mask",_freeze_array(fit_mask,dtype=bool))
        object.__setattr__(self,"fit_sample_positions_px",_freeze_array(fit_positions))
        object.__setattr__(self,"fit_sample_displacements_px",_freeze_array(fit_displacements))
        object.__setattr__(self,"weights",None if weights is None else _freeze_array(weights))
        object.__setattr__(self,"rms_residual",rms)
        object.__setattr__(self,"max_residual",maximum)
        object.__setattr__(self,"diagnostics",_freeze_mapping(self.diagnostics))

    @property
    def valid_mask(self) -> np.ndarray:
        """Compatibility/readability alias for the correspondences used by the fit."""
        return self.fit_mask



def fit_position_alignment(
    registered_positions_px: Any,
    measured_positions_px: Any,
    desired_positions_px: Any,
    *,
    valid_mask: Any=None,
    weights: Any=None,
    model: str=ALIGNMENT_PER_SPOT,
    polynomial_degree: int=2,
    fitted_transform: FittedTransform2D | None=None,
) -> PositionAlignmentResult:
    """Fit/sample one position-reference alignment in detector coordinates.

    Continuous modes fit ``desired - measured`` at valid correspondences as a
    function of registered target positions, then sample that displacement field
    across the complete target.  Per-spot mode applies only valid site errors.
    """
    problem=PositionAlignmentProblem(
        registered_positions_px=registered_positions_px,
        measured_positions_px=measured_positions_px,
        desired_positions_px=desired_positions_px,
        fit_mask=valid_mask,
        weights=weights,
    )
    registered=np.asarray(problem.registered_positions_px,dtype=np.float64)
    measured=np.asarray(problem.measured_positions_px,dtype=np.float64)
    desired=np.asarray(problem.desired_positions_px,dtype=np.float64)
    count=registered.shape[1]
    weight_array=(
        None if problem.weights is None else np.asarray(problem.weights,dtype=np.float64)
    )
    fit_mask=np.array(problem.fit_mask,copy=True)
    if weight_array is not None:
        fit_mask &= weight_array>0
    if not np.any(fit_mask):
        raise ValueError("Position alignment requires at least one valid correspondence")

    normalized=normalize_position_alignment_model(model)
    degree=normalize_polynomial_degree(
        polynomial_degree,allow_legacy_linear=True,
    ) if normalized==TRANSFORM_POLYNOMIAL else 2

    transform=fitted_transform
    if normalized==ALIGNMENT_PER_SPOT:
        if transform is not None:
            raise ValueError("Per-spot alignment cannot use fitted_transform")
        predicted=np.array(measured,copy=True)
        predicted[:,fit_mask]=desired[:,fit_mask]
    else:
        if transform is not None:
            if not isinstance(transform,FittedTransform2D):
                raise TypeError("fitted_transform must be a FittedTransform2D")
            if transform.model!=normalize_transform_model(normalized):
                raise ValueError("fitted_transform model does not match alignment model")
        else:
            fit_weights=None if weight_array is None else weight_array[fit_mask]
            transform=fit_transform2d(
                registered[:,fit_mask],
                desired[:,fit_mask]-measured[:,fit_mask],
                model=normalized,
                polynomial_degree=degree,
                weights=fit_weights,
            )
        predicted=measured+transform.displacement(registered)

    correction=predicted-measured
    residual=desired-predicted
    rms,maximum=_residual_statistics(residual,fit_mask,weight_array)
    effective_weights=(
        None if weight_array is None
        else np.where(fit_mask,weight_array,0.0)
    )
    return PositionAlignmentResult(
        model=normalized,
        registered_positions_px=registered,
        measured_positions_px=measured,
        desired_positions_px=desired,
        predicted_positions_px=predicted,
        correction_px=correction,
        residual_px=residual,
        fit_mask=fit_mask,
        fit_sample_positions_px=registered[:,fit_mask],
        fit_sample_displacements_px=desired[:,fit_mask]-measured[:,fit_mask],
        weights=effective_weights,
        transform=transform,
        rms_residual=rms,
        max_residual=maximum,
        diagnostics={
            "point_count":count,
            "fit_count":int(np.count_nonzero(fit_mask)),
            "weighted":weight_array is not None,
            "weight_sum":(
                None if weight_array is None
                else float(np.sum(weight_array[fit_mask]))
            ),
            "polynomial_degree":degree if normalized==TRANSFORM_POLYNOMIAL else None,
        },
    )


def align_resolved_position_reference(
    target: PositionReferenceTarget,
    reference: ResolvedPositionReference,
    *,
    model: str=ALIGNMENT_PER_SPOT,
    polynomial_degree: int=2,
    fitted_transform: FittedTransform2D | None=None,
) -> PositionAlignmentResult:
    """Align one resolved reference to one target using their valid intersection."""
    if not isinstance(target,PositionReferenceTarget):
        raise TypeError("target must be a PositionReferenceTarget")
    if not isinstance(reference,ResolvedPositionReference):
        raise TypeError("reference must be a ResolvedPositionReference")
    if reference.desired_positions_px.shape[1]!=target.point_count:
        raise ValueError("Resolved reference and target spot counts do not match")
    valid=np.asarray(target.matched_mask,dtype=bool) & np.asarray(
        reference.valid_mask,dtype=bool,
    )
    return fit_position_alignment(
        target.registered_positions_px,
        target.measured_positions_px,
        reference.desired_positions_px,
        valid_mask=valid,
        weights=reference.weights,
        model=model,
        polynomial_degree=polynomial_degree,
        fitted_transform=fitted_transform,
    )


__all__=[
    "ALIGNMENT_PER_SPOT","POSITION_ALIGNMENT_MODELS","PositionAlignmentProblem",
    "PositionAlignmentResult",
    "align_resolved_position_reference","fit_position_alignment",
    "normalize_position_alignment_model",
]
