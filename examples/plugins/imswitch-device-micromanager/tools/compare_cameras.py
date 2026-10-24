#!/usr/bin/env python
"""Measure one camera the same way whichever driver runs it, and diff two such reports.

The point: the same Hamamatsu camera driven once by ImSwitch2's native manager and once by
Micro-Manager's adapter through the bridge, measured identically, so the difference between
the two reports is the difference between the drivers. (DCAM allows one open handle per
camera, so the two runs are sequential: one setup file with one of the two camera entries
disabled, or two setup files; see ``docs/design/plans/micromanager-bridge-rig-test.md``.)

    python tools/compare_cameras.py measure SETUP.json --detector NAME --out report.json \\
        [--exposures 10,20,50] [--frames 200] [--seconds 5] [--roi x,y,w,h] [--binning 2]
    python tools/compare_cameras.py diff native.json bridge.json

``measure`` boots the real ImControl app headlessly on SETUP (only the napari canvas is
replaced) through ``imswitch_device_micromanager.headless`` and, for the named detector,
records its identity (model, shapes, dtype, pixel size, binnings, every parameter), then for
each exposure: sets it through the Settings controller, reads it back, runs the live view and
counts frames, and records ``--frames`` frames through the real ``RecordingManager``, reading
the HDF5 file back. Optional ROI and binning rounds apply the setting, read the applied
geometry back, record ``--round-frames`` frames and restore. Everything goes into one JSON report.

``diff`` prints a Markdown table of every measured quantity with PASS or CHECK, and the mean
intensities side by side without a verdict (whether both runs looked at the same scene is the
operator's call).

**It runs on a rig with the rig's real ``~/ImSwitchConfig``.** The user files are not moved. Like
starting ImSwitch2, importing it syncs the shipped defaults into that folder and the app writes its
log there; the tool never saves the widget state, never writes a setup file, and by default does not
restore the saved widget state either (so a run starts from the setup file's own values). Recordings
go to a temporary folder and are deleted unless ``--record-dir`` is given.

How frames are counted in the live view (and why). The live view is read the way the app reads
it, ``getLatestFrame()``, polled every ``LIVE_POLL_S``. A frame is "new" when a CRC32 of
``SIGNATURE_SAMPLES`` evenly spaced pixels differs from the previous poll's. A hash and not the
chunk path (``getChunk``) because the chunk path *consumes* frames: reading it here would steal them
from the recording and measure a different path than the one a user looks at; the exact frame count
of the chunk path is what the recording rows give. The limits of the method: it undercounts when the
camera is faster than half the poll rate (a warning is written into the report), and a perfectly
noiseless synthetic image would hash equal frame after frame (a sensor's shot and read noise never
does). A mock camera that makes up a new image on every call (the native Hamamatsu mock) is counted
at the poll rate, which says nothing about a frame rate.

Exit codes of ``measure``: 0 done, 1 done but a phase failed (the report says which), 2 could not
boot or bad arguments, 3 the detector runs on a mock or is not connected (refused; ``--allow-mock``
measures it anyway, for dry runs).
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import platform
import re
import shutil
import sys
import tempfile
import time
import traceback
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

TOOL_VERSION = "1"

#: Seconds between two live-view polls. Short against the frame period of any sCMOS at the
#: exposures this tool is for (10 ms and up, i.e. 100 fps and below), so one frame is seen by
#: several polls; the report warns when the poll rate is under twice the observed frame rate.
LIVE_POLL_S = 0.001
#: Pixels hashed per live frame (evenly spaced). Enough that two consecutive frames of a real
#: sensor differ in some sampled pixel through shot and read noise, few enough that the hash costs
#: microseconds and not the 10+ ms that hashing a 4-megapixel frame at poll rate would.
SIGNATURE_SAMPLES = 16384
#: Frames per block when the recorded HDF5 is read back for statistics: 4 frames of 2048 x 2048 as
#: float64 is about 134 MB, so the read-back never needs the recording's whole size in memory.
STATS_BLOCK_FRAMES = 4
#: Most exposures one run takes: a guard against a typo (``--exposures 1,2,...,1000``), since every
#: exposure is a full live window plus a full recording.
MAX_EXPOSURES = 16

#: Defaults, with the reason each is what it is (also printed by ``--help``).
DEFAULT_EXPOSURES = "10,20,50"
DEFAULT_FRAMES = 200
DEFAULT_SECONDS = 5.0
DEFAULT_ROUND_FRAMES = 20
DEFAULT_TOLERANCE_FPS = 0.10
DEFAULT_TOLERANCE_EXPOSURE = 0.01

#: The exposure parameter pairs ``(set, read-back)`` the managers have, tried in this order, and the
#: factor to milliseconds of each unit. Native Hamamatsu: ``Set exposure time`` / ``Real exposure time``
#: in seconds; the bridge: ``Exposure`` in milliseconds (Micro-Manager's unit).
EXPOSURE_PARAMETERS = (("Set exposure time", "Real exposure time"), ("Exposure", "Exposure"))
UNIT_TO_MS = {"s": 1000.0, "ms": 1.0, "us": 1e-3, "µs": 1e-3}

BRIDGE_CAMERA_MANAGER = "micromanager.camera"

#: Tries to put the ROI back after a round. The Settings panel's size limits follow a binning change one
#: ROI call late (seen on the demo camera: after binning 2 and back to 1 the first full-frame ROI is clipped
#: to the old limit, the second sticks), so one retry is the known need; three leaves room without looping.
RESTORE_ATTEMPTS = 3

EXIT_OK, EXIT_PHASE_FAILED, EXIT_USAGE, EXIT_MOCK = 0, 1, 2, 3


# =========================================================================== helpers
def plain(value: Any) -> Any:
    """``value`` as something ``json`` writes: numpy scalars and arrays, bytes, tuples, anything else as text."""
    import numpy as np

    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (list, tuple, set, frozenset)):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {str(plain(k)): plain(v) for k, v in value.items()}
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def frame_signature(frame) -> int:
    """CRC32 of ``SIGNATURE_SAMPLES`` evenly spaced pixels of ``frame`` (see the module docstring)."""
    import numpy as np

    flat = np.ravel(frame)
    step = max(1, flat.size // SIGNATURE_SAMPLES)
    return zlib.crc32(np.ascontiguousarray(flat[::step]).tobytes())


def frame_summary(frame) -> Dict[str, Any]:
    import numpy as np

    arr = np.asarray(frame)
    return {"shape": list(arr.shape), "dtype": str(arr.dtype), "mean": float(arr.mean()),
            "std": float(arr.std()), "min": float(arr.min()), "max": float(arr.max())}


def parse_exposures(text: str) -> List[float]:
    try:
        values = [float(part) for part in text.split(",") if part.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(f"--exposures takes comma-separated milliseconds, got {text!r}")
    if not values or any(not math.isfinite(v) or v <= 0 for v in values):
        raise argparse.ArgumentTypeError(f"--exposures needs one or more positive numbers, got {text!r}")
    if len(values) > MAX_EXPOSURES:
        raise argparse.ArgumentTypeError(f"--exposures takes at most {MAX_EXPOSURES} values")
    return values


def parse_roi(text: str) -> Tuple[int, int, int, int]:
    try:
        x, y, w, h = (int(part) for part in text.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"--roi takes x,y,w,h as four whole numbers, got {text!r}")
    if x < 0 or y < 0 or w < 1 or h < 1:
        raise argparse.ArgumentTypeError(f"--roi needs x,y >= 0 and w,h >= 1, got {text!r}")
    return x, y, w, h


def package_versions() -> Dict[str, Optional[str]]:
    from importlib import metadata

    out: Dict[str, Optional[str]] = {}
    for name in ("imswitch2", "imswitch-device-micromanager", "pymmcore-plus", "pymmcore", "numpy", "h5py"):
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = None
    return out


def recording_stats(path: Path, detector: str) -> Dict[str, Any]:
    """Read a recorded HDF5 back block by block: counters, shape, dtype, per-frame mean and std, summarised."""
    import h5py
    import numpy as np

    with h5py.File(path, "r") as h5:
        group = h5[detector]
        data = group["data"]
        committed = int(group["frames_committed"][0])
        complete = int(group["stream_complete"][0])
        count = data.shape[0]
        means = np.empty(count, dtype=np.float64)
        stds = np.empty(count, dtype=np.float64)
        for start in range(0, count, STATS_BLOCK_FRAMES):
            block = np.asarray(data[start:start + STATS_BLOCK_FRAMES]).reshape(
                min(STATS_BLOCK_FRAMES, count - start), -1).astype(np.float64)
            means[start:start + len(block)] = block.mean(axis=1)
            stds[start:start + len(block)] = block.std(axis=1)
        return {
            "shape": list(data.shape), "dtype": str(data.dtype), "frames_committed": committed,
            "stream_complete": complete, "frames_written": committed,
            "mean_of_means": float(means.mean()) if count else None,
            "std_of_means": float(means.std()) if count else None,
            "min_mean": float(means.min()) if count else None,
            "max_mean": float(means.max()) if count else None,
            "mean_of_stds": float(stds.mean()) if count else None,
        }


# =========================================================================== measure
class Measurement:
    """One ``measure`` run: the booted app, the detector, and the report being filled."""

    def __init__(self, booted, name: str, args, record_dir: Path):
        self.booted, self.name, self.args, self.record_dir = booted, name, args, record_dir
        self.detector = booted.detector(name)
        self.api = booted.api
        info = booted.setup_info.detectors[name]
        self.manager_name = getattr(info, "managerName", None)
        self.is_bridge = self.manager_name == BRIDGE_CAMERA_MANAGER
        self.label = (getattr(info, "managerProperties", None) or {}).get("label") if self.is_bridge else None
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.exposure_params = self._resolve_exposure_parameters()

    # ------------------------------------------------------------ exposure
    def _resolve_exposure_parameters(self) -> Dict[str, Any]:
        parameters = self.detector.parameters
        for set_name, read_name in EXPOSURE_PARAMETERS:
            if set_name in parameters and read_name in parameters:
                unit = getattr(parameters[set_name], "valueUnits", None)
                if unit not in UNIT_TO_MS:
                    raise SystemExit(
                        f"The exposure parameter {set_name!r} of {self.name!r} has the unit {unit!r}, "
                        f"which this tool cannot convert to milliseconds (known: {sorted(UNIT_TO_MS)}).")
                return {"set": set_name, "read": read_name, "unit": unit, "to_ms": UNIT_TO_MS[unit]}
        raise SystemExit(
            f"Detector {self.name!r} has none of the exposure parameters "
            f"{[pair[0] for pair in EXPOSURE_PARAMETERS]}; its parameters are {list(parameters)}.")

    def set_exposure(self, milliseconds: float) -> float:
        """Set through the Settings controller (the app's own path); return the read-back in ms."""
        p = self.exposure_params
        self.api.setDetectorParameter(self.name, p["set"], milliseconds / p["to_ms"])
        return float(self.detector.parameters[p["read"]].value) * p["to_ms"]

    def exposure_ms(self) -> float:
        p = self.exposure_params
        return float(self.detector.parameters[p["read"]].value) * p["to_ms"]

    # ------------------------------------------------------------ bridge state
    def stream_state(self) -> Optional[Dict[str, Any]]:
        """The bridge's per-stream counters of this camera (``frames_received``, ``frames_lost``, fault); None for a native camera."""
        if not self.is_bridge:
            return None
        try:
            streams = self.booted.hub.host.snapshot()["streams"]
        except Exception as exc:  # noqa: BLE001 - recorded, not fatal
            return {"error": f"{type(exc).__name__}: {exc}"}
        stream = streams.get(self.label)
        if stream is None:
            return {"error": f"no stream {self.label!r} in the hub's snapshot (streams: {sorted(streams)})"}
        return {key: stream[key] for key in ("frames_received", "frames_delivered", "frames_lost",
                                             "frames_dropped_consumer", "fault", "shape", "dtype")}

    def hub_summary(self) -> Optional[Dict[str, Any]]:
        if not self.is_bridge:
            return None
        try:
            snap = self.booted.hub.host.snapshot()
            return {"version_info": snap["version_info"], "adapter_source": snap["adapter_source"],
                    "adapter_paths": snap["adapter_paths"]}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}

    # ------------------------------------------------------------ identity
    def identity(self) -> Dict[str, Any]:
        det = self.detector
        parameters = {}
        for pname, param in det.parameters.items():
            entry = {"value": plain(param.value), "editable": bool(param.editable),
                     "group": getattr(param, "group", None)}
            if hasattr(param, "options"):
                entry["options"] = plain(list(param.options))
            else:
                entry["units"] = getattr(param, "valueUnits", None)
            parameters[pname] = entry
        advanced: List[Dict[str, Any]] = []
        getter = getattr(det, "getAdvancedPropertyInfo", None)
        if callable(getter):
            try:
                for item in getter() or []:
                    advanced.append({k: plain(item.get(k)) for k in
                                     ("name", "value", "type", "readable", "writable", "range", "text_options")})
            except Exception as exc:  # noqa: BLE001
                self.warnings.append(f"getAdvancedPropertyInfo failed: {type(exc).__name__}: {exc}")
        runtime_mode = getattr(det, "runtimeMode", None)
        state = getattr(det, "connectionState", None)
        return {
            "model": det.model, "full_shape": list(det.fullShape), "shape": list(det.shape),
            "frame_start": list(det.frameStart), "dtype": str(det.dtype), "bit_depth": int(det.bitDepth),
            "pixel_size_um": plain(det.pixelSizeUm), "binning": int(det.binning),
            "supported_binnings": plain(det.supportedBinnings), "croppable": bool(det.croppable),
            "runtime_mode": getattr(runtime_mode, "value", runtime_mode),
            "connection_state": getattr(state, "value", state),
            "status_summary": getattr(det, "connectionStatusSummary", None),
            "exposure_parameter": {k: self.exposure_params[k] for k in ("set", "read", "unit")},
            "parameter_names": list(det.parameters), "parameters": parameters,
            "advanced_properties": advanced,
        }

    # ------------------------------------------------------------ live view
    def live(self, seconds: float) -> Dict[str, Any]:
        """Count new frames of ``getLatestFrame`` for ``seconds`` after the first new one (module docstring)."""
        booted, det = self.booted, self.detector
        headless = import_headless()

        started_state = self.stream_state()
        self.api.setLiveViewActive(True)
        observations: List[float] = []
        polls = 0
        last_signature = None
        last_frame = None
        received_first = None
        error = None
        begin = time.perf_counter()
        try:
            while True:
                booted.pump(LIVE_POLL_S)
                frame = det.getLatestFrame()
                polls += 1
                now = time.perf_counter()
                signature = frame_signature(frame)
                if signature != last_signature:
                    if last_signature is not None:
                        observations.append(now)
                        if len(observations) == 1 and self.is_bridge:
                            received_first = (self.stream_state() or {}).get("frames_received")
                    last_signature, last_frame = signature, frame
                if observations and now - observations[0] >= seconds:
                    break
                if not observations and now - begin > headless.DEADLINE_S:
                    error = f"no new live frame within {headless.DEADLINE_S:g} s"
                    break
            end = time.perf_counter()
            end_state = self.stream_state()
        finally:
            self.api.setLiveViewActive(False)
            if self.is_bridge:
                try:
                    booted.wait_until(lambda: not booted.hub.host.running_streams(), "the live stream to stop")
                except AssertionError as exc:
                    self.warnings.append(str(exc))
        result: Dict[str, Any] = {
            "seconds_requested": seconds, "polls": polls, "poll_rate_hz": polls / max(end - begin, 1e-9),
            "new_frames_seen": len(observations), "error": error,
            "window_s": (observations[-1] - observations[0]) if len(observations) > 1 else None,
            "fps": None, "last_frame": frame_summary(last_frame) if last_frame is not None else None,
        }
        if len(observations) > 1:
            result["fps"] = (len(observations) - 1) / result["window_s"]
            if result["poll_rate_hz"] < 2 * result["fps"]:
                self.warnings.append(
                    f"live polling ({result['poll_rate_hz']:.0f} Hz) is under twice the observed frame rate "
                    f"({result['fps']:.0f} fps): the live fps undercounts; trust the recording fps")
        if self.is_bridge and received_first is not None and end_state and "frames_received" in end_state:
            delivered = end_state["frames_received"] - received_first
            result["bridge"] = {"frames_received_in_window": delivered,
                                "fps_from_counter": delivered / max(end - observations[0], 1e-9),
                                "stream_before": started_state, "stream_after": end_state}
        elif self.is_bridge:
            result["bridge"] = {"stream_before": started_state, "stream_after": end_state}
        return result

    # ------------------------------------------------------------ recording
    def record(self, tag: str, count: int) -> Dict[str, Any]:
        headless = import_headless()

        timings: Dict[str, float] = {}
        path = headless.record_frames(self.booted, self.record_dir, tag, count, detector=self.name, timings=timings)
        try:
            stats = recording_stats(path, self.name)
            stats["file_bytes"] = path.stat().st_size
        finally:
            if not self.args.record_dir:
                path.unlink(missing_ok=True)
        stats["frames_requested"] = count
        stats["wall_s"] = timings["wall_s"]
        stats["fps"] = stats["frames_written"] / timings["wall_s"] if timings["wall_s"] > 0 else None
        stats["bridge"] = self.stream_state()
        return stats

    def restore_roi(self, frame_start, shape) -> None:
        """Put the ROI back through the Settings controller, retrying while it reads back wrong (``RESTORE_ATTEMPTS``)."""
        det = self.detector
        for _ in range(RESTORE_ATTEMPTS):
            self.api.setDetectorROI(self.name, tuple(frame_start), tuple(shape))
            if tuple(det.frameStart) == tuple(frame_start) and tuple(det.shape) == tuple(shape):
                return
        self.warnings.append(f"the ROI could not be restored to start {tuple(frame_start)}, shape {tuple(shape)} "
                             f"(the detector reads start {tuple(det.frameStart)}, shape {tuple(det.shape)})")

    # ------------------------------------------------------------ phases
    def guarded(self, what: str, function, *args) -> Dict[str, Any]:
        """Run one phase; an exception becomes ``{"error": ...}`` in the report and the run goes on."""
        try:
            return function(*args)
        except Exception as exc:  # noqa: BLE001 - the report must survive a failing phase
            detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            self.errors.append(f"{what}: {detail}")
            print(f"  FAILED {what}: {detail}", file=sys.stderr)
            return {"error": detail, "traceback": traceback.format_exc()}

    def exposure_round(self, milliseconds: float, index: int) -> Dict[str, Any]:
        entry: Dict[str, Any] = {"requested_ms": milliseconds}
        try:
            entry["readback_ms"] = self.set_exposure(milliseconds)
        except Exception as exc:  # noqa: BLE001 - recorded; the other phases of this exposure would be meaningless
            entry["error"] = f"{type(exc).__name__}: {exc}"
            self.errors.append(f"setting the exposure to {milliseconds:g} ms: {entry['error']}")
            return entry
        print(f"exposure {milliseconds:g} ms: read back {entry['readback_ms']:.6g} ms")
        entry["live"] = self.guarded(f"live view at {milliseconds:g} ms", self.live, self.args.seconds)
        if entry["live"].get("fps") is not None:
            print(f"  live view: {entry['live']['fps']:.2f} fps ({entry['live']['new_frames_seen']} new frames seen)")
        entry["recording"] = self.guarded(f"recording at {milliseconds:g} ms", self.record,
                                          f"cmp_exposure{index}", self.args.frames)
        rec = entry["recording"]
        if "frames_written" in rec:
            print(f"  recording: {rec['frames_written']}/{rec['frames_requested']} frames, "
                  f"{rec['wall_s']:.2f} s, {rec['fps']:.2f} fps, shape {tuple(rec['shape'])}, {rec['dtype']}")
        return entry

    def roi_round(self, roi: Tuple[int, int, int, int]) -> Dict[str, Any]:
        det = self.detector
        initial = (tuple(det.frameStart), tuple(det.shape))
        entry: Dict[str, Any] = {"requested": list(roi), "units": "Settings-panel ROI fields (x0, y0, width, height)"}
        try:
            self.api.setDetectorROI(self.name, (roi[0], roi[1]), (roi[2], roi[3]))
            entry["applied_frame_start"] = list(det.frameStart)
            entry["applied_shape"] = list(det.shape)
            print(f"ROI {roi}: applied start {entry['applied_frame_start']}, shape {entry['applied_shape']}")
            entry["recording"] = self.guarded("ROI recording", self.record, "cmp_roi", self.args.round_frames)
        finally:
            self.restore_roi(*initial)
            entry["restored_frame_start"] = list(det.frameStart)
            entry["restored_shape"] = list(det.shape)
        return entry

    def roi_binned_round(self, roi: Tuple[int, int, int, int], binning: int) -> Dict[str, Any]:
        """The ROI at a binning other than 1. The Settings panel multiplies its fields by the binning before
        calling ``crop`` (a native camera's ``crop`` takes unbinned sensor coordinates), so this round shows
        whether a driver's ``crop`` agrees with that convention."""
        det = self.detector
        initial_binning, initial_roi = int(det.binning), (tuple(det.frameStart), tuple(det.shape))
        entry: Dict[str, Any] = {"requested_roi": list(roi), "requested_binning": binning,
                                 "units": "Settings-panel ROI fields (x0, y0, width, height), in binned pixels"}
        try:
            self.api.setDetectorBinning(self.name, binning)
            self.api.setDetectorROI(self.name, (roi[0], roi[1]), (roi[2], roi[3]))
            entry.update(applied_binning=int(det.binning), applied_frame_start=list(det.frameStart),
                         applied_shape=list(det.shape))
            print(f"ROI {roi} at binning {binning}: applied start {entry['applied_frame_start']}, "
                  f"shape {entry['applied_shape']}")
            entry["recording"] = self.guarded("ROI-at-binning recording", self.record, "cmp_roi_binned",
                                              self.args.round_frames)
        finally:
            self.api.setDetectorBinning(self.name, initial_binning)
            self.restore_roi(*initial_roi)
            entry["restored_binning"] = int(det.binning)
            entry["restored_shape"] = list(det.shape)
        return entry

    def binning_round(self, binning: int) -> Dict[str, Any]:
        det = self.detector
        initial = int(det.binning)
        entry: Dict[str, Any] = {"requested": binning}
        try:
            self.api.setDetectorBinning(self.name, binning)
            entry.update(applied_binning=int(det.binning), applied_shape=list(det.shape),
                         applied_frame_start=list(det.frameStart), pixel_size_um=plain(det.pixelSizeUm))
            print(f"binning {binning}: applied {entry['applied_binning']}, manager shape {entry['applied_shape']}")
            entry["recording"] = self.guarded("binning recording", self.record, "cmp_binning", self.args.round_frames)
        finally:
            self.api.setDetectorBinning(self.name, initial)
            entry["restored_binning"] = int(det.binning)
        return entry


