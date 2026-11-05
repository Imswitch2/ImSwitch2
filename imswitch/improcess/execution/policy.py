"""Capability-aware queue execution selection, independent of submission origin.

The reconstructor instance may implement ``supports_streaming_params(params)``
when only a subset of its processing settings can be run incrementally. This
matters for MoNaLISA Full/Robust corrections: the low-latency streaming session
cannot apply them, although the same plugin supports those settings offline.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from imswitch.improcess.model.processing_queue import ExecutionRequirement


class ExecutionPath(str, Enum):
    OFFLINE_FINITE = "offline-finite"
    INCREMENTAL_FINITE = "incremental-finite"
    INCREMENTAL_FOLLOW = "incremental-follow"
    WAIT_FINITE = "wait-finite"


@dataclass(frozen=True)
class ExecutionChoice:
    path: ExecutionPath
    reason: str = ""


def supports_streaming_settings(reconstructor, params: dict) -> bool:
    """A plugin's broad streaming flag is not proof of parameter compatibility."""
    if not bool(getattr(reconstructor, "supports_streaming", False)):
        return False
    probe = getattr(reconstructor, "supports_streaming_params", None)
    return bool(probe(dict(params or {}))) if callable(probe) else True


def choose_execution(job, source_kind: str, reconstructor, *, growing: bool = False,
                     live_reader: bool = False) -> ExecutionChoice:
    """Choose the supported engine; never infer live from AUTO or time-lapse."""
    requirement = ExecutionRequirement(job.execution_requirement)
    if growing:
        if (requirement == ExecutionRequirement.FOLLOW and live_reader
                and supports_streaming_settings(reconstructor, job.params)):
            return ExecutionChoice(ExecutionPath.INCREMENTAL_FOLLOW)
        return ExecutionChoice(ExecutionPath.WAIT_FINITE,
                               "Waiting for acquisition to finish")
    # A finished acquisition never needs the frame-following executor merely
    # because the plugin supports streaming. Finite timepoint-independent jobs
    # use ReconstructionWorker and publish their completed outputs progressively.
    # In particular, MANUAL and completed AUTO jobs must not start a file watcher
    # or inherit a previous stream's state.
    return ExecutionChoice(ExecutionPath.OFFLINE_FINITE)
