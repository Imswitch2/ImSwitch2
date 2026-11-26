"""Shared, algorithm-independent contracts for periodic and finite patterns.

These are deliberately *not* new localizers. An empty prior represents no
knowledge; a canonical result describes what an existing localizer returned.
The legacy ImProcess/uLenses objects and their serialized formats remain the
source of truth until consumers are migrated in a later patch.

Coordinate contract
-------------------
* Pixel positions are full-image ``(x, y)`` (column, row).
* Basis vectors are the *columns* of the 2x2 ``basis_xy`` matrix.
* Finite logical site indices are ``(i, j)`` rows in a ``(N, 2)`` array.
* Unmatched measured positions may be NaN, but only when the mask is False.
* An unbounded periodic geometry never requires a list of image/FOV sites.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import Any, Literal, Mapping

import numpy as np

from .geometry import LatticeRepresentation


def _array(value: Any, name: str, *, dtype: Any = float, ncol: int = 2,
           allow_nan: bool = False) -> np.ndarray:
    arr = np.asarray(value, dtype=dtype)
    if arr.ndim != 2 or arr.shape[1] != ncol:
        raise ValueError(f"{name} must have shape (N, {ncol})")
    if np.any(np.isinf(arr)) or (not allow_nan and not np.all(np.isfinite(arr))):
        raise ValueError(f"{name} must contain finite values")
    arr = np.array(arr, copy=True)
    arr.setflags(write=False)
    return arr


def _mapping(value: Mapping[str, Any] | None, name: str) -> Mapping[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    return MappingProxyType(deepcopy(dict(value)))


def _pair(value: Any, name: str) -> tuple[float, float]:
    arr = np.asarray(value, dtype=float)
    if arr.shape != (2,) or not np.all(np.isfinite(arr)):
        raise ValueError(f"{name} must contain two finite coordinates")
    return float(arr[0]), float(arr[1])


def _optional_float(value: float | None, name: str, *, positive: bool = False,
                    nonnegative: bool = False) -> float | None:
    if value is None:
        return None
    value = float(value)
    if not np.isfinite(value) or (positive and value <= 0) or (nonnegative and value < 0):
        raise ValueError(f"{name} must be finite" + (" and positive" if positive else
                         " and nonnegative" if nonnegative else ""))
    return value


def _indices(value: Any, name: str) -> np.ndarray:
    raw = _array(value, name, dtype=float)
    if not np.all(np.equal(raw, np.rint(raw))):
        raise ValueError(f"{name} must contain integer indices")
    # Guard against overflow when casting huge float indices to int64.
    if np.any(np.abs(raw) >= 2**53):
        raise ValueError(f"{name} indices are too large")
    out = _array(raw, name, dtype=np.int64)
    if len(np.unique(out, axis=0)) != len(out):
        raise ValueError(f"{name} must not contain duplicate site indices")
    return out


@dataclass(frozen=True)
class PatternPrior:
    """Optional *expected* properties, never measured localization values.

    ``stagger_axis='x'`` shifts odd rows in X; ``'y'`` shifts odd columns in Y.
    Stagger is a signed fraction of the corresponding axis period. ``shape``
    follows (columns, rows), not the NumPy image-shape convention.

    These hints are descriptive in Patch 1; adapters do not apply them to a fit.
    """

    expected_period_x_px: float | None = None
    expected_period_y_px: float | None = None
    period_tolerance_fraction: float | None = None
    expected_rotation_deg: float | None = None
    stagger_axis: Literal["x", "y"] | None = None
    stagger_fraction: float | None = None
    finite_shape_xy: tuple[int, int] | None = None
    finite_site_indices: np.ndarray | None = field(default=None, compare=False, repr=False)
    modality_id: str | None = None
    target_type: str | None = None
    target_params: Mapping[str, Any] | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        for name in ("expected_period_x_px", "expected_period_y_px"):
            object.__setattr__(self, name, _optional_float(getattr(self, name), name, positive=True))
        object.__setattr__(self, "period_tolerance_fraction", _optional_float(
            self.period_tolerance_fraction, "period_tolerance_fraction", nonnegative=True))
        for name in ("expected_rotation_deg", "stagger_fraction"):
            object.__setattr__(self, name, _optional_float(getattr(self, name), name))
        if self.stagger_axis not in (None, "x", "y"):
            raise ValueError("stagger_axis must be None, 'x', or 'y'")
        if self.stagger_fraction is not None and self.stagger_axis is None:
            raise ValueError("stagger_fraction requires stagger_axis")
        if self.finite_shape_xy is not None:
            shape = tuple(self.finite_shape_xy)
            if len(shape) != 2 or any(not isinstance(v, (int, np.integer)) or isinstance(v, (bool, np.bool_)) or v <= 0 for v in shape):
                raise ValueError("finite_shape_xy must be positive integer (n_cols, n_rows)")
            object.__setattr__(self, "finite_shape_xy", (int(shape[0]), int(shape[1])))
        if self.finite_site_indices is not None:
            indices = _indices(self.finite_site_indices, "finite_site_indices")
            if not len(indices):
                raise ValueError("finite_site_indices must not be empty")
            if self.finite_shape_xy is not None and (np.any(indices < 0) or
                np.any(indices >= np.asarray(self.finite_shape_xy))):
                raise ValueError("finite_site_indices must fit finite_shape_xy")
            object.__setattr__(self, "finite_site_indices", indices)
        for name in ("modality_id", "target_type"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a nonempty string when supplied")
        if self.target_params is not None:
            object.__setattr__(self, "target_params", _mapping(self.target_params, "target_params"))

    @property
    def provided_fields(self) -> tuple[str, ...]:
        """Names of explicitly supplied values, including descriptive context."""
        return tuple(f.name for f in fields(self) if getattr(self, f.name) is not None)


@dataclass(frozen=True)
class PatternGeometry:
    """2D translation lattice with optional finite target identity.

    ``basis_xy[:, 0]`` and ``basis_xy[:, 1]`` are translation vectors in
    full-image XY pixels. ``motif_offsets_xy`` are offsets within each cell;
    they are not required to preserve a finite target's logical labeling.
    Finite per-site correspondence is represented by ``site_indices_ij`` and
    the companion result's explicit predicted positions.
    """

    origin_xy: tuple[float, float]
    basis_xy: np.ndarray = field(compare=False)
    extent: Literal["periodic", "finite"] = "periodic"
    motif_offsets_xy: tuple[tuple[float, float], ...] = ((0.0, 0.0),)
    site_indices_ij: np.ndarray | None = field(default=None, compare=False, repr=False)
    finite_shape_xy: tuple[int, int] | None = None
    stagger_axis: Literal["x", "y"] | None = None
    stagger_fraction: float | None = None
    representation: LatticeRepresentation | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "origin_xy", _pair(self.origin_xy, "origin_xy"))
        basis = np.asarray(self.basis_xy, dtype=np.float64)
        if basis.shape != (2, 2) or not np.all(np.isfinite(basis)) or abs(float(np.linalg.det(basis))) <= 1e-12:
            raise ValueError("basis_xy must be a finite, non-singular 2x2 matrix")
        basis = np.array(basis, copy=True)
        basis.setflags(write=False)
        object.__setattr__(self, "basis_xy", basis)
        if self.extent not in ("periodic", "finite"):
            raise ValueError("extent must be 'periodic' or 'finite'")
        if not self.motif_offsets_xy:
            raise ValueError("motif_offsets_xy must contain at least one point")
        object.__setattr__(self, "motif_offsets_xy", tuple(
            _pair(v, "motif point") for v in self.motif_offsets_xy))
        if self.stagger_axis not in (None, "x", "y"):
            raise ValueError("stagger_axis must be None, 'x', or 'y'")
        object.__setattr__(self, "stagger_fraction", _optional_float(
            self.stagger_fraction, "stagger_fraction"))
        if self.stagger_fraction is not None and self.stagger_axis is None:
            raise ValueError("stagger_fraction requires stagger_axis")
        if self.extent == "periodic":
            if self.site_indices_ij is not None or self.finite_shape_xy is not None or self.representation is not None:
                raise ValueError("periodic geometry cannot have finite target indices")
        else:
            if self.site_indices_ij is None:
                raise ValueError("finite geometry requires explicit site_indices_ij")
            indices = _indices(self.site_indices_ij, "site_indices_ij")
            if not len(indices):
                raise ValueError("finite geometry requires at least one site")
            object.__setattr__(self, "site_indices_ij", indices)
            if self.finite_shape_xy is not None:
                shape = tuple(self.finite_shape_xy)
                if len(shape) != 2 or any(not isinstance(v, (int, np.integer)) or isinstance(v, (bool, np.bool_)) or v <= 0 for v in shape):
                    raise ValueError("finite_shape_xy must contain positive integer counts")
                if np.any(indices < 0) or np.any(indices >= np.asarray(shape)):
                    raise ValueError("site_indices_ij must fit finite_shape_xy")
                object.__setattr__(self, "finite_shape_xy", (int(shape[0]), int(shape[1])))
            if self.representation is not None:
                if not isinstance(self.representation, LatticeRepresentation):
                    raise TypeError("representation must be a LatticeRepresentation")
                if not np.array_equal(self.representation.lattice_indices.T, indices):
                    raise ValueError("representation and site_indices_ij must align")


@dataclass(frozen=True)
class PatternLocalizationResult:
    """Normalized, *non-serializing* view of a localization result.

    Original host result and persistence formats remain untouched. Predicted,
    measured and logical arrays share the same order for finite targets. A
    missing measured position is distinguished by ``matched_mask=False``;
    it does not indicate that the physical target site is absent.

    ``prior_usage`` maps a provided prior field to ``'applied'``, ``'ignored'``
    or ``'not_evaluated'``. Patch 1 adapters default to ``not_evaluated``:
    they have not performed (or changed) a localization algorithm.
    """

    geometry: PatternGeometry
    strategy: Literal["periodic", "finite"]
    predicted_positions_xy: np.ndarray | None = field(default=None, compare=False, repr=False)
    measured_positions_xy: np.ndarray | None = field(default=None, compare=False, repr=False)
    matched_mask: np.ndarray | None = field(default=None, compare=False, repr=False)
    detected_positions_xy: np.ndarray | None = field(default=None, compare=False, repr=False)
    diagnostics: Mapping[str, Any] = field(default_factory=dict, compare=False)
    prior: PatternPrior | None = None
    prior_usage: Mapping[str, str] | None = field(default=None, compare=False)
    provenance: Mapping[str, Any] = field(default_factory=dict, compare=False)
    compatibility: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.geometry, PatternGeometry):
            raise TypeError("geometry must be a PatternGeometry")
        if self.strategy != self.geometry.extent:
            raise ValueError("strategy must match geometry.extent")
        if self.prior is not None and not isinstance(self.prior, PatternPrior):
            raise TypeError("prior must be a PatternPrior or None")
        for name in ("predicted_positions_xy", "detected_positions_xy"):
            val = getattr(self, name)
            if val is not None:
                object.__setattr__(self, name, _array(val, name))
        if self.measured_positions_xy is not None:
            object.__setattr__(self, "measured_positions_xy", _array(
                self.measured_positions_xy, "measured_positions_xy", allow_nan=True))
        if self.matched_mask is not None:
            if self.measured_positions_xy is None:
                raise ValueError("matched_mask requires measured_positions_xy")
            matched = np.asarray(self.matched_mask, dtype=bool)
            if matched.shape != (len(self.measured_positions_xy),):
                raise ValueError("matched_mask must align with measured positions")
            if np.any(~np.isfinite(self.measured_positions_xy[matched])):
                raise ValueError("matched measured positions must be finite")
            matched = np.array(matched, copy=True)
            matched.setflags(write=False)
            object.__setattr__(self, "matched_mask", matched)
        elif self.measured_positions_xy is not None and not np.all(np.isfinite(self.measured_positions_xy)):
            raise ValueError("NaN measured positions require an explicit matched_mask")
        if self.geometry.extent == "finite":
            n = len(self.geometry.site_indices_ij)
            if self.predicted_positions_xy is None or len(self.predicted_positions_xy) != n:
                raise ValueError("finite predicted positions must align with site indices")
            if self.measured_positions_xy is not None and len(self.measured_positions_xy) != n:
                raise ValueError("finite measurements must align with site indices")
        elif self.matched_mask is not None and self.predicted_positions_xy is not None and len(self.predicted_positions_xy) != len(self.matched_mask):
            raise ValueError("periodic predicted positions must align with matched measurements")
        object.__setattr__(self, "diagnostics", _mapping(self.diagnostics, "diagnostics"))
        object.__setattr__(self, "provenance", _mapping(self.provenance, "provenance"))
        object.__setattr__(self, "compatibility", _mapping(self.compatibility, "compatibility"))
        usage = dict(self.prior_usage or {})
        provided = set(self.prior.provided_fields) if self.prior is not None else set()
        if set(usage) - provided:
            raise ValueError("prior_usage may refer only to explicitly provided hints")
        if any(v not in ("applied", "ignored", "not_evaluated") for v in usage.values()):
            raise ValueError("prior_usage must be applied, ignored, or not_evaluated")
        usage = {name: usage.get(name, "not_evaluated") for name in sorted(provided)}
        object.__setattr__(self, "prior_usage", MappingProxyType(usage))
