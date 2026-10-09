"""Unit-agnostic continuous 2D transform fitting and evaluation.

The module deliberately knows nothing about SLMs, detector pixels, mappings, or
calibration persistence.  Callers provide source positions and desired
*displacements* in one coordinate system; the fitted transform can then be
sampled at arbitrary positions in that same coordinate system.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any,Mapping

import numpy as np


TRANSFORM_TRANSLATION = "translation"
TRANSFORM_RIGID = "rigid"
TRANSFORM_AFFINE = "affine"
TRANSFORM_POLYNOMIAL = "polynomial"
CONTINUOUS_TRANSFORM_MODELS = (
    TRANSFORM_TRANSLATION,
    TRANSFORM_RIGID,
    TRANSFORM_AFFINE,
    TRANSFORM_POLYNOMIAL,
)


def normalize_transform_model(value: Any) -> str:
    model = str(value or "").strip().lower()
    if model not in CONTINUOUS_TRANSFORM_MODELS:
        raise ValueError(
            "Transform model must be one of: %s"
            % ", ".join(CONTINUOUS_TRANSFORM_MODELS)
        )
    return model


def normalize_polynomial_degree(value: Any,*,allow_legacy_linear: bool=True) -> int:
    degree = int(value)
    allowed = (1,2,3,4) if allow_legacy_linear else (2,3,4)
    if degree not in allowed:
        raise ValueError(
            "Polynomial degree must be %s"
            % (", ".join(str(item) for item in allowed))
        )
    return degree


def polynomial_term_count(degree: int) -> int:
    degree = normalize_polynomial_degree(degree,allow_legacy_linear=True)
    return (degree+1)*(degree+2)//2


def minimum_points_for_transform(model: str,polynomial_degree: int=2) -> int:
    model = normalize_transform_model(model)
    if model == TRANSFORM_TRANSLATION:
        return 1
    if model == TRANSFORM_RIGID:
        return 2
    if model == TRANSFORM_AFFINE:
        return 3
    return polynomial_term_count(polynomial_degree)


def _points(value: Any,name: str,*,minimum: int=1) -> np.ndarray:
    array = np.asarray(value,dtype=np.float64)
    if array.ndim != 2 or array.shape[0] != 2:
        raise ValueError(f"{name} must have shape (2, N)")
    if array.shape[1] < int(minimum):
        raise ValueError(f"{name} must contain at least {int(minimum)} points")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} contains non-finite values")
    return array


def polynomial_design(
    positions: Any,*,center: Any,scale: Any,degree: int,
) -> np.ndarray:
    points = _points(positions,"positions")
    degree = normalize_polynomial_degree(degree,allow_legacy_linear=True)
    center_array = np.asarray(center,dtype=np.float64).reshape(-1)
    scale_array = np.asarray(scale,dtype=np.float64).reshape(-1)
    if center_array.shape != (2,) or scale_array.shape != (2,):
        raise ValueError("Polynomial normalization center/scale must have shape (2,)")
    if not np.all(np.isfinite(center_array)) or not np.all(np.isfinite(scale_array)):
        raise ValueError("Polynomial normalization contains non-finite values")
    if np.any(scale_array <= 0):
        raise ValueError("Polynomial normalization scale must be > 0")
    normalized = (points-center_array[:,None])/scale_array[:,None]
    x,y = normalized
    terms = [np.ones(points.shape[1],dtype=np.float64)]
    for total_degree in range(1,degree+1):
        for y_power in range(total_degree+1):
            x_power = total_degree-y_power
            terms.append((x**x_power)*(y**y_power))
    return np.column_stack(terms)


def _freeze_optional_array(value: Any,shape: tuple[int,...],name: str) -> np.ndarray | None:
    if value is None:
        return None
    array = np.asarray(value,dtype=np.float64)
    if array.shape != shape:
        raise ValueError(f"{name} must have shape {shape}")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} contains non-finite values")
    array = np.array(array,copy=True)
    array.setflags(write=False)
    return array


@dataclass(frozen=True)
class FittedTransform2D:
    """A continuous fitted displacement field in an arbitrary 2D coordinate system."""

    model: str
    matrix: np.ndarray | None=None
    coefficients: np.ndarray | None=None
    normalization_center: np.ndarray | None=None
    normalization_scale: np.ndarray | None=None
    polynomial_degree: int | None=None
    rms_residual: float=0.0
    max_residual: float=0.0

    def __post_init__(self) -> None:
        model = normalize_transform_model(self.model)
        rms = float(self.rms_residual)
        maximum = float(self.max_residual)
        if not np.isfinite(rms) or rms < 0 or not np.isfinite(maximum) or maximum < 0:
            raise ValueError("Transform residuals must be finite and >= 0")

        matrix = coefficients = center = scale = None
        degree = None
        if model in {TRANSFORM_TRANSLATION,TRANSFORM_RIGID,TRANSFORM_AFFINE}:
            matrix = _freeze_optional_array(self.matrix,(2,3),"matrix")
            if matrix is None:
                raise ValueError(f"{model} transform requires a 2x3 matrix")
            linear = matrix[:,:2]
            if model == TRANSFORM_TRANSLATION:
                if not np.allclose(linear,np.eye(2),rtol=0.0,atol=1e-12):
                    raise ValueError("Translation transform linear part must be identity")
            elif model == TRANSFORM_RIGID:
                if not np.allclose(linear.T@linear,np.eye(2),rtol=0.0,atol=1e-9):
                    raise ValueError("Rigid transform linear part must be orthonormal")
                if float(np.linalg.det(linear)) <= 0:
                    raise ValueError("Rigid transform must not contain a reflection")
            elif abs(float(np.linalg.det(linear))) < 1e-12:
                raise ValueError("Affine transform must be non-singular")
        else:
            degree = normalize_polynomial_degree(
                2 if self.polynomial_degree is None else self.polynomial_degree,
                allow_legacy_linear=True,
            )
            term_count = polynomial_term_count(degree)
            coefficients = _freeze_optional_array(
                self.coefficients,(2,term_count),"coefficients"
            )
            center = _freeze_optional_array(
                self.normalization_center,(2,),"normalization_center"
            )
            scale = _freeze_optional_array(
                self.normalization_scale,(2,),"normalization_scale"
            )
            if coefficients is None or center is None or scale is None:
                raise ValueError(
                    "Polynomial transform requires coefficients and normalization"
                )
            if np.any(scale <= 0):
                raise ValueError("Polynomial normalization scale must be > 0")

        object.__setattr__(self,"model",model)
        object.__setattr__(self,"matrix",matrix)
        object.__setattr__(self,"coefficients",coefficients)
        object.__setattr__(self,"normalization_center",center)
        object.__setattr__(self,"normalization_scale",scale)
        object.__setattr__(self,"polynomial_degree",degree)
        object.__setattr__(self,"rms_residual",rms)
        object.__setattr__(self,"max_residual",maximum)

    def apply(self,positions: Any) -> np.ndarray:
        points = _points(positions,"positions")
        if self.model == TRANSFORM_POLYNOMIAL:
            return points+self.displacement(points)
        matrix = np.asarray(self.matrix,dtype=np.float64)
        return matrix[:,:2]@points + matrix[:,2,None]

    def displacement(self,positions: Any) -> np.ndarray:
        points = _points(positions,"positions")
        if self.model == TRANSFORM_POLYNOMIAL:
            design = polynomial_design(
                points,
                center=self.normalization_center,
                scale=self.normalization_scale,
                degree=int(self.polynomial_degree),
            )
            return np.asarray(self.coefficients,dtype=np.float64)@design.T
        return self.apply(points)-points

    def to_dict(self) -> dict[str,Any]:
        result: dict[str,Any] = {
            "model":self.model,
            "rms_residual":self.rms_residual,
            "max_residual":self.max_residual,
        }
        if self.model == TRANSFORM_POLYNOMIAL:
            result.update({
                "polynomial_degree":int(self.polynomial_degree),
                "coefficients":np.asarray(self.coefficients).tolist(),
                "normalization_center":np.asarray(self.normalization_center).tolist(),
                "normalization_scale":np.asarray(self.normalization_scale).tolist(),
            })
        else:
            result["matrix"] = np.asarray(self.matrix).tolist()
        return result

    @classmethod
    def from_dict(cls,data: Mapping[str,Any]) -> "FittedTransform2D":
        if not isinstance(data,Mapping):
            raise TypeError("2D transform data must be a mapping")
        return cls(**dict(data))


def fitted_matrix_transform(
    model: str,matrix: Any,*,rms_residual: float=0.0,max_residual: float=0.0,
) -> FittedTransform2D:
    """Construct a validated matrix-backed transform, e.g. a manually edited affine."""
    return FittedTransform2D(
        model=normalize_transform_model(model),matrix=np.asarray(matrix,dtype=np.float64),
        rms_residual=rms_residual,max_residual=max_residual,
    )


def fitted_polynomial_transform(
    *,degree: int,coefficients: Any,normalization_center: Any,
    normalization_scale: Any,rms_residual: float=0.0,max_residual: float=0.0,
) -> FittedTransform2D:
    """Construct a validated polynomial field, including legacy degree-1 fields."""
    return FittedTransform2D(
        model=TRANSFORM_POLYNOMIAL,
        polynomial_degree=degree,
        coefficients=np.asarray(coefficients,dtype=np.float64),
        normalization_center=np.asarray(normalization_center,dtype=np.float64),
        normalization_scale=np.asarray(normalization_scale,dtype=np.float64),
        rms_residual=rms_residual,max_residual=max_residual,
    )


def _fit_weights(value: Any,*,count: int,minimum: int) -> np.ndarray | None:
    if value is None:
        return None
    weights = np.asarray(value,dtype=np.float64)
    if weights.shape != (int(count),):
        raise ValueError(f"weights must have shape ({int(count)},)")
    if not np.all(np.isfinite(weights)) or np.any(weights < 0):
        raise ValueError("weights must be finite and >= 0")
    positive = weights > 0
    if int(np.count_nonzero(positive)) < int(minimum):
        raise ValueError(
            "Transform fit requires at least %d positive-weight samples" % int(minimum)
        )
    return weights


def fit_transform2d(
    source_positions: Any,
    desired_displacements: Any,
    *,
    model: str,
    polynomial_degree: int=2,
    weights: Any=None,
) -> FittedTransform2D:
    """Fit one continuous transform to source positions and desired displacements.

    Optional non-negative per-sample ``weights`` are shared by every transform
    model.  Zero-weight samples are excluded entirely; positive weights enter
    translation averaging, weighted Kabsch, or weighted least squares as
    appropriate.  Residual RMS follows the same weighting while maximum
    residual is reported across the positive-weight fit samples.
    """
    model = normalize_transform_model(model)
    degree = (
        normalize_polynomial_degree(polynomial_degree,allow_legacy_linear=True)
        if model == TRANSFORM_POLYNOMIAL else 2
    )
    minimum = minimum_points_for_transform(model,degree)
    source = _points(source_positions,"source_positions",minimum=minimum)
    displacement = _points(
        desired_displacements,"desired_displacements",minimum=minimum,
    )
    if source.shape != displacement.shape:
        raise ValueError("Transform source/displacement arrays must have matching shapes")
    sample_weights = _fit_weights(weights,count=source.shape[1],minimum=minimum)
    if sample_weights is not None:
        positive = sample_weights > 0
        source = source[:,positive]
        displacement = displacement[:,positive]
        sample_weights = sample_weights[positive]

    if model == TRANSFORM_TRANSLATION:
        shift = (
            np.mean(displacement,axis=1)
            if sample_weights is None
            else np.average(displacement,axis=1,weights=sample_weights)
        )
        matrix = np.column_stack([np.eye(2),shift])
        transform = FittedTransform2D(model=model,matrix=matrix)
    elif model == TRANSFORM_RIGID:
        target = source+displacement
        if sample_weights is None:
            source_center = np.mean(source,axis=1)
            target_center = np.mean(target,axis=1)
        else:
            source_center = np.average(source,axis=1,weights=sample_weights)
            target_center = np.average(target,axis=1,weights=sample_weights)
        source_zero = source-source_center[:,None]
        target_zero = target-target_center[:,None]
        if float(np.linalg.norm(source_zero)) < 1e-12:
            raise ValueError("Rigid source points are degenerate")
        covariance = (
            target_zero@source_zero.T
            if sample_weights is None
            else (target_zero*sample_weights[None,:])@source_zero.T
        )
        u,_singular_values,vt = np.linalg.svd(covariance)
        rotation = u@vt
        if float(np.linalg.det(rotation)) < 0.0:
            u[:,-1] *= -1.0
            rotation = u@vt
        translation = target_center-rotation@source_center
        transform = FittedTransform2D(
            model=model,matrix=np.column_stack([rotation,translation])
        )
    elif model == TRANSFORM_AFFINE:
        target = source+displacement
        design = np.column_stack([
            source.T,np.ones(source.shape[1],dtype=np.float64),
        ])
        if sample_weights is None:
            weighted_design = design
            weighted_target = target.T
        else:
            root = np.sqrt(sample_weights)[:,None]
            weighted_design = design*root
            weighted_target = target.T*root
        coefficients,_,rank,_ = np.linalg.lstsq(
            weighted_design,weighted_target,rcond=None
        )
        if int(rank) < 3:
            raise ValueError("Affine transform samples are rank-deficient")
        transform = FittedTransform2D(model=model,matrix=coefficients.T)
    else:
        center = (
            np.mean(source,axis=1)
            if sample_weights is None
            else np.average(source,axis=1,weights=sample_weights)
        )
        span = np.ptp(source,axis=1)
        scale = np.where(span > 1e-12,0.5*span,1.0)
        design = polynomial_design(
            source,center=center,scale=scale,degree=degree,
        )
        term_count = polynomial_term_count(degree)
        if sample_weights is None:
            weighted_design = design
            weighted_displacement = displacement.T
        else:
            root = np.sqrt(sample_weights)[:,None]
            weighted_design = design*root
            weighted_displacement = displacement.T*root
        coefficients,_,rank,_ = np.linalg.lstsq(
            weighted_design,weighted_displacement,rcond=None
        )
        if int(rank) < term_count:
            raise ValueError(
                "Polynomial transform samples are rank-deficient for degree %d"
                % degree
            )
        transform = FittedTransform2D(
            model=model,polynomial_degree=degree,
            coefficients=coefficients.T,
            normalization_center=center,normalization_scale=scale,
        )

    residual = displacement-transform.displacement(source)
    magnitude = np.linalg.norm(residual,axis=0)
    rms = (
        float(np.sqrt(np.mean(magnitude*magnitude)))
        if sample_weights is None
        else float(np.sqrt(
            np.sum(sample_weights*magnitude*magnitude)/np.sum(sample_weights)
        ))
    )
    return FittedTransform2D(
        model=transform.model,
        matrix=transform.matrix,
        coefficients=transform.coefficients,
        normalization_center=transform.normalization_center,
        normalization_scale=transform.normalization_scale,
        polynomial_degree=transform.polynomial_degree,
        rms_residual=rms,
        max_residual=float(np.max(magnitude)),
    )


__all__ = [
    "CONTINUOUS_TRANSFORM_MODELS",
    "FittedTransform2D",
    "TRANSFORM_AFFINE",
    "TRANSFORM_POLYNOMIAL",
    "TRANSFORM_RIGID",
    "TRANSFORM_TRANSLATION",
    "fit_transform2d",
    "fitted_matrix_transform",
    "fitted_polynomial_transform",
    "minimum_points_for_transform",
    "normalize_polynomial_degree",
    "normalize_transform_model",
    "polynomial_design",
    "polynomial_term_count",
]
