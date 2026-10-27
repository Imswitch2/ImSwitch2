"""Hardware-free controls and instruments for measurement runs.

:class:`MockRotatorControl` moves at a finite speed and records its motion
history, so a mock instrument can ask where a waveplate *was* at any time.
:class:`MockPAXDriver` is a polarimeter behind two such waveplates. It
simulates a rotating-waveplate measurement of finite duration, so a sample
can deliberately straddle a move. With known plate parameters, a whole run
has a known answer.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from imswitch.imcommon.algorithms.polarisation import TwoPlateModel, angles_from_direction
from imswitch.imcommon.model.measurement_run import ControlResult, TimingRule

from .controls import READBACK_FRESH, ControlCapabilities, RunControl
from .instrument import (
    ActionSpec,
    InstrumentDriver,
    InstrumentIdentity,
    MalformedReading,
    RawReading,
    SettingSpec,
    TimingProfile,
    TransportError,
)
from .quantities import PAX_QUANTITIES, POWER_QUANTITY  # noqa: F401 (re-exported)


class MockRotatorControl(RunControl):
    """A rotator with finite speed, fresh readback and injectable failures."""

    def __init__(
        self,
        name: str,
        *,
        speed_deg_s: float = 3600.0,
        start_deg: float = 0.0,
        resource: Optional[str] = None,
        clock: Callable[[], float] = time.monotonic,
        settle_s: float = 0.0,
        tolerance: Optional[float] = 0.05,
        can_stop: bool = True,
    ) -> None:
        self.name = name
        self.resource = resource or f'mock-rotator:{name}'
        self.capabilities = ControlCapabilities(
            unit='deg', can_stop=can_stop, readback=READBACK_FRESH,
            acknowledges=True, settle_s=settle_s, tolerance=tolerance,
        )
        self._speed = float(speed_deg_s)
        self._clock = clock
        self._lock = threading.Lock()
        # Motion history: (t0, t1, a0, a1) segments, linear in between.
        self._segments: List[Tuple[float, float, float, float]] = [
            (-math.inf, clock(), float(start_deg), float(start_deg))]
        self._stop = threading.Event()
        #: Failure injection.
        self.fail_next: Optional[str] = None
        self.stall = threading.Event()          # set → the next move blocks ...
        self.release_stall = threading.Event()  # ... until this is set
        self.moves = 0

    # ----------------------------------------------------------- motion model
    def angle_at(self, t: float) -> float:
        with self._lock:
            segments = list(self._segments)
        for t0, t1, a0, a1 in reversed(segments):
            if t >= t0:
                if t >= t1 or t1 == t0 or math.isinf(t0):
                    return a1 if t >= t1 or math.isinf(t0) else a0
                return a0 + (a1 - a0) * (t - t0) / (t1 - t0)
        return segments[0][2]

    def _now_angle(self) -> float:
        return self.angle_at(self._clock())

    # --------------------------------------------------------------- control
    def apply(self, value: float, token: Optional[str] = None) -> ControlResult:
        target = float(value)
        if self.fail_next is not None:
            cause, self.fail_next = self.fail_next, None
            return ControlResult(self.name, requested=target, ok=False, cause=cause)
        if self.stall.is_set():
            self.release_stall.wait()   # a stuck driver: ignores stop()
        self._stop.clear()
        t0 = self._clock()
        a0 = self._now_angle()
        duration = abs(target - a0) / self._speed if self._speed > 0 else 0.0
        with self._lock:
            self._segments.append((t0, t0 + duration, a0, target))
        end = t0 + duration
        while self._clock() < end:
            if self._stop.wait(min(0.002, max(0.0, end - self._clock()))):
                now = self._clock()
                here = self.angle_at(now)
                with self._lock:
                    self._segments.append((now, now, here, here))
                return ControlResult(self.name, requested=target, acknowledged=None,
                                     measured=here, ok=False, cause='stopped')
        self.moves += 1
        return ControlResult(self.name, requested=target, acknowledged=target,
                             measured=self._now_angle(), ok=True)

    def stop(self) -> None:
        self._stop.set()

    def read_position(self) -> Optional[float]:
        return self._now_angle()


class MockPAXDriver(InstrumentDriver):
    """A rotating-waveplate polarimeter behind two mock waveplates.

    Measurement *k* integrates over ``[origin + k·T, origin + (k+1)·T)``. A
    read returns the latest measurement completed at least ``latency_s``
    earlier, with its start time on the device clock and *k* as the counter,
    so a sample taken during a move really does mix the states.
    """

    quantities = PAX_QUANTITIES
    settings_spec = (SettingSpec('wavelength_nm', 'Wavelength', 'nm'),
                     SettingSpec('mode', 'Measurement mode'))
    actions_spec = ()

    def __init__(
        self,
        plate1: MockRotatorControl,
        plate2: MockRotatorControl,
        *,
        model: TwoPlateModel = TwoPlateModel(),
        revolution_s: float = 0.01,
        latency_s: float = 0.0,
        has_counter: bool = True,
        has_clock: bool = True,
        device_clock_offset_s: float = 1000.0,
        power_w: float = 1e-3,
        dop: float = 0.995,
        noise_deg: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        verified: bool = True,
        serial: str = 'MOCK0001',
        seed: int = 0,
    ) -> None:
        self.plate1, self.plate2 = plate1, plate2
        self.model = model
        self.revolution_s = float(revolution_s)
        self.latency_s = float(latency_s)
        self.has_counter = has_counter
        self.has_clock = has_clock
        self.offset = float(device_clock_offset_s)
        self.power_w = float(power_w)
        self.dop = float(dop)
        self.noise = math.radians(noise_deg)
        self._clock = clock
        self._origin = clock()
        self._rng = np.random.default_rng(seed)
        self._settings = {'wavelength_nm': 633.0, 'mode': 9}
        self._serial = serial
        self._connected = False
        #: Failure injection.
        self.unplugged = False
        self.invalid_next = 0
        self.malformed_next = 0
        self.reads = 0
        rule = TimingRule.DEVICE_TIMESTAMP if (has_counter or has_clock) else TimingRule.UPDATE_BOUND
        self.timing_profiles = (
            TimingProfile(
                id='mock-mode9', rule=rule, verified=verified,
                conditions={'mode': 9},
                update_bound_s=2 * self.revolution_s + self.latency_s,
                update_period_s=self.revolution_s,
            ),
        )

    # ------------------------------------------------------------ lifecycle
    def connect(self) -> InstrumentIdentity:
        if self.unplugged:
            raise TransportError('mock PAX not found')
        self._connected = True
        return InstrumentIdentity('Mock', 'PAX1000 (simulated)', self._serial, 'mock-1.0')

    def close(self) -> None:
        self._connected = False

    def settings(self):
        return dict(self._settings)

    def set_setting(self, name, value):
        if name not in self._settings:
            raise KeyError(name)
        self._settings[name] = type(self._settings[name])(value)
        return self._settings[name]

    def device_clock(self) -> Optional[float]:
        if self.unplugged:
            raise TransportError('mock PAX disconnected')
        return self._clock() + self.offset if self.has_clock else None

    def device_counter(self) -> Optional[int]:
        if not self.has_counter:
            return None
        return int(math.floor((self._clock() - self._origin) / self.revolution_s))

    # ----------------------------------------------------------------- read
    def read(self) -> RawReading:
        if self.unplugged:
            raise TransportError('mock PAX disconnected (USB removed)')
        self.reads += 1
        if self.malformed_next > 0:
            self.malformed_next -= 1
            raise MalformedReading('packet has 7 fields, expected 13')
        now = self._clock()
        k = int(math.floor((now - self.latency_s - self._origin) / self.revolution_s)) - 1
        start = self._origin + k * self.revolution_s
        stokes = self._integrate(start, start + self.revolution_s)
        s0 = stokes[0]
        pol = stokes[1:]
        norm = float(np.linalg.norm(pol))
        psi, chi = angles_from_direction(pol)
        psi = float(psi) + self._rng.normal(0.0, self.noise) if self.noise else float(psi)
        chi = float(chi) + self._rng.normal(0.0, self.noise) if self.noise else float(chi)
        psi = (psi + math.pi / 2) % math.pi - math.pi / 2
        chi = max(-math.pi / 4, min(math.pi / 4, chi))
        values = {
            'azimuth': psi,
            'ellipticity': chi,
            'dop': self.dop * norm / s0 if s0 > 0 else 0.0,
            'power': self.power_w * s0,
        }
        if self.invalid_next > 0:
            self.invalid_next -= 1
            values['azimuth'] = 7.5    # outside its valid range
        return RawReading(
            values=values,
            device_t=start + self.offset if self.has_clock else None,
            device_id=k if self.has_counter else None,
        )

    def _integrate(self, t0: float, t1: float, steps: int = 8) -> np.ndarray:
        total = np.zeros(4)
        for t in np.linspace(t0, t1, steps, endpoint=False) + (t1 - t0) / (2 * steps):
            total += self.model.output(self.plate1.angle_at(t), self.plate2.angle_at(t))
        return total / steps

    def expected_direction(self, angle1: float, angle2: float) -> np.ndarray:
        out = self.model.output(angle1, angle2)
        return out[1:] / np.linalg.norm(out[1:])


class MockLaser(RunControl):
    """A laser with a raw drive (V), an emission switch and a UI value.

    As a run control it sets the **raw** drive. As a laser state (see
    ``laser_lut.LaserState``) it reports and restores the UI value and the
    enabled state. With ``uses_lut`` its UI value is a % setpoint that a
    loaded LUT maps to a drive — as on a real laser with ``calibCsvPath``.
    Emission: ``response(drive) + leak`` while enabled, 0 while disabled.
    """

    def __init__(self, name: str = '775', *, wavelength_nm: float = 775.0,
                 drive_max: float = 5.0, power_max_w: float = 0.01, leak_w: float = 2e-5,
                 response: Optional[Callable[[float], float]] = None, value: float = 0.0,
                 enabled: bool = False, uses_lut: bool = False) -> None:
        self.name = name
        self.resource = f'laser:{name}'
        self.capabilities = ControlCapabilities(
            unit='V', can_stop=False, readback='none', acknowledges=True, settle_s=0.0)
        self.wavelength_nm = float(wavelength_nm)
        self.raw_unit = 'V'
        self.drive_min, self.drive_max = 0.0, float(drive_max)
        self.leak_w = float(leak_w)
        self.response = response or (
            lambda d: power_max_w * math.sin(0.5 * math.pi * min(max(d, 0.0), drive_max)
                                              / drive_max) ** 2)
        self.uses_lut = uses_lut
        self.value = float(value)
        self.enabled = bool(enabled)
        self.drive = self._drive_for(self.value)
        self.fail_restore: Optional[str] = None
        self.events: List[Tuple[str, float]] = []

    def _drive_for(self, value: float) -> float:
        # A loaded LUT maps a % setpoint linearly onto the drive (good enough
        # for a mock: what matters is that it is not the raw value).
        return value / 100.0 * self.drive_max if self.uses_lut else value

    def emitted_w(self) -> float:
        return self.response(self.drive) + self.leak_w if self.enabled else 0.0

    # run control: raw drive
    def apply(self, value: float, token: Optional[str] = None) -> ControlResult:
        drive = float(value)
        if not self.drive_min <= drive <= self.drive_max:
            return ControlResult(self.name, requested=drive, ok=False,
                                 cause=f'{drive:g} V outside [0, {self.drive_max:g}] V')
        self.drive = drive
        self.events.append(('drive', drive))
        return ControlResult(self.name, requested=drive, acknowledged=drive, ok=True)

    # laser state
    def read_state(self) -> Tuple[float, bool]:
        return self.value, self.enabled

    def set_enabled(self, enabled: bool, token: Optional[str] = None) -> None:
        self.enabled = bool(enabled)
        self.events.append(('enabled', float(enabled)))

    def restore(self, value: float, enabled: bool, token: Optional[str] = None) -> None:
        if self.fail_restore:
            raise RuntimeError(self.fail_restore)
        self.value = float(value)
        self.drive = self._drive_for(self.value)
        self.enabled = bool(enabled)
        self.events.append(('restore', float(value)))


class MockPowerMeterDriver(InstrumentDriver):
    """A PM100-like meter reading the light of mock lasers.

    ``zero`` stores the *current* reading as the offset — with the beam
    blocked that is the ambient light; with a laser on it bakes that laser
    into every later reading, as a real meter would.
    """

    quantities = (POWER_QUANTITY,)
    settings_spec = (SettingSpec('wavelength_nm', 'Wavelength', 'nm'),)
    actions_spec = (ActionSpec('zero', 'Zero', confirm='Block the beam at the sensor',
                               requires_dark=True),)

    def __init__(self, lasers: Sequence['MockLaser'] = (), *, transmission: float = 0.8,
                 ambient_w: float = 3e-6, noise_w: float = 2e-7,
                 sensor_range_nm: Tuple[float, float] = (400.0, 1100.0),
                 verified: bool = True, serial: str = 'P0011748', seed: int = 0) -> None:
        self.lasers = list(lasers)
        self.transmission = float(transmission)
        self.ambient_w = float(ambient_w)
        self.noise_w = float(noise_w)
        self.sensor_range = sensor_range_nm
        self._rng = np.random.default_rng(seed)
        self._settings = {'wavelength_nm': 633.0}
        self.zero_offset = 0.0
        self.zeroed_with_w: Optional[float] = None
        self._serial = serial
        self.unplugged = False
        self.timing_profiles = (
            TimingProfile(id='mock-pm100-read', rule=TimingRule.TRIGGERED,
                          verified=verified, integration_s=0.0),
        )

    def connect(self) -> InstrumentIdentity:
        if self.unplugged:
            raise TransportError('mock PM100 not found')
        return InstrumentIdentity('Mock', 'PM100D (simulated)', self._serial, 'mock-1.0')

    def close(self) -> None:
        pass

    def settings(self):
        return dict(self._settings)

    def set_setting(self, name, value):
        if name != 'wavelength_nm':
            raise KeyError(name)
        low, high = self.sensor_range
        self._settings[name] = float(min(max(float(value), low), high))  # the meter clamps
        return self._settings[name]

    def run_action(self, name, **kwargs):
        if name != 'zero':
            raise KeyError(name)
        self.zero_offset = self._light()
        self.zeroed_with_w = self.zero_offset

    def _light(self) -> float:
        return sum(laser.emitted_w() for laser in self.lasers) * self.transmission + self.ambient_w

    def read(self) -> RawReading:
        if self.unplugged:
            raise TransportError('mock PM100 disconnected')
        noise = self._rng.normal(0.0, self.noise_w) if self.noise_w else 0.0
        return RawReading(values={'power': self._light() - self.zero_offset + noise})
