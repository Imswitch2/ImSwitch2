"""Planned acquisition references and metadata-only progress snapshots.

Expected physical locations are not proof of existence or completion.
The readiness layer remains responsible for safe file access and verification.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True)
class PhysicalReference:
    path: str
    dataset: str | None
    logical_t: int


@dataclass(frozen=True)
class AcquisitionProgress:
    planned_timepoints: int | None
    scope: tuple[int, int | None]
    expected_references: tuple[PhysicalReference, ...] = ()
    catalog_timepoints: tuple[int, ...] = ()
    completed_timepoints: tuple[int, ...] = ()
    status: str = "unknown"
    reason: str = ""

    @property
    def completed_count(self) -> int:
        return len(self.completed_timepoints)

    @property
    def catalog_count(self) -> int:
        return len(self.catalog_timepoints)


_GROUP = re.compile(r"scan(?P<number>\d+)(?P<suffix>/[^/]+(?:/.*)?)")


def expected_single_file_hdf5_refs(
    refs: list[PhysicalReference], total: int | None
) -> list[PhysicalReference]:
    """Predict shared-file scanN groups from a known physical/logical anchor.

    ImControl can append successive lapses into the same file, so physical
    scan numbering may start at scan4 even when logical time starts at T0.
    Only the identity of an already-discovered item is extrapolated.
    """
    refs = list(refs)
    if total is None or total <= 1 or not refs:
        return refs

    known = [r for r in refs if 0 <= r.logical_t < total]
    if not known:
        return refs

    positions = []
    for ref in known:
        path = Path(ref.path)
        match = _GROUP.fullmatch(str(ref.dataset or "").strip("/"))
        if path.suffix.lower() not in (".h5", ".hdf5") or match is None:
            return refs
        base = int(match.group("number")) - ref.logical_t
        positions.append((str(path), match.group("suffix"), base))

    anchor = positions[0]
    if anchor[2] < 0 or any(position != anchor for position in positions):
        return refs

    by_t = {ref.logical_t: ref for ref in known}
    if len(by_t) != len(known):
        return refs

    path, suffix, base = anchor
    return [
        by_t.get(t, PhysicalReference(path, f"scan{base + t}{suffix}", t))
        for t in range(total)
    ]
