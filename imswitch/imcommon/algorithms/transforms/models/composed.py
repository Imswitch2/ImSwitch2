"""A chain of transforms applied in order.

Frame-graph composition has to survive *heterogeneous* chains: an affine
followed by a polynomial is neither, and there is no matrix for it. This model
is the honest fallback -- it simply chains ``map_points``, which is the one
primitive every model is required to provide.

Chains that happen to be all-affine collapse to a single matrix instead of
landing here (see :meth:`SpatialTransform.then`), because losing the matrix
would also lose the non-resampling display path.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import numpy as np

from ..base import TransformModel, model_from_params, register_model

__all__ = ["ComposedTransform"]


@register_model
class ComposedTransform(TransformModel):
    """Apply ``steps`` left to right: ``steps[-1](... steps[0](points))``."""

    kind = "composed"

    def __init__(self, steps: Sequence[TransformModel]) -> None:
        steps = tuple(steps)
        if not steps:
            raise ValueError("a composed transform needs at least one step")
        ndims = {step.ndim for step in steps}
        if len(ndims) != 1:
            raise ValueError(
                f"composed steps must agree on dimensionality, got {sorted(ndims)}"
            )
        self._steps = steps

    def __repr__(self) -> str:
        return f"ComposedTransform({list(self._steps)!r})"

    @property
    def ndim(self) -> int:
        return self._steps[0].ndim

    @property
    def steps(self) -> tuple[TransformModel, ...]:
        return self._steps

    def map_points(self, points: Any) -> np.ndarray:
        result = points
        for step in self._steps:
            result = step.map_points(result)
        return np.asarray(result, dtype=np.float64)

    def inverse(self) -> "ComposedTransform":
        return ComposedTransform([step.inverse() for step in reversed(self._steps)])

    def as_matrix(self) -> np.ndarray | None:
        matrices = [step.as_matrix() for step in self._steps]
        if any(matrix is None for matrix in matrices):
            return None
        result = np.eye(self.ndim + 1, dtype=np.float64)
        for matrix in matrices:
            result = matrix @ result
        return result

    @property
    def is_axis_aligned(self) -> bool:
        return all(step.is_axis_aligned for step in self._steps)

    @property
    def has_analytic_inverse(self) -> bool:
        return all(step.has_analytic_inverse for step in self._steps)

    def to_params(self) -> dict[str, Any]:
        return {
            "steps": [
                {"kind": step.kind, "params": step.to_params()} for step in self._steps
            ]
        }

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> "ComposedTransform":
        steps = params.get("steps")
        if not steps:
            raise ValueError("composed transform is missing its steps")
        return cls(
            [model_from_params(step["kind"], step.get("params", {})) for step in steps]
        )
