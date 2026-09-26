"""Write time-lapse recordings the way ImControl does, for the lapse tests.

The pixels go through the real production storers -- ``HDF5Storer``,
``ZarrStorer``, ``TiffStorer`` -- so the lifecycle markers, the attribute
placement (dataset attributes, ``metadata/`` groups, the OME MapAnnotation of a
TIFF) and a single-file lapse's ``scanN`` groups are the writer's own, not an
imitation of them. What the storers do not decide is copied from the callers
that do, and says where it comes from:

- file names from ``RecordingController.nextCameraLapse``/``nextLapse``
  (``<savename>_time07`` / ``<savename>_scan07``, padded to the planned count)
  and ``RecordingManager._getFileDests`` (``_<detector>.<ext>`` per detector);
- per-item attributes from
  ``RecordingWorker._augment_attrs_with_recording_metadata``.

Every frame of point ``i`` holds the value ``i + 1``, so a test can tell which
point it is looking at, and a blank plane (0) from any real one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from imswitch.imcommon.model.acquisition_layout import (
    ACQUISITION_LAYOUT_SCHEMA,
    PAYLOAD_DETECTOR_FRAME_STREAM,
    AcquisitionLayout,
    AcquisitionLoop,
    AcquisitionPartition,
    TraversalRule,
    encode_acquisition_layout,
)
from imswitch.imcontrol.model.managers.RecordingManager import (
    HDF5Storer,
    SaveMode,
    TiffStorer,
    ZarrStorer,
)
from imswitch.imcontrol.model.managers.recording_metadata import (
    MODE_SCAN,
    MODE_TIMELAPSE,
    build_ome_image_meta,
)

FRAME_SHAPE = (4, 5)
PIXEL_SIZE_YX_UM = (0.2, 0.1)
T0 = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)

STORERS = {"hdf5": HDF5Storer, "zarr": ZarrStorer, "ome.tiff": TiffStorer}


class _Detector:
    dtype = np.dtype(np.uint16)
    pixelSizeUm = [1.0, *PIXEL_SIZE_YX_UM]


class _Detectors:
    def __getitem__(self, name):
        return _Detector()


def item_path(folder: Path, savename: str, detector: str, fmt: str, *,
              index: int, total: int, token: str | None) -> Path:
    """Where ImControl puts one lapse item; ``token=None`` for a single file."""
    name = savename
    if token is not None:
        name = f"{savename}_{token}{str(index).zfill(len(str(total)))}"
    return Path(folder) / f"{name}_{detector}.{fmt}"


def scan_layout(index: int, total: int, *, kind: str = "time",
                single_file: bool = False, frames: int = 6) -> AcquisitionLayout:
    loops = (AcquisitionLoop("scan_x", "scan_x", frames, step=1.0, unit="um",
                             direction=1),)
    return AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="Camera",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=loops,
        traversal=tuple(TraversalRule(loop.id, "forward") for loop in loops),
        provenance="recorded",
        partitions=(
            AcquisitionPartition(
                kind, index=index, planned_count=total,
                storage="one-group-per-item" if single_file else "one-file-per-item",
            ),
        ),
    )


def record_point(
    folder: Path,
    *,
    fmt: str = "hdf5",
    savename: str = "12h00m00s_rec",
    detector: str = "Camera",
    index: int,
    total: int,
    frames: int = 1,
    written: int | None = None,
    camera: bool = True,
    single_file: bool = False,
    interval_s: float | None = 10.0,
    started_at: datetime | None = None,
    planned_start: datetime | None = None,
    layout_kind: str | None = None,
    frame_shape: tuple[int, int] = FRAME_SHAPE,
    extra_attrs: dict | None = None,
) -> Path:
    """Record lapse point ``index`` of ``total`` and return its path.

    ``written`` below ``frames`` stops the point early, as pressing Stop does;
    ``camera=False`` records a scan lapse point, which writes no interval or
    schedule and carries a scan layout with a ``layout_kind`` lapse partition.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    token = None if single_file else ("time" if camera else "scan")
    path = item_path(folder, savename, detector, fmt,
                     index=index, total=total, token=token)
    written = frames if written is None else written
    started_at = started_at or (T0 + timedelta(seconds=index * (interval_s or 30.0)))

    attrs = {
        "acquisition:software_version": "test",
        "acquisition:start_time": started_at.isoformat(),
        "recording:detector_name": detector,
        "recording:source_format": {"hdf5": "HDF5", "zarr": "ZARR"}.get(fmt, "TIFF"),
        "recording:expected_frames": frames,
        "recording:planned_frames": frames,
        "recording:frames_per_stack": frames,
        "recording:planned_partitions": 1,
        "recording:num_timepoints": total,
        "recording:lapse_index": index,
        "recording:single_lapse_file": bool(single_file),
    }
    if camera:
        if interval_s is not None:
            attrs["recording:lapse_interval_s"] = float(interval_s)
        planned = planned_start or (
            T0 + timedelta(seconds=index * (interval_s or 0.0))
        )
        attrs["recording:planned_start_time"] = planned.isoformat()
    if layout_kind is not None:
        layout = scan_layout(index, total, kind=layout_kind,
                             single_file=single_file, frames=frames)
        attrs["AcquisitionLayout:schema"] = ACQUISITION_LAYOUT_SCHEMA
        attrs["AcquisitionLayout:json"] = encode_acquisition_layout(layout)
    attrs.update(extra_attrs or {})

    storer = STORERS[fmt](str(folder / savename), _Detectors())
    storer.omeMeta = {
        detector: build_ome_image_meta(
            detector,
            MODE_TIMELAPSE if camera else MODE_SCAN,
            frames,
            pixel_size_yx_um=PIXEL_SIZE_YX_UM,
            dtype=np.uint16,
        )
    }
    storer.openStream(
        {detector: str(path)},
        [detector],
        {detector: tuple(frame_shape)},
        {detector: attrs},
        singleMultiDetectorFile=False,
        singleLapseFile=bool(single_file),
        saveMode=SaveMode.Disk,
    )
    if written:
        storer.writeFrames(
            detector,
            np.full((written, *frame_shape), index + 1, dtype=np.uint16),
        )
    storer.finalizeStream({detector: written}, {detector: str(path)}, None,
                          SaveMode.Disk)
    return path


def record_lapse(folder: Path, total: int, *, points=None, **kwargs) -> list[Path]:
    """Record ``points`` (default: all) of a ``total``-point lapse."""
    return [
        record_point(folder, index=index, total=total, **kwargs)
        for index in (range(total) if points is None else points)
    ]
