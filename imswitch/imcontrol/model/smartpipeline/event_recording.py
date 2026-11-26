"""Saving what a smart pipeline triggered: the scan itself, as a recording.

A triggered scan is recorded by the Recording controller's own scan-bound
transaction (:meth:`RecordingController.recordScanSeries` with one frame:
bind the scan panel, read its geometry, arm the writer, wait for the arm,
request the scan) -- the same path as the Recording panel's *Scan once* --
so the file is an ordinary ImSwitch recording (HDF5/Zarr layout, scan
geometry, shared attributes) that ImProcess and *Load parameters from file*
open like any other. The pipeline only chooses where it goes and adds the
event's metadata: one folder per run under the recordings folder, one file
per event.

The recording replaces the plain scan start the ``run-scan`` action would
otherwise issue: the scan starts when the writer is armed, never before, so
no frame is lost and the scan is not started twice. A recording that is
refused or fails, or a file that is not there afterwards, fails the action,
and a failed action stops the pipeline: a triggered scan is never left
unsaved while the pipeline goes on firing.

What is known only after the scan (its end time, the latencies) is added
to the file's detector groups once the writer has closed it, off the GUI
thread.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime
from typing import Any, Callable

#: The attribute category of everything the pipeline adds to a recording.
ATTR_CATEGORY = "SmartPipeline"
#: Free disk space below which arming in Experiment is refused, bytes.
MIN_FREE_BYTES = 200 * 1024 * 1024


def slug(text: str, limit: int = 40) -> str:
    """``text`` as a file-name part: letters, digits, ``-`` and ``_``."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", str(text or "")).strip("-")
    return (cleaned or "pipeline")[:limit]


def run_folder_name(pipeline_name: str, started: datetime | None = None) -> str:
    started = started or datetime.now()
    return f"{started:%Y%m%d-%H%M%S}_{slug(pipeline_name)}"


def event_file_name(event: int, roi: Any = None, region: Any = None) -> str:
    """``event0003_R0_roi12``: the event number, the region, the ROI label."""
    parts = [f"event{int(event):04d}"]
    if region is not None:
        parts.append(f"R{region}")
    if roi is not None and roi != "":
        try:
            parts.append(f"roi{int(float(roi))}")
        except (TypeError, ValueError):
            parts.append(f"roi{slug(str(roi), 12)}")
    return "_".join(parts)


def attr_value(value):
    """``value`` as an HDF5/Zarr attribute: numbers and strings as they
    are, anything else as JSON text."""
    if isinstance(value, bool) or value is None:
        return str(value) if value is None else bool(value)
    if isinstance(value, (int, float, str)):
        return value
    try:
        import numpy as np

        if isinstance(value, np.generic):
            return value.item()
    except ImportError:  # pragma: no cover
        pass
    return json.dumps(value, default=str)


def event_attrs(fields: dict) -> dict:
    """Flat ``SmartPipeline:event:<key>`` attributes of ``fields``."""
    return {f"{ATTR_CATEGORY}:event:{key}": attr_value(value) for key, value in fields.items()
            if value is not None}


def folder_problems(folder: str, *, min_free_bytes: int = MIN_FREE_BYTES) -> list[str]:
    """Why triggered scans cannot be saved under ``folder``, or ``[]``: it
    cannot be created, is not writable, or the disk is nearly full."""
    if not folder:
        return ["no recordings folder is set (Recording panel)"]
    probe_dir = folder
    while probe_dir and not os.path.isdir(probe_dir):
        if os.path.exists(probe_dir):
            return [f"the recordings folder {folder!r} cannot be made: {probe_dir!r} is a file"]
        parent = os.path.dirname(probe_dir)
        if parent == probe_dir:
            break
        probe_dir = parent
    if not probe_dir or not os.path.isdir(probe_dir):
        return [f"the recordings folder {folder!r} does not exist and cannot be created (no existing parent)"]
    # Written to where the folder will be made: nothing is created by a check.
    try:
        probe = os.path.join(probe_dir, f".write-test-{os.getpid()}-{threading.get_ident()}")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.remove(probe)
    except OSError as exc:
        return [f"cannot write to the recordings folder {probe_dir!r}: {exc.strerror or exc}"]
    try:
        free = shutil.disk_usage(probe_dir).free
    except OSError:
        return []
    if free < min_free_bytes:
        return [f"only {free / 1e6:.0f} MB free on the disk of {folder!r} (need {min_free_bytes / 1e6:.0f} MB)"]
    return []


