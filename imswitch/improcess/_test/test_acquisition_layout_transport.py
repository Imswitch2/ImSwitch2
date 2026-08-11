from __future__ import annotations

from contextlib import contextmanager

import h5py
import numpy as np
import pytest
import tifffile
import zarr

from imswitch.imcommon.model.acquisition_layout import (
    ACQUISITION_LAYOUT_SCHEMA,
    PAYLOAD_DETECTOR_FRAME_STREAM,
    AcquisitionLayout,
    AcquisitionLoop,
    decode_acquisition_layout,
    encode_acquisition_layout,
)
from imswitch.imcommon.model.acquisition_metadata import (
    RecordingLifecycleMarkers,
    normalize_recording_lifecycle,
)
from imswitch.imcontrol.model.managers.RecordingManager import (
    HDF5Storer,
    RecordingWorker,
    SaveFormat,
    SaveMode,
    TiffStorer,
    ZarrStorer,
)
from imswitch.imcontrol.model.managers.recording_metadata import (
    MODE_TIMELAPSE,
    build_ome_image_meta,
)
from imswitch.improcess.live.sources import (
    Hdf5LiveSource,
    InMemoryStackWrapper,
    ZarrLiveSource,
)
from imswitch.improcess.model.image_sources import dataset_names, resolve_image


class _StubDetector:
    dtype = np.dtype(np.uint16)
    pixelSizeUm = [1.0, 0.2, 0.1]
    isScanDriven = False


class _StubDetectorManager:
    def __getitem__(self, name):
        return _StubDetector()


@pytest.fixture
def detman():
    return _StubDetectorManager()


@pytest.fixture
def layout() -> AcquisitionLayout:
    return AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="Cam",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=(
            AcquisitionLoop(
                "condition",
                "condition",
                2,
                labels=("A", "B"),
            ),
            AcquisitionLoop("scan_x", "scan_x", 3),
        ),
        modality="synthetic-transport-test",
        scan_source="test",
    )


def _attrs(layout: AcquisitionLayout, planned_frames: int = 6):
    return {
        "Cam": {
            "AcquisitionLayout:schema": ACQUISITION_LAYOUT_SCHEMA,
            "AcquisitionLayout:json": encode_acquisition_layout(layout),
            "recording:planned_frames": planned_frames,
            "recording:planned_partitions": 1,
            "ScanStage:axis_length": [3, 2],
        }
    }


def _storer(kind: str, path: str, detman):
    classes = {
        "hdf5": HDF5Storer,
        "zarr": ZarrStorer,
        "tiff": TiffStorer,
    }
    storer = classes[kind](path, detman)
    storer.omeMeta = {
        "Cam": build_ome_image_meta(
            "Cam",
            MODE_TIMELAPSE,
            6,
            pixel_size_yx_um=(0.2, 0.1),
            dtype=np.uint16,
        )
    }
    return storer


def _write_stream(
    kind: str,
    path: str,
    detman,
    layout: AcquisitionLayout,
    *,
    actual_frames: int = 6,
    planned_frames: int = 6,
):
    storer = _storer(kind, path, detman)
    storer.openStream(
        {"Cam": path},
        ["Cam"],
        {"Cam": (2, 3)},
        _attrs(layout, planned_frames),
        singleMultiDetectorFile=False,
        singleLapseFile=False,
        saveMode=SaveMode.Disk,
    )
    frames = np.arange(actual_frames * 6, dtype=np.uint16).reshape(actual_frames, 2, 3)
    storer.writeFrames("Cam", frames)
    storer.finalizeStream(
        {"Cam": actual_frames},
        {"Cam": path},
        None,
        SaveMode.Disk,
    )


@contextmanager
def _resolved(kind: str, path: str):
    if kind == "hdf5":
        container = h5py.File(path, "r")
    elif kind == "zarr":
        container = zarr.open(path, mode="r")
    else:
        container = tifffile.TiffFile(path)
    try:
        names = dataset_names(container)
        yield resolve_image(container, names[0])
    finally:
        close = getattr(container, "close", None)
        if close is not None:
            close()


@pytest.mark.parametrize(
    "kind, filename",
    [
        ("hdf5", "layout.h5"),
        ("zarr", "layout.zarr"),
        ("tiff", "layout.ome.tiff"),
    ],
)
def test_canonical_layout_and_complete_lifecycle_round_trip(
    kind, filename, detman, layout, tmp_path
):
    path = str(tmp_path / filename)
    _write_stream(kind, path, detman, layout)

    with _resolved(kind, path) as image:
        assert image.acquisition_layout == layout
        assert image.attrs["AcquisitionLayout:json"] == encode_acquisition_layout(layout)
        assert image.recording_lifecycle is not None
        assert image.recording_lifecycle.writer_state == "finalized"
        assert image.recording_lifecycle.completion_outcome == "complete"
        assert image.recording_lifecycle.planned_frames == 6
        assert image.recording_lifecycle.actual_frames == 6
        assert not image.recording_lifecycle.issues


