"""Bead-based PSF / resolution analysis for ImProcess.

Pure numpy/scipy, no Qt. The ``psf-resolution`` processor is a thin wrapper
around :func:`analyze_beads`, :func:`select_beads`, :func:`summarize` and
:func:`average_psf`; the ``psf-bead-select`` processor re-runs only the
cheap selection and summary on an existing analysis.

Pipeline
    1. candidates  LoG blob detection on the 2-D image (the max projection of
                   a 3-D stack), or one candidate per ROI Manager ROI
    2. pre-filter  border / crowded / saturated / too bright (aggregates);
                   side lobes and defocus rings of brighter beads, and faint
                   noise peaks, are discarded before analysis
    3. fit         2-D: axis-aligned Gaussian + offset per bead crop
                   3-D: ``separable`` (axial 1-D profile, lateral 2-D fit in
                   the focal plane) or ``full`` (axis-aligned 3-D Gaussian)
    4. correct     sigma_psf^2 = sigma_meas^2 - sigma_bead^2. Variances add
                   exactly under convolution; for a uniform sphere of
                   diameter d, sigma_bead^2 = d^2/20 (volume labelled) or
                   d^2/12 (shell labelled)
    5. select      lateral / axial FWHM range (default median +- k*MAD),
                   ellipticity, R^2
    6. summarize   mean / std / SEM / median / MAD, comparison with the
                   widefield diffraction limit
    7. average     sub-pixel aligned, background-subtracted, amplitude-
                   normalised mean bead, refitted with the same model

Units. Arrays are ``(Y, X)`` or ``(Z, Y, X)`` and every width is reported in
``analysis.unit``: ``"nm"`` when the data carry a physical pixel size, ``"px"``
otherwise. The physical extras (bead-size correction, diffraction-limit
comparison, aberrations) need nanometres and are skipped, with a warning,
for pixel-unit data.
"""

from __future__ import annotations

import math
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

BEAD_LABELINGS = ("volume", "shell")
FIT_MODES_3D = ("separable", "full")


# --------------------------------------------------------------------------- #
# Parameters
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BeadPSFParams:
    """Settings of one bead analysis.

    ``pixel_size`` is ``(y, x)`` or ``(z, y, x)`` in ``unit``. Every physical
    input (bead diameter, wavelength, expected FWHM) is in nanometres and is
    ignored when ``unit`` is ``"px"``.
    """

    pixel_size: tuple[float, ...]
    unit: str = "nm"
    bead_diameter_nm: float | None = None
    bead_labeling: str = "volume"
    expected_fwhm_nm: tuple[float, ...] | None = None  # (lateral, axial); detection scale
    na: float | None = None
    wavelength_nm: float | None = None
    refractive_index: float = 1.515
    detection_threshold: float = 5.0          # LoG response > median + k * MAD
    isolation_factor: float = 3.0             # min neighbour distance, in expected FWHMs
    crop_factor: float = 4.0                  # crop half-size, in expected sigmas
    saturation_value: float | None = None     # default: dtype max for integer data
    brightness_outlier_factor: float = 1.8    # amplitude > f * median -> aggregate
    satellite_ratio: float = 2.5              # neighbour > f x brighter -> side lobe, dropped
    min_amp_fraction: float = 0.1             # peaks < f x 90th-percentile amplitude dropped
    fit_mode_3d: str = "separable"
    correction_warn_fraction: float = 0.3     # warn if the bead correction exceeds 30 %

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


def fit_gaussian_nd(data: np.ndarray) -> GaussianFit | None:
    """Fit an axis-aligned Gaussian + constant offset; ``None`` if the fit fails."""
    data = np.asarray(data, dtype=np.float64)
    ndim = data.ndim
    grids = np.indices(data.shape, dtype=np.float64)
    coords = tuple(g.ravel() for g in grids)

    b0 = float(np.percentile(data, 10))
    w = np.clip(data - b0, 0, None)
    total = float(w.sum())
    if not np.isfinite(total) or total <= 0:
        return None
    mu0 = [float((w * g).sum() / total) for g in grids]
    s0 = [float(np.sqrt((w * (g - m) ** 2).sum() / total)) for g, m in zip(grids, mu0)]
    a0 = float(data.max() - b0)

    lower = [0.0] + [0.0] * ndim + [0.3] * ndim + [-np.inf]
    upper = [np.inf] + [n - 1.0 for n in data.shape] + [float(n) for n in data.shape] + [np.inf]
    p0 = [a0, *mu0, *s0, b0]
    eps = 1e-6
    p0 = [
        min(max(v, lo + eps), hi - eps) if np.isfinite(hi) else max(v, lo + eps)
        for v, lo, hi in zip(p0, lower, upper)
    ]
    try:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", optimize.OptimizeWarning)
            popt, pcov = optimize.curve_fit(
                _gauss_nd(ndim), coords, data.ravel(), p0=p0, bounds=(lower, upper), maxfev=10000
            )
    except (RuntimeError, ValueError):
        return None

    resid = data.ravel() - _gauss_nd(ndim)(coords, *popt)
    ss_tot = float(((data - data.mean()) ** 2).sum())
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


