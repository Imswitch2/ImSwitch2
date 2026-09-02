"""Position-reference construction and compatibility helpers."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass,field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any,Mapping

import numpy as np

from ..localization import LocalizationResult


class PositionReferenceMode(str,Enum):
    """How detector-space target positions are chosen for position feedback."""

    GLOBAL_FIT = "global_fit"
    CENTER_WEIGHTED = "center_weighted"
    SAVED = "saved"

    @classmethod
    def normalize(cls,value: Any) -> "PositionReferenceMode":
        if isinstance(value,cls):
            return value
        text = str(value or cls.GLOBAL_FIT.value).strip().lower()
        aliases = {
            "global":cls.GLOBAL_FIT.value,
            "center":cls.CENTER_WEIGHTED.value,
            "centre":cls.CENTER_WEIGHTED.value,
            "centered":cls.CENTER_WEIGHTED.value,
            "centred":cls.CENTER_WEIGHTED.value,
        }
        return cls(aliases.get(text,text))


def _freeze_array(value: Any,name: str,*,dtype=None) -> np.ndarray:
    array = np.asarray(value,dtype=dtype)
    if array.ndim != 2 or array.shape[0] != 2:
        raise ValueError(f"{name} must have shape (2, N)")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} contains non-finite values")
    result = np.array(array,copy=True)
    result.setflags(write=False)
    return result


def _freeze_mapping(value: Mapping[str,Any] | None) -> Mapping[str,Any]:
    if value is None:
        value = {}
    if not isinstance(value,Mapping):
        raise TypeError("metadata must be a mapping")
    return MappingProxyType(deepcopy(dict(value)))


@dataclass(frozen=True)
class PositionReference:
    """Persistent detector-space spot positions indexed by logical lattice site.

    ``positions_px`` always uses full detector-image coordinates.  The source
    SLM/config are provenance only; references are intentionally workspace-wide
    and are keyed semantically by measurement plane.
    """

    name: str
    plane_name: str
    lattice_indices: np.ndarray
    positions_px: np.ndarray
    image_shape: tuple[int,int]
    detector_name: str | None=None
    metadata: Mapping[str,Any]=field(default_factory=dict)
    created_at: str=""

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        plane = str(self.plane_name or "").strip()
        if not name:
            raise ValueError("Position-reference name is required")
        if not plane:
            raise ValueError("Position-reference plane is required")
        indices = _freeze_array(self.lattice_indices,"lattice_indices")
        positions = _freeze_array(
            self.positions_px,"positions_px",dtype=np.float64,
        )
        if indices.shape != positions.shape:
            raise ValueError(
                "Position-reference lattice indices and positions must share shape"
            )
        shape = tuple(int(value) for value in self.image_shape)
        if len(shape) != 2 or shape[0] <= 0 or shape[1] <= 0:
            raise ValueError("image_shape must contain positive (height, width)")
        detector = (
            None
            if self.detector_name is None or not str(self.detector_name).strip()
            else str(self.detector_name).strip()
        )
        object.__setattr__(self,"name",name)
        object.__setattr__(self,"plane_name",plane)
        object.__setattr__(self,"lattice_indices",indices)
        object.__setattr__(self,"positions_px",positions)
        object.__setattr__(self,"image_shape",shape)
        object.__setattr__(self,"detector_name",detector)
        object.__setattr__(self,"metadata",_freeze_mapping(self.metadata))
        object.__setattr__(
            self,"created_at",str(self.created_at or datetime.now().isoformat()),
        )

    def to_dict(self) -> dict[str,Any]:
        return {
            "name":self.name,
            "plane_name":self.plane_name,
            "lattice_indices":self.lattice_indices.tolist(),
            "positions_px":self.positions_px.tolist(),
            "image_shape":list(self.image_shape),
            "detector_name":self.detector_name,
            "metadata":deepcopy(dict(self.metadata)),
            "created_at":self.created_at,
        }

    @classmethod
    def from_dict(cls,data: Mapping[str,Any]) -> "PositionReference":
        if not isinstance(data,Mapping):
            raise TypeError("Position-reference data must be a mapping")
        return cls(
            name=data.get("name",""),
            plane_name=data.get("plane_name",""),
            lattice_indices=np.asarray(data["lattice_indices"]),
            positions_px=np.asarray(data["positions_px"],dtype=np.float64),
            image_shape=tuple(data["image_shape"]),
            detector_name=data.get("detector_name"),
            metadata=data.get("metadata",{}),
            created_at=str(data.get("created_at","") or ""),
        )


def localization_crop_offset_px(localization: LocalizationResult) -> np.ndarray:
    """Return the full-image ``(x, y)`` origin of one localization crop."""
    y1,_y2,x1,_x2 = localization.crop_coord
    return np.asarray([[float(x1)],[float(y1)]],dtype=np.float64)


def localization_positions_full_px(
    localization: LocalizationResult,*,measured: bool=True,
) -> np.ndarray:
    """Convert crop-local localization positions to full detector coordinates."""
    positions = (
        localization.measured_positions_px
        if measured else localization.expected_positions_px
    )
    return np.asarray(positions,dtype=np.float64) + localization_crop_offset_px(
        localization
    )


def reference_positions_for_localization(
    reference: PositionReference,localization: LocalizationResult,
) -> np.ndarray:
    """Resolve a saved full-image reference into this localization's crop frame."""
    if not np.array_equal(reference.lattice_indices,localization.lattice_indices):
        raise ValueError("Saved reference lattice does not match the current target")
    return np.asarray(reference.positions_px,dtype=np.float64) - (
        localization_crop_offset_px(localization)
    )


