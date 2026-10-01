"""Reading and writing ROI sets, with a version on the front (P-6.2, A-24).

A saved ROI set outlives the code that wrote it. Two rules follow, and both are
enforced here rather than left to callers:

* **The version is refused, never guessed.** A file from a future schema is not
  "mostly readable" — loading it half-correctly produces ROIs at plausible-
  looking wrong coordinates, which is worse than an error. Unknown major
  versions raise.
* **A write either happens or it does not.** Serialising straight onto the
  destination turns a full disk or a crash into a truncated file where a valid
  one used to be. Written to a temporary file in the same directory, then
  renamed, which is atomic on every filesystem ImSwitch runs on.

The envelope also records the *coordinate convention*, because pixel indices
alone do not say whether ``(0, 0)`` is a corner or a centre, or whether bounds
are half-open. Two programs that disagree about that produce a half-pixel
offset that nothing in the data reveals.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from .roi import ROIRecord
from .roi_set import MeasurementConfig, ROISet
from .roi_style import ROIStyle
from .spatial_frame import AxisDescriptor, SpatialFrame

#: What this file is. Checked on load: a JSON file that happens to have the
#: right keys is not necessarily one of ours.
FORMAT = "imswitch-roi-set"

#: Bumped when the schema changes in a way an older reader cannot handle.
#: Readers accept everything up to and including this.
SCHEMA_VERSION = 1

#: The conventions the coordinates in this file follow (A-18). Recorded rather
#: than assumed, so a future change is visible in the data instead of being a
#: silent half-pixel shift.
COORDINATE_CONVENTION = "row-col, pixel centres at integers, bounds half-open"


class ROISetFormatError(ValueError):
    """A file could not be read as an ROI set, with a reason worth showing."""


def _frame_to_json(frame: SpatialFrame) -> dict:
    data = asdict(frame)
    data["axes"] = [asdict(axis) for axis in frame.axes]
    return data


def _frame_from_json(payload: dict) -> SpatialFrame:
    axes = tuple(
        AxisDescriptor(
            label=str(axis.get("label", "")),
            size=int(axis.get("size", 0)),
            scale=float(axis.get("scale", 1.0)),
            unit=str(axis.get("unit", "px")),
        )
        for axis in payload.get("axes", ())
    )
    plane_axes = tuple(payload.get("plane_axes", ("Y", "X")))
    shape = tuple(int(v) for v in payload.get("shape", (0, 0)))
    return SpatialFrame(
        coordinate_space_uid=str(payload.get("coordinate_space_uid", "")),
        result_uid=str(payload.get("result_uid", "")),
        dataset_uid=str(payload.get("dataset_uid", "")),
        plane_axes=(str(plane_axes[0]), str(plane_axes[1])),
        axes=axes,
        shape=(shape[0], shape[1]),
        affine=tuple(float(v) for v in payload.get("affine", ())) or None,  # type: ignore[arg-type]
        unit=str(payload.get("unit", "px")),
        component=payload.get("component"),
        view_mode=payload.get("view_mode"),
        lineage=tuple(str(v) for v in (payload.get("lineage") or ())),
        identity_kind=str(payload.get("identity_kind", "minted")),  # type: ignore[arg-type]
        # Deliberately *not* read from the file: frame_uid is derived from the
        # frame's own content (A-27), so recomputing it on load is what makes a
        # saved set match the same plane in a later session. A stored uid that
        # disagreed with the content would be the more trustworthy-looking of
        # two answers and the wrong one.
    )


def set_to_json(roi_set: ROISet) -> dict:
    """The full envelope for one set."""
    from imswitch import __version__ as imswitch_version

    return {
        "format": FORMAT,
        "schema_version": SCHEMA_VERSION,
        "coordinate_convention": COORDINATE_CONVENTION,
        "created_with": f"ImSwitch {imswitch_version}",
        "roi_set": {
            "uid": roi_set.uid,
            "name": roi_set.name,
            "rois": [roi.to_dict() for roi in roi_set.rois],
            "frames": [_frame_to_json(frame) for frame in roi_set.frames],
            "default_style": (
                roi_set.default_style.to_json() if roi_set.default_style else None
            ),
            "measurement_config": roi_set.measurement_config.to_json(),
            "revision": roi_set.revision,
            "dataset_uid": roi_set.dataset_uid,
        },
    }


def set_from_json(payload: dict) -> ROISet:
    """One set from its envelope, or a refusal naming what is wrong."""
    if not isinstance(payload, dict):
        raise ROISetFormatError("this is not an ROI set file")

    fmt = payload.get("format")
    if fmt != FORMAT:
        raise ROISetFormatError(
            f"this is not an ImSwitch ROI set (format is {fmt!r})"
        )

    try:
        version = int(payload.get("schema_version", 0))
    except (TypeError, ValueError):
        raise ROISetFormatError("the schema version is not a number") from None
    if version < 1:
        raise ROISetFormatError(f"schema version {version} is not a valid version")
    if version > SCHEMA_VERSION:
        raise ROISetFormatError(
            f"this file was written by a newer ImSwitch (schema version "
            f"{version}; this one reads up to {SCHEMA_VERSION}). Guessing at "
            "the parts it does not understand would put ROIs at plausible "
            "wrong coordinates, so it is refused instead."
        )
    payload = _migrate(payload, version)

    body = payload.get("roi_set")
    if not isinstance(body, dict):
        raise ROISetFormatError("the file has no ROI set in it")

    rois = []
    for index, item in enumerate(body.get("rois") or ()):
        if not isinstance(item, dict):
            raise ROISetFormatError(f"ROI {index} is not a record")
        try:
            rois.append(ROIRecord.from_dict(item))
        except Exception as exc:
            raise ROISetFormatError(f"ROI {index} could not be read: {exc}") from exc

    frames = []
    for index, item in enumerate(body.get("frames") or ()):
        try:
            frames.append(_frame_from_json(item))
        except Exception as exc:
            raise ROISetFormatError(
                f"frame {index} could not be read: {exc}"
            ) from exc

    roi_set = ROISet(
        uid=str(body.get("uid", "")) or ROISet().uid,
        name=str(body.get("name", "ROIs")),
        rois=tuple(rois),
        frames=tuple(frames),
        default_style=ROIStyle.from_json(body.get("default_style")),
        measurement_config=MeasurementConfig.from_json(
            body.get("measurement_config")
        ),
        revision=int(body.get("revision", 0) or 0),
        dataset_uid=body.get("dataset_uid"),
    )
    validate(roi_set)
    return roi_set


def _migrate(payload: dict, version: int) -> dict:
    """Bring an older payload up to the current schema.

    Empty for now — version 1 is the first — but the hook exists so the first
    migration is a function body rather than a redesign, and so the refusal
    above has a documented counterpart for versions we *can* read.
    """
    return payload


def validate(roi_set: ROISet) -> None:
    """Semantic checks a schema cannot express.

    Structural validity is not enough: a file can be perfectly well-formed JSON
    and still describe a set where two ROIs share a uid, or where a record
    points at a frame that is not in the file. Both produce silently wrong
    behaviour downstream — the first makes one ROI unreachable, the second
    makes a measurement unable to say what it was measured against.
    """
    seen_uids: set[str] = set()
    for roi in roi_set.rois:
        if roi.uid:
            if roi.uid in seen_uids:
                raise ROISetFormatError(
                    f"two ROIs share the identity {roi.uid!r}; one of them "
                    "would be unreachable"
                )
            seen_uids.add(roi.uid)

    known_frames = {frame.frame_uid for frame in roi_set.frames}
    for roi in roi_set.rois:
        if roi.frame_uid and roi.frame_uid not in known_frames:
            raise ROISetFormatError(
                f"ROI {roi.name!r} refers to a frame the file does not "
                "contain, so nothing could say which plane it belongs to"
            )


def dumps(roi_set: ROISet, *, indent: int | None = 2) -> str:
    return json.dumps(set_to_json(roi_set), indent=indent)


def loads(text: str) -> ROISet:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ROISetFormatError(f"this file is not valid JSON: {exc}") from exc
    return set_from_json(payload)


def write_set(path, roi_set: ROISet) -> None:
    """Write atomically: a temporary file in the same directory, then rename.

    Same directory because rename is only atomic within a filesystem. The
    failure this prevents is not exotic — a full disk, or a crash mid-write —
    and its result is a truncated file where a valid one used to be.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(dumps(roi_set))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def read_set(path) -> ROISet:
    return loads(Path(path).read_text(encoding="utf-8"))


__all__ = [
    "COORDINATE_CONVENTION",
    "FORMAT",
    "ROISetFormatError",
    "SCHEMA_VERSION",
    "dumps",
    "loads",
    "read_set",
    "set_from_json",
    "set_to_json",
    "validate",
    "write_set",
]
