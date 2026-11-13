"""Bead FWHM: find isolated beads by threshold, fit a 2D Gaussian to each, per plane.

For every 2D plane of an image or stack:

1. **Find.** The plane is smoothed (Gaussian, ``sigma`` px) and every local
   maximum above the threshold is a candidate. ``relative`` thresholds are in
   multiples of the plane's robust noise above its median, ``median + k *
   1.4826 * MAD`` of the smoothed plane -- the same idea as the spot finder's,
   so one number carries over between planes of different brightness.
   ``absolute`` thresholds are in the image's own units. Touching maxima (a
   flat or saturated top) count as one candidate.
2. **Isolate.** A candidate whose fit window would leave the plane is
   ``edge``; one with another candidate inside its fit window is ``crowded``,
   and so is that neighbour -- two beads in one window bias both fits.
3. **Fit.** A non-rotated 2D Gaussian plus constant background is fitted to
   the raw (not smoothed) pixels of the window
   (:func:`~imswitch.improcess.analysis.psf_resolution.fit_psf`). A fit is
   kept only if it converged with its centre near the candidate, is wider
   than a hot pixel, narrow enough for its window (FWHM at most
   :data:`MAX_FWHM_FRACTION` of it), and explains the window well
   (``R^2 >= min_r2``).

Each kept bead reports FWHM in x and y (``2.3548 sigma``), their mean, and
an **SNR = fitted peak amplitude / RMS of the fit residuals** in its window:
the bead's height above its local background over the noise around it. That
stays defined where a background-only estimate does not (photon counting
with an empty background has no background spread at all), and it includes
shot noise on the bead itself.

No Qt: numpy/scipy in, plain records out.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

import numpy as np
from scipy.ndimage import center_of_mass, gaussian_filter, label, maximum_filter

from .psf_resolution import FWHM_FACTOR, fit_psf

THRESHOLD_MODES = ("relative", "absolute")

STATUS_EDGE = "edge"
STATUS_CROWDED = "crowded"
STATUS_FIT_FAILED = "fit failed"
STATUS_OFF_CENTRE = "off-centre"
STATUS_TOO_NARROW = "too narrow"
STATUS_TOO_WIDE = "too wide for window"
STATUS_POOR_FIT = "poor fit"
REJECTION_REASONS = (
    STATUS_EDGE, STATUS_CROWDED, STATUS_FIT_FAILED, STATUS_OFF_CENTRE,
    STATUS_TOO_NARROW, STATUS_TOO_WIDE, STATUS_POOR_FIT,
)

#: A fit whose FWHM exceeds this fraction of the window has too little
#: background around it to pin the background down, and reads too narrow.
MAX_FWHM_FRACTION = 0.6
#: Below this sigma (px) the "bead" is one pixel: a hot pixel or a spike.
MIN_SIGMA_PX = 0.3

_MAD_TO_SIGMA = 1.4826


@dataclass(frozen=True)
class BeadParams:
    """Detection and fit settings."""

    threshold: float = 10.0
    threshold_mode: str = "relative"
    window: int = 11
    sigma: float = 1.0
    min_r2: float = 0.8

    @classmethod
    def from_params(cls, params: Mapping[str, Any] | None) -> "BeadParams":
        params = dict(params or {})
        mode = str(params.get("threshold_mode", cls.threshold_mode))
        if mode not in THRESHOLD_MODES:
            raise ValueError(f"threshold_mode must be one of {THRESHOLD_MODES}, got {mode!r}")
        window = int(params.get("window", cls.window))
        if window < 5:
            raise ValueError(f"The fit window must be at least 5 px, got {window}")
        sigma = float(params.get("sigma", cls.sigma))
        if sigma < 0:
            raise ValueError(f"The smoothing sigma must not be negative, got {sigma}")
        min_r2 = float(params.get("min_r2", cls.min_r2))
        if not 0.0 <= min_r2 <= 1.0:
            raise ValueError(f"The minimum R^2 must be between 0 and 1, got {min_r2}")
        return cls(
            threshold=float(params.get("threshold", cls.threshold)),
            threshold_mode=mode,
            window=window | 1,
            sigma=sigma,
            min_r2=min_r2,
        )

    @property
    def half(self) -> int:
        return self.window // 2


@dataclass(frozen=True)
class BeadFit:
    """One kept bead, in pixels of its plane."""

    plane: int
    y: float
    x: float
    fwhm_y_px: float
    fwhm_x_px: float
    amplitude: float
    background: float
    residual_rms: float
    r2: float

    @property
    def fwhm_px(self) -> float:
        """The bead's FWHM: the mean of its x and y FWHM."""
        return 0.5 * (self.fwhm_x_px + self.fwhm_y_px)

    @property
    def snr(self) -> float:
        if self.residual_rms <= 0:
            return math.inf
        return self.amplitude / self.residual_rms