def fit_volume(crop: np.ndarray, mode: str = "separable", r_xy: int = 1) -> VolumeFit | None:
    """Fit a 3-D bead crop with the ``separable`` or ``full`` model."""
    if mode == "full":
        f = fit_gaussian_nd(crop)
        if f is None:
            return None
        return VolumeFit(f.center, f.sigma, f.sigma_err, f.amp, f.offset, f.r2, f.r2)
    if mode != "separable":
        raise ValueError(f"unknown 3-D fit mode {mode!r}; expected one of {FIT_MODES_3D}")
    nz, ny, nx = crop.shape
    yc, xc = ny // 2, nx // 2
    column = crop[:, yc - r_xy:yc + r_xy + 1, xc - r_xy:xc + r_xy + 1].mean(axis=(1, 2))
    fz = fit_gaussian_nd(column)
    if fz is None:
        return None
    focus = int(np.clip(round(float(fz.center[0])), 0, nz - 1))
    fl = fit_gaussian_nd(crop[focus])
    if fl is None:
        return None
    return VolumeFit(
        center=np.array([fz.center[0], *fl.center]),
        sigma=np.array([fz.sigma[0], *fl.sigma]),
        sigma_err=np.array([fz.sigma_err[0], *fl.sigma_err]),
        amp=fl.amp,
        offset=fl.offset,
        r2_lateral=fl.r2,
        r2_axial=fz.r2,
    )


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
def detect_beads(
    image: np.ndarray, sigma_yx: Sequence[float], threshold: float, min_distance: float
) -> tuple[np.ndarray, np.ndarray]:
    """LoG blob detection on a 2-D image.

    Returns ``(yx, peak)``: integer ``(N, 2)`` positions and the Gaussian-
    smoothed intensity at each.
    """
    img = np.asarray(image, dtype=np.float64)
    sy, sx = (float(s) for s in sigma_yx)
    response = -ndimage.gaussian_laplace(img, sigma=(sy, sx)) * (sy * sx)
    med = float(np.median(response))
    mad = MAD_SCALE * float(np.median(np.abs(response - med)))
    if mad <= 0:
        mad = float(response.std()) or 1.0
    size = 2 * max(1, int(round(min_distance))) + 1
    peaks = (response == ndimage.maximum_filter(response, size=size, mode="nearest")) & (
        response > med + threshold * mad
    )
    ys, xs = np.nonzero(peaks)
    smooth = ndimage.gaussian_filter(img, (sy, sx))
    return np.column_stack([ys, xs]).astype(int), smooth[ys, xs]


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
@dataclass
class BeadAnalysis:
    """All candidate beads of one analysis, fitted or not.

    ``beads`` holds one dict per candidate with at least ``id``, ``status``,
    ``y_det``/``x_det`` (detection pixel); fitted beads add ``y``/``x``
    (and ``z``) fitted pixel positions and ``fwhm_*`` widths in ``unit``.
    """

    beads: list[dict[str, Any]]
    params: BeadPSFParams
    ndim: int
    shape: tuple[int, ...]
    expected_sigma_px: tuple[float, ...]
    crop_half: tuple[int, ...]
    source: str = "auto"
    warnings: list[str] = field(default_factory=list)

    @property
    def unit(self) -> str:
        return self.params.unit

    @property
    def axes(self) -> tuple[str, ...]:
        return ("z", "y", "x") if self.ndim == 3 else ("y", "x")

    def fitted(self) -> list[dict[str, Any]]:
        return [b for b in self.beads if b["status"] == STATUS_OK]


