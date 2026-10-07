"""Fitting a transform model to corresponding points.

Three things are deliberately separate here:

* **Correspondence finding** is pluggable (see :mod:`.correspondence`). Whether
  the pairs came from a foci grid, matched beads, phase correlation or
  hand-clicked points, they arrive as two ``(N, 2)`` arrays.
* **Which model class to fit** is a choice, made per calibration. A free affine
  is not always what you want: if two detectors differ only by a shift, fitting
  six parameters to describe two lets noise into the four that should be fixed.
  Fewer degrees of freedom means a better-conditioned fit from fewer points.
* **Scoring** -- RANSAC, residuals, inliers -- is shared by all of them, so
  every model is judged the same way and its residuals mean the same thing.

The rigid/similarity/translation estimators all *produce* an affine, because
their constraints live in the fit rather than in a separate stored matrix
type. Polynomial fitting is the deliberate nonlinear exception: it produces a
normalized :class:`PolynomialTransform` and is scored through the same robust
residual/inlier contract as the linear estimators.

Coordinates are this module's ``(row, col)`` convention throughout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import numpy as np

from .base import TransformModel, as_points
from .model import ResidualStats
from .parameters import ParameterSpec, normalize_parameter_values, parameter_defaults
from .models.affine import AffineTransform
from .models.identity import IdentityTransform
from .models.polynomial import PolynomialTransform, polynomial_term_count
from .models.rotation import Rotation90Transform

__all__ = [
    "AffineFit",
    "EstimatorSpec",
    "ModelFit",
    "estimator_specs",
    "estimator_names",
    "fit_transform",
    "estimate_affine",
    "fit_parameter_specs",
    "fit_parameter_defaults",
    "normalize_fit_parameters",
]


@dataclass(frozen=True)
class EstimatorSpec:
    """One fittable model class, what it costs to fit, and its UI parameters."""

    name: str
    label: str
    dof: int
    min_samples: int
    description: str
    parameter_specs: Mapping[str, ParameterSpec] = field(default_factory=dict)

    def parameter_defaults(self) -> dict[str, Any]:
        return parameter_defaults(self.parameter_specs)

    def normalize_parameters(self, values: Mapping[str, Any] | None) -> dict[str, Any]:
        return normalize_parameter_values(self.parameter_specs, values)


_RANSAC_PARAMETER_SPECS = {
    "residual_threshold": ParameterSpec(
        2.0, float, "Inlier residual threshold (px)",
        "Maximum point-to-point residual counted as an inlier during robust fitting. "
        "Decrease it to demand tighter agreement; increase it when localization noise "
        "is genuinely larger.",
        minimum=0.001, step=0.1, decimals=3,
    ),
    "max_trials": ParameterSpec(
        500, int, "Maximum RANSAC trials",
        "Maximum number of random consensus trials. Higher values can help when many "
        "correspondences are outliers but make fitting slower.",
        minimum=1, step=50, advanced=True,
    ),
}

_POLYNOMIAL_PARAMETER_SPECS = {
    "degree": ParameterSpec(
        2, int, "Polynomial degree",
        "Spatial polynomial order. Degree 2 is the recommended default; degrees "
        "3 and 4 can model stronger field-dependent distortion but are easier to "
        "overfit and should be judged with the transform-field preview.",
        minimum=2, maximum=4, step=1, choices=(2, 3, 4), group="Polynomial",
    ),
    **_RANSAC_PARAMETER_SPECS,
}


ESTIMATORS: dict[str, EstimatorSpec] = {
    "affine": EstimatorSpec(
        name="affine",
        label="Affine (6 DOF)",
        dof=6,
        min_samples=3,
        description=(
            "Rotation, anisotropic scale, shear and translation. The general "
            "case; use it when the two views may differ in aspect or skew."
        ),
        parameter_specs=_RANSAC_PARAMETER_SPECS,
    ),
    "polynomial": EstimatorSpec(
        name="polynomial",
        label="Polynomial",
        dof=12,
        min_samples=6,
        description=(
            "Normalized 2-D polynomial mapping for field-dependent distortion. "
            "Degree 2/3/4 uses 6/10/15 terms per output coordinate. Fitting, "
            "saving and inspection are supported; ImControl live Apply is not "
            "available yet because the current live path is affine-only."
        ),
        parameter_specs=_POLYNOMIAL_PARAMETER_SPECS,
    ),
    "similarity": EstimatorSpec(
        name="similarity",
        label="Similarity (4 DOF)",
        dof=4,
        min_samples=2,
        description=(
            "Rotation, one uniform scale and translation. The right choice for "
            "two detectors that differ in magnification but share square pixels."
        ),
        parameter_specs=_RANSAC_PARAMETER_SPECS,
    ),
    "rigid": EstimatorSpec(
        name="rigid",
        label="Rigid (3 DOF)",
        dof=3,
        min_samples=2,
        description=(
            "Rotation and translation, no scaling. For two views at the same "
            "magnification, where any fitted scale would be noise."
        ),
        parameter_specs=_RANSAC_PARAMETER_SPECS,
    ),
    "translation": EstimatorSpec(
        name="translation",
        label="Translation (2 DOF)",
        dof=2,
        min_samples=1,
        description=(
            "A pure shift. The most constrained continuous fit, and often the "
            "honest one for two cameras on the same optical path."
        ),
        parameter_specs=_RANSAC_PARAMETER_SPECS,
    ),
    "rotation90": EstimatorSpec(
        name="rotation90",
        label="Rotation, multiples of 90 deg",
        dof=0,
        min_samples=1,
        description=(
            "Picks the best of the four quarter turns. Discrete, so it is exact "
            "and lossless -- no interpolation is ever applied."
        ),
    ),
    "identity": EstimatorSpec(
        name="identity",
        label="Identity (no transform)",
        dof=0,
        min_samples=1,
        description=(
            "Fits nothing and reports what the residuals would be with no "
            "transform at all. Use it to measure how far apart two spaces are "
            "before deciding whether a calibration is needed."
        ),
    ),
}


def estimator_specs() -> tuple[EstimatorSpec, ...]:
    """Every fittable model class, in a sensible order for a chooser."""
    return tuple(ESTIMATORS.values())


def estimator_names() -> tuple[str, ...]:
    """Names accepted by :func:`fit_transform`."""
    return tuple(ESTIMATORS)


def fit_parameter_specs(kind: str) -> Mapping[str, ParameterSpec]:
    """Return the user-facing fit-parameter specs for one estimator."""
    try:
        return ESTIMATORS[str(kind)].parameter_specs
    except KeyError:
        raise ValueError(
            f"unknown estimator {kind!r}; known estimators: {sorted(ESTIMATORS)}"
        ) from None


def fit_parameter_defaults(kind: str) -> dict[str, Any]:
    """Return a fresh fit-parameter mapping for one estimator."""
    try:
        return ESTIMATORS[str(kind)].parameter_defaults()
    except KeyError:
        raise ValueError(
            f"unknown estimator {kind!r}; known estimators: {sorted(ESTIMATORS)}"
        ) from None


def normalize_fit_parameters(
    kind: str, values: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Validate only parameters that belong to the selected transform model."""
    try:
        return ESTIMATORS[str(kind)].normalize_parameters(values)
    except KeyError:
        raise ValueError(
            f"unknown estimator {kind!r}; known estimators: {sorted(ESTIMATORS)}"
        ) from None


