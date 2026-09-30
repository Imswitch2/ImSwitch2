"""Sharpening a reconstruction, as a step of its own.

How sharp a reconstruction looks and how much it holds are two things. The
footprint of the extraction is a pinhole: a small one gives the narrower
image and the noisier one. On the recordings a footprint of 1.5 sigma showed
filaments 12-17 % narrower than one of 2.5 sigma, at 2.3 times the noise,
and it looked as if resolution had to be bought with noise. It does not: the
image of the wide footprint, sharpened with the filter below to the width of
the small footprint's, has a third of the small footprint's noise
(``docs/monalisa_optimal_reconstruction.md``, section 10). The wide footprint
holds more at every spatial frequency; the small one only weighs the
frequencies differently, and pays for it.

So the pipeline estimates with the wide footprint and leaves the weighing of
the frequencies to a filter that says what it does. :func:`wiener_sharpen`
undoes a Gaussian blur as far as a given noise allows. It is linear, it has
two numbers, and the image it was applied to stays available.
"""

from __future__ import annotations

import numpy as np
from scipy.ndimage import distance_transform_edt


def _fill_holes(image: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``image`` with every hole filled from the nearest pixel that has a value."""
    finite = np.isfinite(image)
    if finite.all():
        return image, finite
    if not finite.any():
        return np.zeros_like(image), finite
    nearest = distance_transform_edt(
        ~finite, return_distances=False, return_indices=True
    )
    return image[tuple(nearest)], finite


def wiener_gain(
    frequency: np.ndarray, sigma: float, regularization: float
) -> np.ndarray:
    """Gain of the sharpening filter at a spatial frequency, cycles per pixel.

    ``H / (H^2 + regularization)`` with ``H`` the transfer function of a
    Gaussian blur of ``sigma`` pixels, scaled to leave the mean of an image
    as it is. The gain rises with the frequency up to where ``H^2`` equals
    the regularization, to ``(1 + r) / (2 sqrt(r))`` there, and falls beyond:
    frequencies the blur has left too little of are not amplified but
    suppressed.
    """
    transfer = np.exp(-2.0 * np.pi**2 * sigma**2 * np.asarray(frequency, float) ** 2)
    return (1.0 + regularization) * transfer / (transfer**2 + regularization)


def wiener_sharpen(
    image: np.ndarray, sigma: float, regularization: float = 0.1
) -> np.ndarray:
    """Undo a Gaussian blur of ``sigma`` pixels as far as the noise allows.

    Args:
        image: Reconstruction on the output raster; holes (NaN) stay holes.
        sigma: Width of the blur to undo, in pixels of the image. About 1.5
            pixels took the recordings' wide-footprint images to the width
            of the small footprint's.
        regularization: Noise power over signal power, taken as the same at
            all frequencies. Smaller sharpens more and amplifies more noise:
            0.1 amplifies by up to 1.7, 0.03 by up to 3.

    Returns:
        The sharpened image. Like every sharpening it can undershoot next to
        bright structures, and values below zero are not clipped.
    """
    if sigma <= 0:
        raise ValueError("sigma must be > 0")
    if regularization <= 0:
        raise ValueError("regularization must be > 0")
    image = np.asarray(image, dtype=float)
    filled, finite = _fill_holes(image)
    # Mirror the image at its edges: the filter is applied in the Fourier
    # domain, which would otherwise join the opposite edges.
    pad = int(np.ceil(6.0 * sigma)) + 2
    pad = min(pad, filled.shape[0] - 1, filled.shape[1] - 1)
    padded = np.pad(filled, pad, mode="reflect")
    fy = np.fft.fftfreq(padded.shape[0])[:, None]
    fx = np.fft.rfftfreq(padded.shape[1])[None, :]
    gain = wiener_gain(np.hypot(fx, fy), float(sigma), float(regularization))
    sharpened = np.fft.irfft2(np.fft.rfft2(padded) * gain, s=padded.shape)
    sharpened = sharpened[pad:pad + image.shape[0], pad:pad + image.shape[1]]
    return np.where(finite, sharpened, np.nan)


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
