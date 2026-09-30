"""Quality of a reconstruction, measured without knowing the specimen.

A recording has no ground truth, but it can be asked two things.

*How much of the image is signal?* Reconstruct twice, from two halves of the
camera pixels that share no pixel (a checkerboard): the specimen is in both
images, the pixel noise of each is its own. Their Fourier ring correlation
is the share of signal at every spatial frequency, and half their difference
is the noise of their mean (:func:`split_masks`, :func:`ring_correlation`,
:func:`split_noise`).

*Does the image show the lattice?* In two ways it can. What differs from
scan position to scan position and should not (the brightness of the frames,
a step that is not the one assumed) is the same pattern in every cell, and
shows as peaks of the image's spectrum at the frequencies of the lattice
(:func:`tiling_contrast`). What differs from focus to focus (gain, offset, a
misplaced cell) is constant inside a cell and jumps at its border, which the
spectrum at those frequencies does not see, and the borders do
(:func:`seam_contrast`).

Neither sees a blur. Two estimators are compared by the ring correlation at
the frequencies where both still have signal: the better one has more of it.

*How wide is a thin structure?* A specimen of filaments holds its own
resolution targets. :func:`find_filaments` picks the isolated ones in one
image, and :func:`filament_width` measures across them in any image of the
same specimen: the width of the mean profile. It is the width of the
filaments and of the image's blur together, so it says which of two images
is the sharper, not what the resolution is.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
from scipy.ndimage import gaussian_filter, map_coordinates, maximum_filter
from scipy.optimize import curve_fit


def split_masks(frame_shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    """The two halves of a checkerboard over the camera frame."""
    rows, cols = frame_shape
    yy, xx = np.mgrid[0:rows, 0:cols]
    even = (xx + yy) % 2 == 0
    return even, ~even


def central_part(image: np.ndarray, border: float = 0.15) -> np.ndarray:
    """The image without its border, holes filled with zero."""
    rows, cols = image.shape
    part = image[
        int(border * rows):int((1 - border) * rows),
        int(border * cols):int((1 - border) * cols),
    ]
    return np.where(np.isfinite(part), part, 0.0)


def ring_correlation(
    first: np.ndarray, second: np.ndarray, num_rings: int = 20
) -> tuple[np.ndarray, np.ndarray]:
    """Fourier ring correlation of two images of the same specimen.

    Returns ``(frequency, correlation)``: the centre of every ring in cycles
    per pixel, up to 0.5, and the correlation there.
    """
    first = np.asarray(first, dtype=float)
    second = np.asarray(second, dtype=float)
    if first.shape != second.shape:
        raise ValueError("The two images must have the same shape")
    window = np.outer(np.hanning(first.shape[0]), np.hanning(first.shape[1]))
    fa = np.fft.fft2((first - first.mean()) * window)
    fb = np.fft.fft2((second - second.mean()) * window)
    fy = np.fft.fftfreq(first.shape[0])[:, None]
    fx = np.fft.fftfreq(first.shape[1])[None, :]
    ring = np.rint(np.hypot(fx, fy) * 2 * num_rings).astype(int).ravel()
    keep = ring <= num_rings
    cross = np.bincount(ring[keep], weights=np.real(fa * np.conj(fb)).ravel()[keep])
    power_a = np.bincount(ring[keep], weights=(np.abs(fa) ** 2).ravel()[keep])
    power_b = np.bincount(ring[keep], weights=(np.abs(fb) ** 2).ravel()[keep])
    with np.errstate(invalid="ignore", divide="ignore"):
        correlation = cross / np.sqrt(power_a * power_b)
    frequency = np.arange(correlation.size) / (2.0 * num_rings)
    return frequency[1:], correlation[1:]


def resolution(
    frequency: np.ndarray, correlation: np.ndarray, threshold: float = 1.0 / 7.0
) -> float:
    """Period, in pixels, at which the ring correlation falls below a threshold.

    The threshold of 1/7 is the customary one. The number says down to which
    period the image holds more signal than noise, which a lower noise
    improves as much as a sharper image does.
    """
    below = np.flatnonzero(np.nan_to_num(correlation) < threshold)
    if below.size == 0:
        return float(1.0 / frequency[-1])
    if below[0] == 0:
        return float("inf")
    i = below[0]
    f0, f1 = frequency[i - 1], frequency[i]
    c0, c1 = correlation[i - 1], correlation[i]
    crossing = f0 + (c0 - threshold) / (c0 - c1) * (f1 - f0)
    return float(1.0 / crossing)


def split_noise(first: np.ndarray, second: np.ndarray) -> float:
    """Noise, as a standard deviation, of the mean of two split images."""
    difference = np.asarray(first, dtype=float) - np.asarray(second, dtype=float)
    finite = np.isfinite(difference)
    return float(np.std(difference[finite]) / 2.0)


def tiling_contrast(
    image: np.ndarray,
    index_matrix: np.ndarray,
    border: float = 0.2,
    max_order: int = 3,
) -> float:
    """How far the image's spectrum stands out at the frequencies of the cell.

    The power of the spectrum at every reciprocal vector of the illumination
    lattice up to ``max_order`` over its power around that vector, averaged
    over the vectors, as an amplitude ratio. An image without a trace of the
    lattice gives 1, give or take 0.15; a pattern that repeats in every cell
    gives more. An image gives less than 1 when what it had at those
    frequencies has been taken out of it.

    Args:
        image: Reconstruction on the output raster.
        index_matrix: The integer matrix ``N`` of
            :func:`.placement.commensurability`: the lattice in raster pixels.
    """
    part = central_part(image, border)
    part = (part - part.mean()) * np.outer(
        np.hanning(part.shape[0]), np.hanning(part.shape[1])
    )
    reciprocal = np.linalg.inv(np.asarray(index_matrix, dtype=float))
    rows = np.arange(part.shape[0], dtype=float)
    cols = np.arange(part.shape[1], dtype=float)

    def power(q: np.ndarray) -> float:
        along_x = np.exp(-2j * np.pi * q[0] * cols)
        along_y = np.exp(-2j * np.pi * q[1] * rows)
        return float(abs(along_y @ part @ along_x) ** 2)

    radius = 0.35 * min(np.linalg.norm(reciprocal[0]), np.linalg.norm(reciprocal[1]))
    angles = np.linspace(0.0, 2.0 * np.pi, 12, endpoint=False)
    ratios = []
    for h in range(0, max_order + 1):
        for k in range(-max_order, max_order + 1):
            if h == 0 and k <= 0:
                continue
            q = h * reciprocal[0] + k * reciprocal[1]
            if max(abs(q[0]), abs(q[1])) >= 0.5 - radius:
                continue
            around = np.mean([
                power(q + radius * np.array([np.cos(a), np.sin(a)])) for a in angles
            ])
            ratios.append(power(q) / max(float(around), 1e-300))
    if not ratios:
        return float("nan")
    return float(np.sqrt(np.mean(ratios)))


def seam_contrast(image: np.ndarray, owner: np.ndarray) -> float:
    """How much more neighbouring pixels differ across a cell border.

    The mean squared difference of neighbouring pixels that come from
    different foci over that of neighbours from the same focus, as an
    amplitude ratio. 1 is an image whose cells cannot be told apart.

    Args:
        image: Reconstruction on the output raster.
        owner: For every pixel the focus it was measured by, negative where
            there is none (``StackReconstruction.owner``).
    """
    image = np.asarray(image, dtype=float)
    owner = np.asarray(owner)
    across, within = [], []
    for axis in (0, 1):
        difference = np.diff(image, axis=axis) ** 2
        first = np.take(owner, np.arange(owner.shape[axis] - 1), axis=axis)
        second = np.take(owner, np.arange(1, owner.shape[axis]), axis=axis)
        usable = np.isfinite(difference) & (first >= 0) & (second >= 0)
        across.append(difference[usable & (first != second)])
        within.append(difference[usable & (first == second)])
    across = np.concatenate(across)
    within = np.concatenate(within)
    if across.size == 0 or within.size == 0:
        return float("nan")
    return float(np.sqrt(across.mean() / max(within.mean(), 1e-300)))


@dataclass(frozen=True)
class Filaments:
    """Places where an image is crossed by an isolated filament.

    Attributes:
        x, y: The places, in pixels; the filament's axis passes through them.
        across_x, across_y: Unit vector across the filament at each place.
        width: Width (FWHM, pixels) of each filament in the image they were
            found in.
    """

    x: np.ndarray
    y: np.ndarray
    across_x: np.ndarray
    across_y: np.ndarray
    width: np.ndarray

    def __len__(self) -> int:
        return int(self.x.size)

    def thinnest(self, fraction: float = 1.0 / 3.0) -> "Filaments":
        """The thinnest of the filaments: the closest to a line."""
        keep = self.width <= np.quantile(self.width, fraction)
        return Filaments(
            self.x[keep], self.y[keep], self.across_x[keep], self.across_y[keep],
            self.width[keep],
        )


_PROFILE = np.arange(-12.0, 12.01, 0.25)


def _bell(u, amplitude, centre, sigma, base):
    return amplitude * np.exp(-((u - centre) ** 2) / (2.0 * sigma**2)) + base


def _profiles(image, x, y, across_x, across_y):
    image = np.nan_to_num(np.asarray(image, dtype=float))
    rows = y[:, None] + _PROFILE[None, :] * across_y[:, None]
    cols = x[:, None] + _PROFILE[None, :] * across_x[:, None]
    return map_coordinates(image, [rows, cols], order=1, mode="nearest")


def _fit_bell(profile):
    start = (profile.max() - profile.min(), 0.0, 2.0, profile.min())
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            fit, _ = curve_fit(_bell, _PROFILE, profile, p0=start)
    except (RuntimeError, ValueError):
        return None
    return fit


def find_filaments(
    image: np.ndarray, scale: float = 2.0, border: int = 40, most: int = 500
) -> Filaments:
    """Find places where ``image`` is crossed by an isolated filament.

    Ridges of the image smoothed over ``scale`` pixels, the strongest first,
    kept where the profile across is one bell on a flat ground.

    Args:
        image: A reconstruction; the lowest-noise one at hand.
        scale: Smoothing of the ridge detection, pixels.
        border: Margin of the image that is left out.
        most: Number of ridge points examined.
    """
    filled = np.nan_to_num(np.asarray(image, dtype=float))
    dyy = gaussian_filter(filled, scale, order=(2, 0))
    dxx = gaussian_filter(filled, scale, order=(0, 2))
    dxy = gaussian_filter(filled, scale, order=(1, 1))
    mean = 0.5 * (dxx + dyy)
    root = np.sqrt((0.5 * (dxx - dyy)) ** 2 + dxy**2)
    low, high = mean - root, mean + root
    ridge = np.where((low < 0) & (np.abs(high) < 0.3 * np.abs(low)), -low, 0.0)
    ridge[:border] = ridge[-border:] = 0.0
    ridge[:, :border] = ridge[:, -border:] = 0.0
    empty = Filaments(*(np.zeros(0) for _ in range(5)))
    if not np.any(ridge > 0):
        return empty
    # Across the ridge: the direction of the most negative curvature.
    angle = 0.5 * np.arctan2(2.0 * dxy, dxx - dyy)
    ax, ay = np.cos(angle), np.sin(angle)
    curvature = dxx * ax * ax + 2.0 * dxy * ax * ay + dyy * ay * ay
    other = np.abs(curvature - low) > np.abs(curvature - high)
    ax, ay = np.where(other, -ay, ax), np.where(other, ax, ay)

    strong = ridge > np.percentile(ridge[ridge > 0], 90)
    peaks = (ridge == maximum_filter(ridge, size=15)) & strong
    py, px = np.nonzero(peaks)
    order = np.argsort(ridge[py, px])[::-1][:most]
    py, px = py[order], px[order]
    x, y = px.astype(float), py.astype(float)
    across_x, across_y = ax[py, px], ay[py, px]

    keep, centre, width = [], [], []
    for index, profile in enumerate(_profiles(filled, x, y, across_x, across_y)):
        fit = _fit_bell(profile)
        if fit is None:
            continue
        amplitude, middle, sigma = fit[0], fit[1], abs(fit[2])
        misfit = np.std(profile - _bell(_PROFILE, *fit))
        if amplitude > 0 and abs(middle) < 1.5 and 0.5 < sigma < 4.0 \
                and misfit < 0.15 * amplitude:
            keep.append(index)
            centre.append(middle)
            width.append(2.355 * sigma)
    if not keep:
        return empty
    keep, centre = np.array(keep), np.array(centre)
    return Filaments(
        x=x[keep] + centre * across_x[keep],
        y=y[keep] + centre * across_y[keep],
        across_x=across_x[keep],
        across_y=across_y[keep],
        width=np.array(width),
    )


def filament_width(image: np.ndarray, filaments: Filaments) -> float:
    """Width (FWHM, pixels) of the mean profile of ``image`` across filaments.

    Every profile is scaled to its own height before the mean is taken, so
    that the bright filaments do not decide. NaN when there are none.
    """
    if len(filaments) == 0:
        return float("nan")
    profiles = _profiles(
        image, filaments.x, filaments.y, filaments.across_x, filaments.across_y
    )
    ground = np.median(profiles[:, :8], axis=1, keepdims=True)
    height = np.maximum(profiles.max(axis=1, keepdims=True) - ground, 1e-300)
    fit = _fit_bell(((profiles - ground) / height).mean(axis=0))
    return float("nan") if fit is None else float(2.355 * abs(fit[2]))


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
