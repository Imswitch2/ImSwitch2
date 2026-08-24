"""Where ROI sets live between sessions (P-6.3, A-25).

The widget-state store is the natural home, and for almost every session it is
the only one used. It is not free, though: saving state serialises the payload
**twice** — once through `json.dumps` purely to check it is serialisable, then
again to write it — so a five-thousand-ROI segmented set would make closing
ImProcess visibly slow, at the moment the user is least willing to wait.

So there is a cap. Under it, everything lives in the state store. Over it, the
sets spill to **one** file — never one per result, never one per set — and the
state store keeps only a marker and a checksum, so the two can never quietly
disagree: a missing or edited spill file is detected rather than half-loaded.

Pure: no Qt, no viewer. The controller owns the adapter that calls this.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from imswitch.imcommon.algorithms.roi_set import ROISet
from imswitch.imcommon.algorithms.roi_set_io import (
    ROISetFormatError,
    SCHEMA_VERSION,
    set_from_json,
    set_to_json,
)

#: The one extra file (C-13), beside `improcess_shortcuts.json`.
SPILL_FILENAME = "improcess_roi_sets.json"

#: Above either of these, the sets spill to their own file. A megabyte of
#: serialised JSON is roughly two thousand ROIs; both are checked because a
#: few enormous masks and a great many small rectangles are both slow, for
#: different reasons.
SPILL_CAP_BYTES = 1_000_000
SPILL_CAP_ROIS = 2000

#: Marks a state-store payload whose sets are in the spill file.
SPILLED_KEY = "spilled"


class ROIStateError(RuntimeError):
    """Saved ROI state could not be restored, with a reason worth showing."""


def spill_path(root: str | Path) -> Path:
    return Path(root) / SPILL_FILENAME


def _checksum(payload: dict) -> str:
    """A digest of exactly the bytes that will be written.

    `sort_keys` because a dict's iteration order is not part of its meaning,
    and a checksum that changed when a key moved would fail on every load.
    """
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sets_payload(sets, active_index: int, options: dict | None = None) -> dict:
    """Every set, as one JSON-ready object."""
    return {
        "schema_version": SCHEMA_VERSION,
        "active_index": int(active_index),
        "options": dict(options or {}),
        "sets": [set_to_json(roi_set) for roi_set in sets],
    }


def _drop_orphans(item: dict) -> tuple[dict, int]:
    """Remove ROIs pointing at a frame this payload does not contain.

    Restoring is deliberately more forgiving than opening a file. A *file* with
    a dangling frame reference is malformed and is refused whole (see
    `roi_set_io.validate`); a *saved session* that lost one frame should not
    cost the user the other hundred and ninety-nine ROIs at startup. The
    tolerance lives here, once, rather than being a `strict=False` flag that
    every caller has to remember to get right.
    """
    body = item.get("roi_set")
    if not isinstance(body, dict):
        return item, 0
    known = {
        frame.frame_uid
        for frame in (_frames_of(body))
    }
    rois = body.get("rois") or []
    kept = [
        roi for roi in rois
        if not roi.get("frame_uid") or roi.get("frame_uid") in known
    ]
    if len(kept) == len(rois):
        return item, 0
    cleaned = dict(item)
    cleaned["roi_set"] = {**body, "rois": kept}
    return cleaned, len(rois) - len(kept)


def _frames_of(body: dict):
    """The frames a serialised set contains, with their derived uids."""
    from imswitch.imcommon.algorithms.roi_set_io import _frame_from_json

    frames = []
    for payload in body.get("frames") or ():
        try:
            frames.append(_frame_from_json(payload))
        except Exception:
            continue
    return frames


def sets_from_payload(payload: dict) -> tuple[list[ROISet], int, dict, int]:
    """``(sets, active_index, options, dropped)``, or a refusal saying why."""
    if not isinstance(payload, dict):
        raise ROIStateError("the saved ROI state is not readable")
    sets, dropped = [], 0
    try:
        for item in payload.get("sets") or ():
            cleaned, lost = _drop_orphans(item)
            dropped += lost
            sets.append(set_from_json(cleaned))
    except ROISetFormatError as exc:
        raise ROIStateError(str(exc)) from exc
    if not sets:
        sets = [ROISet(name="ROIs")]
    active = int(payload.get("active_index", 0) or 0)
    active = max(0, min(active, len(sets) - 1))
    return sets, active, dict(payload.get("options") or {}), dropped


def should_spill(payload: dict) -> bool:
    """Whether this payload is too big for the state store.

    Measured on the **serialised** size rather than estimated from the record
    count: one segmentation mask can outweigh a thousand rectangles, and the
    cost being guarded against is the serialisation itself.
    """
    roi_count = sum(len(item.get("roi_set", {}).get("rois", ())) for item in payload.get("sets", ()))
    if roi_count > SPILL_CAP_ROIS:
        return True
    return len(json.dumps(payload)) > SPILL_CAP_BYTES


def spill_marker(payload: dict) -> dict:
    """What the state store keeps when the sets live in the spill file.

    A marker and a checksum, never a copy: two copies of the same sets in two
    places is how they come to disagree, and the disagreement would be
    invisible. The names ride along so a missing spill file can say *which*
    sets are gone rather than reporting a silent nothing.
    """
    return {
        "schema_version": payload.get("schema_version", SCHEMA_VERSION),
        SPILLED_KEY: True,
        "file": SPILL_FILENAME,
        "checksum": _checksum(payload),
        "set_count": len(payload.get("sets") or []),
        "names": [
            item.get("roi_set", {}).get("name", "")
            for item in payload.get("sets") or []
        ],
        "options": dict(payload.get("options") or {}),
    }


def unpack(state: dict, root: str | Path) -> tuple[list[ROISet], int, dict, int]:
    """Restore from a state-store payload, following a spill marker if present."""
    if not isinstance(state, dict):
        raise ROIStateError("the saved ROI state is not readable")
    if not state.get(SPILLED_KEY):
        return sets_from_payload(state)

    path = spill_path(root)
    if not path.exists():
        raise ROIStateError(
            f"the saved ROI sets are in {path.name}, which is missing "
            f"({', '.join(state.get('names') or []) or 'unknown sets'})"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ROIStateError(f"{path.name} could not be read: {exc}") from exc

    expected = state.get("checksum")
    if expected and _checksum(payload) != expected:
        raise ROIStateError(
            f"{path.name} does not match what this profile saved; it was "
            "edited or replaced. Refusing to load it rather than restoring "
            "ROIs that may belong to another session."
        )
    return sets_from_payload(payload)


def write_spill(payload: dict, root: str | Path) -> Path:
    """Write the spill file atomically, using the shared envelope's writer."""
    import os
    import tempfile

    path = spill_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return path


__all__ = [
    "ROIStateError",
    "SPILLED_KEY",
    "SPILL_CAP_BYTES",
    "SPILL_CAP_ROIS",
    "SPILL_FILENAME",
    "spill_marker",
    "sets_from_payload",
    "sets_payload",
    "should_spill",
    "spill_path",
    "unpack",
    "write_spill",
]
