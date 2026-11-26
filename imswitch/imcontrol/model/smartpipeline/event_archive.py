"""Everything around a triggered event, saved next to its scan, and the run's summary.

The triggered scan itself is a recording (:mod:`.event_recording`). This
module writes, into the same run folder and off every thread that matters
(one worker thread of its own; the camera's GUI-thread slot and the
engine's tick only keep references):

* ``pipeline.json`` -- the pipeline document as armed, and ``run.json`` --
  the run's header (pipeline, mode, start time, transforms with their
  records, camera grid, region, stage position at arm);
* per event, ``<event file base>_context.h5``: the camera frames before the
  trigger (a bounded ring the live source keeps), the frame that triggered,
  the first frames after the scan; the frozen ROI mask and the ROI table at
  that moment; the F / F0 / dF/F trace of every ROI up to the event (the
  processor's bounded ring); the event's fields (the same as the scan
  file's ``SmartPipeline:event:*``) and the transform record;
* ``summary.csv`` / ``summary.json`` -- one row per trigger, scan, skip, dry
  run "would" and failure, with the region, the ROI's id, its camera
  centroid and the stage position, so a map of a run can be drawn from it
  alone;
* at the end, ``region_R<n>.h5`` -- the region's ROI set (labels, table with
  how often each ROI fired / was scanned / was skipped), the camera grid
  and the stage position: the unit a tiled run will repeat per tile.

A failed write is reported (``failures``, and the run's log through
``report``) and never stops the experiment: the scan file is what must not
be lost, and its saving is checked separately.
"""

from __future__ import annotations

import csv
import json
import os
import queue
import threading
import time
from datetime import datetime
from typing import Any, Callable

import numpy as np

SUMMARY_COLUMNS = (
    "wall_time", "time", "status", "event", "region", "roi_id", "roi_label", "dff", "F", "F0",
    "centroid_y_px", "centroid_x_px", "scan_centre_um", "scan_size_um", "stage_position_um", "reason",
    "scan_file", "context_file", "tick",
)


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    return value


