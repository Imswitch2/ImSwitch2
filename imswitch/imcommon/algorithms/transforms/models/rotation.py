"""Lossless multiple-of-90-degree rotation.

This is the "just a rotation for a simple detector" case, and it exists as its
own kind rather than as an affine because it can be done *exactly*: mapping
pixel centres onto pixel centres means ``np.rot90`` reproduces it with no
interpolation, no edge handling and no dtype promotion. Expressing the same
rotation as a general affine would resample it, which is both slower and lossy.

An image rotation is only defined relative to a specific input extent -- the
matrix depends on the width and height, because the origin moves. ``input_shape``
is therefore part of the model, not an argument to it. Getting this wrong is a
classic off-by-one source, so it is stored explicitly and validated on apply.

Flips are deliberately not folded in here. The full dihedral group (rotations
plus reflections) is a natural extension, but "rotation" that silently mirrors
is the kind of naming that causes rig bugs; a reflection kind can be added
alongside when something needs it.
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from ..base import TransformModel, as_points, register_model

__all__ = ["Rotation90Transform"]


@register_model
class Rotation90Transform(TransformModel):
    """Rotate by ``k`` multiples of 90 degrees, in ``np.rot90``'s direction.

    ``k`` counts rotations from the row axis toward the column axis, matching
    ``np.rot90(image, k, axes=(-2, -1))`` exactly.
    """

    kind = "rotation90"

    def __init__(self, k: int, input_shape: tuple[int, int]) -> None:
        shape = tuple(int(size) for size in input_shape)
        if len(shape) != 2:
            raise ValueError(f"input_shape must be (rows, cols), got {input_shape!r}")
        if any(size <= 0 for size in shape):
            raise ValueError(f"input_shape must be positive, got {shape}")
        self._k = int(k) % 4
        self._input_shape = shape

    def __repr__(self) -> str:
        return f"Rotation90Transform(k={self._k}, input_shape={self._input_shape})"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Rotation90Transform)
            and other._k == self._k
            and other._input_shape == self._input_shape
        )

    def __hash__(self) -> int:
        return hash((self.kind, self._k, self._input_shape))

    @property
    def ndim(self) -> int:
        return 2

    @property
    def k(self) -> int:
        return self._k

    @property
    def input_shape(self) -> tuple[int, int]:
        return self._input_shape

    @property
    def output_shape(self) -> tuple[int, int]:
        rows, cols = self._input_shape
        return (rows, cols) if self._k % 2 == 0 else (cols, rows)

    def as_matrix(self) -> np.ndarray:
        """Homogeneous ``(row, col, 1)`` matrix for this rotation.

        Derived directly from ``np.rot90``'s indexing, with ``H, W`` the input
        rows and columns:

        ===  ==========================  =============
        k    (r, c) maps to              output shape
        ===  ==========================  =============
        0    (r, c)                      (H, W)
        1    (W - 1 - c, r)              (W, H)
        2    (H - 1 - r, W - 1 - c)      (H, W)
        3    (c, H - 1 - r)              (W, H)
        ===  ==========================  =============
        """
        rows, cols = self._input_shape
        if self._k == 0:
            return np.eye(3, dtype=np.float64)
        if self._k == 1:
            return np.array(
                [[0.0, -1.0, cols - 1.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]],
                dtype=np.float64,
            )
        if self._k == 2:
            return np.array(
                [[-1.0, 0.0, rows - 1.0], [0.0, -1.0, cols - 1.0], [0.0, 0.0, 1.0]],
                dtype=np.float64,
            )
        return np.array(
            [[0.0, 1.0, 0.0], [-1.0, 0.0, rows - 1.0], [0.0, 0.0, 1.0]],
            dtype=np.float64,
        )

    def map_points(self, points: Any) -> np.ndarray:
        matrix = self.as_matrix()
        array = as_points(points, 2)
        return array @ matrix[:2, :2].T + matrix[:2, 2]

    def inverse(self) -> "Rotation90Transform":
        # The inverse rotation starts from *this* rotation's output extent.
        return Rotation90Transform(-self._k, self.output_shape)

    @property
    def is_axis_aligned(self) -> bool:
        return True

    def to_params(self) -> dict[str, Any]:
        return {"k": self._k, "input_shape": list(self._input_shape)}

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> "Rotation90Transform":
        try:
            input_shape = params["input_shape"]
        except KeyError:
            raise ValueError(
                "rotation90 needs 'input_shape'; an image rotation is only "
                "defined relative to a specific input extent"
            ) from None
        return cls(k=int(params.get("k", 0)), input_shape=tuple(input_shape))

    def warp_fast(
        self,
        image: np.ndarray,
        out_shape: tuple[int, ...],
        *,
        order: int = 1,
        cval: float = 0.0,
    ) -> np.ndarray | None:
        if tuple(image.shape[-2:]) != self._input_shape:
            # The stored extent is what makes the matrix correct; a mismatch
            # means the caller is applying this to something it was not
            # calibrated for. Fall through so the generic path handles it
            # under the model's own (still self-consistent) matrix.
            return None
        if tuple(out_shape) != self.output_shape:
            return None
        return np.ascontiguousarray(np.rot90(image, self._k, axes=(-2, -1)))
