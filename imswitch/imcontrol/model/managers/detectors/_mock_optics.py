"""What the simulated detectors see of the virtual sample during a scan.

:mod:`._mock_sample` is the specimen; this module is the experiment around
it, read from the scan's own signals (``signalDict``) where they carry it:

* where the specimen is -- the x/y scanner waveforms (``scanSignalsDict``),
  and, with a ``zAxis``, how far out of focus (defocus widens the PSF and
  lowers its peak, and a confocal pinhole rejects out-of-focus light);
* whether it is lit -- the TTL of each exciting laser
  (``TTLCycleSignalsDict``), times the laser's analog power when the scan
  carries a power waveform for it (Advanced line-step power) and the setup
  says what voltage is full power. The power a laser was set to outside the
  scan is not in the scan signals, so it is not modelled: a laser without a
  TTL in the scan counts as on at full power;
* what the light did to it -- photobleaching, accumulated over the scan in
  the order the scan exposes the sample.

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
    #: ignores a power waveform (TTL gating only).
    full_scale_v: Optional[float] = None


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


def _resample(signal, n):
    """A scan signal on ``n`` samples (TTL and analog share the scan clock)."""
    signal = np.asarray(signal).ravel()
    if signal.size == n or signal.size == 0:
        return signal
    return signal[np.minimum((np.arange(n) * signal.size) // n, signal.size - 1)]


def laser_light(signalDict, lasers, n, unswitched_on=True) -> Optional[np.ndarray]:
    """Relative light of ``lasers`` on the sample at each of ``n`` scan samples.

    The sum over the lasers of efficiency x TTL x analog power fraction. A
    laser the scan does not switch counts as on (left on by hand), or with
    ``unswitched_on=False`` as off (a pulse the scan did not fire). None
    when no lasers are named (the caller's "always lit").
    """
    if not lasers:
        return None
    ttls = (signalDict or {}).get('TTLCycleSignalsDict') or {}
    analog = (signalDict or {}).get('scanSignalsDict') or {}
    light = np.zeros(int(n), dtype=np.float32)
    for laser in lasers:
        on = ttls.get(laser.name)
        if on is None and not unswitched_on:
            continue
        part = (np.ones(int(n), dtype=np.float32) if on is None
                else _resample(on, n).astype(np.float32))
        power = analog.get(laser.name)
        if power is not None and laser.full_scale_v:
            fraction = _resample(np.asarray(power, dtype=float), n) / laser.full_scale_v
            part = part * np.clip(fraction, 0.0, None).astype(np.float32)
        light += np.float32(laser.efficiency) * part
    return light


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

    Dose is seconds at full excitation, on a 100 nm grid over the field; the
    fraction of fluorophores left is 2^(-dose / half life). It is applied to
    the blurred sample at the exposed position -- a simplification: the dose
    is not itself spread by the PSF, so this grid is the resolution of the
    bleaching pattern.
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
    (lasers that excite the sample; all light when absent),
    ``bleachHalfLifeS`` (none: no bleaching) and ``darkCountRate`` (counts
    per second, APD only; default 0).
    """

    psf_sigma_um: float = PSF_SIGMA_UM
    rayleigh_um: float = RAYLEIGH_UM
    excitation: Tuple[Laser, ...] = ()
    bleach_half_life_s: Optional[float] = None
    dark_count_rate: float = 0.0

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
        )
        if optics.psf_sigma_um <= 0 or optics.rayleigh_um <= 0 or optics.dark_count_rate < 0:
            raise ValueError('mockSample "psfFwhmUm" and "rayleighRangeUm" must be positive '
                             'and "darkCountRate" not negative')
        return optics


class PointScanSignal:
    """Relative brightness under the beam at every detector sample of one scan.

    Built from the scan's signals when the scan is built; called with the
    detector samples a scan worker reads, in order. 1 is about the brightest
    structure in focus under full excitation; the background is
    :data:`BACKGROUND`. ``samples_per_scan_sample`` detector samples share
    one scan (waveform) sample.
    """

    def __init__(self, sample: MockSample, optics: PointScanOptics, signalDict,
                 samples_per_scan_sample: int, detector_rate_hz: float,
                 bleaching: Optional[Bleaching] = None):
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
        self._light = laser_light(signalDict, optics.excitation, self._n) if self._n else None

    @property
    def usable(self) -> bool:
        """Whether the scan moves the beam over the sample at all."""
        return any(p is not None for p in self._xy)

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
        signal = np.empty(count)
        if self._z is None:
            signal[:] = self.sample.view(self.optics.psf_sigma_um).sample(x, y)
            peak = 1.0
        else:
            z = self._z[np.minimum(index, self._z.size - 1)]
            sigma, excitation = defocus(z, self.optics.psf_sigma_um, self.optics.rayleigh_um)
            # The pinhole passes as much less of the defocused light as the
            # excitation peak fell: the confocal peak falls as its square.
            peak = excitation * excitation
            # One view per distinct defocus (a stepped z: one per plane).
            sigmas, which = np.unique(np.round(sigma, 4), return_inverse=True)
            for k, s in enumerate(sigmas):
                hit = which == k
                signal[hit] = self.sample.view(float(s)).sample(x[hit], y[hit])
        if self._bleaching is not None:
            signal *= self._bleaching.survival(x, y)
        signal = BACKGROUND + peak * signal
        light = None
        if self._light is not None:
            light = self._light[np.minimum(index, self._light.size - 1)]
            signal *= light
        if self._bleaching is not None:
            dose = self._dt * peak * (1.0 if light is None else light)
            self._bleaching.expose(x, y, np.broadcast_to(dose, x.shape))
        return signal


__all__ = ['Bleaching', 'Laser', 'PointScanOptics', 'PointScanSignal', 'RAYLEIGH_UM',
           'parse_bleach_half_life',
           'axis_um', 'defocus', 'laser_light', 'parse_lasers']


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
