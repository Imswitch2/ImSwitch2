"""OME recording write -> improcess read round-trip (back-compat sweep).

The write side (RecordingManager storers) and the read side (DataObj) were
standardized on OME independently; each has its own unit tests. This module is
the sweep that chains them: write a recording with the REAL production storer,
then read it back through the REAL DataObj, and assert the pixels, axis labels
and pixel scale survive the boundary — consistently across TIFF, HDF5 and Zarr.

It also pins that pre-OME (legacy) recordings still load with their historical
axis/scale fallback, so standardization did not force a migration.
"""

import h5py
import numpy as np
import pytest

from imswitch.imcontrol.model.managers.RecordingManager import (
    HDF5Storer,
    TiffStorer,
    ZarrStorer,
    SaveMode,
)
from imswitch.imcontrol.model.managers.recording_metadata import (
    MODE_SCAN,
    MODE_SNAP,
    MODE_TIMELAPSE,
    build_ome_image_meta,
)
from imswitch.improcess.model import DataObj


class _StubDetector:
    dtype = np.dtype(np.uint16)
    pixelSizeUm = [1.0, 0.2, 0.1]


class _StubDetectorManager:
    def __getitem__(self, name):
        return _StubDetector()


@pytest.fixture
def detman():
    return _StubDetectorManager()


def _read(name, dataset, path):
    data_obj = DataObj(name, dataset, path=str(path))
    data_obj.checkAndLoadData()
    try:
        return (
            np.asarray(data_obj.data),
            list(data_obj.axis_labels),
            list(data_obj.axis_scales),
            data_obj.scale_unit,
        )
    finally:
        data_obj.checkAndUnloadData()


# --- snap round-trip: all three formats agree on YX-based axes ---------------


def test_snap_roundtrip_tiff(detman, tmp_path):
    storer = TiffStorer(str(tmp_path / "snap"), detman)
    storer.omeMeta = {
        "Cam": build_ome_image_meta(
            "Cam", MODE_SNAP, 1, pixel_size_yx_um=(0.2, 0.1), dtype=np.uint16
        )
    }
    img = np.random.randint(1, 4096, (48, 32), np.uint16)
    storer.snap({"Cam": img})

    data, labels, scales, unit = _read(
        "snap_Cam.ome.tiff", None, tmp_path / "snap_Cam.ome.tiff"
    )
    np.testing.assert_array_equal(data, img)
    assert labels == ["Y", "X"]
    assert scales == [0.2, 0.1]
    assert unit == "um"


def test_snap_roundtrip_hdf5(detman, tmp_path):
    storer = HDF5Storer(str(tmp_path / "snap"), detman)
    storer.omeMeta = {
        "Cam": build_ome_image_meta(
            "Cam", MODE_SNAP, 1, pixel_size_yx_um=(0.2, 0.1), dtype=np.uint16
        )
    }
    img = np.random.randint(1, 4096, (48, 32), np.uint16)
    storer.snap({"Cam": img})

    data, labels, scales, unit = _read("snap_Cam.h5", "Cam", tmp_path / "snap_Cam.h5")
    np.testing.assert_array_equal(data[0], img)
    # A 2D snap is stored (1, Y, X); the padded leading axis is T, not C.
    assert labels == ["T", "Y", "X"]
    assert scales == [1.0, 0.2, 0.1]
    assert unit == "um"


def test_snap_roundtrip_zarr(detman, tmp_path):
    storer = ZarrStorer(str(tmp_path / "snap"), detman)
    storer.omeMeta = {
        "Cam": build_ome_image_meta(
            "Cam", MODE_SNAP, 1, pixel_size_yx_um=(0.2, 0.1), dtype=np.uint16
        )
    }
    img = np.random.randint(1, 4096, (48, 32), np.uint16)
    storer.snap({"Cam": img})

    data, labels, scales, unit = _read("snap.zarr", "Cam", tmp_path / "snap.zarr")
    np.testing.assert_array_equal(data[0], img)
    assert labels == ["T", "Y", "X"]
    assert scales == [1.0, 0.2, 0.1]
    assert unit == "um"


# --- streaming timelapse: the frame axis must read as T across formats -------