def check_widgets(booted) -> Optional[str]:
    """The widgets the tool drives; a message when the setup lacks one."""
    needed = {"Settings", "View", "Recording"}
    present = set(booted.controller.controllers)
    missing = sorted(needed - present)
    return (f"the setup's availableWidgets lack {missing}; the tool drives the Settings, View and Recording "
            f"controllers (add them to availableWidgets)") if missing else None


def import_headless():
    """``imswitch_device_micromanager.headless``; from this checkout's ``src`` when the package is not installed."""
    try:
        from imswitch_device_micromanager import headless
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from imswitch_device_micromanager import headless
    return headless


def run_measure(args) -> int:
    setup = Path(args.setup).resolve()
    if not setup.is_file():
        print(f"error: setup file {setup} does not exist", file=sys.stderr)
        return EXIT_USAGE
    setup_bytes = setup.read_bytes()

    headless = import_headless()
    try:
        booted = headless.boot(None, move_user_files=False, setup=setup,
                               restore_widget_state=args.restore_saved_state)
    except Exception:  # noqa: BLE001 - reported with the traceback
        print("error: could not boot the app on the setup:", file=sys.stderr)
        traceback.print_exc()
        return EXIT_USAGE

    record_dir = Path(args.record_dir) if args.record_dir else Path(tempfile.mkdtemp(prefix="compare-cameras-"))
    record_dir.mkdir(parents=True, exist_ok=True)
    try:
        return _measure_booted(booted, args, setup, setup_bytes, record_dir)
    finally:
        booted.close()
        if not args.record_dir:
            shutil.rmtree(record_dir, ignore_errors=True)


