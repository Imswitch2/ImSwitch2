"""MoNaLISA calibration compatibility API and scan orientation.

Numerical pattern geometry is owned by ``imcommon``; the scan-frame specific
``ScanOrientation`` remains in ImProcess. Historic imports are preserved.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from imswitch.imcommon.algorithms.grid_localization.periodic.pattern import PatternLocalization
from .scan_frame import ORIENTATIONS


@dataclass(frozen=True)
class ScanOrientation:
    """Canonical stage/scan-axis to camera-axis signed permutation."""

    matrix: tuple[tuple[int, int], tuple[int, int]]

    @classmethod
    def from_literal(cls, literal: str) -> "ScanOrientation":
        if literal not in ORIENTATIONS:
            raise ValueError(
                f"Unknown orientation {literal!r}; expected one of {ORIENTATIONS}"
            )
        sign = {"+": 1, "-": -1}
        # Columns are the first and second scan axes expressed in camera X/Y.
        first_axis = 0 if literal[1] == "x" else 1
        second_axis = 0 if literal[3] == "x" else 1
        matrix = np.zeros((2, 2), dtype=int)
        matrix[first_axis, 0] = sign[literal[0]]
        matrix[second_axis, 1] = sign[literal[2]]
        return cls(tuple(tuple(int(v) for v in row) for row in matrix))

    def as_array(self) -> np.ndarray:
        return np.asarray(self.matrix, dtype=int)

    def to_literal(self) -> str:
        matrix = self.as_array()
        parts = []
        for column in range(2):
            rows = np.flatnonzero(matrix[:, column])
            if rows.size != 1 or abs(int(matrix[rows[0], column])) != 1:
                raise ValueError("Orientation matrix is not a signed axis permutation")
            row = int(rows[0])
            parts.append(("+" if matrix[row, column] > 0 else "-") + "xy"[row])
        literal = "".join(parts)
        if literal not in ORIENTATIONS:
            raise ValueError("Orientation matrix maps both scan axes to one camera axis")
        return literal

    def orient_index(self, index: np.ndarray) -> np.ndarray:
        index = np.asarray(index).reshape(-1, 2)
        return index @ self.as_array().T
