"""Regression tests for the pattern-overlay handling in DataFrameController.

Constructed without Qt scaffolding: the overlay logic is plain state + numpy,
so the controller is built bare and given stub collaborators.
"""

import numpy as np

from imswitch.improcess.controller.DataFrameController import DataFrameController


class _WidgetStub:
    def __init__(self):
        self.calls = []

    def setPatternGridData(self, x, y):
        self.calls.append((np.asarray(x), np.asarray(y)))


class _LoggerStub:
    def debug(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class _DataStub:
    def __init__(self, shape):
        self.data = np.zeros(shape)


def _bare_controller():
    controller = DataFrameController.__new__(DataFrameController)
    controller._widget = _WidgetStub()
    controller._logger = _LoggerStub()
    controller._dataObj = _DataStub((5, 60, 60))
    controller._pattern = []
    controller._patternGrid = []
    controller._patternGridMade = False
    controller._patternVisible = False
    controller._explicitPatternPoints = None
    return controller


def test_unchanged_pattern_reemission_keeps_explicit_points():
    """The Find pattern button lives inside the Pattern tree group, so the
    click itself re-fires the group's changed signal with unchanged values —
    which must not wipe the lattice points the click just put up."""
    controller = _bare_controller()
    rect_pattern = [9.89, 10.4, 11.1, 11.1]
    controller.patternUpdated(rect_pattern)

    controller.patternPointsUpdated([1.0, 2.0, 3.0], [4.0, 5.0, 6.0])
    controller.patternUpdated(list(rect_pattern))  # spurious re-emission

    controller._patternVisible = True
    controller.makePatternGrid()

    assert controller._widget.calls, 'overlay was never drawn'
    x, y = controller._widget.calls[-1]
    np.testing.assert_allclose(x, [1.0, 2.0, 3.0])
    np.testing.assert_allclose(y, [4.0, 5.0, 6.0])


def test_changed_pattern_returns_overlay_to_rectangular_grid():
    controller = _bare_controller()
    controller.patternUpdated([9.89, 10.4, 11.1, 11.1])
    controller.patternPointsUpdated([1.0], [2.0])

    controller._patternVisible = True
    controller.patternUpdated([5.0, 5.0, 10.0, 10.0])  # genuine edit

    assert controller._explicitPatternPoints is None
    x, y = controller._widget.calls[-1]
    # Rectangular grid from the edited values, not the single explicit point.
    assert x.size > 1 and y.size > 1


def test_points_drawn_immediately_when_visible():
    controller = _bare_controller()
    controller._patternVisible = True
    controller.patternPointsUpdated([7.0, 8.0], [1.0, 2.0])
    x, y = controller._widget.calls[-1]
    np.testing.assert_allclose(x, [7.0, 8.0])
    np.testing.assert_allclose(y, [1.0, 2.0])