@dataclass(frozen=True)
class Rejection:
    """A candidate that was found but not used, and why."""

    plane: int
    y: float
    x: float
    reason: str


@dataclass
class BeadAnalysis:
    """Every plane's kept beads and rejected candidates."""

    n_planes: int
    params: BeadParams
    fits: list[BeadFit] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    levels: list[float] = field(default_factory=list)

    def fits_in(self, plane: int) -> list[BeadFit]:
        return [fit for fit in self.fits if fit.plane == plane]

    def per_plane(self, values: Callable[[BeadFit], float]) -> np.ndarray:
        """The mean of ``values`` over each plane's beads; NaN for a plane without any."""
        out = np.full(self.n_planes, np.nan)
        for plane in range(self.n_planes):
            numbers = [values(fit) for fit in self.fits_in(plane)]
            finite = [number for number in numbers if math.isfinite(number)]
            if finite:
                out[plane] = float(np.mean(finite))
        return out

    def counts(self) -> np.ndarray:
        out = np.zeros(self.n_planes, dtype=np.int64)
        for fit in self.fits:
            out[fit.plane] += 1
        return out

    def rejection_counts(self) -> dict[str, int]:
        counts = {reason: 0 for reason in REJECTION_REASONS}
        for item in self.rejected:
            counts[item.reason] = counts.get(item.reason, 0) + 1
        return {reason: count for reason, count in counts.items() if count}


# ------------------------------------------------------------------- detection
def robust_noise(values: np.ndarray) -> tuple[float, float]:
    """``(median, sigma)``, sigma from the median absolute deviation.

    A plane whose MAD is zero -- photon counts that are mostly 0 -- falls back
    to the plain standard deviation, so a relative threshold still means
    something there.
    """
    flat = np.asarray(values, dtype=np.float64).ravel()
    flat = flat[np.isfinite(flat)]
    if flat.size == 0:
        return 0.0, 0.0
    median = float(np.median(flat))
    sigma = _MAD_TO_SIGMA * float(np.median(np.abs(flat - median)))
    if sigma <= 0:
        sigma = float(np.std(flat))
    return median, sigma


def detection_level(smoothed: np.ndarray, params: BeadParams) -> float:
    """The smoothed value a candidate has to exceed in this plane."""
    if params.threshold_mode == "absolute":
        return float(params.threshold)
    median, sigma = robust_noise(smoothed)
    return median + float(params.threshold) * sigma


def find_candidates(plane: np.ndarray, params: BeadParams) -> tuple[np.ndarray, float]:
    """Candidate bead centres ``(N, 2)`` as ``(row, col)``, and the level used."""
    image = np.asarray(plane, dtype=np.float64)
    finite = np.isfinite(image)
    if not finite.all():
        image = np.where(finite, image, np.nanmedian(image) if finite.any() else 0.0)
    smoothed = gaussian_filter(image, params.sigma) if params.sigma > 0 else image
    level = detection_level(smoothed, params)
    peaks = (maximum_filter(smoothed, size=3, mode="nearest") == smoothed) & (smoothed > level)
    if not peaks.any():
        return np.empty((0, 2)), level
    # Touching maxima are one flat (or saturated) top, not several beads.
    labels, count = label(peaks, structure=np.ones((3, 3), dtype=bool))
    centres = center_of_mass(smoothed, labels, index=np.arange(1, count + 1))
    return np.asarray(centres, dtype=np.float64).reshape(-1, 2), level