class EventArchive:
    """The run's records besides the scans.

    ``source`` is the live frame source (its ``recent_frames`` /
    ``frames_after``); ``snapshot()`` returns, under the engine's lock, the
    ROI step's ``labels``, ``rois`` table and ``trace`` (or None);
    ``report(message)`` tells the run's log about a failed write.
    """

    def __init__(self, folder: str, *, source=None, snapshot: Callable[[], dict | None] | None = None,
                 pre_frames: int = 10, post_frames: int = 10, post_timeout_s: float = 5.0, region: int = 0,
                 stage_position: Callable[[], dict] | None = None, report: Callable[[str], None] | None = None,
                 logger=None):
        self.folder = str(folder)
        self._source = source
        self._snapshot = snapshot
        self.pre_frames = max(0, int(pre_frames))
        self.post_frames = max(0, int(post_frames))
        self.post_timeout_s = float(post_timeout_s)
        self.region = int(region)
        self._stage_position = stage_position
        self._report = report
        self._logger = logger
        self.rows: list[dict] = []
        self.failures: list[str] = []
        self.written: list[str] = []
        self._pending: dict[int, dict] = {}
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.header: dict = {}
        self._last_snapshot: dict | None = None

    # -- the worker ----------------------------------------------------------------

    def start(self, header: dict, document_json: str | None = None) -> None:
        """Begin the run: its header and document go to disk first."""
        self.header = {"started_wall": time.time(), "started": datetime.now().isoformat(timespec="seconds"),
                       "region": self.region, "stage_position_um_at_arm": self.stage_position(), **header}
        self._thread = threading.Thread(target=self._work, name="smart-pipeline-archive", daemon=True)
        self._thread.start()
        self._queue.put(("header", document_json))

    def _work(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            kind, payload = job
            try:
                if kind == "header":
                    self._write_header(payload)
                elif kind == "event":
                    self._write_event(payload)
                elif kind == "row":
                    self._add_row(payload)
                elif kind == "region":
                    self._write_region(payload)
            except Exception as exc:  # noqa: BLE001 - reported, the experiment goes on
                self._fail(f"{kind}: {type(exc).__name__}: {exc}")

    def _fail(self, message: str) -> None:
        self.failures.append(message)
        if self._logger is not None:
            self._logger.warning("Smart pipeline records: %s", message)
        if self._report is not None:
            try:
                self._report(message)
            except Exception:
                pass

    def close(self, timeout_s: float = 30.0) -> bool:
        """Write the region file, drain the queue and stop; whether it finished in time."""
        if self._thread is None:
            return True
        self._queue.put(("region", self._take_snapshot()))
        self._queue.put(None)
        self._thread.join(timeout_s)
        return not self._thread.is_alive()

    def stage_position(self) -> dict:
        if self._stage_position is None:
            return {}
        try:
            return dict(self._stage_position() or {})
        except Exception:  # noqa: BLE001
            return {}

    def _take_snapshot(self) -> dict | None:
        if self._snapshot is None:
            return self._last_snapshot
        try:
            snap = self._snapshot()
        except Exception as exc:  # noqa: BLE001
            self._fail(f"could not read the ROI state: {exc}")
            return self._last_snapshot
        if snap is not None:
            self._last_snapshot = snap
        return snap if snap is not None else self._last_snapshot

    # -- events (called from the scan action's thread) ----------------------------

    def frames_before(self, sequence) -> list:
        """The kept frames up to and including ``sequence`` (the trigger),
        at most ``pre_frames`` before it: references, cheap -- for the
        engine's thread, at the decision."""
        if self._source is None or not self.pre_frames:
            return []
        recent = list(self._source.recent_frames())
        if sequence is not None:
            recent = [f for f in recent if f.sequence <= int(sequence)]
        return recent[-(self.pre_frames + 1):]

    def event_started(self, event: int, file_base: str, fields: dict, pre: list | None = None) -> None:
        """At the trigger: what must be taken now -- the frames before it
        (``pre`` when taken at the decision, else now), the mask, the
        table, the traces."""
        frames = list(pre) if pre is not None else self.frames_before(fields.get("frame_sequence"))
        with self._lock:
            self._pending[event] = {"event": event, "file_base": file_base, "fields": dict(fields),
                                    "pre": frames, "snapshot": self._take_snapshot()}

    def event_finished(self, event: int, end: dict, paths: list, ok: bool = True, reason: str = "") -> None:
        """After the scan: the rest is written by the worker (it waits for
        the frames after the scan there, not here)."""
        with self._lock:
            pending = self._pending.pop(event, None)
        if pending is None:
            return
        pending.update(end=dict(end), paths=list(paths), ok=bool(ok), reason=reason)
        self._queue.put(("event", pending))

    def add_row(self, row: dict) -> None:
        """A summary row (a skip, a dry-run "would", a failure)."""
        self._queue.put(("row", dict(row)))

    # -- writing (worker) ------------------------------------------------------------

    def _write_header(self, document_json: str | None) -> None:
        os.makedirs(self.folder, exist_ok=True)
        if document_json:
            path = os.path.join(self.folder, "pipeline.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(document_json)
            self.written.append(path)
        path = os.path.join(self.folder, "run.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(_jsonable(self.header), handle, indent=2, default=str)
        self.written.append(path)

    def _write_event(self, pending: dict) -> None:
        import h5py

        fields = pending["fields"]
        end = pending["end"]
        post = []
        if self._source is not None and self.post_frames and end.get("scan_end_wall"):
            post = self._source.frames_after(float(end["scan_end_wall"]), self.post_frames, self.post_timeout_s)
        os.makedirs(self.folder, exist_ok=True)
        path = os.path.join(self.folder, f"{pending['file_base']}_context.h5")
        trigger_sequence = fields.get("frame_sequence")
        frames = list(pending["pre"]) + list(post)
        snap = pending.get("snapshot") or {}
        with h5py.File(path, "w") as handle:
            handle.attrs["SmartPipeline:event"] = json.dumps(_jsonable({**fields, **end}), default=str)
            handle.attrs["SmartPipeline:scan_files"] = json.dumps(pending["paths"])
            handle.attrs["SmartPipeline:region"] = self.region
            if fields.get("transform") is not None:
                handle.attrs["SmartPipeline:transform"] = json.dumps(_jsonable(fields["transform"]), default=str)
            shapes = {tuple(np.shape(f.image)) for f in frames}
            if frames and len(shapes) == 1:
                camera = handle.create_group("camera")
                camera.create_dataset("frames", data=np.stack([np.asarray(f.image) for f in frames]),
                                      chunks=(1, *next(iter(shapes))), compression="gzip", compression_opts=1)
                camera.create_dataset("sequence", data=np.array([f.sequence for f in frames], dtype=np.int64))
                camera.create_dataset("wall_time", data=np.array(
                    [(f.metadata or {}).get("wall_time", np.nan) for f in frames], dtype=np.float64))
                after = {id(f) for f in post}
                roles = ["post" if id(f) in after else ("trigger" if f.sequence == trigger_sequence else "pre")
                         for f in frames]
                camera.create_dataset("role", data=np.array(roles, dtype="S7"))
                camera.attrs["detector"] = str(frames[0].detector)
                identity = getattr(frames[0], "identity", None)
                if identity is not None:
                    camera.attrs["identity"] = json.dumps(_jsonable(getattr(identity, "__dict__", {})), default=str)
            _write_rois(handle, snap)
        self.written.append(path)
        status = "scanned" if pending["ok"] else "failed"
        # File names relative to the run folder: the folder can be moved.
        self._add_row({**fields, **end, "status": status, "reason": pending.get("reason", ""),
                       "wall_time": fields.get("decision_wall"),
                       "scan_file": ";".join(os.path.basename(p) for p in pending["paths"]),
                       "context_file": os.path.basename(path),
                       "centroid_y_px": _centroid(snap, fields.get("roi_label"), 0),
                       "centroid_x_px": _centroid(snap, fields.get("roi_label"), 1)})

    def _add_row(self, row: dict) -> None:
        row = {"wall_time": row.get("wall_time") or time.time(), **row}
        row.setdefault("time", datetime.fromtimestamp(float(row["wall_time"])).isoformat(timespec="milliseconds"))
        row.setdefault("region", self.region)
        if row.get("roi_label") is not None and row.get("roi_id") is None:
            row["roi_id"] = f"R{row['region']}-{int(float(row['roi_label']))}"
        self.rows.append(_jsonable(row))
        os.makedirs(self.folder, exist_ok=True)
        with open(os.path.join(self.folder, "summary.csv"), "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(SUMMARY_COLUMNS)
            for r in self.rows:
                writer.writerow([_cell(r.get(column)) for column in SUMMARY_COLUMNS])
        with open(os.path.join(self.folder, "summary.json"), "w", encoding="utf-8") as handle:
            json.dump({"run": _jsonable(self.header), "rows": self.rows}, handle, indent=1, default=str)

    def _write_region(self, snap: dict | None) -> None:
        import h5py

        os.makedirs(self.folder, exist_ok=True)
        path = os.path.join(self.folder, f"region_R{self.region}.h5")
        with h5py.File(path, "w") as handle:
            handle.attrs["SmartPipeline:region"] = self.region
            handle.attrs["SmartPipeline:run"] = json.dumps(_jsonable(self.header), default=str)
            handle.attrs["SmartPipeline:stage_position_um"] = json.dumps(self.header.get("stage_position_um_at_arm",
                                                                                         {}))
            _write_rois(handle, snap or {})
            if "rois" in handle:
                labels = np.asarray(handle["rois/table/label"][()])
                for status in ("triggered", "scanned", "skipped", "would"):
                    counts = np.zeros(labels.shape[0], dtype=np.int64)
                    for row in self.rows:
                        wanted = row.get("status") == status or (status == "triggered" and row.get("status") in
                                                                  ("scanned", "skipped", "would", "failed"))
                        if wanted and row.get("roi_label") is not None:
                            counts[labels == int(float(row["roi_label"]))] += 1
                    handle["rois/table"].create_dataset(status, data=counts)
        self.written.append(path)


def _cell(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return "" if value is None else value


def _centroid(snap: dict, label, axis: int):
    rois = (snap or {}).get("rois") or {}
    if label is None or not rois.get("n"):
        return None
    where = np.flatnonzero(np.asarray(rois["label"]) == int(float(label)))
    return float(rois["centroid"][where[0], axis]) if where.size else None


def _write_rois(handle, snap: dict) -> None:
    """The mask, the ROI table and the traces of ``snap`` into ``handle``."""
    rois = snap.get("rois") or {}
    labels = snap.get("labels")
    if labels is None or not rois.get("n"):
        return
    group = handle.create_group("rois")
    group.create_dataset("labels", data=np.asarray(labels), compression="gzip", compression_opts=1)
    table = group.create_group("table")
    table.create_dataset("label", data=np.asarray(rois["label"], dtype=np.int64))
    table.create_dataset("area_px", data=np.asarray(rois["area"], dtype=np.int64))
    table.create_dataset("centroid_y_px", data=np.asarray(rois["centroid"])[:, 0])
    table.create_dataset("centroid_x_px", data=np.asarray(rois["centroid"])[:, 1])
    for index, name in enumerate(("bbox_y0", "bbox_x0", "bbox_y1", "bbox_x1")):
        table.create_dataset(name, data=np.asarray(rois["bbox"])[:, index])
    trace = snap.get("trace")
    if trace:
        traces = handle.create_group("traces")
        for key in ("F", "F0", "dff", "frame", "t", "label"):
            traces.create_dataset(key, data=np.asarray(trace[key]))
        last = {key: np.asarray(trace[key])[-1] for key in ("F", "F0", "dff")} if len(trace["frame"]) else {}
        for key, value in last.items():
            table.create_dataset(f"last_{key}", data=value)


def roi_snapshot(state: dict | None) -> dict | None:
    """What :class:`EventArchive` keeps of a ``roi-dff`` step's state: the
    frozen labels, the ROI geometry and the trace (copied); None before the
    mask is frozen. Call it under the engine's lock."""
    if not state or not (state.get("rois") or {}).get("n"):
        return None
    from imswitch.improcess.processors.roi_dff.processor import trace_snapshot

    rois = state["rois"]
    return {"labels": state.get("labels"),
            "rois": {"n": rois["n"], "label": rois["label"].copy(), "area": rois["area"].copy(),
                     "centroid": rois["centroid"].copy(), "bbox": rois["bbox"].copy()},
            "trace": trace_snapshot(state)}


__all__ = ["SUMMARY_COLUMNS", "EventArchive", "roi_snapshot"]


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
