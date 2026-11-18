"""Adaptive optics with a deformable mirror: calibration against a
Shack-Hartmann sensor, closed-loop correction, and sensorless correction
from an image metric.

Ported from the polarization rig's Microscope_GUI (``miao/processes/
process_shwfs.generate_influence_matrices``, ``miao/tools/tool_improc``,
``controller_main``'s AO procedures) and reworked:

* the influence matrix is the slopes per unit actuator push, from a push
  and a pull of each actuator (as the old GUI measured it); the control
  matrix its pseudo-inverse truncated to ``n_singular`` singular values
  (the old GUI used 14), recomputed for the lenslets valid in a frame;
* ``mode_commands`` turns the calibration into a mode matrix -- the command
  for 1 µm RMS of each Zernike mode -- which a mirror's ``modesFile``
  takes, so ``setMirrorModes`` speaks Zernike µm;
* the closed loop is a leaky integrator (the old GUI's LQG controller needed
  a calibration file nothing wrote);
* the sensorless metrics are the old GUI's (maximum, sum, masked sum,
  high-pass and band ratio of the image spectrum) with its NA, wavelength
  and pixel size as parameters instead of constants, and the peak of a
  mode scan is the vertex of a fitted parabola, refused when there is no
  maximum inside the scanned range.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

import numpy as np


# --------------------------------------------------------------------------- #
# Linear algebra
# --------------------------------------------------------------------------- #
def truncated_pinv(matrix: np.ndarray, n_singular: Optional[int] = None,
                   rcond: float = 1e-6) -> np.ndarray:
    """The pseudo-inverse of ``matrix`` from its ``n_singular`` largest
    singular values (all above ``rcond`` x the largest when None)."""
    u, s, vt = np.linalg.svd(np.asarray(matrix, dtype=np.float64), full_matrices=False)
    keep = s > rcond * (s[0] if s.size else 0.0)
    if n_singular is not None:
        keep &= np.arange(s.size) < int(n_singular)
    inverse = np.zeros_like(s)
    inverse[keep] = 1.0 / s[keep]
    return (vt.T * inverse) @ u.T


# --------------------------------------------------------------------------- #
# Calibration
# --------------------------------------------------------------------------- #
@dataclass
class MirrorCalibration:
    """A mirror measured with a sensor: ``influence`` (2N, A) slopes per
    unit push of each actuator (NaN rows: lenslets not seen), the
    ``amplitude`` pushed, the singular values kept, and the mode matrix
    (``modes`` and ``mode_commands`` (M, A), the command per µm RMS)."""

    influence: np.ndarray
    amplitude: float
    n_singular: int
    modes: Optional[list] = None
    mode_commands: Optional[np.ndarray] = None

    @property
    def actuators(self) -> int:
        return int(self.influence.shape[1])

    def usable_rows(self, valid_rows: Optional[np.ndarray] = None) -> np.ndarray:
        rows = np.all(np.isfinite(self.influence), axis=1)
        if valid_rows is not None:
            rows &= np.asarray(valid_rows, dtype=bool)
        return rows

    def control_matrix(self, valid_rows: Optional[np.ndarray] = None) -> np.ndarray:
        """(A, 2N): actuator commands per slope, zero on the rows not used."""
        rows = self.usable_rows(valid_rows)
        control = np.zeros((self.actuators, self.influence.shape[0]))
        control[:, rows] = truncated_pinv(self.influence[rows], self.n_singular)
        return control

    def singular_values(self) -> np.ndarray:
        return np.linalg.svd(self.influence[self.usable_rows()], compute_uv=False)

    def save(self, path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        extra = {}
        if self.modes is not None:
            extra = dict(modes=np.asarray(self.modes, dtype=np.int64),
                         mode_commands=self.mode_commands)
        np.savez_compressed(path, kind=np.asarray('mirror_calibration'),
                            influence=self.influence, amplitude=self.amplitude,
                            n_singular=self.n_singular, **extra)
        return path

    @classmethod
    def load(cls, path) -> "MirrorCalibration":
        with np.load(path, allow_pickle=False) as d:
            modes = [int(j) for j in d['modes']] if 'modes' in d.files else None
            return cls(influence=d['influence'], amplitude=float(d['amplitude']),
                       n_singular=int(d['n_singular']), modes=modes,
                       mode_commands=d['mode_commands'] if 'mode_commands' in d.files else None)


def influence_column(plus: np.ndarray, minus: np.ndarray, amplitude: float,
                     valid_plus=None, valid_minus=None) -> np.ndarray:
    """One actuator's column: ``(slopes at +a - slopes at -a) / 2a``, NaN
    where either reading did not see the lenslet."""
    column = (np.asarray(plus, float) - np.asarray(minus, float)) / (2.0 * float(amplitude))
    for valid in (valid_plus, valid_minus):
        if valid is not None:
            column = np.where(np.asarray(valid, dtype=bool), column, np.nan)
    return column


def mode_commands(calibration: MirrorCalibration, mode_slopes: np.ndarray) -> np.ndarray:
    """(M, A): the least-squares command for 1 µm RMS of each mode, from
    the modes' slopes ``(2N, M)`` (``shack_hartmann.zernike_slope_matrix``)."""
    rows = calibration.usable_rows()
    pinv = truncated_pinv(calibration.influence[rows], calibration.n_singular)
    return (pinv @ np.asarray(mode_slopes, float)[rows]).T