@pytest.mark.parametrize(
    "kind, filename",
    [
        ("hdf5", "early.h5"),
        ("zarr", "early.zarr"),
        ("tiff", "early.ome.tiff"),
    ],
)
def test_graceful_short_stream_round_trips_as_stopped_early(
    kind, filename, detman, layout, tmp_path
):
    path = str(tmp_path / filename)
    _write_stream(
        kind,
        path,
        detman,
        layout,
        actual_frames=3,
        planned_frames=6,
    )

    with _resolved(kind, path) as image:
        lifecycle = image.recording_lifecycle
        assert lifecycle is not None
        assert lifecycle.writer_state == "finalized"
        assert lifecycle.completion_outcome == "stopped_early"
        assert lifecycle.actual_frames == 3
        assert lifecycle.planned_frames == 6
        assert not lifecycle.issues


def test_offline_and_live_zarr_read_the_same_detector_metadata(detman, layout, tmp_path):
    path = str(tmp_path / "parity.zarr")
    _write_stream("zarr", path, detman, layout)

    with _resolved("zarr", path) as offline:
        source = ZarrLiveSource(detector_name="Cam")
        live = source.open(path)
        try:
            for key in (
                "AcquisitionLayout:schema",
                "AcquisitionLayout:json",
                "ScanStage:axis_length",
                "recording:completion_outcome",
                "recording:actual_frames",
            ):
                if isinstance(offline.attrs[key], np.ndarray):
                    np.testing.assert_array_equal(live.attrs[key], offline.attrs[key])
                else:
                    assert live.attrs[key] == offline.attrs[key]
            assert decode_acquisition_layout(live.attrs["AcquisitionLayout:json"]) == layout
            assert live.acquisition_layout is not None
            assert live.acquisition_layout.layout == layout
        finally:
            source.close()


def test_batch_ome_zarr_round_trips_layout_and_lifecycle(detman, layout, tmp_path):
    base_path = str(tmp_path / "batch")
    frames = np.arange(36, dtype=np.uint16).reshape(6, 2, 3)
    storer = _storer("zarr", base_path, detman)

    storer.snap({"Cam": frames}, _attrs(layout))

    with _resolved("zarr", f"{base_path}.zarr") as image:
        assert image.acquisition_layout == layout
        assert image.recording_lifecycle is not None
        assert image.recording_lifecycle.writer_state == "finalized"
        assert image.recording_lifecycle.completion_outcome == "complete"
        assert image.recording_lifecycle.actual_frames == 6
        np.testing.assert_array_equal(image.attrs["ScanStage:axis_length"], [3, 2])


def test_live_hdf5_uses_side_datasets_for_finalization(detman, layout, tmp_path):
    path = str(tmp_path / "markers.h5")
    _write_stream("hdf5", path, detman, layout)

    with h5py.File(path, "r+") as handle:
        # Simulate the SWMR-compatible stale attr: only the sibling dataset is
        # authoritative for finalization.
        handle["Cam/data"].attrs["writing"] = True

    with _resolved("hdf5", path) as image:
        assert image.recording_lifecycle is not None
        assert image.recording_lifecycle.writer_state == "finalized"
        attrs_only = normalize_recording_lifecycle(
            image.attrs,
            RecordingLifecycleMarkers(writing=True, frames_committed=6),
        )
        assert attrs_only.writer_state == "unknown"

    source = Hdf5LiveSource(detector_name="Cam")
    info = source.open(path)
    try:
        assert decode_acquisition_layout(info.attrs["AcquisitionLayout:json"]) == layout
        assert info.acquisition_layout is not None
        assert info.acquisition_layout.layout == layout
        source.poll()
        assert source.is_complete()
    finally:
        source.close()


def test_in_memory_wrapper_honors_layout_axes(layout):
    wrapper = InMemoryStackWrapper(
        "memory",
        "Cam",
        np.zeros((6, 2, 3), dtype=np.uint16),
        {
            "AcquisitionLayout:schema": ACQUISITION_LAYOUT_SCHEMA,
            "AcquisitionLayout:json": encode_acquisition_layout(layout),
            "writing": False,
            "recording:completion_outcome": "complete",
            "recording:planned_frames": 6,
            "recording:actual_frames": 6,
        },
    )

    assert wrapper.axis_labels == ["frame", "detector_y", "detector_x"]
    assert wrapper.acquisition_layout == layout
    assert wrapper.recording_lifecycle.writer_state == "finalized"


def test_in_memory_wrapper_rejects_disagreeing_explicit_schema(layout):
    with pytest.raises(ValueError, match="schema disagrees"):
        InMemoryStackWrapper(
            "memory",
            "Cam",
            np.zeros((6, 2, 3), dtype=np.uint16),
            {
                "AcquisitionLayout:schema": "imswitch.acquisition-layout/999",
                "AcquisitionLayout:json": encode_acquisition_layout(layout),
            },
        )


def test_recording_worker_accepts_synthetic_layout_without_count_crosscheck(detman, layout):
    class _Manager:
        detectorsManager = detman

    worker = RecordingWorker(_Manager())
    worker.saveFormat = SaveFormat.HDF5
    worker.recLapseTotal = 1
    worker.recLapseIndex = 0
    worker.singleLapseFile = False
    worker.acquisitionLayouts = {"Cam": layout}

    augmented = worker._augment_attrs_with_recording_metadata(
        {"Cam": {}},
        # Deliberately differs from the layout's six events. PR 2 transports;
        # the producer validation gate belongs to PR 4.
        {"Cam": 5},
    )

    assert decode_acquisition_layout(augmented["Cam"]["AcquisitionLayout:json"]) == layout
    assert augmented["Cam"]["recording:planned_frames"] == 5
