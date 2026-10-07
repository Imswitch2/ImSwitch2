"""Instruments: drivers, timing profiles, and the session that samples them.

An instrument driver talks to one physical device and returns *raw readings*.
The :class:`InstrumentSession` around it is the only caller of the driver.
It owns the I/O lock and the sample generation, and it implements
acquisition windows (``docs/design/plans/transient-instruments-step-scans.md``
§6.3): after a control change and settle, :meth:`InstrumentSession.open_window`
records a boundary, and :meth:`InstrumentSession.sample_window` returns only
samples that were provably acquired **after** that boundary.

What counts as proof depends on the instrument's operating configuration. A
driver therefore ships a table of :class:`TimingProfile`\\ s. The session
matches the current settings and firmware against that table on connect and
after every settings change. Outside every verified profile, acquisition is
``UNVERIFIED``: live display works, but quantitative windows are refused
unless the caller explicitly allows unverified timing (characterisation).
"""
from __future__ import annotations

import collections
import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from imswitch.imcommon.model.measurement_run import (
    Boundary,
    QuantitySpec,
    Sample,
    TimingRule,
    Verification,
    WindowCause,
    WindowResult,
)

_logger = logging.getLogger(__name__)


class TransportError(RuntimeError):
    """The connection to the instrument failed (timeout, I/O error, device gone).

    Faults the session. Distinct from :class:`MalformedReading`.
    """


class MalformedReading(ValueError):
    """The instrument answered, but the reading is unusable.

    The reading becomes an *invalid sample*; the connection stays up.
    """


class WindowRefused(RuntimeError):
    """An acquisition window cannot be opened (e.g. timing not verified)."""


class WindowFault(WindowRefused):
    """Opening the window hit a transport error; the session is now faulted."""


@dataclass(frozen=True)
class InstrumentIdentity:
    vendor: str
    model: str
    serial: str
    firmware: str = ''

    def as_dict(self) -> Dict[str, str]:
        return {'vendor': self.vendor, 'model': self.model,
                'serial': self.serial, 'firmware': self.firmware}


@dataclass(frozen=True)
class RawReading:
    values: Mapping[str, float]
    #: Device time at which this measurement STARTED (device clock), if known.
    device_t: Optional[float] = None
    #: Device-side sample counter / id, if the instrument has one.
    device_id: Optional[int] = None


@dataclass(frozen=True)
class TimingProfile:
    """One operating configuration and how timing is proven within it."""

    id: str
    rule: TimingRule
    #: True only once the rule was checked on real hardware in this profile.
    verified: bool
    #: Setting name → exact value, or ``(min, max)`` inclusive range.
    conditions: Mapping[str, Any] = field(default_factory=dict)
    #: Accepted firmware strings; empty means any.
    firmware: Tuple[str, ...] = ()
    #: UPDATE_BOUND: a sample returned this long after the boundary was
    #: acquired entirely after it.
    update_bound_s: float = 0.0
    #: UPDATE_BOUND without device ids: minimum spacing of distinct samples.
    update_period_s: float = 0.0
    #: DEVICE_TIMESTAMP with counters: samples whose counter is at most
    #: ``boundary counter + allowance`` may have started before the boundary.
    in_flight_allowance: int = 0
    #: TRIGGERED: integration time of one triggered measurement.
    integration_s: float = 0.0

    def matches(self, settings: Mapping[str, Any], firmware: str) -> bool:
        if self.firmware and firmware not in self.firmware:
            return False
        for name, condition in self.conditions.items():
            if name not in settings:
                return False
            value = settings[name]
            if isinstance(condition, tuple) and len(condition) == 2:
                low, high = condition
                try:
                    if not (low <= value <= high):
                        return False
                except TypeError:
                    return False
            elif value != condition:
                return False
        return True


@dataclass(frozen=True)
class SettingSpec:
    name: str
    label: str
    unit: str = ''


@dataclass(frozen=True)
class ActionSpec:
    name: str
    label: str
    confirm: str = ''
    #: Needs an explicit "the beam is blocked" confirmation.
    requires_dark: bool = False
    timeout_s: float = 10.0