def expected_fwhm_nm(params: BeadPSFParams) -> tuple[float | None, float | None]:
    """Expected *measured* FWHM ``(lateral, axial)`` in nm, bead size included."""
    lateral = axial = None
    if params.expected_fwhm_nm:
        lateral = float(params.expected_fwhm_nm[0])
        if len(params.expected_fwhm_nm) > 1 and params.expected_fwhm_nm[1]:
            axial = float(params.expected_fwhm_nm[1])
    elif params.na and params.wavelength_nm:
        lateral, axial = theoretical_fwhm_nm(params.na, params.wavelength_nm, params.refractive_index)
    if lateral is not None and params.bead_diameter_nm:
        bead = FWHM_FACTOR * bead_sigma_nm(params.bead_diameter_nm, params.bead_labeling)
        lateral = math.hypot(lateral, bead)
        if axial is not None and np.isfinite(axial):
            axial = math.hypot(axial, bead)
    return lateral, axial


def _expected_sigma_px(params: BeadPSFParams, ndim: int, warn: list[str]) -> np.ndarray:
    px = np.asarray(params.pixel_size, dtype=np.float64)
    lateral, axial = expected_fwhm_nm(params) if params.physical else (None, None)
    if lateral is None:
        sig_lat = np.array([2.0, 2.0])
        warn.append("No expected FWHM or NA/wavelength: detection assumes a 2 px sigma.")
        lateral_unit = FWHM_FACTOR * 2.0 * float(px[-1])
    else:
        sig_lat = (lateral / FWHM_FACTOR) / px[-2:]
        lateral_unit = lateral
    if ndim == 2:
        return sig_lat
    if axial is None or not np.isfinite(axial):
        axial = 3.0 * lateral_unit
    return np.array([(axial / FWHM_FACTOR) / px[0], *sig_lat])


def _record_fit(bead: dict[str, Any], analysis: BeadAnalysis, center_px: np.ndarray, sigma: np.ndarray,
                sigma_err: np.ndarray, amp: float, offset: float, r2: float, r2_z: float | None) -> None:
    """Write one fit into a bead record: positions in px, widths in the analysis unit."""
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
    if params.physical and params.bead_diameter_nm:
        for ax in (*analysis.axes, "lat"):
            bead[f"fwhm_{ax}_corr"] = correct_fwhm_for_bead(
                bead[f"fwhm_{ax}"], params.bead_diameter_nm, params.bead_labeling
            )


