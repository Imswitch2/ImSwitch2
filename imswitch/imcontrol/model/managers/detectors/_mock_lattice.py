"""Camera frames of the synthetic sample under a MoNaLISA foci lattice.

A mock camera in a MoNaLISA setup otherwise shows noise that does not depend
on where the scan put the sample: the lattice cannot be found in it and a
reconstruction shows nothing. With a ``mockSample`` block in the camera's
``managerProperties`` each frame instead shows a square lattice of foci,
fixed on the camera (the microlens array is in the illumination path), each
as bright as the :class:`MockSample` under it. The scan moves the sample
(stage scanning): the sample point under a focus is the focus position minus
the stage position at that frame's camera trigger, read from the scan's own
analog waveforms. A scan over one lattice period therefore samples the whole
field, and the real MoNaLISA reconstruction recovers the sample from it.

The optics, per frame, from what the scan's signals say happened before and
during that frame (:func:`trigger_conditions`):

* Each focus excites the sample through a Gaussian excitation PSF
  (``excitationFwhmUm``, default 200 nm). Its light is collected from the
  emitters it excites and imaged through the detection PSF: the spot is
  centred on the excited emitters, not on the focus, so it moves with the
  specimen as an image-scanning microscope's spots do. ``spotSigmaUm`` is
  the spot of the confocal readout in focus, as the frames show it; the
  detection PSF is what that leaves after the excitation PSF.
* With ``switching`` the emitters are reversibly switchable (RESOLFT):
  the ON pulses switch them on, the OFF pulses -- a standing-wave pattern
  whose zeros are the foci -- switch them off everywhere but at the foci,
  and the readout switches some of them off as it reads them. Around a
  zero the ON probability is Gaussian, so the effective PSF is the
  excitation PSF narrowed by sqrt(1 + zeta pi^2 sigma^2 / period^2), zeta
  the OFF dose: the RESOLFT mode of a scan reconstructs sharper than its
  confocal mode, from the TTLs alone.
* ``excitation`` names the readout lasers: a frame is as bright as their
  light over its exposure -- TTL, power and, outside a scan, their set
  on/off state and power (``_mock_optics.py``). The fluorophores saturate
  (``saturationPower``): emission levels off, the focus widens, while a
  scatter background (``scatterBackground``) keeps growing with the light,
  and the readout switches emitters off as fast as they emit. Each switching
  laser's power acts on its own step: the ON dose sets how many are on, the
  OFF dose the resolution. A ``zAxis`` defocuses.
* Photon (Poisson) noise, then a constant ``offsetCounts`` and Gaussian
  ``readNoiseCounts``, as an sCMOS camera reads; ``bleachHalfLifeS``
  bleaches the sample where the foci expose it, in scan order.

Approximations, to stay at tens of ms per 512x512 frame: every focus's spot
is one Gaussian (its centre and width taken from the first moments of the
excited emitters, not their full image); a focus sees only its own zero of
the OFF pattern; the switching state at the focus centre is carried from
frame to frame as one number, not per emitter; and the live view (no scan)
shows the sample in its confocal readout. The camera centre is the sample
origin.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field, replace
from typing import Mapping, Optional, Tuple

import numpy as np

from ._mock_optics import (
    RAYLEIGH_UM, Bleaching, Laser, LaserStates, Photophysics, axis_um, defocus,
    laser_light, parse_bleach_half_life, parse_lasers,
)
from ._mock_sample import BACKGROUND, FWHM_PER_SIGMA, MockSample

#: Square stamp half-width per focus, in spot sigmas.
_STAMP_SIGMAS = 3.0
#: Scan clock when the scan does not say (``scan_time_step``), Hz.
_DEFAULT_SCAN_RATE_HZ = 100_000.0
#: Default saturationPower of the camera's readout: the light of one laser is
#: spread over thousands of foci, so they saturate later than a point scan's.
CAMERA_SATURATION = 0.3


@dataclass(frozen=True)
class Switching:
    """Reversible ON/OFF switching (``mockSample`` ``"switching"``).

    Rates are per ms of light at the focus centre: ``onPerMs`` switches on,
    ``offPerMs`` is the OFF pattern's dose rate at its maxima, and
    ``readoutPerMs`` switches off while reading out.
    """

    on: Tuple[Laser, ...]
    off: Tuple[Laser, ...]
    on_per_ms: float = 5.0
    off_per_ms: float = 20.0
    readout_per_ms: float = 0.3

    @classmethod
    def from_property(cls, value) -> Optional['Switching']:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise ValueError('mockSample "switching" is {"on": [<lasers>], "off": [<lasers>]}')
        switching = cls(
            on=parse_lasers(value.get('on'), 'switching.on'),
            off=parse_lasers(value.get('off'), 'switching.off'),
            on_per_ms=float(value.get('onPerMs', cls.on_per_ms)),
            off_per_ms=float(value.get('offPerMs', cls.off_per_ms)),
            readout_per_ms=float(value.get('readoutPerMs', cls.readout_per_ms)),
        )
        if min(switching.on_per_ms, switching.off_per_ms, switching.readout_per_ms) < 0:
            raise ValueError('mockSample "switching" rates must not be negative')
        return switching


@dataclass(frozen=True)
class FrameConditions:
    """What a frame saw: where the sample was and what the light had done."""

    stage_um: Tuple[float, float] = (0.0, 0.0)
    #: Defocus, µm (0 in focus).
    z_um: float = 0.0
    #: Readout intensity over the exposure (1: efficiency 1 at full range).
    readout: float = 1.0
    #: Fraction of the emitters at a focus centre that are ON (and the
    #: readout's own off-switching during the exposure taken out).
    on_fraction: float = 1.0
    #: OFF dose at the OFF pattern's maxima (0: no OFF pulse).
    zeta: float = 0.0
    #: Seconds of emission at nominal brightness, for photobleaching.
    readout_s: float = 0.0


@dataclass(frozen=True)
class MockLattice:
    """The sample, the foci lattice and the camera that images it."""

    sample: MockSample
    period_um: float = 0.72
    angle_deg: float = 0.0
    #: Spot sigma on the camera for the confocal readout in focus, µm.
    spot_sigma_um: float = 0.12
    peak_counts: float = 2000.0
    offset_counts: float = 100.0
    read_noise_counts: float = 1.5
    #: Excitation PSF sigma of a focus, µm (FWHM 200 nm: a 488 nm focus near
    #: the diffraction limit).
    excitation_sigma_um: float = 0.2 / FWHM_PER_SIGMA
    rayleigh_um: float = RAYLEIGH_UM
    excitation: Tuple[Laser, ...] = ()
    switching: Optional[Switching] = None
    bleach_half_life_s: Optional[float] = None
    physics: Optional[Photophysics] = None
    _foci_cache: dict = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_property(cls, value) -> Optional['MockLattice']:
        """The camera's ``mockSample`` manager property; None when absent.

        Takes the point-scan keys (``axes``, ``seed``, ``preset``, ``zAxis``,
        ``focusUm``) plus the optional ``latticePeriodUm``,
        ``latticeAngleDeg``, ``spotSigmaUm``, ``excitationFwhmUm``,
        ``rayleighRangeUm``, ``peakCounts``, ``offsetCounts``,
        ``readNoiseCounts``, ``excitation`` (with ``saturationPower`` and
        ``scatterBackground``), ``switching`` and ``bleachHalfLifeS``.
        """
        sample = MockSample.from_property(value)
        if sample is None:
            return None
        assert isinstance(value, Mapping)
        fwhm = value.get('excitationFwhmUm')
        lattice = cls(
            sample=sample,
            period_um=float(value.get('latticePeriodUm', cls.period_um)),
            angle_deg=float(value.get('latticeAngleDeg', cls.angle_deg)),
            spot_sigma_um=float(value.get('spotSigmaUm', cls.spot_sigma_um)),
            peak_counts=float(value.get('peakCounts', cls.peak_counts)),
            offset_counts=float(value.get('offsetCounts', cls.offset_counts)),
            read_noise_counts=float(value.get('readNoiseCounts', cls.read_noise_counts)),
            excitation_sigma_um=(cls.excitation_sigma_um if fwhm is None
                                 else float(fwhm) / FWHM_PER_SIGMA),
            rayleigh_um=float(value.get('rayleighRangeUm', cls.rayleigh_um)),
            excitation=parse_lasers(value.get('excitation'), 'excitation'),
            switching=Switching.from_property(value.get('switching')),
            bleach_half_life_s=parse_bleach_half_life(value),
            physics=Photophysics.from_property(value, saturation=CAMERA_SATURATION),
        )
        if lattice.period_um <= 0 or lattice.spot_sigma_um <= 0:
            raise ValueError('mockSample "latticePeriodUm" and "spotSigmaUm" must be positive')
        if lattice.excitation_sigma_um <= 0 or lattice.rayleigh_um <= 0:
            raise ValueError('mockSample "excitationFwhmUm" and "rayleighRangeUm" must be positive')
        if lattice.read_noise_counts < 0:
            raise ValueError('mockSample "readNoiseCounts" must not be negative')
        return lattice

    def foci_um(self, shape, pixel_um):
        """Foci centres on a ``(rows, cols)`` frame, µm from its centre, as (x, y)."""
        rows, cols = (int(v) for v in shape)
        key = (rows, cols, float(pixel_um))
        cached = self._foci_cache.get(key)
        if cached is not None:
            return cached
        half = 0.5 * np.hypot(rows, cols) * float(pixel_um) + self.period_um
        n = int(np.ceil(half / self.period_um))
        i, j = np.meshgrid(np.arange(-n, n + 1), np.arange(-n, n + 1), indexing='ij')
        # Half a period off the centre, so the centre pixel is not a focus.
        u = (j.ravel() + 0.5) * self.period_um
        v = (i.ravel() + 0.5) * self.period_um
        angle = np.deg2rad(self.angle_deg)
        x = u * np.cos(angle) - v * np.sin(angle)
        y = u * np.sin(angle) + v * np.cos(angle)
        inside = (np.abs(x) <= 0.5 * cols * pixel_um) & (np.abs(y) <= 0.5 * rows * pixel_um)
        foci = (x[inside], y[inside])
        self._foci_cache[key] = foci
        return foci

    # ------------------------------------------------------------------
    # One frame
    # ------------------------------------------------------------------

    @property
    def detection_sigma_um(self) -> float:
        """Detection PSF sigma (the image of one emitter), µm.

        What the confocal spot leaves after the excitation PSF; at least a
        quarter of the spot, for a spot set narrower than the excitation.
        """
        return math.sqrt(max(self.spot_sigma_um ** 2 - self.excitation_sigma_um ** 2,
                             (0.25 * self.spot_sigma_um) ** 2))

    def effective_sigma_um(self, conditions: FrameConditions = FrameConditions()):
        """Effective excitation PSF sigma, and the excitation peak, for a frame."""
        sigma, peak = defocus(conditions.z_um, self.excitation_sigma_um, self.rayleigh_um)
        sigma, peak = float(sigma), float(peak)
        if self.physics is not None:
            # A saturated focus is wider, before the OFF pattern trims it.
            sigma *= float(self.physics.widening(conditions.readout))
        if conditions.zeta > 0:
            # ON probability exp(-zeta (sin^2(pi dx/p) + sin^2(pi dy/p)) / 2),
            # Gaussian around a zero of the OFF pattern.
            sigma = 1.0 / math.sqrt(sigma ** -2 + conditions.zeta * math.pi ** 2
                                    / self.period_um ** 2)
        return sigma, peak

    def foci(self, x_um, y_um, conditions: FrameConditions = FrameConditions(),
             survival=None):
        """What the foci at sample points ``(x, y)`` collect.

        Returns the relative brightness of each spot (1: the brightest
        structure in focus, in the confocal readout), the offset of its
        centre from the focus (the centroid of the emitters it excites, µm)
        and the spot sigma on the camera, µm. ``survival``, when given, is
        the fraction of fluorophores bleaching left at each point.
        """
        width, peak = self.effective_sigma_um(conditions)
        view = self.sample.view(width)
        sigma = view.sigma_um           # the view's own, within 2 % of it
        x_um = np.asarray(x_um, dtype=float)
        y_um = np.asarray(y_um, dtype=float)
        signal = view.sample(x_um, y_um)
        h = view.texel_um
        gx = (view.sample(x_um + h, y_um) - view.sample(x_um - h, y_um)) / (2 * h)
        gy = (view.sample(x_um, y_um + h) - view.sample(x_um, y_um - h)) / (2 * h)
        switchable = peak * conditions.on_fraction
        if self.physics is not None:
            switchable = switchable * float(self.physics.emission(conditions.readout))
            baseline = float(self.physics.background(conditions.readout))
        else:
            switchable = switchable * conditions.readout
            baseline = BACKGROUND * conditions.readout
        if survival is not None:
            switchable = switchable * survival
        emitters = switchable * signal
        total = baseline + emitters
        # Centroid of a Gaussian-weighted density: sigma^2 grad(V) / V; the
        # background is uniform and has none.
        weight = np.maximum(total, 1e-12)
        dx = np.clip(sigma ** 2 * switchable * gx / weight, -2 * sigma, 2 * sigma)
        dy = np.clip(sigma ** 2 * switchable * gy / weight, -2 * sigma, 2 * sigma)
        detection = self.detection_sigma_um * math.sqrt(
            1.0 + (conditions.z_um / self.rayleigh_um) ** 2)
        spot = math.hypot(detection, width)
        return total, dx, dy, spot

    def expected_photons(self, shape, pixel_um, conditions: FrameConditions,
                         bleaching: Optional[Bleaching] = None) -> np.ndarray:
        """Mean detected photons per pixel of a frame (no camera offset)."""
        rows, cols = (int(v) for v in shape)
        pixel_um = float(pixel_um)
        stage = conditions.stage_um
        fx, fy = self.foci_um(shape, pixel_um)
        if fx.size == 0:
            return np.zeros((rows, cols), dtype=np.float64)
        sx, sy = fx - stage[0], fy - stage[1]
        survival = None if bleaching is None else bleaching.survival(sx, sy)
        brightness, dx, dy, spot = self.foci(sx, sy, conditions, survival)
        # The confocal readout in focus peaks at peakCounts for brightness 1;
        # a narrower or wider spot keeps its photons.
        reference = math.hypot(self.detection_sigma_um, self.excitation_sigma_um)
        amplitude = self.peak_counts * brightness * (reference / spot) ** 2

        # Spot centres in pixel coordinates (column, row).
        cx = (fx + dx) / pixel_um + 0.5 * (cols - 1)
        cy = (fy + dy) / pixel_um + 0.5 * (rows - 1)
        sigma_px = spot / pixel_um
        r = max(1, int(np.ceil(_STAMP_SIGMAS * sigma_px)))
        d = np.arange(-r, r + 1, dtype=np.float32)
        col0 = np.rint(cx).astype(np.int64)
        row0 = np.rint(cy).astype(np.int64)
        scale = np.float32(-0.5 / sigma_px ** 2)
        gx = np.exp(scale * (d[None, :] - (cx - col0).astype(np.float32)[:, None]) ** 2)
        gy = np.exp(scale * (d[None, :] - (cy - row0).astype(np.float32)[:, None]) ** 2)
        gy *= amplitude.astype(np.float32)[:, None]
        weights = gy[:, :, None] * gx[:, None, :]
        # Stamped on a frame padded by more than a stamp and a spot's shift,
        # so no stamp needs clipping; the padding is cut off after.
        pad = 2 * r + 2
        width = cols + 2 * pad
        steps = np.arange(-r, r + 1)
        offsets = (steps[:, None] * width + steps[None, :]).ravel()
        base = (np.clip(row0, -r - 1, rows + r) + pad) * width \
            + np.clip(col0, -r - 1, cols + r) + pad
        flat = (base[:, None] + offsets[None, :]).ravel()
        padded = np.bincount(flat, weights.ravel(), minlength=(rows + 2 * pad) * width)
        return padded.reshape(rows + 2 * pad, width)[pad:pad + rows, pad:pad + cols]

    def expected_counts(self, shape, pixel_um, stage_um=(0.0, 0.0),
                        conditions: Optional[FrameConditions] = None,
                        bleaching: Optional[Bleaching] = None) -> np.ndarray:
        """Noise-free frame (float counts) with the stage at ``stage_um`` (x, y)."""
        if conditions is None:
            conditions = FrameConditions(stage_um=tuple(stage_um))
        return self.expected_photons(shape, pixel_um, conditions, bleaching) + self.offset_counts

    def exposure(self, shape, pixel_um, conditions: FrameConditions):
        """Sample points the foci of a frame exposed, and for how long (s).

        For photobleaching: what is ON is what bleaches.
        """
        fx, fy = self.foci_um(shape, pixel_um)
        _, peak = self.effective_sigma_um(conditions)
        seconds = conditions.readout_s * peak * conditions.on_fraction
        return fx - conditions.stage_um[0], fy - conditions.stage_um[1], seconds


class MockLatticeFrames:
    """Frame source a mock camera draws from: one frame per camera trigger.

    :meth:`setScan` takes the stage position (or the full
    :class:`FrameConditions`) of every camera trigger of the next scan;
    :meth:`nextFrames` renders the next ones in order, and :meth:`liveFrame`
    renders a frame at the last position (the stage at rest, confocal
    readout by the readout lasers as they are set now: on or off, at their
    power, read from ``states`` every frame). ``seed`` makes the camera
    noise reproducible.
    """

    def __init__(self, lattice: MockLattice, pixel_um: float, seed=None,
                 states: Optional[LaserStates] = None):
        self.lattice = lattice
        self.states = states
        self.pixelUm = float(pixel_um)
        self._conditions = []
        self._cursor = 0
        self._rest = FrameConditions()
        self._rng = np.random.default_rng(seed)
        self._lock = threading.Lock()
        self.bleaching = (Bleaching(lattice.bleach_half_life_s)
                          if lattice.bleach_half_life_s else None)

    def setScan(self, positions_um, pixel_um=None, conditions=None) -> None:
        """The frames of the next scan: ``conditions`` per trigger, or just
        the stage ``positions_um`` (x, y) of each."""
        if conditions is None:
            positions = np.asarray(positions_um, dtype=float).reshape(-1, 2)
            conditions = [FrameConditions(stage_um=(float(x), float(y))) for x, y in positions]
        conditions = list(conditions)
        with self._lock:
            if pixel_um is not None:
                self.pixelUm = float(pixel_um)
            self._conditions = conditions
            self._cursor = 0
        # Blur the sample for this scan's PSFs now, not on its first frame.
        sigmas = {self.lattice.effective_sigma_um(c)[0] for c in conditions[:64]}
        if sigmas:
            threading.Thread(target=lambda: [self.lattice.sample.view(s) for s in sigmas],
                             daemon=True, name='mock-sample').start()

    def _render(self, shape, conditions, bleach=False) -> np.ndarray:
        lattice = self.lattice
        photons = lattice.expected_photons(shape, self.pixelUm, conditions, self.bleaching)
        frame = self._rng.poisson(photons).astype(np.float32)
        if lattice.read_noise_counts:
            noise = self._rng.standard_normal(frame.shape, dtype=np.float32)
            noise *= np.float32(lattice.read_noise_counts)
            frame += noise
        frame += np.float32(lattice.offset_counts)
        if bleach and self.bleaching is not None:
            self.bleaching.expose(*lattice.exposure(shape, self.pixelUm, conditions))
        np.rint(frame, out=frame)
        np.clip(frame, 0, 65535, out=frame)
        return frame.astype(np.uint16)

    def nextFrames(self, n, shape):
        frames = []
        for _ in range(int(n)):
            with self._lock:
                scanned = self._cursor < len(self._conditions)
                if scanned:
                    conditions = self._conditions[self._cursor]
                    self._cursor += 1
                    self._rest = FrameConditions(stage_um=conditions.stage_um,
                                                 z_um=conditions.z_um)
                else:
                    conditions = self._live(self._rest)
            frames.append(self._render(shape, conditions, bleach=scanned))
        return frames

    def liveFrame(self, shape) -> np.ndarray:
        with self._lock:
            conditions = self._live(self._rest)
        return self._render(shape, conditions)

    def _live(self, rest: FrameConditions) -> FrameConditions:
        """The frame at rest: lit by the readout lasers as they are set."""
        light = laser_light({}, self.lattice.excitation, 1, states=self.states)
        if light is None:
            return rest
        return replace(rest, readout=float(light[0]))


def _edges(ttl):
    ttl = np.asarray(ttl).astype(bool, copy=False).ravel()
    step = np.diff(ttl.astype(np.int8), prepend=0, append=0)
    return np.flatnonzero(step == 1), np.flatnonzero(step == -1)


def trigger_positions_um(signalDict, detectorName, axes):
    """Stage (x, y) in µm at each rising edge of the detector's scan TTL.

    ``axes`` are the sample's ``(device, µm per volt)``. An axis the scan does
    not drive reads as 0 µm. Returns None when the scan has no TTL for the
    detector (the simulator then triggers once per position; there is nothing
    to place those frames at).
    """
    ttl = ((signalDict or {}).get('TTLCycleSignalsDict') or {}).get(detectorName)
    if ttl is None:
        return None
    rises, _ = _edges(ttl)
    if rises.size == 0:
        return None
    n = np.asarray(ttl).size
    positions = np.zeros((rises.size, 2))
    for column, axis in enumerate(tuple(axes)[:2]):
        signal = axis_um(signalDict, axis, n)
        if signal is not None:
            positions[:, column] = signal[np.clip(rises, 0, signal.size - 1)]
    return positions


def trigger_conditions(signalDict, detectorName, lattice: MockLattice, scan_time_step=None,
                       states: Optional[LaserStates] = None):
    """:class:`FrameConditions` of every frame the detector's scan TTL triggers.

    Frame k is exposed while its TTL is high; what happens between the end
    of frame k-1 and the start of frame k (the ON and OFF pulses) prepares
    it. Each laser's light is its TTL times its power (``states``: what the
    lasers are set to, where the scan has no power waveform for them): the
    ON dose sets the ON fraction, the OFF dose the resolution, the readout
    the signal and how many emitters it switches off while reading -- as
    fast as they emit, so it saturates with them. None when the scan has no
    TTL for the detector.
    """
    positions = trigger_positions_um(signalDict, detectorName, lattice.sample.axes)
    if positions is None:
        return None
    ttl = np.asarray(signalDict['TTLCycleSignalsDict'][detectorName]).ravel()
    n = ttl.size
    rises, falls = _edges(ttl)
    dt_s = float(scan_time_step) if scan_time_step else 1.0 / _DEFAULT_SCAN_RATE_HZ
    dt_ms = 1e3 * dt_s

    z = axis_um(signalDict, lattice.sample.z_axis, n)
    if z is not None:
        z = z[rises] - lattice.sample.focus_um
    readout = laser_light(signalDict, lattice.excitation, n, states=states)
    switching = lattice.switching
    on = off = None
    if switching is not None:
        # A switching pulse the scan does not fire did not happen.
        on = laser_light(signalDict, switching.on, n, unswitched_on=False, states=states)
        off = laser_light(signalDict, switching.off, n, unswitched_on=False, states=states)

    conditions = []
    onFraction, zeta = 1.0, 0.0
    previousEnd = 0
    for k, (start, end) in enumerate(zip(rises, falls)):
        if readout is None:
            light, lightS = 1.0, (end - start) * dt_s
        else:
            window = readout[start:end]
            light = float(window.mean()) if window.size else 0.0
            if lattice.physics is not None:
                # Emission (and with it switching and bleaching) saturates.
                window = lattice.physics.emission(window)
            lightS = float(np.sum(window)) * dt_s
        frameOn = 1.0
        if switching is not None:
            onMs = 0.0
            offFrom = previousEnd
            if on is not None:
                pulse = on[previousEnd:start]
                onMs = float(pulse.sum()) * dt_ms
                lit = np.flatnonzero(pulse)
                if lit.size:
                    offFrom = previousEnd + int(lit[0])
            offMs = float(off[offFrom:start].sum()) * dt_ms if off is not None else 0.0
            if onMs > 0:
                onFraction = 1.0 - (1.0 - onFraction) * math.exp(-switching.on_per_ms * onMs)
                zeta = switching.off_per_ms * offMs
            else:
                zeta += switching.off_per_ms * offMs
            # Read out while the readout switches the emitters off.
            readMs = lightS * 1e3
            rate = switching.readout_per_ms * readMs
            kept = (1.0 - math.exp(-rate)) / rate if rate > 1e-9 else 1.0
            frameOn = onFraction * kept
            onFraction *= math.exp(-rate)
        conditions.append(FrameConditions(
            stage_um=(float(positions[k, 0]), float(positions[k, 1])),
            z_um=0.0 if z is None else float(z[k]),
            readout=light,
            on_fraction=frameOn,
            zeta=zeta,
            readout_s=lightS,
        ))
        previousEnd = end
    return conditions


__all__ = ['FrameConditions', 'MockLattice', 'MockLatticeFrames', 'Switching',
           'trigger_conditions', 'trigger_positions_um']


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
