"""A simulated adaptive-optics bench for mock setups: a mock deformable
mirror registers itself by name, and a mock wavefront-sensor camera
(``TISManager`` with ``mockShackHartmann``) renders the lenslet spots of
that mirror's current command over a set aberration. So the AO scripts --
calibration, closed loop -- run end to end without hardware.

The optics are :class:`imswitch.imcommon.algorithms.adaptive_optics.
SimulatedMirror` (Gaussian influence functions, actuators beyond the pupil)
seen by a square lenslet array centred on the frame.
"""

import threading
import weakref
from typing import Optional

import numpy as np

from imswitch.imcommon.algorithms import adaptive_optics as _ao
from imswitch.imcommon.algorithms import shack_hartmann as _sh

_mirrors = weakref.WeakValueDictionary()
_lock = threading.Lock()


def register_mirror(name: str, mirror) -> None:
    """A mock mirror (anything with ``command`` and ``actuatorCount``) under
    its setup name."""
    with _lock:
        _mirrors[str(name)] = mirror


def mirror(name: str):
    with _lock:
        return _mirrors.get(str(name))


class SimulatedShackHartmann:
    """Frames of a Shack-Hartmann sensor behind the mirror ``mirror``.

    Properties (a mock camera's ``mockShackHartmann`` block, all optional):
    ``mirror`` (the mirror's setup name, default ``"DM"``), ``pixelUm``
    (3.45), ``lensletPitchUm`` (150), ``focalLengthUm`` (5200) -- the rig's
    camera and MLA150-5C lenslets --, ``lensletsAcross`` (16),
    ``aberration`` ({Noll mode: µm RMS}, default ``{"5": 0.08, "7": 0.05}``),
    ``strokeUm`` (0.5), ``noise`` (2.0), ``spotSigmaPx`` (1.6), ``peak``
    (800), ``offset`` (100), ``seed`` (0).
    """

    def __init__(self, properties: Optional[dict] = None):
        p = dict(properties or {})
        self.mirrorName = str(p.get('mirror', 'DM'))
        self.pixel_um = float(p.get('pixelUm', 3.45))
        self.pitch = float(p.get('lensletPitchUm', 150.0)) / self.pixel_um
        self.across = int(p.get('lensletsAcross', 16))
        self.focal_um = float(p.get('focalLengthUm', 5200.0))
        aberration = p.get('aberration', {'5': 0.08, '7': 0.05})
        self.aberration = {int(k): float(v) for k, v in dict(aberration).items()}
        self.stroke_um = float(p.get('strokeUm', 0.5))
        self.noise = float(p.get('noise', 2.0))
        self.sigma = float(p.get('spotSigmaPx', 1.6))
        self.peak = float(p.get('peak', 800.0))
        self.offset = float(p.get('offset', 100.0))
        self._rng = np.random.default_rng(int(p.get('seed', 0)))
        self._optics = {}           # actuator count -> SimulatedMirror
        self._grids = {}            # frame shape -> (grid, x, y, half width, scale)

    def _grid(self, shape):
        key = tuple(int(s) for s in shape)
        if key not in self._grids:
            radius = self.across * self.pitch / 2
            grid = _sh.regular_grid((key[0] / 2, key[1] / 2), self.pitch, self.across,
                                    self.across, window=max(2, int(self.pitch // 3)),
                                    pupil_radius=radius)
            x, y = grid.pupil_xy()
            scale = (self.pixel_um / self.focal_um) * (grid.pupil_radius * self.pixel_um)
            self._grids[key] = (grid, x, y, grid.half_width(), scale)
        return self._grids[key]

    def _mirrorOptics(self, count):
        if count not in self._optics:
            self._optics[count] = _ao.SimulatedMirror(count, stroke_um=self.stroke_um,
                                                      aberration=self.aberration)
        return self._optics[count]

    def frame(self, shape) -> np.ndarray:
        grid, x, y, half_width, scale = self._grid(shape)
        dm = mirror(self.mirrorName)
        if dm is not None:
            command = np.asarray(dm.command, dtype=np.float64)
        else:
            command = np.zeros(69)
        optics = self._mirrorOptics(command.size)
        slopes = optics.mean_slopes(x, y, half_width, command)
        positions = _sh.spots_for_slopes(grid, slopes, scale)
        frame = _sh.render_spots(shape, positions, sigma=self.sigma, peak=self.peak,
                                 offset=self.offset, noise=self.noise, rng=self._rng)
        return np.clip(frame, 0, 65535).astype(np.uint16)


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
