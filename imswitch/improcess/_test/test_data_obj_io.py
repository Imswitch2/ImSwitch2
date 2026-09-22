import h5py
import numpy as np
import tifffile as tiff
import zarr

from imswitch.improcess.model import DataObj
from imswitch.improcess.reconstructors.view_only.reconstructor import ViewOnlyReconstructor
# Reuse the production zarr helpers so these tests work on both zarr v2
# (DirectoryStore / create_dataset) and zarr v3 (LocalStore / create_array).
from imswitch.imcontrol.model.managers.RecordingManager import ZarrStorer


def test_data_obj_reads_legacy_hdf5_dataset(tmp_path) -> None:
    path = tmp_path / "legacy.h5"
    data = np.arange(2 * 3 * 4, dtype=np.uint16).reshape(2, 3, 4)

    with h5py.File(path, "w") as file:
        dataset = file.create_dataset("CAM", data=data)
        dataset.attrs["writing"] = False
        dataset.attrs["detector_name"] = "CAM"

    assert DataObj.getDatasetNames(str(path)) == ["CAM"]

    data_obj = DataObj("legacy.h5", "CAM", path=str(path))
    data_obj.checkAndLoadData()

    np.testing.assert_array_equal(data_obj.data, data)
    assert data_obj.attrs["detector_name"] == "CAM"
    assert data_obj.attrs["writing"] is False or data_obj.attrs["writing"] == np.bool_(False)

    data_obj.checkAndUnloadData()


def test_data_obj_hdf5_lazy_handle_does_not_materialize(tmp_path) -> None:
    path = tmp_path / "lazy.h5"
    data = np.arange(3 * 4 * 5, dtype=np.uint16).reshape(3, 4, 5)

    with h5py.File(path, "w") as file:
        dataset = file.create_dataset("CAM", data=data, chunks=(1, 4, 5))
        dataset.attrs["detector_name"] = "CAM"

    data_obj = DataObj("lazy.h5", "CAM", path=str(path))

    assert not data_obj.sourceLoaded
    assert not data_obj.dataLoaded
    assert data_obj.attrs["detector_name"] == "CAM"
    assert data_obj.sourceLoaded
    assert not data_obj.dataMaterialized
    assert data_obj.numFrames == 3
    assert data_obj.sourceLoaded
    assert not data_obj.dataMaterialized

    handle = data_obj.data_handle
    assert handle.shape == data.shape
    assert handle.dtype == data.dtype
    assert handle.chunks == (1, 4, 5)
    np.testing.assert_array_equal(handle[1], data[1])
    assert not data_obj.dataMaterialized

    np.testing.assert_array_equal(data_obj.data, data)
    assert data_obj.dataLoaded
    assert data_obj.dataMaterialized

    data_obj.checkAndUnloadData()
    assert not data_obj.sourceLoaded
    assert not data_obj.dataLoaded


def test_view_only_preserves_virtual_hdf5_handle(tmp_path) -> None:
    path = tmp_path / "view-only-virtual.h5"
    data = np.arange(3 * 4 * 5, dtype=np.uint16).reshape(3, 4, 5)

    with h5py.File(path, "w") as file:
        file.create_dataset("CAM", data=data, chunks=(1, 4, 5))

    data_obj = DataObj("view-only-virtual.h5", "CAM", path=str(path))
    data_obj.checkAndOpenData()

    result = ViewOnlyReconstructor().process(data_obj, {})

    assert result.data is data_obj.data_handle
    assert result.data.backend == "hdf5"
    assert data_obj.sourceLoaded
    assert not data_obj.dataMaterialized
    np.testing.assert_array_equal(result.data[1], data[1])
    assert not data_obj.dataMaterialized

    data_obj.checkAndUnloadData()


def test_data_obj_mean_uses_lazy_handle_without_materializing(tmp_path) -> None:
    path = tmp_path / "lazy-mean.h5"
    data = np.arange(4 * 3 * 2, dtype=np.uint16).reshape(4, 3, 2)

    with h5py.File(path, "w") as file:
        file.create_dataset("CAM", data=data, chunks=(1, 3, 2))

    data_obj = DataObj("lazy-mean.h5", "CAM", path=str(path))

    mean = data_obj.getMeanData()

    np.testing.assert_allclose(mean, np.mean(data, axis=0).astype(np.float32))
    assert data_obj.sourceLoaded
    assert not data_obj.dataMaterialized
    assert not data_obj.dataLoaded

    data_obj.checkAndUnloadData()