# --------------------------------------------------------------------------- #
# Closed loop
# --------------------------------------------------------------------------- #
def integrator_step(command: np.ndarray, slopes: np.ndarray, control: np.ndarray, *,
                    gain: float = 0.3, leak: float = 0.0,
                    flat: Optional[np.ndarray] = None) -> np.ndarray:
    """The next command: ``flat + (1 - leak)(command - flat) - gain C s``.
    ``slopes`` are against the reference (the wavefront to reach)."""
    command = np.asarray(command, dtype=np.float64)
    flat = np.zeros_like(command) if flat is None else np.asarray(flat, dtype=np.float64)
    correction = np.asarray(control, float) @ np.nan_to_num(np.asarray(slopes, float))
    return flat + (1.0 - float(leak)) * (command - flat) - float(gain) * correction


def rms(values) -> float:
    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values)]
    return float(np.sqrt(np.mean(values ** 2))) if values.size else float('nan')


# --------------------------------------------------------------------------- #
# Sensorless: image metrics and the mode-scan peak
# --------------------------------------------------------------------------- #
METRICS = ('max', 'sum', 'masked', 'highpass', 'snr', 'band')


def _cutoff_px(shape, na: float, wavelength_um: float, pixel_um: float) -> float:
    """The old GUI's spectrum radius: ``NA / wavelength`` in frequency
    pixels of an image of ``shape``."""
    n = min(shape)
    return float(na) / float(wavelength_um) * n * float(pixel_um)


def _radius_map(shape) -> np.ndarray:
    ny, nx = shape
    fy = np.fft.fftshift(np.fft.fftfreq(ny)) * ny
    fx = np.fft.fftshift(np.fft.fftfreq(nx)) * nx
    return np.hypot(*np.meshgrid(fy, fx, indexing='ij'))


def image_metric(image: np.ndarray, kind: str = 'highpass', *, mask=None,
                 na: float = 1.4, wavelength_um: float = 0.5, pixel_um: float = 0.081,
                 low: float = 0.1, high: float = 0.6, band=(0.3, 0.6)) -> float:
    """An image's sharpness or brightness, larger is better.

    ``max`` and ``sum`` of the intensities, ``masked``: the sum under
    ``mask``. The spectrum metrics weigh ``|FFT|`` inside 0.9 of the
    spectrum radius ``NA / wavelength`` (the old GUI's ``na``, ``wl``,
    ``dx``): ``highpass`` the share beyond ``high`` x the radius (Gaussian
    edge), ``snr`` that share over the share within ``low`` x the radius,
    ``band`` the share between ``band[0]`` and ``band[1]`` x the radius."""
    image = np.asarray(image, dtype=np.float64)
    if kind == 'max':
        return float(image.max())
    if kind == 'sum':
        return float(image.sum())
    if kind == 'masked':
        if mask is None:
            raise ValueError("the masked metric needs a mask")
        return float((image * np.asarray(mask, dtype=np.float64)).sum())
    if kind not in METRICS:
        raise ValueError(f"metric is one of {', '.join(METRICS)}, not {kind!r}")
    radius = _cutoff_px(image.shape, na, wavelength_um, pixel_um)
    r = _radius_map(image.shape)
    spectrum = np.abs(np.fft.fftshift(np.fft.fft2(image - image.mean())))
    support = r <= 0.9 * radius
    total = float((spectrum * support).sum())
    if total <= 0:
        return 0.0
    if kind == 'highpass':
        weight = (1.0 - np.exp(-0.5 * (r / (high * radius)) ** 2)) * support
        return float((spectrum * weight).sum()) / total
    if kind == 'snr':
        hp = (1.0 - np.exp(-0.5 * (r / (high * radius)) ** 2)) * support
        lp = np.exp(-0.5 * (r / (low * radius)) ** 2) * support
        return float((spectrum * hp).sum()) / max(float((spectrum * lp).sum()), 1e-300)
    inner, outer = band
    weight = (r >= inner * radius) & (r <= outer * radius) & support
    return float((spectrum * weight).sum()) / total


@dataclass
class PeakFit:
    """A parabola fitted to a mode scan: its vertex ``peak`` when it is a
    maximum inside the scanned range, else ``peak`` is None and ``reason``
    says why."""

    peak: Optional[float]
    coefficients: tuple
    reason: str = ''


