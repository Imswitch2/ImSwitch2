"""Synthetic MoNaLISA acquisitions with a known answer, for the lattice tests.

The frames follow the amplitude model of
``docs/monalisa_optimal_reconstruction.md`` (equations 2 and 3): every frame
is a lattice of Gaussian spots whose amplitudes are the specimen, already
blurred by the effective PSF, sampled at ``focus - scan offset``. The
specimen is an analytic band-limited field, so the truth is known at any
position and on any raster.

Nothing here imports the code under test: the frames are rendered with their
own few lines of NumPy.

:func:`make_physical_scan` goes one step further down: it images the specimen
itself, excited by a lattice of foci of finite width and blurred by the
detection PSF, so that the spots move with the emitters inside the focus.
That is what image scanning microscopy lives on, and what the amplitude model
leaves out.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def lattice_points(a1, a2, offset, shape, margin=0.0):
    """Points ``offset + m a1 + n a2`` within ``margin`` of a frame, as (N, 2)."""
    rows, cols = shape
    basis = np.array([[a1[0], a2[0]], [a1[1], a2[1]]], dtype=float)
    offset = np.asarray(offset, dtype=float)
    corners = np.array(
        [[-margin, -margin], [cols + margin, -margin],
         [-margin, rows + margin], [cols + margin, rows + margin]], dtype=float
    )
    index = np.linalg.solve(basis, (corners - offset).T).T
    low = np.floor(index.min(axis=0)).astype(int) - 1
    high = np.ceil(index.max(axis=0)).astype(int) + 1
    mm, nn = np.meshgrid(np.arange(low[0], high[0] + 1), np.arange(low[1], high[1] + 1))
    points = offset + mm.reshape(-1, 1) * basis[:, 0] + nn.reshape(-1, 1) * basis[:, 1]
    keep = (
        (points[:, 0] >= -margin) & (points[:, 0] < cols + margin)
        & (points[:, 1] >= -margin) & (points[:, 1] < rows + margin)
    )
    return points[keep]


def render_spots(shape, x, y, sigma, amplitude, cutoff_sigma=7.0):
    """Sum of Gaussian spots, each with its own centre, width and amplitude."""
    rows, cols = shape
    frame = np.zeros(shape)
    sigma = np.broadcast_to(np.asarray(sigma, dtype=float), np.shape(x))
    for cx, cy, width, amp in zip(x, y, sigma, amplitude):
        half = int(np.ceil(cutoff_sigma * width))
        x0, x1 = max(int(round(cx)) - half, 0), min(int(round(cx)) + half + 1, cols)
        y0, y1 = max(int(round(cy)) - half, 0), min(int(round(cy)) + half + 1, rows)
        if x0 >= x1 or y0 >= y1:
            continue
        yy, xx = np.mgrid[y0:y1, x0:x1].astype(float)
        frame[y0:y1, x0:x1] += amp * np.exp(
            -((xx - cx) ** 2 + (yy - cy) ** 2) / (2.0 * width * width)
        )
    return frame


def bandlimited_field(kmax, terms=60, seed=3, mean=3.0):
    """A positive random field with no structure above ``kmax`` cycles/px."""
    rng = np.random.default_rng(seed)
    k = rng.uniform(0, kmax, terms) * np.exp(1j * rng.uniform(0, 2 * np.pi, terms))
    amp = rng.normal(size=terms) / np.sqrt(terms)
    phase = rng.uniform(0, 2 * np.pi, terms)

    def field(x, y):
        x = np.asarray(x, dtype=float)[..., None]
        y = np.asarray(y, dtype=float)[..., None]
        waves = amp * np.cos(2 * np.pi * (k.real * x + k.imag * y) + phase)
        return mean + np.sum(waves, axis=-1)

    return field


@dataclass
class SyntheticScan:
    """One scan of the unit cell and everything that went into it."""

    frames: np.ndarray
    scan_index: np.ndarray
    step: np.ndarray
    focus_x: np.ndarray
    focus_y: np.ndarray
    sigma: np.ndarray
    amplitude: np.ndarray
    specimen: callable
    peak: float

    def truth(self, raster):
        """The specimen on an output raster, in counts."""
        rows, cols = raster.shape
        gy, gx = np.mgrid[0:rows, 0:cols].astype(float)
        step = np.asarray(raster.step, dtype=float)
        x = raster.origin[0] + step[0, 0] * gx + step[0, 1] * gy
        y = raster.origin[1] + step[1, 0] * gx + step[1, 1] * gy
        return self.peak * self.specimen(x, y)


def make_scan(
    shape=(96, 96),
    a1=(11.0, 0.0),
    a2=(0.0, 11.0),
    offset=(4.2, 5.7),
    steps=(22, 22),
    step=None,
    sigma=2.0,
    peak=100.0,
    background=20.0,
    noise="none",
    read_noise=0.0,
    centre_shift=None,
    sigma_field=None,
    kmax_per_step=0.3,
    orientation="+x+y",
    frame_gain=None,
    static=None,
    seed=0,
):
    """Render one scan of the unit cell.

    Args:
        step: 2x2 scan step vectors as columns; the lattice divided by
            ``steps`` along the camera axes when ``None`` (which needs an
            axis-aligned lattice).
        noise: ``"none"``, ``"poisson"`` or ``"gaussian"`` (``read_noise`` sd).
        centre_shift: ``f(x, y) -> (dx, dy)``, a distortion of the foci.
        sigma_field: ``f(x, y) -> sigma``, a width that varies over the frame.
        kmax_per_step: Highest spatial frequency of the specimen, in cycles
            per scan step.
        orientation: Fast axis and directions of the scan, the fast axis
            first: ``"-x+y"`` scans x backwards within a line and y forwards
            from line to line; ``steps`` is then (fast, slow).
        frame_gain: ``(frames,)`` factor on the amplitudes of every frame.
        static: ``(rows, cols)`` image added to every frame: what does not
            move with the scan.
    """
    rng = np.random.default_rng(seed)
    a1, a2 = np.asarray(a1, float), np.asarray(a2, float)
    if step is None:
        step = np.array([[a1[0] / steps[0], 0.0], [0.0, a2[1] / steps[1]]])
    step = np.asarray(step, dtype=float).reshape(2, 2)
    points = lattice_points(a1, a2, offset, shape, margin=4.0 * sigma)
    x, y = points[:, 0].copy(), points[:, 1].copy()
    if centre_shift is not None:
        dx, dy = centre_shift(x, y)
        x, y = x + dx, y + dy
    widths = np.full(x.shape, float(sigma))
    if sigma_field is not None:
        widths = np.asarray(sigma_field(x, y), dtype=float)

    sign = {"+": 1, "-": -1}
    fast_sign, slow_sign = sign[orientation[0]], sign[orientation[2]]
    pairs = [
        (fast_sign * i, slow_sign * j) for j in range(steps[1]) for i in range(steps[0])
    ]
    if orientation[1] == "y":
        pairs = [(slow, fast) for fast, slow in pairs]
    scan_index = np.array(pairs, dtype=int)
    offsets = scan_index @ step.T
    pitch = float(np.sqrt(abs(np.linalg.det(step))))
    specimen = bandlimited_field(kmax=kmax_per_step / pitch, seed=seed + 11)

    qx = x[None, :] - offsets[:, :1]
    qy = y[None, :] - offsets[:, 1:]
    amplitude = peak * specimen(qx, qy)
    if frame_gain is not None:
        amplitude = amplitude * np.asarray(frame_gain, dtype=float)[:, None]
    frames = np.empty((scan_index.shape[0], *shape))
    for k in range(scan_index.shape[0]):
        frames[k] = render_spots(shape, x, y, widths, amplitude[k]) + background
    if static is not None:
        frames = frames + np.asarray(static, dtype=float)[None]
    if noise == "poisson":
        frames = rng.poisson(frames).astype(float)
        if read_noise:
            frames += rng.normal(0.0, read_noise, frames.shape)
    elif noise == "gaussian":
        frames = frames + rng.normal(0.0, read_noise, frames.shape)
    elif noise != "none":
        raise ValueError(f"Unknown noise model {noise!r}")
    return SyntheticScan(
        frames=frames,
        scan_index=scan_index,
        step=step,
        focus_x=x,
        focus_y=y,
        sigma=widths,
        amplitude=amplitude,
        specimen=specimen,
        peak=peak,
    )


@dataclass
class PhysicalScan:
    """A scan rendered from the specimen, on a periodic frame."""

    frames: np.ndarray
    scan_index: np.ndarray
    step: np.ndarray
    focus_x: np.ndarray
    focus_y: np.ndarray
    specimen: np.ndarray
    fine_per_pixel: int
    sigma_e: float
    sigma_d: float

    @property
    def alpha(self) -> float:
        return self.sigma_e**2 / (self.sigma_e**2 + self.sigma_d**2)

    @property
    def spot_sigma(self) -> float:
        return float(np.hypot(self.sigma_e, self.sigma_d))

    def truth(self, raster, blur_sigma=0.0):
        """The specimen at the pixels of an output raster of the scan's step.

        ``blur_sigma`` is in camera pixels.
        """
        image = self.specimen
        if blur_sigma > 0:
            n = image.shape[0]
            k = np.fft.fftfreq(n)
            kernel = np.exp(-2 * (np.pi * blur_sigma * self.fine_per_pixel) ** 2
                            * (k[:, None] ** 2 + k[None, :] ** 2))
            image = np.real(np.fft.ifft2(np.fft.fft2(image) * kernel))
        rows, cols = raster.shape
        gy, gx = np.mgrid[0:rows, 0:cols]
        step = np.asarray(raster.step, dtype=float)
        x = raster.origin[0] + step[0, 0] * gx + step[0, 1] * gy
        y = raster.origin[1] + step[1, 0] * gx + step[1, 1] * gy
        ix = np.rint(x * self.fine_per_pixel).astype(int) % image.shape[1]
        iy = np.rint(y * self.fine_per_pixel).astype(int) % image.shape[0]
        return image[iy, ix]


def _periodic_gaussian(n, sigma):
    d = np.minimum(np.arange(n), n - np.arange(n)).astype(float)
    return np.exp(-(d[:, None] ** 2 + d[None, :] ** 2) / (2.0 * sigma * sigma))


def make_physical_scan(
    sigma_e=1.2,
    sigma_d=1.5,
    period=11,
    steps=22,
    cells=6,
    peak=300.0,
    background=20.0,
    specimen="filaments",
    noise="none",
    seed=0,
):
    """Image a specimen through a scanned lattice of foci.

    The frame is periodic (``cells`` periods wide), so nothing is lost at its
    edges. ``sigma_e`` and ``sigma_d`` are the widths of the focus and of the
    detection PSF in camera pixels; ``period`` is in camera pixels and
    ``steps`` must be a multiple of it.

    Args:
        specimen: ``"filaments"`` (random curved lines), ``"points"`` (well
            separated emitters, one per region) or a 2D array on the fine
            grid.
    """
    rng = np.random.default_rng(seed)
    fine = steps // period
    if fine * period != steps:
        raise ValueError("steps must be a multiple of the period")
    n = cells * period * fine
    if isinstance(specimen, str):
        sample = np.zeros((n, n))
        if specimen == "points":
            spacing = 18
            for cy in range(spacing // 2, n - spacing // 2, spacing):
                for cx in range(spacing // 2, n - spacing // 2, spacing):
                    jitter = rng.integers(-3, 4, size=2)
                    sample[cy + jitter[0], cx + jitter[1]] = 40.0
        elif specimen == "filaments":
            t = np.linspace(0.0, 1.0, 4 * n)
            for _ in range(14):
                start = rng.uniform(0, n, 2)
                angle = rng.uniform(0, 2 * np.pi)
                bend = rng.uniform(-3.0, 3.0)
                length = rng.uniform(0.5, 1.2) * n
                theta = angle + bend * t
                x = start[0] + length * np.cumsum(np.cos(theta)) / t.size
                y = start[1] + length * np.cumsum(np.sin(theta)) / t.size
                sample[np.rint(y).astype(int) % n, np.rint(x).astype(int) % n] = 4.0
        else:
            raise ValueError(f"Unknown specimen {specimen!r}")
    else:
        sample = np.asarray(specimen, dtype=float)
        if sample.shape != (n, n):
            raise ValueError(f"The specimen must have shape {(n, n)}")

    focus_fine = np.array(
        [(7 + period * fine * m, 9 + period * fine * k)
         for k in range(cells) for m in range(cells)]
    )
    single = _periodic_gaussian(n, sigma_e * fine)
    excitation = np.zeros((n, n))
    for fx, fy in focus_fine:
        excitation += np.roll(np.roll(single, fy, axis=0), fx, axis=1)
    detection = np.fft.fft2(_periodic_gaussian(n, sigma_d * fine))
    spot = np.real(np.fft.ifft2(np.fft.fft2(single) * detection))
    scale = peak / spot.max()

    scan_index = np.array(
        [(i, j) for j in range(steps) for i in range(steps)], dtype=int
    )
    frames = np.empty((scan_index.shape[0], n // fine, n // fine))
    for k, (i, j) in enumerate(scan_index):
        moved = np.roll(np.roll(sample, j, axis=0), i, axis=1)
        image = np.real(np.fft.ifft2(np.fft.fft2(moved * excitation) * detection))
        frames[k] = scale * image[::fine, ::fine] + background
    if noise == "poisson":
        frames = rng.poisson(np.maximum(frames, 0.0)).astype(float)
    elif noise != "none":
        raise ValueError(f"Unknown noise model {noise!r}")
    return PhysicalScan(
        frames=frames,
        scan_index=scan_index,
        step=np.diag([1.0 / fine, 1.0 / fine]),
        focus_x=focus_fine[:, 0] / fine,
        focus_y=focus_fine[:, 1] / fine,
        specimen=sample,
        fine_per_pixel=fine,
        sigma_e=float(sigma_e),
        sigma_d=float(sigma_d),
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