def test_data_obj_accepts_legacy_path_as_name_constructor(tmp_path) -> None:
    path = tmp_path / "path_as_name.h5"
    data = np.arange(2 * 3 * 4, dtype=np.uint16).reshape(2, 3, 4)

    with h5py.File(path, "w") as file:
        file.create_dataset("data", data=data)

    data_obj = DataObj(str(path), "display_name")
    data_obj.checkAndLoadData()

    np.testing.assert_array_equal(data_obj.data, data)
    assert data_obj.datasetName == "data"

    data_obj.checkAndUnloadData()


def test_data_obj_reads_structured_hdf5_detector_group(tmp_path) -> None:
    path = tmp_path / "structured.h5"
    data = np.arange(2 * 3 * 4, dtype=np.uint16).reshape(2, 3, 4)

    with h5py.File(path, "w") as file:
        file.attrs["rec_mode"] = "recording"
        det_group = file.create_group("CAM")
        dataset = det_group.create_dataset("data", data=data)
        dataset.attrs["detector_name"] = "CAM"
        dataset.attrs["writing"] = False
        metadata = det_group.create_group("metadata")
        detector = metadata.create_group("detector")
        detector.attrs["exposure"] = 0.01
        metadata.attrs["uncategorized"] = "value"

    assert DataObj.getDatasetNames(str(path)) == ["CAM"]

    data_obj = DataObj("structured.h5", "CAM", path=str(path))
    data_obj.checkAndLoadData()

    np.testing.assert_array_equal(data_obj.data, data)
    assert data_obj.attrs["rec_mode"] == "recording"
    assert data_obj.attrs["detector_name"] == "CAM"
    assert data_obj.attrs["detector:exposure"] == 0.01
    assert data_obj.attrs["uncategorized"] == "value"

    data_obj.checkAndUnloadData()


def test_data_obj_reads_ome_tiff_series_axes_and_scale(tmp_path) -> None:
    path = tmp_path / "ome-series.ome.tif"
    data = np.arange(2 * 3 * 4 * 5, dtype=np.uint16).reshape(2, 3, 4, 5)
    tiff.imwrite(
        path,
        data,
        ome=True,
        metadata={
            "axes": "TCYX",
            "PhysicalSizeX": 0.2,
            "PhysicalSizeXUnit": "micrometer",
            "PhysicalSizeY": 0.3,
            "PhysicalSizeYUnit": "micrometer",
        },
    )

    assert DataObj.getDatasetNames(str(path)) == ["Image0"]

    data_obj = DataObj("ome-series.ome.tif", None, path=str(path))
    data_obj.checkAndLoadData()

    assert data_obj.datasetName == "Image0"
    np.testing.assert_array_equal(data_obj.data, data)
    assert data_obj.axis_labels == ["T", "C", "Y", "X"]
    assert data_obj.axis_scales == [1.0, 1.0, 0.3, 0.2]
    assert data_obj.scale_unit == "um"
    assert data_obj.attrs["ome:PhysicalSizeX"] == 0.2
    assert data_obj.attrs["ome:PhysicalSizeY"] == 0.3
    assert data_obj.source_info == {
        "dataset_name": "Image0",
        "dataset_path": "0",
        "source_format": "ome-tiff",
    }

    data_obj.checkAndUnloadData()


def test_data_obj_ome_tiff_lazy_handle_slices_without_data_cache(tmp_path) -> None:
    path = tmp_path / "lazy-ome.ome.tif"
    data = np.arange(2 * 3 * 4 * 5, dtype=np.uint16).reshape(2, 3, 4, 5)
    tiff.imwrite(path, data, ome=True, metadata={"axes": "TCYX"})

    data_obj = DataObj("lazy-ome.ome.tif", None, path=str(path))
    data_obj.checkAndOpenData()

    assert data_obj.datasetName == "Image0"
    assert data_obj.sourceLoaded
    assert not data_obj.dataMaterialized
    handle = data_obj.data_handle
    assert handle.backend == "tiff"
    assert handle.shape == data.shape
    assert handle.dtype == data.dtype
    np.testing.assert_array_equal(handle[1, 2], data[1, 2])
    assert not data_obj.dataMaterialized

    np.testing.assert_array_equal(data_obj.data, data)
    assert data_obj.dataMaterialized

    data_obj.checkAndUnloadData()


