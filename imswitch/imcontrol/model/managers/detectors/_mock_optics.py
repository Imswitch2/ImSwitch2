"""What the simulated detectors see of the virtual sample during a scan.

:mod:`._mock_sample` is the specimen; this module is the experiment around
it:

* where the specimen is -- the x/y scanner waveforms (``scanSignalsDict``),
  and, with a ``zAxis``, how far out of focus (defocus widens the PSF and
  lowers its peak, and a confocal pinhole rejects out-of-focus light);
* how strongly it is lit -- for each laser named in ``excitation``: its TTL
  in the scan (``TTLCycleSignalsDict``) or, without one, whether it is
  switched on; times its power, from the scan's analog power waveform when
  the scan has one (Advanced line-step power) or else the power it is set
  to. The set state is what the laser controller publishes for each laser
  (:class:`LaserStates`);
* how the fluorophores answer -- they saturate (:class:`Photophysics`):
  emission per fluorophore goes as I / (I + I_sat), a saturated focus is
  wider, and a background (scatter, autofluorescence) grows with the light
  without saturating. With dark counts, camera offset and read noise, and
  Poisson noise on all of it, image quality has an optimum power;
* what the light did to it -- photobleaching, accumulated over the scan in
  the order the scan exposes the sample, in proportion to the light the
  fluorophores emitted.

Power is a fraction of the laser's configured range (``valueRangeMin`` to
``valueRangeMax``) times its ``efficiency``: there is no power calibration
yet. :meth:`Laser.intensity` is the one place a laser's setting becomes
excitation intensity, for a calibration (power in physical units) to
replace.

:class:`PointScanSignal` turns that into the relative brightness under the
beam at every detector sample of a point scan (APD, PMT). The MoNaLISA mock
camera reads the same signals per camera frame in ``_mock_lattice.py``.

Simulation only.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

import numpy as np

from ._mock_sample import BACKGROUND, FIELD_HALF_UM, FWHM_PER_SIGMA, PSF_SIGMA_UM, MockSample

#: Default Rayleigh range (z at which the PSF is sqrt(2) wider), µm.
RAYLEIGH_UM = 0.4


@dataclass(frozen=True)
class Laser:
    """A laser that acts on the sample, as the ``mockSample`` block names it."""

    name: str
    efficiency: float = 1.0
    #: Volts of the laser's analog power waveform that mean full power; None
    #: reads a waveform against the range the laser was last set with, and
    #: ignores it when that is not known either.
    full_scale_v: Optional[float] = None

    def intensity(self, fraction):
        """Excitation intensity of this laser at ``fraction`` of its range.

        Relative: 1 is a laser of efficiency 1 at the top of its range. This
        is the seam for a power calibration: a laser whose power is known in
        physical units would return it here, and ``saturationPower`` would
        be given in the same units.
        """
        return self.efficiency * fraction


def parse_lasers(value, key) -> Tuple[Laser, ...]:
    """``["488"]`` or ``{"488": 1.0}`` or ``{"488": {"efficiency": 1, "fullScaleV": 5}}``."""
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if isinstance(value, Mapping):
        lasers = []
        for name, spec in value.items():
            if isinstance(spec, Mapping):
                full = spec.get('fullScaleV')
                lasers.append(Laser(str(name), float(spec.get('efficiency', 1.0)),
                                    None if full is None else float(full)))
            else:
                lasers.append(Laser(str(name), float(1.0 if spec is None else spec)))
        return tuple(lasers)
    if isinstance(value, (list, tuple)):
        return tuple(Laser(str(name)) for name in value)
    raise ValueError(f'mockSample "{key}" is a list of laser names or {{"<laser>": <efficiency>}}')


@dataclass(frozen=True)
class LaserState:
    """A laser's set state, as far as it is known (None: not known)."""

    enabled: Optional[bool] = None
    #: Set power as a fraction of the laser's range, 0 to 1.
    fraction: Optional[float] = None
    #: Its configured range, (low, high), in its own units.
    range: Optional[Tuple[float, float]] = None