def _measure_booted(booted, args, setup: Path, setup_bytes: bytes, record_dir: Path) -> int:
    master = booted.master
    names = master.detectorsManager.getAllDeviceNames()
    if args.detector not in names:
        disabled = sorted((getattr(booted.setup_info, "disabledDevices", None) or {}).get("detectors", {}))
        print(f"error: the booted app has no detector {args.detector!r}; its detectors are {names}"
              + (f" (disabled in the setup file: {disabled})" if disabled else ""), file=sys.stderr)
        return EXIT_USAGE
    problem = check_widgets(booted)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_USAGE

    run = Measurement(booted, args.detector, args, record_dir)
    identity = run.identity()
    where = (f"{identity['model']!r}, runtime {identity['runtime_mode']}, state {identity['connection_state']}, "
             f"{identity['status_summary']!r}")
    print(f"detector {args.detector}: {type(run.detector).__name__} {where}")
    if (identity["runtime_mode"] not in (None, "real") or identity["connection_state"] not in (None, "connected")) \
            and not args.allow_mock:
        print(f"refused: this detector is not confirmed connected to its hardware (runtime mode "
              f"{identity['runtime_mode']!r}, state {identity['connection_state']!r}), e.g. a camera that "
              f"fell back to its mock, so the numbers would not be the camera's. Fix the connection (see "
              f"the log above), or pass --allow-mock for a dry run.", file=sys.stderr)
        return EXIT_MOCK

    frame_bytes = 1
    for side in identity["full_shape"]:
        frame_bytes *= side
    frame_bytes *= max(1, identity["bit_depth"] // 8)
    print(f"each recording is written to {record_dir} and needs about {frame_bytes * args.frames / 1e6:.0f} MB "
          f"({'kept' if args.record_dir else 'deleted afterwards'})")

    report: Dict[str, Any] = {
        "tool_version": TOOL_VERSION,
        "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "setup": {"path": str(setup), "sha256": hashlib.sha256(setup_bytes).hexdigest(),
                  "enabled_detectors": names,
                  "disabled_detectors": sorted((getattr(booted.setup_info, "disabledDevices", None) or {})
                                               .get("detectors", {}))},
        "detector": args.detector,
        "manager": {"class": f"{type(run.detector).__module__}.{type(run.detector).__qualname__}",
                    "managerName": run.manager_name, "bridge": run.is_bridge},
        "environment": {"platform": platform.platform(), "python": platform.python_version(),
                        "implementation": platform.python_implementation(), "packages": package_versions()},
        "options": {"exposures_ms": args.exposures, "frames": args.frames, "seconds": args.seconds,
                    "round_frames": args.round_frames, "roi": list(args.roi) if args.roi else None,
                    "binning": args.binning, "restore_saved_state": args.restore_saved_state,
                    "live_poll_s": LIVE_POLL_S, "signature_samples": SIGNATURE_SAMPLES},
        "identity": identity,
        "hub": run.hub_summary(),
    }
    initial_exposure = run.exposure_ms()
    report["exposures"] = [run.exposure_round(ms, i) for i, ms in enumerate(args.exposures)]
    # Rounds run at the first exposure, after the exposure rounds; the exposure is set again for them.
    if args.roi or args.binning:
        run.guarded("setting the exposure for the rounds", run.set_exposure, args.exposures[0])
    report["roi"] = run.guarded("ROI round", run.roi_round, args.roi) if args.roi else None
    report["binning"] = run.guarded("binning round", run.binning_round, args.binning) if args.binning else None
    report["roi_binned"] = (run.guarded("ROI-at-binning round", run.roi_binned_round, args.roi, args.binning)
                            if args.roi and args.binning else None)
    run.guarded("restoring the exposure", run.set_exposure, initial_exposure)
    report["warnings"] = run.warnings
    report["errors"] = run.errors

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plain(report), indent=2, allow_nan=True) + "\n", encoding="utf-8")
    print(f"\nreport written to {out}\n")
    print(summary_table(report))
    for line in run.warnings:
        print(f"warning: {line}", file=sys.stderr)
    return EXIT_PHASE_FAILED if run.errors else EXIT_OK


