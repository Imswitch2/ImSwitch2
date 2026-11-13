"""UI-neutral publication lifecycle shared by finite and incremental jobs.

No Qt, viewer, file IO or scientific-result ownership here.  Callbacks are
injected by ProcessingQueueController; the session/viewer keep their existing
single-result identity and autosave signal wiring.

Semantic coverage, scientific assembly, and terminal publication are shared
between finite per-item workers and frame-streaming sessions. Only scientific
outputs already published may survive cancellation.
"""

from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from typing import Callable

import numpy as np

from .result_assembly import ResultAssemblyPlan, ResultCoverage
from imswitch.improcess.model.timepoint_scope import TimepointScope
from .progressive_results import ProgressiveResultAssembler


@dataclass
class PublicationHooks:
    """Optional effects supplied by an owning queue/UI, never by a worker."""

    claim: Callable[[object], None] = lambda result: None
    annotate_scope: Callable[[object, object, int | None], None] = (
        lambda result, scope, total: None
    )
    preview: Callable[[object], None] = lambda result: None
    plane: Callable[[int, object], None] = lambda index, pixels: None
    provisional_plane: Callable[[int, object], None] = lambda index, pixels: None
    discard_provisional: Callable[[str], None] = lambda job_id: None
    timepoint_done: Callable[[int], None] = lambda index: None
    finalized: Callable[[object, str], None] = lambda result, name: None
    current_changed: Callable[[object], None] = lambda result: None
    offline_complete: Callable[[object, str, object], None] = (
        lambda result, name, reconstructor: None
    )