def test_data_obj_selects_named_ome_tiff_series(tmp_path) -> None:
    path = tmp_path / "multi-series.ome.tif"
    first = np.zeros((3, 4), dtype=np.uint16)
    second = np.full((5, 6), 11, dtype=np.uint16)
    with tiff.TiffWriter(path, ome=True) as writer:
        writer.write(first, metadata={"axes": "YX", "Name": "first"})
        writer.write(second, metadata={"axes": "YX", "Name": "second"})

    assert DataObj.getDatasetNames(str(path)) == ["first", "second"]

    data_obj = DataObj("multi-series.ome.tif", "second", path=str(path))
    data_obj.checkAndLoadData()

    np.testing.assert_array_equal(data_obj.data, second)
    assert data_obj.axis_labels == ["Y", "X"]
    assert data_obj.source_info == {
        "dataset_name": "second",
        "dataset_path": "1",
        "source_format": "ome-tiff",
    }

    data_obj.checkAndUnloadData()


def test_data_obj_reads_legacy_zarr_array(tmp_path) -> None:
    path = tmp_path / "legacy.zarr"
    data = np.arange(2 * 3 * 4, dtype=np.uint16).reshape(2, 3, 4)
    root = zarr.group(store=ZarrStorer._make_store(str(path)), overwrite=True)
    array = ZarrStorer._create_array(root, "CAM", data=data, chunks=(1, 3, 4))
    array.attrs["detector_name"] = "CAM"
    array.attrs["writing"] = False

    assert DataObj.getDatasetNames(str(path)) == ["CAM"]

    data_obj = DataObj("legacy.zarr", "CAM", path=str(path))
    data_obj.checkAndLoadData()

    np.testing.assert_array_equal(data_obj.data, data)
    assert data_obj.attrs["detector_name"] == "CAM"
    assert data_obj.attrs["writing"] is False

    data_obj.checkAndUnloadData()


def test_data_obj_zarr_lazy_handle_does_not_materialize(tmp_path) -> None:
    path = tmp_path / "lazy.zarr"
    data = np.arange(3 * 4 * 5, dtype=np.uint16).reshape(3, 4, 5)
    root = zarr.group(store=ZarrStorer._make_store(str(path)), overwrite=True)
    ZarrStorer._create_array(root, "CAM", data=data, chunks=(1, 4, 5))

    data_obj = DataObj("lazy.zarr", "CAM", path=str(path))
    data_obj.checkAndOpenData()

    assert data_obj.sourceLoaded
    assert not data_obj.dataLoaded
    handle = data_obj.data_handle
    assert handle.backend == "zarr"
    assert handle.shape == data.shape
    assert handle.dtype == data.dtype
    assert handle.chunks == (1, 4, 5)
    np.testing.assert_array_equal(handle[2], data[2])
    assert not data_obj.dataMaterialized

    np.testing.assert_array_equal(data_obj.data, data)
    assert data_obj.dataMaterialized

    data_obj.checkAndUnloadData()


def test_data_obj_reads_structured_zarr_detector_group(tmp_path) -> None:
    path = tmp_path / "structured.zarr"
    data = np.arange(2 * 3 * 4, dtype=np.uint16).reshape(2, 3, 4)
    root = zarr.group(store=ZarrStorer._make_store(str(path)), overwrite=True)
    root.attrs["rec_mode"] = "recording"
    det_group = root.create_group("CAM")
    array = ZarrStorer._create_array(det_group, "data", data=data, chunks=(1, 3, 4))
    array.attrs["detector_name"] = "CAM"
    array.attrs["writing"] = False
    metadata = det_group.create_group("metadata")
    detector = metadata.create_group("detector")
    detector.attrs["exposure"] = 0.02
    metadata.attrs["uncategorized"] = "value"

    assert DataObj.getDatasetNames(str(path)) == ["CAM"]

    data_obj = DataObj("structured.zarr", "CAM", path=str(path))
    data_obj.checkAndLoadData()

    np.testing.assert_array_equal(data_obj.data, data)
    assert data_obj.attrs["rec_mode"] == "recording"
    assert data_obj.attrs["detector_name"] == "CAM"
    assert data_obj.attrs["detector:exposure"] == 0.02
    assert data_obj.attrs["uncategorized"] == "value"
    assert data_obj.attrs["writing"] is False

    data_obj.checkAndUnloadData()


