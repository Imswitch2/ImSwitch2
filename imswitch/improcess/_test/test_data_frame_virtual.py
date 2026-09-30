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


class _Signal:
    def __init__(self, calls):
        self._calls = calls

    def emit(self):
        self._calls.append(True)


def _controller_stub():
    calls = SimpleNamespace(
        images=[],
        frames=[],
        names=[],
        datasets=[],
        displayed_frames=[],
    )
    widget = SimpleNamespace(
        setImage=lambda image, autoLevels: calls.images.append((image, autoLevels)),
        setNumFrames=lambda value: calls.frames.append(value),
        setDataName=lambda value: calls.names.append(value),
        setDatasetName=lambda value: calls.datasets.append(value),
    )
    comm_channel = SimpleNamespace(
        sigDisplayedFrameChanged=_Signal(calls.displayed_frames),
    )
    controller = SimpleNamespace(
        _widget=widget,
        _logger=SimpleNamespace(debug=lambda *_: None, info=lambda *_: None,
                                warning=lambda *_: None),
        _commChannel=comm_channel,
        _dataObj=None,
    )
    controller._currentDataArray = DataFrameController._currentDataArray.__get__(controller)
    controller._currentAxisLabels = DataFrameController._currentAxisLabels.__get__(controller)
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
    assert calls.displayed_frames == [True, True]


class _StatusSignal:
    def __init__(self):
        self.lines = []

    def emit(self, line):
        self.lines.append(line)


class _WideVirtualDataObj(_VirtualDataObj):
    """A source whose one plane outgrows the working set; the mean is tracked."""

    def __init__(self, data, notice):
        super().__init__(data)
        self._notice = notice
        self.mean_calls = 0

    def meanPreviewNotice(self):
        return self._notice

    def getMeanData(self):
        self.mean_calls += 1
        return super().getMeanData()


def test_the_automatic_preview_above_the_working_set_shows_the_first_plane_and_says_so():
    data = np.arange(3 * 4 * 5, dtype=np.float32).reshape(3, 4, 5)
    notice = "The mean preview needs 2 GiB for one plane, above the 256 MiB working set (memory.processingWorkingSetMB)."
    data_obj = _WideVirtualDataObj(data, notice)
    controller, calls = _controller_stub()
    status = _StatusSignal()
    controller._commChannel.sigStatusMessage = status

    controller.currentDataChanged(data_obj)                # automatic: on load

    assert data_obj.mean_calls == 0                        # nothing was computed
    np.testing.assert_array_equal(calls.images[0][0], data[0])   # the first plane, native coordinates
    assert data_obj.data_handle.requested == [0]
    assert status.lines and notice in status.lines[0] and "Show mean" in status.lines[0]

    controller.showMean(explicit=True)                     # the button: what was asked for

    assert data_obj.mean_calls == 1
    np.testing.assert_array_equal(calls.images[-1][0], np.mean(data, axis=0))
    assert any("Computing it as requested" in line for line in status.lines)


def test_a_preview_within_the_working_set_is_computed_on_load_as_before():
    data = np.arange(3 * 4 * 5, dtype=np.float32).reshape(3, 4, 5)
    data_obj = _WideVirtualDataObj(data, None)             # no notice: it fits
    controller, calls = _controller_stub()

    controller.currentDataChanged(data_obj)

    assert data_obj.mean_calls == 1
    np.testing.assert_array_equal(calls.images[0][0], np.mean(data, axis=0))


class _UnboundedVirtualDataObj(_WideVirtualDataObj):
    """A source whose plane read decodes the whole series (a non-lazy TIFF)."""

    def planeReadIsBounded(self):
        return False


def test_an_unbounded_source_above_the_working_set_shows_nothing_until_asked():
    data = np.arange(3 * 4 * 5, dtype=np.float32).reshape(3, 4, 5)
    notice = "The mean preview needs 5 GiB: this source has no lazy path, so every plane read decodes the whole series, above the 256 MiB working set."
    data_obj = _UnboundedVirtualDataObj(data, notice)
    controller, calls = _controller_stub()
    status = _StatusSignal()
    controller._commChannel.sigStatusMessage = status

    controller.currentDataChanged(data_obj)

    assert data_obj.mean_calls == 0
    assert data_obj.data_handle.requested == []                # not even the first plane
    assert calls.images[0][0].shape == (1, 1)                  # a placeholder
    assert "Nothing shown until asked" in status.lines[0]

    controller.showMean(explicit=True)
    assert data_obj.mean_calls == 1
