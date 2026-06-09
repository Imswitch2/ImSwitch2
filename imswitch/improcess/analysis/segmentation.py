"""Segmentation helpers for ImProcess."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

from .roi_manager import ROIRecord


ThresholdMethod = Literal["otsu", "manual"]


@dataclass(frozen=True)
class SegmentationRegion:
    """One connected-component region."""

    label: int
    area_pixels: int
    bounds: tuple[int, int, int, int]
    mean_intensity: float
    max_intensity: float
    pixels: tuple[tuple[int, int], ...]

    def to_roi(self, *, name_prefix: str = "ROI", source: str = "segmentation") -> ROIRecord:
        return ROIRecord(
            name=f"{name_prefix}_{self.label}",
            roi_type="mask",
            bounds=self.bounds,
            source=source,
            pixels=self.pixels,
        )

    def to_row(self) -> dict[str, object]:
        return {
            "label": self.label,
            "area_pixels": self.area_pixels,
            "bounds": self.bounds,
            "mean_intensity": self.mean_intensity,
            "max_intensity": self.max_intensity,
        }


@dataclass(frozen=True)
class SegmentationAnalysis:
    """Output of threshold + connected-component segmentation."""

    labels: np.ndarray
    mask: np.ndarray
    threshold: float
    regions: list[SegmentationRegion]
    metadata: dict[str, object]

    def rois(self, *, name_prefix: str = "ROI") -> list[ROIRecord]:
        return [region.to_roi(name_prefix=name_prefix) for region in self.regions]

    def region_rows(self) -> list[dict[str, object]]:
        return [region.to_row() for region in self.regions]


def segment_image(
    image: np.ndarray,
    *,
    threshold_method: ThresholdMethod = "otsu",
    threshold_value: float | None = None,
    min_area: int = 10,
    smooth_sigma: float = 0.0,
) -> SegmentationAnalysis:
    """Segment a 2D image with thresholding and connected components."""
    arr = _prepare_image(image)
    if min_area < 1:
        raise ValueError("min_area must be at least 1")
    work = _smooth(arr, smooth_sigma)
    threshold = _resolve_threshold(
        work,
        threshold_method=threshold_method,
        threshold_value=threshold_value,
    )
    mask = np.isfinite(work) & (work > threshold)
    raw_labels, _count = _connected_components(mask)
    labels, regions = _filter_and_measure(raw_labels, arr, min_area=min_area)
    return SegmentationAnalysis(
        labels=labels,
        mask=labels > 0,
        threshold=float(threshold),
        regions=regions,
        metadata={
            "threshold_method": threshold_method,
            "threshold": float(threshold),
            "min_area": int(min_area),
            "smooth_sigma": float(smooth_sigma),
            "region_count": len(regions),
            "input_shape": tuple(arr.shape),
        },
    )


def otsu_threshold(image: np.ndarray, *, bins: int = 256) -> float:
    """Return Otsu's threshold for finite pixels in a 2D image."""
    arr = np.asarray(image, dtype=np.float64)
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        raise ValueError("Cannot threshold an image with no finite pixels")
    if np.nanmin(finite) == np.nanmax(finite):
        return float(np.nanmin(finite))
    hist, edges = np.histogram(finite, bins=bins)
    centers = 0.5 * (edges[:-1] + edges[1:])
    weight_bg = np.cumsum(hist)
    weight_fg = np.cumsum(hist[::-1])[::-1]
    valid = (weight_bg > 0) & (weight_fg > 0)
    mean_bg = np.divide(
        np.cumsum(hist * centers),
        weight_bg,
        out=np.zeros_like(centers, dtype=np.float64),
        where=weight_bg > 0,
    )
    mean_fg = np.divide(
        np.cumsum((hist * centers)[::-1])[::-1],
        weight_fg,
        out=np.zeros_like(centers, dtype=np.float64),
        where=weight_fg > 0,
    )
    variance = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
    variance = np.where(valid, variance, -1.0)
    return float(centers[int(np.argmax(variance))])


def _prepare_image(image: np.ndarray) -> np.ndarray:
    arr = np.asarray(image, dtype=np.float64)
    if arr.ndim != 2:
        raise ValueError(f"Segmentation expects a 2D image, got shape {arr.shape}")
    return arr


def _smooth(image: np.ndarray, sigma: float) -> np.ndarray:
    if sigma <= 0:
        return image
    try:
        from scipy import ndimage

        return ndimage.gaussian_filter(image, sigma=float(sigma))
    except Exception as exc:
        raise RuntimeError("Segmentation smoothing requires scipy") from exc


def _resolve_threshold(
    image: np.ndarray,
    *,
    threshold_method: ThresholdMethod,
    threshold_value: float | None,
) -> float:
    if threshold_method == "otsu":
        return otsu_threshold(image)
    if threshold_method == "manual":
        if threshold_value is None:
            raise ValueError("Manual threshold requires threshold_value")
        return float(threshold_value)
    raise ValueError(f"Unsupported threshold method: {threshold_method!r}")


def _connected_components(mask: np.ndarray) -> tuple[np.ndarray, int]:
    try:
        from scipy import ndimage

        structure = np.ones((3, 3), dtype=np.uint8)
        labels, count = ndimage.label(mask, structure=structure)
        return labels.astype(np.int32), int(count)
    except Exception:
        return _connected_components_fallback(mask)


def _connected_components_fallback(mask: np.ndarray) -> tuple[np.ndarray, int]:
    labels = np.zeros(mask.shape, dtype=np.int32)
    current = 0
    height, width = mask.shape
    for row in range(height):
        for col in range(width):
            if not mask[row, col] or labels[row, col] != 0:
                continue
            current += 1
            stack = [(row, col)]
            labels[row, col] = current
            while stack:
                r, c = stack.pop()
                for rr in range(max(0, r - 1), min(height, r + 2)):
                    for cc in range(max(0, c - 1), min(width, c + 2)):
                        if mask[rr, cc] and labels[rr, cc] == 0:
                            labels[rr, cc] = current
                            stack.append((rr, cc))
    return labels, current


def _filter_and_measure(
    labels: np.ndarray,
    image: np.ndarray,
    *,
    min_area: int,
) -> tuple[np.ndarray, list[SegmentationRegion]]:
    output = np.zeros(labels.shape, dtype=np.int32)
    regions: list[SegmentationRegion] = []
    next_label = 1
    for label in sorted(int(v) for v in np.unique(labels) if v > 0):
        coords = np.nonzero(labels == label)
        area = int(coords[0].size)
        if area < min_area:
            continue
        r0, r1 = int(coords[0].min()), int(coords[0].max()) + 1
        c0, c1 = int(coords[1].min()), int(coords[1].max()) + 1
        values = image[coords]
        finite = values[np.isfinite(values)]
        mean_intensity = float(np.mean(finite)) if finite.size else float("nan")
        max_intensity = float(np.max(finite)) if finite.size else float("nan")
        pixels = tuple(
            (int(row), int(col))
            for row, col in zip(coords[0].tolist(), coords[1].tolist())
        )
        output[coords] = next_label
        regions.append(
            SegmentationRegion(
                label=next_label,
                area_pixels=area,
                bounds=(r0, r1, c0, c1),
                mean_intensity=mean_intensity,
                max_intensity=max_intensity,
                pixels=pixels,
            )
        )
        next_label += 1
    return output, regions
