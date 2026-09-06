"""How a result gets written: planned, staged, published, receipted.

A save used to be "call ``imwrite`` and hope". Two things were wrong with
that. A writer that produces several files (a TIFF and its drift vectors, a
Picasso HDF5 and its YAML) could crash between them and leave half; and
nothing said afterwards *which* files had been written, so a result could
not describe its own artifact in its provenance.

The protocol here is the same for every result type:

1. **plan** -- the result names every file it will produce
   (:meth:`ProcessingResult.plan_save`), before anything is written;
2. **preflight** -- none of them may exist unless overwriting was asked for;
3. **stage** -- the writer writes all of them into a staging directory
   beside the target, embedding the provenance *document* (graph plus the
   artifact record derived from the plan) wherever the container allows;
4. **publish** -- companions first, the primary last, each by an atomic
   rename, so a reader that sees the primary sees the companions too;
5. **receipt** -- what was actually published, kept on the result.

Any failure removes the staging directory and whatever this save had
already published; the target directory is left as it was found.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from imswitch.imcommon.algorithms.spatial_frame import mint_uid
from imswitch.improcess.model.footprint import HISTORY_KEY, history_of
from imswitch.improcess.model.provenance import PROVENANCE_KEY, graph_of, output_of

#: User-facing format names -> canonical ids.
FORMAT_ALIASES = {
    "tif": "tiff", "tiff": "tiff", "ome.tif": "tiff", "ome.tiff": "tiff", "ome-tiff": "tiff",
    "h5": "hdf5", "hdf": "hdf5", "hdf5": "hdf5",
    "zarr": "zarr", "ome.zarr": "zarr", "ome-zarr": "zarr",
    "csv": "csv", "txt": "csv",
    "json": "json",
    "picasso": "picasso", "napari-storm": "picasso",
    "imagej": "imagej", "imagej-tiff": "imagej",
}

#: Suffix a format gets when the caller's path has none that fits.
DEFAULT_SUFFIX = {
    "tiff": ".ome.tif", "hdf5": ".h5", "zarr": ".ome.zarr", "csv": ".csv",
    "json": ".json", "picasso": ".hdf5", "imagej": ".tif",
}

#: Companion carrying the document for containers with no metadata slot.
COMPANION_SUFFIX = ".provenance.json"


class SaveError(RuntimeError):
    """The save could not be completed; nothing was left half-written."""


class UnsupportedSaveFormat(ValueError):
    """The result cannot be written in the requested format."""


def normalize_format(fmt: str | None, path: Path | None = None) -> str:
    """Canonical format id for ``fmt`` (or for ``path``'s suffix)."""
    if fmt:
        key = str(fmt).lower().lstrip(".")
        if key in FORMAT_ALIASES:
            return FORMAT_ALIASES[key]
        raise UnsupportedSaveFormat(f"unknown save format {fmt!r}")
    if path is not None:
        name = Path(path).name.lower()
        for suffix, canonical in sorted(FORMAT_ALIASES.items(), key=lambda kv: -len(kv[0])):
            if name.endswith("." + suffix):
                return canonical
    raise UnsupportedSaveFormat("no format given and none recognisable from the path")


# --------------------------------------------------------------------------
# plan, document, receipt
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SavePlan:
    """Every file a save will produce, known before writing starts."""

    primary: Path
    fmt: str
    companions: tuple[Path, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "primary", Path(self.primary))
        object.__setattr__(self, "companions", tuple(Path(p) for p in self.companions))
        for companion in self.companions:
            if companion.parent != self.primary.parent:
                raise SaveError(
                    f"companion {companion} must live beside the primary {self.primary}"
                )

    @property
    def files(self) -> tuple[Path, ...]:
        return (self.primary, *self.companions)

    @property
    def directory(self) -> Path:
        return self.primary.parent

    def relative_names(self) -> list[str]:
        return [path.name for path in self.files]

    def staged(self, stage_dir: Path) -> "SavePlan":
        """The same plan with every file moved into ``stage_dir``."""
        stage_dir = Path(stage_dir)
        return SavePlan(
            stage_dir / self.primary.name, self.fmt,
            tuple(stage_dir / companion.name for companion in self.companions),
        )

    def companion(self, suffix: str) -> Path:
        """``primary`` with its suffix replaced, for naming companions."""
        return self.primary.with_name(self.primary.stem.split(".")[0] + suffix)


@dataclass(frozen=True)
class SaveReceipt:
    """What a save actually published."""

    primary: Path
    files: tuple[Path, ...]
    fmt: str
    node: str = ""
    port: str = ""


@dataclass
class ProvenanceDocument:
    """What a file carries: the graph and the artifact that is the file itself.

    ``graph`` is the computational provenance (may be ``None`` for a result
    that has none, or for a file written before the graph existed). ``artifact``
    describes this very file: which node/port it materialises, the format, and
    every file the save produced. ``history`` is the schema-0 linear list, kept
    so old files and old readers meet in the middle.
    """

    graph: dict | None = None
    artifact: dict | None = None
    history: list = field(default_factory=list)

    @property
    def schema(self) -> int:
        if isinstance(self.graph, dict):
            return int(self.graph.get("schema", 1) or 1)
        return 0

    @property
    def node(self) -> str:
        return str((self.artifact or {}).get("node") or "")

    @property
    def port(self) -> str:
        return str((self.artifact or {}).get("port") or "")

    def to_dict(self) -> dict:
        payload: dict[str, Any] = {"schema": self.schema}
        if self.graph is not None:
            payload["graph"] = self.graph
        if self.artifact is not None:
            payload["artifact"] = self.artifact
        if self.history:
            payload[HISTORY_KEY] = list(self.history)
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)

    @classmethod
    def from_dict(cls, payload: Any) -> "ProvenanceDocument":
        if not isinstance(payload, dict):
            return cls()
        graph = payload.get("graph")
        if graph is None and isinstance(payload.get("nodes"), dict):
            graph = payload                       # a bare graph
        history = payload.get(HISTORY_KEY) or []
        return cls(
            graph=graph if isinstance(graph, dict) else None,
            artifact=payload.get("artifact") if isinstance(payload.get("artifact"), dict) else None,
            history=list(history) if isinstance(history, list) else [],
        )

    @classmethod
    def from_json(cls, text: str) -> "ProvenanceDocument":
        try:
            return cls.from_dict(json.loads(text))
        except (TypeError, ValueError):
            return cls()


