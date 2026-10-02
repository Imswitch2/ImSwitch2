"""Bead-based PSF / resolution analysis for ImProcess.

Pure numpy/scipy, no Qt. The ``psf-resolution`` processor is a thin wrapper
around :func:`analyze_beads`, :func:`select_beads`, :func:`summarize` and
:func:`average_psf`; the ``psf-bead-select`` processor re-runs only the
cheap selection and summary on an existing analysis.

Pipeline
    1. candidates  LoG blob detection in the 2-D image or the 3-D stack itself,
                   or one candidate per ROI Manager ROI
    2. pre-filter  side lobes / defocus rings of brighter beads and faint
                   peaks are discarded; edge (image border or no-data region),
                   crowded, saturated and abnormally bright (aggregate) beads
                   are flagged
    3. fit         2-D: axis-aligned Gaussian + offset per bead crop
                   3-D: ``separable`` (lateral 2-D fit in the brightest plane,
                   axial 1-D profile through its centre) or ``full``
                   (axis-aligned 3-D Gaussian). Voxels without data are left
                   out of the fit.
    4. correct     sigma_psf^2 = sigma_meas^2 - sigma_bead^2. Variances add
                   exactly under convolution; for a uniform sphere of
                   diameter d, sigma_bead^2 = d^2/20 (volume labelled) or
                   d^2/12 (shell labelled)
    5. select      fit quality (R^2), ellipticity, then the lateral / axial
                   FWHM range (default median +- k*MAD of the good fits)
    6. summarize   mean / std / SEM / median / MAD, comparison with the
                   widefield diffraction limit
    7. average     sub-pixel aligned, background-subtracted, amplitude-
                   normalised mean bead, refitted with the same model

Detection scale. Crops, isolation and lobe distances scale with the expected
PSF width. Given explicitly it is used as is; otherwise it is estimated from
the data (a first pass from NA and wavelength, or from a 1.5 px guess, is
fitted and the median width of its beads starts the next pass).

No-data voxels. Non-finite voxels, and with ``zero_is_invalid`` solid regions
of exact zeros (the padding of a deskewed or registered volume), carry no
data: beads whose core touches them are flagged ``border``, and the rest of
the crop is fitted without them.

Units. Arrays are ``(Y, X)`` or ``(Z, Y, X)`` and every width is reported in
``analysis.unit``: ``"nm"`` when the data carry a physical pixel size, ``"px"``
otherwise. The physical extras (bead-size correction, diffraction-limit
comparison, aberrations) need nanometres and are skipped, with a warning,
for pixel-unit data.
"""

from __future__ import annotations

import math
import warnings as _warnings
from dataclasses import dataclass, field, replace
from typing import Any, Sequence

import numpy as np
from scipy import ndimage, optimize
from scipy.spatial import cKDTree

FWHM_FACTOR = 2.0 * math.sqrt(2.0 * math.log(2.0))
MAD_SCALE = 1.4826  # MAD -> standard deviation for normally distributed data

STATUS_OK = "ok"
STATUS_BORDER = "border"
STATUS_CROWDED = "crowded"
STATUS_SATURATED = "saturated"
STATUS_BRIGHT = "bright"
STATUS_FIT_FAILED = "fit_failed"
STATUSES = (
    STATUS_OK, STATUS_BORDER, STATUS_CROWDED, STATUS_SATURATED, STATUS_BRIGHT, STATUS_FIT_FAILED,
)
#: What each pre-fit status means, for previews and reports.
STATUS_LABELS = {
    STATUS_OK: "fitted",
    STATUS_BORDER: "at the image or data edge",
    STATUS_CROWDED: "too close to another bead",
    STATUS_SATURATED: "saturated",
    STATUS_BRIGHT: "too bright (aggregate?)",
    STATUS_FIT_FAILED: "fit failed",
}
#: Rejection reasons of fitted beads, in the order they are tested.
REASON_R2 = "fit_quality"
REASON_ELLIPTICITY = "ellipticity"
REASON_FWHM = "fwhm_outlier"
REASON_LABELS = {
    REASON_R2: "poor fit (R²)",
    REASON_ELLIPTICITY: "too elliptical",
    REASON_FWHM: "FWHM outlier",
}

BEAD_LABELINGS = ("volume", "shell")
FIT_MODES_3D = ("separable", "full")

#: Lateral sigma (px) the data-driven scale estimate starts from when nothing
#: else is known, and the axial-to-lateral ratio it assumes for 3-D stacks.
_START_SIGMA_PX = 1.5
_START_AXIAL_RATIO = 2.5
_SCALE_ITERATIONS = 3
_SCALE_TOLERANCE = 0.15


# --------------------------------------------------------------------------- #
# Parameters
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BeadPSFParams:
    """Settings of one bead analysis.

    ``pixel_size`` is ``(y, x)`` or ``(z, y, x)`` in ``unit``. Every physical
    input (bead diameter, wavelength, expected FWHM) is in nanometres and is
    ignored when ``unit`` is ``"px"``, except ``expected_fwhm_nm``, which is
    then read in px.
    """

    pixel_size: tuple[float, ...]
    unit: str = "nm"
    bead_diameter_nm: float | None = None
    bead_labeling: str = "volume"
    expected_fwhm_nm: tuple[float, ...] | None = None  # (lateral, axial); None = estimate from data
    na: float | None = None
    wavelength_nm: float | None = None
    refractive_index: float = 1.515
    detection_threshold: float = 5.0          # smoothed peak > k x background noise
    isolation_factor: float = 3.0             # min neighbour distance, in expected FWHMs
    crop_factor: float = 4.0                  # crop half-size, in expected sigmas
    saturation_value: float | None = None     # default: dtype max for integer data
    brightness_outlier_factor: float = 1.8    # amplitude > f * median (at least) -> aggregate
    satellite_ratio: float = 2.5              # neighbour > f x brighter -> side lobe, dropped
    satellite_factor: float = 2.0             # side-lobe search radius, in isolation distances
    min_amp_fraction: float = 0.1             # peaks < f x 90th-percentile amplitude dropped
    fit_mode_3d: str = "separable"
    correction_warn_fraction: float = 0.3     # warn if the bead correction exceeds 30 %
    zero_is_invalid: bool = True              # solid regions of exact zeros carry no data

    @property
    def physical(self) -> bool:
        """Whether widths are in nanometres, so physical extras apply."""
        return self.unit == "nm"


# --------------------------------------------------------------------------- #
# Physics helpers
# --------------------------------------------------------------------------- #
def theoretical_fwhm_nm(na: float, wavelength_nm: float, n: float = 1.515) -> tuple[float, float]:
    """Widefield FWHM estimates ``(lateral, axial)`` in nm.

    Lateral 0.51 lambda/NA; axial 0.88 lambda / (n - sqrt(n^2 - NA^2)). The
    axial value is NaN when NA >= n.
    """
    lateral = 0.51 * wavelength_nm / na
    if na >= n:
        return lateral, float("nan")
    return lateral, 0.88 * wavelength_nm / (n - math.sqrt(n * n - na * na))


