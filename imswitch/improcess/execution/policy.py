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
    streaming = supports_streaming_settings(reconstructor, job.params)
    requirement = ExecutionRequirement(job.execution_requirement)
    if growing:
        if requirement == ExecutionRequirement.FOLLOW and streaming and live_reader:
            return ExecutionChoice(ExecutionPath.INCREMENTAL_FOLLOW)
        return ExecutionChoice(ExecutionPath.WAIT_FINITE,
                               "Waiting for acquisition to finish")
    if source_kind == "time-lapse" and streaming and requirement != ExecutionRequirement.FOLLOW:
        return ExecutionChoice(ExecutionPath.INCREMENTAL_FINITE)
    if source_kind == "time-lapse" and streaming and requirement == ExecutionRequirement.FOLLOW:
        # A finished source is finite, irrespective of the user's live preference.
        return ExecutionChoice(ExecutionPath.INCREMENTAL_FINITE)
    return ExecutionChoice(ExecutionPath.OFFLINE_FINITE)