class LaserStates:
    """Read-only view of the lasers' set state: on/off and power.

    The laser controller publishes every laser's state as shared attributes
    (``("Laser", <name>, "Enabled")`` and ``("Laser", <name>, "Value")``)
    whenever the Laser widget or a script changes it; this reads them, and
    the laser's configured range from its manager (``valueRangeMin`` /
    ``valueRangeMax``, or 0-100 % for one with a calibration lookup), as
    any laser manager declares them. Nothing is written and no laser driver
    is touched. Reading a key of a dict another thread writes is atomic, so
    the detector worker threads read it as they go. A laser with no
    published state (no laser controller, or a power on a binary laser) is
    unknown: on, at full power, where that matters.
    """

    CATEGORY = 'Laser'

    def __init__(self, attributes=None, lasers=None):
        self._attributes = attributes
        self._lasers = lasers

    def _attribute(self, name, key):
        if self._attributes is None:
            return None
        try:
            return self._attributes[(self.CATEGORY, name, key)]
        except (KeyError, TypeError, ValueError):
            return None

    def _range(self, name):
        try:
            manager = self._lasers[name]
        except (KeyError, TypeError, IndexError):
            return None
        if getattr(manager, 'isBinary', False):
            return None
        lookup = getattr(manager, 'usesCalibrationLookup', None)
        try:
            if callable(lookup) and lookup():
                return 0.0, 100.0
            return float(manager.valueRangeMin), float(manager.valueRangeMax)
        except (AttributeError, TypeError, ValueError):
            return None

    def get(self, name) -> LaserState:
        enabled = self._attribute(name, 'Enabled')
        value = self._attribute(name, 'Value')
        span = self._range(name)
        fraction = None
        if value is not None and span is not None and span[1] > span[0]:
            try:
                fraction = min(max((float(value) - span[0]) / (span[1] - span[0]), 0.0), 1.0)
            except (TypeError, ValueError):
                fraction = None
        return LaserState(None if enabled is None else bool(enabled), fraction, span)


