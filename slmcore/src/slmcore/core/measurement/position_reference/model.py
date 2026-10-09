"""Host-neutral position-reference geometry and correspondence records.

The records in this module deliberately describe *geometry*, not UI selection,
persistence, or one particular feedback workflow.  A reference can therefore
come from a saved localization, an editable lattice, a future dense OFF pattern,
or an arbitrary point source.  Resolvers turn that source geometry into desired
positions for one concrete target before :mod:`position_alignment` decides how
those correspondences should be fitted.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass,field
from types import MappingProxyType
from typing import Any,Hashable,Mapping,Sequence

import numpy as np

from ...cgh.lattice_geometry import LatticeRepresentation


def _freeze_points(value: Any,name: str) -> np.ndarray:
    array=np.asarray(value,dtype=np.float64)
    if array.ndim!=2 or array.shape[0]!=2:
        raise ValueError(f"{name} must have shape (2, N)")
    if array.shape[1]<1:
        raise ValueError(f"{name} must contain at least one point")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} contains non-finite values")
    result=np.array(array,copy=True)
    result.setflags(write=False)
    return result


def _freeze_lattice_indices(value: Any,name: str,*,count: int) -> np.ndarray:
    array=np.asarray(value)
    if array.ndim!=2 or array.shape!=(2,int(count)):
        raise ValueError(f"{name} must have shape (2, {int(count)})")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} contains non-finite values")
    result=np.array(array,copy=True)
    result.setflags(write=False)
    return result


def _freeze_mask(value: Any,name: str,*,count: int,default: bool) -> np.ndarray:
    if value is None:
        array=np.full(int(count),bool(default),dtype=bool)
    else:
        array=np.asarray(value,dtype=bool)
        if array.shape!=(int(count),):
            raise ValueError(f"{name} must have shape ({int(count)},)")
        array=np.array(array,copy=True)
    array.setflags(write=False)
    return array


def _freeze_weights(value: Any,*,count: int) -> np.ndarray | None:
    if value is None:
        return None
    array=np.asarray(value,dtype=np.float64)
    if array.shape!=(int(count),):
        raise ValueError(f"weights must have shape ({int(count)},)")
    if not np.all(np.isfinite(array)) or np.any(array<0):
        raise ValueError("weights must be finite and >= 0")
    result=np.array(array,copy=True)
    result.setflags(write=False)
    return result


def _freeze_mapping(value: Mapping[str,Any] | None) -> Mapping[str,Any]:
    if value is None:
        value={}
    if not isinstance(value,Mapping):
        raise TypeError("Expected a mapping")
    return MappingProxyType(deepcopy(dict(value)))


def _normalize_site_id(value: Any) -> Hashable:
    """Return a stable hashable site identity without imposing one ID scheme."""
    if isinstance(value,np.generic):
        value=value.item()
    if isinstance(value,np.ndarray):
        value=value.tolist()
    if isinstance(value,list):
        value=tuple(_normalize_site_id(item) for item in value)
    elif isinstance(value,tuple):
        value=tuple(_normalize_site_id(item) for item in value)
    try:
        hash(value)
    except TypeError as error:
        raise TypeError("Position-reference site IDs must be hashable") from error
    return value


def _freeze_site_ids(
    value: Sequence[Any] | None,*,count: int,
) -> tuple[Hashable,...] | None:
    if value is None:
        return None
    result=tuple(_normalize_site_id(item) for item in value)
    if len(result)!=int(count):
        raise ValueError(f"site_ids must contain {int(count)} entries")
    if len(set(result))!=len(result):
        raise ValueError("site_ids must be unique")
    return result


def lattice_site_ids(lattice_indices: Any) -> tuple[Hashable,...]:
    """Convert ``(2, N)`` lattice indices to exact per-column site identities."""
    array=np.asarray(lattice_indices)
    if array.ndim!=2 or array.shape[0]!=2:
        raise ValueError("lattice_indices must have shape (2, N)")
    if not np.all(np.isfinite(array)):
        raise ValueError("lattice_indices contains non-finite values")
    return tuple(
        (_normalize_site_id(array[0,index]),_normalize_site_id(array[1,index]))
        for index in range(array.shape[1])
    )


@dataclass(frozen=True)
class ReferenceGeometry:
    """One source of desired detector-space positions.

    ``site_ids`` are optional because future point-cloud references may have no
    logical indexing.  Exact-lattice resolution requires them (or a lattice from
    which they can be derived).  ``valid_mask`` says which source positions are
    usable; optional non-negative ``weights`` express confidence independently
    of validity.
    """

    positions_px: np.ndarray
    site_ids: Sequence[Any] | None=None
    lattice: LatticeRepresentation | None=None
    valid_mask: np.ndarray | None=None
    weights: np.ndarray | None=None
    metadata: Mapping[str,Any]=field(default_factory=dict)

    def __post_init__(self) -> None:
        positions=_freeze_points(self.positions_px,"positions_px")
        count=positions.shape[1]
        lattice=self.lattice
        if lattice is not None and not isinstance(lattice,LatticeRepresentation):
            raise TypeError("lattice must be a LatticeRepresentation")
        if lattice is not None and lattice.lattice_indices.shape[1]!=count:
            raise ValueError("Reference lattice spot count does not match positions")
        site_ids=self.site_ids
        if site_ids is None and lattice is not None:
            site_ids=lattice_site_ids(lattice.lattice_indices)
        site_ids=_freeze_site_ids(site_ids,count=count)
        object.__setattr__(self,"positions_px",positions)
        object.__setattr__(self,"site_ids",site_ids)
        object.__setattr__(self,"valid_mask",_freeze_mask(
            self.valid_mask,"valid_mask",count=count,default=True,
        ))
        object.__setattr__(self,"weights",_freeze_weights(self.weights,count=count))
        object.__setattr__(self,"metadata",_freeze_mapping(self.metadata))

    @property
    def point_count(self) -> int:
        return int(self.positions_px.shape[1])


@dataclass(frozen=True)
class PositionReferenceTarget:
    """Concrete target to which a :class:`ReferenceGeometry` is resolved."""

    registered_positions_px: np.ndarray
    measured_positions_px: np.ndarray
    matched_mask: np.ndarray | None=None
    site_ids: Sequence[Any] | None=None
    lattice_indices: np.ndarray | None=None
    lattice: LatticeRepresentation | None=None
    metadata: Mapping[str,Any]=field(default_factory=dict)

    def __post_init__(self) -> None:
        registered=_freeze_points(self.registered_positions_px,"registered_positions_px")
        measured=_freeze_points(self.measured_positions_px,"measured_positions_px")
        if registered.shape!=measured.shape:
            raise ValueError("registered_positions_px and measured_positions_px must match")
        count=registered.shape[1]
        lattice=self.lattice
        if lattice is not None and not isinstance(lattice,LatticeRepresentation):
            raise TypeError("lattice must be a LatticeRepresentation")
        lattice_indices=self.lattice_indices
        if lattice_indices is None and lattice is not None:
            lattice_indices=lattice.lattice_indices
        if lattice_indices is not None:
            lattice_indices=_freeze_lattice_indices(
                lattice_indices,"lattice_indices",count=count,
            )
        if lattice is not None:
            if lattice.lattice_indices.shape[1]!=count:
                raise ValueError("Target lattice spot count does not match positions")
            if lattice_indices is not None and not np.array_equal(
                lattice_indices,lattice.lattice_indices,
            ):
                raise ValueError("Target lattice_indices disagree with lattice")
        site_ids=self.site_ids
        if site_ids is None and lattice_indices is not None:
            site_ids=lattice_site_ids(lattice_indices)
        site_ids=_freeze_site_ids(site_ids,count=count)
        object.__setattr__(self,"registered_positions_px",registered)
        object.__setattr__(self,"measured_positions_px",measured)
        object.__setattr__(self,"matched_mask",_freeze_mask(
            self.matched_mask,"matched_mask",count=count,default=True,
        ))
        object.__setattr__(self,"site_ids",site_ids)
        object.__setattr__(self,"lattice_indices",lattice_indices)
        object.__setattr__(self,"metadata",_freeze_mapping(self.metadata))

    @property
    def point_count(self) -> int:
        return int(self.registered_positions_px.shape[1])


@dataclass(frozen=True)
class ResolvedPositionReference:
    """Reference positions reordered into one target's site order."""

    desired_positions_px: np.ndarray
    valid_mask: np.ndarray
    source_geometry: ReferenceGeometry
    source_indices: np.ndarray
    weights: np.ndarray | None=None
    correspondence: Mapping[str,Any]=field(default_factory=dict)
    diagnostics: Mapping[str,Any]=field(default_factory=dict)

    def __post_init__(self) -> None:
        desired=_freeze_points(self.desired_positions_px,"desired_positions_px")
        count=desired.shape[1]
        if not isinstance(self.source_geometry,ReferenceGeometry):
            raise TypeError("source_geometry must be a ReferenceGeometry")
        valid=_freeze_mask(self.valid_mask,"valid_mask",count=count,default=False)
        source_indices=np.asarray(self.source_indices,dtype=np.int64)
        if source_indices.shape!=(count,):
            raise ValueError(f"source_indices must have shape ({count},)")
        if np.any(source_indices < -1) or np.any(
            source_indices >= self.source_geometry.point_count
        ):
            raise ValueError("source_indices contain an unavailable source point")
        if np.any(valid & (source_indices<0)):
            raise ValueError("Every valid resolved site must have a source index")
        source_indices=np.array(source_indices,copy=True)
        source_indices.setflags(write=False)
        object.__setattr__(self,"desired_positions_px",desired)
        object.__setattr__(self,"valid_mask",valid)
        object.__setattr__(self,"source_indices",source_indices)
        object.__setattr__(self,"weights",_freeze_weights(self.weights,count=count))
        object.__setattr__(self,"correspondence",_freeze_mapping(self.correspondence))
        object.__setattr__(self,"diagnostics",_freeze_mapping(self.diagnostics))

    @property
    def valid_count(self) -> int:
        return int(np.count_nonzero(self.valid_mask))


__all__=[
    "PositionReferenceTarget",
    "ReferenceGeometry",
    "ResolvedPositionReference",
    "lattice_site_ids",
]
