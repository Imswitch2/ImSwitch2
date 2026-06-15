"""Segmentation helpers for ImProcess."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

from .roi_manager import ROIRecord


ThresholdMethod = Literal["otsu", "manual", "triangle", "yen", "local", "watershed"]


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
    background_radius: float = 0.0,
    morphology_radius: int = 0,
    fill_holes: bool = False,
    clear_border: bool = False,
    local_block_size: int = 51,
    local_offset: float = 0.0,
    watershed_min_distance: int = 5,
) -> SegmentationAnalysis:
    """Segment a 2D image with classical microscopy-oriented methods."""
    arr = _prepare_image(image)
    if min_area < 1:
        raise ValueError("min_area must be at least 1")
    work = _subtract_background(arr, background_radius)
    work = _smooth(work, smooth_sigma)
    mask, threshold = _initial_mask(
        work,
        threshold_method=threshold_method,
        threshold_value=threshold_value,
        local_block_size=local_block_size,
        local_offset=local_offset,
    )
    mask = _cleanup_mask(
        mask,
        morphology_radius=morphology_radius,
        fill_holes=fill_holes,
        clear_border=clear_border,
    )
    if threshold_method == "watershed":
        raw_labels = _watershed_labels(
            mask,
            work,
            min_distance=watershed_min_distance,
        )
    else:
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
            "background_radius": float(background_radius),
            "morphology_radius": int(morphology_radius),
            "fill_holes": bool(fill_holes),
            "clear_border": bool(clear_border),
            "local_block_size": int(local_block_size),
            "local_offset": float(local_offset),
            "watershed_min_distance": int(watershed_min_distance),
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


def _subtract_background(image: np.ndarray, radius: float) -> np.ndarray:
    if radius <= 0:
        return image
    try:
        from skimage import morphology

        footprint = morphology.disk(max(1, int(round(radius))))
        return morphology.white_tophat(image, footprint=footprint)
    except Exception as exc:
        raise RuntimeError("Segmentation background subtraction requires scikit-image") from exc


def _initial_mask(
    image: np.ndarray,
    *,
    threshold_method: ThresholdMethod,
    threshold_value: float | None,
    local_block_size: int,
    local_offset: float,
) -> tuple[np.ndarray, float]:
    if threshold_method == "otsu":
        threshold = otsu_threshold(image)
        return np.isfinite(image) & (image > threshold), float(threshold)
    if threshold_method == "manual":
        if threshold_value is None:
            raise ValueError("Manual threshold requires threshold_value")
        threshold = float(threshold_value)
        return np.isfinite(image) & (image > threshold), threshold
    if threshold_method in ("triangle", "yen"):
        threshold = _skimage_global_threshold(image, threshold_method)
        return np.isfinite(image) & (image > threshold), float(threshold)
    if threshold_method == "local":
        threshold_map = _local_threshold(
            image,
            block_size=local_block_size,
            offset=local_offset,
        )
        mask = np.isfinite(image) & (image > threshold_map)
        return mask, float(np.nanmedian(threshold_map))
    if threshold_method == "watershed":
        threshold = otsu_threshold(image)
        return np.isfinite(image) & (image > threshold), float(threshold)
    raise ValueError(f"Unsupported threshold method: {threshold_method!r}")


def _skimage_global_threshold(image: np.ndarray, method: str) -> float:
    try:
        from skimage import filters
    except Exception as exc:
        raise RuntimeError("Segmentation thresholding requires scikit-image") from exc

    finite = image[np.isfinite(image)]
    if finite.size == 0:
        raise ValueError("Cannot threshold an image with no finite pixels")
    if np.nanmin(finite) == np.nanmax(finite):
        return float(np.nanmin(finite))
    if method == "triangle":
        return float(filters.threshold_triangle(finite))
    if method == "yen":
        return float(filters.threshold_yen(finite))
    raise ValueError(f"Unsupported scikit-image threshold method: {method!r}")


def _local_threshold(image: np.ndarray, *, block_size: int, offset: float) -> np.ndarray:
    if block_size < 3:
        raise ValueError("local_block_size must be at least 3")
    if block_size % 2 == 0:
        block_size += 1
    try:
        from skimage import filters
    except Exception as exc:
        raise RuntimeError("Local segmentation thresholding requires scikit-image") from exc
    finite = np.isfinite(image)
    fill = float(np.nanmedian(image[finite])) if np.any(finite) else 0.0
    work = np.where(finite, image, fill)
    return filters.threshold_local(work, block_size=block_size, offset=float(offset))


def _cleanup_mask(
    mask: np.ndarray,
    *,
    morphology_radius: int,
    fill_holes: bool,
    clear_border: bool,
) -> np.ndarray:
    out = np.asarray(mask, dtype=bool)
    if morphology_radius > 0:
        try:
            from skimage import morphology

            footprint = morphology.disk(int(morphology_radius))
            out = morphology.binary_opening(out, footprint=footprint)
            out = morphology.binary_closing(out, footprint=footprint)
        except Exception as exc:
            raise RuntimeError("Segmentation morphology cleanup requires scikit-image") from exc
    if fill_holes:
        try:
            from scipy import ndimage

            out = ndimage.binary_fill_holes(out)
        except Exception as exc:
            raise RuntimeError("Segmentation hole filling requires scipy") from exc
    if clear_border:
        try:
            from skimage import segmentation

            out = segmentation.clear_border(out)
        except Exception as exc:
            raise RuntimeError("Segmentation border clearing requires scikit-image") from exc
    return np.asarray(out, dtype=bool)


def _watershed_labels(mask: np.ndarray, image: np.ndarray, *, min_distance: int) -> np.ndarray:
    if not np.any(mask):
        return np.zeros(mask.shape, dtype=np.int32)
    try:
        from scipy import ndimage
        from skimage import feature, segmentation

        distance = ndimage.distance_transform_edt(mask)
        coordinates = feature.peak_local_max(
            distance,
            min_distance=max(1, int(min_distance)),
            labels=mask,
            exclude_border=False,
        )
        markers = np.zeros(mask.shape, dtype=np.int32)
        for index, (row, col) in enumerate(coordinates, start=1):
            markers[int(row), int(col)] = index
        if markers.max() == 0:
            markers, _count = ndimage.label(mask)
        labels = segmentation.watershed(-distance, markers, mask=mask)
        return labels.astype(np.int32)
    except Exception as exc:
        raise RuntimeError("Watershed segmentation requires scipy and scikit-image") from exc


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
