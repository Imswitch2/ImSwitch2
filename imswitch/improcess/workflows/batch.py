"""Run one workflow over many inputs, and say what happened to each.

A batch is the ordinary reason to write a workflow at all: forty
recordings, the same reconstruction and the same three processing steps,
one output folder. One bad file must not stop the other thirty-nine, and
the summary must say which one it was and why.

A schema-2 workflow's controls are set for the whole batch (``controls=``,
``--set``) and per row (a manifest column named after a control); the file's
value, then the run-wide one, then the row's. Run-wide values, and a column
setting a control that has no active target, are checked before any row
runs and refuse the batch; a bad cell fails its own row only.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from imswitch.improcess.workflows.prepare import ControlText, ControlValueError, format_value, prepare_run
from imswitch.improcess.workflows.runner import RunError, RunReport, is_result_binding, run
from imswitch.improcess.workflows.sources import SourceSpec, parse_binding
from imswitch.improcess.workflows.steps import Workflow, WorkflowError


@dataclass
class BatchRow:
    index: int
    bindings: dict[str, str]
    ok: bool
    error: str = ""
    failed_step: str = ""
    #: Files published by this row. For a failed row these are the outputs
    #: written *before* the failure (``partial`` is then True); they are
    #: left in place, not rolled back, and listed so nobody has to guess.
    files: list[str] = field(default_factory=list)
    partial: bool = False
    warnings: list[str] = field(default_factory=list)
    #: Schema 2: every control's value as text ("" for one with no active
    #: target), and the steps the run took out because they are bypassed.
    controls: dict[str, str] = field(default_factory=dict)
    bypassed: list[str] = field(default_factory=list)

    def as_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {"index": self.index, "ok": self.ok, "partial": self.partial,
                                  "error": self.error, "failed_step": self.failed_step}
        for key, value in self.bindings.items():
            record[f"source:{key}"] = value
        for name, value in self.controls.items():
            record[f"control:{name}"] = value
        if self.controls or self.bypassed:
            record["bypassed"] = ";".join(self.bypassed)
        record["files"] = ";".join(self.files)
        record["warnings"] = ";".join(self.warnings)
        return record


@dataclass
class ManifestRow:
    """One manifest line: its bindings and the raw text of its control
    columns, kept together (an empty cell is missing: the run-wide value, or
    the file's, applies)."""

    line: int
    bindings: dict[str, SourceSpec]
    values: dict[str, str] = field(default_factory=dict)
    #: The manifest's own warnings (columns that are neither a source nor a
    #: control), the same on every row of one manifest.
    warnings: list[str] = field(default_factory=list)


@dataclass
class BatchReport:
    workflow: str
    rows: list[BatchRow] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(row.ok for row in self.rows)

    @property
    def failures(self) -> list[BatchRow]:
        return [row for row in self.rows if not row.ok]

    def write_summary(self, path) -> Path:
        path = Path(path)
        records = [row.as_record() for row in self.rows]
        columns: list[str] = []
        for record in records:
            for key in record:
                if key not in columns:
                    columns.append(key)
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for record in records:
                writer.writerow(record)
        return path


def bindings_from_manifest(path, source_ids: Iterable[str]) -> list[dict[str, SourceSpec]]:
    """One bindings dict per row of a CSV whose columns are source ids.

    A column ``<id>`` holds ``path`` or ``path::dataset``; an optional
    ``<id>.dataset`` column names the dataset separately.
    """
    source_ids = list(source_ids)
    rows: list[dict[str, SourceSpec]] = []
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [sid for sid in source_ids if sid not in (reader.fieldnames or [])]
        if missing:
            raise WorkflowError(f"manifest {path} lacks column(s) for source(s) {missing}")
        for record in reader:
            bindings: dict[str, SourceSpec] = {}
            for sid in source_ids:
                spec = parse_binding(str(record.get(sid, "")).strip())
                dataset = str(record.get(f"{sid}.dataset", "") or "").strip()
                if dataset:
                    spec = spec.with_path(spec.path, dataset)
                bindings[sid] = spec
            rows.append(bindings)
    return rows


def rows_from_manifest(path, workflow: Workflow) -> list[ManifestRow]:
    """One :class:`ManifestRow` per line of a CSV with a column per source id
    (``path`` or ``path::dataset``, or an ``<id>.dataset`` column) and,
    optionally, a column per control.

    A cell is taken with surrounding spaces removed; to keep spaces, quote it
    (``' a '``). A column that is neither a source, ``<id>.dataset`` nor a
    control is ignored, and said in every row's warnings.
    """
    source_ids = [step.id for step in workflow.sources()]
    control_names = [control.name for control in workflow.controls]
    rows: list[ManifestRow] = []
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        columns = list(reader.fieldnames or [])
        missing = [sid for sid in source_ids if sid not in columns]
        if missing:
            raise WorkflowError(f"manifest {path} lacks column(s) for source(s) {missing}")
        known = set(source_ids) | {f"{sid}.dataset" for sid in source_ids} | set(control_names)
        warnings = [f"manifest column {column!r} is neither a source nor a control; ignored"
                    for column in columns if column not in known]
        for record in reader:
            bindings: dict[str, SourceSpec] = {}
            for sid in source_ids:
                spec = parse_binding(str(record.get(sid, "")).strip())
                dataset = str(record.get(f"{sid}.dataset", "") or "").strip()
                if dataset:
                    spec = spec.with_path(spec.path, dataset)
                bindings[sid] = spec
            values = {name: str(record.get(name) or "").strip() for name in control_names if name in columns}
            rows.append(ManifestRow(line=reader.line_num, bindings=bindings, values=values,
                                    warnings=list(warnings)))
    return rows


def bindings_for_inputs(workflow: Workflow, inputs: Iterable, *, source_id: str | None = None) -> list[dict[str, SourceSpec]]:
    """One bindings dict per input path, for a workflow with one source
    (or with ``source_id`` named)."""
    sources = workflow.sources()
    if source_id is None:
        if len(sources) != 1:
            raise WorkflowError(
                f"the workflow has {len(sources)} sources; name the one to bind with "
                "source_id, or use a manifest with one column per source"
            )
        source_id = sources[0].id
    return [{source_id: parse_binding(str(item))} for item in inputs]


def _check_batch(workflow: Workflow, rows: list, controls, registry) -> None:
    """Refuse the batch before any row runs: run-wide values that cannot be
    used, or a manifest column setting a control with no active target."""
    if not workflow.controls and not controls:
        return
    shapes = set()
    for item in rows:
        bindings = item.bindings if isinstance(item, ManifestRow) else item
        shapes.add(frozenset(sid for sid, value in bindings.items() if is_result_binding(value)))
    probe_registry = registry() if callable(registry) else registry
    for bound in shapes or {frozenset()}:
        prepared = prepare_run(workflow, registry=probe_registry, values=controls, bound=bound)
        for item in rows:
            if not isinstance(item, ManifestRow):
                continue
            for name, text in item.values.items():
                if text and name in prepared.inactive:
                    why = ", ".join(f"{target} ({reason})" for target, reason in prepared.inactive[name])
                    raise ControlValueError(
                        name, f"the manifest sets it (line {item.line}), but it has no active target, "
                              f"so a value would change nothing: {why}")


def _control_columns(workflow: Workflow, report: RunReport | None) -> dict[str, str]:
    values = getattr(report, "controls", None) or {}
    return {control.name: format_value(values[control.name]) if control.name in values else ""
            for control in workflow.controls}


def run_over(
    workflow: Workflow,
    bindings_list: Iterable[dict[str, SourceSpec | str | Any] | ManifestRow],
    *,
    registry,
    out_dir,
    overwrite: bool = False,
    source_root=None,
    mode: str = "run",
    allow_drift: bool = False,
    verify_hash: bool = False,
    hash_sources: bool = False,
    on_row=None,
    keep_reports: bool = False,
    controls: dict[str, Any] | None = None,
) -> BatchReport | tuple[BatchReport, list[RunReport]]:
    """Run ``workflow`` once per bindings dict (or :class:`ManifestRow`);
    failures are rows, not exceptions.

    ``registry`` may be a registry instance or a zero-argument factory. Pass
    a factory (``bootstrap_registry`` itself, or ``functools.partial`` of it)
    to give every row fresh plugin instances, so a plugin that caches state
    cannot carry it from one input to the next. An instance is reused across
    rows, which is faster and fine for the built-ins, all of which are
    stateless.

    ``controls`` are control values for every row (raw ``ControlText`` from
    a command line, or typed); a :class:`ManifestRow`'s filled cells override
    them for its row. Raises :class:`WorkflowError` (or
    :class:`ControlValueError`) before any row runs when the run-wide values
    cannot be used or a manifest column sets a control with no active target.
    """
    rows = list(bindings_list)
    _check_batch(workflow, rows, controls, registry)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    batch = BatchReport(workflow=workflow.name)
    reports: list[RunReport] = []
    for index, item in enumerate(rows):
        manifest_row = item if isinstance(item, ManifestRow) else None
        bindings = manifest_row.bindings if manifest_row else item
        row_values = {name: ControlText(text) for name, text in (manifest_row.values if manifest_row else {}).items()
                      if text}
        values = {**(controls or {}), **row_values}
        extra = list(manifest_row.warnings) if manifest_row else []
        shown = {key: str(getattr(value, "path", value)) for key, value in bindings.items()}
        try:
            row_registry = registry() if callable(registry) else registry
            report = run(
                workflow, registry=row_registry, bindings=bindings, out_dir=out_dir,
                overwrite=overwrite, source_root=source_root, mode=mode, allow_drift=allow_drift,
                verify_hash=verify_hash, hash_sources=hash_sources, controls=values or None,
            )
            row = BatchRow(
                index=index, bindings=shown, ok=True,
                files=[str(f) for receipt in report.receipts for f in receipt.files],
                warnings=list(report.warnings) + extra,
                controls=_control_columns(workflow, report), bypassed=list(report.bypassed),
            )
        except ControlValueError as exc:
            # A bad cell fails its row without running it; the error names
            # the line and the column.
            report = None
            where = f"line {manifest_row.line}, column {exc.control}: " if (
                manifest_row and exc.control in row_values) else ""
            row = BatchRow(index=index, bindings=shown, ok=False, error=f"{where}{exc.reason}" if where else str(exc),
                           warnings=extra, controls={name: manifest_row.values.get(name, "") if manifest_row else ""
                                                     for name in (c.name for c in workflow.controls)})
        except RunError as exc:
            report = getattr(exc, "report", None)
            # Files a Save step published before a later step failed are
            # real files in the output folder; the row must list them, or
            # the summary would say "nothing written" while they sit there.
            written = [str(f) for receipt in getattr(report, "receipts", []) or [] for f in receipt.files]
            row = BatchRow(
                index=index, bindings=shown, ok=False, error=str(exc),
                failed_step=getattr(report, "failed_step", "") or "",
                files=written, partial=bool(written),
                warnings=list(getattr(report, "warnings", []) or []) + extra,
                controls=_control_columns(workflow, report),
                bypassed=list(getattr(report, "bypassed", []) or []),
            )
        except Exception as exc:  # noqa: BLE001 - one row's failure is a row
            report = None
            row = BatchRow(index=index, bindings=shown, ok=False, error=str(exc), warnings=extra)
        if report is not None:
            if keep_reports:
                reports.append(report)
            else:
                report.close()
        batch.rows.append(row)
        if on_row is not None:
            on_row(row)
    if keep_reports:
        return batch, reports
    return batch


__all__ = ["BatchReport", "BatchRow", "ManifestRow", "bindings_for_inputs", "bindings_from_manifest",
           "rows_from_manifest", "run_over"]


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
