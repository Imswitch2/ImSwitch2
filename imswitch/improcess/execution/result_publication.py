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

from .result_assembly import ResultAssemblyPlan, ResultCoverage
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
        self._tag(result)
        self.published = result
        self.hooks.preview(result)

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
        self._finalized = True
        if self.published is None:
            return None
        self._tag(self.published)
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
