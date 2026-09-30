"""The brightness of the foci as it changes over a scan.

Averaged over hundreds of foci, the amplitude of a frame does not depend on
the specimen: every focus sees another part of it. What is left is what the
scan does to the fluorophores and the microscope to the scan. On the first
recordings the mean amplitude changed by 5 % rms over a scan, and smoothly:
it fell towards the end of the scan (bleaching), it was highest in the middle
of every line, and the first frame of a line was up to 12 % brighter than the
others, its fluorophores having rested during the line change
(``docs/monalisa_optimal_reconstruction.md``, section 9).

A frame that is brighter than its neighbours is the same scan position made
brighter in every cell: a pattern that repeats with the lattice. Dividing the
amplitudes by the gain removes it.

The gain is fitted as a smooth function of the position in the scan, not
taken frame by frame: the mean over the foci also holds the part of the
specimen that happens to repeat with the lattice, which is specimen and has
to stay.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class FrameGain:
    """The gain of every frame, and what it was fitted to.

    Attributes:
        gain: ``(K,)``, mean 1. Amplitudes are divided by it.
        measured: ``(K,)`` mean amplitude of the bright foci per frame,
            relative to its mean over the scan.
        rms: Standard deviation of ``gain``.
        residual_rms: Standard deviation of ``measured / gain``: what the
            smooth model leaves, noise and specimen.
    """

    gain: np.ndarray
    measured: np.ndarray
    rms: float
    residual_rms: float


def _axis_terms(position: np.ndarray, count: int, order: int) -> list[np.ndarray]:
    """Polynomial terms of a scan axis, and one for its first step."""
    if count < 3:
        return []
    u = 2.0 * position / (count - 1) - 1.0
    terms = [u**power for power in range(1, min(order, count - 2) + 1)]
    terms.append((position == 0).astype(float))
    return terms


def fit_frame_gain(
    amplitude: np.ndarray,
    scan_shape: tuple[int, int],
    order: int = 3,
    bright_fraction: float = 0.4,
) -> FrameGain:
    """Fit the gain of the frames of one raster scan.

    Args:
        amplitude: ``(K, foci)`` amplitudes in the order recorded, NaN for
            foci that were not extracted.
        scan_shape: ``(num_fast, num_slow)``.
        order: Polynomial order along each axis of the scan.
        bright_fraction: The share of the foci, the brightest, the mean is
            taken over. Foci without specimen only add noise.
    """
    num_fast, num_slow = int(scan_shape[0]), int(scan_shape[1])
    amplitude = np.asarray(amplitude, dtype=float)
    if amplitude.shape[0] != num_fast * num_slow:
        raise ValueError(
            f"{amplitude.shape[0]} frames do not make a scan of "
            f"{num_fast} x {num_slow} steps"
        )
    usable = np.all(np.isfinite(amplitude), axis=0)
    if not usable.any():
        raise ValueError("No focus has an amplitude in every frame")
    strength = amplitude[:, usable].mean(axis=0)
    bright = strength >= np.quantile(strength, 1.0 - bright_fraction)
    per_frame = amplitude[:, usable][:, bright].mean(axis=1)
    level = float(per_frame.mean())
    if not level > 0:
        raise ValueError("The foci have no signal to measure a gain on")
    measured = per_frame / level

    frame = np.arange(num_fast * num_slow)
    fast, slow = frame % num_fast, frame // num_fast
    terms = [np.ones(frame.size)]
    terms += _axis_terms(fast, num_fast, order)
    terms += _axis_terms(slow, num_slow, order)
    design = np.column_stack(terms)
    coefficients = np.linalg.lstsq(design, measured, rcond=None)[0]
    gain = design @ coefficients
    gain = np.clip(gain / gain.mean(), 0.2, 5.0)
    return FrameGain(
        gain=gain,
        measured=measured,
        rms=float(np.std(gain)),
        residual_rms=float(np.std(measured / gain)),
    )


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
