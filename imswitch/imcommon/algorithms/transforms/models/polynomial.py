"""Normalized two-dimensional polynomial spatial transform.

The model follows the same numerical pattern as SLMcore's FOV correction:
source coordinates are centered and scaled to the calibration extent before
polynomial terms are evaluated.  This keeps quadratic/cubic/quartic fits well
conditioned even for detector coordinates in the thousands of pixels.

Important host limitation
-------------------------
Polynomial transforms are fully supported for point mapping, persistence,
diagnostics and generic image resampling.  They are *not* currently available
for ImControl's live display Apply path, because that path intentionally uses
Napari's resample-free ``layer.affine`` property.  Enabling a polynomial live
correction requires an explicit nonlinear live-frame resampling path; callers
must not approximate this model with an affine matrix.
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from ..base import TransformModel, as_points, register_model

__all__ = ["PolynomialTransform", "polynomial_terms", "polynomial_term_count"]


def polynomial_term_count(degree: int) -> int:
    """Number of 2-D monomials with total degree <= ``degree``."""
    degree = int(degree)
    if degree < 1:
        raise ValueError("polynomial degree must be >= 1")
    return (degree + 1) * (degree + 2) // 2


def polynomial_terms(y: np.ndarray, x: np.ndarray, degree: int) -> np.ndarray:
    """Return the normalized 2-D polynomial design matrix.

    Ordering is ``1, y, x, y², yx, x², ...`` and then descending powers of
    ``y`` for each higher total degree.  The ordering is stable and serialized
    implicitly by ``degree``.
    """
    degree = int(degree)
    if degree < 1 or degree > 4:
        raise ValueError("polynomial degree must be between 1 and 4")
    columns = [np.ones_like(y), y, x]
    if degree >= 2:
        columns.extend((y * y, y * x, x * x))
    if degree >= 3:
        for total in range(3, degree + 1):
            for py in range(total, -1, -1):
                px = total - py
                columns.append((y**py) * (x**px))
    return np.column_stack(columns)


@register_model
class PolynomialTransform(TransformModel):
    """Forward 2-D polynomial mapping in ``(row, col)`` coordinates.

    ``coefficients`` has shape ``(2, n_terms)`` and maps normalized source
    coordinates directly to target pixel coordinates.  ``domain_min`` and
    ``domain_max`` retain the calibration coverage for visualization and
    extrapolation warnings; they do not clamp evaluation.
    """

    kind = "polynomial"

    def __init__(
        self,
        *,
        degree: int,
        coefficients: Any,
        normalization_center: Any,
        normalization_scale: Any,
        domain_min: Any | None = None,
        domain_max: Any | None = None,
    ) -> None:
        degree = int(degree)
        if degree < 1 or degree > 4:
            raise ValueError("polynomial degree must be between 1 and 4")
        center = np.asarray(normalization_center, dtype=np.float64).reshape(-1)
        scale = np.asarray(normalization_scale, dtype=np.float64).reshape(-1)
        if center.shape != (2,) or scale.shape != (2,):
            raise ValueError("polynomial normalization center/scale must have shape (2,)")
        if not np.all(np.isfinite(center)) or not np.all(np.isfinite(scale)):
            raise ValueError("polynomial normalization contains non-finite values")
        if np.any(scale <= 0.0):
            raise ValueError("polynomial normalization scale must be > 0")

        coeff = np.asarray(coefficients, dtype=np.float64)
        expected = polynomial_term_count(degree)
        if coeff.shape != (2, expected):
            raise ValueError(
                f"polynomial coefficients must have shape (2, {expected}), "
                f"got {coeff.shape}"
            )
        if not np.all(np.isfinite(coeff)):
            raise ValueError("polynomial coefficients contain non-finite values")

        if domain_min is None:
            domain_min = center - scale
        if domain_max is None:
            domain_max = center + scale
        low = np.asarray(domain_min, dtype=np.float64).reshape(-1)
        high = np.asarray(domain_max, dtype=np.float64).reshape(-1)
        if low.shape != (2,) or high.shape != (2,):
            raise ValueError("polynomial domain bounds must have shape (2,)")
        if not np.all(np.isfinite(low)) or not np.all(np.isfinite(high)):
            raise ValueError("polynomial domain bounds contain non-finite values")
        if np.any(high < low):
            raise ValueError("polynomial domain_max must be >= domain_min")

        self.degree = degree
        self._coefficients = np.array(coeff, copy=True)
        self._center = np.array(center, copy=True)
        self._scale = np.array(scale, copy=True)
        self._domain_min = np.array(low, copy=True)
        self._domain_max = np.array(high, copy=True)

    @property
    def ndim(self) -> int:
        return 2

    @property
    def coefficients(self) -> np.ndarray:
        return self._coefficients.copy()

    @property
    def normalization_center(self) -> np.ndarray:
        return self._center.copy()

    @property
    def normalization_scale(self) -> np.ndarray:
        return self._scale.copy()

    @property
    def domain_min(self) -> np.ndarray:
        return self._domain_min.copy()

    @property
    def domain_max(self) -> np.ndarray:
        return self._domain_max.copy()

    @property
    def has_analytic_inverse(self) -> bool:
        return False

    @classmethod
    def fit(
        cls,
        source_points: Any,
        target_points: Any,
        *,
        degree: int = 2,
    ) -> "PolynomialTransform":
        """Least-squares polynomial fit with SLMcore-style coordinate normalization."""
        source = as_points(source_points, 2, label="source_points")
        target = as_points(target_points, 2, label="target_points")
        if source.shape != target.shape:
            raise ValueError("source and target polynomial samples must have matching shapes")
        degree = int(degree)
        required = polynomial_term_count(degree)
        if len(source) < required:
            raise ValueError(
                f"not enough samples for polynomial degree {degree}: "
                f"need at least {required}, got {len(source)}"
            )

        center = np.mean(source, axis=0)
        span = np.ptp(source, axis=0)
        scale = np.where(span > 1e-12, 0.5 * span, 1.0)
        normalized = (source - center) / scale
        design = polynomial_terms(normalized[:, 0], normalized[:, 1], degree)
        coefficients, _residuals, rank, _singular = np.linalg.lstsq(
            design, target, rcond=None
        )
        if int(rank) < design.shape[1]:
            raise ValueError(
                f"polynomial degree {degree} samples are rank-deficient "
                f"({rank}/{design.shape[1]})"
            )
        return cls(
            degree=degree,
            coefficients=coefficients.T,
            normalization_center=center,
            normalization_scale=scale,
            domain_min=np.min(source, axis=0),
            domain_max=np.max(source, axis=0),
        )

    def map_points(self, points: Any) -> np.ndarray:
        source = as_points(points, 2)
        normalized = (source - self._center) / self._scale
        design = polynomial_terms(normalized[:, 0], normalized[:, 1], self.degree)
        return design @ self._coefficients.T

    def extrapolation_mask(self, points: Any, *, tolerance: float = 0.0) -> np.ndarray:
        """Return points outside the fitted source-coordinate bounding box."""
        source = as_points(points, 2)
        tolerance = max(0.0, float(tolerance))
        margin = tolerance * np.maximum(self._domain_max - self._domain_min, 1.0)
        return np.any(
            (source < (self._domain_min - margin))
            | (source > (self._domain_max + margin)),
            axis=1,
        )

    def inverse(self) -> "PolynomialTransform":
        """Return a numerical polynomial approximation of the inverse mapping.

        A regular calibration-domain sample is mapped forward and the same
        polynomial degree is fitted in reverse.  This is sufficient for the
        generic offline resampling path, but is intentionally *not* used by
        ImControl's live Apply path (see module docstring).
        """
        # The inverse of a degree-N polynomial is not generally degree N.  Fit
        # the richest currently supported inverse basis over a dense domain so
        # the generic offline resampler gets a useful approximation without
        # claiming analytic exactness.
        inverse_degree = min(4, max(2, self.degree + 2))
        counts = max(25, 6 * inverse_degree + 5)
        ys = np.linspace(self._domain_min[0], self._domain_max[0], counts)
        xs = np.linspace(self._domain_min[1], self._domain_max[1], counts)
        yy, xx = np.meshgrid(ys, xs, indexing="ij")
        source = np.column_stack([yy.ravel(), xx.ravel()])
        target = self.map_points(source)
        return PolynomialTransform.fit(target, source, degree=inverse_degree)

    def to_params(self) -> dict[str, Any]:
        return {
            "degree": int(self.degree),
            "coefficients": self._coefficients.tolist(),
            "normalization_center": self._center.tolist(),
            "normalization_scale": self._scale.tolist(),
            "domain_min": self._domain_min.tolist(),
            "domain_max": self._domain_max.tolist(),
        }

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> "PolynomialTransform":
        required = (
            "degree",
            "coefficients",
            "normalization_center",
            "normalization_scale",
        )
        missing = [name for name in required if name not in params]
        if missing:
            raise ValueError(
                "polynomial transform is missing " + ", ".join(repr(name) for name in missing)
            )
        return cls(
            degree=int(params["degree"]),
            coefficients=params["coefficients"],
            normalization_center=params["normalization_center"],
            normalization_scale=params["normalization_scale"],
            domain_min=params.get("domain_min"),
            domain_max=params.get("domain_max"),
        )