def artifact_record(plan: SavePlan, result) -> dict:
    """The ``artifact`` entry for a file written from ``plan``."""
    graph = graph_of(result)
    node, port = output_of(graph) if graph else ("", "")
    return {
        "node": node,
        "port": port,
        "fmt": plan.fmt,
        "primary": plan.primary.name,
        "files": plan.relative_names(),
        "result_uid": str(getattr(result, "result_uid", "") or ""),
    }


def document_for(result, plan: SavePlan) -> ProvenanceDocument:
    graph = graph_of(result)
    return ProvenanceDocument(
        graph=graph,
        artifact=artifact_record(plan, result),
        history=history_of(result),
    )


# --------------------------------------------------------------------------
# embedding helpers for writers
# --------------------------------------------------------------------------

def embed_hdf5(handle, document: ProvenanceDocument) -> None:
    """Root attribute on an open h5py file/group (JSON text).

    The linear ``processing_history`` attribute is left to the writer that
    already produces it, as JSON text, so existing readers keep working.
    """
    handle.attrs[PROVENANCE_KEY] = document.to_json()
    if HISTORY_KEY not in handle.attrs:
        handle.attrs[HISTORY_KEY] = json.dumps(document.history, ensure_ascii=False, default=str)


def embed_hdf5_path(path: Path, document: ProvenanceDocument) -> None:
    """Embed into an HDF5 file a writer has already closed."""
    import h5py

    with h5py.File(str(path), "a") as handle:
        embed_hdf5(handle, document)


def embed_zarr(root, document: ProvenanceDocument) -> None:
    attributes = dict(root.attrs)
    attributes[PROVENANCE_KEY] = document.to_dict()
    if HISTORY_KEY not in attributes:
        attributes[HISTORY_KEY] = json.dumps(document.history, ensure_ascii=False, default=str)
    root.attrs.update(attributes)


def description_payload(document: ProvenanceDocument, extra: dict | None = None) -> dict:
    """The JSON object a TIFF ``Description`` carries."""
    payload: dict[str, Any] = dict(extra or {})
    payload[PROVENANCE_KEY] = document.to_dict()
    if document.history:
        payload[HISTORY_KEY] = list(document.history)
    return payload


def strip_format_suffix(name: str) -> str:
    """``name`` without its recognised format suffix, and nothing else.

    ``sample.v1.csv`` -> ``sample.v1``: only the format suffix goes, so two
    files that differ before it keep distinct companions.
    """
    lowered = name.lower()
    for suffix in sorted(FORMAT_ALIASES, key=len, reverse=True):
        if lowered.endswith("." + suffix):
            return name[: -(len(suffix) + 1)]
    return Path(name).stem if "." in name else name


def companion_json_path(primary: Path) -> Path:
    primary = Path(primary)
    return primary.with_name(strip_format_suffix(primary.name) + COMPANION_SUFFIX)


