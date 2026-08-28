"""Immutable runtime representation of a resolved CGH target."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any,Mapping

import numpy as np

from ..pattern_geometry import PatternGeometry
from ..signature import CGHSignature


@dataclass(frozen=True)
class ResolutionAdjustment:
    """Describe one difference between requested and effective target values."""

    key: str
    requested_value: Any
    effective_value: Any
    source: str
    reason: str

    def __post_init__(self) -> None:
        """Normalize textual fields and reject incomplete adjustment records."""
        key = str(self.key).strip()
        source = str(self.source).strip()
        reason = str(self.reason).strip()

        if not key:
            raise ValueError("ResolutionAdjustment.key cannot be empty")
        if not source:
            raise ValueError("ResolutionAdjustment.source cannot be empty")
        if not reason:
            raise ValueError("ResolutionAdjustment.reason cannot be empty")

        object.__setattr__(self, "key", key)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "reason", reason)


@dataclass(frozen=True)
class TargetResolution:
    """Detached, immutable target data consumed by CGH algorithms.

    All target strengths use relative intensity semantics. Absolute scaling is
    deliberately not part of the contract; algorithms may normalize globally
    for numerical stability and derive field amplitudes using ``sqrt``.

    ``canonical_params`` describe the authoritative user target. Runtime-only
    constraints may alter ``effective_params`` and ``spot_positions_kxy``.
    ``ideal_spot_positions_kxy`` always retains the requested lattice before
    raster quantization or future calibration and feedback transforms.
    """

    section_shape: tuple[int, int]
    target_signature: CGHSignature
    canonical_params: Mapping[str, Any]
    effective_params: Mapping[str, Any]
    adjustments: tuple[ResolutionAdjustment, ...]
    lattice_indices: np.ndarray
    ideal_spot_positions_kxy: np.ndarray
    spot_positions_kxy: np.ndarray
    spot_intensities: np.ndarray
    preview: np.ndarray
    target_array: np.ndarray | None = None
    details: Mapping[str, Any] = field(default_factory=dict)
    geometry: PatternGeometry | None = None

    def __post_init__(self) -> None:
        """Validate shapes and freeze every mutable payload."""
        section_shape = _validate_shape(self.section_shape, "section_shape")
        if self.target_signature is None:
            raise ValueError("target_signature is required")
        target_signature = str(self.target_signature).strip()
        if not target_signature:
            raise ValueError("target_signature cannot be empty")
        canonical_params = _freeze_mapping(self.canonical_params)
        effective_params = _freeze_mapping(self.effective_params)
        details = _freeze_mapping(self.details)
        geometry = self.geometry
        if geometry is not None and not isinstance(geometry,PatternGeometry):
            raise TypeError("geometry must be a PatternGeometry")
        adjustments = tuple(self.adjustments or ())

        for adjustment in adjustments:
            if not isinstance(adjustment, ResolutionAdjustment):
                raise TypeError(
                    "adjustments must contain ResolutionAdjustment instances"
                )

        lattice_indices = _freeze_array(
            self.lattice_indices, "lattice_indices", ndim=2
        )
        ideal_positions = _freeze_array(
            self.ideal_spot_positions_kxy,
            "ideal_spot_positions_kxy",
            ndim=2,
            dtype=np.float64,
        )
        effective_positions = _freeze_array(
            self.spot_positions_kxy,
            "spot_positions_kxy",
            ndim=2,
            dtype=np.float64,
        )
        intensities = _freeze_array(
            self.spot_intensities,
            "spot_intensities",
            ndim=1,
            dtype=np.float64,
        )
        preview = _freeze_array(
            self.preview, "preview", ndim=2, dtype=np.float64
        )

        if lattice_indices.shape[0] != 2:
            raise ValueError(
                "lattice_indices must have shape (2, N), got "
                f"{lattice_indices.shape}"
            )
        if ideal_positions.shape[0] != 2:
            raise ValueError(
                "ideal_spot_positions_kxy must have shape (2, N), got "
                f"{ideal_positions.shape}"
            )
        if effective_positions.shape[0] != 2:
            raise ValueError(
                "spot_positions_kxy must have shape (2, N), got "
                f"{effective_positions.shape}"
            )

        n_spots = lattice_indices.shape[1]
        if ideal_positions.shape[1] != n_spots:
            raise ValueError("ideal positions and lattice indices must align")
        if effective_positions.shape[1] != n_spots:
            raise ValueError("effective positions and lattice indices must align")
        if intensities.shape != (n_spots,):
            raise ValueError(
                f"spot_intensities must have shape ({n_spots},), got "
                f"{intensities.shape}"
            )
        _validate_relative_intensity(intensities,"spot_intensities")
        _validate_nonnegative(preview,"preview")
        target_array = None
        if self.target_array is not None:
            if self.target_array is self.preview:
                target_array = preview
            else:
                target_array = _freeze_array(
                    self.target_array,"target_array",ndim=2,dtype=np.float64,
                )
            _validate_relative_intensity(target_array,"target_array")

        object.__setattr__(self, "section_shape", section_shape)
        object.__setattr__(
            self, "target_signature", CGHSignature(target_signature)
        )
        object.__setattr__(self, "canonical_params", canonical_params)
        object.__setattr__(self, "effective_params", effective_params)
        object.__setattr__(self, "adjustments", adjustments)
        object.__setattr__(self, "lattice_indices", lattice_indices)
        object.__setattr__(self, "ideal_spot_positions_kxy", ideal_positions)
        object.__setattr__(self, "spot_positions_kxy", effective_positions)
        object.__setattr__(self, "spot_intensities", intensities)
        object.__setattr__(self, "preview", preview)
        object.__setattr__(self, "target_array", target_array)
        object.__setattr__(self, "geometry", geometry)
        object.__setattr__(self, "details", details)


def _validate_shape(shape, name):
    """Return a validated positive ``(height, width)`` shape."""
    if shape is None or len(shape) != 2:
        raise ValueError(f"{name} must be (height, width), got {shape}")

    height, width = int(shape[0]), int(shape[1])
    if height <= 0 or width <= 0:
        raise ValueError(f"{name} must be positive, got {shape}")
    return height, width


def _freeze_mapping(value):
    """Deep-copy and expose a mapping through a read-only proxy."""
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise TypeError(f"Expected a mapping, got {type(value).__name__}")
    return MappingProxyType(deepcopy(dict(value)))


def _freeze_array(value, name, ndim, dtype=None):
    """Copy, validate, and mark one numerical array as read-only."""
    array = np.asarray(value, dtype=dtype)
    if array.ndim != ndim:
        raise ValueError(f"{name} must be {ndim}D, got shape {array.shape}")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} contains non-finite values")

    array = np.array(array, copy=True)
    array.setflags(write=False)
    return array


def _validate_nonnegative(values,name):
    if np.any(values < 0):
        raise ValueError(f"{name} cannot contain negative intensities")


def _validate_relative_intensity(values,name):
    _validate_nonnegative(values,name)
    if values.size and not np.any(values > 0):
        raise ValueError(f"{name} must contain a positive intensity")
