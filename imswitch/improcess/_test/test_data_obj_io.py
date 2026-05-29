import h5py
import numpy as np
import zarr

from imswitch.improcess.model import DataObj


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


def test_data_obj_reads_legacy_zarr_array(tmp_path) -> None:
    path = tmp_path / "legacy.zarr"
    data = np.arange(2 * 3 * 4, dtype=np.uint16).reshape(2, 3, 4)
    root = zarr.group(store=zarr.storage.LocalStore(str(path)), overwrite=True)
    array = root.create_array("CAM", data=data, chunks=(1, 3, 4))
    array.attrs["detector_name"] = "CAM"
    array.attrs["writing"] = False

    assert DataObj.getDatasetNames(str(path)) == ["CAM"]

    data_obj = DataObj("legacy.zarr", "CAM", path=str(path))
    data_obj.checkAndLoadData()

    np.testing.assert_array_equal(data_obj.data, data)
    assert data_obj.attrs["detector_name"] == "CAM"
    assert data_obj.attrs["writing"] is False

    data_obj.checkAndUnloadData()


def test_data_obj_reads_structured_zarr_detector_group(tmp_path) -> None:
    path = tmp_path / "structured.zarr"
    data = np.arange(2 * 3 * 4, dtype=np.uint16).reshape(2, 3, 4)
    root = zarr.group(store=zarr.storage.LocalStore(str(path)), overwrite=True)
    root.attrs["rec_mode"] = "recording"
    det_group = root.create_group("CAM")
    array = det_group.create_array("data", data=data, chunks=(1, 3, 4))
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