def write_companion_json(primary: Path, document: ProvenanceDocument) -> Path:
    """The document as a listed sidecar, for containers with no metadata slot (CSV)."""
    path = companion_json_path(primary)
    path.write_text(document.to_json(), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# the protocol
# --------------------------------------------------------------------------

def save_result(result, path, fmt: str | None = None, *, overwrite: bool = False) -> SaveReceipt:
    """Write ``result`` to ``path`` through the staged protocol; returns the receipt."""
    path = Path(path)
    fmt = normalize_format(fmt, path)
    supported = tuple(normalize_format(f) for f in getattr(result, "supported_formats", ()))
    if supported and fmt not in supported:
        raise UnsupportedSaveFormat(
            f"{type(result).__name__} cannot be written as {fmt!r}; "
            f"it supports {', '.join(supported)}"
        )
    plan = result.plan_save(path, fmt)
    if not isinstance(plan, SavePlan):
        raise SaveError(f"{type(result).__name__}.plan_save did not return a SavePlan")

    # 2. preflight (the final no-clobber guarantee is the publish step's
    #    link-based rename; this check is for a readable error before work)
    if not overwrite:
        existing = [str(p) for p in plan.files if p.exists()]
        if existing:
            raise FileExistsError(f"refusing to overwrite: {', '.join(existing)}")
    plan.directory.mkdir(parents=True, exist_ok=True)

    # 3. stage
    token = mint_uid('save')[-8:]
    stage_dir = plan.directory / f".{plan.primary.name}.staging-{token}"
    stage_dir.mkdir()
    backup_dir = plan.directory / f".{plan.primary.name}.backup-{token}"
    staged = plan.staged(stage_dir)
    document = document_for(result, plan)
    published: list[Path] = []
    backups: list[tuple[Path, Path]] = []      # (original target, where it was moved)
    try:
        result.write_files(staged, document)
        missing = [p.name for p in staged.files if not p.exists()]
        if missing:
            raise SaveError(
                f"{type(result).__name__} planned {missing} but did not write them"
            )
        unplanned = sorted(p.name for p in stage_dir.iterdir() if p not in staged.files)
        if unplanned:
            raise SaveError(
                f"{type(result).__name__} wrote files it did not plan: {unplanned}"
            )
        # 4a. overwrite: move what exists aside first, so a failure below can
        #     put it back exactly as it was
        if overwrite:
            for target in plan.files:
                if target.exists() or target.is_symlink():
                    backup_dir.mkdir(exist_ok=True)
                    moved = backup_dir / target.name
                    os.replace(target, moved)
                    backups.append((target, moved))
        # 4b. publish: companions first, primary last
        for staged_file, target in zip(staged.files[1:], plan.files[1:]):
            _publish(staged_file, target)
            published.append(target)
        _publish(staged.primary, plan.primary)
        published.append(plan.primary)
    except Exception:
        for target in published:
            _remove(target)
        for target, moved in backups:
            try:
                os.replace(moved, target)
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)
        shutil.rmtree(backup_dir, ignore_errors=True)

    graph = graph_of(result)
    node, port = output_of(graph) if graph else ("", "")
    receipt = SaveReceipt(primary=plan.primary, files=plan.files, fmt=fmt, node=node, port=port)
    artifacts = getattr(result, "artifacts", None)
    if isinstance(artifacts, list):
        artifacts.append(receipt)
    return receipt


def _publish(staged: Path, target: Path) -> None:
    """Move a staged file to its name without ever replacing an existing one.

    A hard link fails atomically when the target exists, which closes the
    gap between the preflight check and the rename that a plain
    ``os.replace`` leaves open. Directories (Zarr groups) cannot be
    hard-linked; for those an existence check followed by a rename is the
    best POSIX offers, and the window is documented rather than hidden.
    """
    if staged.is_dir() and not staged.is_symlink():
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"refusing to overwrite: {target}")
        os.rename(staged, target)
        return
    try:
        os.link(staged, target)          # atomic no-clobber
    except FileExistsError:
        raise FileExistsError(f"refusing to overwrite: {target}") from None
    except OSError:
        # A filesystem without hard links: fall back to the guarded rename.
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"refusing to overwrite: {target}") from None
        os.rename(staged, target)
        return
    os.unlink(staged)


def _remove(target: Path) -> None:
    try:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists() or target.is_symlink():
            target.unlink()
    except OSError:
        pass


__all__ = [
    "COMPANION_SUFFIX",
    "DEFAULT_SUFFIX",
    "FORMAT_ALIASES",
    "ProvenanceDocument",
    "SaveError",
    "SavePlan",
    "SaveReceipt",
    "UnsupportedSaveFormat",
    "artifact_record",
    "companion_json_path",
    "description_payload",
    "document_for",
    "embed_hdf5",
    "embed_hdf5_path",
    "embed_zarr",
    "normalize_format",
    "save_result",
    "write_companion_json",
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