# ------------------------------------------------------------------------ fits
def _r_squared(values: np.ndarray, rms: float) -> float:
    total = float(np.sum((values - values.mean()) ** 2))
    if total <= 0:
        return 0.0
    return 1.0 - values.size * rms ** 2 / total


def analyse_plane(
    plane: np.ndarray, params: BeadParams, plane_index: int = 0,
) -> tuple[list[BeadFit], list[Rejection], float]:
    """Find, isolate and fit the beads of one 2D plane."""
    image = np.asarray(plane, dtype=np.float64)
    if image.ndim != 2:
        raise ValueError(f"A bead plane must be 2D, got shape {image.shape}")
    centres, level = find_candidates(image, params)
    half = params.half
    height, width = image.shape
    fits: list[BeadFit] = []
    rejected: list[Rejection] = []

    rows = np.rint(centres[:, 0]).astype(int) if len(centres) else np.empty(0, int)
    cols = np.rint(centres[:, 1]).astype(int) if len(centres) else np.empty(0, int)
    for index, (row, col) in enumerate(zip(rows, cols)):
        y, x = float(centres[index, 0]), float(centres[index, 1])
        if row - half < 0 or col - half < 0 or row + half >= height or col + half >= width:
            rejected.append(Rejection(plane_index, y, x, STATUS_EDGE))
            continue
        others = np.delete(np.arange(len(rows)), index)
        if np.any((np.abs(rows[others] - row) <= half) & (np.abs(cols[others] - col) <= half)):
            rejected.append(Rejection(plane_index, y, x, STATUS_CROWDED))
            continue
        bounds = (row - half, row + half + 1, col - half, col + half + 1)
        window = image[bounds[0]:bounds[1], bounds[2]:bounds[3]]
        try:
            fit = fit_psf(image, bounds)
        except Exception:
            rejected.append(Rejection(plane_index, y, x, STATUS_FIT_FAILED))
            continue
        reason = None
        if math.hypot(fit.center_y - y, fit.center_x - x) > max(1.5, half / 2):
            reason = STATUS_OFF_CENTRE
        elif min(fit.sigma_y, fit.sigma_x) < MIN_SIGMA_PX:
            reason = STATUS_TOO_NARROW
        elif max(fit.fwhm_y, fit.fwhm_x) > MAX_FWHM_FRACTION * params.window:
            reason = STATUS_TOO_WIDE
        r2 = _r_squared(window[np.isfinite(window)], fit.fit_error)
        if reason is None and r2 < params.min_r2:
            reason = STATUS_POOR_FIT
        if reason is not None:
            rejected.append(Rejection(plane_index, fit.center_y, fit.center_x, reason))
            continue
        fits.append(BeadFit(
            plane=plane_index,
            y=fit.center_y,
            x=fit.center_x,
            fwhm_y_px=fit.fwhm_y,
            fwhm_x_px=fit.fwhm_x,
            amplitude=fit.amplitude,
            background=fit.background,
            residual_rms=fit.fit_error,
            r2=r2,
        ))
    return fits, rejected, level


def analyse_planes(
    planes: Iterable[np.ndarray],
    n_planes: int,
    params: BeadParams,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> BeadAnalysis:
    """:func:`analyse_plane` over ``n_planes`` planes, in order."""
    analysis = BeadAnalysis(n_planes=int(n_planes), params=params)
    for index, plane in enumerate(planes):
        fits, rejected, level = analyse_plane(plane, params, index)
        analysis.fits.extend(fits)
        analysis.rejected.extend(rejected)
        analysis.levels.append(level)
        if progress is not None:
            progress(index + 1, n_planes)
    return analysis


__all__ = [
    "BeadAnalysis",
    "BeadFit",
    "BeadParams",
    "FWHM_FACTOR",
    "MAX_FWHM_FRACTION",
    "REJECTION_REASONS",
    "Rejection",
    "THRESHOLD_MODES",
    "analyse_plane",
    "analyse_planes",
    "detection_level",
    "find_candidates",
    "robust_noise",
]
