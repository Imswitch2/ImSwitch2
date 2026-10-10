"""How a device moves between two positions (docs/design/plans/
scan-engines-and-expert-scan.md §2.2).

A transition is sampled on a grid: ``samples`` values that start at ``start``
and end at ``end``. The shapes are the ones today's designers use, defined
once here so the designer and the scan-program interpreter cannot drift.
"""

from __future__ import annotations

import numpy as np

#: Transition shapes a ``Motion`` may name.
TRANSITIONS = ('jump', 'linear', 'smooth')

#: The share of a ``smooth`` transition spent moving; the rest holds at the
#: end position. This is BetaScanDesigner's ramp, unchanged: its shape is part
#: of what today's stepped scans play (the Beta goldens pin it).
SMOOTH_MOVING_SHARE = 0.6


def smooth_ramp(start, end, samples):
    """BetaScanDesigner's ramp: a quarter sine from ``start`` to ``end`` over
    the first 60 % of ``samples``, then ``end`` for the rest."""
    start = float(start)
    end = float(end)
    n = int(np.floor(SMOOTH_MOVING_SHARE * samples))
    x = np.linspace(0, np.pi / 2, num=n, endpoint=True)
    signal = start + (end - start) * np.sin(x)
    return np.append(signal, end * np.ones(int(np.ceil((1 - SMOOTH_MOVING_SHARE) * samples))))


def transition(shape, start, end, samples):
    """``samples`` values of a ``shape`` transition from ``start`` to ``end``."""
    samples = int(samples)
    if shape == 'smooth':
        return smooth_ramp(start, end, samples)
    if shape == 'linear':
        return np.linspace(float(start), float(end), num=samples, endpoint=True)
    if shape == 'jump':
        return np.full(samples, float(end))
    raise ValueError(f'Unknown transition shape {shape!r}; expected one of {TRANSITIONS}')


__all__ = ['SMOOTH_MOVING_SHARE', 'TRANSITIONS', 'smooth_ramp', 'transition']


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
