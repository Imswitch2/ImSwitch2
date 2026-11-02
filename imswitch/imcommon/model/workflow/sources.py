"""Where a workflow's input lives: the spec, without any way of opening it.

A source is a path plus the dataset inside it, the kind of container, and
(after a run, or in a replayed workflow) the fingerprint the data had. The
GUI resolves these through file dialogs and dataset pickers; a headless run
resolves them from the spec. Opening, fingerprinting and closing a source
is the host's business (:mod:`imswitch.improcess.workflows.sources`, which
re-exports everything here); this module is the pure part the schema and
the editor need.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SOURCE_KINDS = ("auto", "image", "tiling-manifest")


class SourceError(ValueError):
    """The source cannot be opened as specified."""


@dataclass(frozen=True)
class SourceSpec:
    """Where a workflow's input lives."""

    path: str | None = None
    dataset: str | None = None
    source_kind: str = "auto"
    metadata: dict = field(default_factory=dict)
    fingerprint: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.source_kind not in SOURCE_KINDS:
            raise SourceError(f"unknown source kind {self.source_kind!r}; expected one of {SOURCE_KINDS}")

    @property
    def bound(self) -> bool:
        return bool(self.path)

    def with_path(self, path, dataset: str | None = None) -> "SourceSpec":
        return SourceSpec(
            path=str(path), dataset=dataset if dataset is not None else self.dataset,
            source_kind=self.source_kind, metadata=dict(self.metadata),
            fingerprint=dict(self.fingerprint),
        )

    def resolved(self, root: Path | str | None) -> "SourceSpec":
        """Relative paths resolved against ``root`` (a workflow file's folder,
        or ``--source-root``)."""
        if not self.path or root is None:
            return self
        path = Path(self.path)
        if path.is_absolute():
            return self
        return self.with_path(Path(root) / path)

    def to_dict(self) -> dict:
        payload: dict[str, Any] = {}
        if self.path is not None:
            payload["path"] = str(self.path)
        if self.dataset is not None:
            payload["dataset"] = str(self.dataset)
        if self.source_kind != "auto":
            payload["source_kind"] = self.source_kind
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        if self.fingerprint:
            payload["fingerprint"] = dict(self.fingerprint)
        return payload

    @classmethod
    def from_dict(cls, payload: Any) -> "SourceSpec":
        if payload is None:
            return cls()
        if isinstance(payload, str):
            return parse_binding(payload)
        if not isinstance(payload, dict):
            raise SourceError(f"source must be a path string or an object, got {type(payload).__name__}")
        return cls(
            path=payload.get("path"),
            dataset=payload.get("dataset"),
            source_kind=str(payload.get("source_kind") or payload.get("kind") or "auto"),
            metadata=dict(payload.get("metadata") or {}),
            fingerprint=dict(payload.get("fingerprint") or {}),
        )


def parse_binding(text: str) -> SourceSpec:
    """``path`` or ``path::dataset`` as a spec (the ``--bind`` syntax)."""
    text = str(text)
    if "::" in text:
        path, dataset = text.rsplit("::", 1)
        return SourceSpec(path=path, dataset=dataset or None)
    return SourceSpec(path=text)


def fingerprint_mismatches(expected: dict, actual: dict, *, strict_mtime: bool = True) -> list[str]:
    """Human-readable differences between a recorded and a current fingerprint.

    Every field the record has must be present now and equal; a field that
    was recorded and cannot be determined today is a mismatch, not a pass.
    ``sha256`` is compared only when both sides have it (it is opt-in).
    """
    problems = []
    for key in ("shape", "dtype", "size", "attrs_digest", "manifest"):
        if key not in expected or expected[key] is None:
            continue
        if actual.get(key) is None:
            problems.append(f"{key}: recorded {expected[key]!r}, cannot be determined now")
        elif expected[key] != actual[key]:
            problems.append(f"{key}: recorded {expected[key]!r}, found {actual[key]!r}")
    if strict_mtime and expected.get("mtime") and actual.get("mtime"):
        if abs(float(expected["mtime"]) - float(actual["mtime"])) > 1.0:
            problems.append("mtime: the file was modified since the recorded run")
    if expected.get("sha256") and actual.get("sha256") and expected["sha256"] != actual["sha256"]:
        problems.append("sha256: the file's content differs from the recorded run")
    return problems


def spec_mismatches(recorded: SourceSpec, bound: SourceSpec) -> list[str]:
    """Where a binding disagrees with the recorded source, beyond its path.

    Relocating a file is allowed (that is what ``--source-root`` and
    ``--bind`` are for); pointing at a different dataset or kind of source
    is not a replay of the recorded run.
    """
    problems = []
    if recorded.dataset and bound.dataset and recorded.dataset != bound.dataset:
        problems.append(f"dataset: recorded {recorded.dataset!r}, bound {bound.dataset!r}")
    if recorded.source_kind != "auto" and bound.source_kind != "auto" and recorded.source_kind != bound.source_kind:
        problems.append(f"source kind: recorded {recorded.source_kind!r}, bound {bound.source_kind!r}")
    return problems


def file_sha256(path) -> str | None:
    """Content hash of a file (``None`` for a directory such as a Zarr group)."""
    import hashlib

    path = Path(path)
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = [
    "SOURCE_KINDS",
    "SourceError",
    "SourceSpec",
    "file_sha256",
    "fingerprint_mismatches",
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
