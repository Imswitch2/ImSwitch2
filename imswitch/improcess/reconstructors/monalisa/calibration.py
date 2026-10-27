"""Canonical MoNaLISA calibration values, independent of how they were found.

This module deliberately contains no Qt/UI state.  Automatic localization,
manual entry and later session-locked calibration all converge on the same
objects before reconstruction.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .lattice import Lattice
from .scan_frame import ORIENTATIONS


@dataclass(frozen=True)
class PatternLocalization:
    """Illumination pattern geometry in camera pixels.

    ``lattice`` is the translation lattice. ``motif`` contains offsets from
    each lattice point, in camera pixels.  A normal Bravais lattice has the
    default one-point motif ``((0, 0),)``; an alternating/staggered manual
    grid can use a two-point motif without forcing the downstream extraction
    model to know about a special "hexagonal" mode.
    """

    lattice: Lattice
    motif: tuple[tuple[float, float], ...] = ((0.0, 0.0),)
    provenance: dict = field(default_factory=dict, compare=False)

    def __post_init__(self):
        if not self.motif:
            raise ValueError("PatternLocalization needs at least one motif point")
        motif = tuple((float(x), float(y)) for x, y in self.motif)
        object.__setattr__(self, "motif", motif)

    @classmethod
    def from_lattice(cls, lattice: Lattice, **provenance) -> "PatternLocalization":
        return cls(lattice=lattice, provenance=dict(provenance))

    @classmethod
    def manual_grid(
        cls,
        period_x: float,
        period_y: float,
        offset_x: float = 0.0,
        offset_y: float = 0.0,
        *,
        stagger_axis: str | None = None,
        stagger: float = 0.0,
        **provenance,
    ) -> "PatternLocalization":
        """Axis-aligned manual grid, optionally with alternating stagger.

        ``stagger`` is a signed fraction of the period along ``stagger_axis``.
        ``stagger_axis='x'`` shifts alternating rows in X; ``'y'`` shifts
        alternating columns in Y.
        """
        px, py = float(period_x), float(period_y)
        if px <= 0 or py <= 0:
            raise ValueError("Pattern periods must be positive")
        axis = None if stagger_axis is None else str(stagger_axis).lower()
        if axis in ("none", "") or float(stagger) == 0.0:
            return cls(
                lattice=Lattice.rectangular(px, py, offset_x, offset_y),
                provenance=dict(provenance),
            )
        s = float(stagger)
        # The overwhelmingly common +/- half-period stagger is itself a
        # Bravais lattice.  Represent it that way so scan-step completion,
        # commensurability and coverage keep their exact lattice semantics.
        # More general alternating shifts need a two-point motif.
        half_period = np.isclose(abs(s), 0.5, rtol=0.0, atol=1e-12)
        if axis == "x":
            if half_period:
                return cls(
                    lattice=Lattice(
                        a1=(px, 0.0), a2=(s * px, py),
                        offset=(float(offset_x), float(offset_y)),
                    ),
                    provenance=dict(provenance),
                )
            # Rows repeat every two Y periods; the second row is shifted in X.
            lattice = Lattice(
                a1=(px, 0.0), a2=(0.0, 2.0 * py),
                offset=(float(offset_x), float(offset_y)),
            )
            motif = ((0.0, 0.0), (s * px, py))
        elif axis == "y":
            if half_period:
                return cls(
                    lattice=Lattice(
                        a1=(px, s * py), a2=(0.0, py),
                        offset=(float(offset_x), float(offset_y)),
                    ),
                    provenance=dict(provenance),
                )
            # Columns repeat every two X periods; the second is shifted in Y.
            lattice = Lattice(
                a1=(2.0 * px, 0.0), a2=(0.0, py),
                offset=(float(offset_x), float(offset_y)),
            )
            motif = ((0.0, 0.0), (px, s * py))
        else:
            raise ValueError("stagger_axis must be None, 'x' or 'y'")
        return cls(lattice=lattice, motif=motif, provenance=dict(provenance))

    def points_in_frame(
        self, rows: int, cols: int, margin: float = 0.0
    ) -> tuple[np.ndarray, np.ndarray]:
        """Enumerate all motif points intersecting the requested frame margin."""
        xs, ys = [], []
        for mx, my in self.motif:
            shifted = Lattice(
                a1=self.lattice.a1,
                a2=self.lattice.a2,
                offset=(self.lattice.offset[0] + mx, self.lattice.offset[1] + my),
            )
            x, y = shifted.points_in_frame(rows, cols, margin=margin)
            xs.append(x)
            ys.append(y)
        x = np.concatenate(xs)
        y = np.concatenate(ys)
        if x.size <= 1:
            return x, y
        # Different motif copies may coincide at a boundary/equivalent point.
        rounded = np.round(np.column_stack([x, y]), 10)
        _, keep = np.unique(rounded, axis=0, return_index=True)
        keep.sort()
        return x[keep], y[keep]

    def to_dict(self) -> dict:
        """JSON-shaped representation used for transient/session transport."""
        return {
            "lattice": {
                "a1": [float(v) for v in self.lattice.a1],
                "a2": [float(v) for v in self.lattice.a2],
                "offset": [float(v) for v in self.lattice.offset],
            },
            "motif": [[float(x), float(y)] for x, y in self.motif],
            "provenance": dict(self.provenance),
        }

    @classmethod
    def from_dict(cls, value: dict | None) -> "PatternLocalization | None":
        if not value:
            return None
        lattice = dict(value.get("lattice") or {})
        return cls(
            lattice=Lattice(
                a1=tuple(float(v) for v in lattice["a1"]),
                a2=tuple(float(v) for v in lattice["a2"]),
                offset=tuple(float(v) for v in lattice.get("offset", (0.0, 0.0))),
            ),
            motif=tuple(
                (float(point[0]), float(point[1]))
                for point in (value.get("motif") or ((0.0, 0.0),))
            ),
            provenance=dict(value.get("provenance") or {}),
        )

    def manual_grid_parameters(self, *, axis_tolerance_px: float = 0.08) -> dict | None:
        """Return the Manual-grid controls when this localization can express them.

        The Manual panel deliberately models axis-aligned rectangular grids plus
        alternating X/Y stagger.  Automatic localization may eventually return a
        genuinely rotated/general lattice; in that case returning ``None`` is safer
        than silently changing the pattern when the user switches to Manual.
        """
        # Use the lattice's canonical legacy conversion first.  Automatic
        # detection may store an equivalent rectangular basis with X/Y vectors
        # swapped or signs changed; to_grid_params normalizes those cases and
        # folds the offset into the same unit cell the Manual controls use.
        if len(self.motif) == 1:
            try:
                xp, xo, yp, yo = self.lattice.to_grid_params(tol=0.02)
            except ValueError:
                pass
            else:
                return {
                    "period_x": xp,
                    "period_y": yp,
                    "offset_x": xo,
                    "offset_y": yo,
                    "stagger_axis": "None",
                    "stagger": 0.0,
                }

        a1 = np.asarray(self.lattice.a1, dtype=float)
        a2 = np.asarray(self.lattice.a2, dtype=float)
        offset_x, offset_y = (float(v) for v in self.lattice.offset)

        def close_zero(value: float) -> bool:
            return abs(float(value)) <= float(axis_tolerance_px)

        # Plain rectangular grid.
        if close_zero(a1[1]) and close_zero(a2[0]) and a1[0] > 0 and a2[1] > 0:
            if len(self.motif) == 1:
                return {
                    "period_x": float(a1[0]),
                    "period_y": float(a2[1]),
                    "offset_x": offset_x,
                    "offset_y": offset_y,
                    "stagger_axis": "None",
                    "stagger": 0.0,
                }
            if len(self.motif) == 2:
                second = np.asarray(self.motif[1], dtype=float)
                # X stagger: lattice repeats every two rows.
                if close_zero(second[1] - 0.5 * a2[1]) and abs(a1[0]) > 0:
                    return {
                        "period_x": float(a1[0]),
                        "period_y": float(0.5 * a2[1]),
                        "offset_x": offset_x,
                        "offset_y": offset_y,
                        "stagger_axis": "X",
                        "stagger": float(second[0] / a1[0]),
                    }
                # Y stagger: lattice repeats every two columns.
                if close_zero(second[0] - 0.5 * a1[0]) and abs(a2[1]) > 0:
                    return {
                        "period_x": float(0.5 * a1[0]),
                        "period_y": float(a2[1]),
                        "offset_x": offset_x,
                        "offset_y": offset_y,
                        "stagger_axis": "Y",
                        "stagger": float(second[1] / a2[1]),
                    }

        # Half-period X stagger as a Bravais lattice: a2=(s*px, py).
        if close_zero(a1[1]) and a1[0] > 0 and a2[1] > 0:
            stagger = float(a2[0] / a1[0])
            if np.isclose(abs(stagger), 0.5, atol=axis_tolerance_px / max(abs(a1[0]), 1e-12)):
                return {
                    "period_x": float(a1[0]),
                    "period_y": float(a2[1]),
                    "offset_x": offset_x,
                    "offset_y": offset_y,
                    "stagger_axis": "X",
                    "stagger": stagger,
                }

        # Half-period Y stagger as a Bravais lattice: a1=(px, s*py).
        if close_zero(a2[0]) and a1[0] > 0 and a2[1] > 0:
            stagger = float(a1[1] / a2[1])
            if np.isclose(abs(stagger), 0.5, atol=axis_tolerance_px / max(abs(a2[1]), 1e-12)):
                return {
                    "period_x": float(a1[0]),
                    "period_y": float(a2[1]),
                    "offset_x": offset_x,
                    "offset_y": offset_y,
                    "stagger_axis": "Y",
                    "stagger": stagger,
                }
        return None


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