def analyze_beads(
    data: np.ndarray,
    params: BeadPSFParams,
    candidates_yx: np.ndarray | None = None,
) -> BeadAnalysis:
    """Detect (or take) candidate beads, pre-filter and fit them.

    ``candidates_yx`` replaces automatic detection with given ``(N, 2)`` pixel
    positions (e.g. one per ROI); the side-lobe filter is then not applied,
    as the caller chose the positions deliberately.
    """
    data = np.asarray(data)
    if data.ndim not in (2, 3):
        raise ValueError(f"bead analysis needs a (Y, X) or (Z, Y, X) array, got shape {data.shape}")
    px = np.asarray(params.pixel_size, dtype=np.float64)
    if len(px) != data.ndim:
        raise ValueError(f"pixel_size needs {data.ndim} entries for a {data.ndim}-D array")
    if params.fit_mode_3d not in FIT_MODES_3D:
        raise ValueError(f"unknown 3-D fit mode {params.fit_mode_3d!r}")
    is3d = data.ndim == 3
    warn: list[str] = []
    if not params.physical and (params.bead_diameter_nm or params.na):
        warn.append(
            "Pixel size is in px: bead-size correction and diffraction-limit comparison are skipped."
        )

    sig = _expected_sigma_px(params, data.ndim, warn)
    sig_lat = sig[-2:]
    half = np.ceil(params.crop_factor * sig).astype(int)
    hz, hy, hx = (int(v) for v in half) if is3d else (0, *(int(v) for v in half))
    lat_fwhm_unit = FWHM_FACTOR * float(np.mean(sig_lat * px[-2:]))

    image = data.max(axis=0) if is3d else data
    source = "auto" if candidates_yx is None else "rois"
    if candidates_yx is None:
        yx, peak = detect_beads(
            image, sig_lat, params.detection_threshold, min_distance=2.0 * float(sig_lat.mean())
        )
    else:
        yx = np.asarray(candidates_yx, dtype=int).reshape(-1, 2)
        smooth = ndimage.gaussian_filter(np.asarray(image, dtype=np.float64), tuple(sig_lat))
        inside = (
            (yx[:, 0] >= 0) & (yx[:, 0] < image.shape[0]) & (yx[:, 1] >= 0) & (yx[:, 1] < image.shape[1])
        )
        yx = yx[inside]
        peak = smooth[yx[:, 0], yx[:, 1]] if len(yx) else np.empty(0)

    analysis = BeadAnalysis(
        [], params, data.ndim, tuple(data.shape), tuple(float(s) for s in sig),
        tuple(int(h) for h in half), source, warn,
    )
    if len(yx) == 0:
        warn.append("No beads found.")
        return analysis

    background = float(np.median(image))
    amp_est = peak - background
    min_sep = params.isolation_factor * lat_fwhm_unit
    if candidates_yx is None:
        keep = amp_est >= params.min_amp_fraction * float(np.percentile(amp_est, 90))
        tree = cKDTree(yx * px[-2:])
        for i, point in enumerate(yx * px[-2:]):
            if keep[i]:
                nb = [j for j in tree.query_ball_point(point, min_sep) if j != i]
                if nb and amp_est[nb].max() > params.satellite_ratio * amp_est[i]:
                    keep[i] = False
        dropped = int((~keep).sum())
        if dropped:
            warn.append(f"{dropped} faint peak(s) or side lobe(s) discarded before analysis.")
        yx, amp_est = yx[keep], amp_est[keep]

    tree = cKDTree(yx * px[-2:])
    n_neigh = np.array([len(tree.query_ball_point(p, min_sep)) - 1 for p in yx * px[-2:]])

    saturation = params.saturation_value
    if saturation is None and np.issubdtype(data.dtype, np.integer):
        saturation = float(np.iinfo(data.dtype).max)
    height, width = image.shape
    nz = data.shape[0] if is3d else 1
    zprofile = (
        ndimage.gaussian_filter1d(data.astype(np.float64), sigma=max(float(sig[0]), 0.5), axis=0)
        if is3d else None
    )

    crops: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    for i, (y, x) in enumerate(yx):
        bead: dict[str, Any] = {
            "id": i, "status": STATUS_OK, "y_det": int(y), "x_det": int(x), "amp_est": float(amp_est[i]),
        }
        z = int(np.argmax(zprofile[:, y, x])) if is3d else 0
        if is3d:
            bead["z_det"] = z
        out_lateral = y - hy < 0 or y + hy >= height or x - hx < 0 or x + hx >= width
        out_axial = is3d and (z - hz < 0 or z + hz >= nz)
        if out_lateral or out_axial:
            bead["status"] = STATUS_BORDER
        elif n_neigh[i] > 0:
            bead["status"] = STATUS_CROWDED
        else:
            z_slice = (slice(z - hz, z + hz + 1),) if is3d else ()
            crop = data[z_slice + (slice(y - hy, y + hy + 1), slice(x - hx, x + hx + 1))]
            origin = np.array(([z - hz] if is3d else []) + [y - hy, x - hx])
            crops[i] = (crop, origin)
            if saturation is not None and float(crop.max()) >= saturation:
                bead["status"] = STATUS_SATURATED
        analysis.beads.append(bead)

    ok_amp = np.array([b["amp_est"] for b in analysis.beads if b["status"] == STATUS_OK])
    if ok_amp.size:
        limit = params.brightness_outlier_factor * float(np.median(ok_amp))
        for bead in analysis.beads:
            if bead["status"] == STATUS_OK and bead["amp_est"] > limit:
                bead["status"] = STATUS_BRIGHT

    r_xy = max(1, int(round(0.5 * float(sig_lat.mean()))))
    for bead in analysis.beads:
        if bead["status"] != STATUS_OK:
            continue
        crop, origin = crops[bead["id"]]
        if is3d:
            vf = fit_volume(crop, params.fit_mode_3d, r_xy)
            if vf is None:
                bead["status"] = STATUS_FIT_FAILED
                continue
            center, sigma, sigma_err = vf.center, vf.sigma, vf.sigma_err
            amp, offset, r2, r2_z = vf.amp, vf.offset, vf.r2_lateral, vf.r2_axial
        else:
            gf = fit_gaussian_nd(crop)
            if gf is None:
                bead["status"] = STATUS_FIT_FAILED
                continue
            center, sigma, sigma_err = gf.center, gf.sigma, gf.sigma_err
            amp, offset, r2, r2_z = gf.amp, gf.offset, gf.r2, None
        _record_fit(bead, analysis, center + origin, sigma, sigma_err, amp, offset, r2, r2_z)
    return analysis


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Selection:
    """Which fitted beads enter the statistics. ``None`` ranges mean automatic."""

    fwhm_lat_range: tuple[float, float] | None = None
    fwhm_z_range: tuple[float, float] | None = None
    range_mad: float = 3.0
    max_ellipticity: float = 1.3
    min_r2: float = 0.9


