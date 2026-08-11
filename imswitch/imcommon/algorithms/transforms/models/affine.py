"""General affine transform.

Stored as one homogeneous ``(ndim+1, ndim+1)`` matrix in ``(..., y, x, 1)``
order, forward (source -> target). Validation mirrors
``imcommon.algorithms.detector_transform``: affine-homogeneous last row, finite,
non-singular -- a singular matrix cannot be inverted, and every resampling path
needs the inverse.
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from ..base import TransformModel, as_points, register_model

__all__ = ["AffineTransform"]


@register_model
class AffineTransform(TransformModel):
    """An invertible affine mapping."""

    kind = "affine"

    def __init__(self, matrix: Any) -> None:
        self._matrix = _validate_matrix(matrix)
        self._ndim = self._matrix.shape[0] - 1

    def __repr__(self) -> str:
        return f"AffineTransform(ndim={self._ndim}, matrix={self._matrix.tolist()!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, AffineTransform) and np.array_equal(
            other._matrix, self._matrix
        )

    def __hash__(self) -> int:
        return hash((self.kind, self._matrix.tobytes()))

    @property
    def ndim(self) -> int:
        return self._ndim

    @property
    def matrix(self) -> np.ndarray:
        """A fresh copy of the homogeneous matrix."""
        return self._matrix.copy()

    @classmethod
    def from_linear_offset(cls, linear: Any, offset: Any) -> "AffineTransform":
        """Build from an ``(ndim, ndim)`` linear part and an ``(ndim,)`` offset."""
        linear = np.asarray(linear, dtype=np.float64)
        offset = np.asarray(offset, dtype=np.float64).reshape(-1)
        ndim = linear.shape[0]
        matrix = np.eye(ndim + 1, dtype=np.float64)
        matrix[:ndim, :ndim] = linear
        matrix[:ndim, ndim] = offset
        return cls(matrix)

    @classmethod
    def translation(cls, offset: Any) -> "AffineTransform":
        """Build a pure translation."""
        offset = np.asarray(offset, dtype=np.float64).reshape(-1)
        return cls.from_linear_offset(np.eye(len(offset)), offset)

    def map_points(self, points: Any) -> np.ndarray:
        array = as_points(points, self._ndim)
        linear = self._matrix[: self._ndim, : self._ndim]
        offset = self._matrix[: self._ndim, self._ndim]
        return array @ linear.T + offset

    def inverse(self) -> "AffineTransform":
        return AffineTransform(np.linalg.inv(self._matrix))

    def as_matrix(self) -> np.ndarray:
        return self.matrix

    def to_params(self) -> dict[str, Any]:
        return {"matrix": [list(row) for row in self._matrix.tolist()]}

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> "AffineTransform":
        try:
            matrix = params["matrix"]
        except KeyError:
            raise ValueError("affine transform is missing its matrix") from None
        return cls(matrix)

    def warp_fast(
        self,
        image: np.ndarray,
        out_shape: tuple[int, ...],
        *,
        order: int = 1,
        cval: float = 0.0,
    ) -> np.ndarray | None:
        from scipy.ndimage import affine_transform as ndimage_affine

        ndim = self._ndim
        if image.ndim < ndim:
            return None

        # ndimage computes output[o] = input[matrix @ o + offset], i.e. it wants
        # the *inverse* (target -> source) mapping. Inverting here, once, is why
        # no caller of this module ever has to think about it.
        inverse = np.linalg.inv(self._matrix)
        linear = inverse[:ndim, :ndim]
        offset = inverse[:ndim, ndim]

        spatial_shape = image.shape[-ndim:]
        leading_shape = image.shape[:-ndim]

        def resample(plane: np.ndarray) -> np.ndarray:
            return ndimage_affine(
                plane,
                linear,
                offset=offset,
                output_shape=tuple(out_shape),
                order=order,
                mode="constant",
                cval=cval,
            )

        if not leading_shape:
            return resample(image)

        planes = image.reshape(-1, *spatial_shape)
        stacked = np.stack([resample(plane) for plane in planes], axis=0)
        return stacked.reshape(*leading_shape, *tuple(out_shape))


def _validate_matrix(value: Any) -> np.ndarray:
    try:
        matrix = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ValueError("affine matrix must contain numeric values") from exc

    if matrix.ndim != 2 or matrix.shape[0] != matrix.shape[1] or matrix.shape[0] < 3:
        raise ValueError(
            "affine matrix must be square homogeneous of size >= 3, got shape "
            f"{tuple(matrix.shape)}"
        )
    if not np.all(np.isfinite(matrix)):
        raise ValueError("affine matrix contains a non-finite value")

    ndim = matrix.shape[0] - 1
    expected_last_row = np.zeros(ndim + 1, dtype=np.float64)
    expected_last_row[-1] = 1.0
    if not np.allclose(matrix[ndim], expected_last_row, rtol=0.0, atol=1e-12):
        raise ValueError(
            "affine matrix must be homogeneous with last row "
            f"{expected_last_row.tolist()}, got {matrix[ndim].tolist()}"
        )
    if abs(float(np.linalg.det(matrix[:ndim, :ndim]))) <= np.finfo(np.float64).eps:
        raise ValueError("affine matrix is singular and cannot be inverted")

    return matrix
