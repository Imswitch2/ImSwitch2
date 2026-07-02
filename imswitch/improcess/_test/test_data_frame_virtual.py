from types import SimpleNamespace

import numpy as np

from imswitch.improcess.controller.DataFrameController import DataFrameController


class _VirtualFrames:
    def __init__(self, data):
        self._data = np.asarray(data)
        self.shape = self._data.shape
        self.requested = []

    def __getitem__(self, item):
        self.requested.append(item)
        return self._data[item]


class _VirtualDataObj:
    name = "virtual"
    datasetName = "CAM"
    dataMaterialized = False

    def __init__(self, data):
        self.data_handle = _VirtualFrames(data)
        self.numFrames = data.shape[0]

    @property
    def data(self):
        raise AssertionError("virtual current data should not materialize")

    def getMeanData(self):
        return np.mean(self.data_handle._data, axis=0)


def _controller_stub():
    calls = SimpleNamespace(
        images=[],
        frames=[],
        names=[],
        datasets=[],
    )
    widget = SimpleNamespace(
        setImage=lambda image, autoLevels: calls.images.append((image, autoLevels)),
        setNumFrames=lambda value: calls.frames.append(value),
        setDataName=lambda value: calls.names.append(value),
        setDatasetName=lambda value: calls.datasets.append(value),
    )
    controller = SimpleNamespace(
        _widget=widget,
        _logger=SimpleNamespace(debug=lambda *_: None),
        _dataObj=None,
    )
    controller._currentDataArray = DataFrameController._currentDataArray.__get__(controller)
    controller.showMean = DataFrameController.showMean.__get__(controller)
    controller.currentDataChanged = DataFrameController.currentDataChanged.__get__(controller)
    controller.setImgSlice = DataFrameController.setImgSlice.__get__(controller)
    return controller, calls


def test_current_data_panel_uses_virtual_handle_for_display():
    data = np.arange(3 * 4 * 5, dtype=np.float32).reshape(3, 4, 5)
    data_obj = _VirtualDataObj(data)
    controller, calls = _controller_stub()

    controller.currentDataChanged(data_obj)
    controller.setImgSlice(2)

    np.testing.assert_array_equal(calls.images[0][0], np.mean(data, axis=0))
    assert calls.images[0][1] is True
    np.testing.assert_array_equal(calls.images[1][0], data[2])
    assert calls.images[1][1] is False
    assert data_obj.data_handle.requested == [2]
    assert calls.frames == [3]
    assert calls.names == ["virtual"]
    assert calls.datasets == ["CAM"]
