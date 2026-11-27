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

from imswitch.improcess.model.series import without_series_params
from imswitch.improcess.reconstructors import series_run
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


class InputContractError(ValueError):
    """The input does not fit what the reconstructor declared (``check_input``)."""


def _reconstruct_one(reconstructor, source, requested: dict, context, progress, cancel):
    """One source through the plugin's contract, auto-indexing and ``process``.

    Returns ``(result, extra)``; the provenance node is recorded by the caller.
    """
    from imswitch.improcess.model import iteration
    from imswitch.improcess.model.core_dims import ITERATE_PARAM, CoreDimsError

    params = without_series_params(iteration.without_reserved(requested))
    check = getattr(reconstructor, "check_input", None)
    if callable(check):
        problems = check(source, requested)
        if problems:
            raise InputContractError("; ".join(str(problem) for problem in problems))
    match = None
    if getattr(reconstructor, "core_dims_in", None):
        handle = getattr(source, "data_handle", None)
        shape = tuple(getattr(handle, "shape", ()) or ())
        if shape:
            try:
                match = iteration.plan(reconstructor, getattr(source, "axis_labels", None) or [], shape, requested,
                                       labels_declared=bool(getattr(source, "axis_labels_declared", True)))
            except CoreDimsError as exc:
                raise InputContractError(f"{getattr(reconstructor, 'name', '')}: {exc}") from exc
    extra = None
    if match is not None and match.iterates:
        result = iteration.reconstruct_each(
            reconstructor, source, params, match, context=context, progress=progress, cancel=cancel,
        )
        extra = {"iterated_over": iteration.iterated_over(match)}
    else:
        if context is not None:
            result = reconstructor.process(source, params, context=context)
        else:
            result = reconstructor.process(source, params)
        if ITERATE_PARAM in requested and not iteration.wants_iteration(requested):
            # Not a parameter of the plugin, so not in ``params``; a replay
            # must still hand it the whole stack.
            extra = {"iterate": False}
    return result, extra


def run_reconstruction(reconstructor, source, params: dict | None, context=None, *,
                       progress=None, cancel=None) -> ReconstructionRun:
    """Call ``reconstructor.process`` and record the reconstruct node.

    ``context`` is passed only when given: reconstructors written before the
    context existed (and test stubs) still take ``(data_obj, params)``.

    ``source`` may be a result as well as a ``DataObj``: it is bridged
    (:mod:`~imswitch.improcess.model.result_source`). A reconstructor that
    declares ``core_dims_in`` is first asked whether it can take the input
    (``check_input``; :class:`InputContractError` with every reason before a
    pixel is processed) and, given more axes than its core dims, is
    **auto-indexed** over the extra leading ones
    (:mod:`~imswitch.improcess.model.iteration`): one call per index, the
    outputs stacked, and one provenance node recording ``iterated_over``.
    ``params["_iterate"] = False`` hands it the whole stack instead.
    ``progress(done, total, "T=3/120")`` and ``cancel() -> bool`` are asked
    once per index.
    """
    from imswitch.improcess.model import iteration
    from imswitch.improcess.model.result_source import as_reconstruction_input

    source = as_reconstruction_input(source)
    requested = dict(params or {})
    params = without_series_params(iteration.without_reserved(requested))
    # A source that is one timepoint of a series may be asked for as a whole
    # (see series_run). ``None`` -- not a series, or only this timepoint wanted --
    # leaves everything below exactly as it was.
    selection = series_run.plan_series(reconstructor, source, requested)
    if selection is not None:
        if getattr(reconstructor, "series_policy", "default") == "own":
            result, extra = series_run.run_own(reconstructor, selection, source, params, context, progress, cancel)
        else:
            member_requested = without_series_params(requested)

            def reconstruct_one(member, member_context):
                return _reconstruct_one(reconstructor, member, member_requested, member_context, None, cancel)

            result, extra = series_run.run_default(
                reconstructor, selection, source, context, progress, cancel, reconstruct_one)
    else:
        result, extra = _reconstruct_one(reconstructor, source, requested, context, progress, cancel)
    record_reconstruction(result, reconstructor, params, source, extra=extra)
    return ReconstructionRun(
        result=result,
        source=describe_source(source),
        params=params,
        plugin_id=str(getattr(reconstructor, "id", "") or type(reconstructor).__name__),
        node=output_ref(result),
    )


def run_consolidation(reconstructor, runs_or_results, params: dict | None = None):
    """Call ``reconstructor.consolidate`` over the results and record the node.

    ``consolidate`` takes no parameters, so none may be recorded either: a
    node that claimed settings the merge never saw would be a lie replay
    would faithfully repeat.
    """
    if params:
        raise ValueError(
            f"consolidation takes no parameters; got {sorted(params)}"
        )
    results = [
        item.result if isinstance(item, ReconstructionRun) else item
        for item in runs_or_results
    ]
    merged = reconstructor.consolidate(results)
    record_consolidation(merged, results, reconstructor, None)
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


__all__ = ["InputContractError", "ReconstructionRun", "record_snapshot", "run_consolidation", "run_reconstruction"]


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
