"""Canonical finite lattice representation shared by targets and localization."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass,field
from types import MappingProxyType
from typing import Any,Mapping

import numpy as np


def _freeze_array(value,name,ndim,dtype=None):
    array = np.asarray(value,dtype=dtype)
    if array.ndim != ndim:
        raise ValueError("%s must be %dD, got shape %s" % (name,ndim,array.shape))
    if not np.all(np.isfinite(array)):
        raise ValueError("%s contains non-finite values" % name)
    array = np.array(array,copy=True)
    array.setflags(write=False)
    return array


def _freeze_mapping(value):
    if value is None:
        value = {}
    if not isinstance(value,Mapping):
        raise TypeError("Expected a mapping, got %s" % type(value).__name__)
    return MappingProxyType(deepcopy(dict(value)))


@dataclass(frozen=True)
class LatticeRepresentation:
    """Finite lattice as translation cells plus a motif/basis.

    ``cell_vectors`` and ``basis_offsets`` live in logical coordinates.  Each
    finite target spot is identified by a stable ``lattice_indices`` column,
    one integer ``cell_indices`` column and one ``basis_indices`` entry.

    This representation deliberately separates translation symmetry from the
    motif inside one cell.  It therefore stays stable for rectangular,
    staggered and other finite lattices even when a special parameter value
    happens to admit a smaller primitive Bravais cell.
    """

    lattice_indices: np.ndarray
    cell_indices: np.ndarray
    basis_indices: np.ndarray
    cell_vectors: np.ndarray
    basis_offsets: np.ndarray
    diagnostics: Mapping[str,Any] = field(default_factory=dict)

    def __post_init__(self):
        lattice = _freeze_array(self.lattice_indices,"lattice_indices",2)
        cells = _freeze_array(self.cell_indices,"cell_indices",2)
        basis_index = np.asarray(self.basis_indices,dtype=np.int64)
        vectors = _freeze_array(self.cell_vectors,"cell_vectors",2,dtype=np.float64)
        offsets = _freeze_array(self.basis_offsets,"basis_offsets",2,dtype=np.float64)

        if lattice.shape[0] != 2 or cells.shape != lattice.shape:
            raise ValueError(
                "lattice_indices/cell_indices must share shape (2, N)"
            )
        if vectors.shape != (2,2):
            raise ValueError("cell_vectors must have shape (2, 2)")
        if abs(float(np.linalg.det(vectors))) < 1e-12:
            raise ValueError("cell_vectors must be non-singular")
        if offsets.shape[0] != 2 or offsets.shape[1] < 1:
            raise ValueError("basis_offsets must have shape (2, M), M >= 1")
        if basis_index.shape != (lattice.shape[1],):
            raise ValueError("basis_indices must have shape (N,)")
        if np.any(basis_index < 0) or np.any(basis_index >= offsets.shape[1]):
            raise ValueError("basis_indices reference an unavailable basis point")

        basis_index = np.array(basis_index,copy=True)
        basis_index.setflags(write=False)
        object.__setattr__(self,"lattice_indices",lattice)
        object.__setattr__(self,"cell_indices",cells)
        object.__setattr__(self,"basis_indices",basis_index)
        object.__setattr__(self,"cell_vectors",vectors)
        object.__setattr__(self,"basis_offsets",offsets)
        object.__setattr__(self,"diagnostics",_freeze_mapping(self.diagnostics))

    @property
    def logical_positions(self):
        """Return uncentered logical positions with shape ``(2, N)``."""
        return (
            self.cell_vectors.dot(self.cell_indices.astype(np.float64))
            + self.basis_offsets[:,self.basis_indices]
        )

    @property
    def centered_logical_positions(self):
        """Return mean-centered logical positions for numerical fitting."""
        logical = self.logical_positions
        return logical - np.mean(logical,axis=1,keepdims=True)

    @property
    def basis_count(self):
        return int(self.basis_offsets.shape[1])

    @classmethod
    def alternating_rows(cls,lattice_indices,stagger):
        """Build the canonical two-row representation used by SLM lattices.

        The public stagger remains in ``[0, 1]``.  Internally the odd-row motif
        is canonicalized to a signed X offset in ``[-0.5, 0.5]``.  When the
        public value is above 0.5, an integer +X cell shift on odd rows exactly
        preserves every finite target position and its stable lattice index.
        """
        indices = np.asarray(lattice_indices)
        if indices.ndim != 2 or indices.shape[0] != 2 or indices.shape[1] == 0:
            raise ValueError("lattice_indices must have shape (2, N)")
        rounded = np.rint(indices).astype(np.int64)
        if not np.allclose(indices,rounded,rtol=0.0,atol=1e-9):
            raise ValueError("alternating-row lattice indices must be integers")

        value = float(stagger)
        if not np.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError("stagger must be finite and in [0, 1]")

        if value > 0.5:
            canonical = value - 1.0
            odd_x_cell_shift = 1
        else:
            canonical = value
            odd_x_cell_shift = 0

        parity = np.mod(rounded[1],2).astype(np.int64)
        cells = np.vstack([
            rounded[0] + parity * odd_x_cell_shift,
            np.floor_divide(rounded[1],2),
        ])
        basis = parity

        return cls(
            lattice_indices=rounded,
            cell_indices=cells,
            basis_indices=basis,
            cell_vectors=np.array([
                [1.0,0.0],
                [0.0,2.0],
            ],dtype=np.float64),
            basis_offsets=np.array([
                [0.0,canonical],
                [0.0,1.0],
            ],dtype=np.float64),
            diagnostics={
                "family":"alternating_rows",
                "stagger":value,
                "canonical_stagger":canonical,
                "odd_basis_cell_shift_x":odd_x_cell_shift,
            },
        )