def test_data_obj_normalizes_zarr_chunk_path_to_store_root(tmp_path) -> None:
    path = tmp_path / "structured.zarr"
    data = np.arange(2 * 3 * 4, dtype=np.uint16).reshape(2, 3, 4)
    root = zarr.group(store=ZarrStorer._make_store(str(path)), overwrite=True)
    det_group = root.create_group("APD")
    array = ZarrStorer._create_array(det_group, "data", data=data, chunks=(1, 3, 4))
    array.attrs["detector_name"] = "APD"
    array.attrs["writing"] = False

    data_dir = path / "APD" / "data"
    chunk_path = next(
        candidate for candidate in data_dir.rglob("*")
        if candidate.is_file() and candidate.name != "zarr.json"
    )

    assert DataObj.getDatasetNames(str(chunk_path)) == ["APD"]

    data_obj = DataObj("chunk", "APD", path=str(chunk_path))
    data_obj.checkAndLoadData()

    assert data_obj.dataPath == str(path)
    np.testing.assert_array_equal(data_obj.data, data)

    data_obj.checkAndUnloadData()


def test_data_obj_reads_root_ngff_zarr_full_resolution_level(tmp_path) -> None:
    path = tmp_path / "ngff-root.zarr"
    level0 = np.arange(2 * 4 * 6, dtype=np.uint16).reshape(2, 4, 6)
    level1 = level0[:, ::2, ::2]
    root = zarr.group(store=ZarrStorer._make_store(str(path)), overwrite=True)
    ZarrStorer._create_array(root, "0", data=level0, chunks=(1, 4, 6))
    ZarrStorer._create_array(root, "1", data=level1, chunks=(1, 2, 3))
    root.attrs["multiscales"] = [
        {
            "version": "0.4",
            "axes": [
                {"name": "t", "type": "time"},
                {"name": "y", "type": "space", "unit": "micrometer"},
                {"name": "x", "type": "space", "unit": "micrometer"},
            ],
            "datasets": [
                {
                    "path": "0",
                    "coordinateTransformations": [
                        {"type": "scale", "scale": [1.0, 0.2, 0.2]}
                    ],
                },
                {
                    "path": "1",
                    "coordinateTransformations": [
                        {"type": "scale", "scale": [1.0, 0.4, 0.4]}
                    ],
                },
            ],
        }
    ]

    assert DataObj.getDatasetNames(str(path)) == ["0"]

    data_obj = DataObj("ngff-root.zarr", None, path=str(path))
    data_obj.checkAndLoadData()

    assert data_obj.datasetName == "0"
    np.testing.assert_array_equal(data_obj.data, level0)
    assert data_obj.axis_labels == ["T", "Y", "X"]
    assert data_obj.axis_scales == [1.0, 0.2, 0.2]
    assert data_obj.scale_unit == "um"
    assert data_obj.source_info == {
        "dataset_name": "0",
        "dataset_path": "0",
        "source_format": "ome-zarr",
    }
    assert data_obj.attrs["ngff:axes"][1]["name"] == "y"
    assert data_obj.attrs["ngff:coordinateTransformations"][0]["scale"] == [1.0, 0.2, 0.2]

    data_obj.checkAndUnloadData()

    assert data_obj.axis_labels == ["T", "Y", "X"]
    assert data_obj.axis_scales == [1.0, 0.2, 0.2]
    assert data_obj.scale_unit == "um"


def test_data_obj_reads_ngff_zarr_image_groups_as_logical_datasets(tmp_path) -> None:
    path = tmp_path / "ngff-series.zarr"
    root = zarr.group(store=ZarrStorer._make_store(str(path)), overwrite=True)
    first = np.ones((1, 3, 5), dtype=np.uint16)
    second = np.full((1, 4, 6), 7, dtype=np.uint16)

    for name, data in {"series_0": first, "series_1": second}.items():
        group = root.create_group(name)
        ZarrStorer._create_array(group, "0", data=data, chunks=data.shape)
        group.attrs["ome"] = {
            "version": "0.5",
            "multiscales": [
                {
                    "axes": [
                        {"name": "z", "type": "space"},
                        {"name": "y", "type": "space"},
                        {"name": "x", "type": "space"},
                    ],
                    "datasets": [{"path": "0"}],
                }
            ],
        }

    assert DataObj.getDatasetNames(str(path)) == ["series_0", "series_1"]

    data_obj = DataObj("ngff-series.zarr", "series_1", path=str(path))
    data_obj.checkAndLoadData()

    np.testing.assert_array_equal(data_obj.data, second)
    assert data_obj.attrs["ngff:axes"][0]["name"] == "z"
    assert data_obj.axis_labels == ["Z", "Y", "X"]
    assert data_obj.axis_scales == [1.0, 1.0, 1.0]
    assert data_obj.scale_unit == "px"

    data_obj.checkAndUnloadData()


