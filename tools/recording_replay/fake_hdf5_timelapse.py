#!/usr/bin/env python3
"""Replay one complete ImControl HDF5 scan as a growing ScanLapse.

Two storage modes mirror ImControl's HDF5Storer:

  --mode single-file  ->  <name>_CAM.hdf5
                           /scan0/CAM/{data,frames_committed,stream_complete}
                           /scan1/CAM/{data,...}
                           (writer closes and reopens the same file between scans)

  --mode multi-file   ->  <name>_scan0_CAM.hdf5  /CAM/{data,...}
                           <name>_scan1_CAM.hdf5  /CAM/{data,...}
                           (each finished file is closed and never changed again)

Each timepoint repeats the source scan's *original raw camera frames*. The
script copies the detector metadata, acquisition layout, and physical pixel
calibration, changing only lapse identity and recording lifecycle fields.
It streams frames to expandable, gzip-compressed HDF5 datasets using the
same committed-frame and SWMR completion markers as the ImControl writer.
On completion it closes the SWMR writer, reopens r+ to mark writing=False,
and writes the post-close recording:completion_outcome='complete' attribute.


Usage:
    python fake_hdf5_timelapse.py "<source-hdf5-file-path>" "<target-folder-path>" --mode multi-file --timepoints 5 --duration 5
    python fake_hdf5_timelapse.py "<source-hdf5-file-path>" "<target-folder-path>" --mode single-file --timepoints 5 --duration 5
"""

from __future__ import annotations

import argparse
import json
import math
import re
import time
from datetime import datetime
from pathlib import Path

import h5py
import numpy as np


TARGET_CHUNK_BYTES = 4 * 1024 * 1024  # ImControl RecordingManager
MAX_CHUNK_FRAMES = 32                 # ImControl RecordingManager


# These fields describe the *source* recording, not the simulated one.
# They must be regenerated per timepoint, never blindly copied from the input.
RUNTIME_DATA_ATTRS = frozenset({
    "writing", "recording:actual_frames", "recording:actual_partitions",
    "recording:completion_outcome", "recording:discarded_frames",
    "recording:frames_committed", "recording:expected_frames",
    "recording:planned_frames", "recording:planned_partitions",
    "recording:frames_per_stack", "recording:num_timepoints",
    "recording:lapse_index", "recording:single_lapse_file",
    "recording:dataset_path", "recording:source_format",
})