class ModelFit:
    """The outcome of :func:`fit_transform`."""

    __slots__ = ("transform", "residuals", "inliers", "mapped_points", "estimator")

    def __init__(
        self,
        transform: TransformModel,
        residuals: ResidualStats,
        inliers: np.ndarray,
        mapped_points: np.ndarray,
        estimator: str,
    ) -> None:
        self.transform = transform
        self.residuals = residuals
        self.inliers = inliers
        self.mapped_points = mapped_points
        self.estimator = estimator

    def __repr__(self) -> str:
        return f"ModelFit({self.estimator}, {self.residuals.summary()})"


# Retained so existing callers of the affine-only API keep working.
AffineFit = ModelFit


def fit_transform(
    kind: str,
    source_points: Any,
    target_points: Any,
    *,
    residual_threshold: float = 2.0,
    max_trials: int = 500,
    random_state: int | None = 0,
    input_shape: tuple[int, int] | None = None,
    degree: int = 2,
) -> ModelFit:
    """Fit ``kind`` to paired points and report how well it did.

    Continuous models are fitted through RANSAC, so a handful of mis-paired
    points cannot drag the result. When RANSAC finds no consensus the fit falls
    back to least squares over every pair and marks them all inliers -- the
    residuals then show how bad it is, rather than the failure being hidden.

    ``random_state`` is seeded by default so a calibration is reproducible;
    pass ``None`` for a non-deterministic fit. ``input_shape`` is required only
    by ``rotation90``, whose matrix depends on the extent it rotates.
    """
    try:
        spec = ESTIMATORS[str(kind)]
    except KeyError:
        raise ValueError(
            f"unknown estimator {kind!r}; known estimators: {sorted(ESTIMATORS)}"
        ) from None

    source = as_points(source_points, 2, label="source_points")
    target = as_points(target_points, 2, label="target_points")

    if source.shape != target.shape:
        raise ValueError(
            f"source and target must have matching shapes, got {source.shape} "
            f"and {target.shape}"
        )
    required_samples = (
        polynomial_term_count(int(degree))
        if spec.name == "polynomial"
        else spec.min_samples
    )
    if len(source) < required_samples:
        pairs = "pair" if required_samples == 1 else "pairs"
        raise ValueError(
            f"fitting {spec.name!r} needs at least {required_samples} point "
            f"{pairs}, got {len(source)}"
        )
    if residual_threshold <= 0:
        raise ValueError(
            f"residual_threshold must be positive, got {residual_threshold}"
        )

    if spec.name == "identity":
        model: TransformModel = IdentityTransform(ndim=2)
    elif spec.name == "rotation90":
        model = _fit_rotation90(source, target, input_shape)
    elif spec.name == "polynomial":
        model = _ransac_polynomial(
            source,
            target,
            degree=int(degree),
            threshold=float(residual_threshold),
            max_trials=int(max_trials),
            random_state=random_state,
        )
    else:
        model = AffineTransform(
            _ransac(
                source,
                target,
                fit_fn=_CLOSED_FORM[spec.name],
                min_samples=spec.min_samples,
                threshold=float(residual_threshold),
                max_trials=int(max_trials),
                random_state=random_state,
            )
        )

    mapped = model.map_points(source)
    per_point = np.linalg.norm(mapped - target, axis=1)
    inliers = per_point < float(residual_threshold)

    return ModelFit(
        transform=model,
        residuals=ResidualStats.from_residuals(
            per_point, inliers=inliers, threshold=float(residual_threshold)
        ),
        inliers=inliers,
        mapped_points=mapped,
        estimator=spec.name,
    )