def test_view_only_uses_data_obj_axis_metadata(tmp_path) -> None:
    path = tmp_path / "ngff-view.zarr"
    data = np.arange(2 * 3 * 4, dtype=np.uint16).reshape(2, 3, 4)
    root = zarr.group(store=ZarrStorer._make_store(str(path)), overwrite=True)
    ZarrStorer._create_array(root, "0", data=data, chunks=(1, 3, 4))
    root.attrs["multiscales"] = [
        {
            "axes": [
                {"name": "t", "type": "time"},
                {"name": "y", "type": "space", "unit": "micrometer"},
                {"name": "x", "type": "space", "unit": "micrometer"},
            ],
            "datasets": [
                {
                    "path": "0",
                    "coordinateTransformations": [
                        {"type": "scale", "scale": [1.0, 0.108, 0.108]}
                    ],
                }
            ],
        }
    ]

    data_obj = DataObj("ngff-view.zarr", None, path=str(path))
    result = ViewOnlyReconstructor().process(data_obj, {})

    np.testing.assert_array_equal(result.data, data)
    assert result.axis_labels == ["T", "Y", "X"]
    assert result.axis_scales == [1.0, 0.108, 0.108]
    assert result.scale_unit == "um"
    assert not data_obj.dataLoaded


def test_view_only_labels_an_unexplained_frame_axis_frame(tmp_path):
    """A plain 3D stack is Frame x Y x X, not channel data.

    The rank-based default called the leading axis "C" for every unlabelled
    three-dimensional file, which is a claim about the acquisition that
    nothing in the file supports.
    """
    path = tmp_path / "unlabelled.h5"
    with h5py.File(path, "w") as file:
        file.create_dataset("CAM", data=np.zeros((7, 4, 5), dtype=np.uint16))
    data_obj = DataObj("unlabelled.h5", "CAM", path=str(path))

    result = ViewOnlyReconstructor().process(data_obj, {})

    assert result.axis_labels == ["Frame", "Y", "X"]


def test_view_only_keeps_axes_the_source_actually_declared(tmp_path):
    """An explicit container axis order is evidence and must survive."""
    path = tmp_path / "declared.h5"
    with h5py.File(path, "w") as file:
        dataset = file.create_dataset("CAM", data=np.zeros((3, 2, 4, 5), dtype=np.uint16))
        dataset.attrs["axes"] = "TZYX"
    data_obj = DataObj("declared.h5", "CAM", path=str(path))

    result = ViewOnlyReconstructor().process(data_obj, {})

    assert result.axis_labels == ["T", "Z", "Y", "X"]


def test_view_only_reports_how_the_acquisition_was_interpreted(tmp_path):
    """View-only is often the first place an unfamiliar file is opened."""
    path = tmp_path / "unlabelled-inspect.h5"
    with h5py.File(path, "w") as file:
        file.create_dataset("CAM", data=np.zeros((7, 4, 5), dtype=np.uint16))
    data_obj = DataObj("unlabelled-inspect.h5", "CAM", path=str(path))

    inspection = ViewOnlyReconstructor().inspect_source(data_obj)

    assert inspection is not None
    assert inspection.metadata["acquisition_layout_confidence"] == "low"
    assert inspection.warning  # the guess is stated, not buried in a log


# ----------------------------------------------------------------------
# What loading and previewing would cost, read without materialising
# ----------------------------------------------------------------------


class _LogLines:
    def __init__(self):
        self.warnings = []

    def warning(self, message, *_a, **_k):
        self.warnings.append(str(message))

    def debug(self, *_a, **_k):
        pass

    def error(self, *_a, **_k):
        pass


def test_decoded_bytes_comes_from_the_source_without_materializing(tmp_path) -> None:
    path = tmp_path / "size.h5"
    data = np.arange(3 * 4 * 5, dtype=np.uint16).reshape(3, 4, 5)
    with h5py.File(path, "w") as file:
        file.create_dataset("CAM", data=data, chunks=(1, 4, 5), compression="gzip")

    data_obj = DataObj("size.h5", "CAM", path=str(path))

    assert data_obj.decodedBytes() == data.nbytes
    assert data_obj.meanPreviewBytes() == 4 * 5 * (12 + 2)     # accumulator, result, one uint16 plane
    assert data_obj.planeReadIsBounded()
    assert data_obj.sourceHasLazyPath()
    assert not data_obj.dataMaterialized
    assert data_obj.materializationNotice() is None      # fits the working set