class ResultPublication:
    """One job, one evolving published result, one terminal notification.

    The `preview` callback may replace an existing job-owned viewer entry. A
    plane update is deliberately not coalesced here: the viewer applies pixels
    immediately and may throttle redraws independently.  No unpublished data
    is retained on Cancel or failure.
    """

    def __init__(self, plan: ResultAssemblyPlan, hooks: PublicationHooks):
        self.plan = plan
        self.hooks = hooks
        self.coverage = ResultCoverage(plan)
        self.published = None
        self._finalized = False
        self._annotated_ids: set[int] = set()
        self._assembler = None
        self._input_provenance = []
        self._provisional_view = None
        self._had_provisional = False

    def _tag(self, result):
        if result is None:
            return
        result._improcess_queue_job_id = self.plan.job_id
        if id(result) not in self._annotated_ids:
            self.hooks.annotate_scope(result, self.plan.scope, self.plan.total_timepoints)
            self.hooks.claim(result)
            self._annotated_ids.add(id(result))
        metadata = getattr(result, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
            result.metadata = metadata
        metadata["result_assembly"] = self.coverage.to_dict()
        # Preserve the M2C metadata contract until saved-result consumers have
        # migrated. 'partial' covers a cancellation/failure in that older API.
        metadata["incremental_completion"] = {
            "status": "complete" if self.coverage.status == "complete" else "partial",
            "completed_timepoints": len(self.coverage.completed_ordinals),
            "requested_timepoints": self.plan.expected_count,
        }

    def snapshot(self, result):
        if result is None or self._finalized:
            return
        # The worker can emit a completed scientific snapshot just before its
        # separate timepoint-progress event. Capture the completed coverage
        # from this snapshot *now*, so an immediate Cancel cannot misclassify
        # an already published T as unfinished. Provisional pixels never enter
        # snapshot(), so they cannot inflate scientific coverage.
        streaming = (getattr(result, "metadata", None) or {}).get("monalisa_streaming", {})
        if (self.plan.axis == "T" and "completed_timepoints" in streaming):
            self.coverage.mark_completed_count(int(streaming["completed_timepoints"]))
        self._tag(result)
        self.published = result
        # The completed scientific result is also the Session-owned viewer
        # entry. Provisional pixel overlays must not modify this object.
        self._provisional_view = None
        self.hooks.preview(result)

    def provisional_snapshot(self, result):
        """Show a temporary full-T buffer before the first completed scan."""
        if self._finalized or self.published is not None:
            raise RuntimeError("A provisional initial volume must precede completed output")
        result._improcess_queue_job_id = self.plan.job_id
        self._provisional_view = result
        self._had_provisional = True
        self.hooks.preview(result)

    def provisional_plane(self, index: int, pixels):
        """Display an unfinished T without changing Session-owned result data."""
        if not self._finalized and pixels is not None and (
                self.published is not None or self._provisional_view is not None):
            self._had_provisional = True
            self.hooks.provisional_plane(int(index), pixels)

    def append_completed(self, part, ordinal: int, *, run=None):
        """Assemble a finished independent T/Dataset step and publish in place.

        The first step creates the viewer entry; each subsequent step writes
        into the same preallocated data object. A completed step is counted
        only after every scientific output layer has been copied successfully.
        """
        if self._finalized:
            raise RuntimeError("Cannot append to a finalized result")
        if self._assembler is None:
            self._assembler = ProgressiveResultAssembler(self.plan)
        result = self._assembler.append(part, ordinal)
        self.coverage.mark_ordinal(ordinal)
        # The first result's ordinary processing_history describes only its
        # own source. Preserve every other reconstruction's source/parameters
        # and calibration, without retaining another full image array.
        entry = {
            "ordinal": int(ordinal),
            "coordinate": self.plan.coordinate_for_ordinal(ordinal),
        }
        if run is not None:
            entry["source"] = deepcopy(getattr(run, "source", {}) or {})
            entry["params"] = deepcopy(getattr(run, "params", {}) or {})
            entry["node"] = deepcopy(getattr(run, "node", {}) or {})
        calibration = (getattr(part, "metadata", None) or {}).get("monalisa_calibration")
        if calibration:
            entry["monalisa_calibration"] = deepcopy(calibration)
        self._input_provenance.append(entry)
        metadata = getattr(result, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
            result.metadata = metadata
        metadata["result_assembly_inputs"] = self._input_provenance
        self._tag(result)
        self.snapshot(result)
        if self.plan.axis == "T":
            self.hooks.timepoint_done(int(ordinal))
        return result

    def plane(self, index: int, pixels):
        if not self._finalized and self.published is not None and pixels is not None:
            self.hooks.plane(int(index), pixels)

    def timepoint_completed(self, count: int):
        if self._finalized:
            return
        self.coverage.mark_completed_count(count)
        if self.published is not None:
            self._tag(self.published)
            if count:
                self.hooks.timepoint_done(int(count) - 1)

    def missing_frames(self, ranges):
        if self._finalized:
            return
        self.coverage.set_missing_frames(ranges)
        if self.published is not None:
            self._tag(self.published)

    def _trim_streaming_completed_prefix(self) -> None:
        """Discard preallocated, unacquired T after a cancelled Follow run.

        Follow reconstructor snapshots may allocate all planned T even though
        only the completed prefix is scientifically valid. Unlike independent
        finite T assembly, they do not have a ProgressiveResultAssembler.
        """
        if self._assembler is not None or self.published is None or self.plan.axis != "T":
            return
        count = len(self.coverage.completed_ordinals)
        if count < 1 or self.coverage.completed_ordinals != set(range(count)):
            return
        labels = list(getattr(self.published, "axis_labels", ()) or ())
        if labels.count("T") != 1:
            return
        axis = labels.index("T")
        data = getattr(self.published, "data", None)
        if data is None or np.ndim(data) != len(labels) or np.shape(data)[axis] < count:
            return
        for name in ("data", "background", "sharpened", "pinholes"):
            value = getattr(self.published, name, None)
            if value is None:
                continue
            arr = np.asarray(value)
            if arr.ndim != len(labels) or arr.shape[axis] <= count:
                continue
            index = [slice(None)] * arr.ndim
            index[axis] = slice(0, count)
            setattr(self.published, name, np.array(arr[tuple(index)], copy=True))

    def finish(self, status: str, result=None):
        """Finalize the already published entry, or publish final-only output.

        `status` is 'complete', 'cancelled' or 'failed'. For incomplete runs,
        retain only snapshots already sent to the viewer. Called exactly once
        after worker teardown; it never creates a result for an early cancel.
        """
        if self._finalized:
            return None
        if status == "complete" and (result is not None or self.published is not None):
            # A finalized executor result proves the requested finite source
            # was consumed even if a queued timepoint notification arrives
            # after the final-result signal. Do not mistake delayed GUI
            # progress signals for an incomplete scientific result.
            if result is not None and self._assembler is None and self.plan.expected_count is not None:
                self.coverage.mark_completed_count(self.plan.expected_count)
            # A completed processing run with missing raw frames has a valid
            # output but not complete raw coverage; expose that distinction.
            effective = "partial" if self.coverage.missing_frame_ranges else "complete"
            self.coverage.finalize(effective)
            if result is not None:
                self.snapshot(result)
        else:
            self.coverage.finalize(status if status in ("cancelled", "failed") else "failed")
            if self._assembler is not None:
                # Finite Auto may have preallocated all planned T before the
                # writer aborted. Never expose missing future T as zero frames
                # in the retained, finalized scientific result.
                self._assembler.trim_completed_prefix()
            else:
                self._trim_streaming_completed_prefix()
        self._finalized = True
        if self.published is None:
            if self._provisional_view is not None:
                self.hooks.discard_provisional(self.plan.job_id)
                self._provisional_view = None
            return None
        if status != "complete" and self.plan.axis == "T":
            # The originally requested All-T scope is NOT the scientific
            # output's extent after an interrupted acquisition. Record the
            # verified prefix as the effective input scope, retaining the
            # original planned total and requested scope in result_assembly.
            count = len(self.coverage.completed_ordinals)
            if (count > 0 and self.coverage.completed_ordinals == set(range(count))):
                start, _ = self.plan.scope.bounds(self.plan.total_timepoints)
                self.hooks.annotate_scope(
                    self.published, TimepointScope.range(start, start + count - 1),
                    self.plan.total_timepoints,
                )
        self._tag(self.published)
        # Remove a GUI-only overlay of the unfinished timepoint. The viewer
        # switches back to the Session-owned completed scientific object.
        if status != "complete" and self._had_provisional:
            self.hooks.preview(self.published)
        self.hooks.finalized(self.published, getattr(self.published, "name", "result"))
        self.hooks.current_changed(self.published)
        return self.published

    def offline_final(self, result, reconstructor):
        """Publish a conventional finite result using the same job/scope tag.

        Used by one-shot and non-progressive compatibility fallbacks.
        Independent finite outputs instead use :meth:`append_completed`.
        """
        if self._finalized:
            raise RuntimeError("A result job cannot be published twice")
        if self.plan.expected_count is not None:
            self.coverage.mark_completed_count(self.plan.expected_count)
        self.coverage.finalize("complete")
        self._tag(result)
        self.hooks.offline_complete(result, getattr(result, "name", "result"), reconstructor)
        self.published = result
        self._finalized = True
        return result


__all__ = ["PublicationHooks", "ResultPublication"]
