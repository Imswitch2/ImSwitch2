"""Semantic result assembly descriptors and logical publication coverage.

This module deliberately does not concatenate arrays.  Acquisition membership
and job scope establish *why* outputs can be combined; a reconstructor's data
shape cannot answer that question.  M2D-B/C will supply scientific-result
adapters and consume these descriptors when assembling finite results.

Pure Python: safe for workers, controllers and tests without Qt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from imswitch.improcess.model.timepoint_scope import (
    TimepointScope, coerce_timepoint_scope,
)


class AssemblyError(ValueError):
    """Missing semantic identity or incompatible output for a proposed merge."""


class AssemblyRelation(str, Enum):
    SINGLE = "single"
    TIMEPOINT = "timepoint"
    DATASET = "dataset"


def _ranges(indices: set[int]) -> list[list[int]]:
    """Compact sorted logical positions into inclusive-start/exclusive-end ranges."""
    output: list[list[int]] = []
    for index in sorted(indices):
        if output and output[-1][1] == index:
            output[-1][1] += 1
        else:
            output.append([index, index + 1])
    return output


@dataclass(frozen=True)
class ResultAssemblyPlan:
    """Job-relative meaning of individual output steps (not pixel dimensions).

    For a Session lapse, ordinal 0 maps to the first **logical** source T in
    the snapped scope. For Multi Data, independently selected acquisition IDs
    map to Dataset; an explicit lapse membership API can be added separately.
    Never choose T merely because input arrays have matching shapes.
    """

    job_id: str
    source_id: str
    relation: AssemblyRelation
    scope: TimepointScope = field(default_factory=TimepointScope.all)
    total_timepoints: int | None = None
    dataset_ids: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "relation", AssemblyRelation(self.relation))
        object.__setattr__(self, "scope", coerce_timepoint_scope(self.scope))
        if not self.job_id or not self.source_id:
            raise AssemblyError("Assembly requires explicit job and source identity")
        if self.total_timepoints is not None and int(self.total_timepoints) < 1:
            raise AssemblyError("Total timepoints must be positive when known")
        if self.relation == AssemblyRelation.DATASET:
            if not self.dataset_ids or len(set(self.dataset_ids)) != len(self.dataset_ids):
                raise AssemblyError("Dataset assembly requires distinct explicit dataset identities")
        elif self.dataset_ids:
            raise AssemblyError("Dataset identities belong only to Dataset assembly")
        if self.relation == AssemblyRelation.TIMEPOINT:
            # Fail early on invalid scoped selections, rather than replacing
            # real logical indices with a contiguous ordinal numbering.
            self.scope.bounds(self.total_timepoints)

    @classmethod
    def for_timepoints(cls, job_id: str, source_id: str, *, scope=None, total=None):
        return cls(job_id, source_id, AssemblyRelation.TIMEPOINT,
                   coerce_timepoint_scope(scope), total)

    @classmethod
    def for_datasets(cls, job_id: str, source_id: str, dataset_ids):
        return cls(job_id, source_id, AssemblyRelation.DATASET,
                   dataset_ids=tuple(str(item) for item in dataset_ids))

    @property
    def axis(self) -> str | None:
        return {AssemblyRelation.TIMEPOINT: "T", AssemblyRelation.DATASET: "Dataset"}.get(
            self.relation
        )

    @property
    def expected_count(self) -> int | None:
        if self.relation == AssemblyRelation.TIMEPOINT:
            return self.scope.count(self.total_timepoints)
        if self.relation == AssemblyRelation.DATASET:
            return len(self.dataset_ids)
        return 1

    def coordinate_for_ordinal(self, ordinal: int) -> int | str:
        """Zero-based step ordinal -> stable source coordinate."""
        index = int(ordinal)
        if index < 0 or (self.expected_count is not None and index >= self.expected_count):
            raise AssemblyError(f"Step ordinal {index} is outside the requested scope")
        if self.relation == AssemblyRelation.TIMEPOINT:
            start, _ = self.scope.bounds(self.total_timepoints)
            return start + index
        if self.relation == AssemblyRelation.DATASET:
            return self.dataset_ids[index]
        return 0

    def validate_append(self, original: Any, new: Any) -> None:
        """Validate a future assembler's use of this axis, without copying data.

        A historical MoNaLISA T can encode conditions rather than time. It is
        unsafe to append to that T even when the image shapes otherwise match.
        This gate is intentionally stricter than viewing the results separately.
        """
        if self.axis is None:
            raise AssemblyError("Single-output results do not have an append axis")
        for result in (original, new):
            labels = list(getattr(result, "axis_labels", ()) or ())
            if labels.count(self.axis) != 1:
                raise AssemblyError(f"Result must have exactly one {self.axis} axis")
            if self.axis == "T":
                projection = getattr(result, "acquisition_projection", None) or {}
                components = tuple(projection.get("components", ())) if isinstance(projection, dict) else ()
                if "condition" in components:
                    raise AssemblyError("Historical MoNaLISA T folds conditions; cannot append as elapsed time")
        if str(getattr(original, "kind", "image")) != str(getattr(new, "kind", "image")):
            raise AssemblyError("Scientific result kinds differ")
        left = tuple(getattr(original, "axis_labels", ()))
        right = tuple(getattr(new, "axis_labels", ()))
        if left != right:
            raise AssemblyError("Semantic output axes differ")
        axis = left.index(self.axis)
        for result in (original, new):
            shape = tuple(getattr(getattr(result, "data", None), "shape", ()))
            if len(shape) != len(left):
                raise AssemblyError("Result shape does not match labelled axes")
        shape_a = tuple(original.data.shape)
        shape_b = tuple(new.data.shape)
        if shape_a[:axis] + shape_a[axis+1:] != shape_b[:axis] + shape_b[axis+1:]:
            raise AssemblyError("Output geometry differs outside the assembly axis")
        if getattr(original, "scale_unit", None) != getattr(new, "scale_unit", None):
            raise AssemblyError("Output scale units differ")
        scales_a = tuple(getattr(original, "axis_scales", ()) or ())
        scales_b = tuple(getattr(new, "axis_scales", ()) or ())
        if len(scales_a) != len(left) or len(scales_b) != len(left):
            raise AssemblyError("Output pixel calibration is incomplete")
        if any(a != b for i, (a, b) in enumerate(zip(scales_a, scales_b)) if i != axis):
            raise AssemblyError("Output spatial calibrations differ")
        # Any optional layers are assembled only by a later, type-specific
        # adapter. This gate must never silently drop them.
        for component in ("background", "sharpened", "pinholes"):
            first_layer = getattr(original, component, None)
            next_layer = getattr(new, component, None)
            if (first_layer is None) != (next_layer is None):
                raise AssemblyError(f"Optional output layer {component} differs")
            if first_layer is not None:
                first_shape = tuple(getattr(first_layer, "shape", ()))
                next_shape = tuple(getattr(next_layer, "shape", ()))
                if (len(first_shape) != len(next_shape) or len(first_shape) < len(left)
                        or first_shape[:axis] + first_shape[axis+1:] !=
                        next_shape[:axis] + next_shape[axis+1:]):
                    raise AssemblyError(f"Optional output layer {component} geometry differs")


@dataclass
class ResultCoverage:
    """Tracks what has actually been published; requested != completed.

    External missing-frame ranges are retained as separate diagnostics, not
    guessed to mean an entire timepoint failed.
    """

    plan: ResultAssemblyPlan
    completed_ordinals: set[int] = field(default_factory=set)
    missing_frame_ranges: tuple[tuple[int, int], ...] = ()
    status: str = "partial"
    _contiguous_end: int = field(default=0, init=False, repr=False)

    def mark_ordinal(self, ordinal: int):
        self.plan.coordinate_for_ordinal(ordinal)
        self.completed_ordinals.add(int(ordinal))
        while self._contiguous_end in self.completed_ordinals:
            self._contiguous_end += 1

    def mark_completed_count(self, count: int):
        """Worker reports consecutive completed steps, not a logical T index."""
        count = int(count)
        if count < 0 or (self.plan.expected_count is not None and count > self.plan.expected_count):
            raise AssemblyError("Completed count is outside the requested scope")
        for ordinal in range(self._contiguous_end, count):
            self.mark_ordinal(ordinal)

    def set_missing_frames(self, ranges):
        self.missing_frame_ranges = tuple((int(a), int(b)) for a, b in (ranges or ()))
        if any(a < 0 or b <= a for a, b in self.missing_frame_ranges):
            raise AssemblyError("Missing-frame ranges must be nonempty and nonnegative")

    def finalize(self, status: str):
        if status not in ("complete", "cancelled", "failed", "partial"):
            raise AssemblyError(f"Unsupported publication outcome: {status}")
        if status == "complete" and self.plan.expected_count is not None:
            if len(self.completed_ordinals) != self.plan.expected_count:
                raise AssemblyError("Cannot finalize an incomplete logical result as complete")
        self.status = status

    def to_dict(self) -> dict:
        payload = {
            "version": 1,
            "job_id": self.plan.job_id,
            "source_id": self.plan.source_id,
            "relation": self.plan.relation.value,
            "axis": self.plan.axis,
            "status": self.status,
            "requested_scope": self.plan.scope.to_dict(self.plan.total_timepoints),
            "expected_steps": self.plan.expected_count,
            "published_steps": len(self.completed_ordinals),
            "missing_frame_ranges": [list(entry) for entry in self.missing_frame_ranges],
        }
        if self.plan.relation == AssemblyRelation.TIMEPOINT:
            start, _ = self.plan.scope.bounds(self.plan.total_timepoints)
            if len(self.completed_ordinals) == self._contiguous_end:
                # Normal progressive case: O(1) metadata even for long lapses.
                payload["published_t_ranges"] = (
                    [[start, start + self._contiguous_end]] if self._contiguous_end else []
                )
            else:
                payload["published_t_ranges"] = _ranges({
                    int(self.plan.coordinate_for_ordinal(i))
                    for i in self.completed_ordinals
                })
        elif self.plan.relation == AssemblyRelation.DATASET:
            payload["published_dataset_ids"] = [
                self.plan.coordinate_for_ordinal(i) for i in sorted(self.completed_ordinals)
            ]
        return payload


__all__ = ["AssemblyError", "AssemblyRelation", "ResultAssemblyPlan", "ResultCoverage"]
