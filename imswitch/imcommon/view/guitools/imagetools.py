import numpy as np


#: ImageJ's "Auto" brightness/contrast (``ContrastAdjuster.autoAdjust``):
#: a 256-bin histogram; any bin holding more than this fraction of the pixels
#: is treated as background and ignored...
_IMAGEJ_AUTO_LIMIT_FRACTION = 1 / 10
#: ...and the levels go from the first to the last bin holding more than
#: ``pixelCount / AUTO_THRESHOLD`` pixels, ImageJ's ``AUTO_THRESHOLD`` being
#: 5000 on the first press of the button.
_IMAGEJ_AUTO_THRESHOLD = 5000
_IMAGEJ_HISTOGRAM_BINS = 256


def bestLevels(arr):
    """Display levels by ImageJ's "Auto" brightness/contrast.

    Ported from ImageJ's ``ContrastAdjuster.autoAdjust`` (first press): a
    256-bin histogram, bins holding more than a tenth of the pixels ignored
    as background, and the levels running from the first to the last bin
    holding more than 1/5000 of them -- so a few hot or dead pixels saturate
    instead of setting the range. As in ImageJ, when no bin qualifies, or
    the two ends meet, the levels are the data's own minimum and maximum.

    The earlier port (via the CMCI ImageJ cookbook) began its search at the
    second bin, and returned inverted levels for an image with no
    qualifying bin, such as a uniform one.
    """
    data = np.asarray(arr)
    pixelCount = data.size
    hist, bin_edges = np.histogram(data, _IMAGEJ_HISTOGRAM_BINS)
    counts = np.where(hist > pixelCount * _IMAGEJ_AUTO_LIMIT_FRACTION, 0, hist)
    qualifying = np.flatnonzero(counts > pixelCount / _IMAGEJ_AUTO_THRESHOLD)
    if qualifying.size:
        low = bin_edges[qualifying[0]]
        high = bin_edges[qualifying[-1]]
        if high > low:
            return low, high
    return data.min(), data.max()


def minmaxLevels(arr):
    """``(0, max + 2)`` over the finite values: a NaN (a trajectory bin no
    window reached) must not make the contrast limits NaN."""
    minlevel = 0
    arr = np.asarray(arr)
    finite = arr[np.isfinite(arr)] if np.issubdtype(arr.dtype, np.floating) else arr
    maxlevel = (finite.max() if finite.size else 0) + 2

    return minlevel, maxlevel


# Copyright (C) 2017 Federico Barabas
# This file is part of Tormenta.
#
# Tormenta is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Tormenta is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
