"""Finite-grid correspondence resolution between independently localized images.

This is an ImSwitch adaptation of the SLMcore reference-correspondence layer.
Localization and correspondence intentionally remain separate: each image may have
an internally valid finite-grid indexing, while the two index conventions can
still differ by an exact rectangular-grid symmetry (flip/rotation/transpose).

The resolver never moves or re-localizes a spot.  It only reindexes the target
localization, scores the finite set of topology-preserving alternatives, and
returns genuinely measured source/target pairs.  ``auto`` mirrors SLMcore's
policy: prefer non-reflected, low-rotation, low-deformation relationships, using
RMS only as a later tie-breaker.  A manual ordering bypasses that choice.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

import numpy as np


class GridOrdering(str, Enum):
    """Topology-preserving ways to number one already localized rectangle."""

    CANONICAL = "canonical"
    FLIP_X = "flip_x"
    FLIP_Y = "flip_y"
    ROTATE_180 = "rotate_180"
    TRANSPOSE = "transpose"
    ROTATE_90_CW = "rotate_90_cw"
    ROTATE_90_CCW = "rotate_90_ccw"
    ANTI_TRANSPOSE = "anti_transpose"

    @classmethod
    def normalize(cls, value: Any) -> "GridOrdering":
        if isinstance(value, cls):
            return value
        text = str(value or cls.CANONICAL.value).strip().lower()
        aliases = {
            "identity": cls.CANONICAL.value,
            "none": cls.CANONICAL.value,
            "flip_horizontal": cls.FLIP_X.value,
            "flip_vertical": cls.FLIP_Y.value,
            "180": cls.ROTATE_180.value,
            "90_cw": cls.ROTATE_90_CW.value,
            "90_ccw": cls.ROTATE_90_CCW.value,
        }
        return cls(aliases.get(text, text))


class GridCorrespondence(str, Enum):
    """Requested cross-image finite-grid correspondence."""

    AUTO = "auto"
    CANONICAL = "canonical"
    FLIP_X = "flip_x"
    FLIP_Y = "flip_y"
    ROTATE_180 = "rotate_180"
    TRANSPOSE = "transpose"
    ROTATE_90_CW = "rotate_90_cw"
    ROTATE_90_CCW = "rotate_90_ccw"
    ANTI_TRANSPOSE = "anti_transpose"

    @classmethod
    def normalize(cls, value: Any) -> "GridCorrespondence":
        if isinstance(value, cls):
            return value
        text = str(value or cls.AUTO.value).strip().lower()
        aliases = {"identity": cls.CANONICAL.value, "none": cls.CANONICAL.value}
        return cls(aliases.get(text, text))

    @property
    def ordering(self) -> GridOrdering | None:
        if self is GridCorrespondence.AUTO:
            return None
        return GridOrdering.normalize(self.value)


_ORDERING_LABELS = {
    GridOrdering.CANONICAL: "Canonical",
    GridOrdering.FLIP_X: "Flip X",
    GridOrdering.FLIP_Y: "Flip Y",
    GridOrdering.ROTATE_180: "Rotate 180°",
    GridOrdering.TRANSPOSE: "Swap X/Y",
    GridOrdering.ROTATE_90_CW: "Rotate 90° CW",
    GridOrdering.ROTATE_90_CCW: "Rotate 90° CCW",
    GridOrdering.ANTI_TRANSPOSE: "Swap X/Y + 180°",
}


def grid_ordering_label(value: GridOrdering | GridCorrespondence | str) -> str:
    if isinstance(value, GridCorrespondence):
        if value is GridCorrespondence.AUTO:
            return "Auto"
        value = value.ordering
    return _ORDERING_LABELS[GridOrdering.normalize(value)]


@dataclass(frozen=True)
class GridCorrespondenceResolution:
    """Diagnostics for one resolved source->target finite-grid pairing."""

    requested: GridCorrespondence
    resolved: GridOrdering
    relative_rotation_deg: float
    rms_px: float
    reflection: bool
    singular_values: tuple[float, float]
    deformation_score: float
    candidate_count: int
    pair_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested.value,
            "resolved": self.resolved.value,
            "resolved_label": grid_ordering_label(self.resolved),
            "relative_rotation_deg": float(self.relative_rotation_deg),
            "rms_px": float(self.rms_px),
            "reflection": bool(self.reflection),
            "singular_values": tuple(float(value) for value in self.singular_values),
            "deformation_score": float(self.deformation_score),
            "candidate_count": int(self.candidate_count),
            "pair_count": int(self.pair_count),
        }


def grid_ordering_permutation(
    lattice_indices: np.ndarray,
    ordering: GridOrdering | str,
) -> np.ndarray:
    """Return source-column indices for one rectangular-grid ordering.

    Input indices can be either ``(N, 2)`` or ``(2, N)``.  The returned
    permutation follows SLMcore's convention::

        new_values = old_values[permutation]
    """
    ordering = GridOrdering.normalize(ordering)
    indices = _indices_n2(lattice_indices)
    count = int(indices.shape[0])
    if count == 0 or ordering is GridOrdering.CANONICAL:
        return np.arange(count, dtype=np.int64)

    grid = _index_grid(indices)
    if ordering is GridOrdering.FLIP_X:
        transformed = np.fliplr(grid)
    elif ordering is GridOrdering.FLIP_Y:
        transformed = np.flipud(grid)
    elif ordering is GridOrdering.ROTATE_180:
        transformed = np.rot90(grid, 2)
    elif ordering is GridOrdering.TRANSPOSE:
        transformed = grid.T
    elif ordering is GridOrdering.ROTATE_90_CW:
        transformed = np.rot90(grid, -1)
    elif ordering is GridOrdering.ROTATE_90_CCW:
        transformed = np.rot90(grid, 1)
    elif ordering is GridOrdering.ANTI_TRANSPOSE:
        transformed = np.fliplr(np.flipud(grid)).T
    else:  # pragma: no cover - Enum normalization makes this unreachable.
        raise ValueError(f"Unsupported grid ordering: {ordering}")
    return np.asarray(transformed, dtype=np.int64).ravel()


def available_grid_orderings(
    lattice_indices: np.ndarray,
    logical_positions: np.ndarray | None = None,
) -> tuple[GridOrdering, ...]:
    """Return exact ordering symmetries that preserve this finite lattice.

    This is the same topology test used by SLMcore.  A non-square rectangle does
    not expose X/Y-swapping choices.  When logical coordinates are supplied,
    stagger/motif-changing permutations are rejected as well.
    """
    indices = _indices_n2(lattice_indices)
    _index_grid(indices)  # validate completeness once
    candidates = tuple(GridOrdering)
    if logical_positions is None:
        x_count = int(np.unique(indices[:, 0]).size)
        y_count = int(np.unique(indices[:, 1]).size)
        return tuple(
            candidate
            for candidate in candidates
            if candidate
            in (
                GridOrdering.CANONICAL,
                GridOrdering.FLIP_X,
                GridOrdering.FLIP_Y,
                GridOrdering.ROTATE_180,
            )
            or x_count == y_count
        )

    logical = _points_n2(logical_positions, "logical_positions")
    if logical.shape != indices.shape:
        raise ValueError("logical_positions must align with lattice_indices")
    span = float(np.max(np.ptp(logical, axis=0))) if len(logical) else 1.0
    tolerance = 1e-7 * max(1.0, span)
    valid: list[GridOrdering] = []
    for candidate in candidates:
        permutation = grid_ordering_permutation(indices, candidate)
        _linear, _translation, rms = _fit_affine(logical, logical[permutation])
        if rms <= tolerance:
            valid.append(candidate)
    return tuple(valid)


def correspondence_options(
    lattice_indices: np.ndarray,
    logical_positions: np.ndarray | None = None,
) -> tuple[GridCorrespondence, ...]:
    """Return Auto plus all valid manual correspondence choices."""
    return (GridCorrespondence.AUTO,) + tuple(
        GridCorrespondence(ordering.value)
        for ordering in available_grid_orderings(lattice_indices, logical_positions)
    )


def resolve_grid_correspondence(
    source_positions: np.ndarray,
    target_positions: np.ndarray,
    lattice_indices: np.ndarray,
    *,
    source_matched: np.ndarray | None = None,
    target_matched: np.ndarray | None = None,
    logical_positions: np.ndarray | None = None,
    correspondence: GridCorrespondence | str = GridCorrespondence.AUTO,
) -> tuple[np.ndarray, np.ndarray, GridCorrespondenceResolution]:
    """Resolve and return genuinely measured source/target point pairs.

    ``source_positions`` and ``target_positions`` contain full target-aligned
    finite-grid arrays.  Missing sites stay in those arrays but are excluded by
    the masks.  Auto tests only exact finite-grid symmetries; each candidate is
    globally affinely registered for *scoring only*.  The fitted affine is never
    used as the actual transform-calibration result.
    """
    source = _points_n2(source_positions, "source_positions")
    target = _points_n2(target_positions, "target_positions")
    indices = _indices_n2(lattice_indices)
    if target.shape != source.shape or indices.shape != source.shape:
        raise ValueError(
            "source_positions, target_positions and lattice_indices must share shape (N, 2)"
        )

    source_mask = _matched_mask(source_matched, len(source), "source_matched")
    target_mask = _matched_mask(target_matched, len(target), "target_matched")
    requested = GridCorrespondence.normalize(correspondence)
    options = correspondence_options(indices, logical_positions)
    manual = requested.ordering
    if manual is not None and requested not in options:
        raise ValueError(
            f"Grid correspondence '{requested.value}' is not valid for this finite lattice"
        )

    orderings = (
        (manual,)
        if manual is not None
        else tuple(item.ordering for item in options if item.ordering is not None)
    )
    candidates = []
    for ordering in orderings:
        permutation = grid_ordering_permutation(indices, ordering)
        candidate_target = target[permutation]
        candidate_target_mask = target_mask[permutation]
        paired = source_mask & candidate_target_mask
        pair_count = int(np.count_nonzero(paired))
        if pair_count < 3:
            continue
        linear, translation, rms = _fit_affine(source[paired], candidate_target[paired])
        metrics = _transform_metrics(linear, rms)
        # Ported SLMcore policy: after each image has been normalized to its own
        # finite topology, prefer the cross-image convention requiring the least
        # residual reflection/rotation/deformation. RMS only breaks later ties.
        score = (
            1 if metrics["reflection"] else 0,
            abs(float(metrics["rotation_deg"])),
            float(metrics["deformation_score"]),
            float(rms),
            ordering.value,
        )
        candidates.append(
            (score, ordering, candidate_target, paired, metrics, translation)
        )

    if not candidates:
        raise RuntimeError(
            "Grid correspondence needs at least three genuinely localized spots "
            "shared by one valid finite-grid ordering"
        )
    candidates.sort(key=lambda item: item[0])
    _score, resolved, candidate_target, paired, metrics, _translation = candidates[0]
    resolution = GridCorrespondenceResolution(
        requested=requested,
        resolved=resolved,
        relative_rotation_deg=float(metrics["rotation_deg"]),
        rms_px=float(metrics["rms_px"]),
        reflection=bool(metrics["reflection"]),
        singular_values=tuple(float(value) for value in metrics["singular_values"]),
        deformation_score=float(metrics["deformation_score"]),
        candidate_count=len(candidates),
        pair_count=int(np.count_nonzero(paired)),
    )
    return source[paired], candidate_target[paired], resolution


def _indices_n2(value: np.ndarray) -> np.ndarray:
    array = np.asarray(value, dtype=np.int64)
    if array.ndim != 2:
        raise ValueError("lattice_indices must be a 2-D array")
    if array.shape[1] == 2:
        return array
    if array.shape[0] == 2:
        return array.T
    raise ValueError("lattice_indices must have shape (N, 2) or (2, N)")


def _points_n2(value: np.ndarray, label: str) -> np.ndarray:
    array = np.asarray(value, dtype=np.float64)
    if array.ndim != 2:
        raise ValueError(f"{label} must be a 2-D array")
    if array.shape[1] == 2:
        return array
    if array.shape[0] == 2:
        return array.T
    raise ValueError(f"{label} must have shape (N, 2) or (2, N)")


def _matched_mask(value: np.ndarray | None, count: int, label: str) -> np.ndarray:
    if value is None:
        return np.ones(count, dtype=bool)
    array = np.asarray(value, dtype=bool)
    if array.shape != (count,):
        raise ValueError(f"{label} must have shape ({count},)")
    return array


def _index_grid(indices: np.ndarray) -> np.ndarray:
    indices = _indices_n2(indices)
    x_values = np.unique(indices[:, 0])
    y_values = np.unique(indices[:, 1])
    if int(x_values.size * y_values.size) != int(indices.shape[0]):
        raise ValueError("Grid correspondence requires a complete rectangular lattice")
    x_rank = {int(value): rank for rank, value in enumerate(x_values)}
    y_rank = {int(value): rank for rank, value in enumerate(y_values)}
    grid = np.full((int(y_values.size), int(x_values.size)), -1, dtype=np.int64)
    for column, (x_value, y_value) in enumerate(indices):
        x = x_rank[int(x_value)]
        y = y_rank[int(y_value)]
        if grid[y, x] >= 0:
            raise ValueError("Lattice indices contain duplicate cells")
        grid[y, x] = int(column)
    if np.any(grid < 0):
        raise ValueError("Lattice indices do not form a complete rectangular grid")
    return grid


def _fit_affine(source: np.ndarray, target: np.ndarray):
    source = _points_n2(source, "source")
    target = _points_n2(target, "target")
    if source.shape != target.shape or len(source) < 3:
        raise ValueError("Affine point sets must share shape (N, 2) with at least 3 points")
    design = np.column_stack([source, np.ones(len(source), dtype=np.float64)])
    coefficients, _, _, _ = np.linalg.lstsq(design, target, rcond=None)
    linear = coefficients[:2, :].T
    translation = coefficients[2, :]
    fitted = source @ linear.T + translation
    residual = target - fitted
    rms = float(np.sqrt(np.mean(np.sum(residual * residual, axis=1))))
    return linear, translation, rms


def _transform_metrics(linear: np.ndarray, rms: float) -> dict[str, Any]:
    linear = np.asarray(linear, dtype=np.float64)
    determinant = float(np.linalg.det(linear))
    u, singular, vt = np.linalg.svd(linear)
    rotation = u @ vt
    if np.linalg.det(rotation) < 0:
        u = np.array(u, copy=True)
        u[:, -1] *= -1.0
        rotation = u @ vt
    angle = float(np.degrees(np.arctan2(rotation[1, 0], rotation[0, 0])))
    singular = np.maximum(np.asarray(singular, dtype=np.float64), 1e-12)
    deformation = float(np.sqrt(np.mean(np.square(np.log(singular)))))
    return {
        "rotation_deg": angle,
        "rms_px": float(rms),
        "reflection": bool(determinant < 0.0),
        "singular_values": tuple(float(value) for value in singular),
        "deformation_score": deformation,
    }


__all__ = [
    "GridCorrespondence",
    "GridCorrespondenceResolution",
    "GridOrdering",
    "available_grid_orderings",
    "correspondence_options",
    "grid_ordering_label",
    "grid_ordering_permutation",
    "resolve_grid_correspondence",
]
