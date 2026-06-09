"""ROI statistics helpers for ImProcess."""

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

    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return ROIStats(
            area_pixels=int(arr.size),
            finite_pixels=0,
            mean=float("nan"),
            median=float("nan"),
            std=float("nan"),
            minimum=float("nan"),
            maximum=float("nan"),
            total=float("nan"),
        )

    return ROIStats(
        area_pixels=int(arr.size),
        finite_pixels=int(finite.size),
        mean=float(np.mean(finite)),
        median=float(np.median(finite)),
        std=float(np.std(finite)),
        minimum=float(np.min(finite)),
        maximum=float(np.max(finite)),
        total=float(np.sum(finite)),
    )
