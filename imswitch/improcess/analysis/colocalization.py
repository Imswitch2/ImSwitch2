"""Colocalization helpers for ImProcess."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .roi_manager import ROIRecord


@dataclass(frozen=True)
class ColocalizationRecord:
    """Colocalization metrics for one image region."""

    name: str
    bounds: tuple[int, int, int, int] | None
    pixel_count: int
    pearson: float
    manders_m1: float
    manders_m2: float
    overlap_coefficient: float
    threshold_a: float
    threshold_b: float
    mean_a: float
    mean_b: float

    def to_row(self) -> dict[str, object]:
        return {
            "name": self.name,
            "bounds": self.bounds,
            "pixel_count": self.pixel_count,
            "pearson": self.pearson,
            "manders_m1": self.manders_m1,
            "manders_m2": self.manders_m2,
            "overlap_coefficient": self.overlap_coefficient,
            "threshold_a": self.threshold_a,
            "threshold_b": self.threshold_b,
            "mean_a": self.mean_a,
            "mean_b": self.mean_b,
        }


@dataclass(frozen=True)
class ColocalizationAnalysis:
    """Batch colocalization output."""

    records: list[ColocalizationRecord]
    scatter_a: np.ndarray
    scatter_b: np.ndarray
    metadata: dict[str, object]

    def rows(self) -> list[dict[str, object]]:
        return [record.to_row() for record in self.records]


def colocalization_metrics(
    image_a: np.ndarray,
    image_b: np.ndarray,
    *,
    roi: ROIRecord | tuple[int, int, int, int] | None = None,
    name: str = "Full image",
    threshold_a: float = 0.0,
    threshold_b: float = 0.0,
) -> ColocalizationRecord:
    """Compute colocalization metrics for two 2D images."""
    a, b, bounds = _extract_values(image_a, image_b, roi)
    if a.size == 0:
        raise ValueError("Colocalization region has no finite paired pixels")
    pearson = _pearson(a, b)
    positive_a = np.clip(a - float(threshold_a), 0.0, None)
    positive_b = np.clip(b - float(threshold_b), 0.0, None)
    coloc = (a > threshold_a) & (b > threshold_b)
    denom_a = float(np.sum(positive_a))
    denom_b = float(np.sum(positive_b))
    m1 = _safe_ratio(float(np.sum(positive_a[coloc])), denom_a)
    m2 = _safe_ratio(float(np.sum(positive_b[coloc])), denom_b)
    overlap = _safe_ratio(
        float(np.sum(positive_a * positive_b)),
        float(np.sqrt(np.sum(positive_a**2) * np.sum(positive_b**2))),
    )
    return ColocalizationRecord(
        name=name,
        bounds=bounds,
        pixel_count=int(a.size),
        pearson=pearson,
        manders_m1=m1,
        manders_m2=m2,
        overlap_coefficient=overlap,
        threshold_a=float(threshold_a),
        threshold_b=float(threshold_b),
        mean_a=float(np.mean(a)),
        mean_b=float(np.mean(b)),
    )


def colocalization_batch(
    image_a: np.ndarray,
    image_b: np.ndarray,
    rois: list[ROIRecord] | None = None,
    *,
    threshold_a: float = 0.0,
    threshold_b: float = 0.0,
    scatter_limit: int = 5000,
) -> ColocalizationAnalysis:
    """Compute colocalization metrics for full image or each ROI."""
    targets: list[ROIRecord | None] = rois or [None]
    records = [
        colocalization_metrics(
            image_a,
            image_b,
            roi=roi,
            name=roi.name if roi is not None else "Full image",
            threshold_a=threshold_a,
            threshold_b=threshold_b,
        )
        for roi in targets
    ]
    scatter_a, scatter_b, _bounds = _extract_values(image_a, image_b, None)
    if scatter_a.size > scatter_limit:
        step = int(np.ceil(scatter_a.size / max(scatter_limit, 1)))
        scatter_a = scatter_a[::step]
        scatter_b = scatter_b[::step]
    return ColocalizationAnalysis(
        records=records,
        scatter_a=scatter_a.astype(np.float64),
        scatter_b=scatter_b.astype(np.float64),
        metadata={
            "threshold_a": float(threshold_a),
            "threshold_b": float(threshold_b),
            "region_count": len(records),
            "source": "roi" if rois else "full-image",
        },
    )


def _extract_values(
    image_a: np.ndarray,
    image_b: np.ndarray,
    roi: ROIRecord | tuple[int, int, int, int] | None,
) -> tuple[np.ndarray, np.ndarray, tuple[int, int, int, int] | None]:
    a = np.asarray(image_a, dtype=np.float64)
    b = np.asarray(image_b, dtype=np.float64)
    if a.ndim != 2 or b.ndim != 2:
        raise ValueError(f"Colocalization expects two 2D images, got {a.shape} and {b.shape}")
    if a.shape != b.shape:
        raise ValueError(f"Colocalization images must have matching shapes, got {a.shape} and {b.shape}")

    bounds = None
    if isinstance(roi, ROIRecord) and roi.pixels is not None:
        coords = np.asarray(roi.pixels, dtype=np.int64).reshape((-1, 2))
        inside = (
            (coords[:, 0] >= 0)
            & (coords[:, 0] < a.shape[0])
            & (coords[:, 1] >= 0)
            & (coords[:, 1] < a.shape[1])
        )
        coords = coords[inside]
        av = a[coords[:, 0], coords[:, 1]]
        bv = b[coords[:, 0], coords[:, 1]]
        bounds = roi.bounds
    else:
        bounds = roi.bounds if isinstance(roi, ROIRecord) else roi
        if bounds is None:
            av = a.ravel()
            bv = b.ravel()
        else:
            r0, r1, c0, c1 = _clip_bounds(bounds, a.shape)
            av = a[r0:r1, c0:c1].ravel()
            bv = b[r0:r1, c0:c1].ravel()
            bounds = (r0, r1, c0, c1)

    finite = np.isfinite(av) & np.isfinite(bv)
    return av[finite], bv[finite], bounds


def _clip_bounds(bounds: tuple[int, int, int, int], shape: tuple[int, int]):
    r0, r1, c0, c1 = bounds
    rlo, rhi = sorted((int(round(r0)), int(round(r1))))
    clo, chi = sorted((int(round(c0)), int(round(c1))))
    rlo, rhi = max(0, rlo), min(shape[0], rhi)
    clo, chi = max(0, clo), min(shape[1], chi)
    if rlo >= rhi or clo >= chi:
        raise ValueError("Colocalization ROI is empty after clipping")
    return rlo, rhi, clo, chi


def _pearson(a: np.ndarray, b: np.ndarray) -> float:
    if a.size < 2:
        return float("nan")
    aa = a - np.mean(a)
    bb = b - np.mean(b)
    denom = float(np.sqrt(np.sum(aa**2) * np.sum(bb**2)))
    return _safe_ratio(float(np.sum(aa * bb)), denom)


def _safe_ratio(num: float, denom: float) -> float:
    if denom == 0 or not np.isfinite(denom):
        return float("nan")
    return float(num / denom)
