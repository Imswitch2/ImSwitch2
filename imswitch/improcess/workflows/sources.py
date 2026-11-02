"""What a workflow reads, and how a headless run opens it.

The spec (where an input lives) is :mod:`imswitch.imcommon.model.workflow.sources`,
re-exported here; this module adds what needs ImProcess's readers: opening a
spec as a ``DataObj``, fingerprinting it, closing it. A container with
several datasets is refused rather than guessed.
"""

from __future__ import annotations

from pathlib import Path

from imswitch.imcommon.model.workflow.sources import (
    SOURCE_KINDS,
    SourceError,
    SourceSpec,
    file_sha256,
    fingerprint_mismatches,
    parse_binding,
    spec_mismatches,
)


def open_source(spec: SourceSpec, *, source_root=None):
    """A ``DataObj`` for ``spec``, opened (metadata read) but not loaded.

    A multi-dataset container without a dataset name is an error listing
    the datasets, not a guess. A tiling run (any file in it, or the
    manifest) becomes a manifest-backed source the tiling reconstructor
    understands.
    """
    from imswitch.improcess.model.DataObj import DataObj
    from imswitch.improcess.model.dataset_sources import (
        TILING_MANIFEST_SPEC,
        resolve_dataset_source,
    )

    spec = spec.resolved(source_root)
    if not spec.path:
        raise SourceError("source has no path; bind one with --bind or --input")
    path = Path(spec.path)
    if not path.exists():
        raise SourceError(f"source does not exist: {path}")
    try:
        resolved = resolve_dataset_source(str(path))
    except Exception as exc:
        raise SourceError(f"{path}: {exc}") from exc

    from imswitch.imcommon.model.measurement_run import (
        MeasurementRunFile,
        is_measurement_run_file,
    )
    from imswitch.improcess.model.dataset_sources import MEASUREMENT_RUN_SOURCE_KIND

    if is_measurement_run_file(resolved.path):
        run = MeasurementRunFile.load(resolved.path)
        data_obj = DataObj.fromMetadataSource(
            resolved.path.name, resolved.path, MEASUREMENT_RUN_SOURCE_KIND, run,
            originalPath=resolved.original_path,
        )
        data_obj.sourceFingerprint = run.run_id
        return data_obj

    if resolved.format_id == TILING_MANIFEST_SPEC.id or spec.source_kind == "tiling-manifest":
        from imswitch.imcommon.algorithms.tile_mosaic import inspect_dataset, manifest_fingerprint

        index, completeness = inspect_dataset(resolved.path)
        data_obj = DataObj.fromMetadataSource(
            resolved.path.parent.name, resolved.path, resolved.format_id, index,
            originalPath=resolved.original_path,
        )
        data_obj.sourceSummary = completeness
        data_obj.sourceFingerprint = manifest_fingerprint(resolved.path)
        return data_obj

    names = list(DataObj.getDatasetNames(str(resolved.path)) or [])
    dataset = spec.dataset
    if dataset is None:
        if len(names) == 1:
            dataset = names[0]
        elif not names:
            raise SourceError(f"{path} contains no datasets")
        else:
            raise SourceError(
                f"{path} contains {len(names)} datasets; name one with '::<dataset>' "
                f"(available: {', '.join(names)})"
            )
    elif names and dataset not in names:
        raise SourceError(f"{path} has no dataset {dataset!r} (available: {', '.join(names)})")
    data_obj = DataObj(path.name, dataset, path=str(resolved.path))
    data_obj.checkAndOpenData()
    return data_obj


def fingerprint_of(data_obj) -> dict:
    from imswitch.improcess.model.provenance import describe_source

    return dict(describe_source(data_obj).get("fingerprint") or {})


def close_source(data_obj) -> None:
    for name in ("checkAndUnloadData",):
        method = getattr(data_obj, name, None)
        if callable(method):
            try:
                method()
            except Exception:
                pass
    handle = getattr(data_obj, "_file", None)
    close = getattr(handle, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


__all__ = [
    "SOURCE_KINDS",
    "SourceError",
    "SourceSpec",
    "close_source",
    "file_sha256",
    "fingerprint_mismatches",
    "fingerprint_of",
    "open_source",
    "parse_binding",
    "spec_mismatches",
]


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
