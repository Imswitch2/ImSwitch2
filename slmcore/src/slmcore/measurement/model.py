"""Generic immutable image-measurement records shared by host workflows."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass,field
from datetime import datetime
from types import MappingProxyType
from typing import Any,Mapping
from uuid import uuid4

import numpy as np


def _freeze_image(value: Any,name: str="measurement image") -> np.ndarray:
    image = np.asarray(value,dtype=np.float64)
    if image.ndim != 2:
        raise ValueError(f"{name} must be 2D, got shape {image.shape}")
    if not np.all(np.isfinite(image)):
        raise ValueError(f"{name} contains non-finite values")
    image = np.array(image,copy=True)
    image.setflags(write=False)
    return image


def _freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str,Any]:
    if value is None:
        value = {}
    if not isinstance(value,Mapping):
        raise TypeError(f"Expected a mapping, got {type(value).__name__}")
    return MappingProxyType(deepcopy(dict(value)))


@dataclass(frozen=True)
class ImageMeasurement:
    """One immutable 2D image with acquisition/loading provenance.

    ``measurement_id`` is a workflow identity rather than a content hash.  It
    lets asynchronous localization results prove which measurement they were
    computed from even when two images happen to share timestamps or pixels.

    The record is deliberately host-neutral: an application may obtain the
    image from a detector, a file, a simulation, or another source and feed the
    same object to localization, calibration, or feedback workflows.
    """

    image: np.ndarray
    source: str = "unknown"
    created_at: str = ""
    metadata: Mapping[str,Any] = field(default_factory=dict)
    detector: str | None = None
    measurement_id: str = ""

    def __post_init__(self) -> None:
        source = str(self.source or "unknown").strip() or "unknown"
        detector = (
            None
            if self.detector is None or not str(self.detector).strip()
            else str(self.detector).strip()
        )
        created_at = str(self.created_at or datetime.now().isoformat())
        measurement_id = str(self.measurement_id or uuid4().hex).strip()
        if not measurement_id:
            raise ValueError("measurement_id cannot be empty")

        object.__setattr__(self,"image",_freeze_image(self.image))
        object.__setattr__(self,"source",source)
        object.__setattr__(self,"detector",detector)
        object.__setattr__(self,"created_at",created_at)
        object.__setattr__(self,"measurement_id",measurement_id)
        object.__setattr__(self,"metadata",_freeze_mapping(self.metadata))


def create_image_measurement(
    image: Any,
    *,
    source: str,
    detector: str | None=None,
    metadata: Mapping[str, Any] | None=None,
) -> ImageMeasurement:
    """Create a normalized 2D measurement from common detector/file arrays."""
    array = np.asarray(image)
    if array.ndim == 3:
        array = np.mean(array.astype(np.float64),axis=-1)
    if array.ndim != 2:
        raise ValueError(
            f"Image measurement must be 2D, got shape {array.shape}"
        )
    return ImageMeasurement(
        image=np.asarray(array,dtype=np.float64),
        source=source,
        detector=detector,
        metadata=dict(metadata or {}),
    )


__all__ = ["ImageMeasurement","create_image_measurement"]