def summary_table(report: Dict[str, Any]) -> str:
    lines = ["| exposure (ms) | read back (ms) | live fps | frames written | recording fps | lost | mean of means |",
             "|---|---|---|---|---|---|---|"]
    for entry in report["exposures"]:
        live, rec = entry.get("live", {}), entry.get("recording", {})
        lost = _lost(rec)
        lines.append("| " + " | ".join([
            f"{entry['requested_ms']:g}", fmt(entry.get("readback_ms")), fmt(live.get("fps")),
            f"{rec.get('frames_written', 'n/a')}/{rec.get('frames_requested', 'n/a')}",
            fmt(rec.get("fps")), fmt(lost), fmt(rec.get("mean_of_means"))]) + " |")
    return "\n".join(lines)


# =========================================================================== diff
def fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return f"{value:.5g}"
    if isinstance(value, (list, tuple)):
        return ", ".join(fmt(v) for v in value)
    return str(value)


def dims(value: Any) -> str:
    """A shape as ``2048x2048`` (``n/a`` for None)."""
    return "n/a" if value is None else "x".join(str(v) for v in value)


def _lost(recording: Dict[str, Any]) -> Optional[int]:
    """Frames the bridge says it lost (plus 1 for a fault); None when there is no counter (a native camera)."""
    stream = recording.get("bridge") if isinstance(recording, dict) else None
    if not stream or "frames_lost" not in stream:
        return None
    return int(stream["frames_lost"]) + (1 if stream.get("fault") else 0)


