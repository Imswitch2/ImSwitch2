"""Run one workflow over many inputs, and say what happened to each.

A batch is the ordinary reason to write a workflow at all: forty
recordings, the same reconstruction and the same three processing steps,
one output folder. One bad file must not stop the other thirty-nine, and
the summary must say which one it was and why.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from imswitch.improcess.workflows.runner import RunError, RunReport, run
from imswitch.improcess.workflows.sources import SourceSpec, parse_binding
from imswitch.improcess.workflows.steps import Workflow, WorkflowError


@dataclass
class BatchRow:
    index: int
    bindings: dict[str, str]
    ok: bool
    error: str = ""
    failed_step: str = ""
    files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {"index": self.index, "ok": self.ok, "error": self.error,
                                  "failed_step": self.failed_step}
        for key, value in self.bindings.items():
            record[f"source:{key}"] = value
        record["files"] = ";".join(self.files)
        record["warnings"] = ";".join(self.warnings)
        return record


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


def run_over(
    workflow: Workflow,
    bindings_list: Iterable[dict[str, SourceSpec | str]],
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
) -> BatchReport | tuple[BatchReport, list[RunReport]]:
    """Run ``workflow`` once per bindings dict; failures are rows, not exceptions.

    ``registry`` may be a registry instance or a zero-argument factory. Pass
    a factory (``bootstrap_registry`` itself, or ``functools.partial`` of it)
    to give every row fresh plugin instances, so a plugin that caches state
    cannot carry it from one input to the next. An instance is reused across
    rows, which is faster and fine for the built-ins, all of which are
    stateless.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    batch = BatchReport(workflow=workflow.name)
    reports: list[RunReport] = []
    for index, bindings in enumerate(bindings_list):
        shown = {key: str(getattr(value, "path", value)) for key, value in bindings.items()}
        try:
            row_registry = registry() if callable(registry) else registry
            report = run(
                workflow, registry=row_registry, bindings=bindings, out_dir=out_dir,
                overwrite=overwrite, source_root=source_root, mode=mode, allow_drift=allow_drift,
                verify_hash=verify_hash, hash_sources=hash_sources,
            )
            row = BatchRow(
                index=index, bindings=shown, ok=True,
                files=[str(f) for receipt in report.receipts for f in receipt.files],
                warnings=list(report.warnings),
            )
        except RunError as exc:
            report = getattr(exc, "report", None)
            row = BatchRow(
                index=index, bindings=shown, ok=False, error=str(exc),
                failed_step=getattr(report, "failed_step", "") or "",
                warnings=list(getattr(report, "warnings", []) or []),
            )
        except Exception as exc:  # noqa: BLE001 - one row's failure is a row
            report = None
            row = BatchRow(index=index, bindings=shown, ok=False, error=str(exc))
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


__all__ = ["BatchReport", "BatchRow", "bindings_for_inputs", "bindings_from_manifest", "run_over"]


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
