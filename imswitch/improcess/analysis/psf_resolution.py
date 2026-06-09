"""PSF / bead resolution helpers for ImProcess."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .roi_manager import ROIRecord


FWHM_FACTOR = 2.354820045


@dataclass(frozen=True)
class PSFFitRecord:
    """One 2D Gaussian PSF fit."""

    name: str
    bounds: tuple[int, int, int, int]
    center_y: float
    center_x: float
    sigma_y: float
    sigma_x: float
    fwhm_y: float
    fwhm_x: float
    amplitude: float
    background: float
    fit_error: float
    area_pixels: int

    def scaled_row(self, *, pixel_size: float = 1.0, unit: str = "px") -> dict[str, object]:
        return {
            "name": self.name,
            "bounds": self.bounds,
            "center_y_px": self.center_y,
            "center_x_px": self.center_x,
            "sigma_y_px": self.sigma_y,
            "sigma_x_px": self.sigma_x,
            "fwhm_y_px": self.fwhm_y,
            "fwhm_x_px": self.fwhm_x,
            f"sigma_y_{unit}": self.sigma_y * pixel_size,
            f"sigma_x_{unit}": self.sigma_x * pixel_size,
            f"fwhm_y_{unit}": self.fwhm_y * pixel_size,
            f"fwhm_x_{unit}": self.fwhm_x * pixel_size,
            "amplitude": self.amplitude,
            "background": self.background,
            "fit_error": self.fit_error,
            "area_pixels": self.area_pixels,
        }


@dataclass(frozen=True)
class PSFResolutionAnalysis:
    """Batch PSF resolution output."""

    fits: list[PSFFitRecord]
    pixel_size: float = 1.0
    unit: str = "px"
    metadata: dict[str, object] | None = None

    def rows(self) -> list[dict[str, object]]:
        return [fit.scaled_row(pixel_size=self.pixel_size, unit=self.unit) for fit in self.fits]


def fit_psf(
    image: np.ndarray,
    roi: ROIRecord | tuple[int, int, int, int] | None = None,
    *,
    name: str = "PSF",
) -> PSFFitRecord:
    """Fit a non-rotated 2D Gaussian to a 2D image or ROI."""
    arr = np.asarray(image, dtype=np.float64)
    if arr.ndim != 2:
        raise ValueError(f"PSF fitting expects a 2D image, got shape {arr.shape}")
    bounds, yy, xx, values = _extract_fit_points(arr, roi)
    if values.size < 6:
        raise ValueError("PSF fit needs at least six finite pixels")

    try:
        from scipy.optimize import curve_fit
    except Exception as exc:
        raise RuntimeError("PSF Gaussian fitting requires scipy") from exc

    p0 = _initial_params(yy, xx, values)
    value_range = _finite_range(values)
    lower = [
        float(np.nanmin(values)) - abs(value_range) - 1.0,
        0.0,
        float(bounds[0]),
        float(bounds[2]),
        1e-6,
        1e-6,
    ]
    upper = [
        float(np.nanmax(values)) + abs(value_range) + 1.0,
        max(abs(value_range) * 10.0, 1.0),
        float(max(bounds[1] - 1, bounds[0])),
        float(max(bounds[3] - 1, bounds[2])),
        float(max(arr.shape[0], 1)),
        float(max(arr.shape[1], 1)),
    ]
    popt, _pcov = curve_fit(
        _gaussian_2d,
        (yy, xx),
        values,
        p0=p0,
        bounds=(lower, upper),
        maxfev=20000,
    )
    background, amplitude, center_y, center_x, sigma_y, sigma_x = popt
    fitted = _gaussian_2d((yy, xx), *popt)
    error = float(np.sqrt(np.mean((fitted - values) ** 2)))
    return PSFFitRecord(
        name=name,
        bounds=bounds,
        center_y=float(center_y),
        center_x=float(center_x),
        sigma_y=abs(float(sigma_y)),
        sigma_x=abs(float(sigma_x)),
        fwhm_y=FWHM_FACTOR * abs(float(sigma_y)),
        fwhm_x=FWHM_FACTOR * abs(float(sigma_x)),
        amplitude=float(amplitude),
        background=float(background),
        fit_error=error,
        area_pixels=int(values.size),
    )


def fit_psf_batch(
    image: np.ndarray,
    rois: list[ROIRecord] | None = None,
    *,
    pixel_size: float = 1.0,
    unit: str = "px",
) -> PSFResolutionAnalysis:
    """Fit one PSF per ROI, or the full image when no ROIs are given."""
    targets = rois or [None]
    fits = [
        fit_psf(
            image,
            roi,
            name=(roi.name if isinstance(roi, ROIRecord) else "Full image"),
        )
        for roi in targets
    ]
    return PSFResolutionAnalysis(
        fits=fits,
        pixel_size=float(pixel_size),
        unit=str(unit),
        metadata={"fit_count": len(fits), "source": "roi" if rois else "full-image"},
    )


def _gaussian_2d(coords, background, amplitude, center_y, center_x, sigma_y, sigma_x):
    yy, xx = coords
    return background + amplitude * np.exp(
        -(
            ((yy - center_y) ** 2) / (2.0 * sigma_y**2)
            + ((xx - center_x) ** 2) / (2.0 * sigma_x**2)
        )
    )


def _extract_fit_points(
    image: np.ndarray,
    roi: ROIRecord | tuple[int, int, int, int] | None,
) -> tuple[tuple[int, int, int, int], np.ndarray, np.ndarray, np.ndarray]:
    if isinstance(roi, ROIRecord) and roi.pixels is not None:
        coords = np.asarray(roi.pixels, dtype=np.int64).reshape((-1, 2))
        inside = (
            (coords[:, 0] >= 0)
            & (coords[:, 0] < image.shape[0])
            & (coords[:, 1] >= 0)
            & (coords[:, 1] < image.shape[1])
        )
        coords = coords[inside]
        if coords.size == 0:
            raise ValueError(f"ROI {roi.name!r} is empty after clipping")
        values = image[coords[:, 0], coords[:, 1]]
        finite = np.isfinite(values)
        coords = coords[finite]
        values = values[finite]
        bounds = roi.bounds
        return bounds, coords[:, 0].astype(np.float64), coords[:, 1].astype(np.float64), values

    bounds = roi.bounds if isinstance(roi, ROIRecord) else roi
    if bounds is None:
        bounds = (0, image.shape[0], 0, image.shape[1])
    r0, r1, c0, c1 = _clip_bounds(bounds, image.shape)
    yy, xx = np.mgrid[r0:r1, c0:c1]
    values = image[r0:r1, c0:c1].ravel()
    yy = yy.ravel().astype(np.float64)
    xx = xx.ravel().astype(np.float64)
    finite = np.isfinite(values)
    return (r0, r1, c0, c1), yy[finite], xx[finite], values[finite]


def _clip_bounds(
    bounds: tuple[int, int, int, int],
    shape: tuple[int, int],
) -> tuple[int, int, int, int]:
    r0, r1, c0, c1 = bounds
    rlo, rhi = sorted((int(round(r0)), int(round(r1))))
    clo, chi = sorted((int(round(c0)), int(round(c1))))
    rlo, rhi = max(0, rlo), min(shape[0], rhi)
    clo, chi = max(0, clo), min(shape[1], chi)
    if rlo >= rhi or clo >= chi:
        raise ValueError("PSF ROI is empty after clipping")
    return rlo, rhi, clo, chi


def _initial_params(
    yy: np.ndarray,
    xx: np.ndarray,
    values: np.ndarray,
) -> tuple[float, float, float, float, float, float]:
    background = float(np.nanpercentile(values, 10.0))
    amplitude = max(float(np.nanmax(values) - background), 1e-6)
    weights = np.clip(values - background, 0.0, None)
    if np.sum(weights) <= 0:
        weights = np.ones_like(values, dtype=np.float64)
    center_y = float(np.sum(yy * weights) / np.sum(weights))
    center_x = float(np.sum(xx * weights) / np.sum(weights))
    sigma_y = float(np.sqrt(max(np.sum(((yy - center_y) ** 2) * weights) / np.sum(weights), 1e-6)))
    sigma_x = float(np.sqrt(max(np.sum(((xx - center_x) ** 2) * weights) / np.sum(weights), 1e-6)))
    return background, amplitude, center_y, center_x, max(sigma_y, 0.5), max(sigma_x, 0.5)


def _finite_range(values: np.ndarray) -> float:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return 0.0
    return float(np.max(finite) - np.min(finite))
