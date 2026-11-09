"""Physical source adaptation for committed-frame Session streams.

This module contains no queue ownership, readiness heuristics or UI policy.
"""

from __future__ import annotations

from imswitch.improcess.execution.disk_sources import make_incremental_disk_source
from imswitch.improcess.live.source_type import LAYOUT_SINGLE_LAPSE_FILE
from imswitch.improcess.model.timepoint_scope import coerce_timepoint_scope


def detector_from_logical_source(source) -> str | None:
    """Respect the Session/Loaded-data detector, never select a sibling by name.

    SourceRef.dataset can be 'CAM', 'scan0/CAM' or '/scan0/CAM/data'.
    For a Loaded lapse, the TimeLapseIndex's anchor_detector is definitive.
    """
    index = getattr(source, "sourceMetadata", None)
    if index is not None and getattr(index, "anchor_detector", None):
        return str(index.anchor_detector)
    refs = getattr(source, "source_refs", ()) or ()
    dataset = (getattr(refs[0], "dataset", None) if refs else None) or getattr(
        source, "primary_dataset", None
    )
    if dataset in (None, "", "time-lapse"):
        return None
    parts = [part for part in str(dataset).replace("\\", "/").strip("/").split("/") if part]
    if parts and parts[-1] == "data":
        parts.pop()
    if parts and parts[0].startswith("scan") and parts[0][4:].isdigit():
        parts.pop(0)
    return parts[0] if len(parts) == 1 else None


def make_finite_source(prepared, logical_source, scope, total, *, origin=0):
    """Adapt a probed logical lapse to the physical reader, scoped once.

    Readers use local zero-based T. Only one-file scanN layouts have physical
    scan indices that can start after zero for a later lapse in the same file.
    """
    start, stop = coerce_timepoint_scope(scope).bounds(total)
    if prepared.source_type.layout == LAYOUT_SINGLE_LAPSE_FILE:
        start += max(0, int(origin or 0))
        if stop is not None:
            stop += max(0, int(origin or 0))
    return make_incremental_disk_source(
        prepared.seed_path, source_type=prepared.source_type,
        detector_name=detector_from_logical_source(logical_source),
        timepoint_start=start, timepoint_stop=stop,
    )
