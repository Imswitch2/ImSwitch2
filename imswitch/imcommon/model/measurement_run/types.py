"""Value types shared by measurement runs: samples, windows, control results.

A measurement run (``docs/design/plans/transient-instruments-step-scans.md``)
sets controls to a sequence of points and, at each point, samples
instruments inside an *acquisition window*. These types are what crosses the
boundaries between instrument, run engine, storage and analysis. They are
plain frozen dataclasses: no Qt, no hardware, no file handles.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Optional, Tuple


class Verification(str, Enum):
    """Whether the timing rule a sample was accepted under is rig-verified."""

    VERIFIED = 'verified'
    UNVERIFIED = 'unverified'


class TimingRule(str, Enum):
    """How an instrument proves a sample was acquired after a boundary."""

    #: Samples carry their acquisition start (device clock or counter).
    DEVICE_TIMESTAMP = 'device_timestamp'
    #: Each read triggers a fresh measurement that starts after the command.
    TRIGGERED = 'triggered'
    #: No device timing; a verified bound after the boundary is waited out.
    UPDATE_BOUND = 'update_bound'


class WindowCause(str, Enum):
    """Why an acquisition window ended without its full sample count."""

    TIMEOUT = 'timeout'
    CANCELLED = 'cancelled'
    TRANSPORT_FAULT = 'transport_fault'
    INVALID_SAMPLES = 'invalid_samples'
    #: The window could not be opened at all (e.g. timing not verified).
    REFUSED = 'refused'


class PointStatus(str, Enum):
    """Outcome of one measurement point, as committed to storage."""

    #: Every control applied and every instrument window complete.
    COMMITTED = 'committed'
    #: Some samples were taken, but the point is not complete.
    FAILED_PARTIAL = 'failed_partial'
    #: Nothing usable was measured at this point.
    FAILED = 'failed'


class AcquisitionOutcome(str, Enum):
    RUNNING = 'running'
    COMPLETE = 'complete'
    STOPPED = 'stopped'
    FAILED = 'failed'
    #: Found on disk without a final outcome: the writer did not finish.
    INTERRUPTED = 'interrupted'


class CleanupOutcome(str, Enum):
    PENDING = 'pending'
    RUNNING = 'running'
    DONE = 'done'
    FAILED = 'failed'
    #: A backend is still executing an earlier command; its cleanup step was
    #: not dispatched and the resource stays held.
    QUARANTINED = 'quarantined'


class RunLifecycle(str, Enum):
    ACTIVE = 'active'
    CLEANING_UP = 'cleaning_up'
    FINISHED = 'finished'


@dataclass(frozen=True)
class QuantitySpec:
    """One quantity an instrument reports in every sample."""

    name: str
    unit: str
    #: Stable semantic id, e.g. ``polarisation.azimuth`` or ``optical.power``.
    quantity: str
    valid_min: float = -math.inf
    valid_max: float = math.inf

    def check(self, value: float) -> Optional[str]:
        """Return why ``value`` is invalid for this quantity, or ``None``."""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return f'{self.name}: not a number ({value!r})'
        if not math.isfinite(number):
            return f'{self.name}: not finite'
        if number < self.valid_min or number > self.valid_max:
            return (
                f'{self.name}: {number:g} outside '
                f'[{self.valid_min:g}, {self.valid_max:g}]'
            )
        return None


@dataclass(frozen=True)
class Sample:
    """All quantities of ONE instrument read. Quantities are never split."""

    values: Mapping[str, float]
    t_host: float
    generation: int
    sequence: int
    device_t: Optional[float] = None
    device_id: Optional[int] = None
    valid: bool = True
    reason: str = ''
    verification: Verification = Verification.UNVERIFIED
    profile_id: Optional[str] = None


@dataclass(frozen=True)
class Boundary:
    """The moment after which a window's samples must have been acquired."""

    t_host: float
    generation: int
    verification: Verification
    profile_id: Optional[str]
    rule: Optional[TimingRule]
    #: Device clock at the boundary, mapped from host time; ``None`` if the
    #: instrument has no clock.
    device_t: Optional[float] = None
    #: Uncertainty of the host↔device clock mapping (half the round trip).
    clock_uncertainty_s: float = 0.0
    #: Device sample counter read after settle; ``None`` if none.
    device_counter: Optional[int] = None


@dataclass(frozen=True)
class WindowResult:
    """What one acquisition window produced, complete or not.

    Never raised as an exception: a timeout, cancellation, transport fault or
    a run of invalid samples ends the window with ``complete=False`` and a
    ``cause``, and the samples already accepted are kept here so the run can
    commit them.
    """

    samples: Tuple[Sample, ...]
    invalid: Tuple[Sample, ...]
    discarded: int
    complete: bool
    cause: Optional[WindowCause]
    verification: Verification
    profile_id: Optional[str]
    boundary_t: float
    requested: int = 0
    detail: str = ''


@dataclass(frozen=True)
class ControlResult:
    """Observable outcome of setting one control at one point."""

    control: str
    requested: float
    #: What the device or driver confirmed it applied; ``None`` if it cannot say.
    acknowledged: Optional[float] = None
    #: Fresh readback after settling; ``None`` unless the readback is FRESH.
    measured: Optional[float] = None
    ok: bool = True
    cause: str = ''


@dataclass(frozen=True)
class GridInfo:
    """Grid metadata when a point sequence came from a grid generator."""

    #: Axes outermost first: ``(control name, values)``.
    axes: Tuple[Tuple[str, Tuple[float, ...]], ...]
    traversal: str
    #: For each point in sequence order, its index tuple into the grid.
    index: Tuple[Tuple[int, ...], ...] = field(default_factory=tuple)

    @property
    def shape(self) -> Tuple[int, ...]:
        return tuple(len(values) for _, values in self.axes)
