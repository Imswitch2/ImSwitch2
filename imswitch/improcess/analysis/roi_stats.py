"""ROI statistics helpers for ImProcess.

The eight statistics the ROI-statistics panel has always shown, kept as a
small dataclass because that panel and its tests are written against it. The
values now come from the shared measurement registry, so this panel and the
ROI manager cannot disagree about what a mean is.

One number changed deliberately: **standard deviation is now the sample
standard deviation (n-1), matching ImageJ**, where it was the population one
(n). A std that disagrees with Fiji for the same region is a support burden,
and this is a measurement tool for people who check their numbers against
Fiji. For a single pixel it is NaN rather than 0 — undefined, not "no spread".
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ROIStats:
    """Summary statistics for one image region."""

    area_pixels: int
    finite_pixels: int
    mean: float
    median: float
    std: float
    minimum: float
    maximum: float
    total: float


def compute_roi_stats(image: np.ndarray, roi: tuple[int, int, int, int] | None = None) -> ROIStats:
    """Compute basic statistics for a 2D image or rectangular ROI.

    Args:
        image: 2D image.
        roi: Optional ``(row0, row1, col0, col1)`` bounds. Bounds are clipped to
            the image shape.
    """
    arr = np.asarray(image, dtype=np.float64)
    if arr.ndim != 2:
        raise ValueError(f"ROI statistics expect a 2D image, got shape {arr.shape}")

    if roi is not None:
        r0, r1, c0, c1 = roi
        rlo, rhi = sorted((int(round(r0)), int(round(r1))))
        clo, chi = sorted((int(round(c0)), int(round(c1))))
        rlo, rhi = max(0, rlo), min(arr.shape[0], rhi)
        clo, chi = max(0, clo), min(arr.shape[1], chi)
        if rlo >= rhi or clo >= chi:
            raise ValueError("ROI is empty after clipping to image bounds")
        arr = arr[rlo:rhi, clo:chi]

    return stats_from_values(arr[np.isfinite(arr)], area_pixels=int(arr.size))


def stats_from_values(finite: np.ndarray, *, area_pixels: int) -> ROIStats:
    """The eight legacy statistics from an ROI's finite values.

    A thin shim over the registry: each field is computed by the same function
    that fills the corresponding column in the ROI manager.
    """
    from .roi_measurements import MEASUREMENTS, MeasurementContext

    finite = np.asarray(finite, dtype=np.float64)
    if finite.size == 0:
        nan = float("nan")
        return ROIStats(
            area_pixels=int(area_pixels),
            finite_pixels=0,
            mean=nan, median=nan, std=nan,
            minimum=nan, maximum=nan, total=nan,
        )

    # The registry measures a mask over an image; the values are already
    # selected here, so they are presented as a 1xN row that is entirely ROI.
    row = finite.reshape(1, -1)
    context = MeasurementContext(
        local_mask=np.ones(row.shape, dtype=bool),
        local_image=row,
        roi=None,
    )
    value = {key: MEASUREMENTS[key].compute(context) for key in
             ("mean", "median", "std", "min", "max", "sum")}
    return ROIStats(
        area_pixels=int(area_pixels),
        finite_pixels=int(finite.size),
        mean=value["mean"],
        median=value["median"],
        std=value["std"],
        minimum=value["min"],
        maximum=value["max"],
        total=value["sum"],
    )
