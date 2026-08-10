"""Multi-ROI helpers for ImProcess."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# ROIRecord moved to the shared imcommon layer so imcontrol workflows and
# improcess can both use it without either importing the other. Re-exported
# here for backward compatibility with existing ``from ...roi_manager import
# ROIRecord`` call sites.
from imswitch.imcommon.algorithms.roi_geometry import roi_mask_local
from imswitch.imcommon.algorithms.roi import (
    ROIRecord,
    duplicated,
    new_uid,
    replaced,
)

from .roi_stats import ROIStats, compute_roi_stats


def _empty_stats(area_pixels: int = 0) -> ROIStats:
    """Statistics for an ROI that could not be measured."""
    nan = float("nan")
    return ROIStats(
        area_pixels=int(area_pixels),
        finite_pixels=0,
        mean=nan,
        median=nan,
        std=nan,
        minimum=nan,
        maximum=nan,
        total=nan,
    )


@dataclass(frozen=True)
class ROIStatsRecord:
    """Statistics for one managed ROI.

    ``measured`` is False for ROIs that were deliberately skipped (hidden), and
    ``error`` carries the reason an ROI could not be measured. Both exist so a
    single unmeasurable ROI reports itself instead of aborting the whole batch.
    """

    roi: ROIRecord
    stats: ROIStats
    measured: bool = True
    error: str | None = None

    @property
    def note(self) -> str:
        """Short human-readable reason the statistics are missing, else ''."""
        if self.error:
            return self.error
        if not self.measured:
            return "hidden"
        return ""

    def to_row(self) -> dict[str, object]:
        return {
            "name": self.roi.name,
            "type": self.roi.roi_type,
            "source": self.roi.source,
            "bounds": self.roi.bounds,
            "visible": self.roi.visible,
            "measured": self.measured,
            "area_pixels": self.stats.area_pixels,
            "finite_pixels": self.stats.finite_pixels,
            "mean": self.stats.mean,
            "median": self.stats.median,
            "std": self.stats.std,
            "min": self.stats.minimum,
            "max": self.stats.maximum,
            "sum": self.stats.total,
            "note": self.note,
        }


class ROIManagerModel:
    """In-memory ImageJ-like ROI list model."""

    def __init__(self, rois: list[ROIRecord] | None = None):
        # Seeding goes through add() so the "names are unique" invariant holds
        # however the model was built, not only when ROIs arrive one at a time.
        self._rois: list[ROIRecord] = []
        for roi in rois or []:
            self.add(roi)

    @property
    def rois(self) -> list[ROIRecord]:
        return list(self._rois)

    def add(self, roi: ROIRecord, *, replace: bool = False) -> ROIRecord:
        if replace:
            self.remove(roi.name)
        elif self.get(roi.name) is not None:
            roi = replaced(roi, name=self.unique_name(roi.name))
        # Identity is assigned on the way in, on every path, so nothing
        # downstream ever has to cope with an ROI that has no uid.
        if not roi.uid:
            roi = replaced(roi, uid=new_uid())
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
        updated = replaced(roi, name=new_name)
        self._rois = [updated if item.name == old_name else item for item in self._rois]
        return updated

    def duplicate(self, name: str) -> ROIRecord:
        roi = self.get(name)
        if roi is None:
            raise KeyError(name)
        # duplicated() is the one operation that mints a new identity: a copy
        # is a different ROI, however identical its geometry.
        return self.add(duplicated(roi, name=self.unique_name(f"{roi.name}_copy")))

    def set_visible(self, name: str, visible: bool) -> ROIRecord:
        roi = self.get(name)
        if roi is None:
            raise KeyError(name)
        updated = replaced(roi, visible=bool(visible))
        self._rois = [updated if item.name == name else item for item in self._rois]
        return updated

    def set_rois(self, rois) -> None:
        """Replace the whole list, keeping the model's invariants.

        Used by undo to restore an ROI at the position it was removed from;
        order is user-visible, so appending it to the end would be a change of
        its own.
        """
        self._rois = []
        for roi in rois:
            self.add(roi)

    def update(self, name: str, **changes) -> ROIRecord:
        """Change fields of one ROI in place, preserving its identity."""
        roi = self.get(name)
        if roi is None:
            raise KeyError(name)
        updated = replaced(roi, **changes)
        self._rois = [updated if item.name == name else item for item in self._rois]
        return updated

    def replace_record(self, roi: ROIRecord) -> ROIRecord:
        """Put ``roi`` back, matched on identity rather than name.

        Undoing a rename has to find the record whose name has already
        changed, so the uid is the only reliable handle.
        """
        for index, item in enumerate(self._rois):
            if (item.uid and item.uid == roi.uid) or item.name == roi.name:
                self._rois[index] = roi
                return roi
        return self.add(roi)

    def unique_name(self, base: str = "ROI") -> str:
        names = {roi.name for roi in self._rois}
        if base not in names:
            return base
        i = 1
        while f"{base}_{i}" in names:
            i += 1
        return f"{base}_{i}"

    def compute_stats(
        self,
        image: np.ndarray,
        *,
        visible_only: bool = False,
        measure_hidden: bool = False,
    ) -> list[ROIStatsRecord]:
        """Measure the managed ROIs against ``image``.

        Never raises for a single bad ROI: one that is empty or falls outside
        the image comes back with NaN statistics and an ``error``, so the other
        ROIs still report their numbers.

        ``visible_only`` drops hidden ROIs from the output entirely. Otherwise
        they are still listed — the panel needs a row to hold the checkbox that
        turns them back on — but are not measured unless ``measure_hidden``.
        """
        records: list[ROIStatsRecord] = []
        for roi in self._rois:
            if not roi.visible:
                if visible_only:
                    continue
                if not measure_hidden:
                    records.append(
                        ROIStatsRecord(roi=roi, stats=_empty_stats(), measured=False)
                    )
                    continue
            try:
                stats = _compute_roi_record_stats(image, roi)
            except Exception as exc:
                records.append(
                    ROIStatsRecord(
                        roi=roi,
                        stats=_empty_stats(),
                        measured=False,
                        error=str(exc) or exc.__class__.__name__,
                    )
                )
                continue
            records.append(ROIStatsRecord(roi=roi, stats=stats))
        return records

    def to_dicts(self) -> list[dict[str, object]]:
        return [roi.to_dict() for roi in self._rois]

    @classmethod
    def from_dicts(cls, data: list[dict[str, object]]) -> "ROIManagerModel":
        # Route every record through add() rather than seeding the list
        # directly: names are the panel's row key, and a payload holding two
        # "cell" entries would otherwise produce two rows that both resolve to
        # the first one — and a delete that removes both.
        model = cls()
        for item in data:
            model.add(ROIRecord.from_dict(item))
        return model


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


def roi_values(image: np.ndarray, roi: ROIRecord) -> np.ndarray:
    """The image values inside ``roi``.

    The single extraction path: the ROI manager, PSF resolution and
    colocalization all read pixels through this, so a polygon or a segmentation
    mask cannot be measured as a polygon in one panel and as its bounding box
    in another.
    """
    arr = np.asarray(image, dtype=np.float64)
    if arr.ndim != 2:
        raise ValueError(f"ROI statistics expect a 2D image, got shape {arr.shape}")
    local, slices = roi_mask_local(roi, arr.shape)
    if local.size == 0 or not local.any():
        raise ValueError(f"ROI {roi.name!r} is empty after clipping to the image")
    return arr[slices][local]


def _compute_roi_record_stats(image: np.ndarray, roi: ROIRecord) -> ROIStats:
    """Statistics over an ROI, whatever its shape.

    Everything goes through the shared rasteriser now, so a polygon measures
    as a polygon and a composite as its exact pixels — rather than a rectangle
    ROI taking one code path and everything else quietly taking another.
    """
    values = roi_values(image, roi)
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return _empty_stats(area_pixels=int(values.size))
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