def _str(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _array_paths(group: h5py.Group, prefix: str = ""):
    """Look up structured ImControl detector data, including scanN groups."""
    for key, obj in group.items():
        path = f"{prefix}/{key}" if prefix else key
        if isinstance(obj, h5py.Dataset):
            if key == "data" and obj.ndim >= 2:
                yield path, obj
        elif isinstance(obj, h5py.Group):
            yield from _array_paths(obj, path)


def _choose_scan(root: h5py.File, detector: str | None, source_timepoint: int):
    matches = []
    for name, ds in _array_paths(root):
        components = name.split("/")
        if len(components) >= 3 and re.fullmatch(r"scan\d+", components[0]):
            if int(components[0][4:]) != source_timepoint:
                continue
            det_name = components[1]
        else:
            if source_timepoint != 0:
                continue
            det_name = components[0]
        if detector is None or det_name == detector:
            matches.append((name, det_name, ds))
    if len(matches) != 1:
        choices = [name for name, _ in _array_paths(root)]
        raise ValueError(
            f"Expected exactly one scan matching detector={detector!r} and "
            f"source-timepoint={source_timepoint}; found {len(matches)}. "
            f"Available data: {choices}. Supply --detector when needed."
        )
    name, det_name, ds = matches[0]
    if ds.ndim != 3 or ds.shape[0] < 2:
        raise ValueError(f"Expected a complete (frames, Y, X) scan with at least 2 frames; got {name} {ds.shape}")
    if _str(ds.attrs.get("writing", False)).lower() in ("true", "1"):
        raise ValueError("Input scan is marked writing=True; provide a completed acquisition")
    return name, det_name, ds


def _chunk_depth(shape, dtype):
    frame_bytes = np.dtype(dtype).itemsize * math.prod(shape)
    return max(1, min(MAX_CHUNK_FRAMES, TARGET_CHUNK_BYTES // max(1, frame_bytes)))


def _copy_attrs(src, dst, skip=()):
    for key, value in src.attrs.items():
        if key not in skip:
            dst.attrs[key] = value


def _lapse_layout(encoded, index: int, count: int, mode: str):
    if not encoded:
        return None
    data = json.loads(_str(encoded))
    if not isinstance(data, dict):
        raise ValueError("Source AcquisitionLayout:json does not contain a JSON object")
    # Preserve the original scan axis counts, traversal, spacing and geometry.
    partitions = [entry for entry in data.get("partitions", []) if entry.get("kind") != "time"]
    partitions.append({
        "kind": "time", "index": index, "planned_count": count,
        "storage": "one-group-per-item" if mode == "single-file" else "one-file-per-item",
    })
    data["partitions"] = partitions
    return json.dumps(data, separators=(",", ":"))


def _create_timepoint(file: h5py.File, src_ds: h5py.Dataset, detector: str,
                      index: int, count: int, mode: str, interval_seconds: float):
    """Create all objects BEFORE enabling SWMR (as ImControl does)."""
    root = file.require_group(f"scan{index}") if mode == "single-file" else file
    if detector in root:
        raise ValueError(f"Refusing to overwrite existing {root.name}/{detector}")
    target_group = root.create_group(detector)
    src_group = src_ds.parent
    if "metadata" in src_group:
        src_group.copy("metadata", target_group, name="metadata")
        rec = target_group.get("metadata/Rec")
        if isinstance(rec, h5py.Group):
            rec.attrs["Mode"] = "ScanLapse"
            rec.attrs["LapseTime"] = str(count)
    # ImControl embeds OME XML and sets final axis attrs during post-close
    # finalization. We defer copying source ome_xml until then too.
    n, ny, nx = map(int, src_ds.shape)
    ds = target_group.create_dataset(
        "data", shape=(0, ny, nx), maxshape=(None, ny, nx), dtype=src_ds.dtype,
        chunks=(_chunk_depth((ny, nx), src_ds.dtype), ny, nx),
        compression="gzip", shuffle=True,
    )
    _copy_attrs(src_ds, ds, skip=RUNTIME_DATA_ATTRS)
    layout = _lapse_layout(ds.attrs.get("AcquisitionLayout:json"), index, count, mode)
    if layout is not None:
        ds.attrs["AcquisitionLayout:json"] = layout
    ds.attrs["detector_name"] = detector
    ds.attrs["writing"] = True
    ds.attrs["recording:source_format"] = "HDF5"
    ds.attrs["recording:detector_name"] = detector
    ds.attrs["recording:num_timepoints"] = count
    ds.attrs["recording:lapse_index"] = index
    ds.attrs["recording:single_lapse_file"] = mode == "single-file"
    ds.attrs["recording:dataset_path"] = ds.name
    ds.attrs["recording:frames_per_stack"] = n
    ds.attrs["recording:planned_frames"] = n
    ds.attrs["recording:expected_frames"] = n
    ds.attrs["recording:planned_partitions"] = 1
    ds.attrs["recording:actual_frames"] = 0
    ds.attrs["recording:actual_partitions"] = 0
    ds.attrs["recording:discarded_frames"] = 0
    ds.attrs["recording:lapse_interval_s"] = interval_seconds
    # Create the placeholder before SWMR, like HDF5Storer._createDetectorGroup.
    ds.attrs.create("recording:completion_outcome", np.bytes_(b""), dtype="S32")
    committed = target_group.create_dataset("frames_committed", shape=(1,), dtype=np.int64)
    complete = target_group.create_dataset("stream_complete", shape=(1,), dtype=np.uint8)
    committed[0] = 0
    complete[0] = 0
    return ds, committed, complete


def _post_close_finalize(path: Path, dataset_path: str, source_group: h5py.Group):
    """Only publish post-close metadata after all writer handles are released."""
    # ImControl reopens r+ after closing the SWMR writer for these attrs/OME.
    with h5py.File(path, "r+", libver="latest") as file:
        ds = file[dataset_path]
        n = int(ds.shape[0])
        ds.attrs.modify("writing", False)
        ds.attrs.modify("recording:actual_frames", n)
        ds.attrs.modify("recording:actual_partitions", 1)
        ds.attrs.modify("recording:discarded_frames", 0)
        ds.attrs.modify("recording:completion_outcome", np.bytes_(b"complete"))
        group = ds.parent
        if "ome_xml" in source_group.attrs:
            group.attrs["ome_xml"] = source_group.attrs["ome_xml"]
        if "axes" in source_group["data"].attrs:
            ds.attrs["axes"] = source_group["data"].attrs["axes"]
        file.flush()


def _replay_one(path: Path, src_ds: h5py.Dataset, detector: str, index: int,
                count: int, mode: str, *, duration: float, interval: float,
                between_timepoints: float):
    n = int(src_ds.shape[0])
    # Divide the original frames into enough batches to simulate the requested
    # duration. All raw pixels are identical to the source in every timepoint.
    n_steps = max(2, math.ceil(duration / interval))
    batch_size = max(1, math.ceil(n / n_steps))
    file_mode = "x" if mode == "multi-file" or index == 0 else "a"
    with h5py.File(path, file_mode, libver="latest") as file:
        ds, committed, complete = _create_timepoint(
            file, src_ds, detector, index, count, mode, duration + between_timepoints,
        )
        dataset_path = ds.name
        # Actual ImControl SWMR writer enables this after all datasets and
        # metadata exist. Never create new groups while SWMR is enabled.
        file.flush()
        file.swmr_mode = True
        print(f"T{index}/{count - 1}: {path.name} {ds.name}: 0/{n} frames", flush=True)
        cursor = 0
        while cursor < n:
            stop = min(n, cursor + batch_size)
            pixels = np.asarray(src_ds[cursor:stop, :, :])
            ds.resize(stop, axis=0)
            ds[cursor:stop, ...] = pixels
            # The ImControl committed barrier is set only AFTER the pixels
            # have been flushed, and is itself flushed for readers.
            file.flush()
            committed[0] = stop
            file.flush()
            cursor = stop
            print(f"  T{index}: {cursor}/{n} frames committed", flush=True)
            if cursor < n:
                time.sleep(interval)
        committed[0] = n
        complete[0] = 1
        file.flush()
    # Important: ImControl's writing=False and completion_outcome happen on
    # r+ reopen AFTER the SWMR writer closes, not when stream_complete flips.
    _post_close_finalize(path, dataset_path, src_ds.parent)
    print(f"  T{index}: sealed and closed ({path.name})", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path, help="Completed HDF5 scan to repeat")
    parser.add_argument("watch_folder", type=Path, help="Existing ImProcess Session root")
    parser.add_argument("--mode", choices=("single-file", "multi-file"), required=True,
                        help="One shared scanN file or one sealed file per timepoint")
    parser.add_argument("--name", default=None, help="Output recording base name, without extension")
    parser.add_argument("--timepoints", type=int, default=5, help="Number of repeats (default 5)")
    parser.add_argument("--duration", type=float, default=10.0, help="Approximate seconds per T (default 10)")
    parser.add_argument("--interval", type=float, default=0.5, help="Seconds between frame batches")
    parser.add_argument("--between-timepoints", type=float, default=3.0,
                        help="Delay between timepoints (default 3 seconds)")
    parser.add_argument("--startup-delay", type=float, default=5.0,
                        help="Delay before creating the first output file (default 5 seconds)")
    parser.add_argument("--detector", default=None, help="Choose source detector if multiple present")
    parser.add_argument("--source-timepoint", type=int, default=0,
                        help="Select scanN from an existing lapse (default scan0)")
    args = parser.parse_args(argv)

    if not (2 <= args.timepoints <= 1000):
        parser.error("--timepoints must be between 2 and 1000")
    if args.source_timepoint < 0:
        parser.error("--source-timepoint must be non-negative")
    if args.duration <= 0 or args.interval <= 0:
        parser.error("--duration and --interval must be positive")
    if args.between_timepoints < 0 or args.startup_delay < 0:
        parser.error("--between-timepoints and --startup-delay cannot be negative")
    src_path = args.source.expanduser().resolve()
    folder = args.watch_folder.expanduser().resolve()
    if src_path.suffix.lower() not in (".hdf5", ".h5") or not src_path.is_file():
        parser.error("Source must be an existing .hdf5 or .h5 recording")
    if not folder.is_dir():
        parser.error("watch_folder must already exist")

    with h5py.File(src_path, "r") as source_file:
        try:
            _, detector, source = _choose_scan(source_file, args.detector, args.source_timepoint)
        except ValueError as exc:
            parser.error(str(exc))
        name = args.name or f"fake_hdf5_lapse_{datetime.now():%Y%m%d_%H%M%S}"
        if name != Path(name).name or name in (".", "..") or name.lower().endswith((".hdf5", ".h5")):
            parser.error("--name must be a plain recording base name without extension or directories")
        index_width = len(str(args.timepoints))  # ImControl/ImProcess index convention
        paths = ([folder / f"{name}_{detector}.hdf5"]
                 if args.mode == "single-file" else
                 [folder / f"{name}_scan{index:0{index_width}d}_{detector}.hdf5"
                  for index in range(args.timepoints)])
        existing = [p for p in paths if p.exists()]
        if existing:
            parser.error(f"Refusing to overwrite existing file(s): {existing}")
        if src_path in paths:
            parser.error("Source and destination must not be the same file")
        print(f"Source: {src_path}  [{detector}/data, {source.shape}, {source.dtype}]", flush=True)
        print(f"Mode: {args.mode}; output: {paths[0]}" +
              (f" ... {paths[-1]}" if len(paths) > 1 else ""), flush=True)
        print(f"Waiting {args.startup_delay:g} s before creating first output", flush=True)
        time.sleep(args.startup_delay)
        for index in range(args.timepoints):
            output = paths[0] if args.mode == "single-file" else paths[index]
            _replay_one(output, source, detector, index, args.timepoints, args.mode,
                        duration=args.duration, interval=args.interval,
                        between_timepoints=args.between_timepoints)
            if index + 1 < args.timepoints:
                time.sleep(args.between_timepoints)
        print(f"Finished {args.timepoints} completed timepoints", flush=True)
        return paths


if __name__ == "__main__":
    main()
