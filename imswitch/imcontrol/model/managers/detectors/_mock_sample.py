"""A synthetic fluorescent sample for simulated detectors.

With the NI-DAQ simulated, a point detector reads Poisson noise of the same
mean everywhere: an overview shows nothing to draw a region around, and an
acquisition cannot show whether it scanned the region that was drawn. With a
``mockSample`` block in the detector's ``managerProperties`` it instead counts
photons from this sample at the position the scan's own analog waveforms put
the beam at each detector sample -- so the image follows the scanners, and a
region scanned twice looks the same both times (unless photobleaching is
switched on). The MoNaLISA mock camera (``_mock_lattice.py``) images the same
sample.

The sample is a map of fluorophores in physical coordinates (µm, 60 µm
across, 25 nm texels) drawn from one of a few presets (:data:`PRESETS`):

* ``cells`` -- cells with a membrane, cytoplasm, a nucleus with its
  envelope, microtubules growing from a centrosome, stress fibres, small
  and resolvable vesicles; beads of 40, 100 and 200 nm between them.
* ``filaments`` -- microtubule asters, long stiff filaments and short
  floppy actin-like ones, crossing at every angle.
* ``beads`` -- rows of beads of known diameter (40 nm to 1 µm), pairs of
  40 nm beads at known separations, and scattered 40 nm beads for the PSF.
* ``resolution`` -- a Siemens star, line pairs and bead pairs from 600 nm
  down to 40 nm apart, and gratings of known period.

The point spread function is not part of the map: :meth:`MockSample.view`
blurs it by the excitation PSF the detector asks for, and lookups interpolate
bilinearly. Brightness is relative: 1 is about the brightest structure under
the default PSF (sigma 0.1 µm, FWHM 235 nm), :data:`BACKGROUND` is the
autofluorescence everywhere. The excitation is peak-normalised, so a point
emitter at the centre of the focus reads the same under any PSF and extended
structures read brighter under a wider one, as they do.

Every texel also carries the structure that dominates it
(:meth:`MockSample.labels`), so per-structure properties (a lifetime, a
spectrum) can be attached later without redrawing the sample.

Simulation only; the property is ignored against real hardware. Positions
come from the scanned axes' waveforms, so an axis the scan does not sweep
(parked by a separate analog write the detector never sees) reads as 0 µm.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

import numpy as np

#: Half the side of the simulated field, µm; outside it only background.
FIELD_HALF_UM = 30.0
#: Texel of the fluorophore map, µm.
TEXEL_UM = 0.025
#: Default PSF sigma, µm (FWHM ~235 nm): what :meth:`MockSample.texture` and
#: :meth:`MockSample.brightness` show.
PSF_SIGMA_UM = 0.1
#: Brightness outside any structure, relative to the brightest structure.
BACKGROUND = 0.03
#: The sample presets ``mockSample`` ``"preset"`` selects from.
PRESETS = ('cells', 'filaments', 'beads', 'resolution')
#: FWHM over sigma of a Gaussian.
FWHM_PER_SIGMA = 2.0 * math.sqrt(2.0 * math.log(2.0))

#: Blurred views kept per sample (each ~23 MB at full resolution).
_VIEW_CACHE_SIZE = 4
#: Views of a wider PSF than this many texels are blurred on a coarser grid.
_COARSE_ABOVE_TEXELS = 6.0
#: Views whose sigmas differ by less than this factor are the same view.
_SIGMA_QUANTUM = 1.02

_densities = {}
_views = OrderedDict()
_cacheLock = threading.Lock()


@dataclass(eq=False)
class _View:
    """The sample blurred by one PSF, on a grid of its own."""

    values: np.ndarray          # (n, n) float32, peak-normalised excitation
    texel_um: float
    origin_um: float            # centre of texel 0, µm
    sigma_um: float
    #: ``values`` plus the background, made once (:meth:`MockSample.texture`).
    texture: Optional[np.ndarray] = None

    def sample(self, x_um, y_um) -> np.ndarray:
        """Bilinear lookup; 0 outside the field."""
        n = self.values.shape[0]
        scale = 1.0 / self.texel_um
        c = (np.asarray(x_um, dtype=np.float64) - self.origin_um) * scale
        r = (np.asarray(y_um, dtype=np.float64) - self.origin_um) * scale
        c, r = np.broadcast_arrays(c, r)
        shape = c.shape
        c, r = c.ravel(), r.ravel()
        c0 = np.floor(c)
        r0 = np.floor(r)
        fc = c - c0
        fr = r - r0
        inside = (c0 >= 0) & (c0 < n - 1) & (r0 >= 0) & (r0 < n - 1)
        index = (r0.astype(np.intp) * n + c0.astype(np.intp))
        if not inside.all():
            index[~inside] = 0
        flat = self.values.ravel()
        top = np.take(flat, index)
        top += fc * (np.take(flat, index + 1) - top)
        bottom = np.take(flat, index + n)
        bottom += fc * (np.take(flat, index + n + 1) - bottom)
        out = top + fr * (bottom - top)
        if not inside.all():
            out[~inside] = 0.0
        return out.reshape(shape)


@dataclass(frozen=True)
class MockSample:
    """The sample, and which scanner devices move it under the optics.

    ``axes`` are ``(device, µm per volt)`` for the sample's x and y: the
    scanner waveforms are in volts, and the factor is the positioner's
    ``conversionFactor``. ``z_axis`` is the same for focus, when a z scanner
    is named (``"zAxis"``); ``focus_um`` is the z position at which the
    sample is in focus.
    """

    axes: Tuple[Tuple[str, float], ...]
    seed: int = 0
    preset: str = 'cells'
    z_axis: Optional[Tuple[str, float]] = None
    focus_um: float = 0.0

    @classmethod
    def from_property(cls, value) -> Optional['MockSample']:
        """The ``mockSample`` manager property; None when absent."""
        if value is None or value is False:
            return None
        if not isinstance(value, Mapping) or not isinstance(value.get('axes'), Mapping):
            raise ValueError(
                'mockSample needs "axes": {"<x device>": <µm per V>, "<y device>": <µm per V>}'
            )
        axes = tuple((str(device), float(factor)) for device, factor in value['axes'].items())
        if not 1 <= len(axes) <= 2 or any(factor == 0 for _, factor in axes):
            raise ValueError('mockSample "axes" names one or two devices with a non-zero µm per V')
        preset = str(value.get('preset', 'cells'))
        if preset not in PRESETS:
            raise ValueError(f'mockSample "preset" is one of {", ".join(PRESETS)}, not {preset!r}')
        z_axis = None
        zValue = value.get('zAxis')
        if zValue is not None:
            if not isinstance(zValue, Mapping) or len(zValue) != 1:
                raise ValueError('mockSample "zAxis" names one device: {"<z device>": <µm per V>}')
            (device, factor), = zValue.items()
            if float(factor) == 0:
                raise ValueError('mockSample "zAxis" needs a non-zero µm per V')
            z_axis = (str(device), float(factor))
        return cls(axes=axes, seed=int(value.get('seed', 0)), preset=preset,
                   z_axis=z_axis, focus_um=float(value.get('focusUm', 0.0)))

    # ------------------------------------------------------------------
    # The fluorophore map
    # ------------------------------------------------------------------

    def _built(self):
        key = (self.seed, self.preset)
        with _cacheLock:
            built = _densities.get(key)
            if built is None:
                built = _densities[key] = _build(self.seed, self.preset)
        return built

    def density(self) -> np.ndarray:
        """Fluorophores per texel, scaled so the default view peaks at 1."""
        return self._built()[0]

    def labels(self) -> Tuple[np.ndarray, Tuple[str, ...]]:
        """The structure dominating each texel (uint8) and the structure names.

        Label 0 (``"none"``) is no structure: only background.
        """
        _, labels, names = self._built()
        return labels, names

    def view(self, sigma_um: float) -> _View:
        """The sample under a Gaussian excitation PSF of this sigma, µm.

        Cached: a few PSFs per sample stay in memory, and sigmas within 2 %
        share a view.
        """
        # Quantised around the default PSF, which is then exactly itself.
        quantum = round(math.log(max(float(sigma_um), 1e-3) / PSF_SIGMA_UM)
                        / math.log(_SIGMA_QUANTUM))
        key = (self.seed, self.preset, quantum)
        with _cacheLock:
            view = _views.get(key)
            if view is not None:
                _views.move_to_end(key)
                return view
        density = self.density()
        view = _blur(density, PSF_SIGMA_UM * _SIGMA_QUANTUM ** quantum)
        with _cacheLock:
            _views[key] = view
            _views.move_to_end(key)
            while len(_views) > _VIEW_CACHE_SIZE:
                _views.popitem(last=False)
        return view

    def texture(self) -> np.ndarray:
        """The sample under the default PSF, background included, on its grid."""
        view = self.view(PSF_SIGMA_UM)
        if view.texture is None:
            view.texture = (view.values + np.float32(BACKGROUND)).astype(np.float32)
        return view.texture

    def brightness(self, x_um, y_um, sigma_um: float = PSF_SIGMA_UM) -> np.ndarray:
        """Relative brightness (background to about 1) at these positions."""
        return BACKGROUND + self.view(sigma_um).sample(x_um, y_um)

    def prepare(self) -> None:
        """Build the map and the default view (for a background thread)."""
        self.texture()


def _blur(density: np.ndarray, sigma_um: float) -> _View:
    """Peak-normalised Gaussian excitation over the map."""
    from scipy.ndimage import gaussian_filter

    sigma_texels = sigma_um / TEXEL_UM
    factor = 1
    if sigma_texels > _COARSE_ABOVE_TEXELS:
        factor = int(sigma_texels // (_COARSE_ABOVE_TEXELS / 2))
    grid = density
    if factor > 1:
        n = density.shape[0] // factor * factor
        grid = density[:n, :n].reshape(n // factor, factor, n // factor, factor).sum(axis=(1, 3))
    s = sigma_texels / factor
    values = gaussian_filter(grid, s, truncate=4.0) * np.float32(2.0 * np.pi * s * s)
    texel = TEXEL_UM * factor
    origin = -FIELD_HALF_UM + 0.5 * (factor - 1) * TEXEL_UM
    return _View(values=values.astype(np.float32, copy=False), texel_um=texel,
                 origin_um=origin, sigma_um=float(sigma_um))


# ----------------------------------------------------------------------
# Drawing
# ----------------------------------------------------------------------

#: Fluorophores per µm of a filament, per µm² of an area, or per object:
#: chosen so each structure's peak under the default PSF is roughly the
#: number in the comment (before the whole map is scaled to peak at 1).
_MEMBRANE_PER_UM = 2.0          # ~0.5
_MICROTUBULE_PER_UM = 3.0       # ~0.75
_ACTIN_PER_UM = 1.6             # ~0.4
_STRESS_FIBRE_PER_UM = 2.4      # ~0.6
_CYTOPLASM_PER_UM2 = 2.0        # ~0.13
_NUCLEUS_PER_UM2 = 5.0          # ~0.31
_ENVELOPE_PER_UM = 1.2          # ~0.3
_TARGET_LINE_PER_UM = 3.2       # ~0.8
_TARGET_AREA_PER_UM2 = 9.5      # ~0.6


class _Canvas:
    """Structures drawn layer by layer, with the dominant one per texel."""

    def __init__(self, n):
        self.n = n
        self.total = np.zeros((n, n), dtype=np.float32)
        self.labels = np.zeros((n, n), dtype=np.uint8)
        self._top = np.zeros((n, n), dtype=np.float32)
        self.names = ['none']

    def add(self, name: str, layer: np.ndarray) -> None:
        if name not in self.names:
            self.names.append(name)
        index = self.names.index(name)
        self.total += layer
        dominant = layer > self._top
        self.labels[dominant] = index
        self._top[dominant] = layer[dominant]


class _Layer:
    """One structure class being drawn."""

    def __init__(self, n):
        self.n = n
        self.values = np.zeros((n, n), dtype=np.float32)
        self._points = []

    def points(self, x, y, weight):
        """Point emitters, splatted bilinearly (flushed by :meth:`done`)."""
        x = np.asarray(x, dtype=float).ravel()
        y = np.asarray(y, dtype=float).ravel()
        weight = np.broadcast_to(np.asarray(weight, dtype=float), x.shape).ravel()
        self._points.append((x, y, weight))

    def curve(self, x, y, per_um, spacing=0.01):
        """A filament along a polyline: points every ``spacing`` µm."""
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        if x.size < 2:
            return
        seg = np.hypot(np.diff(x), np.diff(y))
        s = np.concatenate([[0.0], np.cumsum(seg)])
        if s[-1] <= 0:
            return
        t = np.arange(0.0, s[-1], spacing)
        self.points(np.interp(t, s, x), np.interp(t, s, y), per_um * spacing)

    def area(self, box, weight):
        """Add ``weight`` (fluorophores per texel) over ``box`` (slices)."""
        self.values[box] += weight.astype(np.float32, copy=False)

    def done(self) -> np.ndarray:
        n = self.n
        if self._points:
            x = np.concatenate([p[0] for p in self._points])
            y = np.concatenate([p[1] for p in self._points])
            w = np.concatenate([p[2] for p in self._points])
            c = (x + FIELD_HALF_UM) / TEXEL_UM
            r = (y + FIELD_HALF_UM) / TEXEL_UM
            c0 = np.floor(c).astype(np.int64)
            r0 = np.floor(r).astype(np.int64)
            fc, fr = c - c0, r - r0
            index, weights = [], []
            for dr, dc, part in ((0, 0, (1 - fr) * (1 - fc)), (0, 1, (1 - fr) * fc),
                                 (1, 0, fr * (1 - fc)), (1, 1, fr * fc)):
                rr, cc = r0 + dr, c0 + dc
                ok = (rr >= 0) & (rr < n) & (cc >= 0) & (cc < n)
                index.append(rr[ok] * n + cc[ok])
                weights.append((w * part)[ok])
            flat = np.bincount(np.concatenate(index), np.concatenate(weights), minlength=n * n)
            self.values += flat.reshape(n, n).astype(np.float32)
            self._points = []
        return self.values


def _axis(n):
    return np.arange(n) * TEXEL_UM - FIELD_HALF_UM


def _box(axis, x, y, half):
    """Slices of the grid around (x, y), ``half`` µm each way, and its coordinates."""
    rows = slice(*np.searchsorted(axis, (y - half, y + half)))
    cols = slice(*np.searchsorted(axis, (x - half, x + half)))
    yy, xx = np.meshgrid(axis[rows] - y, axis[cols] - x, indexing='ij')
    return (rows, cols), xx, yy


def _ellipse(layer, axis, x, y, a, b, angle, per_um2, rim_per_um=0.0):
    """A filled ellipse (soft edge), optionally with a bright outline."""
    box, xx, yy = _box(axis, x, y, max(a, b) + 0.1)
    u = xx * np.cos(angle) + yy * np.sin(angle)
    v = -xx * np.sin(angle) + yy * np.cos(angle)
    q = np.hypot(u / a, v / b)
    edge = np.clip(0.5 + (1.0 - q) * min(a, b) / TEXEL_UM, 0.0, 1.0)
    if per_um2:
        layer.area(box, edge * per_um2 * TEXEL_UM ** 2)
    if rim_per_um:
        t = np.linspace(0, 2 * np.pi, max(16, int(2 * np.pi * max(a, b) / 0.01)), endpoint=False)
        ex, ey = a * np.cos(t), b * np.sin(t)
        layer.curve(np.append(x + ex * np.cos(angle) - ey * np.sin(angle),
                              x + a * np.cos(angle)),
                    np.append(y + ex * np.sin(angle) + ey * np.cos(angle),
                              y + a * np.sin(angle)),
                    rim_per_um)


def _sphere(layer, axis, x, y, radius, total, shell=0.0):
    """A bead (filled) or vesicle (``shell`` thick membrane), projected."""
    if radius < 2 * TEXEL_UM:
        layer.points(x, y, total)
        return
    box, xx, yy = _box(axis, x, y, radius + TEXEL_UM)
    rho2 = xx * xx + yy * yy
    profile = np.sqrt(np.maximum(radius ** 2 - rho2, 0.0))
    if shell:
        profile -= np.sqrt(np.maximum((radius - shell) ** 2 - rho2, 0.0))
    s = float(profile.sum())
    if s > 0:
        layer.area(box, profile * (total / s))


def _bead_total(radius):
    """Fluorophores of a bead so its peak under the default PSF is ~1.2.

    Each size gets its own dye loading: in a real sample a 1 µm bead is
    thousands of times brighter than a 40 nm one, which no display shows at
    once.
    """
    return 1.2 * (PSF_SIGMA_UM ** 2 + radius ** 2 / 5.0) / PSF_SIGMA_UM ** 2


def _worm(rng, x, y, angle, length, persistence, step=0.02):
    """A worm-like chain from (x, y): a filament of this persistence length."""
    n = max(2, int(length / step))
    turns = rng.normal(0.0, math.sqrt(step / persistence), n - 1)
    angles = angle + np.concatenate([[0.0], np.cumsum(turns)])
    xs = x + np.concatenate([[0.0], np.cumsum(step * np.cos(angles[:-1]))])
    ys = y + np.concatenate([[0.0], np.cumsum(step * np.sin(angles[:-1]))])
    return xs, ys


def _aster_ray(rng, x, y, length, persistence):
    """A microtubule growing out of a centrosome at (x, y).

    It starts somewhere in the pericentriolar cloud (0.2 to 0.6 µm out), not
    at one point, so twenty of them do not pile up into one hot texel.
    """
    heading = rng.uniform(0, 2 * np.pi)
    start = rng.uniform(0.2, 0.6)
    return _worm(rng, x + start * np.cos(heading), y + start * np.sin(heading),
                 heading, length, persistence)


def _inside_ellipse(xs, ys, x, y, a, b, angle, limit=0.97):
    u = (xs - x) * np.cos(angle) + (ys - y) * np.sin(angle)
    v = -(xs - x) * np.sin(angle) + (ys - y) * np.cos(angle)
    inside = np.hypot(u / a, v / b) < limit
    # The filament ends where it first leaves the cell.
    if not inside.all():
        stop = int(np.argmin(inside))
        return xs[:stop], ys[:stop]
    return xs, ys


def _draw_cells(canvas, axis, rng):
    n = canvas.n
    membrane, cytoplasm, nucleus = _Layer(n), _Layer(n), _Layer(n)
    microtubules, actin, vesicles, beads = _Layer(n), _Layer(n), _Layer(n), _Layer(n)

    cells = []
    for _ in range(400):
        if len(cells) >= 16:
            break
        x, y = rng.uniform(-FIELD_HALF_UM + 5, FIELD_HALF_UM - 5, 2)
        radius = rng.uniform(3.0, 5.5)
        if all(np.hypot(x - cx, y - cy) > radius + cr + 0.5 for cx, cy, cr in cells):
            cells.append((x, y, radius))
    for x, y, radius in cells:
        aspect = rng.uniform(0.7, 1.0)
        angle = rng.uniform(0, np.pi)
        a, b = radius, radius * aspect
        _ellipse(membrane, axis, x, y, a, b, angle, 0.0, _MEMBRANE_PER_UM)
        _ellipse(cytoplasm, axis, x, y, a, b, angle, _CYTOPLASM_PER_UM2)

        # Nucleus, off centre, with its envelope.
        nu, nv = rng.uniform(-0.2, 0.2, 2) * radius
        nx = x + nu * np.cos(angle) - nv * aspect * np.sin(angle)
        ny = y + nu * np.sin(angle) + nv * aspect * np.cos(angle)
        na, nb = 0.4 * a, 0.4 * b * rng.uniform(0.85, 1.0)
        _ellipse(nucleus, axis, nx, ny, na, nb, angle, _NUCLEUS_PER_UM2, _ENVELOPE_PER_UM)

        # Microtubules from a centrosome beside the nucleus to the cortex.
        phi = rng.uniform(0, 2 * np.pi)
        sx = nx + 1.15 * na * np.cos(phi)
        sy = ny + 1.15 * nb * np.sin(phi)
        for _ in range(int(rng.integers(14, 22))):
            xs, ys = _aster_ray(rng, sx, sy, 2.2 * radius, 8.0)
            xs, ys = _inside_ellipse(xs, ys, x, y, a, b, angle)
            microtubules.curve(xs, ys, _MICROTUBULE_PER_UM)

        # A few stress fibres: nearly straight chords.
        for _ in range(int(rng.integers(2, 5))):
            t = rng.uniform(0, 2 * np.pi)
            px = x + 0.85 * a * np.cos(t) * np.cos(angle) - 0.85 * b * np.sin(t) * np.sin(angle)
            py = y + 0.85 * a * np.cos(t) * np.sin(angle) + 0.85 * b * np.sin(t) * np.cos(angle)
            heading = math.atan2(y - py, x - px) + rng.normal(0, 0.35)
            xs, ys = _worm(rng, px, py, heading, 2 * radius, 40.0)
            xs, ys = _inside_ellipse(xs, ys, x, y, a, b, angle, limit=0.9)
            actin.curve(xs, ys, _STRESS_FIBRE_PER_UM)

        # Vesicles: most below the resolution, some resolvable rings.
        for _ in range(int(rng.integers(10, 26))):
            rr = np.sqrt(rng.uniform(0.2, 0.85))
            t = rng.uniform(0, 2 * np.pi)
            vx = x + rr * (a * np.cos(t) * np.cos(angle) - b * np.sin(t) * np.sin(angle))
            vy = y + rr * (a * np.cos(t) * np.sin(angle) + b * np.sin(t) * np.cos(angle))
            if rng.uniform() < 0.7:
                _sphere(vesicles, axis, vx, vy, rng.uniform(0.02, 0.04), 1.0)
            else:
                r = rng.uniform(0.08, 0.16)
                _sphere(vesicles, axis, vx, vy, r, 1.5 + r / 0.05, shell=0.012)

    # Beads of known size between (and over) the cells.
    for diameter, count in ((0.04, 15), (0.1, 25), (0.2, 10)):
        for x, y in rng.uniform(-FIELD_HALF_UM, FIELD_HALF_UM, (count, 2)):
            _sphere(beads, axis, x, y, diameter / 2, _bead_total(diameter / 2))

    for name, layer in (('cytoplasm', cytoplasm), ('nucleus', nucleus),
                        ('membrane', membrane), ('microtubule', microtubules),
                        ('actin', actin), ('vesicle', vesicles), ('bead', beads)):
        canvas.add(name, layer.done())


def _draw_filaments(canvas, axis, rng):
    n = canvas.n
    microtubules, actin, beads = _Layer(n), _Layer(n), _Layer(n)
    half = FIELD_HALF_UM
    for _ in range(5):                                   # asters
        cx, cy = rng.uniform(-half + 8, half - 8, 2)
        for _ in range(25):
            xs, ys = _aster_ray(rng, cx, cy, rng.uniform(6, 22), 10.0)
            microtubules.curve(xs, ys, _MICROTUBULE_PER_UM)
    for _ in range(30):                                  # long free filaments
        x, y = rng.uniform(-half, half, 2)
        xs, ys = _worm(rng, x, y, rng.uniform(0, 2 * np.pi), rng.uniform(15, 40), 20.0)
        microtubules.curve(xs, ys, _MICROTUBULE_PER_UM)
    for _ in range(120):                                 # short floppy actin
        x, y = rng.uniform(-half, half, 2)
        xs, ys = _worm(rng, x, y, rng.uniform(0, 2 * np.pi), rng.uniform(2, 6), 1.5)
        actin.curve(xs, ys, _ACTIN_PER_UM)
    for x, y in rng.uniform(-half, half, (20, 2)):
        _sphere(beads, axis, x, y, 0.05, _bead_total(0.05))
    for name, layer in (('microtubule', microtubules), ('actin', actin), ('bead', beads)):
        canvas.add(name, layer.done())


#: Bead diameters of the ``beads`` preset, µm, one row band each.
BEAD_DIAMETERS_UM = (0.04, 0.1, 0.2, 0.5, 1.0)
#: Separations of the line and bead pairs of the ``beads`` and ``resolution``
#: presets, µm.
PAIR_SEPARATIONS_UM = (0.6, 0.4, 0.3, 0.25, 0.2, 0.16, 0.13, 0.1, 0.08, 0.06, 0.05, 0.04)
#: Grating periods of the ``resolution`` preset, µm.
GRATING_PERIODS_UM = (0.5, 0.3, 0.2, 0.15, 0.12, 0.1)


def _draw_beads(canvas, axis, rng):
    layer = _Layer(canvas.n)
    # Rows of each size: y bands from -24 µm up, 5 µm apart, beads 4 µm apart.
    for row, diameter in enumerate(BEAD_DIAMETERS_UM):
        y = -24.0 + 5.0 * row
        for x in np.arange(-26.0, 26.5, 4.0):
            jx, jy = rng.uniform(-0.3, 0.3, 2)
            _sphere(layer, axis, x + jx, y + jy, diameter / 2, _bead_total(diameter / 2))
    # Pairs of 40 nm beads at known separations, at a random angle each.
    for k, separation in enumerate(PAIR_SEPARATIONS_UM):
        x, y = -22.0 + 4.0 * k, 3.0
        t = rng.uniform(0, np.pi)
        dx, dy = 0.5 * separation * np.cos(t), 0.5 * separation * np.sin(t)
        layer.points([x - dx, x + dx], [y - dy, y + dy], _bead_total(0.02))
    # Scattered 40 nm beads above, for PSF statistics.
    xy = rng.uniform((-28.0, 7.0), (28.0, 28.0), (250, 2))
    layer.points(xy[:, 0], xy[:, 1], _bead_total(0.02))
    canvas.add('bead', layer.done())


def _draw_resolution(canvas, axis, rng):
    n = canvas.n
    target, beads = _Layer(n), _Layer(n)
    # Siemens star at the centre: 36 spokes, 6 µm radius.
    box, xx, yy = _box(axis, 0.0, 0.0, 6.0)
    r = np.hypot(xx, yy)
    spokes = 36
    phase = (np.arctan2(yy, xx) % (2 * np.pi / spokes)) * spokes / (2 * np.pi)
    # Soft spoke edges, a texel wide at the radius.
    edge = TEXEL_UM / np.maximum(r, TEXEL_UM) * spokes / (2 * np.pi)
    inside = np.where(phase < 0.5, np.minimum(phase, 0.5 - phase),
                      -np.minimum(phase - 0.5, 1.0 - phase))
    wedge = np.clip(0.5 + inside / edge, 0, 1)
    wedge *= (r <= 6.0) & (r >= 0.15)
    target.area(box, wedge * _TARGET_AREA_PER_UM2 * TEXEL_UM ** 2)

    # Line pairs, vertical above the star and horizontal below it.
    for k, separation in enumerate(PAIR_SEPARATIONS_UM):
        x = -12.1 + 2.2 * k
        for dx in (-0.5 * separation, 0.5 * separation):
            target.curve([x + dx, x + dx], [8.5, 11.5], _TARGET_LINE_PER_UM)
            target.curve([x - 1.5, x + 0.5], [-9.5 + dx, -9.5 + dx], _TARGET_LINE_PER_UM)
    # Pairs of 40 nm beads at the same separations, along x and along y.
    for k, separation in enumerate(PAIR_SEPARATIONS_UM):
        x = -12.1 + 2.2 * k
        beads.points([x - 0.5 * separation, x + 0.5 * separation], [14.0, 14.0],
                     _bead_total(0.02))
        beads.points([x, x], [-13.5 - 0.5 * separation, -13.5 + 0.5 * separation],
                     _bead_total(0.02))
    # Gratings of known period left of the star, a column of 2.5 µm patches,
    # all of the same mean brightness (fewer fluorophores per line when
    # the lines are closer).
    for k, period in enumerate(GRATING_PERIODS_UM):
        y0 = -7.5 + 2.6 * k
        for x in np.arange(-15.0, -12.5, period):
            target.curve([x, x], [y0, y0 + 2.4],
                         _TARGET_LINE_PER_UM * period / max(GRATING_PERIODS_UM))
    # A field of 40 nm beads right of the star, for the PSF.
    xy = np.stack(np.meshgrid(np.arange(9.0, 15.1, 1.0), np.arange(-6.0, 6.1, 1.0)), -1)
    xy = xy.reshape(-1, 2) + rng.uniform(-0.15, 0.15, (xy.size // 2, 2))
    beads.points(xy[:, 0], xy[:, 1], _bead_total(0.02))
    canvas.add('target', target.done())
    canvas.add('bead', beads.done())


_DRAW = {
    'cells': _draw_cells,
    'filaments': _draw_filaments,
    'beads': _draw_beads,
    'resolution': _draw_resolution,
}


def _build(seed: int, preset: str):
    from scipy.ndimage import gaussian_filter

    rng = np.random.default_rng(seed)
    n = int(round(2 * FIELD_HALF_UM / TEXEL_UM)) + 1
    canvas = _Canvas(n)
    _DRAW[preset](canvas, _axis(n), rng)
    density = canvas.total
    # Scale so the default view peaks at 1 - BACKGROUND (texture() peaks at 1).
    s = PSF_SIGMA_UM / TEXEL_UM
    peak = float((gaussian_filter(density, s, truncate=4.0) * (2 * np.pi * s * s)).max())
    if peak > 0:
        density *= np.float32((1.0 - BACKGROUND) / peak)
    return density, canvas.labels, tuple(canvas.names)


__all__ = ['MockSample', 'BACKGROUND', 'FIELD_HALF_UM', 'PRESETS', 'PSF_SIGMA_UM',
           'FWHM_PER_SIGMA', 'TEXEL_UM']


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