def auto_range(analysis: BeadAnalysis, key: str, k: float = 3.0) -> tuple[float, float]:
    """``median +- k * MAD`` of ``key`` over fitted beads (NaN if none)."""
    values = np.array([b[key] for b in analysis.fitted() if key in b], dtype=np.float64)
    if values.size == 0:
        return (float("nan"), float("nan"))
    med = float(np.median(values))
    mad = MAD_SCALE * float(np.median(np.abs(values - med)))
    return (med - k * mad, med + k * mad)


def resolve_selection(analysis: BeadAnalysis, selection: Selection) -> Selection:
    """Fill automatic ranges in from the data."""
    lat = selection.fwhm_lat_range or auto_range(analysis, "fwhm_lat", selection.range_mad)
    z = selection.fwhm_z_range
    if analysis.ndim == 3 and z is None:
        z = auto_range(analysis, "fwhm_z", selection.range_mad)
    return replace(selection, fwhm_lat_range=lat, fwhm_z_range=z)


def select_beads(analysis: BeadAnalysis, selection: Selection) -> np.ndarray:
    """Boolean mask over ``analysis.beads``. Cheap; meant to be re-run freely."""
    sel = resolve_selection(analysis, selection)
    lo, hi = sel.fwhm_lat_range
    mask = np.zeros(len(analysis.beads), dtype=bool)
    for i, bead in enumerate(analysis.beads):
        if bead["status"] != STATUS_OK:
            continue
        ok = lo <= bead["fwhm_lat"] <= hi
        ok = ok and bead["ellipticity"] <= sel.max_ellipticity and bead["r2"] >= sel.min_r2
        if analysis.ndim == 3:
            zlo, zhi = sel.fwhm_z_range
            ok = ok and zlo <= bead["fwhm_z"] <= zhi and bead.get("r2_z", 1.0) >= sel.min_r2
        mask[i] = ok
    return mask


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
    return keys


