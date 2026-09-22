"""The edit window opens without computing a deferred mean.

Round 6 caught it setting the mean aside and then computing it anyway on
the way to showing it: "Not computed on opening" followed by "Computing it
as requested" with no button pressed.
"""
from types import SimpleNamespace

import numpy as np

from imswitch.improcess.controller.DataEditController import DataEditController


class _Frames:
    def __init__(self, data):
        self._data = np.asarray(data)
        self.shape = self._data.shape
        self.requested = []

    def __getitem__(self, item):
        self.requested.append(item)
        return self._data[item]


class _DataObj:
    name = "big"
    datasetName = "CAM"
    sourceKind = "image"
    axis_labels = None

    def __init__(self, data, notice, bounded=True):
        self._frames = _Frames(data)
        self.data = self._frames
        self.numFrames = data.shape[0]
        self._notice = notice
        self._bounded = bounded
        self.mean_calls = 0

    def meanPreviewNotice(self):
        return self._notice

    def planeReadIsBounded(self):
        return self._bounded

    def getMeanData(self):
        self.mean_calls += 1
        return np.mean(self._frames._data, axis=0)


def _controller():
    images = []
    widget = SimpleNamespace(
        setImage=lambda image, autoLevels: images.append((image, autoLevels)),
        updateDataProperties=lambda *_: None,
    )
    lines = []
    controller = SimpleNamespace(
        _widget=widget,
        _logger=SimpleNamespace(debug=lambda *_: None, info=lambda m, *_: lines.append(m),
                                warning=lambda m, *_: lines.append(m)),
        _dataObj=None,
        _meanData=None,
    )
    for name in ('setData', 'showMean', '_displayMean', 'setImgSlice'):
        setattr(controller, name, getattr(DataEditController, name).__get__(controller))
    return controller, images, lines


def test_a_deferred_mean_is_not_computed_on_opening_and_the_button_computes_it():
    data = np.arange(3 * 4 * 5, dtype=np.float32).reshape(3, 4, 5)
    data_obj = _DataObj(data, notice="The mean preview needs 2 GiB for one plane, above 256 MiB.")
    controller, images, lines = _controller()

    controller.setData(data_obj)                              # opening the window

    assert data_obj.mean_calls == 0
    np.testing.assert_array_equal(images[0][0], data[0])       # the first plane
    assert data_obj._frames.requested == [0]
    assert any("Opening on the first plane" in line for line in lines)
    assert not any("Computing it as requested" in line for line in lines)

    controller.showMean()                                      # the button

    assert data_obj.mean_calls == 1
    np.testing.assert_array_equal(images[-1][0], np.mean(data, axis=0))
    assert any("Computing it as requested" in line for line in lines)


def test_an_unbounded_source_opens_on_nothing_and_still_computes_on_request():
    data = np.arange(3 * 4 * 5, dtype=np.float32).reshape(3, 4, 5)
    data_obj = _DataObj(data, notice="needs 5 GiB: no lazy path", bounded=False)
    controller, images, lines = _controller()

    controller.setData(data_obj)

    assert data_obj.mean_calls == 0
    assert data_obj._frames.requested == []                    # not even one plane
    assert images[0][0].shape == (1, 1)
    assert any("Nothing shown until asked" in line for line in lines)

    controller.showMean()
    assert data_obj.mean_calls == 1


def test_a_mean_within_the_working_set_is_shown_on_opening_as_before():
    data = np.arange(3 * 4 * 5, dtype=np.float32).reshape(3, 4, 5)
    data_obj = _DataObj(data, notice=None)
    controller, images, lines = _controller()

    controller.setData(data_obj)

    assert data_obj.mean_calls == 1
    np.testing.assert_array_equal(images[0][0], np.mean(data, axis=0))
