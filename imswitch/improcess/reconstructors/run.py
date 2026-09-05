"""The one way a reconstruction is run, so every result knows where it came from.

Eight code paths produce reconstructions (the inline button, the worker
thread, the live batch fallback, RAM recordings, streaming sessions, legacy
MoNaLISA, consolidation, and the headless runner). Each used to call
``process()`` and hand the result on; none wrote down the source or the
parameters. This module wraps the call and records the provenance node in
the same breath, so a path that goes through here cannot produce a result
without one.

GUI-independent: no Qt here. The callers that live in Qt controllers call
these from whichever thread ran the reconstruction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from imswitch.improcess.model.provenance import (
    describe_source,
    output_ref,
    record_consolidation,
    record_reconstruction,
    record_streaming,
)


@dataclass(frozen=True)
class ReconstructionRun:
    """A reconstruction together with what made it."""

    result: Any
    source: dict = field(default_factory=dict)      # describe_source(...)
    params: dict = field(default_factory=dict)
    plugin_id: str = ""
    node: dict = field(default_factory=dict)         # {"node", "port"} of the result


def run_reconstruction(reconstructor, source, params: dict | None, context=None) -> ReconstructionRun:
    """Call ``reconstructor.process`` and record the reconstruct node.

    ``context`` is passed only when given: reconstructors written before the
    context existed (and test stubs) still take ``(data_obj, params)``.
    """
    params = dict(params or {})
    if context is not None:
        result = reconstructor.process(source, params, context=context)
    else:
        result = reconstructor.process(source, params)
    record_reconstruction(result, reconstructor, params, source)
    return ReconstructionRun(
        result=result,
        source=describe_source(source),
        params=params,
        plugin_id=str(getattr(reconstructor, "id", "") or type(reconstructor).__name__),
        node=output_ref(result),
    )


def run_consolidation(reconstructor, runs_or_results, params: dict | None = None):
    """Call ``reconstructor.consolidate`` over the results and record the node."""
    results = [
        item.result if isinstance(item, ReconstructionRun) else item
        for item in runs_or_results
    ]
    merged = reconstructor.consolidate(results)
    record_consolidation(merged, results, reconstructor, params)
    return merged


def record_snapshot(result, reconstructor, params, source, *, session, status: str,
                    frames_committed=None, expected_frames=None):
    """Record a streaming snapshot (see :func:`~.provenance.record_streaming`)."""
    return record_streaming(
        result, reconstructor, dict(params or {}), source,
        session=session,
        completion={
            "status": status,
            "frames_committed": frames_committed,
            "expected_frames": expected_frames,
        },
    )


__all__ = ["ReconstructionRun", "record_snapshot", "run_consolidation", "run_reconstruction"]


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
