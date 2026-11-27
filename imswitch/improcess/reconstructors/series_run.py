"""Running a reconstruction over a series (one source per timepoint).

:func:`~imswitch.improcess.reconstructors.run.run_reconstruction` calls
:func:`plan_series` first. It answers ``None`` -- and the run goes on exactly as
it always did -- unless the source is one timepoint of a series *and* the
request (or the reconstructor's ``series_default``) asks for more than the
opened timepoint.

The rule for composing with auto-indexing and ``Map``:

* A series is the **outermost** loop and exists only across *sources*. Each
  member is opened as a source and goes through the same single-source path as
  any other (``check_input``, auto-indexing over *its own* leading axes,
  ``process``), so nothing is iterated twice: a member whose recording has a
  ``(T, Z, Y, X)`` array is indexed over its T by auto-indexing, and the
  series stacks the members in front of that. The series axis is named ``T``;
  where a member's output already has a ``T`` of its own the usual stacking
  rule names the series axis ``T_loop`` instead.
* A source that is a *result* (the input of a chained step, the item a ``Map``
  binds) is never a series: there is no file to discover siblings of. So a
  ``Map`` body never expands a series, and a ``Map`` over the ``T`` axis of a
  series result iterates that result like any other array.

GUI-independent: no Qt.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from imswitch.improcess.model import iteration
from imswitch.improcess.model import series as series_model
from imswitch.improcess.model.series import (
    SeriesError,
    SeriesRequest,
    SeriesSelection,
    describe_selection,
    discover_series,
    open_member,
    select_members,
    stack_series,
)

ReconstructOne = Callable[[Any, Any], tuple[Any, dict | None]]


def plan_series(reconstructor, source, requested: dict | None, *, discover=discover_series) -> SeriesSelection | None:
    """The members a run covers, or ``None`` for an ordinary single-source run."""
    policy = getattr(reconstructor, "series_policy", "default")
    if policy == "none":
        return None
    if getattr(source, "provenance_parent", None) is not None:
        return None
    default = getattr(reconstructor, "series_default", "this")
    request = SeriesRequest.from_params(requested)
    if request is None and default != "all":
        return None
    request = request or SeriesRequest()
    if request.mode == "this":
        return None
    if request.mode == "auto" and default != "all":
        # Only the series' own manifest turns an unasked-for series on, and
        # that is decided without opening anything.
        path, _dataset, as_series = series_model._source_path_and_dataset(source)
        if not as_series:
            return None
    info = discover(source, deep=True)
    if info is None or len(info.members) < 2:
        return None
    mode = request.effective_mode(default, opened_as_series=info.opened_as_series)
    if mode == "this":
        return None
    accepted = tuple(getattr(reconstructor, "accepted_source_kinds", ("image",)))
    if info.member_kind not in accepted:
        raise SeriesError(
            f"The timepoints of {info.name} are {'tiling datasets' if info.member_kind == 'tiling-manifest' else info.member_kind + ' sources'}, "
            f"which {getattr(reconstructor, 'name', 'this reconstructor')} does not read "
            f"(it reads {', '.join(accepted)}). Choose a reconstructor that reads them, or this timepoint only."
        )
    return select_members(info, request, mode=mode)


def _member_context(context, position: int, total: int, base: float = 0.0):
    """A context whose progress is this member's share of the run after ``base``.

    ``base`` is the fraction already reported (a series that does its
    alignment first reports that on the outer context), so the bar never
    steps back when the timepoints start.
    """
    if context is None:
        return None
    from imswitch.improcess.reconstructors.base import ReconstructionContext

    def forward(update):
        callback = context.progress_callback
        if callback is not None:
            callback(replace(
                update,
                message=f"Timepoint {position + 1}/{total}: {update.message}",
                fraction=min(1.0, base + (1.0 - base) * (position + update.fraction) / total),
            ))

    return ReconstructionContext(
        progress_callback=forward,
        cancellation_token=context.cancellation_token,
        memory_budget_bytes=context.memory_budget_bytes,
        confirmed_over_budget=context.confirmed_over_budget,
    )


def report_timepoint(context, position: int, total: int, message: str, base: float = 0.0) -> None:
    """One progress update for finishing timepoint ``position`` (0-based) of ``total``."""
    if context is None or context.progress_callback is None:
        return
    from imswitch.improcess.reconstructors.base import ReconstructionProgress

    context.progress_callback(ReconstructionProgress(
        phase="assemble", completed=position + 1, total=total,
        message=message, fraction=min(1.0, base + (1.0 - base) * (position + 1) / total),
    ))


def run_default(reconstructor, selection: SeriesSelection, source, context, progress, cancel,
                reconstruct_one: ReconstructOne):
    """``process`` per timepoint, stacked along ``T``. Returns ``(result, extra)``."""
    from imswitch.improcess.workflows.sources import close_source

    info = selection.info
    total = len(selection.process)
    per_index: dict[int, Any] = {}
    held: list = []
    iterated = None
    what = getattr(reconstructor, "name", None) or getattr(reconstructor, "id", "reconstruction")
    anchor_path = str(getattr(source, "dataPath", "") or "")
    for number, position in enumerate(selection.process):
        iteration.check_cancelled(cancel, context)
        member = info.members[position]
        reuse = (
            position == info.anchor and str(member.path) == anchor_path
            and info.kind != series_model.KIND_TILING_TIMELAPSE
        )
        opened = source if reuse else open_member(info, member)
        try:
            result, extra = reconstruct_one(opened, _member_context(context, number, total))
        except Exception as exc:
            if isinstance(exc, (SeriesError,)) or type(exc).__name__ in ("ReconstructionCancelled", "OperationCancelled"):
                raise
            raise type(exc)(f"timepoint {member.index} of {info.name}: {exc}") from exc
        finally:
            pass
        if opened is not source:
            # A view result reads lazily from the member's file; read it before
            # the file is closed, or the stack would hold dead handles. Only
            # the member's own output is read, never the whole series at once.
            if _materialize(result):
                close_source(opened)
            else:
                held.append(opened)
        if number == 0:
            from imswitch.improcess.model.result import result_kind

            kind = result_kind(result)
            if kind not in ("image", "labels"):
                raise SeriesError(
                    f"{what} produces {kind!r} results, which the default series run cannot "
                    "stack along T. Process the timepoints one at a time, or give the "
                    "reconstructor series_policy = 'own' and a process_series."
                )
            iteration.guard_memory(result, total, str(what))
            iterated = (extra or {}).get("iterated_over")
        per_index[position] = result
        if progress is not None:
            progress(number + 1, total, f"T={number + 1}/{total}")
        report_timepoint(context, number, total, f"Timepoint {member.index} done")
    for opened in held:
        close_source(opened)
    iteration.check_cancelled(cancel, context)
    results = [per_index.get(position) for position in selection.positions]
    first = next(r for r in results if r is not None)
    name = f"{getattr(first, 'name', info.name)} (series of {total})"
    stack = getattr(reconstructor, "stack_results", None)
    stacked = stack_series(
        results, selection, name=name, stack_hook=stack if callable(stack) else None,
        extra_metadata={"member_iterated_over": iterated} if iterated else None,
    )
    return stacked, series_extra(selection, iterated)


def _materialize(result) -> bool:
    """Turn a lazy ``result.data`` into an array; ``False`` if that is not possible."""
    import numpy as np

    data = getattr(result, "data", None)
    if data is None or isinstance(data, np.ndarray):
        return True
    try:
        result.data = np.asarray(data)
        return True
    except Exception:  # noqa: BLE001 - the member simply stays open
        return False


def series_extra(selection: SeriesSelection, iterated: dict | None = None) -> dict:
    """The provenance node's extra keys for a run over ``selection``."""
    extra = {"series": describe_selection(selection)}
    if iterated:
        extra["series"]["member_iterated_over"] = iterated
    return extra


def run_own(reconstructor, selection: SeriesSelection, source, params, context, progress, cancel):
    """The reconstructor's own ``process_series``. Returns ``(result, extra)``."""
    if context is not None:
        result = reconstructor.process_series(
            selection, source, params, context, progress=progress, cancel=cancel)
    else:
        result = reconstructor.process_series(selection, source, params, progress=progress, cancel=cancel)
    return result, series_extra(selection)
