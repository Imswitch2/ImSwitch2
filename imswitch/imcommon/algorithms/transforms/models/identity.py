"""The do-nothing transform.

Present as a real registered kind rather than a special case, so that "no
transform" travels through the same wrappers, the same file format and the same
frame bookkeeping as everything else. A caller should never have to branch on
whether a transform exists.
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from ..base import TransformModel, as_points, register_model

__all__ = ["IdentityTransform"]


@register_model
class IdentityTransform(TransformModel):
    """Maps every point to itself."""

    kind = "identity"

    def __init__(self, ndim: int = 2) -> None:
        ndim = int(ndim)
        if ndim < 1:
            raise ValueError(f"ndim must be at least 1, got {ndim}")
        self._ndim = ndim

    def __repr__(self) -> str:
        return f"IdentityTransform(ndim={self._ndim})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, IdentityTransform) and other._ndim == self._ndim

    def __hash__(self) -> int:
        return hash((self.kind, self._ndim))

    @property
    def ndim(self) -> int:
        return self._ndim

    def map_points(self, points: Any) -> np.ndarray:
        return as_points(points, self._ndim).copy()

    def inverse(self) -> "IdentityTransform":
        return self

    def as_matrix(self) -> np.ndarray:
        return np.eye(self._ndim + 1, dtype=np.float64)

    @property
    def is_axis_aligned(self) -> bool:
        return True

    def to_params(self) -> dict[str, Any]:
        return {"ndim": self._ndim}

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> "IdentityTransform":
        return cls(ndim=int(params.get("ndim", 2)))

    def warp_fast(
        self,
        image: np.ndarray,
        out_shape: tuple[int, ...],
        *,
        order: int = 1,
        cval: float = 0.0,
    ) -> np.ndarray | None:
        # Only a shortcut when the output extent matches; a different out_shape
        # is a crop/pad, which the generic path already does correctly.
        if tuple(image.shape[-self._ndim:]) != tuple(out_shape):
            return None
        # Copy rather than alias: every other path returns a fresh array, and a
        # caller that mutates the result must not corrupt its input.
        return image.copy()