def recorded_files(savename: str) -> list[str]:
    """The files (or Zarr stores) a recording named ``savename`` wrote."""
    found = [p for p in glob.glob(glob.escape(savename) + "*") if not p.endswith(".tmp")]
    return sorted(found)


def add_attrs(path: str, detectors, attrs: dict) -> None:
    """Add ``attrs`` to the detector groups of a closed recording (HDF5 or
    Zarr); raises when the file cannot be opened."""
    attrs = {key: attr_value(value) for key, value in attrs.items()}
    if os.path.isdir(path) or path.endswith(".zarr"):
        import zarr

        root = zarr.open(path, mode="r+")
        for name in detectors:
            if name in root:
                root[name].attrs.update(attrs)
        return
    if path.lower().endswith((".h5", ".hdf5")):
        import h5py

        with h5py.File(path, "r+") as handle:
            for name in detectors:
                if name in handle:
                    for key, value in attrs.items():
                        handle[name].attrs[key] = value


class ScanRecording:
    """One triggered scan being recorded: what ``run-scan`` waits on instead
    of the bare scan's completion (``wait``, ``successful``, ``message``),
    plus the file it wrote (``paths``)."""

    def __init__(self, recorder: "TriggeredScanRecorder", detectors, file_base: str):
        self._recorder = recorder
        self.detectors = tuple(detectors)
        self.file_base = file_base
        self.savename: str | None = None
        self.paths: list[str] = []
        self.successful = False
        self.message = ""
        self.started_wall: float | None = None
        self.ended_wall: float | None = None
        self._ended = threading.Event()

    def _on_ended(self, failed, message) -> None:
        # GUI thread (the Recording controller's terminal): record, wake the waiter.
        self.ended_wall = time.time()
        self.successful = not failed
        self.message = str(message or "")
        self._ended.set()

    def wait(self, timeout: float | None = None) -> bool:
        if not self._ended.wait(timeout):
            return False
        if self.successful:
            self.paths = recorded_files(self.savename or "")
            if not self.paths:
                self.successful = False
                self.message = (f"the recording ended but no file named {os.path.basename(self.savename or '?')}* "
                                f"is in {os.path.dirname(self.savename or '') or '?'}")
        return True

    def abort(self) -> bool:
        return self._recorder.stop()