def center_weighted_reference_positions(
    localization: LocalizationResult,*,emphasis: float=50.0,
) -> np.ndarray:
    """Return an affine reference biased toward the central measured spots.

    ``emphasis`` is a presentation-friendly 0..100 value.  Zero approaches an
    ordinary affine least-squares fit; 100 makes edge spots contribute about
    2% as much as central spots.  Localization/spot correspondence itself is
    never changed.
    """
    expected = np.asarray(localization.expected_positions_px,dtype=np.float64)
    measured = np.asarray(localization.measured_positions_px,dtype=np.float64)
    if expected.shape != measured.shape or expected.shape[1] < 3:
        raise ValueError("Center-weighted reference requires at least three spots")
    value = float(emphasis)
    if not np.isfinite(value) or not 0.0 <= value <= 100.0:
        raise ValueError("Center emphasis must be between 0 and 100")
    if value == 0.0:
        return np.array(expected,copy=True)

    center = np.mean(expected,axis=1,keepdims=True)
    radius = np.linalg.norm(expected-center,axis=0)
    maximum = float(np.max(radius)) if radius.size else 0.0
    if maximum <= 0.0:
        return np.array(expected,copy=True)
    normalized_radius = radius/maximum
    strength = 4.0*(value/100.0)
    weights = np.exp(-strength*np.square(normalized_radius))
    linear,translation = _fit_weighted_affine(expected,measured,weights)
    return linear@expected + translation[:,None]


def _fit_weighted_affine(
    source: np.ndarray,target: np.ndarray,weights: np.ndarray,
) -> tuple[np.ndarray,np.ndarray]:
    source = np.asarray(source,dtype=np.float64)
    target = np.asarray(target,dtype=np.float64)
    weights = np.asarray(weights,dtype=np.float64)
    if source.shape != target.shape or source.ndim != 2 or source.shape[0] != 2:
        raise ValueError("Affine point arrays must share shape (2, N)")
    if weights.shape != (source.shape[1],):
        raise ValueError("Affine weights must have shape (N,)")
    if source.shape[1] < 3 or np.any(weights <= 0) or not np.all(np.isfinite(weights)):
        raise ValueError("Weighted affine fit requires positive weights and 3+ points")
    design = np.column_stack([
        source.T,np.ones(source.shape[1],dtype=np.float64),
    ])
    root_weights = np.sqrt(weights)[:,None]
    coefficients,_,rank,_ = np.linalg.lstsq(
        design*root_weights,target.T*root_weights,rcond=None,
    )
    if int(rank) < 3:
        raise ValueError("Affine point set is rank-deficient")
    linear = coefficients[:2,:].T
    translation = coefficients[2,:]
    if abs(float(np.linalg.det(linear))) < 1e-12:
        raise ValueError("Affine mapping is singular")
    return linear,translation


__all__ = [
    "PositionReference",
    "PositionReferenceMode",
    "center_weighted_reference_positions",
    "localization_crop_offset_px",
    "localization_positions_full_px",
    "reference_positions_for_localization",
]
