"""A synthetic fluorescent sample for simulated point detectors.

With the NI-DAQ simulated, a point detector reads Poisson noise of the same
mean everywhere: an overview shows nothing to draw a region around, and an
acquisition cannot show whether it scanned the region that was drawn. With a
``mockSample`` block in the detector's ``managerProperties`` it instead counts
photons from this sample at the position the scan's own analog waveforms put
the beam at each detector sample -- so the image follows the scanners, and a
region scanned twice looks the same both times.

Cells with a bright membrane, a dimmer nucleus and diffraction-limited
vesicles, and scattered beads, blurred by a 230 nm FWHM point spread
function: coarse pixels show cells, pixels near Nyquist resolve the dots.

Simulation only; the property is ignored against real hardware. Positions
come from the scanned axes' waveforms, so an axis the scan does not sweep
(parked by a separate analog write the detector never sees) reads as 0 µm.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

import numpy as np

#: Half the side of the simulated field, µm; outside it only background.
FIELD_HALF_UM = 30.0
#: Texture pitch, µm.
TEXEL_UM = 0.04
#: PSF sigma, µm (FWHM ~230 nm).
PSF_SIGMA_UM = 0.1
#: Brightness outside any structure, relative to the brightest structure.
BACKGROUND = 0.03

_cache = {}
_cacheLock = threading.Lock()


@dataclass(frozen=True)
class MockSample:
    """The sample, and which scanner devices move the beam across it.

    ``axes`` are ``(device, µm per volt)`` for the sample's x and y: the
    scanner waveforms are in volts, and the factor is the positioner's
    ``conversionFactor``.
    """

    axes: Tuple[Tuple[str, float], ...]
    seed: int = 0

    @classmethod
    def from_property(cls, value) -> Optional['MockSample']:
        """The ``mockSample`` manager property; None when absent."""
        if value is None or value is False:
            return None
        if not isinstance(value, Mapping) or not isinstance(value.get('axes'), Mapping):
            raise ValueError(
                'mockSample needs "axes": {"<x device>": <µm per V>, "<y device>": <µm per V>}'
            )
        axes = tuple((str(device), float(factor)) for device, factor in value['axes'].items())
        if not 1 <= len(axes) <= 2 or any(factor == 0 for _, factor in axes):
            raise ValueError('mockSample "axes" names one or two devices with a non-zero µm per V')
        return cls(axes=axes, seed=int(value.get('seed', 0)))

    def texture(self) -> np.ndarray:
        with _cacheLock:
            texture = _cache.get(self.seed)
            if texture is None:
                texture = _cache[self.seed] = _build_texture(self.seed)
        return texture

    def brightness(self, x_um, y_um) -> np.ndarray:
        """Relative brightness (background to about 1) at these positions."""
        texture = self.texture()
        n = texture.shape[0]
        col = np.rint((np.asarray(x_um, dtype=float) + FIELD_HALF_UM) / TEXEL_UM).astype(np.int64)
        row = np.rint((np.asarray(y_um, dtype=float) + FIELD_HALF_UM) / TEXEL_UM).astype(np.int64)
        inside = (col >= 0) & (col < n) & (row >= 0) & (row < n)
        values = np.full(np.broadcast(col, row).shape, BACKGROUND, dtype=float)
        values[inside] = texture[row[inside], col[inside]]
        return values


def _build_texture(seed: int) -> np.ndarray:
    from scipy.ndimage import gaussian_filter

    rng = np.random.default_rng(seed)
    n = int(round(2 * FIELD_HALF_UM / TEXEL_UM)) + 1
    texture = np.zeros((n, n), dtype=np.float32)
    axis = np.arange(n) * TEXEL_UM - FIELD_HALF_UM

    def paint_point(x, y, value):
        col = int(round((x + FIELD_HALF_UM) / TEXEL_UM))
        row = int(round((y + FIELD_HALF_UM) / TEXEL_UM))
        if 0 <= row < n and 0 <= col < n:
            texture[row, col] += value

    # Cells, placed so they rarely overlap.
    centres = []
    for _ in range(400):
        if len(centres) >= 16:
            break
        x, y = rng.uniform(-FIELD_HALF_UM + 5, FIELD_HALF_UM - 5, 2)
        radius = rng.uniform(3.0, 5.5)
        if all(np.hypot(x - cx, y - cy) > radius + cr + 0.5 for cx, cy, cr in centres):
            centres.append((x, y, radius))
    for x, y, radius in centres:
        aspect = rng.uniform(0.7, 1.0)
        angle = rng.uniform(0, np.pi)
        box = slice(*np.searchsorted(axis, (y - radius - 1, y + radius + 1))), \
            slice(*np.searchsorted(axis, (x - radius - 1, x + radius + 1)))
        yy, xx = np.meshgrid(axis[box[0]] - y, axis[box[1]] - x, indexing='ij')
        u = xx * np.cos(angle) + yy * np.sin(angle)
        v = (-xx * np.sin(angle) + yy * np.cos(angle)) / aspect
        r = np.hypot(u, v)
        patch = np.where(r < radius, 0.12, 0.0)                         # cytoplasm
        patch += np.exp(-((r - radius) / 0.12) ** 2)                     # membrane
        nx, ny = rng.uniform(-0.2, 0.2, 2) * radius
        patch += np.where(np.hypot(u - nx, v - ny) < 0.4 * radius, 0.25, 0.0)   # nucleus
        texture[box] += patch.astype(np.float32)
        for _ in range(10):                                              # vesicles
            rr = radius * np.sqrt(rng.uniform(0.2, 0.8))
            phi = rng.uniform(0, 2 * np.pi)
            px, py = rr * np.cos(phi), rr * np.sin(phi) * aspect
            paint_point(x + px * np.cos(angle) - py * np.sin(angle),
                        y + px * np.sin(angle) + py * np.cos(angle), 35.0)
    for x, y in rng.uniform(-FIELD_HALF_UM, FIELD_HALF_UM, (40, 2)):     # beads
        paint_point(x, y, 45.0)

    texture = gaussian_filter(texture, PSF_SIGMA_UM / TEXEL_UM)
    texture /= float(texture.max())
    return np.maximum(texture, BACKGROUND).astype(np.float32)


__all__ = ['MockSample', 'BACKGROUND', 'FIELD_HALF_UM']


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
