#!/usr/bin/env python3
"""Replay ONE complete scan as a growing, multi-timepoint ImSwitch Zarr lapse.

Each scanN repeats the same raw frames. Intended for testing Session Auto + Live
timepoint publication without changing the MoNaLISA reconstruction implementation.
Writes only a new .zarr under the watched folder. Requires numpy + zarr;
h5py is only needed for an HDF5 source.


Usage:
    python fake_zarr_timelapse.py "<source-zarr-file-path>" "<target-folder-path>" --timepoints 5 --duration 5 --between-timepoints 3
"""

import argparse
import asyncio
import json
import math
import re
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import zarr


# Mirror the ImControl Zarr-v3 writer's Windows metadata-write retry.  A live
# reader can temporarily hold zarr.json open during the writer's atomic rename.
if hasattr(zarr.storage, "LocalStore"):
    class RetryingLocalStore(zarr.storage.LocalStore):
        async def set(self, key, value):
            for attempt in range(50):
                try:
                    return await super().set(key, value)
                except PermissionError:
                    if attempt == 49:
                        raise
                    await asyncio.sleep(0.01)


def json_value(value):
    """Normalize HDF5 attributes to the JSON types accepted by Zarr."""
    if isinstance(value, np.ndarray):
        return json_value(value.tolist())
    if isinstance(value, np.generic):
        return json_value(value.item())
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    return value


def copy_attrs(source, target, *, omit=()):
    for key, value in source.attrs.items():
        if key not in omit:
            target.attrs[str(key)] = json_value(value)


def is_array(obj):
    return hasattr(obj, "shape") and hasattr(obj, "dtype")


def discover_data(group, prefix=""):
    """Find ImControl's detector/data arrays, including scanN/detector/data."""
    result = []
    for key in group.keys():
        obj = group[key]
        path = f"{prefix}/{key}" if prefix else str(key)
        if is_array(obj):
            if key == "data" and obj.ndim >= 2:
                result.append((path, obj))
        else:
            result.extend(discover_data(obj, path))
    return result


def natural_key(path):
    return [int(s) if s.isdigit() else s.lower()
            for s in re.split(r"(\d+)", path)]


def copy_metadata_groups(source, target):
    """Preserve detector metadata subgroups, but not auxiliary HDF5 arrays."""
    for key in source.keys():
        child = source[key]
        if is_array(child):
            continue
        dest_child = target.create_group(str(key))
        copy_attrs(child, dest_child)
        copy_metadata_groups(child, dest_child)


