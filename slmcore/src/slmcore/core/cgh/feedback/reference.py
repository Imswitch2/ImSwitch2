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
from ...measurement.reference_geometry import (
    EditablePositionReferenceGeometry,editable_position_reference_geometry,
    editable_reference_center_px,editable_reference_positions,
    fit_center_reference_geometry,
)


class PositionReferenceMode(str,Enum):
    """How detector-space target positions are chosen for position feedback."""

    GLOBAL_FIT = "global_fit"
    EDITABLE = "editable"
    REFERENCE = "reference"
    CURRENT = "current"

    @classmethod
    def normalize(cls,value: Any) -> "PositionReferenceMode":
        if isinstance(value,cls):
            return value
        text = str(value or cls.GLOBAL_FIT.value).strip().lower()
        aliases = {
            "global":cls.GLOBAL_FIT.value,
            "saved":cls.REFERENCE.value,
            "self":cls.CURRENT.value,
            "free":cls.CURRENT.value,
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
    and are keyed semantically by observation plane.
    """

    name: str
    observation_plane_name: str
    lattice_indices: np.ndarray
    positions_px: np.ndarray
    image_shape: tuple[int,int]
    detector_name: str | None=None
    metadata: Mapping[str,Any]=field(default_factory=dict)
    created_at: str=""

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        observation_plane = str(self.observation_plane_name or "").strip()
        if not name:
            raise ValueError("Position-reference name is required")
        if not observation_plane:
            raise ValueError("Position-reference observation plane is required")
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
        object.__setattr__(self,"observation_plane_name",observation_plane)
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
            "observation_plane_name":self.observation_plane_name,
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
            observation_plane_name=data.get("observation_plane_name",""),
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
        raise ValueError("Reference lattice does not match the current target")
    return np.asarray(reference.positions_px,dtype=np.float64) - (
        localization_crop_offset_px(localization)
    )


__all__ = [
    "EditablePositionReferenceGeometry",
    "PositionReference",
    "PositionReferenceMode",
    "fit_center_reference_geometry",
    "editable_position_reference_geometry",
    "editable_reference_center_px",
    "editable_reference_positions",
    "localization_crop_offset_px",
    "localization_positions_full_px",
    "reference_positions_for_localization",
]