class Table:
    def __init__(self):
        self.rows: List[Tuple[str, str, str, str, str]] = []
        self.verdicts: List[str] = []

    def add(self, quantity: str, a: Any, b: Any, rel: Optional[float], verdict: str, show=fmt) -> None:
        self.rows.append((quantity, show(a), show(b), "" if rel is None else f"{rel * 100:+.2f} %", verdict))
        self.verdicts.append(verdict)

    def equal(self, quantity: str, a: Any, b: Any, show=fmt) -> None:
        self.add(quantity, a, b, None, "n/a" if a is None and b is None else ("PASS" if a == b else "CHECK"), show)

    def relative(self, quantity: str, a: Optional[float], b: Optional[float], tolerance: float) -> None:
        if a is None or b is None:
            self.add(quantity, a, b, None, "n/a")
            return
        if a == 0:
            rel = 0.0 if b == 0 else None
        else:
            rel = (b - a) / a
        self.add(quantity, a, b, rel, "CHECK" if rel is None or abs(rel) > tolerance else "PASS")

    def info(self, quantity: str, a: Any, b: Any) -> None:
        self.add(quantity, a, b, None, "-")

    def render(self, header: Sequence[str]) -> str:
        out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
        out += ["| " + " | ".join(row) + " |" for row in self.rows]
        return "\n".join(out)


