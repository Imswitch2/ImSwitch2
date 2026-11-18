"""Shack-Hartmann wavefront sensing: from a camera frame of lenslet spots to
wavefront slopes, Zernike coefficients and a wavefront map.

Ported from the polarization rig's Microscope_GUI
(``miao/processes/process_shwfs.py``) and reworked:

* the lenslet grid is found in a reference frame (:func:`find_grid`), or
  typed in as the old GUI's parameters were (:func:`regular_grid`: centre,
  lenslet count, spacing, half window);
* a spot's shift is its thresholded centroid against the reference's, or
  the cross-correlation of its window with the reference's window and a
  parabolic peak -- the old GUI's two methods, with its background rule
  (``background x`` an Otsu threshold of the whole frame, subtracted);
* slopes are in µm of wavefront per unit of normalised pupil radius, from
  the camera's pixel size and the lenslets' focal length (the old GUI had
  ``pixel / 5.2 mm x 150 µm`` typed into the code);
* the wavefront is a least-squares Zernike fit to the slopes
  (:func:`zernike_slope_matrix`, exact lenslet-mean gradients) and, for a
  picture, a Southwell least-squares zonal map on the lenslet lattice
  (:func:`zonal_wavefront`) -- instead of the Fourier (Hudgins) method,
  which needs a full rectangle of lenslets.

Coordinates: positions are ``(y, x)`` in pixels (row, column); pupil
coordinates are ``x`` right, ``y`` down, in units of the pupil radius --
the convention of :mod:`.zernike`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from . import zernike as _z


# --------------------------------------------------------------------------- #
# The lenslet grid
# --------------------------------------------------------------------------- #
@dataclass
class LensletGrid:
    """Where the lenslets are on the camera.

    ``lattice`` is each lenslet's centre on the regular lattice and
    ``indices`` its (row, column) on it; ``window`` is the half side of the
    square each lenslet's spot is looked for in (the old GUI's ``hsp``).
    The pupil is a circle (``pupil_center``, ``pupil_radius``) in pixels.
    """

    lattice: np.ndarray                  # (N, 2) y, x
    indices: np.ndarray                  # (N, 2) int row, column
    pitch: tuple                         # (pitch_y, pitch_x) in pixels
    window: int
    pupil_center: tuple                  # (y, x)
    pupil_radius: float

    def __post_init__(self):
        self.lattice = np.asarray(self.lattice, dtype=np.float64).reshape(-1, 2)
        self.indices = np.asarray(self.indices, dtype=np.int64).reshape(-1, 2)
        self.pitch = (float(self.pitch[0]), float(self.pitch[1]))
        self.window = int(self.window)
        self.pupil_center = (float(self.pupil_center[0]), float(self.pupil_center[1]))
        self.pupil_radius = float(self.pupil_radius)
        if len(self.lattice) != len(self.indices):
            raise ValueError("lattice and indices differ in length")
        if self.window < 2:
            raise ValueError("a lenslet window needs at least 2 pixels each way")

    @property
    def count(self) -> int:
        return len(self.lattice)

    def pupil_xy(self) -> tuple[np.ndarray, np.ndarray]:
        """Lenslet centres in pupil units: ``(x, y)``."""
        cy, cx = self.pupil_center
        return ((self.lattice[:, 1] - cx) / self.pupil_radius,
                (self.lattice[:, 0] - cy) / self.pupil_radius)

    def half_width(self) -> float:
        """Half a lenslet's side in pupil units."""
        return 0.5 * float(np.mean(self.pitch)) / self.pupil_radius

    def to_dict(self) -> dict:
        return dict(lattice=self.lattice, indices=self.indices, pitch=np.asarray(self.pitch),
                    window=np.asarray(self.window), pupil_center=np.asarray(self.pupil_center),
                    pupil_radius=np.asarray(self.pupil_radius))

    @classmethod
    def from_dict(cls, d) -> "LensletGrid":
        return cls(lattice=d['lattice'], indices=d['indices'], pitch=tuple(np.ravel(d['pitch'])),
                   window=int(np.ravel(d['window'])[0]),
                   pupil_center=tuple(np.ravel(d['pupil_center'])),
                   pupil_radius=float(np.ravel(d['pupil_radius'])[0]))