def summarize(analysis: BeadAnalysis, mask: np.ndarray, selection: Selection) -> dict[str, Any]:
    """Statistics over the selected beads plus counts, theory and warnings."""
    p = analysis.params
    chosen = [b for b, m in zip(analysis.beads, mask) if m]
    counts = {status: 0 for status in STATUSES}
    for bead in analysis.beads:
        counts[bead["status"]] = counts.get(bead["status"], 0) + 1
    sel = resolve_selection(analysis, selection)
    out: dict[str, Any] = {
        "unit": analysis.unit,
        "n_candidates": len(analysis.beads),
        "n_selected": len(chosen),
        "status_counts": counts,
        "selection": {
            "fwhm_lat_range": sel.fwhm_lat_range, "fwhm_z_range": sel.fwhm_z_range,
            "max_ellipticity": sel.max_ellipticity, "min_r2": sel.min_r2,
        },
        "stats": {k: describe([b.get(k, np.nan) for b in chosen]) for k in summary_keys(analysis)},
        "warnings": list(analysis.warnings),
    }
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

    Each crop is offset-subtracted and amplitude-normalised before averaging.
    ``half_px`` overrides the crop half-size (aberration fitting wants a wider
    crop); beads whose crop would leave the image or contain another candidate
    are skipped.
    """
    data = np.asarray(data, dtype=np.float64)
    axes = analysis.axes
    half = np.asarray(half_px if half_px is not None else analysis.crop_half, dtype=int)
    margin = 2
    detections = np.array([[b["y_det"], b["x_det"]] for b in analysis.beads]).reshape(-1, 2)
    total, n, used = None, 0, []
    for bead, chosen in zip(analysis.beads, mask):
        if not chosen:
            continue
        center = np.array([bead[ax] for ax in axes])
        icenter = np.round(center).astype(int)
        lo = icenter - half - margin
        hi = icenter + half + margin + 1
        if np.any(lo < 0) or np.any(hi > np.array(data.shape)):
            continue
        inside = np.all(np.abs(detections - icenter[-2:]) <= half[-2:] + margin, axis=1)
        if inside.sum() > 1:
            continue
        crop = data[tuple(slice(a, b) for a, b in zip(lo, hi))]
        crop = ndimage.shift(crop, -(center - icenter), order=3, mode="nearest")
        crop = crop[tuple(slice(margin, -margin) for _ in axes)]
        crop = (crop - bead["offset"]) / bead["amp"]
        total = crop if total is None else total + crop
        n += 1
        used.append(int(bead["id"]))
    if n == 0:
        return None
    result = AveragedPSF(total / n, n, used)
    if not fit:
        return result
    px = np.asarray(analysis.params.pixel_size, dtype=np.float64)
    if analysis.ndim == 3:
        r_xy = max(1, int(round(0.5 * float(np.mean(analysis.expected_sigma_px[1:])))))
        vf = fit_volume(result.image, analysis.params.fit_mode_3d, r_xy)
        sigma = None if vf is None else vf.sigma
    else:
        gf = fit_gaussian_nd(result.image)
        sigma = None if gf is None else gf.sigma
    if sigma is None:
        return result
    p = analysis.params
    for ax, s, size in zip(axes, sigma, px):
        result.fwhm[f"fwhm_{ax}"] = float(FWHM_FACTOR * s * size)
    result.fwhm["fwhm_lat"] = 0.5 * (result.fwhm["fwhm_x"] + result.fwhm["fwhm_y"])
    if p.physical and p.bead_diameter_nm:
        for key in list(result.fwhm):
            result.fwhm[f"{key}_corr"] = correct_fwhm_for_bead(
                result.fwhm[key], p.bead_diameter_nm, p.bead_labeling
            )
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


def focal_surface(analysis: BeadAnalysis, mask: np.ndarray, quadratic: bool = True) -> dict[str, float] | None:
    """Fit the beads' focal positions ``z(x, y)``: a plane (sample/stage tilt)
    plus an optional radial quadratic term (field curvature).

    Assumes the beads lie on a flat surface (e.g. dried on the coverslip).
    3-D, physically calibrated data only (a tilt needs the z step and the
    lateral pixel in the same unit); ``None`` otherwise or with too few beads.
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
    out = {
        "tilt_x_mrad": 1000.0 * float(coef[1]),
        "tilt_y_mrad": 1000.0 * float(coef[2]),
        "tilt_mrad": 1000.0 * float(math.hypot(coef[1], coef[2])),
        "residual_rms": float(np.sqrt(np.mean(residual**2))),
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
    warn: list[str] = []
    sig = _expected_sigma_px(params, data.ndim, [])
    analysis = BeadAnalysis(
        [], params, data.ndim, tuple(data.shape), tuple(float(s) for s in sig),
        tuple(int(n) // 2 for n in data.shape), "full_image", warn,
    )
    center = [n // 2 for n in data.shape]
    bead: dict[str, Any] = {"id": 0, "status": STATUS_OK, "y_det": center[-2], "x_det": center[-1],
                            "amp_est": float("nan")}
    if data.ndim == 3:
        bead["z_det"] = center[0]
        r_xy = max(1, int(round(0.5 * float(np.mean(sig[1:])))))
        vf = fit_volume(data.astype(np.float64), params.fit_mode_3d, r_xy)
        fit = None if vf is None else (vf.center, vf.sigma, vf.sigma_err, vf.amp, vf.offset, vf.r2_lateral, vf.r2_axial)
    else:
        gf = fit_gaussian_nd(data)
        fit = None if gf is None else (gf.center, gf.sigma, gf.sigma_err, gf.amp, gf.offset, gf.r2, None)
    if fit is None:
        bead["status"] = STATUS_FIT_FAILED
        analysis.beads.append(bead)
        return analysis
    _record_fit(bead, analysis, *fit)
    analysis.beads.append(bead)
    return analysis