def load_report(path: str) -> Dict[str, Any]:
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    if "tool_version" not in report or "identity" not in report:
        raise SystemExit(f"{path} is not a compare_cameras report (no tool_version / identity)")
    return report


def _normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _property_index(report: Dict[str, Any]) -> Dict[str, Tuple[str, str]]:
    """Normalised name -> (name as the driver spells it, value text with the allowed values or text options)."""
    index: Dict[str, Tuple[str, str]] = {}
    identity = report["identity"]
    for name, entry in (identity.get("parameters") or {}).items():
        text = fmt(entry.get("value"))
        if entry.get("options"):
            text += f" [{', '.join(str(o) for o in entry['options'])}]"
        elif entry.get("units"):
            text += f" {entry['units']}"
        index[_normalise(name)] = (name, text)
    for entry in identity.get("advanced_properties") or []:
        text = fmt(entry.get("value"))
        if entry.get("text_options"):
            text += " {" + ", ".join(f"{k}={v}" for k, v in entry["text_options"].items()) + "}"
        index.setdefault(_normalise(entry["name"]), (entry["name"], text))
    return index


def build_diff(a: Dict[str, Any], b: Dict[str, Any], tolerance_fps: float, tolerance_exposure: float) -> str:
    ia, ib = a["identity"], b["identity"]
    table = Table()
    table.info("manager class", a["manager"]["class"].rsplit(".", 1)[-1], b["manager"]["class"].rsplit(".", 1)[-1])
    table.info("model", ia["model"], ib["model"])
    real = lambda i: i.get("runtime_mode") in (None, "real") and i.get("connection_state") in (None, "connected")  # noqa: E731
    table.add("runs on the real backend", real(ia), real(ib), None, "PASS" if real(ia) and real(ib) else "CHECK")
    table.equal("full shape (w x h)", ia["full_shape"], ib["full_shape"], dims)
    table.equal("shape at start (w x h)", ia["shape"], ib["shape"], dims)
    table.equal("binning at start", ia["binning"], ib["binning"])
    table.equal("dtype", ia["dtype"], ib["dtype"])
    table.equal("pixel size (um, z/y/x)", ia["pixel_size_um"], ib["pixel_size_um"])
    table.equal("supported binnings", sorted(ia["supported_binnings"]), sorted(ib["supported_binnings"]))

    by_ms_a = {e["requested_ms"]: e for e in a["exposures"]}
    by_ms_b = {e["requested_ms"]: e for e in b["exposures"]}
    for ms in sorted(set(by_ms_a) | set(by_ms_b)):
        ea, eb = by_ms_a.get(ms), by_ms_b.get(ms)
        label = f"{ms:g} ms"
        if ea is None or eb is None:
            table.add(f"{label}: measured in both reports", ea is not None, eb is not None, None, "CHECK")
            continue
        table.relative(f"{label}: exposure read-back (ms)", ea.get("readback_ms"), eb.get("readback_ms"), tolerance_exposure)
        table.relative(f"{label}: live fps", ea.get("live", {}).get("fps"), eb.get("live", {}).get("fps"), tolerance_fps)
        ra, rb = ea.get("recording", {}), eb.get("recording", {})
        _recording_rows(table, label, ra, rb, tolerance_fps)

    for key, title in (("roi", "ROI"), ("binning", "binning"), ("roi_binned", "ROI at binning")):
        sa, sb = a.get(key), b.get(key)
        if sa is None and sb is None:
            continue
        if sa is None or sb is None or "error" in sa or "error" in sb:
            table.add(f"{title} round ran in both", None if sa is None else "error" if "error" in sa else "ok",
                      None if sb is None else "error" if "error" in sb else "ok", None, "CHECK")
            continue
        if key == "roi_binned":
            table.equal(f"{title}: requested ROI", sa["requested_roi"], sb["requested_roi"], dims)
            table.equal(f"{title}: requested binning", sa["requested_binning"], sb["requested_binning"])
            table.equal(f"{title}: applied binning", sa.get("applied_binning"), sb.get("applied_binning"))
        else:
            table.equal(f"{title}: requested", sa["requested"], sb["requested"], fmt if key == "binning" else dims)
        if key in ("roi", "roi_binned"):
            table.equal(f"{title}: applied frame start (x, y)", sa.get("applied_frame_start"),
                        sb.get("applied_frame_start"), dims)
        elif key == "binning":
            table.equal("binning: applied", sa.get("applied_binning"), sb.get("applied_binning"))
        table.equal(f"{title}: applied shape (manager, w x h)", sa.get("applied_shape"), sb.get("applied_shape"), dims)
        _recording_rows(table, title, sa.get("recording", {}), sb.get("recording", {}), tolerance_fps, rate=False)

    passed, checks = table.verdicts.count("PASS"), table.verdicts.count("CHECK")
    lines = [
        "# Camera comparison", "",
        f"- A: `{a['setup']['path']}` detector `{a['detector']}` ({a['manager']['class']}), measured {a['created']}",
        f"- B: `{b['setup']['path']}` detector `{b['detector']}` ({b['manager']['class']}), measured {b['created']}",
        f"- tolerances: exposure read-back {tolerance_exposure * 100:g} %, fps {tolerance_fps * 100:g} %; "
        f"relative difference is (B - A) / A", "",
        table.render(("Quantity", "A", "B", "Rel. diff.", "Verdict")), "",
        f"{passed} PASS, {checks} CHECK.", "",
        "## Mean intensity of the recorded frames (no verdict: the same scene is the operator's job)", "",
        "| Exposure | A mean of means | B mean of means | A std of means | B std of means |", "|---|---|---|---|---|",
    ]
    for ms in sorted(set(by_ms_a) | set(by_ms_b)):
        ra = (by_ms_a.get(ms) or {}).get("recording", {})
        rb = (by_ms_b.get(ms) or {}).get("recording", {})
        lines.append(f"| {ms:g} ms | {fmt(ra.get('mean_of_means'))} | {fmt(rb.get('mean_of_means'))} | "
                     f"{fmt(ra.get('std_of_means'))} | {fmt(rb.get('std_of_means'))} |")
    lines += ["", "## Property names that exist in both (matched on letters and digits only)", ""]
    pa, pb = _property_index(a), _property_index(b)
    both = sorted(set(pa) & set(pb))
    if both:
        lines += ["| A name | A value | B name | B value |", "|---|---|---|---|"]
        lines += [f"| {pa[k][0]} | {pa[k][1]} | {pb[k][0]} | {pb[k][1]} |" for k in both]
    else:
        lines.append("None matched.")
    lines += ["", f"{len(set(pa) - set(pb))} names only in A and {len(set(pb) - set(pa))} only in B "
                  f"(all of them are in the JSON reports)."]
    warnings = [f"A: {w}" for w in a.get("warnings", [])] + [f"B: {w}" for w in b.get("warnings", [])] \
        + [f"A failed: {e}" for e in a.get("errors", [])] + [f"B failed: {e}" for e in b.get("errors", [])]
    if warnings:
        lines += ["", "## Warnings and failures from the runs", ""] + [f"- {w}" for w in warnings]
    return "\n".join(lines) + "\n"


