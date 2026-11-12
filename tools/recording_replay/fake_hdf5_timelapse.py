#!/usr/bin/env python3
"""Replay one complete ImControl HDF5 scan as a growing ScanLapse.

Two storage modes mirror ImControl's HDF5Storer:

  --mode single-file  ->  <name>_CAM.hdf5.partial during the entire lapse;
                           /scan0/CAM/{data,frames_committed,stream_complete}
                           /scan1/CAM/{data,...}
                           publish as <name>_CAM.hdf5 when the lapse ends.

  --mode multi-file   ->  <name>_scan0_CAM.hdf5.partial during T0;
                           publish <name>_scan0_CAM.hdf5 after T0;
                           repeat for each subsequent timepoint.

The temporary-file protocol mirrors ImControl HDF5 SaveMode.Disk timelapses:
working filename is <public>.partial, <public>.partial.claim exclusively
reserves the final name, and the same-directory publication happens only after
post-close finalization. The normal Session HDF5 discovery ignores .partial.
Use --direct-write to reproduce the previous growing-.hdf5 access behavior.

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
    python fake_hdf5_timelapse.py "<source-hdf5-file-path>" "<target-folder-path>" --mode single-file --timepoints 5 --stop-after 3
"""

from __future__ import annotations

import argparse
import json
import math
import os
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


def _working_path(public_path: Path) -> Path:
    """ImControl's Hdf5PublicationRegistry.working_name convention."""
    return Path(f"{public_path}.partial")


def _claim_path(public_path: Path) -> Path:
    """ImControl's Hdf5PublicationRegistry.claim_name convention."""
    return Path(f"{public_path}.partial.claim")


def _check_publication_destinations(paths: list[Path]) -> None:
    """Never reuse an old public file, abandoned stage, or reservation."""
    occupied = [str(candidate) for public in paths
                for candidate in (public, _working_path(public), _claim_path(public))
                if os.path.lexists(candidate)]
    if occupied:
        raise FileExistsError(f"Refusing to overwrite existing recording/stage/claim: {occupied}")


def _reserve_working_path(public_path: Path) -> Path:
    """Cross-process reservation, matching ImControl's .partial.claim contract."""
    _check_publication_destinations([public_path])
    claim = _claim_path(public_path)
    fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    return _working_path(public_path)


def _publish_finalized(working: Path, public: Path) -> None:
    """Publish without overwriting, after all writer and metadata handles close.

    Equivalent to Hdf5PublicationRegistry._publish_one; intentionally
    stand-alone so this tool does not import ImControl or Qt.
    """
    if os.path.lexists(public):
        raise FileExistsError(f"HDF5 destination already exists: {public}")
    if not working.is_file():
        raise FileNotFoundError(f"Finalized HDF5 working file missing: {working}")
    if os.name == "nt":
        os.rename(working, public)  # same-directory, no-overwrite on Windows
    else:
        os.link(working, public)  # no-overwrite across POSIX processes
        os.unlink(working)
    try:
        _claim_path(public).unlink()
    except OSError:
        # Publication succeeded. A stale claim is harmless, as in ImControl.
        pass
    print(f"  Published {public.name}", flush=True)


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
    parser.add_argument("--direct-write", action="store_true",
                        help="Legacy mode: write directly to the visible .hdf5 files to reproduce the HDF5 discovery race")
    parser.add_argument("--atomic-publish", action="store_true",
                        help="Compatibility alias for the default staged publication (now using .hdf5.partial in the watched folder)")
    parser.add_argument("--stop-after", type=int, default=None, metavar="N",
                        help="Simulate a deliberate STOP after N completed timepoints; keep the original planned total")
    parser.add_argument("--detector", default=None, help="Choose source detector if multiple present")
    parser.add_argument("--source-timepoint", type=int, default=0,
                        help="Select scanN from an existing lapse (default scan0)")
    args = parser.parse_args(argv)

    if not (2 <= args.timepoints <= 1000):
        parser.error("--timepoints must be between 2 and 1000")
    if args.source_timepoint < 0:
        parser.error("--source-timepoint must be non-negative")
    if args.stop_after is not None and not 1 <= args.stop_after <= args.timepoints:
        parser.error("--stop-after must be between 1 and --timepoints")
    if args.direct_write and args.atomic_publish:
        parser.error("--direct-write and --atomic-publish cannot be combined")
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
        try:
            if args.direct_write:
                existing = [str(p) for p in paths if os.path.lexists(p)]
                if existing:
                    raise FileExistsError(f"Existing output files: {existing}")
            else:
                _check_publication_destinations(paths)
        except FileExistsError as exc:
            parser.error(str(exc))
        if src_path in paths:
            parser.error("Source and destination must not be the same file")
        print(f"Source: {src_path}  [{detector}/data, {source.shape}, {source.dtype}]", flush=True)
        print(f"Mode: {args.mode}; output: {paths[0]}" +
              (f" ... {paths[-1]}" if len(paths) > 1 else ""), flush=True)
        print("Publication: " + ("direct/legacy" if args.direct_write else
              ".hdf5.partial + .claim; publish after finalization"), flush=True)
        print(f"Waiting {args.startup_delay:g} s before creating first output", flush=True)
        time.sleep(args.startup_delay)
        completed = args.stop_after or args.timepoints
        shared_working = None
        try:
            for index in range(completed):
                public = paths[0] if args.mode == "single-file" else paths[index]
                if args.direct_write:
                    working = public
                elif args.mode == "single-file" and shared_working is not None:
                    working = shared_working  # reopen identical staged file for scan1...
                else:
                    working = _reserve_working_path(public)
                    if args.mode == "single-file":
                        shared_working = working
                _replay_one(working, source, detector, index, args.timepoints, args.mode,
                            duration=args.duration, interval=args.interval,
                            between_timepoints=args.between_timepoints)
                if not args.direct_write and args.mode == "multi-file":
                    _publish_finalized(working, public)
                if index + 1 < completed:
                    time.sleep(args.between_timepoints)
            # Shared single-file lapse publishes at the *operation* terminal,
            # including deliberate early STOP, not after each writer terminal.
            if not args.direct_write and args.mode == "single-file":
                _publish_finalized(shared_working, paths[0])
        except BaseException:
            if not args.direct_write:
                print("Replay failed; staged .partial file(s) and .claim(s) "
                      "are retained for inspection/recovery", flush=True)
            raise
        if completed < args.timepoints:
            print(f"Stopped after {completed}/{args.timepoints} completed timepoints; "
                  "existing metadata retains planned count", flush=True)
        else:
            print(f"Finished {completed} completed timepoints", flush=True)
        return paths[:completed] if args.mode == "multi-file" else paths


if __name__ == "__main__":
    main()
