"""MoNaLISAController: Find pattern on raw frames, and the live session's grid shown."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np

from imswitch.improcess.controller.MoNaLISAController import MoNaLISAController


def _grid_stack(frames=3, rows=100, cols=100, xp=10.0, yp=10.0, xo=5.0, yo=5.0):
    rng = np.random.default_rng(1)
    stack = rng.integers(50, 150, size=(frames, rows, cols), dtype=np.uint16)
    for cy in np.arange(yo, rows, yp):
        for cx in np.arange(xo, cols, xp):
            stack[:, int(cy), int(cx)] += 300
    return stack


class _DataObj:
    def __init__(self, stack):
        self.data_handle = stack
        self.axis_labels = ['T', 'Y', 'X']
        self.mean_reads = 0

    def getMeanData(self):
        self.mean_reads += 1
        return self.data_handle.mean(axis=0)


def _controller(data_obj):
    controller = MoNaLISAController.__new__(MoNaLISAController)
    controller._logger = MagicMock()
    controller._main = SimpleNamespace(_currentDataObj=data_obj)
    controller._widget = MagicMock()
    controller._widget.getPatternParams.return_value = (0, 0, 1, 1)
    controller._commChannel = MagicMock()
    controller._settingPatternParams = False
    controller._pattern = (0, 0, 1, 1)
    return controller


def test_find_pattern_localizes_on_the_first_raw_frames_not_the_mean():
    data_obj = _DataObj(_grid_stack())
    controller = _controller(data_obj)

    controller.findPattern()

    row_offset, col_offset, row_period, col_period = controller._widget.setPatternParams.call_args.args
    np.testing.assert_allclose([row_period, col_period], [10.0, 10.0], atol=0.5)
    np.testing.assert_allclose([row_offset, col_offset], [5.0, 5.0], atol=0.6)
    assert data_obj.mean_reads == 0
    controller._commChannel.sigPatternUpdated.emit.assert_called()


def test_find_pattern_says_so_when_no_frame_shows_a_grid():
    controller = _controller(_DataObj(np.zeros((3, 60, 60), dtype=np.uint16)))

    controller.findPattern()

    controller._widget.setPatternParams.assert_not_called()
    message = controller._commChannel.sigStatusMessage.emit.call_args.args[0]
    assert 'No illumination grid' in message


def test_find_pattern_without_data_does_nothing():
    controller = _controller(None)

    controller.findPattern()

    controller._widget.setPatternParams.assert_not_called()


def test_a_live_session_grid_is_shown_in_the_widget():
    controller = _controller(None)

    controller.livePatternLocalized({
        'row_offset': 1.25, 'col_offset': 2.5, 'row_period': 10.5, 'col_period': 11.0,
        'source': 'auto',
    })

    controller._widget.setPatternParams.assert_called_once_with(1.25, 2.5, 10.5, 11.0)
    controller._commChannel.sigPatternUpdated.emit.assert_called()

    controller._widget.setPatternParams.reset_mock()
    controller.livePatternLocalized({'row_offset': 'x'})
    controller._widget.setPatternParams.assert_not_called()
