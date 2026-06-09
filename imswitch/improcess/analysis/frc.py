"""Fourier ring correlation analysis."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np


WindowKind = Literal["hann", "none"]
SingleImageSplit = Literal["checkerboard", "odd-even"]
ThresholdKind = Literal["one-seventh"]


@dataclass(frozen=True)
class FRCAnalysis:
    """Output of an FRC computation."""

    frequency: np.ndarray
    frc: np.ndarray
    threshold: np.ndarray
    cutoff_frequency: float
    resolution: float
    pixel_size: float
    frequency_unit: str
    resolution_unit: str
    metadata: dict[str, object]


def frc_two_image(
    image_a: np.ndarray,
    image_b: np.ndarray,
    *,
    pixel_size: float = 1.0,
    frequency_unit: str = "cycles/px",
    resolution_unit: str = "px",
    window: WindowKind = "hann",
    threshold: ThresholdKind = "one-seventh",
) -> FRCAnalysis:
    """Compute Fourier ring correlation between two 2D images."""
    a = _prepare_image(image_a)
    b = _prepare_image(image_b)
    if a.shape != b.shape:
        raise ValueError(f"FRC images must have the same shape, got {a.shape} and {b.shape}")
    if min(a.shape) < 4:
        raise ValueError(f"FRC images must be at least 4x4 pixels, got {a.shape}")
    if pixel_size <= 0:
        raise ValueError("pixel_size must be positive")

    a = _apodize(a, window)
    b = _apodize(b, window)
    fa = np.fft.fftshift(np.fft.fft2(a))
    fb = np.fft.fftshift(np.fft.fft2(b))

    frequency, frc = _radial_frc(fa, fb, pixel_size)
    threshold_curve = _threshold_curve(threshold, frequency.size)
    cutoff = _cutoff_frequency(frequency, frc, threshold_curve)
    resolution = np.nan if not np.isfinite(cutoff) or cutoff <= 0 else 1.0 / cutoff

    return FRCAnalysis(
        frequency=frequency,
        frc=frc,
        threshold=threshold_curve,
        cutoff_frequency=float(cutoff),
        resolution=float(resolution),
        pixel_size=float(pixel_size),
        frequency_unit=frequency_unit,
        resolution_unit=resolution_unit,
        metadata={
            "mode": "two-image",
            "window": window,
            "threshold": threshold,
            "shape": a.shape,
        },
    )


def single_image_frc(
    image: np.ndarray,
    *,
    split: SingleImageSplit = "checkerboard",
    pixel_size: float = 1.0,
    frequency_unit: str = "cycles/px",
    resolution_unit: str = "px",
    window: WindowKind = "hann",
    threshold: ThresholdKind = "one-seventh",
) -> FRCAnalysis:
    """Estimate FRC from one image by splitting pixels into two sub-images."""
    img = _prepare_image(image)
    a, b = split_single_image(img, split=split)
    analysis = frc_two_image(
        a,
        b,
        pixel_size=pixel_size,
        frequency_unit=frequency_unit,
        resolution_unit=resolution_unit,
        window=window,
        threshold=threshold,
    )
    metadata = dict(analysis.metadata)
    metadata.update({"mode": "single-image", "split": split})
    return FRCAnalysis(
        frequency=analysis.frequency,
        frc=analysis.frc,
        threshold=analysis.threshold,
        cutoff_frequency=analysis.cutoff_frequency,
        resolution=analysis.resolution,
        pixel_size=analysis.pixel_size,
        frequency_unit=analysis.frequency_unit,
        resolution_unit=analysis.resolution_unit,
        metadata=metadata,
    )


def split_single_image(
    image: np.ndarray,
    *,
    split: SingleImageSplit = "checkerboard",
) -> tuple[np.ndarray, np.ndarray]:
    """Split one image into two same-shaped images for single-image FRC."""
    img = _prepare_image(image)
    if split == "checkerboard":
        rr, cc = np.indices(img.shape)
        mask = (rr + cc) % 2 == 0
    elif split == "odd-even":
        mask = np.zeros(img.shape, dtype=bool)
        mask[:, 0::2] = True
    else:
        raise ValueError(f"Unsupported single-image FRC split: {split!r}")

    a = np.where(mask, img, 0.0)
    b = np.where(~mask, img, 0.0)
    return a, b


def _prepare_image(image: np.ndarray) -> np.ndarray:
    arr = np.asarray(image, dtype=np.float64)
    if arr.ndim != 2:
        raise ValueError(f"FRC expects a 2D image, got shape {arr.shape}")
    arr = np.where(np.isfinite(arr), arr, 0.0)
    arr = arr - np.mean(arr)
    std = np.std(arr)
    if std > 0:
        arr = arr / std
    return arr


def _apodize(image: np.ndarray, window: WindowKind) -> np.ndarray:
    if window == "none":
        return image
    if window != "hann":
        raise ValueError(f"Unsupported FRC window: {window!r}")
    wy = np.hanning(image.shape[0])
    wx = np.hanning(image.shape[1])
    return image * np.outer(wy, wx)


def _radial_frc(fa: np.ndarray, fb: np.ndarray, pixel_size: float) -> tuple[np.ndarray, np.ndarray]:
    fy = np.fft.fftshift(np.fft.fftfreq(fa.shape[0], d=pixel_size))
    fx = np.fft.fftshift(np.fft.fftfreq(fa.shape[1], d=pixel_size))
    yy, xx = np.meshgrid(fy, fx, indexing="ij")
    radius = np.sqrt(xx**2 + yy**2)

    bin_width = min(1.0 / (fa.shape[0] * pixel_size), 1.0 / (fa.shape[1] * pixel_size))
    max_freq = min(0.5 / pixel_size, float(np.nanmax(radius)))
    bins = np.arange(0.0, max_freq + bin_width, bin_width)
    if bins.size < 2:
        raise ValueError("Could not construct FRC frequency bins")

    bin_index = np.digitize(radius.ravel(), bins) - 1
    valid = (bin_index >= 0) & (bin_index < bins.size - 1)
    bin_index = bin_index[valid]
    cross = (fa * np.conj(fb)).real.ravel()[valid]
    power_a = (np.abs(fa) ** 2).ravel()[valid]
    power_b = (np.abs(fb) ** 2).ravel()[valid]

    numerator = np.bincount(bin_index, weights=cross, minlength=bins.size - 1)
    denom_a = np.bincount(bin_index, weights=power_a, minlength=bins.size - 1)
    denom_b = np.bincount(bin_index, weights=power_b, minlength=bins.size - 1)
    counts = np.bincount(bin_index, minlength=bins.size - 1)

    denom = np.sqrt(denom_a * denom_b)
    frc = np.divide(numerator, denom, out=np.full_like(numerator, np.nan), where=denom > 0)
    frequency = 0.5 * (bins[:-1] + bins[1:])

    nonempty = counts > 0
    return frequency[nonempty], frc[nonempty]


def _threshold_curve(threshold: ThresholdKind, size: int) -> np.ndarray:
    if threshold != "one-seventh":
        raise ValueError(f"Unsupported FRC threshold: {threshold!r}")
    return np.full(size, 1.0 / 7.0, dtype=np.float64)


def _cutoff_frequency(
    frequency: np.ndarray,
    frc: np.ndarray,
    threshold: np.ndarray,
) -> float:
    finite = np.isfinite(frequency) & np.isfinite(frc) & np.isfinite(threshold)
    frequency = frequency[finite]
    frc = frc[finite]
    threshold = threshold[finite]
    if frequency.size == 0:
        return float("nan")

    diff = frc - threshold
    below = np.nonzero(diff < 0)[0]
    if below.size == 0:
        return float("nan")

    idx = int(below[0])
    if idx == 0:
        return float(frequency[0])

    f0, f1 = frequency[idx - 1], frequency[idx]
    d0, d1 = diff[idx - 1], diff[idx]
    if d1 == d0:
        return float(f1)
    alpha = np.clip(-d0 / (d1 - d0), 0.0, 1.0)
    return float(f0 + alpha * (f1 - f0))