def bead_sigma_nm(diameter_nm: float, labeling: str = "volume") -> float:
    """Per-axis standard deviation of a uniform sphere's intensity distribution."""
    if labeling == "volume":
        return diameter_nm / math.sqrt(20.0)
    if labeling == "shell":
        return diameter_nm / math.sqrt(12.0)
    raise ValueError(f"unknown bead labeling {labeling!r}; expected one of {BEAD_LABELINGS}")


def correct_fwhm_for_bead(fwhm_nm: float, diameter_nm: float, labeling: str = "volume") -> float:
    """Remove the bead's own extent by subtracting variances; NaN if impossible."""
    var = (fwhm_nm / FWHM_FACTOR) ** 2 - bead_sigma_nm(diameter_nm, labeling) ** 2
    return FWHM_FACTOR * math.sqrt(var) if var > 0 else float("nan")


# --------------------------------------------------------------------------- #
# Gaussian fitting (axis-aligned, 1-D to 3-D)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GaussianFit:
    """Axis-aligned Gaussian + offset fitted to a crop, in crop pixel coordinates."""

    amp: float
    center: np.ndarray
    sigma: np.ndarray
    offset: float
    center_err: np.ndarray
    sigma_err: np.ndarray
    r2: float


def _gauss_nd(ndim: int):
    def model(coords, *p):
        r2 = 0.0
        for k in range(ndim):
            r2 = r2 + ((coords[k] - p[1 + k]) / p[1 + ndim + k]) ** 2
        return p[0] * np.exp(-0.5 * r2) + p[-1]
    return model


def fit_gaussian_nd(data: np.ndarray, valid: np.ndarray | None = None) -> GaussianFit | None:
    """Fit an axis-aligned Gaussian + constant offset; ``None`` if the fit fails.

    ``valid`` (same shape, bool) leaves the ``False`` voxels out of the fit.
    """
    data = np.asarray(data, dtype=np.float64)
    ndim = data.ndim
    grids = np.indices(data.shape, dtype=np.float64)
    if valid is None:
        valid = np.isfinite(data)
    else:
        valid = np.asarray(valid, dtype=bool) & np.isfinite(data)
    if valid.sum() < 2 * ndim + 3:
        return None
    values = data[valid]
    coords = tuple(g[valid] for g in grids)

    b0 = float(np.percentile(values, 10))
    w = np.clip(values - b0, 0, None)
    total = float(w.sum())
    if not np.isfinite(total) or total <= 0:
        return None
    mu0 = [float((w * c).sum() / total) for c in coords]
    s0 = [float(np.sqrt((w * (c - m) ** 2).sum() / total)) for c, m in zip(coords, mu0)]
    a0 = float(values.max() - b0)

    lower = [0.0] + [0.0] * ndim + [0.3] * ndim + [-np.inf]
    upper = [np.inf] + [n - 1.0 for n in data.shape] + [float(n) for n in data.shape] + [np.inf]
    p0 = [a0, *mu0, *s0, b0]
    eps = 1e-6
    p0 = [
        min(max(v, lo + eps), hi - eps) if np.isfinite(hi) else max(v, lo + eps)
        for v, lo, hi in zip(p0, lower, upper)
    ]
    try:
        with _warnings.catch_warnings():
            _warnings.simplefilter("ignore", optimize.OptimizeWarning)
            popt, pcov = optimize.curve_fit(
                _gauss_nd(ndim), coords, values, p0=p0, bounds=(lower, upper), maxfev=10000
            )
    except (RuntimeError, ValueError):
        return None

    resid = values - _gauss_nd(ndim)(coords, *popt)
    ss_tot = float(((values - values.mean()) ** 2).sum())
    r2 = 1.0 - float((resid**2).sum()) / ss_tot if ss_tot > 0 else float("nan")
    perr = (
        np.sqrt(np.clip(np.diag(pcov), 0, None))
        if np.all(np.isfinite(pcov))
        else np.full(len(popt), np.nan)
    )
    return GaussianFit(
        amp=float(popt[0]),
        center=np.asarray(popt[1:1 + ndim], dtype=np.float64),
        sigma=np.asarray(popt[1 + ndim:1 + 2 * ndim], dtype=np.float64),
        offset=float(popt[-1]),
        center_err=perr[1:1 + ndim],
        sigma_err=perr[1 + ndim:1 + 2 * ndim],
        r2=r2,
    )


@dataclass(frozen=True)
class VolumeFit:
    """A 3-D bead fit in crop pixel coordinates, ``(z, y, x)`` order."""

    center: np.ndarray
    sigma: np.ndarray
    sigma_err: np.ndarray
    amp: float
    offset: float
    r2_lateral: float
    r2_axial: float
    focus: int = 0  # the plane the lateral widths were measured in


def _column(crop: np.ndarray, valid: np.ndarray, y: int, x: int, r_xy: int) -> np.ndarray:
    """Mean over the valid voxels of a ``(2 r + 1)^2`` column per plane; NaN where none."""
    ny, nx = crop.shape[1:]
    window = (slice(None), slice(max(0, y - r_xy), min(ny, y + r_xy + 1)),
              slice(max(0, x - r_xy), min(nx, x + r_xy + 1)))
    values, weights = crop[window], valid[window]
    count = weights.sum(axis=(1, 2))
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(count > 0, (values * weights).sum(axis=(1, 2)) / np.maximum(count, 1), np.nan)


