"""Classical threshold + connected-component segmentation.

Shared building block for ImProcess segmentation, WidefieldSTARSS analysis and
imcontrol tiling/cell-targeting workflows. Pure NumPy in / dataclass out; the
scipy/scikit-image dependencies are imported lazily inside the functions that
need them, so importing this module stays cheap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

from .roi import ROIRecord


ThresholdMethod = Literal["otsu", "manual", "triangle", "yen", "local", "watershed"]
LabelMethod = Literal["connected_components", "watershed"]


@dataclass(frozen=True)
class SegmentationRegion:
    """One segmented region.

    ``bounds`` uses NumPy half-open order:
    ``(row_min, row_max_exclusive, col_min, col_max_exclusive)``.
    ``bbox`` uses scikit-image order:
    ``(min_row, min_col, max_row_exclusive, max_col_exclusive)``.
    """

    label: int
    area_pixels: int
    bounds: tuple[int, int, int, int]
    mean_intensity: float
    max_intensity: float
    pixels: tuple[tuple[int, int], ...]
    centroid_row: float | None = None
    centroid_col: float | None = None
    bbox: tuple[int, int, int, int] | None = None
    eccentricity: float | None = None
    area_um2: float | None = None
    centroid_y_um: float | None = None
    centroid_x_um: float | None = None
    height_um: float | None = None
    width_um: float | None = None

    def to_roi(self, *, name_prefix: str = "ROI", source: str = "segmentation") -> ROIRecord:
        return ROIRecord(
            name=f"{name_prefix}_{self.label}",
            roi_type="mask",
            bounds=self.bounds,
            source=source,
            pixels=self.pixels,
        )

    def to_row(self, *, include_optional: bool = False) -> dict[str, object]:
        row = {
            "label": self.label,
            "area_pixels": self.area_pixels,
            "bounds": self.bounds,
            "mean_intensity": self.mean_intensity,
            "max_intensity": self.max_intensity,
        }
        if not include_optional:
            return row
        optional = {
            "centroid_row": self.centroid_row,
            "centroid_col": self.centroid_col,
            "bbox": self.bbox,
            "eccentricity": self.eccentricity,
            "area_um2": self.area_um2,
            "centroid_y_um": self.centroid_y_um,
            "centroid_x_um": self.centroid_x_um,
            "height_um": self.height_um,
            "width_um": self.width_um,
        }
        row.update({key: value for key, value in optional.items() if value is not None})
        return row


@dataclass(frozen=True)
class SegmentationAnalysis:
    """Output of threshold + connected-component segmentation."""

    labels: np.ndarray
    mask: np.ndarray
    threshold: float
    regions: list[SegmentationRegion]
    metadata: dict[str, object]
    processed_image: np.ndarray | None = None
    binary_mask: np.ndarray | None = None

    def rois(self, *, name_prefix: str = "ROI") -> list[ROIRecord]:
        return [region.to_roi(name_prefix=name_prefix) for region in self.regions]

    def region_rows(self, *, include_optional: bool = False) -> list[dict[str, object]]:
        return [region.to_row(include_optional=include_optional) for region in self.regions]


def segment_image(
    image: np.ndarray,
    *,
    threshold_method: ThresholdMethod = "otsu",
    threshold_value: float | None = None,
    threshold_scale: float = 1.0,
    label_method: LabelMethod = "connected_components",
    min_area: int = 10,
    smooth_sigma: float = 0.0,
    background_radius: float = 0.0,
    normalize: bool = False,
    morphology_radius: int = 0,
    opening_radius: int | None = None,
    closing_radius: int | None = None,
    fill_holes: bool = False,
    max_hole_area: int | None = None,
    clear_border: bool = False,
    local_block_size: int = 51,
    local_offset: float = 0.0,
    watershed_min_distance: int = 5,
    pixel_size_um: float | tuple[float, float] | None = None,
) -> SegmentationAnalysis:
    """Segment a 2D image with classical microscopy-oriented methods."""
    arr = _prepare_image(image)
    if min_area < 1:
        raise ValueError("min_area must be at least 1")
    if threshold_scale < 0:
        raise ValueError("threshold_scale must be non-negative")
    threshold_method_for_mask = threshold_method
    effective_label_method = label_method
    if threshold_method == "watershed":
        threshold_method_for_mask = "otsu"
        if label_method == "connected_components":
            effective_label_method = "watershed"
    work = prepare_segmentation_image(
        arr,
        background_radius=background_radius,
        smooth_sigma=smooth_sigma,
        normalize=normalize,
    )
    mask, threshold, threshold_summary = _initial_mask(
        work,
        threshold_method=threshold_method_for_mask,
        threshold_value=threshold_value,
        threshold_scale=threshold_scale,
        local_block_size=local_block_size,
        local_offset=local_offset,
    )
    mask = _cleanup_mask(
        mask,
        morphology_radius=morphology_radius,
        opening_radius=opening_radius,
        closing_radius=closing_radius,
        fill_holes=fill_holes,
        max_hole_area=max_hole_area,
        clear_border=clear_border,
    )
    if effective_label_method == "watershed":
        raw_labels = _watershed_labels(
            mask,
            min_distance=watershed_min_distance,
        )
    elif effective_label_method == "connected_components":
        raw_labels, _count = _connected_components(mask)
    else:
        raise ValueError(f"Unsupported label method: {label_method!r}")
    labels, regions = _filter_and_measure(
        raw_labels,
        arr,
        min_area=min_area,
        pixel_size_um=pixel_size_um,
    )
    metadata = {
        "threshold_method": threshold_method,
        "threshold": float(threshold),
        "threshold_scale": float(threshold_scale),
        "label_method": effective_label_method,
        "min_area": int(min_area),
        "smooth_sigma": float(smooth_sigma),
        "background_radius": float(background_radius),
        "normalize": bool(normalize),
        "morphology_radius": int(morphology_radius),
        "opening_radius": opening_radius,
        "closing_radius": closing_radius,
        "fill_holes": bool(fill_holes),
        "max_hole_area": None if max_hole_area is None else int(max_hole_area),
        "clear_border": bool(clear_border),
        "local_block_size": int(local_block_size),
        "local_offset": float(local_offset),
        "watershed_min_distance": int(watershed_min_distance),
        "pixel_size_um": pixel_size_um,
        "region_count": len(regions),
        "input_shape": tuple(arr.shape),
    }
    if threshold_summary is not None:
        metadata["threshold_summary"] = threshold_summary
    return SegmentationAnalysis(
        labels=labels,
        mask=labels > 0,
        threshold=float(threshold),
        regions=regions,
        metadata=metadata,
        processed_image=work,
        binary_mask=mask.astype(bool, copy=True),
    )


def prepare_segmentation_image(
    image: np.ndarray,
    *,
    smooth_sigma: float = 0.0,
    background_radius: float = 0.0,
    normalize: bool = False,
) -> np.ndarray:
    """Return the preprocessed image used for thresholding and labeling."""
    arr = _prepare_image(image)
    work = _subtract_background(arr, background_radius)
    work = _smooth(work, smooth_sigma)
    if normalize:
        work = _normalize_minmax(work)
    return work


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
    weight_fg = finite.size - weight_bg
    sum_total = np.sum(hist * centers)
    sum_bg = np.cumsum(hist * centers)
    sum_fg = sum_total - sum_bg
    mean_bg = np.divide(
        sum_bg,
        weight_bg,
        out=np.zeros_like(centers, dtype=np.float64),
        where=weight_bg > 0,
    )
    mean_fg = np.divide(
        sum_fg,
        weight_fg,
        out=np.zeros_like(centers, dtype=np.float64),
        where=weight_fg > 0,
    )
    valid = (weight_bg > 0) & (weight_fg > 0)
    variance = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
    variance = np.where(valid, variance, -1.0)
    return float(centers[int(np.argmax(variance))])


def _prepare_image(image: np.ndarray) -> np.ndarray:
    arr = np.asarray(image, dtype=np.float64)
    if arr.ndim != 2:
        raise ValueError(f"Segmentation expects a 2D image, got shape {arr.shape}")
    return arr


def _normalize_minmax(image: np.ndarray) -> np.ndarray:
    finite = np.isfinite(image)
    if not np.any(finite):
        return np.zeros_like(image, dtype=np.float64)
    out = np.asarray(image, dtype=np.float64)
    min_value = float(np.nanmin(out[finite]))
    max_value = float(np.nanmax(out[finite]))
    out = out - min_value
    span = max_value - min_value
    if span > 0:
        out = out / span
    out = np.where(finite, out, 0.0)
    return out


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
    threshold_scale: float,
    local_block_size: int,
    local_offset: float,
) -> tuple[np.ndarray, float, dict[str, float] | None]:
    if threshold_method == "otsu":
        threshold = otsu_threshold(image) * threshold_scale
        return np.isfinite(image) & (image > threshold), float(threshold), None
    if threshold_method == "manual":
        if threshold_value is None:
            raise ValueError("Manual threshold requires threshold_value")
        threshold = float(threshold_value) * threshold_scale
        return np.isfinite(image) & (image > threshold), threshold, None
    if threshold_method in ("triangle", "yen"):
        threshold = _skimage_global_threshold(image, threshold_method) * threshold_scale
        return np.isfinite(image) & (image > threshold), float(threshold), None
    if threshold_method == "local":
        threshold_map = _local_threshold(
            image,
            block_size=local_block_size,
            offset=local_offset,
        ) * threshold_scale
        mask = np.isfinite(image) & (image > threshold_map)
        finite_thresholds = threshold_map[np.isfinite(threshold_map)]
        if finite_thresholds.size:
            summary = {
                "median": float(np.nanmedian(finite_thresholds)),
                "min": float(np.nanmin(finite_thresholds)),
                "max": float(np.nanmax(finite_thresholds)),
            }
            threshold = summary["median"]
        else:
            summary = {"median": float("nan"), "min": float("nan"), "max": float("nan")}
            threshold = float("nan")
        return mask, threshold, summary
    if threshold_method == "watershed":
        threshold = otsu_threshold(image) * threshold_scale
        return np.isfinite(image) & (image > threshold), float(threshold), None
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
    opening_radius: int | None,
    closing_radius: int | None,
    fill_holes: bool,
    max_hole_area: int | None,
    clear_border: bool,
) -> np.ndarray:
    out = np.asarray(mask, dtype=bool)
    if opening_radius is None:
        opening_radius = morphology_radius
    if closing_radius is None:
        closing_radius = morphology_radius
    if opening_radius > 0 or closing_radius > 0:
        try:
            from skimage import morphology

            if opening_radius > 0:
                footprint = morphology.disk(int(opening_radius))
                out = morphology.binary_opening(out, footprint=footprint)
            if closing_radius > 0:
                footprint = morphology.disk(int(closing_radius))
                out = morphology.binary_closing(out, footprint=footprint)
        except Exception as exc:
            raise RuntimeError("Segmentation morphology cleanup requires scikit-image") from exc
    if max_hole_area is not None and max_hole_area > 0:
        try:
            import inspect
            from skimage import morphology

            params = inspect.signature(morphology.remove_small_holes).parameters
            if "max_size" in params:
                out = morphology.remove_small_holes(out, max_size=int(max_hole_area))
            else:
                out = morphology.remove_small_holes(out, area_threshold=int(max_hole_area))
        except Exception as exc:
            raise RuntimeError("Bounded segmentation hole filling requires scikit-image") from exc
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


def _watershed_labels(mask: np.ndarray, *, min_distance: int) -> np.ndarray:
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
    pixel_size_um: float | tuple[float, float] | None = None,
) -> tuple[np.ndarray, list[SegmentationRegion]]:
    output = np.zeros(labels.shape, dtype=np.int32)
    regions: list[SegmentationRegion] = []
    pixel_size = _normalize_pixel_size(pixel_size_um)
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
        centroid_row = float(np.mean(coords[0])) if area else float("nan")
        centroid_col = float(np.mean(coords[1])) if area else float("nan")
        bbox = (r0, c0, r1, c1)
        eccentricity = _region_eccentricity(labels == label)
        area_um2 = centroid_y_um = centroid_x_um = height_um = width_um = None
        if pixel_size is not None:
            pixel_y_um, pixel_x_um = pixel_size
            area_um2 = float(area * pixel_y_um * pixel_x_um)
            centroid_y_um = float(centroid_row * pixel_y_um)
            centroid_x_um = float(centroid_col * pixel_x_um)
            height_um = float((r1 - r0) * pixel_y_um)
            width_um = float((c1 - c0) * pixel_x_um)
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
                centroid_row=centroid_row,
                centroid_col=centroid_col,
                bbox=bbox,
                eccentricity=eccentricity,
                area_um2=area_um2,
                centroid_y_um=centroid_y_um,
                centroid_x_um=centroid_x_um,
                height_um=height_um,
                width_um=width_um,
            )
        )
        next_label += 1
    return output, regions


def _normalize_pixel_size(
    pixel_size_um: float | tuple[float, float] | None,
) -> tuple[float, float] | None:
    if pixel_size_um is None:
        return None
    if isinstance(pixel_size_um, tuple):
        if len(pixel_size_um) != 2:
            raise ValueError("pixel_size_um tuple must be (y_um, x_um)")
        pixel_y_um, pixel_x_um = pixel_size_um
    else:
        pixel_y_um = pixel_x_um = float(pixel_size_um)
    pixel_y_um = float(pixel_y_um)
    pixel_x_um = float(pixel_x_um)
    if pixel_y_um <= 0 or pixel_x_um <= 0:
        raise ValueError("pixel_size_um must be positive")
    return pixel_y_um, pixel_x_um


def _region_eccentricity(mask: np.ndarray) -> float:
    try:
        from skimage import measure

        props = measure.regionprops(mask.astype(np.uint8))
        if not props:
            return float("nan")
        return float(props[0].eccentricity)
    except Exception:
        return float("nan")


__all__ = [
    "ThresholdMethod",
    "LabelMethod",
    "SegmentationRegion",
    "SegmentationAnalysis",
    "segment_image",
    "prepare_segmentation_image",
    "otsu_threshold",
]
