"""Data containers for generic time-resolved detector products."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

import numpy as np


GATE_REFERENCES = ("peak", "absolute")


@dataclass(frozen=True)
class GateSpec:
    """One time gate in nanoseconds.

    Gate intervals are interpreted as ``start_ns <= t < stop_ns`` by the shared
    processing helpers. ``reference`` says what the bounds are measured
    from: ``"absolute"`` (the default, and what every existing script and
    saved file means) is the histogram's own time axis; ``"peak"`` is the
    IRF peak of the decay the gate is applied to, so a STED preset survives
    a change of ``t0``. New presets and the Lifetime widget ask for
    ``"peak"`` explicitly; the default never changes an old script's image.
    :func:`resolve_gates` turns peak-relative gates into absolute ones once
    the peak is known.
    """

    name: str
    start_ns: float
    stop_ns: float
    reference: str = "absolute"

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
        reference = str(self.reference).strip().lower()
        if reference not in GATE_REFERENCES:
            raise ValueError(
                f"Gate {name!r} reference must be one of {GATE_REFERENCES}, "
                f"not {self.reference!r}"
            )
        object.__setattr__(self, "reference", reference)


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
    """Standard output object for one time-resolved scan product snapshot.

    Version 2 (Lifetime 2.0) adds the fields after ``is_final``, all with
    defaults so a version-1 producer still constructs it: the TCSPC
    direction the frame was taken in, the background rate subtracted, the
    worst per-pixel pile-up, the USB overflows during the scan, how many
    scans were summed, an optional IRF (``{"t_axis_ns", "counts",
    "peak_ns", "fwhm_ns"}``) and the format version. ``cube_counts``, when
    kept, holds the raw integer counts (the background is subtracted from
    the gates and the fits, never from the stored cube).
    """

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
    tcspc_direction: str = "forward"
    background_rate_hz: float = 0.0
    pileup_max: float = 0.0
    overflows: int = 0
    frames_accumulated: int = 1
    irf: dict[str, Any] | None = None
    format_version: int = 2


@dataclass
class LiveProducts:
    """What a time-resolved detector publishes while a scan runs.

    Small by design: the per-pixel cube never travels here (a 512x512x391
    cube is hundreds of megabytes per tick), so a widget on the GUI thread
    can take every one of these. ``is_final`` marks the frame the card
    closed; everything before it is a preview of the same frame filling up.
    """

    intensity: np.ndarray
    """ Photon counts per pixel, ``(Ny, Nx)``. """
    lifetime_ns: np.ndarray | None
    """ Fitted lifetime per pixel in ns, ``0`` where below threshold; ``None``
    when no fit ran on this tick. """
    decay_counts: np.ndarray
    """ The aggregated decay over valid pixels, forward time. """
    t_axis_ns: np.ndarray
    """ Bin centres of ``decay_counts``, forward time, ns. """
    gate_images: dict[str, np.ndarray]
    global_tau_ns: float
    peak_time_ns: float
    """ Where the IRF peak sits on ``t_axis_ns``. """
    background_per_bin: float
    """ Flat background subtracted from every pixel's histogram, counts per
    bin per pixel (``0`` when none). """
    pileup_max: float
    """ Highest photons-per-excitation-pulse fraction of any pixel. """
    tcspc_direction: str
    frame_index: int
    is_final: bool
    metadata: dict[str, Any] = field(default_factory=dict)


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
        tcspc_direction=str(products.tcspc_direction),
        background_rate_hz=float(products.background_rate_hz),
        pileup_max=float(products.pileup_max),
        overflows=int(products.overflows),
        frames_accumulated=int(products.frames_accumulated),
        irf=copy.deepcopy(products.irf),
        format_version=int(products.format_version),
    )
