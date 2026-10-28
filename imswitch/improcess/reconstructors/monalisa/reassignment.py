"""Pixel reassignment for foci that are not tightly confined.

A focus of effective width ``sigma_e`` imaged with a detection PSF of width
``sigma_d`` does not send the same image to every pixel of its spot. The
pixel at offset ``d`` from the focus sees the specimen around ``alpha * d``,
with ``alpha = sigma_e^2 / (sigma_e^2 + sigma_d^2)``. For a RESOLFT focus far
narrower than the detection PSF ``alpha`` is a few percent and the amplitude
of the spot is all there is to extract. For a focus that is only weakly
confined ``alpha`` approaches 0.5, and summing the spot into one amplitude
blurs: the wider the footprint, the more. Placing every pixel's value where
it was measured instead, at ``q + alpha * d``, keeps the resolution of a
small pinhole at the noise of a large one. That is image scanning
microscopy.

Which regime a recording is in is measured on the recording
(:func:`measure_shift_factor`), not assumed. The three recordings this was
developed on measure ``alpha`` = 0.30, 0.37 and 0.45
(``docs/monalisa_optimal_reconstruction.md``, section 9).

Coordinates are ``(x, y)`` in camera pixels, x along columns.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .extraction import ExtractionOperator
from .placement import HOLE_DENSITY, OutputRaster, PlacedImage, sample_positions

DEFAULT_OFFSETS = (
    (1, 0), (-1, 0), (0, 1), (0, -1),
    (1, 1), (-1, -1), (1, -1), (-1, 1),
    (2, 0), (-2, 0), (0, 2), (0, -2),
)


@dataclass(frozen=True)
class ShiftFactor:
    """The measured shift factor and what it was measured from.

    Attributes:
        alpha: Median over the detector offsets.
        alpha_x, alpha_y: The same along the two camera axes; they differ
            when the focus is not round.
        per_offset: ``(offsets, 4)``: ``dx, dy, alpha, correlation``.
        spread: Robust standard deviation of the per-offset values.
    """

    alpha: float
    alpha_x: float
    alpha_y: float
    per_offset: np.ndarray
    spread: float

    @property
    def width_ratio(self) -> float:
        """``sigma_e / sigma_d`` that ``alpha`` corresponds to."""
        alpha = float(np.clip(self.alpha, 0.0, 0.999))
        return float(np.sqrt(alpha / (1.0 - alpha)))


def _raster_coordinates(x, y, offsets, raster: OutputRaster):
    qx, qy = sample_positions(x, y, offsets[:, 0], offsets[:, 1])
    return raster.coordinates(qx, qy)


def _splat(gx, gy, values, weights, shape):
    """Bilinear accumulation of weighted values and of the weights."""
    rows, cols = shape
    gx, gy = gx.ravel(), gy.ravel()
    values, weights = values.ravel(), weights.ravel()
    x0 = np.floor(gx).astype(np.int64)
    y0 = np.floor(gy).astype(np.int64)
    fx, fy = gx - x0, gy - y0
    total = np.zeros(rows * cols)
    norm = np.zeros(rows * cols)
    for dy, wy in ((0, 1.0 - fy), (1, fy)):
        for dx, wx in ((0, 1.0 - fx), (1, fx)):
            px, py = x0 + dx, y0 + dy
            tap = wx * wy
            inside = (px >= 0) & (px < cols) & (py >= 0) & (py < rows) & (tap > 0)
            flat = (py * cols + px)[inside]
            total += np.bincount(
                flat, weights=(tap * values)[inside], minlength=rows * cols
            )
            norm += np.bincount(
                flat, weights=(tap * weights)[inside], minlength=rows * cols
            )
    return total.reshape(rows, cols), norm.reshape(rows, cols)


def _shift_between(reference: np.ndarray, image: np.ndarray) -> tuple[float, float, float]:
    """Shift of ``image`` against ``reference`` in pixels, and their correlation."""
    window = np.outer(np.hanning(reference.shape[0]), np.hanning(reference.shape[1]))
    a = (reference - reference.mean()) * window
    b = (image - image.mean()) * window
    correlation = np.fft.fftshift(
        np.real(np.fft.ifft2(np.conj(np.fft.fft2(a)) * np.fft.fft2(b)))
    )
    peak_y, peak_x = np.unravel_index(np.argmax(correlation), correlation.shape)
    half = 3
    if not (
        half <= peak_y < correlation.shape[0] - half
        and half <= peak_x < correlation.shape[1] - half
    ):
        return np.nan, np.nan, 0.0
    yy, xx = np.mgrid[-half:half + 1, -half:half + 1]
    patch = correlation[peak_y - half:peak_y + half + 1, peak_x - half:peak_x + half + 1]
    design = np.column_stack(
        [np.ones(xx.size), xx.ravel(), yy.ravel(),
         xx.ravel() ** 2, yy.ravel() ** 2, (xx * yy).ravel()]
    )
    c = np.linalg.lstsq(design, patch.ravel(), rcond=None)[0]
    hessian = np.array([[2 * c[3], c[5]], [c[5], 2 * c[4]]])
    if np.linalg.det(hessian) <= 0 or c[3] >= 0:
        return np.nan, np.nan, 0.0
    delta = -np.linalg.solve(hessian, c[1:3])
    norm = float(np.sqrt(np.sum(a * a) * np.sum(b * b)))
    return (
        float(peak_x + delta[0] - reference.shape[1] // 2),
        float(peak_y + delta[1] - reference.shape[0] // 2),
        float(correlation.max() / norm) if norm > 0 else 0.0,
    )


def measure_shift_factor(
    frames: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    offsets: np.ndarray,
    raster: OutputRaster,
    detector_offsets=DEFAULT_OFFSETS,
    border: float = 0.15,
) -> ShiftFactor:
    """Measure the image-scanning shift factor of a recording.

    For every detector offset ``d`` the image seen by the pixels at ``d``
    from their focus is assembled and registered against the image of the
    central pixels. It is displaced by ``-alpha * d``.

    Args:
        frames: ``(K, rows, cols)``, one scan.
        x, y: Focus centres, px.
        offsets: ``(K, 2)`` displacement of the sample per frame, camera px.
        raster: The output raster the scan is commensurate with.
        detector_offsets: The pixel offsets to measure at.
        border: Fraction of the image left out at each edge.
    """
    frames = np.asarray(frames)
    rows, cols = frames.shape[1:]
    x = np.asarray(x, dtype=float).ravel()
    y = np.asarray(y, dtype=float).ravel()
    reach = int(max(max(abs(dx), abs(dy)) for dx, dy in detector_offsets))
    cx, cy = np.rint(x).astype(int), np.rint(y).astype(int)
    inside = (
        (cx - reach >= 0) & (cx + reach < cols)
        & (cy - reach >= 0) & (cy + reach < rows)
    )
    cx, cy = cx[inside], cy[inside]
    gx, gy = _raster_coordinates(x[inside], y[inside], np.asarray(offsets, float), raster)
    # The static background carries no image; without it the registration
    # sees the modulation only.
    mean = frames.mean(axis=0, dtype=np.float64)

    def image(dx: int, dy: int) -> np.ndarray:
        values = frames[:, cy + dy, cx + dx].astype(float) - mean[cy + dy, cx + dx]
        total, norm = _splat(gx, gy, values, np.ones_like(values), raster.shape)
        r0, r1 = int(border * total.shape[0]), int((1 - border) * total.shape[0])
        c0, c1 = int(border * total.shape[1]), int((1 - border) * total.shape[1])
        with np.errstate(invalid="ignore", divide="ignore"):
            out = np.where(norm > HOLE_DENSITY, total / norm, 0.0)
        return out[r0:r1, c0:c1]

    central = image(0, 0)
    step = np.asarray(raster.step, dtype=float)
    measured = []
    for dx, dy in detector_offsets:
        sx, sy, correlation = _shift_between(central, image(dx, dy))
        shift = step @ np.array([sx, sy])
        d = np.array([dx, dy], dtype=float)
        measured.append((dx, dy, -float(shift @ d) / float(d @ d), correlation))
    measured = np.array(measured)
    good = np.isfinite(measured[:, 2]) & (measured[:, 3] > 0.05)
    if not good.any():
        raise ValueError("The shift factor could not be measured: no image correlates")
    values = measured[good, 2]
    alpha = float(np.median(values))

    def along(axis: int) -> float:
        other = 1 - axis
        pick = good & (measured[:, other] == 0) & (measured[:, axis] != 0)
        return float(np.median(measured[pick, 2])) if pick.any() else alpha

    spread = float(1.4826 * np.median(np.abs(values - alpha)))
    return ShiftFactor(
        alpha=alpha, alpha_x=along(0), alpha_y=along(1),
        per_offset=measured, spread=spread,
    )


@dataclass(frozen=True)
class PinholeStack:
    """The scan as every pixel of the footprint saw it, one image per pixel.

    Attributes:
        images: ``(pinholes, rows, cols)`` on the output raster, NaN where a
            pinhole has no sample.
        dx, dy: ``(pinholes,)`` offset of each virtual pinhole from the pixel
            its focus is centred in, camera pixels; the central one first,
            then by distance.
        shifted: Whether the images were moved to where they belong
            (by the shift factor times the offset) or left where the focus
            is.
    """

    images: np.ndarray
    dx: np.ndarray
    dy: np.ndarray
    shifted: bool


def pinhole_offsets(radius: float) -> tuple[np.ndarray, np.ndarray]:
    """The pixel offsets within ``radius``, the centre first, then by distance."""
    half = int(np.floor(radius))
    span = np.arange(-half, half + 1)
    oy, ox = np.meshgrid(span, span, indexing="ij")
    inside = ox**2 + oy**2 <= radius * radius
    ox, oy = ox[inside], oy[inside]
    order = np.lexsort((ox, oy, ox**2 + oy**2))
    return ox[order], oy[order]


def pinhole_stack(
    frames: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    offsets: np.ndarray,
    raster: OutputRaster,
    radius: float,
    alpha: float | tuple[float, float] | None = None,
) -> PinholeStack:
    """Assemble the raw frames into one image per virtual pinhole.

    The check on a reconstruction that needs no model: for every pixel offset
    ``d`` within ``radius`` of a focus, the values of the pixels at ``d``
    from their foci are put where their foci were, frame by frame. Nothing is
    fitted, weighted or subtracted. Every image shows the specimen, on the
    camera's offset and background; the image of an off-centre pinhole is
    dimmer, and displaced against the central one by the shift factor times
    its offset.

    Args:
        frames: ``(K, rows, cols)``, one scan.
        x, y: Focus centres, px.
        offsets: ``(K, 2)`` displacement of the sample per frame, camera px.
        raster: The output raster.
        radius: Radius of the footprint, camera pixels. It sets the number
            of images: 9 for 1.5 px, 29 for 3 px, 81 for 5 px.
        alpha: When given, every image is moved to where it belongs, by
            ``alpha`` times its offset: the images then coincide, and their
            weighted sum is the reassigned reconstruction.
    """
    frames = np.asarray(frames)
    rows, cols = frames.shape[1:]
    x = np.asarray(x, dtype=float).ravel()
    y = np.asarray(y, dtype=float).ravel()
    gx, gy = _raster_coordinates(x, y, np.asarray(offsets, float).reshape(-1, 2), raster)
    cx, cy = np.rint(x).astype(int), np.rint(y).astype(int)
    ox, oy = pinhole_offsets(float(radius))
    inverse = np.linalg.inv(np.asarray(raster.step, dtype=float))
    alpha_xy = None if alpha is None else np.broadcast_to(np.asarray(alpha, float), (2,))

    images = np.full((ox.size, *raster.shape), np.nan, dtype=np.float32)
    for index, (dx, dy) in enumerate(zip(ox, oy)):
        px, py = cx + dx, cy + dy
        inside = (px >= 0) & (px < cols) & (py >= 0) & (py < rows)
        if not inside.any():
            continue
        values = frames[:, py[inside], px[inside]].astype(float)
        sx, sy = gx[:, inside], gy[:, inside]
        if alpha_xy is not None:
            shift = inverse @ np.array([alpha_xy[0] * dx, alpha_xy[1] * dy])
            sx, sy = sx + shift[0], sy + shift[1]
        total, norm = _splat(sx, sy, values, np.ones_like(values), raster.shape)
        with np.errstate(invalid="ignore", divide="ignore"):
            images[index] = np.where(norm > HOLE_DENSITY, total / norm, np.nan)
    return PinholeStack(images=images, dx=ox, dy=oy, shifted=alpha is not None)


def reassign(
    frames: np.ndarray,
    operator: ExtractionOperator,
    x: np.ndarray,
    y: np.ndarray,
    offsets: np.ndarray,
    raster: OutputRaster,
    alpha: float | tuple[float, float],
    chunk: int = 16,
    frame_gain: np.ndarray | None = None,
    focus_offset: np.ndarray | None = None,
) -> PlacedImage:
    """Reconstruct by placing every footprint pixel where it was measured.

    Every pixel of a focus' footprint, cleared of the background and of the
    other foci by the joint fit, is a sample of the specimen at
    ``q + alpha * d``. The samples are combined by least squares with the
    spot model as the weight: ``sum(g v) / sum(g^2)``, accumulated on the
    raster by bilinear splatting. With ``alpha = 0`` this is the amplitude
    image of the joint fit.

    Args:
        frames: ``(K, rows, cols)``.
        operator: The extraction operator of this geometry; its footprint
            sets the pinhole.
        x, y: The focus centres the operator was built with.
        offsets: ``(K, 2)`` displacement of the sample per frame, camera px.
        raster: Output raster.
        alpha: Shift factor, one value or ``(alpha_x, alpha_y)``.
        frame_gain: ``(K,)`` gain of every frame; its values are divided by
            it (:mod:`.frame_gain`).
        focus_offset: ``(foci,)`` offset of every focus' amplitude, taken
            off its values (:mod:`.cell_offsets`).

    Returns:
        The image, with ``count`` the accumulated weight per pixel relative
        to its median (the sample density).
    """
    frames = np.asarray(frames)
    offsets = np.asarray(offsets, dtype=float).reshape(-1, 2)
    if offsets.shape[0] != frames.shape[0]:
        raise ValueError(
            f"{frames.shape[0]} frames but {offsets.shape[0]} scan positions"
        )
    alpha_xy = np.broadcast_to(np.asarray(alpha, dtype=float), (2,))
    entries = operator.footprints
    x = np.asarray(x, dtype=float).ravel()
    y = np.asarray(y, dtype=float).ravel()
    gx, gy = _raster_coordinates(x, y, offsets, raster)
    inverse = np.linalg.inv(np.asarray(raster.step, dtype=float))
    shift_x = alpha_xy[0] * entries.dx
    shift_y = alpha_xy[1] * entries.dy
    entry_gx = inverse[0, 0] * shift_x + inverse[0, 1] * shift_y
    entry_gy = inverse[1, 0] * shift_x + inverse[1, 1] * shift_y
    weight = entries.weight

    total = np.zeros(raster.shape)
    norm = np.zeros(raster.shape)
    for start in range(0, frames.shape[0], int(chunk)):
        block = slice(start, start + int(chunk))
        values = operator.footprint_values(frames[block])
        if frame_gain is not None:
            values = values / np.asarray(frame_gain, dtype=float)[block][:, None]
        if focus_offset is not None:
            offset = np.asarray(focus_offset, dtype=float)[entries.focus]
            values = values - (offset * weight)[None, :]
        px = gx[block][:, entries.focus] + entry_gx[None, :]
        py = gy[block][:, entries.focus] + entry_gy[None, :]
        weights = np.broadcast_to(weight[None, :], values.shape)
        part_total, part_norm = _splat(
            px, py, values * weights, weights * weights, raster.shape
        )
        total += part_total
        norm += part_norm
    typical = float(np.median(norm[norm > 0])) if np.any(norm > 0) else 1.0
    density = norm / typical
    with np.errstate(invalid="ignore", divide="ignore"):
        image = np.where(density >= HOLE_DENSITY, total / norm, np.nan)
    return PlacedImage(image=image, count=density, method="reassigned")


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
