"""Data containers for generic time-resolved detector products."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass(frozen=True)
class GateSpec:
    """One time gate in nanoseconds.

    Gate intervals are interpreted as ``start_ns <= t < stop_ns`` by the shared
    processing helpers.
    """

    name: str
    start_ns: float
    stop_ns: float

    def __post_init__(self) -> None:
        name = str(self.name).strip()
        if not name:
            raise ValueError("GateSpec name must not be empty")
        object.__setattr__(self, "name", name)

        start = float(self.start_ns)
        stop = float(self.stop_ns)
        if not np.isfinite(start) or not np.isfinite(stop):
            raise ValueError(f"Gate {name!r} has non-finite bounds")
        if stop <= start:
            raise ValueError(
                f"Gate {name!r} stop_ns must be greater than start_ns"
            )
        object.__setattr__(self, "start_ns", start)
        object.__setattr__(self, "stop_ns", stop)


@dataclass(frozen=True)
class LifetimeFitConfig:
    """Configuration for a per-pixel lifetime fit."""

    method: str = "moment"
    min_counts_per_pixel: int = 20
    laser_rep_rate_mhz: float | None = None

    def __post_init__(self) -> None:
        method = str(self.method).strip().lower()
        if method not in {"moment", "phasor", "exp1"}:
            raise ValueError(
                f"Unsupported lifetime fit method {self.method!r}; "
                "expected 'moment', 'phasor', or 'exp1'"
            )
        object.__setattr__(self, "method", method)
        object.__setattr__(
            self,
            "min_counts_per_pixel",
            max(0, int(self.min_counts_per_pixel)),
        )
        if self.laser_rep_rate_mhz is not None:
            rate = float(self.laser_rep_rate_mhz)
            if not np.isfinite(rate) or rate <= 0:
                raise ValueError("laser_rep_rate_mhz must be positive")
            object.__setattr__(self, "laser_rep_rate_mhz", rate)


@dataclass(frozen=True)
class TimeResolvedScanConfig:
    """Opt-in products requested from a time-resolved detector scan."""

    capture_cube: bool = False
    gates: tuple[GateSpec, ...] = ()
    fit: LifetimeFitConfig = field(default_factory=LifetimeFitConfig)
    include_live_products: bool = False
    max_retained_products: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "capture_cube", bool(self.capture_cube))
        object.__setattr__(self, "gates", tuple(self.gates or ()))
        object.__setattr__(
            self, "include_live_products", bool(self.include_live_products)
        )
        object.__setattr__(
            self, "max_retained_products", max(1, int(self.max_retained_products))
        )
        # Validate duplicate gate names at config construction time.
        names = [gate.name for gate in self.gates]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(
                "Duplicate time gate names: " + ", ".join(repr(n) for n in duplicates)
            )


@dataclass
class TimeResolvedScanProducts:
    """Standard output object for one time-resolved scan product snapshot."""

    cube_counts: np.ndarray | None
    cube_axes: tuple[str, ...]
    t_axis_ns: np.ndarray
    intensity: np.ndarray
    lifetime_ns: np.ndarray | None
    gate_images: dict[str, np.ndarray]
    decay_counts: np.ndarray
    global_tau_ns: float
    metadata: dict[str, Any]
    is_final: bool


def copy_time_resolved_products(
    products: TimeResolvedScanProducts | None,
) -> TimeResolvedScanProducts | None:
    """Return a defensive copy of a products object."""

    if products is None:
        return None
    return TimeResolvedScanProducts(
        cube_counts=(
            None if products.cube_counts is None else np.array(products.cube_counts, copy=True)
        ),
        cube_axes=tuple(products.cube_axes),
        t_axis_ns=np.array(products.t_axis_ns, copy=True),
        intensity=np.array(products.intensity, copy=True),
        lifetime_ns=(
            None if products.lifetime_ns is None else np.array(products.lifetime_ns, copy=True)
        ),
        gate_images={
            name: np.array(image, copy=True)
            for name, image in products.gate_images.items()
        },
        decay_counts=np.array(products.decay_counts, copy=True),
        global_tau_ns=float(products.global_tau_ns),
        metadata=copy.deepcopy(products.metadata),
        is_final=bool(products.is_final),
    )