def test_a_load_above_the_working_set_is_announced_before_it_starts(tmp_path) -> None:
    from types import SimpleNamespace

    from imswitch.imcommon.model import memory_limits

    path = tmp_path / "big.h5"
    data = np.zeros((3, 1024, 512), dtype=np.uint16)           # 3 MiB decoded
    with h5py.File(path, "w") as file:
        file.create_dataset("CAM", data=data, chunks=(1, 1024, 512), compression="gzip")

    memory_limits.configure(SimpleNamespace(processingWorkingSetMB=1), logger=None)
    data_obj = DataObj("big.h5", "CAM", path=str(path))
    log = _LogLines()
    data_obj._DataObj__logger = log

    notice = data_obj.materializationNotice()
    assert notice is not None
    assert "3.0 MiB" in notice and "1.0 MiB" in notice
    assert "memory.processingWorkingSetMB in imcontrol_options.json" in notice
    assert "Open virtual" in notice                         # HDF5 has a lazy path
    assert not data_obj.dataMaterialized                    # saying it cost nothing

    data_obj.checkAndLoadData()

    assert log.warnings == [notice]                         # said, then loaded
    assert data_obj.dataMaterialized
    assert data_obj.materializationNotice() is None         # nothing left to warn about


def test_the_mean_preview_notice_is_about_the_plane_not_the_plane_count(tmp_path) -> None:
    from types import SimpleNamespace

    from imswitch.imcommon.model import memory_limits

    path = tmp_path / "wide.h5"
    with h5py.File(path, "w") as file:
        file.create_dataset("CAM", shape=(2, 512, 512), dtype=np.uint16, chunks=(1, 512, 512))

    data_obj = DataObj("wide.h5", "CAM", path=str(path))
    assert data_obj.meanPreviewNotice() is None             # 3 MiB fits 256 MiB

    memory_limits.configure(SimpleNamespace(processingWorkingSetMB=1), logger=None)
    notice = data_obj.meanPreviewNotice()
    assert notice is not None
    assert "3.5 MiB for one plane" in notice          # 512 x 512 x (12 + 2) bytes
    assert "memory.processingWorkingSetMB" in notice


def test_the_preview_estimate_covers_what_the_lazy_mean_actually_allocates(tmp_path) -> None:
    """Measured, not argued: the lazy mean used to peak at 22 B/px against a
    12 B/px estimate (a second float64 plane from ``accumulator / n``)."""
    import tracemalloc

    path = tmp_path / "peak.h5"
    with h5py.File(path, "w") as file:
        file.create_dataset("CAM", data=np.ones((4, 256, 256), np.uint16), chunks=(1, 256, 256))
    data_obj = DataObj("peak.h5", "CAM", path=str(path))
    estimate = data_obj.meanPreviewBytes()
    assert estimate == 256 * 256 * 14
    data_obj.data_handle                                       # open the source outside the trace

    tracemalloc.start()
    try:
        mean = data_obj.getMeanData()
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert mean.dtype == np.float32 and float(mean.mean()) == 1.0
    assert peak <= estimate + 64 * 1024                         # the estimate is a ceiling


def test_a_source_without_a_lazy_path_is_charged_the_whole_series_for_its_preview(tmp_path) -> None:
    """A TIFF whose ``aszarr()`` failed serves every plane by a whole-series
    read; its "plane" costs the decoded dataset, and so does the first plane."""
    from types import SimpleNamespace

    from imswitch.imcommon.model import memory_limits

    path = tmp_path / "nolazy.h5"
    with h5py.File(path, "w") as file:
        file.create_dataset("CAM", shape=(150, 128, 128), dtype=np.uint16, chunks=(1, 128, 128))
    data_obj = DataObj("nolazy.h5", "CAM", path=str(path))
    handle = data_obj.data_handle
    handle.supports_lazy_indexing = False                     # what the TIFF fallback declares

    assert not data_obj.sourceHasLazyPath()
    assert not data_obj.planeReadIsBounded()
    decoded = 150 * 128 * 128 * 2                              # 4.7 MiB
    assert data_obj.meanPreviewBytes() == 128 * 128 * 14 + decoded

    memory_limits.configure(SimpleNamespace(processingWorkingSetMB=1), logger=None)
    notice = data_obj.meanPreviewNotice()
    assert notice is not None
    assert "no lazy path" in notice and "decodes the whole series" in notice
    assert "memory.processingWorkingSetMB" in notice
