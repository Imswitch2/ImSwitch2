"""Camera frames of the synthetic sample under a MoNaLISA foci lattice.

A mock camera in a MoNaLISA setup otherwise shows noise that does not depend
on where the scan put the sample: the lattice cannot be found in it and a
reconstruction shows nothing. With a ``mockSample`` block in the camera's
``managerProperties`` each frame instead shows a square lattice of foci,
fixed on the camera (the microlens array is in the illumination path), each
as bright as the :class:`MockSample` at the sample point under it. The scan
moves the sample (stage scanning): the sample point under a focus is the
focus position minus the stage position at that frame's camera trigger, read
from the scan's own analog waveforms. A scan over one lattice period
therefore samples the whole field, and a reconstruction shows the cells and
beads of the point-scan mock.

Simulation only. Only the sample's x and y are scanned; Z, and the switching
(ON/OFF) steps, do not change the frames. The camera centre is the sample
origin.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Mapping, Optional

import numpy as np

from ._mock_sample import MockSample

#: Square stamp half-width per focus, in spot sigmas.
_STAMP_SIGMAS = 3.0


@dataclass(frozen=True)
class MockLattice:
    """The sample, the foci lattice and the counts it is drawn with."""

    sample: MockSample
    period_um: float = 0.72
    angle_deg: float = 0.0
    spot_sigma_um: float = 0.12
    peak_counts: float = 2000.0
    offset_counts: float = 100.0

    @classmethod
    def from_property(cls, value) -> Optional['MockLattice']:
        """The camera's ``mockSample`` manager property; None when absent.

        Takes the point-scan keys (``axes``, ``seed``) plus the optional
        ``latticePeriodUm``, ``latticeAngleDeg``, ``spotSigmaUm``,
        ``peakCounts`` and ``offsetCounts``.
        """
        sample = MockSample.from_property(value)
        if sample is None:
            return None
        assert isinstance(value, Mapping)
        lattice = cls(
            sample=sample,
            period_um=float(value.get('latticePeriodUm', cls.period_um)),
            angle_deg=float(value.get('latticeAngleDeg', cls.angle_deg)),
            spot_sigma_um=float(value.get('spotSigmaUm', cls.spot_sigma_um)),
            peak_counts=float(value.get('peakCounts', cls.peak_counts)),
            offset_counts=float(value.get('offsetCounts', cls.offset_counts)),
        )
        if lattice.period_um <= 0 or lattice.spot_sigma_um <= 0:
            raise ValueError('mockSample "latticePeriodUm" and "spotSigmaUm" must be positive')
        return lattice

    def foci_um(self, shape, pixel_um):
        """Foci centres on a ``(rows, cols)`` frame, µm from its centre, as (x, y)."""
        rows, cols = (int(v) for v in shape)
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
        return x[inside], y[inside]

    def expected_counts(self, shape, pixel_um, stage_um=(0.0, 0.0)) -> np.ndarray:
        """Noise-free frame (float counts) with the stage at ``stage_um`` (x, y)."""
        rows, cols = (int(v) for v in shape)
        pixel_um = float(pixel_um)
        frame = np.full((rows, cols), self.offset_counts, dtype=np.float64)
        fx, fy = self.foci_um(shape, pixel_um)
        if fx.size == 0:
            return frame
        brightness = self.sample.brightness(fx - stage_um[0], fy - stage_um[1])

        # Focus centres in pixel coordinates (column, row).
        cx = fx / pixel_um + 0.5 * (cols - 1)
        cy = fy / pixel_um + 0.5 * (rows - 1)
        sigma_px = self.spot_sigma_um / pixel_um
        r = max(1, int(np.ceil(_STAMP_SIGMAS * sigma_px)))
        d = np.arange(-r, r + 1)
        col0 = np.rint(cx).astype(np.int64)
        row0 = np.rint(cy).astype(np.int64)
        gx = np.exp(-0.5 * ((col0[:, None] + d[None, :] - cx[:, None]) / sigma_px) ** 2)
        gy = np.exp(-0.5 * ((row0[:, None] + d[None, :] - cy[:, None]) / sigma_px) ** 2)
        weights = (self.peak_counts * brightness)[:, None, None] * gy[:, :, None] * gx[:, None, :]
        rr = np.broadcast_to(row0[:, None, None] + d[None, :, None], weights.shape)
        cc = np.broadcast_to(col0[:, None, None] + d[None, None, :], weights.shape)
        keep = (rr >= 0) & (rr < rows) & (cc >= 0) & (cc < cols)
        np.add.at(frame, (rr[keep], cc[keep]), weights[keep])
        return frame


class MockLatticeFrames:
    """Frame source a mock camera draws from: one frame per camera trigger.

    :meth:`setScan` takes the stage position of every camera trigger of the
    next scan; :meth:`nextFrames` renders the next ones in order, and
    :meth:`liveFrame` renders a frame at the last position (the stage at rest).
    """

    def __init__(self, lattice: MockLattice, pixel_um: float, seed=None):
        self.lattice = lattice
        self.pixelUm = float(pixel_um)
        self._positions = np.zeros((0, 2))
        self._cursor = 0
        self._restUm = (0.0, 0.0)
        self._rng = np.random.default_rng(seed)
        self._lock = threading.Lock()

    def setScan(self, positions_um, pixel_um=None) -> None:
        positions = np.asarray(positions_um, dtype=float).reshape(-1, 2)
        with self._lock:
            if pixel_um is not None:
                self.pixelUm = float(pixel_um)
            self._positions = positions
            self._cursor = 0

    def _render(self, shape, stage_um) -> np.ndarray:
        counts = self.lattice.expected_counts(shape, self.pixelUm, stage_um)
        return np.clip(self._rng.poisson(counts), 0, 65535).astype(np.uint16)

    def nextFrames(self, n, shape):
        frames = []
        for _ in range(int(n)):
            with self._lock:
                if self._cursor < len(self._positions):
                    self._restUm = tuple(self._positions[self._cursor])
                    self._cursor += 1
                stage = self._restUm
            frames.append(self._render(shape, stage))
        return frames

    def liveFrame(self, shape) -> np.ndarray:
        with self._lock:
            stage = self._restUm
        return self._render(shape, stage)


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
    ttl = np.asarray(ttl).astype(np.int8, copy=False).ravel()
    edges = np.flatnonzero(np.diff(ttl, prepend=0) == 1)
    if edges.size == 0:
        return None
    signals = (signalDict or {}).get('scanSignalsDict') or {}
    positions = np.zeros((edges.size, 2))
    for column, (device, umPerVolt) in enumerate(tuple(axes)[:2]):
        signal = signals.get(device)
        if signal is None:
            continue
        signal = np.asarray(signal, dtype=float).ravel()
        if signal.size == 0:
            continue
        # TTL and analog outputs share the scan clock; scale if they do not.
        index = edges if signal.size == ttl.size else (edges * signal.size) // ttl.size
        positions[:, column] = signal[np.clip(index, 0, signal.size - 1)] * umPerVolt
    return positions


__all__ = ['MockLattice', 'MockLatticeFrames', 'trigger_positions_um']


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
