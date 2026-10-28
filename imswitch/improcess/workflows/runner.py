"""Run a workflow: the same reconstruction and processing paths the GUI uses.

Nothing here reimplements a step. A reconstruction goes through
:func:`~imswitch.improcess.reconstructors.run.run_reconstruction`, a
processing step through :func:`~imswitch.improcess.processors.run.run_processor`,
a save through :meth:`ProcessingResult.save`; so a result made by a
workflow carries the same provenance, on the same ports, with the same
receipts, as one made by clicking.

Two modes, named apart on purpose: ``run`` binds whatever sources the
caller gives it (apply this recipe to new data); ``replay`` verifies the
sources against the fingerprints recorded in the workflow and refuses
drift unless told to allow it.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from imswitch.imcommon.model import OperationCancelled
from imswitch.improcess.workflows.sources import (
    SourceError,
    SourceSpec,
    close_source,
    file_sha256,
    fingerprint_mismatches,
    fingerprint_of,
    open_source,
    spec_mismatches,
)
from imswitch.improcess.workflows.steps import (
    DEFAULT_PORT,
    SOURCE_PORT,
    Consolidate,
    Process,
    Reconstruct,
    Ref,
    Save,
    Source,
    Workflow,
    WorkflowError,
    validate,
)

MODES = ("run", "replay")

_SUFFIX = {"tiff": ".ome.tif", "hdf5": ".h5", "zarr": ".ome.zarr", "csv": ".csv",
           "picasso": ".hdf5", "imagej": ".tif", "json": ".json"}

#: The placeholders a ``Save`` step's ``path_template`` may use; what
#: :func:`render_save_path` fills in, and what an editor can offer.
PATH_PLACEHOLDERS = ("out_dir", "source_stem", "step", "input_step", "name", "ext", "fmt")


class RunError(RuntimeError):
    """A step failed; the report says which and why."""


@dataclass
class RunReport:
    """What a run produced. A context manager: ``close()`` releases the
    sources it opened and drops results that were neither saved nor
    materialised by the caller."""

    workflow: str
    mode: str = "run"
    out_dir: Path | None = None
    results: dict[str, Any] = field(default_factory=dict)      # "step.port" -> result
    receipts: list = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    steps_run: list[str] = field(default_factory=list)
    failed_step: str | None = None
    error: str | None = None
    #: ``"step.port"`` keys that were *bound* to results handed in by the
    #: caller rather than produced by this run (see ``run(bindings=...)``);
    #: a publisher must not add those to a list they already sit in.
    bound: list[str] = field(default_factory=list)
    #: Save steps not written because the run was asked not to
    #: (``write_saves=False``): step id -> the path it would have written.
    would_write: dict[str, Path] = field(default_factory=dict)
    #: What failed, as data: ``step``, ``type``, ``message``, and where the
    #: exception has them ``line``, ``traceback`` and ``stdout`` (a Python step's
    #: line in the user's code, its trimmed traceback, what it printed).
    failure: dict | None = None
    _sources: list = field(default_factory=list, repr=False)
    _closed: bool = field(default=False, repr=False)

    @property
    def ok(self) -> bool:
        return self.error is None

    def result(self, ref) -> Any:
        ref = ref if isinstance(ref, Ref) else _parse(ref)
        key = _resolve_key(self.results, ref)
        return self.results[key]

    def ports_of(self, step_id: str) -> list[str]:
        prefix = f"{step_id}."
        return [key[len(prefix):] for key in self.results if key.startswith(prefix)]

    def detach_sources(self) -> list:
        """Hand the opened sources to the caller instead of closing them.

        For a caller that keeps the results alive (the GUI publishes them
        into the reconstruction list): a lazily-backed result reads from its
        source handle, so closing the handle would break the result.
        """
        sources, self._sources = list(self._sources), []
        self._closed = True
        return sources

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for data_obj in self._sources:
            close_source(data_obj)
        self._sources = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _parse(text) -> Ref:
    from imswitch.improcess.workflows.steps import parse_ref

    return parse_ref(text)


def _resolve_key(results: dict, ref: Ref) -> str:
    if ref.port is not None:
        key = f"{ref.step}.{ref.port}"
        if key not in results:
            raise KeyError(f"{ref}: no such result (have {sorted(k for k in results if k.startswith(ref.step + '.'))})")
        return key
    keys = [k for k in results if k.startswith(f"{ref.step}.")]
    if not keys:
        raise KeyError(f"{ref.step}: produced no result")
    for preferred in (f"{ref.step}.{DEFAULT_PORT}", f"{ref.step}.{SOURCE_PORT}"):
        if preferred in keys:
            return preferred
    return keys[0]


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]+", "-", str(text)).strip("-") or "result"


#: Container suffixes a source file may carry, longest first.
_SOURCE_SUFFIXES = (
    ".ome.zarr", ".ome.tiff", ".ome.tif", ".zarr", ".tiff", ".tif",
    ".hdf5", ".hdf", ".h5", ".csv", ".json", ".npy", ".npz",
)


def source_stem_of(path) -> str:
    """The file name without its *container* suffix, dots inside kept.

    ``sample.v1.h5`` and ``sample.v2.h5`` must not both become ``sample``:
    only the recognised container suffix comes off (``sample.v1``), and an
    unknown suffix loses just its last extension.
    """
    name = Path(str(path)).name
    lower = name.lower()
    for suffix in _SOURCE_SUFFIXES:
        if lower.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    stem = Path(name).stem
    return stem or name


def render_save_path(step: Save, *, out_dir: Path, source_stem: str, result, input_step: str) -> Path:
    from imswitch.improcess.model.save_protocol import normalize_format

    fmt = normalize_format(step.fmt)
    values = {
        "out_dir": str(out_dir),
        "source_stem": _slug(source_stem),
        "step": _slug(step.id),
        "input_step": _slug(input_step),
        "name": _slug(getattr(result, "name", "") or step.id),
        "ext": _SUFFIX.get(fmt, "." + fmt),
        "fmt": fmt,
    }
    try:
        rendered = step.path_template.format(**values)
    except KeyError as exc:
        raise WorkflowError(f"{step.id}: unknown placeholder {exc} in path template") from exc
    out_dir = Path(out_dir).resolve()
    path = Path(rendered)
    if not path.is_absolute():
        path = out_dir / path
    path = path.resolve()
    # The output directory is the one place a workflow may write. A template
    # with `..` or an absolute path elsewhere is refused, not honoured.
    try:
        path.relative_to(out_dir)
    except ValueError:
        raise WorkflowError(
            f"{step.id}: save path {path} is outside the output directory {out_dir}"
        ) from None
    return path


def run(
    workflow: Workflow,
    *,
    registry,
    bindings: dict[str, SourceSpec | str | Any] | None = None,   # Any: an in-memory result
    out_dir=None,
    source_root=None,
    overwrite: bool = False,
    mode: str = "run",
    allow_drift: bool = False,
    verify_hash: bool = False,
    hash_sources: bool = False,
    progress: Callable[[int, int, str], None] | None = None,
    cancel: Callable[[], bool] | None = None,
    logger=None,
    on_step_done: Callable[[str, dict, float, bool], None] | None = None,
    write_saves: bool = True,
    source_stem: str | None = None,
) -> RunReport:
    """Execute ``workflow`` and return its :class:`RunReport`.

    ``bindings`` map source step ids to specs (or ``path[::dataset]``
    strings); a source step with a path of its own needs none. A binding
    may instead map a reconstruct, consolidate or process step id to an
    **in-memory result**: that step is not run, its ``out`` port *is* the
    result, and every step that only fed it (its source, typically) is
    skipped -- which is how the GUI applies a workflow's processing to
    results already in its list. Such keys are listed in ``report.bound``.
    Raises :class:`RunError` on the first failing step, with the report
    attached as ``exc.report``.

    A binding to a step with several ports is a ``{port: result}`` mapping,
    in the order the ports were produced (a reference without a port takes the
    first); a plain result binds the ``out`` port.

    ``hash_sources`` records a sha256 of every file source in the provenance
    (so a later replay can be verified byte-for-byte); ``verify_hash`` makes
    a replay require and check that hash. Accepting drift (``allow_drift``)
    turns a replay into a run, and the report's ``mode`` says so.

    ``on_step_done(step_id, {port: result}, seconds, cached)`` is called as
    each step finishes, and for each bound step with ``cached=True``; a step
    skipped because it only fed a bound one is not reported. An exception in
    the callback is logged, never a failure of the step. With
    ``write_saves=False`` no Save step writes: ``report.would_write`` holds the
    path each would have written. ``source_stem`` names the files of the run
    (``{source_stem}``); without it the stem comes from the first source the
    run opens, else from the first bound result's name. A failure is described
    as data in ``report.failure``.
    """
    if mode not in MODES:
        raise WorkflowError(f"unknown mode {mode!r}; expected one of {MODES}")
    issues = validate(workflow, registry)
    if issues:
        raise WorkflowError("workflow is invalid:\n  " + "\n  ".join(str(i) for i in issues))
    logger = logger or _log()
    out_dir = (Path(out_dir) if out_dir is not None else Path.cwd()).resolve()
    report = RunReport(workflow=workflow.name, mode=mode, out_dir=out_dir)
    bindings = dict(bindings or {})
    explicit_stem = bool(source_stem)
    source_stem = str(source_stem) if explicit_stem else "result"
    total = len(workflow.steps)
    # A binding may also be an in-memory result: "start from this". That
    # step is not run, its output port is the result, and any step that
    # only fed it (its source, say) is skipped too.
    bound_results = {sid: _bound_ports(value) for sid, value in bindings.items() if is_result_binding(value)}
    for sid in bound_results:
        try:
            target = workflow.step(sid)
        except KeyError:
            raise WorkflowError(f"binding names a step that does not exist: {sid!r}") from None
        if isinstance(target, Save):
            raise WorkflowError(
                f"{sid!r} cannot be bound to a result: only a reconstruct, consolidate or "
                "process step (or an input source) can start from an existing result"
            )
        if isinstance(target, Source) and any(
            isinstance(step, (Reconstruct, Consolidate)) and any(ref.step == sid for ref in step.inputs)
            for step in workflow.steps
        ):
            raise WorkflowError(
                f"{sid!r} cannot be bound to a result: only a reconstruct, consolidate or process "
                "step can start from an existing result (a source that feeds a reconstruction "
                "needs a recording; bind the reconstruction itself to the result)"
            )
    skipped = steps_replaced_by_bindings(workflow, set(bound_results))
    # Refuse an input nobody bound BEFORE any work: a workflow exported around
    # an interactive step (Fiji) has path-less sources, and finding the first
    # one only after a long reconstruction would waste the run.
    for step in workflow.steps:
        if isinstance(step, Source) and step.id not in skipped:
            wanted = bindings.get(step.id, step.source)
            if isinstance(wanted, str):
                from imswitch.improcess.workflows.sources import parse_binding

                wanted = parse_binding(wanted)
            if not wanted.bound:
                what = (
                    f"input {step.id!r} is not bound; bind a file or run it on a selected result"
                    if (step.source.metadata or {}).get("input") else
                    f"source {step.id!r} is not bound to a path"
                )
                report.failed_step = step.id
                report.error = f"{step.id}: {what}"
                error = RunError(report.error)
                error.report = report          # type: ignore[attr-defined]
                raise error

    from imswitch.improcess.processors.run import run_processor
    from imswitch.improcess.reconstructors.run import run_consolidation, run_reconstruction
    from imswitch.improcess.model.provenance import graph_of, output_of

    def report_done(step_id, keys_before, started, cached=False):
        if on_step_done is None:
            return
        produced = {key.split(".", 1)[1]: value for key, value in report.results.items()
                    if key not in keys_before and key.split(".", 1)[0] == step_id}
        try:
            on_step_done(step_id, produced, time.perf_counter() - started, cached)
        except Exception:
            logger.exception("on_step_done failed for step %s", step_id)

    for index, step in enumerate(workflow.steps):
        if progress is not None:
            progress(index, total, step.id)
        keys_before = set(report.results)
        started = time.perf_counter()
        reported = False
        try:
            if cancel is not None and cancel():
                # Inside the handler on purpose: a cancellation is reported
                # like any failure, with the report (and its open sources)
                # attached so the caller can close them.
                raise RunError("cancelled")
            if step.id in skipped:
                if step.id in bound_results:
                    ports = bound_results[step.id]
                    if isinstance(step, Source) and list(ports) == [DEFAULT_PORT]:
                        # A bound source stands for the recording: its port is the source's.
                        ports = {SOURCE_PORT: ports[DEFAULT_PORT]}
                    for port, result in ports.items():
                        report.results[f"{step.id}.{port}"] = result
                        report.bound.append(f"{step.id}.{port}")
                    first = next(iter(ports.values()))
                    if not report._sources and not explicit_stem:
                        source_stem = str(getattr(first, "name", "") or source_stem)
                    report_done(step.id, keys_before, started, cached=True)
                report.steps_run.append(step.id)
                continue
            if isinstance(step, Source):
                spec = bindings.get(step.id, step.source)
                if isinstance(spec, str):
                    from imswitch.improcess.workflows.sources import parse_binding

                    spec = parse_binding(spec)
                if not spec.bound:
                    raise SourceError(f"source {step.id!r} is not bound to a path")
                if mode == "replay":
                    # A binding may relocate the file; it may not point at a
                    # different dataset or kind of source and still be a replay.
                    drift = spec_mismatches(step.source, spec)
                    if drift:
                        message = f"source {step.id!r} is bound to something other than the recorded run: " + "; ".join(drift)
                        if not allow_drift:
                            raise SourceError(message + " (use allow_drift to run anyway; that is a run, not a replay)")
                        report.warnings.append(message)
                    if not spec.dataset and step.source.dataset:
                        spec = spec.with_path(spec.path, step.source.dataset)
                data_obj = open_source(spec, source_root=source_root)
                report._sources.append(data_obj)
                resolved_path = spec.resolved(source_root).path
                if hash_sources or (mode == "replay" and verify_hash):
                    digest = file_sha256(resolved_path)
                    if digest:
                        data_obj._provenance_sha256 = digest
                if mode == "replay":
                    recorded = dict(step.source.fingerprint or {})
                    if verify_hash and not recorded.get("sha256"):
                        raise SourceError(
                            f"source {step.id!r}: verify_hash asked, but the recorded run kept no hash "
                            "(record one with hash_sources=True / --hash-sources)"
                        )
                    problems = fingerprint_mismatches(recorded, fingerprint_of(data_obj)) if recorded else []
                    if not recorded:
                        problems.append("no fingerprint was recorded for this source")
                    if problems:
                        message = f"source {step.id!r} differs from the recorded run: " + "; ".join(problems)
                        if allow_drift:
                            report.warnings.append(message)
                        else:
                            raise SourceError(message + " (use allow_drift to run anyway; that is a run, not a replay)")
                    if report.warnings and allow_drift:
                        # Drift was accepted: whatever this produces, it did not
                        # reproduce the recorded run.
                        report.mode = "run"
                if len(report._sources) == 1 and not explicit_stem:
                    source_stem = source_stem_of(spec.path)
                report.results[f"{step.id}.{SOURCE_PORT}"] = data_obj

            elif isinstance(step, Reconstruct):
                plugin = registry.get_reconstructor(step.reconstructor)
                data_obj = report.result(step.inputs[0])
                params = {**type(plugin).default_params(), **step.params}
                params = plugin.prepare_params(data_obj, params)
                run_result = run_reconstruction(plugin, data_obj, params)
                report.results[f"{step.id}.{DEFAULT_PORT}"] = run_result.result

            elif isinstance(step, Consolidate):
                plugin = registry.get_reconstructor(step.reconstructor)
                inputs = [report.result(ref) for ref in step.inputs]
                merged = run_consolidation(plugin, inputs, step.params or None)
                report.results[f"{step.id}.{DEFAULT_PORT}"] = merged

            elif isinstance(step, Process):
                plugin = registry.get_processor(step.processor)
                inputs = [report.result(ref) for ref in step.inputs]
                params = {**type(plugin).default_params(), **step.params}
                if step.restriction:
                    # Narrow the run to the recorded regions, the way the GUI
                    # does: the restriction travels beside the params and is
                    # applied around the processor, never handed to it.
                    from imswitch.improcess.analysis.roi_restriction import ROI_PARAM, ROIRestriction

                    if not getattr(plugin, "accepts_roi", False):
                        raise WorkflowError(f"{step.processor!r} cannot be restricted to ROIs")
                    if step.restriction.get("by_reference"):
                        raise WorkflowError(
                            f"{step.id}: the ROI restriction was recorded by reference only "
                            "(its geometry was too large to store); it cannot be replayed"
                        )
                    params[ROI_PARAM] = ROIRestriction.from_provenance(step.restriction)
                for item in inputs:
                    if not plugin.accepts(item):
                        shape = tuple(getattr(getattr(item, "data", None), "shape", ()) or ())
                        raise WorkflowError(
                            f"{step.processor!r} does not accept result {getattr(item, 'name', item)!r} "
                            f"(kind {getattr(item, 'kind', 'image')!r}, axes "
                            f"{list(getattr(item, 'axis_labels', []) or [])}, shape {shape})"
                        )
                from imswitch.improcess.model.result import result_kind
                from imswitch.improcess.processors.base import call_output_spec

                spec = call_output_spec(plugin, params, tuple(result_kind(item) for item in inputs))
                fan_out = len(inputs) > 1 and getattr(plugin, "max_inputs", 1) == 1
                # A single-input processor over several inputs runs once per
                # input, and each run's ports are suffixed with *that input's*
                # index (signal, background, signal1, background1, ...).
                groups = [[item] for item in inputs] if fan_out else [inputs]
                for input_index, group in enumerate(groups):
                    results, failures = run_processor(plugin, group, params, logger)
                    if failures:
                        who, why = failures[0]
                        message = f"{step.processor!r} failed on {getattr(who, 'name', who)!r}: {why}"
                        cause = getattr(why, "exception", None)
                        if cause is not None:
                            raise RunError(message) from cause
                        raise RunError(message)
                    if not results:
                        raise RunError(f"{step.processor!r} produced no results")
                    for result in results:
                        graph = graph_of(result)
                        port = output_of(graph)[1] if graph else DEFAULT_PORT
                        if not spec.matches(port):
                            raise RunError(
                                f"{step.id}: {step.processor!r} produced port {port!r}, "
                                f"outside its declared {spec.describe()}"
                            )
                        if fan_out and input_index > 0:
                            port = f"{port}{input_index}"
                        key = f"{step.id}.{port}"
                        if key in report.results:
                            raise RunError(f"{step.id}: {step.processor!r} produced port {port!r} twice")
                        report.results[key] = result

            elif isinstance(step, Save):
                result = report.result(step.input)
                path = render_save_path(
                    step, out_dir=out_dir, source_stem=source_stem, result=result, input_step=step.input.step,
                )
                if not write_saves:
                    # Asked not to write: say what would have been, and do not
                    # count it as run.
                    report.would_write[step.id] = path
                    reported = True
                    continue
                receipt = result.save(path, step.fmt, overwrite=overwrite)
                report.receipts.append(receipt)
            else:  # pragma: no cover - the step types are closed
                raise WorkflowError(f"unknown step type {type(step).__name__}")
        except (Exception, OperationCancelled) as exc:
            # A cancellation raised *inside* a step (a processor that called
            # checkpoint(), or the worker interrupting one) is reported like the
            # between-steps one: a failure of this step, with the report attached.
            report.failed_step = step.id
            report.error = f"{step.id}: {'cancelled' if isinstance(exc, OperationCancelled) else exc}"
            report.failure = failure_details(step.id, exc)
            error = RunError(report.error)
            error.report = report          # type: ignore[attr-defined]
            raise error from exc
        report.steps_run.append(step.id)
        if not reported:
            try:
                report_done(step.id, keys_before, started)
            except OperationCancelled as exc:
                # An interruption meant for the step that just ended, landing
                # while it is reported: the run stops here, report attached,
                # so its open sources are closed rather than lost.
                report.error = f"{step.id}: cancelled"
                report.failure = failure_details(step.id, exc)
                error = RunError(report.error)
                error.report = report      # type: ignore[attr-defined]
                raise error from exc
    if progress is not None:
        progress(total, total, "done")
    return report


def _is_result(value) -> bool:
    return (
        not isinstance(value, (str, SourceSpec))
        and hasattr(value, "axis_labels")
        and hasattr(value, "data")
    )


def is_result_binding(value) -> bool:
    """Whether a binding value is an in-memory result (or a ``{port: result}``
    mapping of them) rather than a source spec."""
    if isinstance(value, Mapping):
        return bool(value) and all(isinstance(port, str) and _is_result(item) for port, item in value.items())
    return _is_result(value)


def _bound_ports(value) -> dict:
    """``{port: result}`` for a binding: a mapping as given (its order kept),
    a single result on the ``out`` port."""
    if isinstance(value, Mapping):
        return dict(value)
    return {DEFAULT_PORT: value}


def failure_details(step_id: str, exc: BaseException) -> dict:
    """A failure as data, for a caller that shows it rather than logs it.

    A processor's failure arrives wrapped in a :class:`RunError` whose cause is
    the processor's own exception; the details are read from that cause.
    """
    origin = exc.__cause__ if isinstance(exc, RunError) and exc.__cause__ is not None else exc
    if isinstance(origin, OperationCancelled):
        return {"step": step_id, "type": "OperationCancelled", "message": "cancelled"}
    details = {"step": step_id, "type": type(origin).__name__, "message": str(origin)}
    for attribute, key in (("line", "line"), ("traceback_text", "traceback"), ("stdout", "stdout")):
        value = getattr(origin, attribute, None)
        if value not in (None, ""):
            details[key] = value
    return details


def steps_replaced_by_bindings(workflow, bound_ids) -> set:
    """The bound steps plus every step that only exists to feed them.

    A source whose only consumer is a bound reconstruction is not opened;
    a step with any consumer that still runs is kept.
    """
    consumers: dict[str, set] = {step.id: set() for step in workflow.steps}
    for step in workflow.steps:
        for ref in getattr(step, "inputs", ()) or ():
            consumers.setdefault(ref.step, set()).add(step.id)
        ref = getattr(step, "input", None)
        if ref is not None:
            consumers.setdefault(ref.step, set()).add(step.id)
    skipped = set(bound_ids)
    for step in reversed(workflow.steps):
        if step.id in skipped:
            continue
        users = consumers.get(step.id, set())
        if users and users <= skipped and not isinstance(step, Save):
            skipped.add(step.id)
    return skipped


def _log():
    from imswitch.imcommon.model import initLogger

    return initLogger("ImProcessWorkflows", tryInheritParent=False)


__all__ = ["MODES", "RunError", "RunReport", "failure_details", "is_result_binding", "render_save_path", "run",
           "source_stem_of", "steps_replaced_by_bindings"]


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
