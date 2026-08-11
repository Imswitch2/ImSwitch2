"""Multi-ROI helpers for ImProcess."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# ROIRecord moved to the shared imcommon layer so imcontrol workflows and
# improcess can both use it without either importing the other. Re-exported
# here for backward compatibility with existing ``from ...roi_manager import
# ROIRecord`` call sites.
from imswitch.imcommon.algorithms.line_sampling import polyline_samples
from imswitch.imcommon.algorithms.roi_geometry import (
    roi_capabilities,
    roi_from_vertices,
    roi_mask_local,
)
from imswitch.imcommon.algorithms.roi import (
    ROIRecord,
    duplicated,
    new_uid,
    replaced,
)

from .roi_stats import ROIStats, stats_from_values


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


def _stats_from_values(values: dict) -> ROIStats:
    """The legacy eight-statistic view of a registry row.

    Kept so callers that predate the registry keep working, without measuring
    the same ROI twice. A statistic the user did not select is NaN here — the
    row is what was asked for, and this is only a view onto it.
    """
    nan = float("nan")

    def number(key: str) -> float:
        try:
            return float(values.get(key, nan))
        except (TypeError, ValueError):
            return nan

    def count(key: str) -> int:
        try:
            return int(values.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    return ROIStats(
        area_pixels=count("area_px"),
        finite_pixels=count("finite_px"),
        mean=number("mean"),
        median=number("median"),
        std=number("std"),
        minimum=number("min"),
        maximum=number("max"),
        total=number("sum"),
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
    #: Registry-backed measurements for this ROI, when a selection was asked
    #: for. Empty for the legacy eight-statistic path.
    values: dict = field(default_factory=dict)

    @property
    def note(self) -> str:
        """Short human-readable reason the statistics are missing, else ''."""
        if self.error:
            return self.error
        if not self.measured:
            return "hidden"
        return ""

    def to_row(self) -> dict[str, object]:
        """One flat row for the table and for export.

        With a registry row present, the eight legacy statistics are left out:
        the registry already carries them under the ids the columns are named
        for, and emitting both would export ``mean`` twice with one of the two
        silently winning.
        """
        identity = {
            "name": self.roi.name,
            "type": self.roi.roi_type,
            "source": self.roi.source,
            "bounds": self.roi.bounds,
            "visible": self.roi.visible,
            "measured": self.measured,
        }
        if self.values:
            return {**identity, **self.values, "note": self.note}
        return {
            **identity,
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

    def get_by_uid(self, uid: str) -> ROIRecord | None:
        """The ROI with this identity.

        The lookup the panel uses: names are display text the user can edit
        and can collide on import, so a row must resolve through something
        that cannot change under it.
        """
        for roi in self._rois:
            if roi.uid and roi.uid == uid:
                return roi
        return None

    def update(self, target: str, **changes) -> ROIRecord:
        """Change fields of one ROI in place, preserving its identity.

        Identity is not an ordinary field: an update that could rewrite it, or
        rename an ROI onto a name already in use, would break the two
        invariants everything else relies on, so both are refused rather than
        quietly applied.
        """
        roi = self.get(target)
        if roi is None:
            raise KeyError(target)
        if "uid" in changes and changes["uid"] != roi.uid:
            raise ValueError(
                "an ROI's identity cannot be changed by an update; "
                "duplicate it if a new ROI is what you want"
            )
        new_name = changes.get("name", roi.name)
        if new_name != roi.name and self.get(new_name) is not None:
            raise ValueError(f"ROI {new_name!r} already exists")
        updated = replaced(roi, **changes)
        self._rois = [updated if item.name == target else item for item in self._rois]
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
        selection=None,
        row_scale: float = 1.0,
        col_scale: float = 1.0,
        unit: str = "px",
        threshold=None,
        line_width: int = 1,
        geometry_match_for=None,
        cache=None,
        cache_key_for=None,
    ) -> list[ROIStatsRecord]:
        """Measure the managed ROIs against ``image``.

        Never raises for a single bad ROI: one that is empty or falls outside
        the image comes back with NaN statistics and an ``error``, so the other
        ROIs still report their numbers.

        ``visible_only`` drops hidden ROIs from the output entirely. Otherwise
        they are still listed — the panel needs a row to hold the checkbox that
        turns them back on — but are not measured unless ``measure_hidden``.

        ``selection`` switches on the measurement registry; without it only the
        eight legacy statistics are produced, which is what existing callers
        expect. ``cache_key_for`` returns an opaque key per ROI (None disables
        caching for it), so the model never has to know what makes two
        measurements the same — only the caller can know that.
        """
        records: list[ROIStatsRecord] = []
        caching = cache is not None and cache_key_for is not None
        for roi in self._rois:
            if not roi.visible:
                if visible_only:
                    continue
                if not measure_hidden:
                    records.append(
                        ROIStatsRecord(roi=roi, stats=_empty_stats(), measured=False)
                    )
                    continue
            key = cache_key_for(roi) if caching else None
            if key is not None:
                hit = cache.get(key)
                if hit is not None:
                    records.append(
                        ROIStatsRecord(
                            roi=roi, stats=_stats_from_values(hit), values=dict(hit)
                        )
                    )
                    continue
            try:
                if selection is not None:
                    values = measure_roi(
                        image, roi,
                        selection=selection,
                        row_scale=row_scale, col_scale=col_scale, unit=unit,
                        threshold=threshold,
                        line_width=line_width,
                        geometry_match=(
                            geometry_match_for(roi)
                            if geometry_match_for is not None
                            else "exact"
                        ),
                    )
                    # Derived rather than measured a second time: masking the
                    # ROI twice to produce the same numbers under two names is
                    # work no one asked for.
                    stats = _stats_from_values(values)
                else:
                    stats = _compute_roi_record_stats(image, roi)
                    values = {}
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
            if key is not None:
                cache.put(key, dict(values))
            records.append(ROIStatsRecord(roi=roi, stats=stats, values=values))
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
    """Build a rectangular ROI from napari shape vertices in row/column order.

    Signature preserved for existing callers; the work is done by the shared
    geometry helper so a rectangle is built exactly the way every other shape
    type is.
    """
    return roi_from_shape(vertices, shape_type="rectangle", name=name, source=source)


#: napari shape type -> the roi_type recorded for it. Points are absent
#: deliberately: a Shapes layer has no point type, and point ROIs need a
#: Points layer of their own (deferred to the points phase).
_SHAPE_TYPE_TO_ROI_TYPE = {
    "rectangle": "rectangle",
    "ellipse": "ellipse",
    "polygon": "polygon",
    "path": "freehand",
    "line": "line",
}


def roi_from_shape(
    vertices,
    *,
    shape_type: str,
    name: str = "ROI",
    source: str = "manual",
    position: tuple[tuple[str, int], ...] = (),
    frame_uid: str = "",
) -> ROIRecord:
    """Build an ROI from one drawn napari shape.

    An axis-aligned rectangle keeps only its bounds — that is all it is — while
    every other type keeps its vertices, so a polygon measures as a polygon and
    a rotated rectangle as the rotated shape.
    """
    arr = np.asarray(vertices, dtype=np.float64)
    if arr.ndim != 2 or arr.shape[1] < 2:
        raise ValueError(f"Shape vertices must have shape (N, >=2), got {arr.shape}")
    arr = arr[:, -2:]

    roi_type = _SHAPE_TYPE_TO_ROI_TYPE.get(str(shape_type).lower())
    if roi_type is None:
        raise ValueError(f"Unsupported shape type {shape_type!r}")

    if roi_type == "rectangle" and _is_axis_aligned(arr):
        rows, cols = arr[:, 0], arr[:, 1]
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
            position=position,
            frame_uid=frame_uid,
        )

    return roi_from_vertices(
        arr,
        roi_type=roi_type,
        name=name,
        source=source,
        position=position,
        frame_uid=frame_uid,
    )


def _is_axis_aligned(vertices: np.ndarray, *, tol: float = 1e-6) -> bool:
    """True when a 4-corner shape's edges run along the axes.

    A rotated rectangle is not, and must keep its vertices — dropping them
    would turn it into the axis-aligned box that encloses it.
    """
    if len(vertices) != 4:
        return False
    rows = np.unique(np.round(vertices[:, 0] / max(tol, 1e-12)) * max(tol, 1e-12))
    cols = np.unique(np.round(vertices[:, 1] / max(tol, 1e-12)) * max(tol, 1e-12))
    return len(rows) <= 2 and len(cols) <= 2


def measure_roi(
    image: np.ndarray,
    roi: ROIRecord,
    *,
    selection=None,
    row_scale: float = 1.0,
    col_scale: float = 1.0,
    unit: str = "px",
    plane: tuple = (),
    threshold=None,
    geometry_match: str = "exact",
    line_width: int = 1,
) -> dict:
    """Every selected measurement for one ROI on one plane.

    The single entry point the panel and the job runner both use, so the
    columns in the table and the columns pushed to the Results dock are
    produced by the same code rather than two that agree by inspection.
    """
    from .roi_measurements import MeasurementContext, measure

    arr = np.asarray(image)
    if arr.ndim != 2:
        raise ValueError(f"ROI measurement expects a 2D image, got shape {arr.shape}")

    if roi_capabilities(roi.roi_type).is_line:
        # A line has no interior, so there is nothing for the area rasteriser
        # to produce — and asking it anyway is what made every line ROI
        # unmeasurable through Measure and Multi Measure. Sample along it
        # instead, through the same sampler the Profile panel plots.
        samples = polyline_samples(arr, roi.vertices or (), width=int(line_width))
        if samples is None or samples.size == 0:
            raise ValueError(f"ROI {roi.name!r} has no line to sample")
        empty = np.zeros((0, 0), dtype=bool)
        context = MeasurementContext(
            local_mask=empty,
            local_image=empty.astype(np.float64),
            roi=roi,
            samples=samples,
            line_width=int(line_width),
            row_scale=float(row_scale),
            col_scale=float(col_scale),
            unit=unit,
            plane=tuple(plane),
            threshold=threshold,
            geometry_match=geometry_match,
        )
        return measure(context, selection)

    local, slices = roi_mask_local(roi, arr.shape)
    if local.size == 0 or not local.any():
        raise ValueError(f"ROI {roi.name!r} is empty after clipping to the image")
    context = MeasurementContext(
        local_mask=local,
        local_image=arr[slices].astype(np.float64, copy=False),
        roi=roi,
        row_scale=float(row_scale),
        col_scale=float(col_scale),
        unit=unit,
        plane=tuple(plane),
        threshold=threshold,
        geometry_match=geometry_match,
    )
    return measure(context, selection)


def roi_values(image: np.ndarray, roi: ROIRecord) -> np.ndarray:
    """The image values inside ``roi``.

    The single extraction path: the ROI manager, PSF resolution and
    colocalization all read pixels through this, so a polygon or a segmentation
    mask cannot be measured as a polygon in one panel and as its bounding box
    in another.
    """
    arr = np.asarray(image)
    if arr.ndim != 2:
        raise ValueError(f"ROI statistics expect a 2D image, got shape {arr.shape}")
    local, slices = roi_mask_local(roi, arr.shape)
    if local.size == 0 or not local.any():
        raise ValueError(f"ROI {roi.name!r} is empty after clipping to the image")
    # Slice, mask, *then* convert. Converting the whole image to float64 first
    # costs a full image copy per ROI — which for a 200-ROI set on a 2048²
    # image is exactly the work the local rasteriser exists to avoid.
    return arr[slices][local].astype(np.float64, copy=False)


def _compute_roi_record_stats(image: np.ndarray, roi: ROIRecord) -> ROIStats:
    """Statistics over an ROI, whatever its shape.

    Everything goes through the shared rasteriser now, so a polygon measures
    as a polygon and a composite as its exact pixels — rather than a rectangle
    ROI taking one code path and everything else quietly taking another.
    """
    # Through the shared statistics helper, which is itself a view onto the
    # measurement registry. Computing them again here is how the legacy path
    # kept `ddof=0` after the registry moved to ImageJ's `ddof=1`: two
    # definitions of "standard deviation" in one panel, differing by a factor
    # nobody could see.
    values = roi_values(image, roi)
    finite = values[np.isfinite(values)]
    return stats_from_values(finite, area_pixels=int(values.size))
