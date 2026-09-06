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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from imswitch.improcess.workflows.sources import (
    SourceError,
    SourceSpec,
    close_source,
    fingerprint_mismatches,
    fingerprint_of,
    open_source,
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
    path = Path(rendered)
    if not path.is_absolute():
        path = out_dir / path
    return path


def run(
    workflow: Workflow,
    *,
    registry,
    bindings: dict[str, SourceSpec | str] | None = None,
    out_dir=None,
    source_root=None,
    overwrite: bool = False,
    mode: str = "run",
    allow_drift: bool = False,
    progress: Callable[[int, int, str], None] | None = None,
    cancel: Callable[[], bool] | None = None,
    logger=None,
) -> RunReport:
    """Execute ``workflow`` and return its :class:`RunReport`.

    ``bindings`` map source step ids to specs (or ``path[::dataset]``
    strings); a source step with a path of its own needs none. Raises
    :class:`RunError` on the first failing step, with the report attached
    as ``exc.report``.
    """
    if mode not in MODES:
        raise WorkflowError(f"unknown mode {mode!r}; expected one of {MODES}")
    issues = validate(workflow, registry)
    if issues:
        raise WorkflowError("workflow is invalid:\n  " + "\n  ".join(str(i) for i in issues))
    logger = logger or _log()
    out_dir = Path(out_dir) if out_dir is not None else Path.cwd()
    report = RunReport(workflow=workflow.name, mode=mode, out_dir=out_dir)
    bindings = dict(bindings or {})
    source_stem = "result"
    total = len(workflow.steps)

    from imswitch.improcess.processors.run import run_processor
    from imswitch.improcess.reconstructors.run import run_consolidation, run_reconstruction
    from imswitch.improcess.model.provenance import graph_of, output_of

    for index, step in enumerate(workflow.steps):
        if cancel is not None and cancel():
            report.error = "cancelled"
            report.failed_step = step.id
            raise RunError("cancelled", ) from None
        if progress is not None:
            progress(index, total, step.id)
        try:
            if isinstance(step, Source):
                spec = bindings.get(step.id, step.source)
                if isinstance(spec, str):
                    from imswitch.improcess.workflows.sources import parse_binding

                    spec = parse_binding(spec)
                if not spec.bound:
                    raise SourceError(f"source {step.id!r} is not bound to a path")
                data_obj = open_source(spec, source_root=source_root)
                report._sources.append(data_obj)
                if mode == "replay" and step.source.fingerprint:
                    problems = fingerprint_mismatches(step.source.fingerprint, fingerprint_of(data_obj))
                    if problems:
                        message = f"source {step.id!r} differs from the recorded run: " + "; ".join(problems)
                        if allow_drift:
                            report.warnings.append(message)
                        else:
                            raise SourceError(message + " (use allow_drift to run anyway; that is a run, not a replay)")
                if len(report._sources) == 1:
                    source_stem = Path(str(spec.path)).name.split(".")[0]
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
                for item in inputs:
                    if not plugin.accepts(item):
                        raise WorkflowError(
                            f"{step.processor!r} does not accept result {getattr(item, 'name', item)!r} "
                            f"(kind {getattr(item, 'kind', 'image')!r})"
                        )
                results, failures = run_processor(plugin, inputs, params, logger)
                if failures:
                    who, why = failures[0]
                    raise RunError(f"{step.processor!r} failed on {getattr(who, 'name', who)!r}: {why}")
                if not results:
                    raise RunError(f"{step.processor!r} produced no results")
                spec = plugin.output_spec(params)
                fan_out = len(inputs) > 1 and getattr(plugin, "max_inputs", 1) == 1
                used: set[str] = set()
                for position, result in enumerate(results):
                    graph = graph_of(result)
                    port = output_of(graph)[1] if graph else DEFAULT_PORT
                    if not spec.matches(port):
                        raise RunError(
                            f"{step.id}: {step.processor!r} produced port {port!r}, "
                            f"outside its declared {spec.describe()}"
                        )
                    if fan_out and port in used:
                        # One run per input: the same port from the second
                        # input onwards is suffixed with its input position.
                        port = f"{port}{position}"
                    used.add(port)
                    report.results[f"{step.id}.{port}"] = result

            elif isinstance(step, Save):
                result = report.result(step.input)
                path = render_save_path(
                    step, out_dir=out_dir, source_stem=source_stem, result=result, input_step=step.input.step,
                )
                receipt = result.save(path, step.fmt, overwrite=overwrite)
                report.receipts.append(receipt)
            else:  # pragma: no cover - the step types are closed
                raise WorkflowError(f"unknown step type {type(step).__name__}")
        except Exception as exc:
            report.failed_step = step.id
            report.error = f"{step.id}: {exc}"
            error = RunError(report.error)
            error.report = report          # type: ignore[attr-defined]
            raise error from exc
        report.steps_run.append(step.id)
    if progress is not None:
        progress(total, total, "done")
    return report


def _log():
    from imswitch.imcommon.model import initLogger

    return initLogger("ImProcessWorkflows", tryInheritParent=False)


__all__ = ["MODES", "RunError", "RunReport", "render_save_path", "run"]


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