def _resample(signal, n):
    """A scan signal on ``n`` samples (TTL and analog share the scan clock)."""
    signal = np.asarray(signal).ravel()
    if signal.size == n or signal.size == 0:
        return signal
    return signal[np.minimum((np.arange(n) * signal.size) // n, signal.size - 1)]


def laser_light(signalDict, lasers, n, unswitched_on=True,
                states: Optional[LaserStates] = None) -> Optional[np.ndarray]:
    """Excitation intensity of ``lasers`` at each of ``n`` scan samples.

    The sum over the lasers of :meth:`Laser.intensity` while each is on. On
    is its TTL in the scan; without one, its set state (``states``); not
    known either, on (left on by hand) or, with ``unswitched_on=False``, off
    (a pulse the scan did not fire). Its power is the scan's analog power
    waveform where there is one and its scale is known (``fullScaleV``, or
    the range the laser was set with), else its set power, else full. None
    when no lasers are named (the caller's "always lit").
    """
    if not lasers:
        return None
    ttls = (signalDict or {}).get('TTLCycleSignalsDict') or {}
    analog = (signalDict or {}).get('scanSignalsDict') or {}
    states = states or LaserStates()
    n = int(n)
    light = np.zeros(n, dtype=np.float32)
    for laser in lasers:
        state = states.get(laser.name)
        ttl = ttls.get(laser.name)
        if ttl is None:
            if not (state.enabled if state.enabled is not None else unswitched_on):
                continue
            part = np.ones(n, dtype=np.float32)
        else:
            part = _resample(ttl, n).astype(np.float32)
        power = analog.get(laser.name)
        low, high = ((0.0, laser.full_scale_v) if laser.full_scale_v
                     else (state.range or (0.0, 0.0)))
        if power is not None and high > low:
            fraction = (_resample(np.asarray(power, dtype=float), n) - low) / (high - low)
            part = part * np.clip(fraction, 0.0, 1.0).astype(np.float32)
        elif state.fraction is not None:
            part = part * np.float32(state.fraction)
        light += np.asarray(laser.intensity(part), dtype=np.float32)
    return light


@dataclass(frozen=True)
class Photophysics:
    """How the fluorophores and the background answer the excitation.

    In force when ``excitation`` names lasers; without it the sample is lit
    at a fixed level as before. ``saturationPower`` (I_sat; default 0.2 for
    a point scan, 0.3 for the camera, whose readout light is spread) is
    the intensity, in :meth:`Laser.intensity` units (1: a laser of
    efficiency 1 at the top of its range), at which a fluorophore emits half
    its most; null turns saturation off. ``scatterBackground`` (default
    0.12) is the background the light adds at intensity 1, relative to the
    brightest structure, on top of the sample's autofluorescence
    (:data:`BACKGROUND`); background does not saturate. Emission is scaled to
    1 at intensity 1, so a laser at the top of its range shows the sample at
    its nominal brightness. ``saturationPower: null, scatterBackground: 0``
    is the linear response of the plain ``excitation`` gating.
    """

    saturation: Optional[float] = 0.2
    scatter: float = 0.12

    @classmethod
    def from_property(cls, value, saturation=None) -> Optional['Photophysics']:
        if not isinstance(value, Mapping) or not value.get('excitation'):
            return None
        if saturation is None:
            saturation = cls.saturation
        saturation = value.get('saturationPower', saturation)
        physics = cls(saturation=None if saturation is None else float(saturation),
                      scatter=float(value.get('scatterBackground', cls.scatter)))
        if (physics.saturation is not None and physics.saturation <= 0) or physics.scatter < 0:
            raise ValueError('mockSample "saturationPower" must be positive and '
                             '"scatterBackground" not negative')
        return physics

    def emission(self, intensity):
        """Emission per fluorophore at the focus centre; 1 at intensity 1."""
        intensity = np.maximum(np.asarray(intensity, dtype=float), 0.0)
        if self.saturation is None:
            return intensity
        x = intensity / self.saturation
        full = 1.0 / self.saturation
        return (x / (1.0 + x)) * ((1.0 + full) / full)

    def widening(self, intensity):
        """Factor on the excitation PSF sigma that saturation causes.

        A saturated Gaussian focus I g(r) excites s(I g), s(x) = x / (1 + x):
        flat-topped and wider. The Gaussian of the same peak and the same
        volume has its sigma^2 times (1 + x) ln(1 + x) / x, x = I / I_sat --
        exact in volume (what a uniform region collects), approximate in
        shape.
        """
        intensity = np.maximum(np.asarray(intensity, dtype=float), 0.0)
        if self.saturation is None:
            return np.ones_like(intensity)
        x = intensity / self.saturation
        safe = np.where(x > 1e-6, x, 1.0)
        factor = np.where(x > 1e-6, (1.0 + safe) * np.log1p(safe) / safe, 1.0)
        return np.sqrt(factor)

    def background(self, intensity):
        """Background at this intensity: autofluorescence and scatter."""
        return (BACKGROUND + self.scatter) * np.asarray(intensity, dtype=float)


def axis_um(signalDict, axis, n=None) -> Optional[np.ndarray]:
    """A scanner waveform in µm; None when the scan does not drive it."""
    if axis is None:
        return None
    device, umPerVolt = axis
    signal = ((signalDict or {}).get('scanSignalsDict') or {}).get(device)
    if signal is None:
        return None
    signal = np.asarray(signal, dtype=float).ravel()
    if signal.size == 0:
        return None
    if n is not None:
        signal = _resample(signal, n)
    return signal * umPerVolt


def defocus(z_um, sigma_um, rayleigh_um):
    """PSF sigma and excitation peak (relative to in focus) at defocus ``z``.

    A Gaussian beam: sigma grows as sqrt(1 + (z / zR)^2) and, the power
    being the same, the peak falls as the square of that.
    """
    widening = 1.0 + (np.asarray(z_um, dtype=float) / rayleigh_um) ** 2
    return sigma_um * np.sqrt(widening), 1.0 / widening


class Bleaching:
    """Photobleaching of the sample, accumulated where it was exposed.

    Dose is seconds of emission at the nominal brightness (a laser of
    efficiency 1 at full range, or as much light emitted), on a 100 nm grid
    over the field; the fraction of fluorophores left is 2^(-dose / half
    life). More power bleaches faster, as the fluorophores emit more, up to
    their saturation. It is applied to the blurred sample at the exposed
    position -- a simplification: the dose is not itself spread by the PSF,
    so this grid is the resolution of the bleaching pattern.
    """

    CELL_UM = 0.1

    def __init__(self, half_life_s: float):
        self.half_life_s = float(half_life_s)
        n = int(round(2 * FIELD_HALF_UM / self.CELL_UM)) + 1
        self._dose = np.zeros((n, n), dtype=np.float64)
        self._lock = threading.Lock()

    def _cells(self, x_um, y_um):
        """The grid cell of each position (nearest), and which are inside."""
        n = self._dose.shape[0]
        c = np.rint((np.asarray(x_um, dtype=float).ravel() + FIELD_HALF_UM) / self.CELL_UM)
        r = np.rint((np.asarray(y_um, dtype=float).ravel() + FIELD_HALF_UM) / self.CELL_UM)
        inside = (c >= 0) & (c < n) & (r >= 0) & (r < n)
        return np.where(inside, r * n + c, 0).astype(np.intp), inside

    def survival(self, x_um, y_um) -> np.ndarray:
        """Fraction of fluorophores left at these positions."""
        x, y = np.broadcast_arrays(np.asarray(x_um, float), np.asarray(y_um, float))
        index, inside = self._cells(x, y)
        with self._lock:
            dose = self._dose.ravel()[index]
        left = np.exp2(-np.where(inside, dose, 0.0) / self.half_life_s)
        return left.reshape(x.shape)

    def expose(self, x_um, y_um, seconds) -> None:
        """Add ``seconds`` at full excitation at these positions."""
        x, y = np.broadcast_arrays(np.asarray(x_um, float), np.asarray(y_um, float))
        index, inside = self._cells(x, y)
        seconds = np.broadcast_to(np.asarray(seconds, dtype=float), x.shape).ravel()
        added = np.bincount(index, np.where(inside, seconds, 0.0), minlength=self._dose.size)
        with self._lock:
            self._dose += added.reshape(self._dose.shape)


def parse_bleach_half_life(value) -> Optional[float]:
    """The block's ``bleachHalfLifeS``: None (no bleaching) or a positive time."""
    halfLife = value.get('bleachHalfLifeS')
    if halfLife is None:
        return None
    if float(halfLife) <= 0:
        raise ValueError('mockSample "bleachHalfLifeS" must be positive')
    return float(halfLife)


@dataclass(frozen=True)
class PointScanOptics:
    """The point-scan side of the ``mockSample`` block.

    ``psfFwhmUm`` (default 0.235), ``rayleighRangeUm`` (0.4), ``excitation``
    (lasers that excite the sample; all light when absent) with its
    :class:`Photophysics` (``saturationPower``, ``scatterBackground``),
    ``bleachHalfLifeS`` (none: no bleaching) and ``darkCountRate`` (counts
    per second, APD only; default 0).
    """

    psf_sigma_um: float = PSF_SIGMA_UM
    rayleigh_um: float = RAYLEIGH_UM
    excitation: Tuple[Laser, ...] = ()
    bleach_half_life_s: Optional[float] = None
    dark_count_rate: float = 0.0
    physics: Optional[Photophysics] = None

    @classmethod
    def from_property(cls, value) -> 'PointScanOptics':
        if not isinstance(value, Mapping):
            return cls()
        fwhm = value.get('psfFwhmUm')
        optics = cls(
            psf_sigma_um=PSF_SIGMA_UM if fwhm is None else float(fwhm) / FWHM_PER_SIGMA,
            rayleigh_um=float(value.get('rayleighRangeUm', RAYLEIGH_UM)),
            excitation=parse_lasers(value.get('excitation'), 'excitation'),
            bleach_half_life_s=parse_bleach_half_life(value),
            dark_count_rate=float(value.get('darkCountRate', 0.0)),
            physics=Photophysics.from_property(value),
        )
        if optics.psf_sigma_um <= 0 or optics.rayleigh_um <= 0 or optics.dark_count_rate < 0:
            raise ValueError('mockSample "psfFwhmUm" and "rayleighRangeUm" must be positive '
                             'and "darkCountRate" not negative')
        return optics


class PointScanSignal:
    """Relative brightness under the beam at every detector sample of one scan.

    Built from the scan's signals (and the lasers' set state, ``states``)
    when the scan is built; called with the detector samples a scan worker
    reads, in order. 1 is about the brightest structure in focus at nominal
    excitation; the background is :data:`BACKGROUND` (plus the scatter of
    :class:`Photophysics`, in proportion to the light).
    ``samples_per_scan_sample`` detector samples share one scan (waveform)
    sample.
    """

    def __init__(self, sample: MockSample, optics: PointScanOptics, signalDict,
                 samples_per_scan_sample: int, detector_rate_hz: float,
                 bleaching: Optional[Bleaching] = None,
                 states: Optional[LaserStates] = None):
        self.sample = sample
        self.optics = optics
        self._per = max(1, int(samples_per_scan_sample))
        self._dt = 1.0 / float(detector_rate_hz) if detector_rate_hz else 0.0
        self._bleaching = bleaching
        self._xy = [axis_um(signalDict, axis) for axis in sample.axes]
        if len(self._xy) == 1:
            self._xy.append(None)
        lengths = [p.size for p in self._xy if p is not None]
        self._n = max(lengths) if lengths else 0
        z = axis_um(signalDict, sample.z_axis)
        self._z = None if z is None else z - sample.focus_um
        if self._z is not None:
            self._n = max(self._n, self._z.size)
        self._light = (laser_light(signalDict, optics.excitation, self._n, states=states)
                       if self._n else None)

    @property
    def usable(self) -> bool:
        """Whether the scan moves the beam over the sample at all."""
        return any(p is not None for p in self._xy)

    @property
    def unlit(self) -> bool:
        """Whether no excitation light reaches the sample in this scan."""
        return self._light is not None and not np.any(self._light > 0)

    def __call__(self, start: int, count: int) -> np.ndarray:
        count = int(count)
        index = np.arange(int(start), int(start) + count) // self._per
        coords = []
        for positions in self._xy:
            if positions is None:
                coords.append(np.zeros(count))
            else:
                coords.append(positions[np.minimum(index, positions.size - 1)])
        x, y = coords
        light = None
        if self._light is not None:
            light = self._light[np.minimum(index, self._light.size - 1)].astype(float)
        physics = self.optics.physics if light is not None else None

        sigma = np.full(count, self.optics.psf_sigma_um)
        peak = 1.0
        if self._z is not None:
            z = self._z[np.minimum(index, self._z.size - 1)]
            sigma, excitation = defocus(z, self.optics.psf_sigma_um, self.optics.rayleigh_um)
            # The pinhole passes as much less of the defocused light as the
            # excitation peak fell: the confocal peak falls as its square.
            peak = excitation * excitation
        if physics is not None:
            # A saturated focus is wider (lit samples only; unlit read 0).
            sigma = sigma * physics.widening(light)
        signal = np.empty(count)
        if count == 0 or sigma.min() == sigma.max():
            signal[:] = self.sample.view(float(sigma[0]) if count else 1.0).sample(x, y)
        else:
            # One view per distinct PSF (one per plane and power, in practice).
            sigmas, which = np.unique(np.round(sigma, 4), return_inverse=True)
            for k, s in enumerate(sigmas):
                hit = which == k
                signal[hit] = self.sample.view(float(s)).sample(x[hit], y[hit])
        if self._bleaching is not None:
            signal *= self._bleaching.survival(x, y)

        if physics is not None:
            emitted = peak * physics.emission(light)
            signal = emitted * signal + physics.background(light)
        else:
            emitted = peak * (1.0 if light is None else light)
            signal = BACKGROUND + peak * signal
            if light is not None:
                signal *= light
        if self._bleaching is not None:
            self._bleaching.expose(x, y, np.broadcast_to(self._dt * emitted, x.shape))
        return signal


def unlit_message(lasers) -> str:
    """What the mock tells a user whose scan lights nothing."""
    names = ', '.join(f'"{laser.name}"' for laser in lasers)
    return (f'Mock sample: no excitation light reaches it in this scan -- {names} '
            f'off or at zero power. Lasers start off and at their lowest power, as '
            f'on the instrument: raise one in the Laser widget (or the scan\'s '
            f'channel power) to see the sample.')


__all__ = ['Bleaching', 'Laser', 'LaserState', 'LaserStates', 'Photophysics',
           'PointScanOptics', 'PointScanSignal', 'RAYLEIGH_UM', 'axis_um', 'defocus',
           'laser_light', 'parse_bleach_half_life', 'parse_lasers', 'unlit_message']


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
