"""The data panels must render any dataset rank, not just ``(frames, Y, X)``.

pyqtgraph rejects anything that is not a 2D plane (or an RGB(A) triple), and it
does so inside ``ImageItem.render()`` at paint time -- so a bad image reaches
the renderer long after the call that supplied it, and the traceback names no
ImSwitch code. These tests therefore drive a real ``ImageItem`` to completion
rather than merely asserting on shapes.
"""

from types import SimpleNamespace

import numpy as np
import pyqtgraph as pg
import pytest
from pyqtgraph.Qt import QtWidgets

from imswitch.improcess.controller.DataFrameController import DataFrameController
from imswitch.improcess.model.plane_navigation import mean_plane, plane_count


@pytest.fixture(scope="module")
def qapp():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _render(image):
    """Push ``image`` through the same path the Data dock uses."""
    item = pg.ImageItem(axisOrder="row-major")
    item.setImage(np.asarray(image, dtype=np.float32), autoLevels=True)
    item.render()


class _Handle:
    def __init__(self, data):
        self._data = np.asarray(data)
        self.shape = self._data.shape
        self.ndim = self._data.ndim
        self.requested = []

    def __getitem__(self, item):
        self.requested.append(item)
        return self._data[item]


class _DataObj:
    name = "stack"
    datasetName = "APD"
    dataMaterialized = False

    def __init__(self, data, axis_labels=None):
        self._array = np.asarray(data)
        self.data_handle = _Handle(self._array)
        self.axis_labels = axis_labels

    @property
    def data(self):
        return self._array

    def getMeanData(self):
        return mean_plane(self._array, self.axis_labels)

    @property
    def numFrames(self):
        return plane_count(self._array.shape, self.axis_labels)


def _controller():
    calls = SimpleNamespace(images=[], frames=[])
    widget = SimpleNamespace(
        setImage=lambda image, autoLevels: calls.images.append(image),
        setNumFrames=lambda value: calls.frames.append(value),
        setDataName=lambda value: None,
        setDatasetName=lambda value: None,
    )
    controller = SimpleNamespace(
        _widget=widget,
        _logger=SimpleNamespace(debug=lambda *_: None),
        _commChannel=SimpleNamespace(
            sigDisplayedFrameChanged=SimpleNamespace(emit=lambda: None)
        ),
        _dataObj=None,
        _displayedImage=None,
    )
    for name in ("_currentDataArray", "_currentAxisLabels", "showMean",
                 "currentDataChanged", "setImgSlice"):
        setattr(controller, name, getattr(DataFrameController, name).__get__(controller))
    return controller, calls


@pytest.mark.parametrize(
    "shape, labels, expected_frames",
    [
        ((6, 5), None, 1),                          # single image
        ((7, 6, 5), None, 7),                       # classic frame stack
        ((4, 7, 6, 5), ["T", "Z", "Y", "X"], 28),   # z-stack timelapse
        ((2, 4, 7, 6, 5), None, 56),                # 5D
    ],
)
def test_data_panel_renders_every_rank(qapp, shape, labels, expected_frames):
    data = np.arange(int(np.prod(shape)), dtype=np.float32).reshape(shape)
    controller, calls = _controller()

    controller.currentDataChanged(_DataObj(data, labels))
    controller.setImgSlice(expected_frames - 1)

    assert calls.frames == [expected_frames]
    for image in calls.images:
        assert np.ndim(image) == 2
        _render(image)


def test_four_dimensional_stack_used_to_crash_the_renderer(qapp):
    """Regression guard for ``TypeError: data.shape[2] must be <= 4``."""
    data = np.arange(4 * 7 * 6 * 5, dtype=np.float32).reshape(4, 7, 6, 5)

    with pytest.raises(TypeError, match=r"shape\[2\] must be <= 4"):
        _render(data[0])          # what the panel used to hand over

    controller, calls = _controller()
    controller.currentDataChanged(_DataObj(data))
    _render(calls.images[0])      # what it hands over now


def test_data_panel_still_reads_one_plane_at_a_time(qapp):
    data = np.arange(4 * 7 * 6 * 5, dtype=np.float32).reshape(4, 7, 6, 5)
    dataObj = _DataObj(data)
    controller, _ = _controller()

    controller.currentDataChanged(dataObj)
    dataObj.data_handle.requested.clear()
    controller.setImgSlice(9)

    assert dataObj.data_handle.requested == [(1, 2)]


def test_frame_slider_walks_planes_in_order(qapp):
    data = np.arange(3 * 2 * 6 * 5, dtype=np.float32).reshape(3, 2, 6, 5)
    controller, calls = _controller()
    controller.currentDataChanged(_DataObj(data))
    calls.images.clear()

    for frame in range(6):
        controller.setImgSlice(frame)

    expected = [data[i, j] for i in range(3) for j in range(2)]
    for shown, want in zip(calls.images, expected):
        np.testing.assert_array_equal(shown, want)