def regular_grid(center_yx, pitch_px: float, rows: int, columns: int, *,
                 window: Optional[int] = None,
                 pupil_radius: Optional[float] = None) -> LensletGrid:
    """A ``rows x columns`` lattice centred on ``center_yx`` -- the old GUI's
    ``y_center_base``, ``x_center_base``, ``n_lenslets_y``, ``n_lenslets_x``,
    ``lenslet_spacing`` and ``hsp``. Without ``pupil_radius`` every lenslet
    is kept; with it, those whose centre is inside the circle."""
    cy, cx = float(center_yx[0]), float(center_yx[1])
    pitch = float(pitch_px)
    rows, columns = int(rows), int(columns)
    # The old GUI put lenslet (rows // 2, columns // 2) on the centre.
    r, c = np.meshgrid(np.arange(rows), np.arange(columns), indexing='ij')
    indices = np.stack([r.ravel(), c.ravel()], axis=1)
    lattice = np.stack([cy + (indices[:, 0] - rows // 2) * pitch,
                        cx + (indices[:, 1] - columns // 2) * pitch], axis=1)
    centre = lattice.mean(axis=0)
    if pupil_radius is None:
        radius = float(np.max(np.hypot(*(lattice - centre).T))) + pitch / 2
        keep = np.ones(len(lattice), dtype=bool)
    else:
        radius = float(pupil_radius)
        keep = np.hypot(*(lattice - centre).T) <= radius
    return LensletGrid(lattice=lattice[keep], indices=indices[keep], pitch=(pitch, pitch),
                       window=int(window if window is not None else max(2, int(pitch // 3))),
                       pupil_center=tuple(centre), pupil_radius=radius)


def find_grid(reference: np.ndarray, pitch_px: float, *, window: Optional[int] = None,
              min_brightness: float = 0.2, background: float = 0.1,
              pupil_radius: Optional[float] = None) -> LensletGrid:
    """The lenslet grid found in a reference frame (a flat wavefront).

    Spots are local maxima of the smoothed frame, brighter than
    ``min_brightness`` times the brightest, at least half a pitch apart,
    refined by centroid. Each gets the lattice index nearest to it, counted
    from the spot nearest the frame's bright centre, and the lattice's
    origin and pitch are fitted to all of them. ``pitch_px`` is the
    lenslet spacing on the camera, roughly (lenslet pitch / pixel size).
    """
    from scipy import ndimage
    image = np.asarray(reference, dtype=np.float64)
    if image.ndim != 2:
        raise ValueError("a reference frame is a 2-D image")
    pitch = float(pitch_px)
    if not pitch >= 4:
        raise ValueError("the lenslet pitch must be at least 4 pixels")
    smooth = ndimage.gaussian_filter(image - np.median(image), sigma=max(pitch / 10, 0.8))
    size = max(3, int(pitch * 0.6) | 1)
    peaks = (smooth == ndimage.maximum_filter(smooth, size=size)) & (
        smooth > min_brightness * smooth.max())
    ys, xs = np.nonzero(peaks)
    if len(ys) < 3:
        raise ValueError(f"found {len(ys)} spots; check the pitch ({pitch:g} px) and "
                         "that the frame shows the lenslet spots")
    win = int(window if window is not None else max(2, int(pitch // 3)))
    points = np.stack([ys, xs], axis=1).astype(np.float64)
    points = _centroids(image, points, win, background=background)[0]
    points = points[np.all(np.isfinite(points), axis=1)]
    # The anchor: the spot nearest the brightness-weighted centre.
    weights = np.clip(smooth, 0, None)
    total = weights.sum()
    centre = np.array([(weights.sum(axis=1) @ np.arange(image.shape[0])) / total,
                       (weights.sum(axis=0) @ np.arange(image.shape[1])) / total])
    anchor = points[np.argmin(np.hypot(*(points - centre).T))]
    indices = np.rint((points - anchor) / pitch).astype(np.int64)
    # Fit origin and pitch per axis, then index again with the fitted lattice.
    pitch_yx, origin = [], []
    for axis in (0, 1):
        a = np.stack([np.ones(len(points)), indices[:, axis]], axis=1)
        (o, p), *_ = np.linalg.lstsq(a, points[:, axis], rcond=None)
        origin.append(o)
        pitch_yx.append(p)
    origin, pitch_yx = np.array(origin), np.array(pitch_yx)
    indices = np.rint((points - origin) / pitch_yx).astype(np.int64)
    # One spot per lattice site: the one nearest the site.
    lattice = origin + indices * pitch_yx
    distance = np.hypot(*(points - lattice).T)
    order = np.argsort(distance)
    seen, keep = set(), []
    for i in order:
        key = (int(indices[i, 0]), int(indices[i, 1]))
        if key in seen or distance[i] > 0.35 * pitch:
            continue
        seen.add(key)
        keep.append(i)
    keep = np.array(sorted(keep, key=lambda i: (indices[i, 0], indices[i, 1])))
    indices = indices[keep] - indices[keep].min(axis=0)
    lattice = lattice[keep]
    pupil_centre = lattice.mean(axis=0)
    if pupil_radius is None:
        radius = float(np.max(np.hypot(*(lattice - pupil_centre).T))) + float(pitch_yx.mean()) / 2
    else:
        radius = float(pupil_radius)
        inside = np.hypot(*(lattice - pupil_centre).T) <= radius
        lattice, indices = lattice[inside], indices[inside]
    return LensletGrid(lattice=lattice, indices=indices, pitch=tuple(pitch_yx), window=win,
                       pupil_center=tuple(pupil_centre), pupil_radius=radius)


# --------------------------------------------------------------------------- #
# Spots
# --------------------------------------------------------------------------- #
def otsu_threshold(image: np.ndarray, bins: int = 256) -> float:
    """Otsu's threshold of ``image`` (numpy only)."""
    values = np.asarray(image, dtype=np.float64).ravel()
    low, high = float(values.min()), float(values.max())
    if high <= low:
        return low
    hist, edges = np.histogram(values, bins=bins, range=(low, high))
    centres = (edges[:-1] + edges[1:]) / 2
    w0 = np.cumsum(hist)
    w1 = w0[-1] - w0
    s0 = np.cumsum(hist * centres)
    m0 = np.divide(s0, w0, out=np.zeros_like(s0), where=w0 > 0)
    m1 = np.divide(s0[-1] - s0, w1, out=np.zeros_like(s0), where=w1 > 0)
    between = w0 * w1 * (m0 - m1) ** 2
    return float(centres[int(np.argmax(between))])


def subtract_background(image: np.ndarray, background: float) -> np.ndarray:
    """The old GUI's rule on a frame less its dark level: the median (spots
    cover little of a frame) is subtracted, then ``background x`` Otsu's
    threshold; what is below it is zero. ``background = 0`` only clips at
    the dark level."""
    image = np.asarray(image, dtype=np.float64)
    image = image - float(np.median(image))
    if background <= 0:
        return np.clip(image, 0.0, None)
    threshold = max(float(background) * otsu_threshold(image), 0.0)
    return np.where(image > threshold, image - threshold, 0.0)


def _windows(image: np.ndarray, centres: np.ndarray, win: int):
    """``(N, 2 win, 2 win)`` windows around the rounded centres, and which
    of them lie wholly inside the frame (the others are zero)."""
    ny, nx = image.shape
    top = np.rint(centres[:, 0]).astype(np.int64) - win
    left = np.rint(centres[:, 1]).astype(np.int64) - win
    inside = (top >= 0) & (left >= 0) & (top + 2 * win <= ny) & (left + 2 * win <= nx)
    offsets = np.arange(2 * win)
    rows = np.clip(top[:, None] + offsets[None, :], 0, ny - 1)
    cols = np.clip(left[:, None] + offsets[None, :], 0, nx - 1)
    stack = image[rows[:, :, None], cols[:, None, :]]
    stack[~inside] = 0.0
    return stack, top, left, inside


def _centroids(image, centres, win, background):
    work = subtract_background(image, background)
    stack, top, left, inside = _windows(work, np.asarray(centres, dtype=np.float64), win)
    total = stack.sum(axis=(1, 2))
    offsets = np.arange(2 * win, dtype=np.float64)
    with np.errstate(invalid='ignore', divide='ignore'):
        cy = (stack.sum(axis=2) @ offsets) / total
        cx = (stack.sum(axis=1) @ offsets) / total
    positions = np.stack([top + cy, left + cx], axis=1)
    positions[~inside | ~(total > 0)] = np.nan
    return positions, total


def spot_positions(image: np.ndarray, grid: LensletGrid, *, around=None,
                   background: float = 0.1) -> tuple[np.ndarray, np.ndarray]:
    """Each lenslet's spot position (thresholded centroid in its window
    around ``around``, the lattice by default) and brightness. A spot whose
    window leaves the frame or holds nothing is NaN."""
    centres = grid.lattice if around is None else np.asarray(around, dtype=np.float64)
    return _centroids(image, centres, grid.window, background)


def _parabolic(c: np.ndarray, index: int) -> float:
    if 0 < index < len(c) - 1:
        denominator = c[index - 1] + c[index + 1] - 2 * c[index]
        if denominator != 0:
            return index + 0.5 * (c[index - 1] - c[index + 1]) / denominator
    return float(index)


def correlation_shifts(reference: np.ndarray, image: np.ndarray, grid: LensletGrid, *,
                       around=None, background: float = 0.1) -> np.ndarray:
    """Each spot's shift against the reference's: the peak of the
    cross-correlation of the two windows, refined by a parabola through it
    and its neighbours (the old GUI's ``correlation`` method)."""
    centres = grid.lattice if around is None else np.asarray(around, dtype=np.float64)
    win = grid.window
    ref, _, _, inside_ref = _windows(subtract_background(reference, background), centres, win)
    img, _, _, inside = _windows(subtract_background(image, background), centres, win)
    size = 4 * win
    fr = np.fft.rfft2(ref, s=(size, size))
    fi = np.fft.rfft2(img, s=(size, size))
    corr = np.fft.irfft2(fi * np.conj(fr), s=(size, size))
    corr = np.fft.fftshift(corr, axes=(1, 2))         # zero shift at (size/2, size/2)
    shifts = np.full((len(centres), 2), np.nan)
    flat = corr.reshape(len(centres), -1).argmax(axis=1)
    peak_y, peak_x = np.unravel_index(flat, (size, size))
    for i in range(len(centres)):
        if not (inside[i] and inside_ref[i]) or not np.any(img[i]) or not np.any(ref[i]):
            continue
        y = _parabolic(corr[i, :, peak_x[i]], int(peak_y[i]))
        x = _parabolic(corr[i, peak_y[i], :], int(peak_x[i]))
        shifts[i] = (y - size // 2, x - size // 2)
    return shifts


# --------------------------------------------------------------------------- #
# The sensor
# --------------------------------------------------------------------------- #
@dataclass
class Reading:
    """One frame measured against the reference.

    ``shifts`` (N, 2) are pixels (y, x); ``slopes`` (N, 2) are
    ``(dW/dx, dW/dy)`` in µm of wavefront per unit pupil radius; ``valid``
    marks the lenslets measured in both frames; ``brightness`` is each
    spot's sum after background."""

    shifts: np.ndarray
    slopes: np.ndarray
    valid: np.ndarray
    brightness: np.ndarray

    def vector(self) -> np.ndarray:
        """``[dW/dx of every lenslet, dW/dy of every lenslet]``, 0 where not
        valid -- the order of :func:`zernike_slope_matrix`'s rows."""
        slopes = np.where(self.valid[:, None], self.slopes, 0.0)
        return np.concatenate([slopes[:, 0], slopes[:, 1]])

    def valid_rows(self) -> np.ndarray:
        return np.concatenate([self.valid, self.valid])


@dataclass
class ShackHartmannSensor:
    """A lenslet grid, the optics that scale its spot shifts, and a
    reference. ``pixel_um`` is the camera pixel at the lenslets (the
    camera pixel, for a camera right behind them), ``focal_length_um`` the
    lenslets' focal length. ``method`` is ``"centroid"`` or
    ``"correlation"``; ``background`` the old GUI's Otsu factor."""

    grid: LensletGrid
    pixel_um: float
    focal_length_um: float
    method: str = 'centroid'
    background: float = 0.1
    min_brightness: float = 0.2
    reference_positions: Optional[np.ndarray] = None
    reference_image: Optional[np.ndarray] = field(default=None, repr=False)
    reference_brightness: Optional[np.ndarray] = field(default=None, repr=False)

    def __post_init__(self):
        if self.method not in ('centroid', 'correlation'):
            raise ValueError(f'method is "centroid" or "correlation", not {self.method!r}')
        if not (self.pixel_um > 0 and self.focal_length_um > 0):
            raise ValueError("pixel_um and focal_length_um must be positive")

    @property
    def slope_per_pixel(self) -> float:
        """µm of wavefront per pupil radius for one pixel of spot shift."""
        return (self.pixel_um / self.focal_length_um) * (self.grid.pupil_radius * self.pixel_um)

    def set_reference(self, image: np.ndarray) -> np.ndarray:
        """Take ``image`` (a flat wavefront, or the wavefront to hold) as the
        reference; returns its spot positions."""
        positions, brightness = spot_positions(image, self.grid, background=self.background)
        self.reference_positions = positions
        self.reference_brightness = brightness
        self.reference_image = np.asarray(image, dtype=np.float64).copy()
        return positions

    def set_lattice_reference(self, image: np.ndarray) -> np.ndarray:
        """Take the grid's lattice as the reference positions: what a plane
        wave makes on an ideal lenslet array, so readings are absolute
        (up to the array's own errors, and the tip, tilt and defocus the
        fitted lattice absorbed). ``image`` gives the spots' brightness."""
        self.set_reference(image)
        self.reference_positions = self.grid.lattice.copy()
        return self.reference_positions

    def measure(self, image: np.ndarray) -> Reading:
        if self.reference_positions is None:
            raise RuntimeError("set a reference first (a frame of the flat wavefront)")
        ref = self.reference_positions
        valid_ref = np.all(np.isfinite(ref), axis=1)
        around = np.where(valid_ref[:, None], ref, self.grid.lattice)
        if self.method == 'centroid':
            positions, brightness = spot_positions(image, self.grid, around=around,
                                                   background=self.background)
            shifts = positions - ref
        else:
            shifts = correlation_shifts(self.reference_image, image, self.grid, around=around,
                                        background=self.background)
            _, brightness = spot_positions(image, self.grid, around=around,
                                           background=self.background)
        bright_enough = brightness >= self.min_brightness * np.nanmedian(
            np.where(valid_ref, self.reference_brightness, np.nan))
        valid = valid_ref & np.all(np.isfinite(shifts), axis=1) & bright_enough
        shifts = np.where(valid[:, None], shifts, np.nan)
        # Image rows grow downwards, as the pupil's y does: no sign flip.
        slopes = shifts[:, ::-1] * self.slope_per_pixel           # (dW/dx, dW/dy)
        return Reading(shifts=shifts, slopes=slopes, valid=valid, brightness=brightness)

    # ------------------------------------------------------------- storage
    def save(self, path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        extra = {}
        if self.reference_positions is not None:
            extra = dict(reference_positions=self.reference_positions,
                         reference_brightness=self.reference_brightness,
                         reference_image=self.reference_image)
        np.savez_compressed(path, kind=np.asarray('shack_hartmann_sensor'),
                            pixel_um=self.pixel_um, focal_length_um=self.focal_length_um,
                            method=np.asarray(self.method), background=self.background,
                            min_brightness=self.min_brightness,
                            **{f'grid_{k}': v for k, v in self.grid.to_dict().items()}, **extra)
        return path

    @classmethod
    def load(cls, path) -> "ShackHartmannSensor":
        with np.load(path, allow_pickle=False) as d:
            grid = LensletGrid.from_dict({k[5:]: d[k] for k in d.files if k.startswith('grid_')})
            sensor = cls(grid=grid, pixel_um=float(d['pixel_um']),
                         focal_length_um=float(d['focal_length_um']), method=str(d['method']),
                         background=float(d['background']),
                         min_brightness=float(d['min_brightness']))
            if 'reference_positions' in d.files:
                sensor.reference_positions = d['reference_positions']
                sensor.reference_brightness = d['reference_brightness']
                sensor.reference_image = d['reference_image']
        return sensor


# --------------------------------------------------------------------------- #
# Reconstruction
# --------------------------------------------------------------------------- #
def zernike_slope_matrix(grid: LensletGrid, modes: Sequence[int]) -> np.ndarray:
    """``(2N, M)``: the slopes (µm per pupil radius, :meth:`Reading.vector`
    order) that 1 µm RMS of each mode makes, as each lenslet averages it."""
    x, y = grid.pupil_xy()
    gx, gy = _z.mean_gradients(_z.as_mode_list(modes), x, y, grid.half_width())
    return np.concatenate([gx, gy], axis=0)


def fit_zernike(reading: Reading, grid: LensletGrid, modes: Sequence[int],
                matrix: Optional[np.ndarray] = None) -> dict:
    """``{mode: µm RMS}`` -- the least-squares fit of the modes' slopes to
    the reading's valid lenslets. Piston (mode 1) has no slope and is
    refused."""
    modes = _z.as_mode_list(modes)
    if 1 in modes:
        raise ValueError("piston (mode 1) has no slope; fit from mode 2")
    if matrix is None:
        matrix = zernike_slope_matrix(grid, modes)
    rows = reading.valid_rows()
    if rows.sum() < len(modes):
        raise ValueError(f"{int(rows.sum() // 2)} valid lenslets cannot fit {len(modes)} modes")
    coefficients, *_ = np.linalg.lstsq(matrix[rows], reading.vector()[rows], rcond=None)
    return {j: float(c) for j, c in zip(modes, coefficients)}


def zonal_wavefront(reading: Reading, grid: LensletGrid) -> np.ndarray:
    """The wavefront (µm) on the lenslet lattice, least squares from the
    slopes in Southwell's geometry, zero mean; NaN where a lenslet is not
    valid. Rows and columns are the grid's ``indices``."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.linalg import lsqr
    rows_n, cols_n = grid.indices.max(axis=0) + 1
    site = -np.ones((rows_n, cols_n), dtype=np.int64)
    valid = reading.valid
    for i in np.nonzero(valid)[0]:
        site[grid.indices[i, 0], grid.indices[i, 1]] = i
    unknown = -np.ones(grid.count, dtype=np.int64)
    unknown[valid] = np.arange(int(valid.sum()))
    # Spacing in pupil units along x and y.
    dy, dx = grid.pitch[0] / grid.pupil_radius, grid.pitch[1] / grid.pupil_radius
    r_idx, c_idx, values, rhs = [], [], [], []
    equation = 0
    for (r, c) in zip(*np.nonzero(site >= 0)):
        a = site[r, c]
        for (rr, cc, axis, step) in ((r, c + 1, 0, dx), (r + 1, c, 1, dy)):
            if rr < rows_n and cc < cols_n and site[rr, cc] >= 0:
                b = site[rr, cc]
                r_idx += [equation, equation]
                c_idx += [unknown[b], unknown[a]]
                values += [1.0, -1.0]
                rhs.append(step * 0.5 * (reading.slopes[a, axis] + reading.slopes[b, axis]))
                equation += 1
    n = int(valid.sum())
    if n == 0:
        return np.full((rows_n, cols_n), np.nan)
    # Zero mean as one more equation.
    r_idx += [equation] * n
    c_idx += list(range(n))
    values += [1.0] * n
    rhs.append(0.0)
    a = coo_matrix((values, (r_idx, c_idx)), shape=(equation + 1, n)).tocsr()
    w = lsqr(a, np.asarray(rhs), atol=1e-12, btol=1e-12)[0]
    out = np.full((rows_n, cols_n), np.nan)
    for i in np.nonzero(valid)[0]:
        out[grid.indices[i, 0], grid.indices[i, 1]] = w[unknown[i]]
    return out - np.nanmean(out)


# --------------------------------------------------------------------------- #
# Simulation (tests, the mock wavefront sensor)
# --------------------------------------------------------------------------- #
def render_spots(shape, positions, *, sigma: float = 1.5, peak: float = 1000.0,
                 brightness=None, offset: float = 100.0, noise: float = 0.0,
                 rng: Optional[np.random.Generator] = None) -> np.ndarray:
    """A frame of Gaussian spots at ``positions`` (y, x), each spot rendered
    only near itself."""
    ny, nx = int(shape[0]), int(shape[1])
    frame = np.full((ny, nx), float(offset))
    positions = np.asarray(positions, dtype=np.float64).reshape(-1, 2)
    scale = np.ones(len(positions)) if brightness is None else np.asarray(brightness, float)
    reach = int(np.ceil(4 * sigma))
    offsets = np.arange(-reach, reach + 1)
    for (y, x), s in zip(positions, scale):
        if not (np.isfinite(y) and np.isfinite(x)):
            continue
        yi, xi = int(round(y)), int(round(x))
        rows, cols = yi + offsets, xi + offsets
        rows = rows[(rows >= 0) & (rows < ny)]
        cols = cols[(cols >= 0) & (cols < nx)]
        if not len(rows) or not len(cols):
            continue
        gy = np.exp(-0.5 * ((rows - y) / sigma) ** 2)
        gx = np.exp(-0.5 * ((cols - x) / sigma) ** 2)
        frame[np.ix_(rows, cols)] += peak * s * np.outer(gy, gx)
    if noise > 0:
        rng = rng or np.random.default_rng()
        frame += rng.normal(scale=noise, size=frame.shape)
    return frame


def spots_for_slopes(grid: LensletGrid, slopes: np.ndarray, sensor_scale: float) -> np.ndarray:
    """Spot positions (y, x) for ``slopes`` (N, 2: dW/dx, dW/dy in µm per
    pupil radius), ``sensor_scale`` being :attr:`ShackHartmannSensor.slope_per_pixel`."""
    slopes = np.asarray(slopes, dtype=np.float64)
    return grid.lattice + slopes[:, ::-1] / float(sensor_scale)
