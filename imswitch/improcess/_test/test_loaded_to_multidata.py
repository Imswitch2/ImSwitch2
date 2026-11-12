from pathlib import Path
from types import SimpleNamespace

from imswitch.improcess.controller.FileIOController import FileIOController
from imswitch.improcess.model.lapse_source import TIME_LAPSE_SOURCE_KIND


class _RecordingMultiData:
    def __init__(self):
        self.existing = []
        self.made = []

    def addDataObj(self, data_obj):
        self.existing.append(data_obj)
        return data_obj

    def makeAndAddDataObj(self, name, dataset_name, path=None, file=None):
        self.made.append((name, dataset_name, path))


def _controller():
    ctl = SimpleNamespace()
    ctl.multiDataFrameController = _RecordingMultiData()
    ctl._logger = SimpleNamespace(warning=lambda *args, **kwargs: None)
    ctl._addDataObjToWorkingSet = FileIOController._addDataObjToWorkingSet.__get__(ctl)
    return ctl


def test_loaded_ordinary_image_is_reused_in_multidata():
    ctl = _controller()
    data_obj = SimpleNamespace(sourceKind="image")

    ctl._addDataObjToWorkingSet(data_obj)

    assert ctl.multiDataFrameController.existing == [data_obj]
    assert ctl.multiDataFrameController.made == []


def test_loaded_logical_timelapse_expands_into_multidata_timepoints():
    ctl = _controller()
    refs = (
        SimpleNamespace(path=Path(str(Path("/raw/scan0_CAM.hdf5"))), dataset="Camera"),
        SimpleNamespace(path=Path(str(Path("/raw/scan1_CAM.hdf5"))), dataset="Camera"),
        SimpleNamespace(path=Path(str(Path("/raw/scan2_CAM.hdf5"))), dataset="Camera"),
    )
    index = SimpleNamespace(
        channel=lambda: SimpleNamespace(candidates=refs),
    )
    data_obj = SimpleNamespace(
        sourceKind=TIME_LAPSE_SOURCE_KIND,
        sourceMetadata=index,
    )

    ctl._addDataObjToWorkingSet(data_obj)

    assert ctl.multiDataFrameController.existing == []
    assert ctl.multiDataFrameController.made == [
        ("scan0_CAM.hdf5", "Camera", str(Path("/raw/scan0_CAM.hdf5"))),
        ("scan1_CAM.hdf5", "Camera", str(Path("/raw/scan1_CAM.hdf5"))),
        ("scan2_CAM.hdf5", "Camera", str(Path("/raw/scan2_CAM.hdf5"))),
    ]


def test_loaded_single_file_timelapse_keeps_group_dataset_names():
    ctl = _controller()
    refs = (
        SimpleNamespace(path=Path(str(Path("/raw/lapse_CAM.hdf5"))), dataset="scan0/Camera"),
        SimpleNamespace(path=Path(str(Path("/raw/lapse_CAM.hdf5"))), dataset="scan1/Camera"),
    )
    index = SimpleNamespace(
        channel=lambda: SimpleNamespace(candidates=refs),
    )
    data_obj = SimpleNamespace(
        sourceKind=TIME_LAPSE_SOURCE_KIND,
        sourceMetadata=index,
    )

    ctl._addDataObjToWorkingSet(data_obj)

    assert ctl.multiDataFrameController.made == [
        ("lapse_CAM.hdf5", "scan0/Camera", str(Path("/raw/lapse_CAM.hdf5"))),
        ("lapse_CAM.hdf5", "scan1/Camera", str(Path("/raw/lapse_CAM.hdf5"))),
    ]