def _stream_timelapse(storer, path, frames=32):
    storer.omeMeta = {
        "Cam": build_ome_image_meta(
            "Cam",
            MODE_TIMELAPSE,
            frames,
            pixel_size_yx_um=(0.2, 0.1),
            t_interval_s=0.05,
            dtype=np.uint16,
        )
    }
    storer.openStream(
        {"Cam": path},
        ["Cam"],
        {"Cam": (48, 32)},
        {"Cam": {}},
        singleMultiDetectorFile=False,
        singleLapseFile=False,
        saveMode=SaveMode.Disk,
    )
    for _ in range(frames // 8):
        storer.writeFrames("Cam", np.random.randint(1, 4096, (8, 48, 32), np.uint16))
    storer.finalizeStream({"Cam": frames}, {"Cam": path}, None, SaveMode.Disk)


def test_timelapse_roundtrip_hdf5_labels_frame_axis_t(detman, tmp_path):
    path = str(tmp_path / "tl.h5")
    _stream_timelapse(HDF5Storer(str(tmp_path / "tl"), detman), path)

    data, labels, scales, unit = _read("tl.h5", "Cam", path)
    assert data.shape == (32, 48, 32)
    # Regression: a 32-frame timelapse must not be read as 32 channels.
    assert labels == ["T", "Y", "X"]
    assert scales == [1.0, 0.2, 0.1]
    assert unit == "um"


def test_timelapse_roundtrip_zarr_labels_frame_axis_t(detman, tmp_path):
    path = str(tmp_path / "tl.zarr")
    _stream_timelapse(ZarrStorer(str(tmp_path / "tl"), detman), path)

    data, labels, scales, unit = _read("tl.zarr", "Cam", path)
    assert data.shape == (32, 48, 32)
    assert labels == ["T", "Y", "X"]
    # NGFF carries the frame interval (t_interval_s=0.05) as the T coordinate
    # scale; the spatial axes match the pixel size.
    assert scales == [0.05, 0.2, 0.1]
    assert unit == "um"


def test_all_formats_agree_on_timelapse_axes(detman, tmp_path):
    """OME standardization makes the same logical recording read back with
    identical axis labels and spatial scale regardless of container format.

    The one deliberate asymmetry: NGFF encodes the frame interval as the T
    coordinate scale, while the HDF5 Fiji ``element_size_um`` is spatial-only
    (leading slot is the detector Z pixel), so the T-axis scale differs by
    format. Labels and Y/X scales must match.
    """
    hdf5_path = str(tmp_path / "a.h5")
    zarr_path = str(tmp_path / "a.zarr")
    _stream_timelapse(HDF5Storer(str(tmp_path / "a_h"), detman), hdf5_path)
    _stream_timelapse(ZarrStorer(str(tmp_path / "a_z"), detman), zarr_path)

    _, hdf5_labels, hdf5_scales, hdf5_unit = _read("a.h5", "Cam", hdf5_path)
    _, zarr_labels, zarr_scales, zarr_unit = _read("a.zarr", "Cam", zarr_path)

    assert hdf5_labels == zarr_labels == ["T", "Y", "X"]
    assert hdf5_scales[1:] == zarr_scales[1:] == [0.2, 0.1]  # spatial Y, X agree
    assert hdf5_unit == zarr_unit == "um"


# --- scan (ZYX) round-trip ---------------------------------------------------


def test_scan_roundtrip_hdf5_labels_z(detman, tmp_path):
    path = str(tmp_path / "scan.h5")
    storer = HDF5Storer(str(tmp_path / "scan"), detman)
    storer.omeMeta = {
        "Cam": build_ome_image_meta(
            "Cam",
            MODE_SCAN,
            8,
            pixel_size_yx_um=(0.2, 0.1),
            scan_dims=(1, 1, 8),
            z_step_um=0.5,
            dtype=np.uint16,
        )
    }
    storer.openStream(
        {"Cam": path},
        ["Cam"],
        {"Cam": (48, 32)},
        {"Cam": {}},
        singleMultiDetectorFile=False,
        singleLapseFile=False,
        saveMode=SaveMode.Disk,
    )
    storer.writeFrames("Cam", np.random.randint(1, 4096, (8, 48, 32), np.uint16))
    storer.finalizeStream({"Cam": 8}, {"Cam": path}, None, SaveMode.Disk)

    data, labels, scales, unit = _read("scan.h5", "Cam", path)
    assert data.shape == (8, 48, 32)
    assert labels == ["Z", "Y", "X"]
    assert scales == [0.5, 0.2, 0.1]
    assert unit == "um"


# --- pre-OME (legacy) files still load with historical fallback --------------


def test_legacy_hdf5_without_axes_attr_uses_fallback(tmp_path):
    """A pre-OME ImSwitch HDF5 recording has element_size_um but no axes attr;
    it must still load with the historical default labeling (unchanged)."""
    path = tmp_path / "legacy.h5"
    with h5py.File(path, "w") as handle:
        group = handle.create_group("CAM")
        dataset = group.create_dataset(
            "data", data=np.zeros((5, 48, 32), dtype=np.uint16)
        )
        dataset.attrs["element_size_um"] = [1.0, 0.2, 0.1]

    data, labels, scales, unit = _read("legacy.h5", "CAM", path)
    assert data.shape == (5, 48, 32)
    # Historical behavior: no axes attr -> default_axis_labels padding (C,Y,X).
    assert labels == ["C", "Y", "X"]
    assert scales == [1.0, 0.2, 0.1]
    assert unit == "um"


def test_legacy_flat_zarr_array_still_loads(detman, tmp_path):
    """A bare (non-OME) zarr array named ``data`` remains readable."""
    import zarr

    path = tmp_path / "legacy.zarr"
    root = zarr.group(store=ZarrStorer._make_store(str(path)), overwrite=True)
    payload = np.arange(3 * 4 * 5, dtype=np.uint16).reshape(3, 4, 5)
    ZarrStorer._create_array(root, "data", data=payload, chunks=(1, 4, 5))

    data, labels, _scales, _unit = _read("legacy.zarr", "data", path)
    np.testing.assert_array_equal(data, payload)
    assert labels == ["T", "Z", "C", "Y", "X"][-3:]
