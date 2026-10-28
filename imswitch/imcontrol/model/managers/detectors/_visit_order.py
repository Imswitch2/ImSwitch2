"""Where a point detector's visit belongs in its image
(docs/design/plans/scan-engines-and-expert-scan.md §2.5, M2).

A point detector reads its scan in time order: pixel after pixel, line after
line. With a forward raster, time order is image order. An interleaved axis
(0, 2, 4, then 1, 3, 5) or a serpentine one (every other line reversed) visits
its positions in another order, and the detector must place each visit at
its image index, or the image is scrambled while every count adds up.

The scan says how in ``scanInfoDict['axis_visit_order']``: one entry per
image axis (x, y, z) -- ``None`` (forward), ``{'interleave': [image index per
visit]}``, ``{'serpentine': 'line'}`` (x runs back on every other line played
in a slice, line steps included) or ``{'serpentine': 'slice'}`` (y runs back
on every other slice). Scans without the key are placed as before.
"""

from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

AXIS_ORDER_KEY = 'axis_visit_order'


class VisitOrder:
    def __init__(self, orders: Sequence, dims: Sequence[int]):
        self._orders = list(orders) + [None] * (3 - len(orders))
        self._dims = [int(d) for d in dims] + [1] * (3 - len(dims))
        for axis, order in enumerate(self._orders):
            if order is None:
                continue
            if not isinstance(order, dict) or len(order) != 1 or not (
                    'interleave' in order
                    or (axis < 2 and order.get('serpentine') == ('line', 'slice')[axis])):
                raise ValueError(f'Unknown visit order for image axis {axis}: {order!r}')

    @classmethod
    def from_scan_info(cls, scanInfoDict) -> Optional['VisitOrder']:
        orders = (scanInfoDict or {}).get(AXIS_ORDER_KEY)
        if not orders or all(order is None for order in orders):
            return None
        return cls(orders, scanInfoDict.get('img_dims') or ())

    def _kind(self, axis):
        order = self._orders[axis]
        return next(iter(order)) if order else 'forward'

    def place(self, pixels, y, outer=(), line=None):
        """``(pixels, y, outer)`` in image order, from a line read in visit
        order. ``y`` is the line's visit ordinal; ``outer`` the outer visit
        ordinals, outermost first (the last is z); ``line`` the ordinal of the
        line played in its slice, line steps included (``y`` without them)."""
        outer = tuple(int(v) for v in outer)
        line = int(y) if line is None else int(line)
        z = outer[-1] if outer else 0
        pixels = np.asarray(pixels)
        if self._kind(0) == 'interleave':
            index = np.asarray(self._orders[0]['interleave'][:len(pixels)], dtype=int)
            placed = np.empty_like(pixels)
            placed[index] = pixels[:len(index)]
            pixels = placed
        elif self._kind(0) == 'serpentine' and line % 2:
            pixels = pixels[::-1]
        y = int(y)
        if self._kind(1) == 'interleave':
            y = int(self._orders[1]['interleave'][y])
        elif self._kind(1) == 'serpentine' and z % 2:
            y = self._dims[1] - 1 - y
        if outer and self._kind(2) == 'interleave':
            outer = outer[:-1] + (int(self._orders[2]['interleave'][z]),)
        return pixels, y, outer


def require_forward(detector, scanInfoDict):
    """Refuse, with the reason, a scan that visits an axis out of order, for
    a detector that builds its image in time order and cannot place visits."""
    if VisitOrder.from_scan_info(scanInfoDict) is not None:
        raise ValueError(
            f'{detector} cannot place a scan that visits an axis out of order '
            '(interleaved or serpentine); scan forward, or use an APD or PMT.')


__all__ = ['AXIS_ORDER_KEY', 'VisitOrder', 'require_forward']


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
