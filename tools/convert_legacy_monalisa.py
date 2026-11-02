#!/usr/bin/env python
"""Convert a legacy MoNaLISA scan lapse (HDF5) into the ImSwitch2 format (Zarr).

Legacy: one file per timepoint, ``<name>_scanNN_<det>.hdf5``, holding a single
dataset ``/<det>`` (frames, Y, X) whose attributes are flat ``Section:key``
metadata (``Rec:``, ``ScanStage:``, ``ScanTTL:``, ``Detector:``, ...).

ImSwitch2: one Zarr store per timepoint, ``<name>_scanNN_<det>.zarr``, holding
``<det>/data`` and ``<det>/metadata/<Section>/``, the ``recording:*`` lapse
attributes, and an ``AcquisitionLayout`` that states the scan geometry. That
is what ImControl writes for a scan lapse without "Single lapse file", so
ImProcess -- offline and live -- reads the result as one 20-timepoint lapse.

    python tools/convert_legacy_monalisa.py map <legacy folder or files>
    python tools/convert_legacy_monalisa.py convert <legacy folder or files> <out_dir> [--fill-missing]

``map`` only reads metadata and prints what each file would become, including
the scan geometry ImSwitch2 will take from it: check that before converting.

``--fill-missing`` writes each timepoint missing from the lapse as a copy of
the nearest earlier one (the nearest later one if there is none earlier). A
copy says so in ``<det>/metadata/acquisition/stand_in``.

The pixels are copied unchanged. The metadata is copied unchanged too, except
``ScanStage:axis_startpos``, which the legacy writer nested ([[0], [0], [0]])
and which is flattened to one value per axis so the geometry can be read.

The files are written by ImControl's own ZarrStorer and the scan geometry is
built by ImProcess's own resolver, so the output follows the format as the
recorder writes it. Both are reached through private helpers
(``ZarrStorer._createDetectorGroup``, ``_adapt_frame_scan``): if this tool
breaks after a refactor, that is where to look.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys
import time

# Bind to the ImSwitch this script ships with, not to whatever is installed
# (see tools/inspect_acquisition_layout.py for why).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.isdir(os.path.join(_REPO_ROOT, "imswitch")):
    sys.path.insert(0, _REPO_ROOT)

import h5py  # noqa: E402
import numpy as np  # noqa: E402
import zarr  # noqa: E402

from imswitch.imcommon.model.acquisition_layout import (  # noqa: E402
    ACQUISITION_LAYOUT_SCHEMA,
    encode_acquisition_layout,
)
from imswitch.imcontrol.controller.controllers._acquisition_layout_source import (  # noqa: E402
    with_time_partition,
)
from imswitch.imcontrol.model.managers.RecordingManager import ZarrStorer  # noqa: E402
from imswitch.improcess.model.acquisition_layout_resolver import (  # noqa: E402
    _adapt_frame_scan,
)

LAPSE_NAME = re.compile(r"_scan(\d+)_")

# Attributes the legacy writer put on the dataset itself rather than as
# metadata; ZarrStorer writes its own versions of these on the data array.
DATASET_ATTRS = ("detector_name", "element_size_um", "writing")


def _native(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def map_legacy(path: str, as_index: int | None = None) -> dict:
    """Everything the ImSwitch2 file will hold, apart from the pixels.

    ``as_index`` writes the timepoint as another one: a stand-in for a
    timepoint missing from the lapse, marked as such in its metadata.
    """
    name = os.path.basename(path)
    match = LAPSE_NAME.search(name)
    if match is None:
        raise ValueError(f"{name}: not a scan-lapse timepoint (no _scanNN_ in the name)")
    lapse_index = int(match.group(1))
    source_name = name
    if as_index is not None:
        width = len(match.group(1))
        name = name[:match.start(1)] + str(as_index).zfill(width) + name[match.end(1):]
        lapse_index = as_index

    with h5py.File(path, "r") as f:
        first = list(f.keys())[0]
        detector = _native(f[first].attrs.get("detector_name")) or first
        dataset = f[detector]
        shape, dtype = tuple(dataset.shape), dataset.dtype
        legacy = {k: _native(v) for k, v in dataset.attrs.items()}

    if legacy.get("Rec:Mode") != "ScanLapse":
        raise ValueError(f"{name}: Rec:Mode is {legacy.get('Rec:Mode')!r}, not 'ScanLapse'")
    num_timepoints = int(legacy["Rec:LapseTime"])
    frames = int(shape[0])

    # The flat "Section:key" attributes, as the recorder collects them.
    metadata = {k: v for k, v in legacy.items() if ":" in k and k not in DATASET_ATTRS}
    metadata["acquisition:converted_from"] = source_name
    if as_index is not None:
        metadata["acquisition:stand_in"] = (
            f"timepoint {as_index} is missing from the legacy lapse; "
            f"this is a copy of {source_name}"
        )
    # The legacy writer nested the start positions, [[0], [0], [0]]. They are
    # one number per axis, as for axis_length and axis_step_size, and the
    # resolver can only read them that way.
    if "ScanStage:axis_startpos" in metadata:
        metadata["ScanStage:axis_startpos"] = (
            np.asarray(metadata["ScanStage:axis_startpos"]).reshape(-1).tolist()
        )

    # The scan geometry, from ScanStage, by the resolver's own frame-scan
    # adapter (the one both legacy adapters use), then scoped to this
    # timepoint of the lapse the way ImControl scopes a lapse item.
    resolved = _adapt_frame_scan(
        metadata, shape=shape, detector=detector, source="monalisa-scan-legacy",
    )
    layout = with_time_partition(
        resolved.layout, index=lapse_index, planned_count=num_timepoints, single_file=False,
    )
    layout_attrs = {
        "AcquisitionLayout:schema": ACQUISITION_LAYOUT_SCHEMA,
        "AcquisitionLayout:json": encode_acquisition_layout(layout),
    }

    recording = {
        "detector_name": detector,
        "dataset_path": f"/{detector}/data",
        "source_format": "ZARR",
        "expected_frames": frames,
        "planned_frames": frames,
        "frames_per_stack": frames,
        "frames_committed": frames,
        "actual_frames": frames,
        "discarded_frames": 0,
        "planned_partitions": 1,
        "actual_partitions": 1,
        "completion_outcome": "complete",
        "num_timepoints": num_timepoints,
        "lapse_index": lapse_index,
        "single_lapse_file": False,
        "lapse_interval_s": float(legacy.get("Rec:LapseFreq") or 0.0),
    }
    return dict(
        name=name, detector=detector, shape=shape, dtype=dtype,
        lapse_index=lapse_index, num_timepoints=num_timepoints,
        element_size_um=legacy.get("element_size_um", [1.0, 1.0, 1.0]),
        metadata=metadata, layout_attrs=layout_attrs, layout=layout,
        resolved=resolved, recording=recording,
    )


class _Detector:
    """The one thing ZarrStorer asks a detector manager for."""

    def __init__(self, pixel_size_um):
        self.pixelSizeUm = pixel_size_um


def convert(path: str, out_dir: str, as_index: int | None = None) -> str:
    """Write one legacy timepoint as an ImSwitch2 Zarr store; returns its path."""
    mapped = map_legacy(path, as_index)
    detector = mapped["detector"]
    out = os.path.join(out_dir, os.path.splitext(mapped["name"])[0] + ".zarr")
    if os.path.exists(out):
        raise FileExistsError(out)

    with h5py.File(path, "r") as f:
        data = f[detector][...]

    storer = ZarrStorer(out, {detector: _Detector(mapped["element_size_um"])})
    root = zarr.open_group(out, mode="w")
    root.attrs["timestamp"] = os.path.getmtime(path)  # when the legacy file was written
    root.attrs["rec_mode"] = "recording"
    dataset = storer._createDetectorGroup(
        root, detector, data.dtype, {**mapped["metadata"], **mapped["layout_attrs"]},
        data=data, writing=False,
    )
    # The recording:* block on the data array, as ZarrStorer's stream path writes it.
    for key, value in mapped["recording"].items():
        dataset.attrs[f"recording:{key}"] = storer._zarr_attr_value(value)
    return out


def _legacy_files(inputs: list[str]) -> list[str]:
    files = []
    for item in inputs:
        if os.path.isdir(item):
            files += glob.glob(os.path.join(item, "*_scan*_*.hdf5"))
            files += glob.glob(os.path.join(item, "*_scan*_*.h5"))
        else:
            files.append(item)
    return sorted(set(files))


def _print_map(path: str) -> None:
    m = map_legacy(path)
    print(f"{os.path.basename(path)}  ->  {os.path.splitext(m['name'])[0]}.zarr")
    print(f"  {m['detector']}/data  {m['shape']} {m['dtype']}")
    sections: dict[str, int] = {}
    for key in m["metadata"]:
        section = key.split(":", 1)[0]
        sections[section] = sections.get(section, 0) + 1
    for section, count in sorted(sections.items()):
        print(f"  {m['detector']}/metadata/{section}/  {count} attrs")
    print(f"  timepoint {m['lapse_index']} of {m['num_timepoints']}, "
          f"{m['recording']['frames_per_stack']} frames")
    resolved = m["resolved"]
    print(f"  scan geometry: confidence {resolved.confidence}, usable {resolved.is_usable}")
    for loop in m["layout"].event_loops:
        print(f"    {loop.kind:<8} count={loop.count:<4} step={loop.step}  "
              f"device={loop.device}  direction={loop.direction:+d}"
              if loop.direction is not None else
              f"    {loop.kind:<8} count={loop.count:<4} step={loop.step}  device={loop.device}")
    for issue in resolved.issues:
        print(f"    [{issue.severity}] {issue.code}: {issue.message}")


def _convert_all(files: list[str], out_dir: str, fill_missing: bool) -> None:
    os.makedirs(out_dir, exist_ok=True)
    present: dict[int, str] = {}
    num_timepoints = None
    for path in files:
        t0 = time.time()
        out = convert(path, out_dir)
        mapped = map_legacy(path)
        present[mapped["lapse_index"]] = path
        num_timepoints = mapped["num_timepoints"]
        print(f"{os.path.basename(path)} -> {os.path.basename(out)}  ({time.time() - t0:.1f} s)",
              flush=True)

    missing = sorted(set(range(num_timepoints or 0)) - set(present))
    if not missing:
        return
    if not fill_missing:
        print(f"Missing from the lapse: timepoint(s) {missing} of {num_timepoints}. "
              f"A live reader waits for them; --fill-missing writes stand-ins.")
        return
    for index in missing:
        earlier = [i for i in present if i < index]
        source = present[max(earlier)] if earlier else present[min(present)]
        out = convert(source, out_dir, as_index=index)
        print(f"{os.path.basename(source)} -> {os.path.basename(out)}  "
              f"(stand-in for missing timepoint {index})", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_map = sub.add_parser("map", help="print what each file would become; writes nothing")
    p_map.add_argument("inputs", nargs="+", help="legacy folder(s) or file(s)")
    p_conv = sub.add_parser("convert", help="write the ImSwitch2 Zarr stores")
    p_conv.add_argument("inputs", nargs="+", help="legacy folder(s) or file(s)")
    p_conv.add_argument("out_dir", help="folder to write the .zarr stores into")
    p_conv.add_argument("--fill-missing", action="store_true",
                        help="write each missing timepoint as a copy of the nearest earlier one")
    args = parser.parse_args(argv)

    files = _legacy_files(args.inputs)
    if not files:
        parser.error("no legacy *_scanNN_*.hdf5 files found")
    if args.command == "map":
        for path in files:
            _print_map(path)
    else:
        _convert_all(files, args.out_dir, args.fill_missing)
    return 0


if __name__ == "__main__":
    sys.exit(main())