def _recording_rows(table: Table, label: str, ra: Dict[str, Any], rb: Dict[str, Any], tolerance_fps: float,
                    rate: bool = True) -> None:
    if "error" in ra or "error" in rb or not ra or not rb:
        table.add(f"{label}: recording ran in both", "error" if "error" in ra else bool(ra),
                  "error" if "error" in rb else bool(rb), None, "CHECK")
        return
    wanted = ra.get("frames_requested")
    exact = ra["frames_written"] == rb["frames_written"] == wanted == rb.get("frames_requested")
    table.add(f"{label}: frames written (requested {wanted})", ra["frames_written"], rb["frames_written"], None,
              "PASS" if exact else "CHECK")
    table.equal(f"{label}: recorded shape (n, h, w)", ra["shape"], rb["shape"], dims)
    table.equal(f"{label}: recorded dtype", ra["dtype"], rb["dtype"])
    if rate:
        table.relative(f"{label}: recording fps (includes start and stop latency)", ra.get("fps"), rb.get("fps"),
                       tolerance_fps)
    la, lb = _lost(ra), _lost(rb)
    known = [x for x in (la, lb) if x is not None]
    table.add(f"{label}: lost frames (bridge counter; native has none)", la, lb, None,
              "n/a" if not known else ("PASS" if all(x == 0 for x in known) else "CHECK"))


