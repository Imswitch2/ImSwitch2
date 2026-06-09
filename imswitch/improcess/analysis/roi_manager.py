"""Multi-ROI helpers for ImProcess."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from .roi_stats import ROIStats, compute_roi_stats


@dataclass(frozen=True)
class ROIRecord:
    """A named ROI in image row/column coordinates."""

    name: str
    roi_type: str
    bounds: tuple[int, int, int, int]
    visible: bool = True
    source: str = "manual"
    pixels: tuple[tuple[int, int], ...] | None = None

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["bounds"] = list(self.bounds)
        if self.pixels is not None:
            data["pixels"] = [list(pixel) for pixel in self.pixels]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "ROIRecord":
        bounds = data.get("bounds", (0, 0, 0, 0))
        pixels = data.get("pixels")
        return cls(
            name=str(data.get("name", "ROI")),
            roi_type=str(data.get("roi_type", "rectangle")),
            bounds=tuple(int(v) for v in bounds),  # type: ignore[arg-type]
            visible=bool(data.get("visible", True)),
            source=str(data.get("source", "manual")),
            pixels=(
                tuple((int(row), int(col)) for row, col in pixels)  # type: ignore[union-attr]
                if pixels is not None
                else None
            ),
        )


@dataclass(frozen=True)
class ROIStatsRecord:
    """Statistics for one managed ROI."""

    roi: ROIRecord
    stats: ROIStats

    def to_row(self) -> dict[str, object]:
        return {
            "name": self.roi.name,
            "type": self.roi.roi_type,
            "source": self.roi.source,
            "bounds": self.roi.bounds,
            "visible": self.roi.visible,
            "area_pixels": self.stats.area_pixels,
            "finite_pixels": self.stats.finite_pixels,
            "mean": self.stats.mean,
            "median": self.stats.median,
            "std": self.stats.std,
            "min": self.stats.minimum,
            "max": self.stats.maximum,
            "sum": self.stats.total,
        }


class ROIManagerModel:
    """In-memory ImageJ-like ROI list model."""

    def __init__(self, rois: list[ROIRecord] | None = None):
        self._rois = list(rois or [])

    @property
    def rois(self) -> list[ROIRecord]:
        return list(self._rois)

    def add(self, roi: ROIRecord, *, replace: bool = False) -> ROIRecord:
        if replace:
            self.remove(roi.name)
        elif self.get(roi.name) is not None:
            roi = ROIRecord(
                name=self.unique_name(roi.name),
                roi_type=roi.roi_type,
                bounds=roi.bounds,
                visible=roi.visible,
                source=roi.source,
                pixels=roi.pixels,
            )
        self._rois.append(roi)
        return roi

    def remove(self, name: str) -> bool:
        before = len(self._rois)
        self._rois = [roi for roi in self._rois if roi.name != name]
        return len(self._rois) != before

    def clear(self) -> None:
        self._rois.clear()

    def get(self, name: str) -> ROIRecord | None:
        for roi in self._rois:
            if roi.name == name:
                return roi
        return None

    def rename(self, old_name: str, new_name: str) -> ROIRecord:
        roi = self.get(old_name)
        if roi is None:
            raise KeyError(old_name)
        if new_name != old_name and self.get(new_name) is not None:
            raise ValueError(f"ROI {new_name!r} already exists")
        updated = ROIRecord(
            name=new_name,
            roi_type=roi.roi_type,
            bounds=roi.bounds,
            visible=roi.visible,
            source=roi.source,
            pixels=roi.pixels,
        )
        self._rois = [updated if item.name == old_name else item for item in self._rois]
        return updated

    def duplicate(self, name: str) -> ROIRecord:
        roi = self.get(name)
        if roi is None:
            raise KeyError(name)
        return self.add(
            ROIRecord(
                name=self.unique_name(f"{roi.name}_copy"),
                roi_type=roi.roi_type,
                bounds=roi.bounds,
                visible=roi.visible,
                source=roi.source,
                pixels=roi.pixels,
            )
        )

    def set_visible(self, name: str, visible: bool) -> ROIRecord:
        roi = self.get(name)
        if roi is None:
            raise KeyError(name)
        updated = ROIRecord(
            name=roi.name,
            roi_type=roi.roi_type,
            bounds=roi.bounds,
            visible=bool(visible),
            source=roi.source,
            pixels=roi.pixels,
        )
        self._rois = [updated if item.name == name else item for item in self._rois]
        return updated

    def unique_name(self, base: str = "ROI") -> str:
        names = {roi.name for roi in self._rois}
        if base not in names:
            return base
        i = 1
        while f"{base}_{i}" in names:
            i += 1
        return f"{base}_{i}"

    def compute_stats(self, image: np.ndarray, *, visible_only: bool = False) -> list[ROIStatsRecord]:
        rois = [roi for roi in self._rois if roi.visible or not visible_only]
        return [
            ROIStatsRecord(roi=roi, stats=_compute_roi_record_stats(image, roi))
            for roi in rois
        ]

    def to_dicts(self) -> list[dict[str, object]]:
        return [roi.to_dict() for roi in self._rois]

    @classmethod
    def from_dicts(cls, data: list[dict[str, object]]) -> "ROIManagerModel":
        return cls([ROIRecord.from_dict(item) for item in data])


def rectangle_roi_from_vertices(
    vertices: np.ndarray,
    *,
    name: str = "ROI",
    source: str = "manual",
) -> ROIRecord:
    """Build a rectangular ROI from napari shape vertices in row/column order."""
    arr = np.asarray(vertices, dtype=np.float64)
    if arr.ndim != 2 or arr.shape[1] < 2:
        raise ValueError(f"Rectangle vertices must have shape (N, >=2), got {arr.shape}")
    rows = arr[:, 0]
    cols = arr[:, 1]
    r0 = int(round(float(np.nanmin(rows))))
    r1 = int(round(float(np.nanmax(rows))))
    c0 = int(round(float(np.nanmin(cols))))
    c1 = int(round(float(np.nanmax(cols))))
    if r0 == r1 or c0 == c1:
        raise ValueError("Rectangle ROI has zero area")
    return ROIRecord(
        name=name,
        roi_type="rectangle",
        bounds=(r0, r1, c0, c1),
        source=source,
    )


def _compute_roi_record_stats(image: np.ndarray, roi: ROIRecord) -> ROIStats:
    if roi.pixels is None:
        return compute_roi_stats(image, roi.bounds)

    arr = np.asarray(image, dtype=np.float64)
    if arr.ndim != 2:
        raise ValueError(f"ROI statistics expect a 2D image, got shape {arr.shape}")
    coords = np.asarray(roi.pixels, dtype=np.int64)
    if coords.size == 0:
        raise ValueError("Mask ROI has no pixels")
    coords = coords.reshape((-1, 2))
    inside = (
        (coords[:, 0] >= 0)
        & (coords[:, 0] < arr.shape[0])
        & (coords[:, 1] >= 0)
        & (coords[:, 1] < arr.shape[1])
    )
    coords = coords[inside]
    if coords.size == 0:
        raise ValueError("Mask ROI is empty after clipping to image bounds")
    values = arr[coords[:, 0], coords[:, 1]]
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return ROIStats(
            area_pixels=int(values.size),
            finite_pixels=0,
            mean=float("nan"),
            median=float("nan"),
            std=float("nan"),
            minimum=float("nan"),
            maximum=float("nan"),
            total=float("nan"),
        )
    return ROIStats(
        area_pixels=int(values.size),
        finite_pixels=int(finite.size),
        mean=float(np.mean(finite)),
        median=float(np.median(finite)),
        std=float(np.std(finite)),
        minimum=float(np.min(finite)),
        maximum=float(np.max(finite)),
        total=float(np.sum(finite)),
    )