def create_live_array(parent, source, frames):
    shape = (0, *source.shape[1:]) if source.ndim > 2 else (0, *source.shape)
    frame_bytes = int(np.dtype(source.dtype).itemsize * np.prod(shape[1:]))
    # Same 4 MiB target and max 32 frame chunk policy as ImControl.
    chunk_depth = max(1, min(32, (4 * 1024 * 1024) // max(1, frame_bytes)))
    chunks = (chunk_depth, *shape[1:])
    options = dict(shape=shape, dtype=source.dtype, chunks=chunks)
    if hasattr(parent, "create_array"):
        try:
            array = parent.create_array(
                "data", **options,
                chunk_key_encoding={"name": "v2", "configuration": {"separator": "."}},
            )
        except (TypeError, ValueError):
            array = parent.create_array("data", **options)
    else:
        array = parent.create_dataset("data", **options)

    copy_attrs(source, array, omit=(
        "writing", "recording:frames_committed", "recording:completion_outcome",
        "recording:actual_frames", "recording:actual_partitions",
        "recording:discarded_frames",
    ))
    array.attrs["recording:source_format"] = "ZARR"
    # The source is a completed recording; this replay writes every source frame.
    array.attrs["recording:planned_frames"] = frames
    array.attrs["recording:expected_frames"] = frames
    array.attrs["recording:planned_partitions"] = 1
    array.attrs["recording:frames_committed"] = 0
    array.attrs["writing"] = True
    return array


def frame_count(source):
    return int(source.shape[0]) if source.ndim > 2 else 1


def choose_source_array(root_in, args):
    """Select exactly one detector scan; do not concatenate other timepoints."""
    candidates = sorted(discover_data(root_in), key=lambda entry: natural_key(entry[0]))
    if not candidates:
        raise ValueError("Source contains no detector/data arrays")
    matches = []
    for path, array in candidates:
        parts = path.split("/")
        if len(parts) >= 3 and re.fullmatch(r"scan\d+", parts[0]):
            if int(parts[0][4:]) != args.source_timepoint:
                continue
            detector = parts[1]
        else:
            if args.source_timepoint != 0:
                continue
            detector = parts[0]
        if args.detector is None or detector == args.detector:
            matches.append((path, detector, array))
    if not matches:
        raise ValueError("No matching source timepoint/detector; check --source-timepoint and --detector")
    if len(matches) != 1:
        raise ValueError("Multiple source detectors found; specify --detector to select one")
    path, detector, array = matches[0]
    if array.ndim != 3:
        raise ValueError(f"Expected a 3D (frames, Y, X) scan, found {array.shape}")
    if frame_count(array) < 2:
        raise ValueError("The source must be a scan containing at least two frames")
    return path, detector, array


def copy_detector_into_scan(root_in, root_out, source_path, detector_name, index, n_timepoints):
    """Create scanN/Detector and preserve detector metadata/NGFF axes."""
    scan_name = f"scan{index}"
    scan_group = root_out[scan_name] if scan_name in root_out else root_out.create_group(scan_name)
    src_parts = source_path.split("/")[:-1]
    source_detector = root_in
    for part in src_parts:
        source_detector = source_detector[part]
    if detector_name in scan_group:
        dest_detector = scan_group[detector_name]
    else:
        dest_detector = scan_group.create_group(detector_name)
        copy_attrs(source_detector, dest_detector)
        copy_metadata_groups(source_detector, dest_detector)
        # copy_metadata_groups skips arrays, including the source 'data'.
    try:
        rec = dest_detector["metadata"]["Rec"]
        # The input is ScanOnce, but the replay is a ScanLapse.  Leaving
        # ScanOnce here makes the incremental adapter report T=1 even though
        # Session Explorer correctly catalogs all scanN groups.
        rec.attrs["Mode"] = "ScanLapse"
        rec.attrs["LapseTime"] = str(n_timepoints)
    except (KeyError, TypeError):
        pass
    return dest_detector


def stamp_lapse_layout(array, index, n_timepoints):
    """Retain the input scan geometry; add this scan's time partition."""
    raw = array.attrs.get("AcquisitionLayout:json")
    if not raw:
        return
    layout = json.loads(raw)
    if not isinstance(layout, dict):
        raise ValueError("AcquisitionLayout:json must contain a JSON object")
    other = [p for p in layout.get("partitions", [])
             if p.get("kind") != "time"]
    other.append({
        "kind": "time", "index": int(index),
        "planned_count": int(n_timepoints), "storage": "one-group-per-item",
    })
    layout["partitions"] = other
    array.attrs["AcquisitionLayout:json"] = json.dumps(layout, separators=(",", ":"))


def publish_root_series(root_out, detector_name, index):
    """Append only existing image groups to the top-level NGFF index."""
    ome = dict(root_out.attrs.get("ome", {}) or {})
    series = list(ome.get("series", []))
    entry = {"path": f"scan{index}/{detector_name}"}
    if entry not in series:
        series.append(entry)
    ome["version"] = "0.5"
    ome["series"] = series
    root_out.attrs["ome"] = ome


def replay_lapse(root_in, root_out, path, detector, source, args):
    n_frames = frame_count(source)
    steps = max(2, int(math.ceil(args.duration / args.interval)))
    batch_size = max(1, int(math.ceil(n_frames / steps)))
    # Keep writer metadata consistent with a real single-file scan lapse.
    # The first scan starts after the startup delay, and each subsequent scan
    # group appears only when its acquisition begins.
    for index in range(args.timepoints):
        parent = copy_detector_into_scan(
            root_in, root_out, path, detector, index, args.timepoints
        )
        destination = create_live_array(parent, source, n_frames)
        destination.attrs["recording:mode"] = "ScanLapse"
        destination.attrs["recording:num_timepoints"] = int(args.timepoints)
        stamp_lapse_layout(destination, index, args.timepoints)
        destination.attrs["recording:lapse_index"] = int(index)
        destination.attrs["recording:single_lapse_file"] = True
        destination.attrs["recording:dataset_path"] = f"/scan{index}/{detector}/data"
        destination.attrs["recording:frames_per_stack"] = int(n_frames)
        destination.attrs["recording:planned_frames"] = int(n_frames)
        destination.attrs["recording:expected_frames"] = int(n_frames)
        publish_root_series(root_out, detector, index)
        print(f"Starting T{index + 1}/{args.timepoints}: "
              f"scan{index}/{detector}/data ({n_frames} frames)", flush=True)
        committed = 0
        while committed < n_frames:
            stop = min(n_frames, committed + batch_size)
            data = np.asarray(source[committed:stop])
            destination.resize((stop, *destination.shape[1:]))
            destination[committed:stop, ...] = data
            # Reader barrier: ALWAYS publish after the corresponding pixels.
            destination.attrs["recording:frames_committed"] = int(stop)
            committed = stop
            print(f"  T{index + 1}: {committed}/{n_frames} frames committed", flush=True)
            if committed < n_frames:
                time.sleep(args.interval)
        destination.attrs["recording:actual_frames"] = n_frames
        destination.attrs["recording:actual_partitions"] = 1
        destination.attrs["recording:discarded_frames"] = 0
        destination.attrs["recording:completion_outcome"] = "complete"
        destination.attrs["writing"] = False
        print(f"  T{index + 1} complete; next timepoint not yet created", flush=True)
        if index < args.timepoints - 1:
            time.sleep(args.between_timepoints)
    print(f"All {args.timepoints} timepoints finalized.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Completed .zarr / .h5 / .hdf5 scan")
    parser.add_argument("watch_folder", type=Path, help="Existing ImProcess Session directory")
    parser.add_argument("--name", default=None, help="Name of NEW .zarr store (must not exist)")
    parser.add_argument("--timepoints", type=int, default=5,
                        help="How many times to repeat the scan (default: 5)")
    parser.add_argument("--duration", type=float, default=10.,
                        help="Approximate replay seconds PER timepoint (default: 10)")
    parser.add_argument("--interval", type=float, default=0.5,
                        help="Delay between frame batches (default: 0.5)")
    parser.add_argument("--between-timepoints", type=float, default=3.,
                        help="Delay between scans (default: 3)")
    parser.add_argument("--startup-delay", type=float, default=5.,
                        help="Pause after creating unreadable store (default: 5)")
    parser.add_argument("--precreate-shell", action="store_true",
                        help="Precreate scan0/detector metadata without the data array")
    parser.add_argument("--detector", default=None,
                        help="Choose a detector if source has more than one")
    parser.add_argument("--source-timepoint", type=int, default=0,
                        help="Select one timepoint if source is already a lapse (default: 0)")
    args = parser.parse_args()
    if args.timepoints < 2 or args.timepoints > 1000:
        parser.error("--timepoints must be between 2 and 1000")
    if (args.interval <= 0 or args.duration <= 0 or
            args.startup_delay < 0 or args.between_timepoints < 0):
        parser.error("duration and interval must be positive; delays may be zero")
    if args.source_timepoint < 0:
        parser.error("--source-timepoint must be nonnegative")
    src_path = args.source.expanduser().resolve()
    watched = args.watch_folder.expanduser().resolve()
    if not src_path.exists():
        parser.error(f"Source does not exist: {src_path}")
    if not watched.is_dir():
        parser.error(f"Watched folder does not exist: {watched}")
    if src_path.suffix.lower() not in (".zarr", ".h5", ".hdf5"):
        parser.error("Source must be .zarr, .h5, or .hdf5")

    if src_path.suffix.lower() == ".zarr":
        source_root = zarr.open_group(str(src_path), mode="r")
        closer = lambda: None
    else:
        import h5py
        source_root = h5py.File(src_path, "r")
        closer = source_root.close

    try:
        try:
            source_path, detector, array = choose_source_array(source_root, args)
        except ValueError as exc:
            parser.error(str(exc))
        name = args.name or f"timelapse_{src_path.stem}_{datetime.now():%Y%m%d_%H%M%S}.zarr"
        if Path(name).name != name or not name.lower().endswith(".zarr"):
            parser.error("--name must be a plain .zarr folder name")
        dest_path = watched / name
        if dest_path.exists():
            parser.error(f"Refusing to overwrite existing recording: {dest_path}")
        # Avoid creating input inside output, or vice versa.
        if dest_path == src_path or dest_path in src_path.parents or src_path in dest_path.parents:
            parser.error("Source and destination must not contain one another")
        store = (RetryingLocalStore(str(dest_path))
                 if hasattr(zarr.storage, "LocalStore")
                 else zarr.storage.DirectoryStore(str(dest_path)))
        output = zarr.group(store=store, overwrite=False)
        copy_attrs(source_root, output, omit=(
            "ome", "recording:completion_outcome", "recording:actual_frames",
            "recording:frames_committed", "writing",
            "recording:single_lapse_file", "recording:num_timepoints",
            "recording:lapse_index",
        ))
        output.attrs["timestamp"] = time.time()
        output.attrs["rec_mode"] = "recording"
        output.attrs["recording:mode"] = "ScanLapse"
        output.attrs["recording:num_timepoints"] = int(args.timepoints)
        output.attrs["recording:single_lapse_file"] = True
        print(f"Created incomplete timelapse {dest_path}", flush=True)
        if args.precreate_shell:
            copy_detector_into_scan(source_root, output, source_path,
                                    detector, 0, args.timepoints)
            print("Precreated scan0/detector metadata; no image array yet", flush=True)
        print(f"Waiting {args.startup_delay:g} s before T1...", flush=True)
        time.sleep(args.startup_delay)
        replay_lapse(source_root, output, source_path, detector, array, args)
        print(f"Finished: {dest_path}", flush=True)
    finally:
        closer()


if __name__ == "__main__":
    main()