def estimate_affine(
    source_points: Any,
    target_points: Any,
    *,
    residual_threshold: float = 2.0,
    max_trials: int = 500,
    random_state: int | None = 0,
) -> ModelFit:
    """Fit a robust affine mapping ``source -> target``.

    A thin wrapper over :func:`fit_transform` for the common case.
    """
    return fit_transform(
        "affine",
        source_points,
        target_points,
        residual_threshold=residual_threshold,
        max_trials=max_trials,
        random_state=random_state,
    )


# ---------------------------------------------------------------------------
# Closed-form solvers, all returning a 3x3 homogeneous (row, col) matrix
# ---------------------------------------------------------------------------


def _fit_affine(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    design = np.column_stack([source, np.ones(len(source))])
    solution, *_ = np.linalg.lstsq(design, target, rcond=None)
    matrix = np.eye(3, dtype=np.float64)
    matrix[:2, :] = solution.T
    return matrix


def _umeyama(
    source: np.ndarray, target: np.ndarray, *, with_scale: bool
) -> np.ndarray:
    """Least-squares similarity/rigid fit (Umeyama 1991).

    The reflection guard matters: without it a noisy or near-degenerate point
    set can produce a mirrored "rotation", which fits the samples but is
    physically wrong for a microscope.
    """
    source_mean = source.mean(axis=0)
    target_mean = target.mean(axis=0)
    source_centred = source - source_mean
    target_centred = target - target_mean

    covariance = (target_centred.T @ source_centred) / len(source)
    left, singular_values, right = np.linalg.svd(covariance)

    correction = np.eye(2, dtype=np.float64)
    if np.linalg.det(left) * np.linalg.det(right) < 0:
        correction[1, 1] = -1.0

    rotation = left @ correction @ right
    scale = 1.0
    if with_scale:
        variance = float((source_centred**2).sum() / len(source))
        if variance <= np.finfo(np.float64).eps:
            raise ValueError("cannot fit a scale to coincident source points")
        scale = float(np.trace(np.diag(singular_values) @ correction) / variance)
        if abs(scale) <= np.finfo(np.float64).eps:
            raise ValueError("degenerate scale in similarity fit")

    matrix = np.eye(3, dtype=np.float64)
    matrix[:2, :2] = scale * rotation
    matrix[:2, 2] = target_mean - (scale * rotation) @ source_mean
    return matrix


def _fit_similarity(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    return _umeyama(source, target, with_scale=True)


def _fit_rigid(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    return _umeyama(source, target, with_scale=False)


def _fit_translation(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    matrix = np.eye(3, dtype=np.float64)
    matrix[:2, 2] = (target - source).mean(axis=0)
    return matrix


_CLOSED_FORM: dict[str, Callable[[np.ndarray, np.ndarray], np.ndarray]] = {
    "affine": _fit_affine,
    "similarity": _fit_similarity,
    "rigid": _fit_rigid,
    "translation": _fit_translation,
}


def _fit_rotation90(
    source: np.ndarray, target: np.ndarray, input_shape: tuple[int, int] | None
) -> Rotation90Transform:
    """Pick whichever quarter turn puts the source points nearest the target.

    Discrete, so there is nothing to optimize -- all four candidates are tried
    and scored. ``input_shape`` is required because a quarter turn is only
    defined relative to the extent it turns.
    """
    if input_shape is None:
        raise ValueError(
            "rotation90 needs input_shape: a quarter turn is defined relative to "
            "a specific image extent, so the source image shape must be given"
        )

    best: tuple[float, Rotation90Transform] | None = None
    for k in range(4):
        candidate = Rotation90Transform(k=k, input_shape=input_shape)
        score = float(
            np.median(np.linalg.norm(candidate.map_points(source) - target, axis=1))
        )
        if best is None or score < best[0]:
            best = (score, candidate)

    assert best is not None
    return best[1]


# ---------------------------------------------------------------------------
# One RANSAC, shared by every continuous model
# ---------------------------------------------------------------------------


def _ransac(
    source: np.ndarray,
    target: np.ndarray,
    *,
    fit_fn: Callable[[np.ndarray, np.ndarray], np.ndarray],
    min_samples: int,
    threshold: float,
    max_trials: int,
    random_state: int | None,
) -> np.ndarray:
    """Robustly fit ``fit_fn``, returning the consensus model's matrix.

    Implemented here rather than delegating so that every model class is fitted
    and scored identically, and so the module does not depend on the details of
    any one library's estimator protocol.
    """
    count = len(source)
    if count == min_samples:
        return fit_fn(source, target)

    generator = np.random.default_rng(random_state)
    best_inliers = np.zeros(count, dtype=bool)

    for _ in range(max_trials):
        sample = generator.choice(count, min_samples, replace=False)
        try:
            candidate = fit_fn(source[sample], target[sample])
        except (ValueError, np.linalg.LinAlgError):
            continue

        mapped = source @ candidate[:2, :2].T + candidate[:2, 2]
        inliers = np.linalg.norm(mapped - target, axis=1) < threshold
        if inliers.sum() > best_inliers.sum():
            best_inliers = inliers
        if best_inliers.all():
            break

    if best_inliers.sum() >= min_samples:
        try:
            return fit_fn(source[best_inliers], target[best_inliers])
        except (ValueError, np.linalg.LinAlgError):
            pass

    # No consensus: fall back to every pair, and let the residuals say so.
    return fit_fn(source, target)


def _ransac_polynomial(
    source: np.ndarray,
    target: np.ndarray,
    *,
    degree: int,
    threshold: float,
    max_trials: int,
    random_state: int | None,
) -> PolynomialTransform:
    """Robustly fit a normalized polynomial using the shared inlier semantics."""
    min_samples = polynomial_term_count(degree)
    count = len(source)
    if count == min_samples:
        return PolynomialTransform.fit(source, target, degree=degree)

    generator = np.random.default_rng(random_state)
    best_inliers = np.zeros(count, dtype=bool)
    best_median = float("inf")

    for _ in range(max_trials):
        sample = generator.choice(count, min_samples, replace=False)
        try:
            candidate = PolynomialTransform.fit(
                source[sample], target[sample], degree=degree
            )
        except (ValueError, np.linalg.LinAlgError):
            continue
        residual = np.linalg.norm(candidate.map_points(source) - target, axis=1)
        inliers = residual < threshold
        n_inliers = int(np.sum(inliers))
        if n_inliers < min_samples:
            continue
        median = float(np.median(residual[inliers]))
        if (
            n_inliers > int(np.sum(best_inliers))
            or (n_inliers == int(np.sum(best_inliers)) and median < best_median)
        ):
            best_inliers = inliers
            best_median = median
        if best_inliers.all():
            break

    if int(np.sum(best_inliers)) >= min_samples:
        try:
            return PolynomialTransform.fit(
                source[best_inliers], target[best_inliers], degree=degree
            )
        except (ValueError, np.linalg.LinAlgError):
            pass
    return PolynomialTransform.fit(source, target, degree=degree)
