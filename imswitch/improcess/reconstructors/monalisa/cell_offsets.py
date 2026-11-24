"""The offset of every focus, read from the borders of the cells.

Light that does not move with the scan (out of focus, autofluorescence) is
background to every frame alike. Where it is smooth under a spot, the fit
takes it for the constant it has for that. Where it is structured on the
scale of the spot, a part of it has the shape of the spot, and the fit takes
that part for amplitude: the same amount in every frame. The focus' cell of
the image is then too bright or too dark as a whole.

Within one cell nothing tells this offset from specimen. Between cells the
image does: the specimen is continuous across the border of two cells, and
the offsets are not. For every pair of neighbouring cells the step of the
image across their border, beyond what the image's slope on both sides makes
expected, measures the difference of their offsets. The offsets follow from
all the differences by least squares.

Only differences are measured, so what all cells have in common, and what
varies slowly over many cells, stays in the image; it looks like specimen
and cannot be told from it.

An offset read from a border is as noisy as the border's pixels allow, and a
cell that is moved by that noise as a whole shows more than the noise of its
pixels did: on a recording of sparse filaments the offsets tiled the image
they were meant to clear. So the offsets are measured twice, from two halves
of the border pixels. What the two measurements share is offset; what they
differ by is noise, and the offsets are scaled down by its share. Where
there are no offsets to find, none are applied.
"""

from __future__ import annotations

import numpy as np
from scipy import sparse
from scipy.sparse.linalg import spsolve


def _border_steps(image: np.ndarray, owner: np.ndarray, axis: int):
    """Excess steps across the cell borders that run across ``axis``.

    For neighbours ``p`` and ``p + 1`` along ``axis`` in different cells:
    the step of the image from ``p`` to ``p + 1`` minus the mean of the
    slopes just inside the two cells.
    """
    image = np.moveaxis(image, axis, 0)
    owner = np.moveaxis(owner, axis, 0)
    inner, outer = owner[1:-2], owner[2:-1]
    before, after = owner[:-3], owner[3:]
    values = image[:-3], image[1:-2], image[2:-1], image[3:]
    border = (
        (inner >= 0) & (outer >= 0) & (inner != outer)
        & (before == inner) & (after == outer)
    )
    for value in values:
        border &= np.isfinite(value)
    step = values[2] - values[1]
    slope = 0.5 * ((values[1] - values[0]) + (values[3] - values[2]))
    return inner[border], outer[border], (step - slope)[border]


def _solve(first, second, excess, num_foci, stiffness, min_border):
    """Least-squares offsets from the excess steps of pixel pairs.

    Returns ``(offsets, connected)``; ``connected`` marks the foci that have
    a usable border.
    """
    none = np.zeros(num_foci), np.zeros(num_foci, dtype=bool)
    if first.size == 0:
        return none
    # One measurement per pair of cells: the median over their border, which
    # a structure crossing the border in a few pixels does not move.
    swap = first > second
    low = np.where(swap, second, first)
    high = np.where(swap, first, second)
    excess = np.where(swap, -excess, excess)
    pair = low * np.int64(num_foci) + high
    order = np.argsort(pair, kind="stable")
    pair, excess = pair[order], excess[order]
    starts = np.flatnonzero(np.concatenate([[True], np.diff(pair) > 0]))
    counts = np.diff(np.concatenate([starts, [pair.size]]))
    medians = np.array([
        np.median(part) for part in np.split(excess, starts[1:])
    ])
    use = counts >= int(min_border)
    if not use.any():
        return none
    low = (pair[starts] // num_foci)[use]
    high = (pair[starts] % num_foci)[use]
    difference = medians[use]          # offset[high] - offset[low]
    weight = counts[use].astype(float)

    edges = np.arange(low.size)
    incidence = sparse.csr_matrix(
        (
            np.concatenate([np.ones(low.size), -np.ones(low.size)]),
            (np.concatenate([edges, edges]), np.concatenate([high, low])),
        ),
        shape=(low.size, num_foci),
    )
    weighted = sparse.diags(weight) @ incidence
    normal = (incidence.T @ weighted).tocsr()
    diagonal = normal.diagonal()
    connected = diagonal > 0
    pull = stiffness * np.where(connected, diagonal, 1.0)
    system = (normal + sparse.diags(np.where(connected, pull, 1.0))).tocsc()
    offsets = spsolve(system, incidence.T @ (weight * difference))
    return np.where(connected, offsets, 0.0), connected


def fit_cell_offsets(
    image: np.ndarray,
    owner: np.ndarray,
    num_foci: int,
    stiffness: float = 0.02,
    min_border: int = 4,
    shrink: bool = True,
    return_share: bool = False,
):
    """The offset of every focus' cell, from the steps at the cell borders.

    Args:
        image: Amplitude image on the output raster, placed without
            interpolation.
        owner: For every pixel the focus it was measured by, negative where
            there is none.
        num_foci: Number of foci; the offsets are indexed like the foci.
        stiffness: Pull of every offset towards zero, relative to the weight
            of its borders. It fixes what the differences leave open and
            keeps what varies slowly over many cells in the image.
        min_border: Borders of fewer pixel pairs are not used.
        shrink: Scale the offsets down by the share of noise in them.
        return_share: Also return the share of the measured offsets that is
            offset and not noise, between 0 and 1.

    Returns:
        ``(num_foci,)`` offsets to subtract from the amplitudes of the foci,
        0 for foci without a usable border; with ``return_share`` a tuple of
        the offsets and the share.
    """
    image = np.asarray(image, dtype=float)
    owner = np.asarray(owner)
    first, second, excess = [], [], []
    for axis in (0, 1):
        a, b, d = _border_steps(image, owner, axis)
        first.append(a)
        second.append(b)
        excess.append(d)
    first = np.concatenate(first)
    second = np.concatenate(second)
    excess = np.concatenate(excess)
    offsets, connected = _solve(first, second, excess, num_foci, stiffness, min_border)

    share = 1.0
    if shrink and connected.any():
        half = np.random.default_rng(0).random(first.size) < 0.5
        minimum = max(2, int(min_border) // 2)
        one, has_one = _solve(
            first[half], second[half], excess[half], num_foci, stiffness, minimum
        )
        other, has_other = _solve(
            first[~half], second[~half], excess[~half], num_foci, stiffness, minimum
        )
        both = has_one & has_other
        if both.sum() >= 8:
            # Each half has twice the noise variance of the whole.
            signal = float(np.mean(one[both] * other[both]))
            noise = float(np.mean((one[both] - other[both]) ** 2)) / 4.0
            share = max(signal, 0.0) / max(max(signal, 0.0) + noise, 1e-300)
        offsets = share * offsets
    if return_share:
        return offsets, float(share)
    return offsets


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
