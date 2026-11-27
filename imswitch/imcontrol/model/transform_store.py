"""Where transform calibrations are kept, and how the smart pipeline finds them.

One calibration id, whatever method made it (beads, a foci grid, clicked
pairs), is saved as two files in ``<ImSwitchConfig>/calibrations/``:

* ``transforms/<id>.json`` -- the shared transform module's record
  (``imswitch.transform/1``: model, frames, contexts, residuals,
  provenance), which the Transform Calibration panel loads and the
  command line verifies;
* ``<id>.json`` -- a smart-pipeline transform of type ``spatial-transform``
  pointing at that record, so a pipeline's ``transform`` step names
  ``<id>``. It is written when both frames are known (``detector:<name>``,
  ``scanner:<name>`` or ``stage:<name>``): taken from the record when its
  frames are such tags, else from the detectors in its contexts, else
  given by the caller; without them the record is saved alone and the
  result says why.

Optional extras next to the record: ``<id>_qc.png`` and ``<id>_report.txt``.
The folder is shared with the laser-power store (``*.calib.json``), which
neither reader touches, and the pipeline loader does not read
``transforms/`` (records are reached through their entries). Records saved
elsewhere through the panel's *Save as file…* (any path, JSON or HDF5) are
not seen by the pipeline until saved under an id.

Qt-free.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from imswitch.imcommon.algorithms.transforms import SpatialTransform
from imswitch.imcommon.algorithms.transforms import io as transform_io

#: The transform-record subfolder of the calibrations folder.
TRANSFORMS_DIRNAME = "transforms"


def calibrations_root(folder=None) -> Path:
    """``folder``, or ``<ImSwitchConfig>/calibrations`` when ``None``."""
    from imswitch.imcontrol.model.smartpipeline.calibrations import calibrations_folder

    return calibrations_folder() if folder is None else Path(folder)


def file_name(calibration_id: str) -> str:
    """The file stem an id is saved under (unsafe characters replaced)."""
    from imswitch.imcontrol.model.smartpipeline.calibrations import _file_name

    return _file_name(calibration_id)


def record_path(calibration_id: str, folder=None) -> Path:
    return calibrations_root(folder) / TRANSFORMS_DIRNAME / f"{file_name(calibration_id)}.json"


def entry_path(calibration_id: str, folder=None) -> Path:
    return calibrations_root(folder) / f"{file_name(calibration_id)}.json"


def exists(calibration_id: str, folder=None) -> bool:
    """Whether ``calibration_id`` is taken (its record or its pipeline entry)."""
    return record_path(calibration_id, folder).exists() or entry_path(calibration_id, folder).exists()


def _frame(text) -> Optional[str]:
    from imswitch.imcommon.model.smartpipeline.frames import FrameTag

    try:
        tag = FrameTag.parse(text)
    except Exception:  # noqa: BLE001 - not a frame tag: the caller falls back
        return None
    return str(tag) if tag.known else None


def pipeline_frames(transform: SpatialTransform, source: Optional[str] = None,
                    target: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """The pipeline frames of a record: ``source``/``target`` when given and
    valid, else the record's own frames when they are frame tags, else
    ``detector:<name>`` from its contexts; ``None`` when unknown."""
    def pick(given, own, context):
        for candidate in (given, own, f"detector:{context.detector}" if context.detector else None):
            if candidate:
                frame = _frame(candidate)
                if frame:
                    return frame
        return None
    return (pick(source, transform.source_frame, transform.source_context),
            pick(target, transform.target_frame, transform.target_context))


def save_record(transform: SpatialTransform, calibration_id: str, folder=None, *, overwrite: bool = False,
                source_frame: Optional[str] = None, target_frame: Optional[str] = None,
                report: Optional[str] = None,
                figure: Optional[Callable[[Path], Any]] = None) -> dict:
    """Save ``transform`` under ``calibration_id`` (see the module docstring).

    Returns ``{"record": path, "pipeline": path (when written), "figure":
    path, "report": path, "notes": [...]}``. Raises ``CalibrationError``
    when the id is taken and ``overwrite`` is false. ``figure(path)`` writes
    the QC figure to ``path``; ``report`` is the text of the report file."""
    from imswitch.imcommon.model.smartpipeline import SpatialTransformRef
    from imswitch.imcontrol.model.smartpipeline.calibrations import CalibrationError, save_calibration

    root = calibrations_root(folder)
    name = file_name(calibration_id)
    path = record_path(calibration_id, folder)
    if not overwrite and exists(calibration_id, folder):
        raise CalibrationError(f"calibration {calibration_id!r} exists ({path}); choose another id or overwrite")
    transform_io.save_json(path, transform)
    paths: dict = {"record": path, "notes": []}
    source, target = pipeline_frames(transform, source_frame, target_frame)
    if source and target and source != target:
        reference = SpatialTransformRef(
            id=str(calibration_id), source_frame=source, target_frame=target,
            path=f"{TRANSFORMS_DIRNAME}/{path.name}", base=root)
        paths["pipeline"] = save_calibration(reference, root, overwrite=True)
    else:
        stale = entry_path(calibration_id, folder)
        if stale.exists():
            stale.unlink()   # an entry for an older record under this id would point at the new one
        paths["notes"].append(
            "not offered to the smart pipeline: the frames are unknown "
            f"(from {source or '?'} to {target or '?'}); save with a detector or scanner frame to use it there")
    if figure is not None:
        try:
            paths["figure"] = Path(figure(path.with_name(f"{name}_qc.png")))
        except Exception as error:  # noqa: BLE001 - a figure is a convenience, the record is saved
            paths["notes"].append(f"no QC figure: {error}")
    if report is not None:
        report_path = path.with_name(f"{name}_report.txt")
        listing = "\n".join(f"{kind}: {value}" for kind, value in paths.items() if kind != "notes")
        report_path.write_text(f"{report}\n{listing}\nreport: {report_path}\n", encoding="utf-8")
        paths["report"] = report_path
    return paths


def archive(calibration_id: str, folder=None) -> str:
    """Keep an existing calibration under a dated id
    (``<id>_<YYYYmmdd-HHMMSS>``): its record, figure and report are renamed
    and its pipeline entry rewritten to the new id. Returns the new id."""
    root = calibrations_root(folder)
    name = file_name(calibration_id)
    new_id = f"{name}_{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    transforms = root / TRANSFORMS_DIRNAME
    for suffix in (".json", "_qc.png", "_report.txt"):
        old = transforms / f"{name}{suffix}"
        if old.exists():
            old.rename(transforms / f"{new_id}{suffix}")
    entry = root / f"{name}.json"
    if entry.exists():
        payload = json.loads(entry.read_text(encoding="utf-8"))
        payload["id"] = new_id
        if payload.get("path"):
            payload["path"] = f"{TRANSFORMS_DIRNAME}/{new_id}.json"
        (root / f"{new_id}.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        entry.unlink()
    return new_id


def load_record(calibration, folder=None) -> SpatialTransform:
    """A saved calibration by id (``<calibrations>/transforms/<id>.json``)
    or by path (JSON or HDF5)."""
    path = Path(calibration)
    if not path.suffix:
        path = record_path(str(calibration), folder)
    return transform_io.load(path)


def list_records(folder=None) -> list[dict]:
    """The saved records, newest first: ``id``, ``path``, ``pipeline``
    (whether the pipeline can name it), frames and creation time. A file
    that is not a transform record is listed with its ``error``."""
    root = calibrations_root(folder)
    transforms = root / TRANSFORMS_DIRNAME
    out = []
    if not transforms.is_dir():
        return out
    for path in sorted(transforms.glob("*.json")):
        item = {"id": path.stem, "path": path, "pipeline": (root / path.name).exists()}
        try:
            record = transform_io.load_json(path)
            item.update(source_frame=record.source_frame, target_frame=record.target_frame,
                        created_at=record.created_at, units=record.units, kind=record.kind)
        except Exception as error:  # noqa: BLE001 - listed, not raised
            item["error"] = str(error)
        out.append(item)
    out.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    return out


class TransformStore:
    """The calibrations folder as the Transform Calibration panel's store
    (``exists``, ``save``, ``archive``, ``load``, ``list``, ``folder``)."""

    def __init__(self, folder=None):
        self._folder = folder

    @property
    def folder(self) -> Path:
        return calibrations_root(self._folder)

    def exists(self, calibration_id: str) -> bool:
        return exists(calibration_id, self._folder)

    def save(self, transform: SpatialTransform, calibration_id: str, *, overwrite: bool = False,
             report: Optional[str] = None, figure: Optional[Callable[[Path], Any]] = None, **frames) -> dict:
        return save_record(transform, calibration_id, self._folder, overwrite=overwrite, report=report,
                           figure=figure, **frames)

    def archive(self, calibration_id: str) -> str:
        return archive(calibration_id, self._folder)

    def load(self, calibration) -> SpatialTransform:
        return load_record(calibration, self._folder)

    def list(self) -> list[dict]:
        return list_records(self._folder)


__all__ = [
    "TRANSFORMS_DIRNAME",
    "TransformStore",
    "archive",
    "calibrations_root",
    "entry_path",
    "exists",
    "file_name",
    "list_records",
    "load_record",
    "pipeline_frames",
    "record_path",
    "save_record",
]