class InstrumentDriver(ABC):
    """Talks to one physical instrument. Called only by its session."""

    quantities: Tuple[QuantitySpec, ...] = ()
    timing_profiles: Tuple[TimingProfile, ...] = ()
    settings_spec: Tuple[SettingSpec, ...] = ()
    actions_spec: Tuple[ActionSpec, ...] = ()
    #: Consecutive invalid samples that end a window with INVALID_SAMPLES.
    invalid_limit: int = 5
    #: Pause between reads while waiting for a new sample.
    poll_interval_s: float = 0.005

    @abstractmethod
    def connect(self) -> InstrumentIdentity: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def read(self) -> RawReading:
        """One read. Raise TransportError or MalformedReading on failure."""

    def settings(self) -> Dict[str, Any]:
        return {}

    def set_setting(self, name: str, value: Any) -> Any:
        raise KeyError(name)

    def run_action(self, name: str, **kwargs: Any) -> None:
        raise KeyError(name)

    def device_clock(self) -> Optional[float]:
        """Current device time, if the device has a readable clock."""
        return None

    def device_counter(self) -> Optional[int]:
        """Current device sample counter, if any."""
        return None


class InstrumentSession:
    """The one owner of an instrument driver: lock, generations, windows."""

    def __init__(
        self,
        name: str,
        driver: InstrumentDriver,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        ring_size: int = 2000,
    ) -> None:
        self.name = name
        self.driver = driver
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.RLock()
        self._generation = 0
        self._sequence = 0
        self._identity: Optional[InstrumentIdentity] = None
        self._connected = False
        self._faulted: Optional[str] = None
        self._profile: Optional[TimingProfile] = None
        self._ring: collections.deque = collections.deque(maxlen=ring_size)
        self._fault_listeners: List[Callable[[str, str], None]] = []
        self._sample_listeners: List[Callable[[str, Sample], None]] = []

    # ------------------------------------------------------------ properties
    @property
    def quantities(self) -> Tuple[QuantitySpec, ...]:
        return tuple(self.driver.quantities)

    @property
    def identity(self) -> Optional[InstrumentIdentity]:
        return self._identity

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def faulted(self) -> Optional[str]:
        return self._faulted

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def profile(self) -> Optional[TimingProfile]:
        return self._profile

    @property
    def verification(self) -> Verification:
        if self._profile is not None and self._profile.verified:
            return Verification.VERIFIED
        return Verification.UNVERIFIED

    def recent_samples(self) -> List[Sample]:
        with self._lock:
            return list(self._ring)

    def add_fault_listener(self, callback: Callable[[str, str], None]) -> None:
        self._fault_listeners.append(callback)

    def remove_fault_listener(self, callback) -> None:
        try:
            self._fault_listeners.remove(callback)
        except ValueError:
            pass

    def add_sample_listener(self, callback: Callable[[str, Sample], None]) -> None:
        self._sample_listeners.append(callback)

    # ------------------------------------------------------------ lifecycle
    def connect(self) -> InstrumentIdentity:
        with self._lock:
            identity = self.driver.connect()
            self._identity = identity
            self._connected = True
            self._faulted = None
            self._generation += 1
            self._ring.clear()
            self._evaluate_profile()
            return identity

    def close(self) -> None:
        with self._lock:
            try:
                if self._connected:
                    self.driver.close()
            finally:
                self._connected = False
                self._generation += 1
                self._ring.clear()

    def report_fault(self, cause: str) -> None:
        """Mark the session faulted: new generation, cache dropped, listeners told."""
        with self._lock:
            if self._faulted is not None:
                return
            self._faulted = cause
            self._generation += 1
            self._ring.clear()
        _logger.warning('Instrument %s faulted: %s', self.name, cause)
        for callback in list(self._fault_listeners):
            try:
                callback(self.name, cause)
            except Exception:
                _logger.exception('fault listener failed')

    # -------------------------------------------------------------- settings
    def settings(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self.driver.settings())

    def set_setting(self, name: str, value: Any) -> Any:
        """Apply a setting; the readback is what applies. Re-matches profiles."""
        with self._lock:
            self._require_ready()
            applied = self.driver.set_setting(name, value)
            self._evaluate_profile()
            return applied

    def run_action(self, name: str, *, confirm_dark: bool = False, **kwargs: Any) -> None:
        spec = next((a for a in self.driver.actions_spec if a.name == name), None)
        if spec is None:
            raise KeyError(f'{self.name} has no action {name!r}')
        if spec.requires_dark and not confirm_dark:
            raise PermissionError(
                f'{self.name}.{name} requires the beam to be blocked; '
                f'pass confirm_dark=True once that is confirmed'
            )
        with self._lock:
            self._require_ready()
            self.driver.run_action(name, **kwargs)

    def _evaluate_profile(self) -> None:
        settings = self.driver.settings()
        firmware = self._identity.firmware if self._identity else ''
        self._profile = next(
            (p for p in self.driver.timing_profiles if p.matches(settings, firmware)),
            None,
        )

    def _require_ready(self) -> None:
        if not self._connected:
            raise WindowRefused(f'instrument {self.name} is not connected')
        if self._faulted is not None:
            raise WindowRefused(f'instrument {self.name} is faulted: {self._faulted}')

    # --------------------------------------------------------------- windows
    def open_window(self, *, allow_unverified: bool = False) -> Boundary:
        """Record the boundary samples of the next window must follow."""
        with self._lock:
            self._require_ready()
            profile = self._profile
            if profile is None:
                raise WindowRefused(
                    f'instrument {self.name}: no timing profile matches the current '
                    f'settings {self.driver.settings()!r}'
                )
            verification = (
                Verification.VERIFIED if profile.verified else Verification.UNVERIFIED
            )
            if verification is Verification.UNVERIFIED and not allow_unverified:
                raise WindowRefused(
                    f'instrument {self.name}: timing profile {profile.id!r} is not '
                    f'verified on hardware'
                )
            device_t = None
            uncertainty = 0.0
            counter = None
            fault = None
            if profile.rule is TimingRule.DEVICE_TIMESTAMP:
                try:
                    counter = self.driver.device_counter()
                    t0 = self._clock()
                    device_now = self.driver.device_clock()
                    t1 = self._clock()
                except TransportError as exc:
                    fault = str(exc) or 'transport error'
                else:
                    if device_now is not None:
                        # The boundary instant is t1. Device time at t1 lies in
                        # [device_now, device_now + (t1 - t0)]; take the late end.
                        uncertainty = t1 - t0
                        device_t = device_now + uncertainty
                    if counter is None and device_t is None:
                        raise WindowRefused(
                            f'instrument {self.name}: profile {profile.id!r} needs a '
                            f'device clock or counter, the driver reports neither'
                        )
            if fault is None:
                return Boundary(
                    t_host=self._clock(), generation=self._generation,
                    verification=verification, profile_id=profile.id, rule=profile.rule,
                    device_t=device_t, clock_uncertainty_s=uncertainty,
                    device_counter=counter,
                )
        self.report_fault(fault)
        raise WindowFault(f'instrument {self.name}: {fault}')

    def sample_window(
        self,
        boundary: Boundary,
        n: int,
        deadline_s: float,
        cancel: Optional[threading.Event] = None,
    ) -> WindowResult:
        """``n`` distinct samples acquired after ``boundary`` — or fewer, with a cause."""
        profile = next(
            (p for p in self.driver.timing_profiles if p.id == boundary.profile_id), None)
        deadline = self._clock() + float(deadline_s)
        accepted: List[Sample] = []
        invalid: List[Sample] = []
        discarded = 0
        seen_ids = set()
        last_t: Optional[float] = None
        consecutive_invalid = 0
        cause: Optional[WindowCause] = None
        detail = ''

        while len(accepted) < n:
            if cancel is not None and cancel.is_set():
                cause, detail = WindowCause.CANCELLED, 'cancelled'
                break
            if self._clock() > deadline:
                cause = WindowCause.TIMEOUT
                detail = f'{len(accepted)} of {n} samples within {deadline_s:g} s'
                break
            with self._lock:
                if self._generation != boundary.generation or self._faulted:
                    cause = WindowCause.TRANSPORT_FAULT
                    detail = self._faulted or 'instrument reconnected during the window'
                    break
                if profile is None or profile.id != (self._profile.id if self._profile else None):
                    cause = WindowCause.CANCELLED
                    detail = 'timing profile changed during the window'
                    break
                fault = None
                raw = malformed = None
                try:
                    raw = self.driver.read()
                except MalformedReading as exc:
                    malformed = str(exc) or 'malformed reading'
                except TransportError as exc:
                    fault = str(exc) or 'transport error'
                t_host = self._clock()
                self._sequence += 1
                sequence = self._sequence
                generation = self._generation
            if fault is not None:
                # Outside the lock: listeners (e.g. the run engine) must not
                # run while the instrument lock is held.
                self.report_fault(fault)
                cause, detail = WindowCause.TRANSPORT_FAULT, fault
                break

            sample = self._make_sample(raw, malformed, t_host, generation, sequence, boundary)
            if not sample.valid:
                invalid.append(sample)
                consecutive_invalid += 1
                if consecutive_invalid > self.driver.invalid_limit:
                    cause = WindowCause.INVALID_SAMPLES
                    detail = f'{consecutive_invalid} invalid samples in a row: {sample.reason}'
                    break
                self._sleep(self.driver.poll_interval_s)
                continue
            consecutive_invalid = 0

            if self._accept(sample, boundary, profile, seen_ids, last_t):
                accepted.append(sample)
                if sample.device_id is not None:
                    seen_ids.add(sample.device_id)
                elif sample.device_t is not None:
                    seen_ids.add(('t', sample.device_t))
                last_t = sample.t_host
                self._publish(sample)
            else:
                discarded += 1
                self._sleep(self.driver.poll_interval_s)

        return WindowResult(
            samples=tuple(accepted), invalid=tuple(invalid), discarded=discarded,
            complete=len(accepted) >= n and cause is None, cause=cause,
            verification=boundary.verification, profile_id=boundary.profile_id,
            boundary_t=boundary.t_host, requested=int(n), detail=detail,
        )

    # --------------------------------------------------------------- helpers
    def _make_sample(self, raw, malformed, t_host, generation, sequence, boundary) -> Sample:
        if raw is None:
            return Sample(values={}, t_host=t_host, generation=generation,
                          sequence=sequence, valid=False, reason=malformed or 'no data',
                          verification=boundary.verification,
                          profile_id=boundary.profile_id)
        reasons = []
        for spec in self.driver.quantities:
            if spec.name not in raw.values:
                reasons.append(f'{spec.name}: missing')
                continue
            problem = spec.check(raw.values[spec.name])
            if problem:
                reasons.append(problem)
        return Sample(
            values={k: float(v) if _is_number(v) else float('nan')
                    for k, v in raw.values.items()},
            t_host=t_host, generation=generation, sequence=sequence,
            device_t=raw.device_t, device_id=raw.device_id,
            valid=not reasons, reason='; '.join(reasons),
            verification=boundary.verification, profile_id=boundary.profile_id,
        )

    def _accept(self, sample: Sample, boundary: Boundary, profile, seen_ids, last_t) -> bool:
        rule = boundary.rule
        if rule is TimingRule.TRIGGERED:
            return True
        if rule is TimingRule.DEVICE_TIMESTAMP:
            if sample.device_id is not None and sample.device_id in seen_ids:
                return False
            if boundary.device_counter is not None:
                if sample.device_id is None:
                    return False
                allowance = profile.in_flight_allowance if profile else 0
                return sample.device_id > boundary.device_counter + allowance
            if boundary.device_t is not None:
                if sample.device_t is None or ('t', sample.device_t) in seen_ids:
                    return False
                return sample.device_t >= boundary.device_t
            return False
        if rule is TimingRule.UPDATE_BOUND:
            bound = profile.update_bound_s if profile else 0.0
            if sample.t_host < boundary.t_host + bound:
                return False
            if sample.device_id is not None:
                return sample.device_id not in seen_ids
            period = profile.update_period_s if profile else 0.0
            return last_t is None or sample.t_host - last_t >= period
        return False

    def _publish(self, sample: Sample) -> None:
        with self._lock:
            self._ring.append(sample)
        for callback in list(self._sample_listeners):
            try:
                callback(self.name, sample)
            except Exception:
                _logger.exception('sample listener failed')


def _is_number(value) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False