def run_diff(args) -> int:
    a, b = load_report(args.a), load_report(args.b)
    text = build_diff(a, b, args.tolerance_fps, args.tolerance_exposure)
    sys.stdout.write(text)
    if args.fail_on_check and "| CHECK |" in text:
        return EXIT_PHASE_FAILED
    return EXIT_OK


# =========================================================================== CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure a camera the same way whichever driver runs it, and diff two reports "
                    "(see the module docstring and docs/design/plans/micromanager-bridge-rig-test.md).",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    m = sub.add_parser("measure", help="boot SETUP headless and measure one detector",
                       formatter_class=argparse.RawDescriptionHelpFormatter,
                       description="Boot the real app on SETUP (the rig's user files stay where they are) and "
                                   "measure --detector. Exit codes: 0 done, 1 a phase failed, 2 bad arguments or "
                                   "boot failure, 3 mock backend (see --allow-mock).")
    m.add_argument("setup", help="setup JSON file (any path; it is read, never written)")
    m.add_argument("--detector", required=True, help="name of the detector in the setup's detectors section")
    m.add_argument("--out", required=True, help="report file to write (JSON)")
    m.add_argument("--exposures", type=parse_exposures, default=parse_exposures(DEFAULT_EXPOSURES),
                   metavar="MS,MS,...",
                   help=f"exposures in milliseconds (default {DEFAULT_EXPOSURES}: 100, 50 and 20 fps limits of a "
                        f"camera that reads out faster than that, so the exposure is what sets the rate)")
    m.add_argument("--frames", type=int, default=DEFAULT_FRAMES,
                   help=f"frames per recording (default {DEFAULT_FRAMES}: 2 s at 10 ms, long against the "
                        f"start and stop latency of a recording, and 1.6 GB of temporary disk at 2048 x 2048 16 bit)")
    m.add_argument("--seconds", type=float, default=DEFAULT_SECONDS,
                   help=f"live-view counting window after the first new frame (default {DEFAULT_SECONDS:g}: "
                        f">= 100 frames at 50 ms, so +-1 frame of polling is <= 1 %%)")
    m.add_argument("--round-frames", type=int, default=DEFAULT_ROUND_FRAMES,
                   help=f"frames per ROI and binning recording (default {DEFAULT_ROUND_FRAMES}: enough to see the "
                        f"geometry and an obvious loss, short enough not to write another full recording)")
    m.add_argument("--roi", type=parse_roi, default=None, metavar="X,Y,W,H",
                   help="also apply this ROI (the Settings panel's x0, y0, width, height), record, restore")
    m.add_argument("--binning", type=int, default=None,
                   help="also apply this binning, record, restore; with --roi, a third round applies the ROI "
                        "at that binning (the Settings panel multiplies its fields by the binning, so this "
                        "is where drivers whose crop() disagrees on binned or unbinned pixels differ)")
    m.add_argument("--allow-mock", action="store_true",
                   help="measure a detector that runs on a mock backend or is not connected (dry runs); "
                        "without it such a run is refused, because a camera that fell back to its mock "
                        "would report numbers that are not the camera's")
    m.add_argument("--restore-saved-state", action="store_true",
                   help="restore the rig's saved widget state at startup (off by default: a run starts from "
                        "the setup file's own values, and the saved state is keyed by detector name)")
    m.add_argument("--record-dir", default=None,
                   help="keep the recordings in this folder (default: a temporary folder, deleted)")
    m.set_defaults(func=run_measure)

    d = sub.add_parser("diff", help="compare two reports", formatter_class=argparse.RawDescriptionHelpFormatter,
                       description="Print a Markdown table of every measured quantity, A against B, with PASS or "
                                   "CHECK. Relative difference is (B - A) / A.")
    d.add_argument("a", help="first report (JSON)")
    d.add_argument("b", help="second report (JSON)")
    d.add_argument("--tolerance-fps", type=float, default=DEFAULT_TOLERANCE_FPS,
                   help=f"relative tolerance of frame rates (default {DEFAULT_TOLERANCE_FPS}: live counting "
                        f"quantises by one frame in the window and a recording's rate carries its start latency; "
                        f"a real driver difference (a different readout mode) is tens of per cent)")
    d.add_argument("--tolerance-exposure", type=float, default=DEFAULT_TOLERANCE_EXPOSURE,
                   help=f"relative tolerance of the exposure read-back (default {DEFAULT_TOLERANCE_EXPOSURE}: a "
                        f"camera rounds the exposure to its line time, so exact equality is not expected, but "
                        f"more than 1 %% would show as a different brightness)")
    d.add_argument("--fail-on-check", action="store_true", help="exit 1 when any row is CHECK")
    d.set_defaults(func=run_diff)
    return parser


def validate_measure(args, parser: argparse.ArgumentParser) -> None:
    if args.frames < 1 or args.round_frames < 1:
        parser.error("--frames and --round-frames must be at least 1")
    if args.seconds <= 0:
        parser.error("--seconds must be positive")
    if args.binning is not None and args.binning < 1:
        parser.error("--binning must be at least 1")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "measure":
        validate_measure(args, parser)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