def fit_volume(
    crop: np.ndarray, mode: str = "separable", r_xy: int = 1, valid: np.ndarray | None = None
) -> VolumeFit | None:
    """Fit a 3-D bead crop with the ``separable`` or ``full`` model.

    ``separable`` takes the lateral widths from a 2-D fit in the brightest
    plane of the central column (the best focus, also for an asymmetric
    axial profile) and the axial width from the profile through that fit's
    centre.
    """
    crop = np.asarray(crop, dtype=np.float64)
    valid = np.isfinite(crop) if valid is None else (np.asarray(valid, dtype=bool) & np.isfinite(crop))
    if mode == "full":
        f = fit_gaussian_nd(crop, valid)
        if f is None:
            return None
        focus = int(np.clip(round(float(f.center[0])), 0, crop.shape[0] - 1))
        return VolumeFit(f.center, f.sigma, f.sigma_err, f.amp, f.offset, f.r2, f.r2, focus)
    if mode != "separable":
        raise ValueError(f"unknown 3-D fit mode {mode!r}; expected one of {FIT_MODES_3D}")
    nz, ny, nx = crop.shape
    column = _column(crop, valid, ny // 2, nx // 2, r_xy)
    if not np.isfinite(column).any():
        return None
    focus = int(np.nanargmax(column))
    fl = fit_gaussian_nd(crop[focus], valid[focus])
    if fl is None:
        return None
    yl = int(np.clip(round(float(fl.center[0])), 0, ny - 1))
    xl = int(np.clip(round(float(fl.center[1])), 0, nx - 1))
    column = _column(crop, valid, yl, xl, r_xy)
    finite = np.isfinite(column)
    fz = fit_gaussian_nd(np.where(finite, column, 0.0), finite)
    if fz is None:
        return None
    return VolumeFit(
        center=np.array([fz.center[0], *fl.center]),
        sigma=np.array([fz.sigma[0], *fl.sigma]),
        sigma_err=np.array([fz.sigma_err[0], *fl.sigma_err]),
        amp=fl.amp,
        offset=fl.offset,
        r2_lateral=fl.r2,
        r2_axial=fz.r2,
        focus=focus,
    )


# --------------------------------------------------------------------------- #
# Direct (half-maximum) widths
# --------------------------------------------------------------------------- #
_HM_STEP = 0.05  # profile sampling, px


def half_max_width(profile: np.ndarray, step: float) -> float:
    """Full width at half maximum of a background-subtracted 1-D profile
    sampled every ``step`` around its centre, from linearly interpolated
    half-maximum crossings either side of the central peak; NaN if the
    profile does not fall to half on both sides (or holds no data there)."""
    profile = np.asarray(profile, dtype=np.float64)
    n = profile.size
    mid = n // 2
    reach = max(1, int(round(1.0 / step)))  # the peak is looked for within 1 px of the centre
    window = profile[max(0, mid - reach):mid + reach + 1]
    if not np.isfinite(window).any():
        return float("nan")
    peak_index = max(0, mid - reach) + int(np.nanargmax(window))
    peak = profile[peak_index]
    if not np.isfinite(peak) or peak <= 0:
        return float("nan")
    half = 0.5 * peak

    def crossing(direction: int) -> float | None:
        i = peak_index
        while 0 <= i + direction < n:
            j = i + direction
            if not np.isfinite(profile[j]):
                return None
            if profile[j] < half:
                frac = (profile[i] - half) / (profile[i] - profile[j])
                return (i + direction * frac) * step
            i = j
        return None

    left, right = crossing(-1), crossing(+1)
    if left is None or right is None:
        return float("nan")
    return float(right - left)


def half_max_widths(
    crop: np.ndarray, center: Sequence[float], offset: float, valid: np.ndarray | None = None,
    focus: int | None = None,
) -> np.ndarray:
    """Half-maximum widths (px) along each axis through ``center``.

    In a 3-D crop every profile runs through the plane ``focus`` (default:
    the centre's plane), the brightest one, so an asymmetric axial profile is
    measured from its peak. Values are linearly interpolated; ``offset`` is
    the background removed first.
    """
    crop = np.asarray(crop, dtype=np.float64)
    data = crop if valid is None else np.where(valid, crop, np.nan)
    point = np.asarray(center, dtype=np.float64).copy()
    ndim = crop.ndim
    widths = np.full(ndim, np.nan)
    if ndim == 3 and focus is not None:
        point[0] = float(np.clip(focus, 0, crop.shape[0] - 1))
    for axis in range(ndim):
        reach = crop.shape[axis] - 1
        t = np.arange(-reach, reach + _HM_STEP / 2, _HM_STEP)
        coords = [np.full_like(t, point[k]) for k in range(ndim)]
        coords[axis] = coords[axis] + t
        inside = (coords[axis] >= 0) & (coords[axis] <= crop.shape[axis] - 1)
        profile = np.full_like(t, np.nan)
        profile[inside] = ndimage.map_coordinates(
            np.nan_to_num(data, nan=offset), [c[inside] for c in coords], order=1
        ) - offset
        if valid is not None:
            ok = ndimage.map_coordinates(
                np.asarray(valid, dtype=np.float64), [c[inside] for c in coords], order=1
            ) > 0.999
            profile[np.flatnonzero(inside)[~ok]] = np.nan
        widths[axis] = half_max_width(profile, _HM_STEP)
    return widths


# --------------------------------------------------------------------------- #
# No-data voxels, background and detection
# --------------------------------------------------------------------------- #
def valid_mask(data: np.ndarray, zero_is_invalid: bool = True) -> np.ndarray:
    """Voxels that carry data.

    Non-finite voxels never do. With ``zero_is_invalid``, solid regions of
    exact zeros do not either (the padding a deskew or registration leaves
    around the data); isolated zeros, e.g. clipped background noise, are
    removed from that mask by a morphological opening and stay valid.
    """
    data = np.asarray(data)
    invalid = ~np.isfinite(data) if np.issubdtype(data.dtype, np.floating) else np.zeros(data.shape, bool)
    if zero_is_invalid:
        zeros = data == 0
        if zeros.any():
            invalid |= ndimage.binary_opening(zeros, structure=np.ones((3,) * data.ndim, dtype=bool))
    return ~invalid


def background_and_noise(data: np.ndarray, valid: np.ndarray) -> tuple[float, float]:
    """Median and robust standard deviation (MAD) of the valid voxels."""
    values = np.asarray(data)[valid]
    if values.size == 0:
        return 0.0, 1.0
    if values.size > 2_000_000:  # a regular subsample is plenty for two quantiles
        values = values[:: values.size // 1_000_000]
    values = values.astype(np.float64)
    med = float(np.median(values))
    noise = MAD_SCALE * float(np.median(np.abs(values - med)))
    if noise <= 0:
        noise = float(values.std()) or 1.0
    return med, noise


def detect_beads(
    data: np.ndarray,
    sigma_px: Sequence[float],
    threshold: float,
    valid: np.ndarray | None = None,
    background: float | None = None,
    noise: float | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """LoG blob detection in a 2-D image or 3-D stack.

    Peaks are local maxima of the Laplacian-of-Gaussian response within one
    expected FWHM whose Gaussian-smoothed height above the background
    exceeds ``threshold`` times the background noise. Returns ``(points,
    amplitude)``: integer ``(N, ndim)`` positions and the smoothed height above
    background at each, brightest first.
    """
    data = np.asarray(data)
    valid = np.ones(data.shape, bool) if valid is None else valid
    if background is None or noise is None:
        background, noise = background_and_noise(data, valid)
    image = np.where(valid, data, background).astype(np.float32)
    sigma = tuple(float(s) for s in sigma_px)
    response = -ndimage.gaussian_laplace(image, sigma=sigma, output=np.float32)
    smooth = ndimage.gaussian_filter(image, sigma=sigma, output=np.float32)
    size = tuple(2 * max(1, int(round(FWHM_FACTOR * s / 2))) + 1 for s in sigma)
    peaks = (
        (response == ndimage.maximum_filter(response, size=size, mode="nearest"))
        & (response > 0)
        & (smooth - background > threshold * noise)
        & valid
    )
    points = np.argwhere(peaks)
    amplitude = smooth[tuple(points.T)].astype(np.float64) - background
    order = np.argsort(-amplitude, kind="stable")
    return points[order].astype(int), amplitude[order]


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
@dataclass
class BeadAnalysis:
    """All candidate beads of one analysis, fitted or not.

    ``beads`` holds one dict per candidate with at least ``id``, ``status``,
    ``y_det``/``x_det`` (and ``z_det``) detection pixels; fitted beads add
    ``y``/``x`` (and ``z``) fitted pixel positions and ``fwhm_*`` widths in
    ``unit``. ``notes`` are informational, ``warnings`` describe problems.
    """

    beads: list[dict[str, Any]]
    params: BeadPSFParams
    ndim: int
    shape: tuple[int, ...]
    expected_sigma_px: tuple[float, ...]
    crop_half: tuple[int, ...]
    source: str = "auto"
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    n_discarded: int = 0

    @property
    def unit(self) -> str:
        return self.params.unit

    @property
    def axes(self) -> tuple[str, ...]:
        return ("z", "y", "x") if self.ndim == 3 else ("y", "x")

    def fitted(self) -> list[dict[str, Any]]:
        return [b for b in self.beads if b["status"] == STATUS_OK]

    def expected_fwhm(self) -> tuple[float, ...]:
        """The detection scale as FWHMs ``(z,) y, x`` in ``unit``."""
        px = np.asarray(self.params.pixel_size, dtype=np.float64)
        return tuple(float(FWHM_FACTOR * s * p) for s, p in zip(self.expected_sigma_px, px))


def expected_fwhm_nm(params: BeadPSFParams) -> tuple[float | None, float | None]:
    """Expected *measured* FWHM ``(lateral, axial)`` in nm, bead size included."""
    lateral = axial = None
    if params.expected_fwhm_nm:
        lateral = float(params.expected_fwhm_nm[0])
        if len(params.expected_fwhm_nm) > 1 and params.expected_fwhm_nm[1]:
            axial = float(params.expected_fwhm_nm[1])
        return lateral, axial
    if params.na and params.wavelength_nm:
        lateral, axial = theoretical_fwhm_nm(params.na, params.wavelength_nm, params.refractive_index)
    if lateral is not None and params.bead_diameter_nm:
        bead = FWHM_FACTOR * bead_sigma_nm(params.bead_diameter_nm, params.bead_labeling)
        lateral = math.hypot(lateral, bead)
        if axial is not None and np.isfinite(axial):
            axial = math.hypot(axial, bead)
    return lateral, axial


def _start_sigma_px(params: BeadPSFParams, ndim: int) -> tuple[np.ndarray, bool]:
    """``(sigma_px, given)``: the detection scale, and whether the user gave it.

    An explicit expected FWHM is in nm (or px for pixel-unit data); otherwise
    NA and wavelength give a theoretical start, else a 1.5 px guess.
    """
    px = np.asarray(params.pixel_size, dtype=np.float64)
    given = bool(params.expected_fwhm_nm and params.expected_fwhm_nm[0])
    if given or params.physical:
        lateral, axial = expected_fwhm_nm(params)
    else:
        lateral = axial = None
    if lateral is None:
        sig_lat = np.array([_START_SIGMA_PX, _START_SIGMA_PX])
        lateral = FWHM_FACTOR * _START_SIGMA_PX * float(px[-1])
    else:
        sig_lat = (lateral / FWHM_FACTOR) / px[-2:]
    if ndim == 2:
        return sig_lat, given
    if axial is None or not np.isfinite(axial):
        axial = _START_AXIAL_RATIO * lateral
    return np.array([(axial / FWHM_FACTOR) / px[0], *sig_lat]), given


def _record_fit(bead: dict[str, Any], analysis: BeadAnalysis, center_px: np.ndarray, sigma: np.ndarray,
                sigma_err: np.ndarray, amp: float, offset: float, r2: float, r2_z: float | None,
                hm_px: np.ndarray | None = None) -> None:
    """Write one fit into a bead record: positions in px, widths in the analysis unit.

    ``hm_px`` are the direct half-maximum widths per axis, in px."""
    params = analysis.params
    px = np.asarray(params.pixel_size, dtype=np.float64)
    bead.update({"amp": float(amp), "offset": float(offset), "r2": float(r2)})
    if r2_z is not None:
        bead["r2_z"] = float(r2_z)
    for k, ax in enumerate(analysis.axes):
        bead[ax] = float(center_px[k])
        bead[f"fwhm_{ax}"] = float(FWHM_FACTOR * sigma[k] * px[k])
        bead[f"fwhm_{ax}_err"] = float(FWHM_FACTOR * sigma_err[k] * px[k])
    bead["fwhm_lat"] = 0.5 * (bead["fwhm_x"] + bead["fwhm_y"])
    bead["ellipticity"] = max(bead["fwhm_x"], bead["fwhm_y"]) / min(bead["fwhm_x"], bead["fwhm_y"])
    if hm_px is not None:
        for k, ax in enumerate(analysis.axes):
            bead[f"fwhm_{ax}_hm"] = float(hm_px[k] * px[k])
        bead["fwhm_lat_hm"] = 0.5 * (bead["fwhm_x_hm"] + bead["fwhm_y_hm"])
    if params.physical and params.bead_diameter_nm:
        for ax in (*analysis.axes, "lat"):
            bead[f"fwhm_{ax}_corr"] = correct_fwhm_for_bead(
                bead[f"fwhm_{ax}"], params.bead_diameter_nm, params.bead_labeling
            )


def _drop_satellites(points: np.ndarray, amp: np.ndarray, metric: np.ndarray, radius: float,
                     ratio: float) -> np.ndarray:
    """Keep-mask: a peak with a neighbour more than ``ratio`` times brighter
    within ``radius`` (in ``metric`` units) is a side lobe or a contaminated
    faint bead."""
    keep = np.ones(len(points), dtype=bool)
    if len(points) < 2:
        return keep
    scaled = points * metric
    tree = cKDTree(scaled)
    for i, point in enumerate(scaled):
        nb = [j for j in tree.query_ball_point(point, radius) if j != i]
        if nb and amp[nb].max() > ratio * amp[i]:
            keep[i] = False
    return keep


def _core_touches_invalid(valid_crop: np.ndarray, sigma: np.ndarray) -> bool:
    """Whether a no-data voxel lies within one FWHM of the crop centre."""
    if valid_crop.all():
        return False
    grids = np.indices(valid_crop.shape, dtype=np.float64)
    centre = (np.array(valid_crop.shape) - 1) / 2.0
    r2 = sum(((g - c) / (FWHM_FACTOR * s)) ** 2 for g, c, s in zip(grids, centre, sigma))
    return bool((~valid_crop & (r2 <= 1.0)).any())


def _analyze_once(
    data: np.ndarray,
    params: BeadPSFParams,
    sig: np.ndarray,
    valid: np.ndarray,
    background: float,
    noise: float,
    candidates_yx: np.ndarray | None,
) -> BeadAnalysis:
    is3d = data.ndim == 3
    px = np.asarray(params.pixel_size, dtype=np.float64)
    half = np.ceil(params.crop_factor * sig).astype(int)
    analysis = BeadAnalysis(
        [], params, data.ndim, tuple(data.shape), tuple(float(s) for s in sig),
        tuple(int(h) for h in half), "auto" if candidates_yx is None else "rois",
    )
    # Distances in expected FWHMs per axis, so z counts by the axial width.
    metric = 1.0 / (FWHM_FACTOR * sig)
    smooth_sigma = tuple(float(s) for s in sig)

    if candidates_yx is None:
        points, amp = detect_beads(data, sig, params.detection_threshold, valid, background, noise)
        if len(points):
            keep = _drop_satellites(
                points, amp, metric, params.satellite_factor * params.isolation_factor,
                params.satellite_ratio,
            )
            if len(points[keep]):
                reference = float(np.percentile(amp[keep], 90))
                keep &= amp >= params.min_amp_fraction * reference
            analysis.n_discarded = int((~keep).sum())
            points, amp = points[keep], amp[keep]
    else:
        yx = np.asarray(candidates_yx, dtype=int).reshape(-1, 2)
        lateral = data.shape[-2:]
        inside = (yx[:, 0] >= 0) & (yx[:, 0] < lateral[0]) & (yx[:, 1] >= 0) & (yx[:, 1] < lateral[1])
        yx = yx[inside]
        smooth = ndimage.gaussian_filter(np.where(valid, data, background).astype(np.float32), smooth_sigma)
        if is3d:
            z = np.array([int(np.argmax(smooth[:, y, x])) for y, x in yx], dtype=int)
            points = np.column_stack([z, yx]) if len(yx) else np.empty((0, 3), int)
        else:
            points = yx
        amp = smooth[tuple(points.T)].astype(np.float64) - background if len(points) else np.empty(0)

    if len(points) == 0:
        analysis.warnings.append("No beads found.")
        return analysis

    tree = cKDTree(points * metric)
    n_neigh = np.array([len(tree.query_ball_point(p, params.isolation_factor)) - 1 for p in points * metric])

    saturation = params.saturation_value
    if saturation is None and np.issubdtype(data.dtype, np.integer):
        saturation = float(np.iinfo(data.dtype).max)

    crops: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for i, point in enumerate(points):
        bead: dict[str, Any] = {"id": i, "status": STATUS_OK, "amp_est": float(amp[i])}
        for ax, value in zip(analysis.axes, point):
            bead[f"{ax}_det"] = int(value)
        lo, hi = point - half, point + half + 1
        box = tuple(slice(a, b) for a, b in zip(lo, hi))
        if np.any(lo < 0) or np.any(hi > np.array(data.shape)):
            bead["status"] = STATUS_BORDER
        else:
            valid_crop = valid[box]
            if _core_touches_invalid(valid_crop, sig) or valid_crop.mean() < 0.75:
                bead["status"] = STATUS_BORDER
            elif n_neigh[i] > 0:
                bead["status"] = STATUS_CROWDED
            else:
                crop = data[box]
                crops[i] = (crop, lo, valid_crop)
                if saturation is not None and float(crop[valid_crop].max()) >= saturation:
                    bead["status"] = STATUS_SATURATED
        analysis.beads.append(bead)

    _flag_bright(analysis, params.brightness_outlier_factor)

    r_xy = max(1, int(round(0.5 * float(sig[-2:].mean()))))
    for bead in analysis.beads:
        if bead["status"] != STATUS_OK:
            continue
        crop, origin, valid_crop = crops[bead["id"]]
        if is3d:
            vf = fit_volume(crop, params.fit_mode_3d, r_xy, valid_crop)
            if vf is None:
                bead["status"] = STATUS_FIT_FAILED
                continue
            center, sigma, sigma_err = vf.center, vf.sigma, vf.sigma_err
            amp_fit, offset, r2, r2_z, focus = vf.amp, vf.offset, vf.r2_lateral, vf.r2_axial, vf.focus
        else:
            gf = fit_gaussian_nd(crop, valid_crop)
            if gf is None:
                bead["status"] = STATUS_FIT_FAILED
                continue
            center, sigma, sigma_err = gf.center, gf.sigma, gf.sigma_err
            amp_fit, offset, r2, r2_z, focus = gf.amp, gf.offset, gf.r2, None, None
        hm = half_max_widths(crop, center, offset, valid_crop, focus)
        _record_fit(bead, analysis, center + origin, sigma, sigma_err, amp_fit, offset, r2, r2_z, hm)
    return analysis


def _flag_bright(analysis: BeadAnalysis, factor: float, k: float = 3.0) -> None:
    """Flag aggregates: amplitude above ``max(factor, exp(k * MAD(log amp)))``
    times the median of the unflagged beads.

    Relative to the spread of the population, so data whose brightness varies
    anyway (a light sheet's profile, depth) does not lose its brightest beads,
    while uniformly lit beads still lose their doublets and triplets.
    """
    amps = np.array([b["amp_est"] for b in analysis.beads if b["status"] == STATUS_OK], dtype=np.float64)
    amps = amps[amps > 0]
    if amps.size < 3:
        return
    logs = np.log(amps)
    spread = MAD_SCALE * float(np.median(np.abs(logs - np.median(logs))))
    limit = float(np.exp(np.median(logs))) * max(factor, math.exp(k * spread))
    for bead in analysis.beads:
        if bead["status"] == STATUS_OK and bead["amp_est"] > limit:
            bead["status"] = STATUS_BRIGHT


def _fitted_sigma_px(analysis: BeadAnalysis, min_r2: float = 0.5) -> np.ndarray | None:
    """Median fitted sigma per axis (px) of the reasonably fitted beads."""
    px = np.asarray(analysis.params.pixel_size, dtype=np.float64)
    good = [b for b in analysis.fitted() if b.get("r2", 0) >= min_r2 and b.get("r2_z", 1.0) >= min_r2]
    if len(good) < 3:
        return None
    widths = np.array([[b[f"fwhm_{ax}"] for ax in analysis.axes] for b in good], dtype=np.float64)
    sigma = np.median(widths, axis=0) / FWHM_FACTOR / px
    lateral = float(np.mean(sigma[-2:]))  # round lateral detection kernel
    sigma[-2:] = lateral
    return np.maximum(sigma, 0.5)


def analyze_beads(
    data: np.ndarray,
    params: BeadPSFParams,
    candidates_yx: np.ndarray | None = None,
) -> BeadAnalysis:
    """Detect (or take) candidate beads, pre-filter and fit them.

    ``candidates_yx`` replaces automatic detection with given ``(N, 2)`` pixel
    positions (e.g. one per ROI; in a stack each is taken at its brightest
    plane); the side-lobe filter is then not applied, as the caller chose the
    positions deliberately.
    """
    data = np.asarray(data)
    if data.ndim not in (2, 3):
        raise ValueError(f"bead analysis needs a (Y, X) or (Z, Y, X) array, got shape {data.shape}")
    px = np.asarray(params.pixel_size, dtype=np.float64)
    if len(px) != data.ndim:
        raise ValueError(f"pixel_size needs {data.ndim} entries for a {data.ndim}-D array")
    if params.fit_mode_3d not in FIT_MODES_3D:
        raise ValueError(f"unknown 3-D fit mode {params.fit_mode_3d!r}")
    valid = valid_mask(data, params.zero_is_invalid)
    if not valid.any():
        raise ValueError("the image holds no data (every pixel is zero or not finite)")
    background, noise = background_and_noise(data, valid)

    sig, given = _start_sigma_px(params, data.ndim)
    analysis = _analyze_once(data, params, sig, valid, background, noise, candidates_yx)
    if not given:
        estimated = False
        for _ in range(_SCALE_ITERATIONS):
            fitted = _fitted_sigma_px(analysis)
            if fitted is None:
                break
            estimated = True
            if np.all(np.abs(fitted / sig - 1.0) < _SCALE_TOLERANCE):
                break
            sig = fitted
            analysis = _analyze_once(data, params, sig, valid, background, noise, candidates_yx)
        unit = params.unit
        widths = analysis.expected_fwhm()
        text = f"lateral FWHM ≈ {widths[-1]:.4g} {unit}"
        if data.ndim == 3:
            text += f", axial ≈ {widths[0]:.4g} {unit}"
        if estimated:
            analysis.notes.append(f"Detection scale estimated from the beads: {text}.")
        else:
            analysis.warnings.append(
                f"Too few beads to estimate the detection scale; assumed {text}. "
                "Set 'Expected FWHM' if beads are missed."
            )

    if not params.physical and (params.bead_diameter_nm or params.na):
        analysis.warnings.append(
            "Pixel size is in px: bead-size correction and diffraction-limit comparison are skipped."
        )
    if analysis.n_discarded:
        analysis.notes.append(
            f"{analysis.n_discarded} faint peak(s) or side lobe(s) discarded before analysis."
        )
    if valid.mean() < 0.999:
        analysis.notes.append(
            f"{100 * (1 - valid.mean()):.0f} % of the voxels carry no data (zero padding) and are ignored."
        )
    return analysis


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Selection:
    """Which fitted beads enter the statistics. ``None`` means automatic.

    ``max_ellipticity`` ``None`` is median + k MAD of the well-fitted beads,
    at least ``min_auto_ellipticity`` (an astigmatic system has elliptical
    beads; a doublet stands out from them). FWHM ranges ``None`` are
    median +- k MAD of the beads passing the quality cuts.
    """

    fwhm_lat_range: tuple[float, float] | None = None
    fwhm_z_range: tuple[float, float] | None = None
    range_mad: float = 3.0
    max_ellipticity: float | None = None
    min_r2: float = 0.8
    min_auto_ellipticity: float = 1.3


def _robust_range(values: Sequence[float], k: float) -> tuple[float, float]:
    v = np.asarray([x for x in values if x is not None and np.isfinite(x)], dtype=np.float64)
    if v.size == 0:
        return (float("nan"), float("nan"))
    med = float(np.median(v))
    mad = MAD_SCALE * float(np.median(np.abs(v - med)))
    return (med - k * mad, med + k * mad)


def auto_range(analysis: BeadAnalysis, key: str, k: float = 3.0) -> tuple[float, float]:
    """``median +- k * MAD`` of ``key`` over fitted beads (NaN if none)."""
    return _robust_range([b[key] for b in analysis.fitted() if key in b], k)


def _passes_r2(bead: dict[str, Any], min_r2: float) -> bool:
    return bead.get("r2", -np.inf) >= min_r2 and bead.get("r2_z", 1.0) >= min_r2


def resolve_selection(analysis: BeadAnalysis, selection: Selection) -> Selection:
    """Fill the automatic bounds in from the data."""
    good = [b for b in analysis.fitted() if _passes_r2(b, selection.min_r2)]
    ellipticity = selection.max_ellipticity
    if ellipticity is None:
        hi = _robust_range([b["ellipticity"] for b in good], selection.range_mad)[1]
        ellipticity = max(selection.min_auto_ellipticity, hi) if np.isfinite(hi) else selection.min_auto_ellipticity
    good = [b for b in good if b["ellipticity"] <= ellipticity]
    lat = selection.fwhm_lat_range or _robust_range([b["fwhm_lat"] for b in good], selection.range_mad)
    z = selection.fwhm_z_range
    if analysis.ndim == 3 and z is None:
        z = _robust_range([b["fwhm_z"] for b in good], selection.range_mad)
    return replace(selection, fwhm_lat_range=lat, fwhm_z_range=z, max_ellipticity=ellipticity)


def rejection_reasons(analysis: BeadAnalysis, selection: Selection) -> list[str]:
    """Per bead: ``""`` if selected, else its pre-fit status or the first
    selection cut it fails (``REASON_*``)."""
    sel = resolve_selection(analysis, selection)
    lo, hi = sel.fwhm_lat_range
    reasons = []
    for bead in analysis.beads:
        if bead["status"] != STATUS_OK:
            reasons.append(bead["status"])
        elif not _passes_r2(bead, sel.min_r2):
            reasons.append(REASON_R2)
        elif bead["ellipticity"] > sel.max_ellipticity:
            reasons.append(REASON_ELLIPTICITY)
        elif not lo <= bead["fwhm_lat"] <= hi:
            reasons.append(REASON_FWHM)
        elif analysis.ndim == 3 and not sel.fwhm_z_range[0] <= bead["fwhm_z"] <= sel.fwhm_z_range[1]:
            reasons.append(REASON_FWHM)
        else:
            reasons.append("")
    return reasons


def select_beads(analysis: BeadAnalysis, selection: Selection) -> np.ndarray:
    """Boolean mask over ``analysis.beads``. Cheap; meant to be re-run freely."""
    return np.array([r == "" for r in rejection_reasons(analysis, selection)], dtype=bool)


def reason_label(reason: str) -> str:
    """Human-readable text of a status or rejection reason."""
    if not reason:
        return "selected"
    return STATUS_LABELS.get(reason) or REASON_LABELS.get(reason) or reason


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
def describe(values: Sequence[float]) -> dict[str, float]:
    """n, mean, std, SEM, median and (normal-scaled) MAD of the finite values."""
    v = np.asarray([x for x in values if x is not None and np.isfinite(x)], dtype=np.float64)
    nan = float("nan")
    if v.size == 0:
        return {"n": 0, "mean": nan, "std": nan, "sem": nan, "median": nan, "mad": nan}
    med = float(np.median(v))
    std = float(v.std(ddof=1)) if v.size > 1 else nan
    return {
        "n": int(v.size), "mean": float(v.mean()), "std": std,
        "sem": std / math.sqrt(v.size) if v.size > 1 else nan,
        "median": med, "mad": MAD_SCALE * float(np.median(np.abs(v - med))),
    }


def summary_keys(analysis: BeadAnalysis) -> list[str]:
    """The width keys a summary reports, in display order."""
    keys = [f"fwhm_{ax}" for ax in ("lat", *analysis.axes)]
    if analysis.params.physical and analysis.params.bead_diameter_nm:
        keys += [f"{k}_corr" for k in keys]
    keys += [f"fwhm_{ax}_hm" for ax in ("lat", *analysis.axes)]
    return keys


def summarize(analysis: BeadAnalysis, mask: np.ndarray, selection: Selection) -> dict[str, Any]:
    """Statistics over the selected beads plus counts, theory and warnings."""
    p = analysis.params
    chosen = [b for b, m in zip(analysis.beads, mask) if m]
    counts = {status: 0 for status in STATUSES}
    for bead in analysis.beads:
        counts[bead["status"]] = counts.get(bead["status"], 0) + 1
    rejected: dict[str, int] = {}
    for reason in rejection_reasons(analysis, selection):
        if reason:
            rejected[reason] = rejected.get(reason, 0) + 1
    sel = resolve_selection(analysis, selection)
    out: dict[str, Any] = {
        "unit": analysis.unit,
        "n_candidates": len(analysis.beads),
        "n_selected": len(chosen),
        "status_counts": counts,
        "rejected": rejected,
        "selection": {
            "fwhm_lat_range": sel.fwhm_lat_range, "fwhm_z_range": sel.fwhm_z_range,
            "max_ellipticity": sel.max_ellipticity, "min_r2": sel.min_r2,
        },
        "stats": {k: describe([b.get(k, np.nan) for b in chosen]) for k in summary_keys(analysis)},
        "warnings": list(analysis.warnings),
        "notes": list(analysis.notes),
    }
    if not chosen and analysis.beads:
        out["warnings"].append(
            "No bead passed the selection; relax it (Selection) or check the detection (Preview)."
        )
    if p.physical and p.na and p.wavelength_nm:
        lateral, axial = theoretical_fwhm_nm(p.na, p.wavelength_nm, p.refractive_index)
        out["theory_fwhm_nm"] = {"lat": lateral, "z": axial}
        suffix = "_corr" if p.bead_diameter_nm else ""
        out["ratio_to_theory"] = {
            ax: out["stats"][f"fwhm_{ax}{suffix}"]["median"] / out["theory_fwhm_nm"][ax]
            for ax in (("lat", "z") if analysis.ndim == 3 else ("lat",))
        }
    if p.physical and p.bead_diameter_nm:
        for ax in ("lat", *analysis.axes):
            measured = out["stats"][f"fwhm_{ax}"]["median"]
            corrected = out["stats"][f"fwhm_{ax}_corr"]["median"]
            if not np.isfinite(measured):
                continue
            if not np.isfinite(corrected):
                out["warnings"].append(f"Bead larger than the measured {ax} width; no correction possible.")
            elif (measured - corrected) / measured > p.correction_warn_fraction:
                out["warnings"].append(
                    f"Bead-size correction on {ax} is {100 * (measured - corrected) / measured:.0f} % "
                    "of the measured FWHM: the bead is not small against the PSF, so the "
                    "corrected value is unreliable."
                )
    return out


# --------------------------------------------------------------------------- #
# Averaged PSF
# --------------------------------------------------------------------------- #
@dataclass
class AveragedPSF:
    """Mean bead image and its refit. ``fwhm`` keys as in the bead table."""

    image: np.ndarray
    n: int
    bead_ids: list[int]
    fwhm: dict[str, float] = field(default_factory=dict)


def average_psf(
    data: np.ndarray,
    analysis: BeadAnalysis,
    mask: np.ndarray,
    half_px: Sequence[int] | None = None,
    fit: bool = True,
) -> AveragedPSF | None:
    """Align selected beads on their fitted centres and average them.

    Each crop is offset-subtracted and amplitude-normalised before averaging;
    no-data voxels are left out of the mean (a voxel no bead covers is 0).
    ``half_px`` overrides the crop half-size (aberration fitting wants a wider
    crop); beads whose crop would leave the image, contain another candidate
    or mostly lack data are skipped.
    """
    data = np.asarray(data)
    valid = valid_mask(data, analysis.params.zero_is_invalid)
    axes = analysis.axes
    half = np.asarray(half_px if half_px is not None else analysis.crop_half, dtype=int)
    margin = 2
    detections = np.array(
        [[b[f"{ax}_det"] for ax in axes] for b in analysis.beads], dtype=np.float64
    ).reshape(-1, len(axes))
    total = weight = None
    n, used = 0, []
    for bead, chosen in zip(analysis.beads, mask):
        if not chosen:
            continue
        center = np.array([bead[ax] for ax in axes])
        icenter = np.round(center).astype(int)
        lo = icenter - half - margin
        hi = icenter + half + margin + 1
        if np.any(lo < 0) or np.any(hi > np.array(data.shape)):
            continue
        inside = np.all(np.abs(detections - icenter) <= half + margin, axis=1)
        if inside.sum() > 1:
            continue
        box = tuple(slice(a, b) for a, b in zip(lo, hi))
        if valid[box].mean() < 0.75:
            continue
        shift = -(center - icenter)
        # No-data voxels are filled with the bead's background before the
        # spline shift, so they do not ring into their valid neighbours.
        crop = ndimage.shift(np.where(valid[box], data[box], bead["offset"]).astype(np.float64), shift,
                             order=3, mode="nearest")
        w = ndimage.shift(valid[box].astype(np.float64), shift, order=1, mode="nearest")
        trim = tuple(slice(margin, -margin) for _ in axes)
        crop, w = crop[trim], np.clip(w[trim], 0.0, 1.0)
        w = np.where(w > 0.99, 1.0, 0.0)
        crop = (crop - bead["offset"]) / bead["amp"]
        total = crop * w if total is None else total + crop * w
        weight = w if weight is None else weight + w
        n += 1
        used.append(int(bead["id"]))
    if n == 0:
        return None
    image = np.where(weight > 0, total / np.maximum(weight, 1e-12), 0.0)
    result = AveragedPSF(image, n, used)
    if not fit:
        return result
    px = np.asarray(analysis.params.pixel_size, dtype=np.float64)
    if analysis.ndim == 3:
        r_xy = max(1, int(round(0.5 * float(np.mean(analysis.expected_sigma_px[1:])))))
        vf = fit_volume(result.image, analysis.params.fit_mode_3d, r_xy)
        fit_ = None if vf is None else (vf.center, vf.sigma, vf.offset, vf.focus)
    else:
        gf = fit_gaussian_nd(result.image)
        fit_ = None if gf is None else (gf.center, gf.sigma, gf.offset, None)
    if fit_ is None:
        return result
    center, sigma, offset, focus = fit_
    p = analysis.params
    for ax, s, size in zip(axes, sigma, px):
        result.fwhm[f"fwhm_{ax}"] = float(FWHM_FACTOR * s * size)
    result.fwhm["fwhm_lat"] = 0.5 * (result.fwhm["fwhm_x"] + result.fwhm["fwhm_y"])
    if p.physical and p.bead_diameter_nm:
        for key in list(result.fwhm):
            result.fwhm[f"{key}_corr"] = correct_fwhm_for_bead(
                result.fwhm[key], p.bead_diameter_nm, p.bead_labeling
            )
    hm = half_max_widths(result.image, center, offset, focus=focus)
    for ax, w, size in zip(axes, hm, px):
        result.fwhm[f"fwhm_{ax}_hm"] = float(w * size)
    result.fwhm["fwhm_lat_hm"] = 0.5 * (result.fwhm["fwhm_x_hm"] + result.fwhm["fwhm_y_hm"])
    return result


# --------------------------------------------------------------------------- #
# Field dependence
# --------------------------------------------------------------------------- #
def field_trend(analysis: BeadAnalysis, mask: np.ndarray, key: str = "fwhm_lat") -> dict[str, float] | None:
    """Linear trend ``key = a + b*x + c*y`` across the field, slopes per pixel
    and the total change over the field of view. ``None`` with < 4 beads."""
    chosen = [b for b, m in zip(analysis.beads, mask) if m and key in b]
    if len(chosen) < 4:
        return None
    x = np.array([b["x"] for b in chosen])
    y = np.array([b["y"] for b in chosen])
    v = np.array([b[key] for b in chosen])
    design = np.column_stack([np.ones_like(x), x, y])
    coef, *_ = np.linalg.lstsq(design, v, rcond=None)
    height, width = analysis.shape[-2:]
    return {
        "offset": float(coef[0]), "per_px_x": float(coef[1]), "per_px_y": float(coef[2]),
        "change_over_fov": float(abs(coef[1]) * width + abs(coef[2]) * height),
    }


def focal_surface(
    analysis: BeadAnalysis, mask: np.ndarray, quadratic: bool = True, notes: list[str] | None = None
) -> dict[str, float] | None:
    """Fit the beads' focal positions ``z(x, y)``: a plane (sample/stage tilt)
    plus an optional radial quadratic term (field curvature).

    Assumes the beads lie on a flat surface (e.g. dried on the coverslip).
    3-D, physically calibrated data only (a tilt needs the z step and the
    lateral pixel in the same unit); ``None`` otherwise, with too few beads,
    or when the beads are evidently not on one surface (the fit leaves more
    than an axial FWHM unexplained), which ``notes`` is then told.
    """
    if analysis.ndim != 3 or not analysis.params.physical:
        return None
    chosen = [b for b, m in zip(analysis.beads, mask) if m]
    if len(chosen) < (4 if quadratic else 3):
        return None
    pz, py, px = analysis.params.pixel_size
    x = np.array([b["x"] for b in chosen]) * px
    y = np.array([b["y"] for b in chosen]) * py
    z = np.array([b["z"] for b in chosen]) * pz
    xc, yc = x.mean(), y.mean()
    columns = [np.ones_like(x), x - xc, y - yc]
    if quadratic:
        columns.append((x - xc) ** 2 + (y - yc) ** 2)
    design = np.column_stack(columns)
    coef, *_ = np.linalg.lstsq(design, z, rcond=None)
    residual = z - design @ coef
    residual_rms = float(np.sqrt(np.mean(residual**2)))
    axial = float(np.median([b["fwhm_z"] for b in chosen]))
    if residual_rms > axial:
        if notes is not None:
            notes.append(
                f"The beads are spread over {float(np.ptp(z)) / 1000:.1f} µm in z, not on one surface: "
                "no focal-plane tilt or field curvature reported."
            )
        return None
    out = {
        "tilt_x_mrad": 1000.0 * float(coef[1]),
        "tilt_y_mrad": 1000.0 * float(coef[2]),
        "tilt_mrad": 1000.0 * float(math.hypot(coef[1], coef[2])),
        "residual_rms": residual_rms,
        "n": len(chosen),
    }
    if quadratic:
        r_max = float(np.max(np.hypot(x - xc, y - yc)))
        out["curvature_sag_at_edge"] = float(coef[3] * r_max**2)
    return out


# --------------------------------------------------------------------------- #
# ROI candidates
# --------------------------------------------------------------------------- #
def roi_candidates(image: np.ndarray, rois: Sequence[Any], sigma_px: float = 1.0) -> np.ndarray:
    """One ``(y, x)`` candidate per ROI: the smoothed maximum inside the ROI."""
    from imswitch.imcommon.algorithms.roi_geometry import roi_mask_local

    smooth = ndimage.gaussian_filter(np.asarray(image, dtype=np.float64), sigma_px)
    points = []
    for roi in rois:
        local, slices = roi_mask_local(roi, smooth.shape)
        if local.size == 0 or not local.any():
            continue
        region = np.where(local, smooth[slices], -np.inf)
        iy, ix = np.unravel_index(int(np.argmax(region)), region.shape)
        points.append((iy + slices[0].start, ix + slices[1].start))
    return np.array(points, dtype=int).reshape(-1, 2)


def analyze_whole_image(data: np.ndarray, params: BeadPSFParams) -> BeadAnalysis:
    """Fit the whole 2-D image or 3-D stack as a single PSF (the v1 behaviour
    without ROIs). No detection, no pre-filtering; one bead, possibly failed."""
    data = np.asarray(data)
    px = np.asarray(params.pixel_size, dtype=np.float64)
    if data.ndim not in (2, 3) or len(px) != data.ndim:
        raise ValueError(f"need a (Y, X) or (Z, Y, X) array and matching pixel size, got {data.shape}")
    sig, _given = _start_sigma_px(params, data.ndim)
    analysis = BeadAnalysis(
        [], params, data.ndim, tuple(data.shape), tuple(float(s) for s in sig),
        tuple(int(n) // 2 for n in data.shape), "full_image",
    )
    center = [n // 2 for n in data.shape]
    bead: dict[str, Any] = {"id": 0, "status": STATUS_OK, "y_det": center[-2], "x_det": center[-1],
                            "amp_est": float("nan")}
    valid = valid_mask(data, params.zero_is_invalid)
    if data.ndim == 3:
        bead["z_det"] = center[0]
        r_xy = max(1, int(round(0.5 * float(np.mean(sig[1:])))))
        vf = fit_volume(data.astype(np.float64), params.fit_mode_3d, r_xy, valid)
        fit = None if vf is None else (vf.center, vf.sigma, vf.sigma_err, vf.amp, vf.offset, vf.r2_lateral, vf.r2_axial)
        focus = None if vf is None else vf.focus
    else:
        gf = fit_gaussian_nd(data, valid)
        fit = None if gf is None else (gf.center, gf.sigma, gf.sigma_err, gf.amp, gf.offset, gf.r2, None)
        focus = None
    if fit is None:
        bead["status"] = STATUS_FIT_FAILED
        analysis.beads.append(bead)
        return analysis
    hm = half_max_widths(data, fit[0], fit[4], valid, focus)
    _record_fit(bead, analysis, *fit, hm)
    analysis.beads.append(bead)
    return analysis