def quadratic_peak(amplitudes: Sequence[float], values: Sequence[float],
                   errors: Optional[Sequence[float]] = None) -> PeakFit:
    """The old GUI's ``peak_find``: a parabola through the metric values of
    a mode scan (weighted by ``errors`` when given)."""
    x = np.asarray(amplitudes, dtype=np.float64)
    y = np.asarray(values, dtype=np.float64)
    if x.size < 3:
        return PeakFit(None, (), 'a parabola needs three amplitudes')
    weights = None if errors is None else 1.0 / np.maximum(np.asarray(errors, float), 1e-300)
    a, b, c = np.polyfit(x, y, 2, w=weights)
    if not a < 0:
        return PeakFit(None, (a, b, c), 'no maximum (the parabola opens upwards)')
    vertex = -b / (2 * a)
    if vertex >= x.max():
        return PeakFit(None, (a, b, c), f'the maximum ({vertex:.4g}) is above the scanned range')
    if vertex <= x.min():
        return PeakFit(None, (a, b, c), f'the maximum ({vertex:.4g}) is below the scanned range')
    return PeakFit(float(vertex), (a, b, c))


# --------------------------------------------------------------------------- #
# Simulation (tests, the mock mirror and wavefront sensor)
# --------------------------------------------------------------------------- #
def actuator_layout(count: int) -> np.ndarray:
    """(count, 2) actuator positions ``(x, y)`` in pupil units: the
    smallest square grid clipped to a disk that holds ``count`` (ALPAO's
    DM69 and DM97 are such grids: 9 x 9 and 11 x 11 without corners),
    fewest-from-the-centre first when the disk holds more."""
    side = 1
    while True:
        coords = np.arange(side) - (side - 1) / 2
        xs, ys = np.meshgrid(coords, coords)
        points = np.stack([xs.ravel(), ys.ravel()], axis=1)
        radius = np.hypot(*points.T)
        if (radius <= (side - 1) / 2 + 0.5).sum() >= count:
            order = np.lexsort((points[:, 0], points[:, 1], radius))[:count]
            chosen = points[np.sort(order)]
            return chosen / ((side - 1) / 2 if side > 1 else 1.0)
        side += 1


@dataclass
class SimulatedMirror:
    """A mirror whose surface is a sum of Gaussian influence functions
    (``coupling``: an actuator's push at its neighbour) over a static
    aberration; ``wavefront(x, y, command)`` in µm. The outermost
    actuators sit at ``extent`` pupil radii: beyond the pupil, as on the
    rig's DM69 (9 actuators 1.5 mm apart across a 10.5 mm pupil)."""

    count: int
    stroke_um: float = 1.0
    coupling: float = 0.3
    extent: float = 1.3
    aberration: Optional[dict] = None          # {Noll mode: µm RMS}

    def __post_init__(self):
        self.positions = actuator_layout(self.count) * float(self.extent)
        spacing = np.min([np.min(np.hypot(*(self.positions[np.arange(self.count) != i]
                                              - self.positions[i]).T))
                          for i in range(min(self.count, 8))]) if self.count > 1 else 1.0
        self.sigma = spacing / np.sqrt(-2.0 * np.log(self.coupling))

    def surface(self, x, y, command) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64)[..., None]
        y = np.asarray(y, dtype=np.float64)[..., None]
        px, py = self.positions[:, 0], self.positions[:, 1]
        g = np.exp(-((x - px) ** 2 + (y - py) ** 2) / (2 * self.sigma ** 2))
        return self.stroke_um * (g @ np.asarray(command, dtype=np.float64))

    def wavefront(self, x, y, command) -> np.ndarray:
        from .zernike import wavefront as zernike_wavefront
        total = 2.0 * self.surface(x, y, command)            # reflection doubles it
        if self.aberration:
            total = total + zernike_wavefront(self.aberration, x, y)
        return total

    def mean_slopes(self, x_centres, y_centres, half_width: float, command,
                    samples: int = 4) -> np.ndarray:
        """(N, 2) the mean ``(dW/dx, dW/dy)`` over square lenslets."""
        t = (np.arange(samples) + 0.5) / samples * 2 - 1
        xc = np.asarray(x_centres, float)[:, None]
        yc = np.asarray(y_centres, float)[:, None]
        h = float(half_width)
        right = self.wavefront(xc + h, yc + h * t, command).mean(axis=1)
        left = self.wavefront(xc - h, yc + h * t, command).mean(axis=1)
        bottom = self.wavefront(xc + h * t, yc + h, command).mean(axis=1)
        top = self.wavefront(xc + h * t, yc - h, command).mean(axis=1)
        return np.stack([(right - left) / (2 * h), (bottom - top) / (2 * h)], axis=1)
