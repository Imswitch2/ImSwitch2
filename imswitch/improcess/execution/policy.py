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
    INCREMENTAL_FOLLOW = "incremental-follow"
    WAIT_FINITE = "wait-finite"


@dataclass(frozen=True)
class ExecutionChoice:
    path: ExecutionPath
    reason: str = ""


@dataclass(frozen=True)
class StreamingCapability:
    """Whether the *actual* streaming implementation honors these settings."""

    supported: bool
    reason: str = ""


def streaming_capability(reconstructor, params: dict) -> StreamingCapability:
    """Ask the plugin for an explicit reason; unknown plugins use legacy checks.

    This query is deliberately independent of source format. The queue also
    checks the committed-frame reader at admission/execution time.
    """
    if reconstructor is None or not getattr(reconstructor, "supports_streaming", False):
        return StreamingCapability(False, "This reconstructor has no streaming implementation.")
    probe = getattr(reconstructor, "streaming_capability", None)
    try:
        if callable(probe):
            capability = probe(dict(params or {}))
            if isinstance(capability, StreamingCapability):
                return capability
            if isinstance(capability, tuple) and len(capability) == 2:
                return StreamingCapability(bool(capability[0]), str(capability[1] or ""))
            raise TypeError("streaming_capability must return (supported, reason)")
        legacy_probe = getattr(reconstructor, "supports_streaming_params", None)
        if callable(legacy_probe) and not legacy_probe(dict(params or {})):
            return StreamingCapability(False, "Selected reconstruction settings do not support streaming.")
    except (TypeError, ValueError, KeyError) as exc:
        return StreamingCapability(False, f"Cannot validate streaming settings: {exc}")
    return StreamingCapability(True)


def supports_streaming_settings(reconstructor, params: dict) -> bool:
    """Backwards-compatible boolean form for existing callers."""
    return streaming_capability(reconstructor, params).supported


def choose_execution(job, source_kind: str, reconstructor, *, growing: bool = False,
                     live_reader: bool = False) -> ExecutionChoice:
    """Choose the supported engine; never infer live from AUTO or time-lapse."""
    requirement = ExecutionRequirement(job.execution_requirement)
    if growing:
        capability = (streaming_capability(reconstructor, job.params)
                      if requirement == ExecutionRequirement.FOLLOW else None)
        if capability is not None and capability.supported and live_reader:
            return ExecutionChoice(ExecutionPath.INCREMENTAL_FOLLOW)
        if capability is not None and not capability.supported:
            return ExecutionChoice(ExecutionPath.WAIT_FINITE,
                                   f"Waiting for completion — {capability.reason}")
        if requirement == ExecutionRequirement.FOLLOW and not live_reader:
            return ExecutionChoice(ExecutionPath.WAIT_FINITE,
                                   "Waiting for completion — no safe committed-frame reader")
        return ExecutionChoice(ExecutionPath.WAIT_FINITE,
                               "Waiting for acquisition to finish")
    # A finished acquisition never needs the frame-following executor merely
    # because the plugin supports streaming. Finite timepoint-independent jobs
    # use ReconstructionWorker and publish their completed outputs progressively.
    # In particular, MANUAL and completed AUTO jobs must not start a file watcher
    # or inherit a previous stream's state.
    return ExecutionChoice(ExecutionPath.OFFLINE_FINITE)