class TriggeredScanRecorder:
    """Records the triggered scans of one run.

    ``controllers()`` is the comm channel's controller registry (the
    ``Recording`` controller and the scan panel by name); ``marshal`` runs a
    function on the GUI thread and returns its value; ``folder`` is the
    run's folder; ``detectors`` the detectors recorded (the scan-driven ones:
    the APDs); ``save_format`` 'HDF5' or 'ZARR'.
    """

    def __init__(self, controllers: Callable[[], dict], marshal: Callable, *, folder: str, detectors,
                 save_format: str = "HDF5", base_attrs: dict | None = None, logger=None,
                 stage_position: Callable[[], dict] | None = None, region: int = 0):
        self._controllers = controllers
        self._stage_position = stage_position
        #: The region (tile) the run watches: 0 until a region loop sets it.
        self.region = int(region)
        self._marshal = marshal
        self.folder = str(folder)
        self.detectors = tuple(str(d) for d in detectors)
        self.save_format = str(save_format or "HDF5").upper()
        self.base_attrs = dict(base_attrs or {})
        self._logger = logger
        self._lock = threading.Lock()
        self.events = 0
        self.saved: list[dict] = []
        self.failures: list[str] = []

    def _registry(self) -> dict:
        registry = self._controllers() if callable(self._controllers) else self._controllers
        return registry or {}

    def problems(self, source: str | None = None) -> list[str]:
        """Why this recorder cannot save, checked at arm."""
        found = []
        registry = self._registry()
        recording = registry.get("Recording")
        if recording is None or not hasattr(recording, "recordScanSeries"):
            found.append("this setup has no Recording panel to save the triggered scans with")
        if source is not None and registry.get(source) is None:
            found.append(f"no scan panel {source!r} to record")
        if not self.detectors:
            found.append("no scan-driven detector (APD/PMT) to record the triggered scan from")
        if self.save_format not in ("HDF5", "ZARR"):
            found.append(f"triggered scans are saved as HDF5 or ZARR, not {self.save_format}")
        found.extend(folder_problems(self.folder))
        return found

    def stage_position(self) -> dict:
        """Where the stage is now ({positioner: {axis: um}}), {} when unknown."""
        if self._stage_position is None:
            return {}
        try:
            return dict(self._stage_position() or {})
        except Exception:  # noqa: BLE001 - a position that cannot be read is not a reason to lose the scan
            return {}

    def next_event(self) -> int:
        with self._lock:
            self.events += 1
            return self.events

    def start(self, source: str, *, file_name: str, attrs: dict) -> ScanRecording:
        """Arm the recording of the scan panel ``source``'s next scan and
        start that scan (GUI thread, marshalled). Raises with the reason when
        the Recording controller refuses."""
        recording = ScanRecording(self, self.detectors, file_name)
        extra = {**self.base_attrs, **attrs}

        def begin():
            registry = self._registry()
            controller = registry.get("Recording")
            panel = registry.get(source)
            if controller is None or panel is None:
                raise RuntimeError("no Recording panel" if controller is None else f"no scan panel {source!r}")
            os.makedirs(self.folder, exist_ok=True)
            result = controller.recordScanSeries(
                panel, list(self.detectors), frames=1, onEnded=recording._on_ended, folder=self.folder,
                fileName=file_name, saveFormat=self.save_format, extraAttrs=extra,
            )
            if not getattr(result, "accepted", False):
                raise RuntimeError(f"the recording was refused: {getattr(result, 'reason', '') or 'no reason given'}")
            recording.savename = getattr(controller, "savename", None) or os.path.join(self.folder, file_name)
            recording.started_wall = time.time()

        self._marshal(begin)
        return recording

    def stop(self) -> bool:
        def end():
            controller = self._registry().get("Recording")
            return bool(controller.stopRecording()) if controller is not None else False

        try:
            return bool(self._marshal(end))
        except Exception:  # noqa: BLE001 - an abort must not raise
            return False

    def finish(self, recording: ScanRecording, attrs: dict) -> list[str]:
        """After the scan: add ``attrs`` (scan end, latencies) to its file(s);
        returns the problems (empty when all went well). Off the GUI thread."""
        problems = []
        for path in recording.paths:
            try:
                add_attrs(path, recording.detectors, event_attrs(attrs))
            except Exception as exc:  # noqa: BLE001 - reported, the scan itself is saved
                problems.append(f"could not add the event's end metadata to {path}: {exc}")
        self.saved.append({"paths": list(recording.paths), **attrs})
        return problems


def scan_driven_detectors(detectorsManager) -> list[str]:
    """The setup's scan-driven detectors (APD, PMT): what a triggered scan records."""
    names = []
    try:
        items = list(detectorsManager)
    except TypeError:
        items = [(name, detectorsManager[name]) for name in detectorsManager.getAllDeviceNames()]
    for name, detector in items:
        if bool(getattr(detector, "isScanDriven", False)):
            names.append(str(name))
    return names


__all__ = [
    "ATTR_CATEGORY", "MIN_FREE_BYTES", "ScanRecording", "TriggeredScanRecorder", "add_attrs", "attr_value",
    "event_attrs", "event_file_name", "folder_problems", "recorded_files", "run_folder_name",
    "scan_driven_detectors", "slug",
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
