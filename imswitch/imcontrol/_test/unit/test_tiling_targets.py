"""The tiling target list: stage positions kept by the controller, drawn on
the overview, edited from the widget and from the scripting API."""

import itertools
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from qtpy import QtCore

from imswitch.imcontrol.controller.controllers.TilingController import (
    TilingController,
)
from imswitch.imcontrol.model.workflows.stitched_image import StitchedImage
from imswitch.imcontrol.model.workflows.target import TargetList
from imswitch.imcontrol.view.widgets.TilingWidget import (
    CLICK_ADD_TARGET,
    CLICK_NAVIGATE,
    CLICK_NONE,
)

ALL_ORIENTATIONS = list(itertools.product([False, True], repeat=3))
TILE = (16, 16)
STEP_UM = 8.0
ORIGIN = (1000.0, 2000.0)


class _Widget:
    def __init__(self, mode=CLICK_NONE):
        self.mode = mode
        self.targets = []
        self.markers = []

    def getClickMode(self):
        return self.mode

    def setTargets(self, records):
        self.targets.append(records)

    def setTargetMarkers(self, markers):
        self.markers.append(markers)


def _controller(orientation=(False, False, False), with_overview=True):
    ctrl = TilingController.__new__(TilingController)
    QtCore.QObject.__init__(ctrl)
    ctrl._logger = MagicMock()
    ctrl._widget = _Widget()
    ctrl._targets = TargetList()
    ctrl._scanning = False
    ctrl._closed = False
    ctrl._orientation = orientation
    ctrl._stitcher = None
    ctrl._originXY = None
    ctrl._gridPositions = []
    positioner = MagicMock()
    ctrl._master = SimpleNamespace(positionersManager={'xy': positioner})
    ctrl._setupInfo = SimpleNamespace(
        tiling=SimpleNamespace(xyPositioner='xy'),
        positioners={'xy': SimpleNamespace(axes=('X', 'Y'))},
        detectors={},
    )
    ctrl.sigTargetsChanged.connect(ctrl._widget.setTargets)
    ctrl.sigTargetMarkers.connect(ctrl._widget.setTargetMarkers)
    if with_overview:
        stitcher = StitchedImage(
            tile_size_px=None, tile_step_um=STEP_UM, px_per_um=None,
            tile_shape_px=TILE, pixel_size_um=1.0,
        )
        for gx, gy in [(0, 0), (1, 0), (0, 1), (-1, 0), (0, -1)]:
            ix, iy = TilingController._gridToImage(gx, gy, orientation)
            stitcher.add_tile(np.ones(TILE, dtype=np.uint16), ix, iy)
            ctrl._gridPositions.append((gx, gy))
        ctrl._stitcher = stitcher
        ctrl._originXY = ORIGIN
    return ctrl, positioner


# ----------------------------------------------------------------------
# Geometry
# ----------------------------------------------------------------------


@pytest.mark.parametrize('orientation', ALL_ORIENTATIONS)
def test_stage_to_pixel_inverts_pixel_to_stage(orientation):
    """A target marked at a pixel is drawn back at that pixel, every mounting."""
    ctrl, _ = _controller(orientation)
    for row, col in [(3, 5), (20, 7), (11, 30), (0, 0)]:
        stage = ctrl._canvasPixelToStage(row, col)
        back = ctrl._stageToCanvasPixel(*stage)
        assert back == pytest.approx((row, col))


def test_a_position_off_the_overview_has_no_pixel():
    ctrl, _ = _controller()
    assert ctrl._stageToCanvasPixel(ORIGIN[0] + 1e6, ORIGIN[1]) is None


def test_without_an_overview_nothing_has_a_pixel():
    ctrl, _ = _controller(with_overview=False)
    assert ctrl._stageToCanvasPixel(*ORIGIN) is None


# ----------------------------------------------------------------------
# Clicks on the overview
# ----------------------------------------------------------------------


def test_a_click_in_add_mode_records_the_place_without_moving():
    ctrl, positioner = _controller()
    ctrl._widget.mode = CLICK_ADD_TARGET

    ctrl._onOverviewClicked(5, 7)

    positioner.setPosition.assert_not_called()
    (target,) = ctrl.getTargets()
    assert target['stage_xy'] == pytest.approx(ctrl._canvasPixelToStage(5, 7))
    assert target['source'] == 'manual'
    assert target['row_col'] == [5, 7]
    assert ctrl._widget.markers[-1][target['id']] == pytest.approx((5, 7))


def test_a_click_in_navigate_mode_moves_and_records_nothing():
    ctrl, positioner = _controller()
    ctrl._widget.mode = CLICK_NAVIGATE

    ctrl._onOverviewClicked(5, 7)

    assert positioner.setPosition.call_count == 2
    assert ctrl.getTargets() == []


def test_a_click_with_clicks_off_does_nothing():
    ctrl, positioner = _controller()

    ctrl._onOverviewClicked(5, 7)

    positioner.setPosition.assert_not_called()
    assert ctrl.getTargets() == []


def test_adding_before_any_overview_is_refused_quietly():
    ctrl, _ = _controller(with_overview=False)
    ctrl._widget.mode = CLICK_ADD_TARGET

    ctrl._onOverviewClicked(5, 7)

    assert ctrl.getTargets() == []
    assert ctrl._logger.info.called


def test_targets_outlive_the_overview_but_their_markers_do_not():
    """A target is a place on the sample, not a pixel of one overview."""
    ctrl, _ = _controller()
    ctrl._widget.mode = CLICK_ADD_TARGET
    ctrl._onOverviewClicked(5, 7)
    assert len(ctrl._widget.markers[-1]) == 1

    ctrl._onDetectorChanged()

    assert len(ctrl.getTargets()) == 1
    assert ctrl._widget.markers[-1] == {}


def test_targets_off_the_overview_are_listed_but_not_drawn():
    ctrl, _ = _controller()

    far = ctrl.addTarget(ORIGIN[0] + 1e6, ORIGIN[1])
    near = ctrl.addTarget(ORIGIN[0] + 2.0, ORIGIN[1] - 3.0)

    assert [t['id'] for t in ctrl.getTargets()] == [far, near]
    assert set(ctrl._widget.markers[-1]) == {near}


# ----------------------------------------------------------------------
# The list and its API
# ----------------------------------------------------------------------


def test_set_and_get_targets_round_trip():
    ctrl, _ = _controller()

    count = ctrl.setTargets([
        {'stage_xy': (1.0, 2.0), 'enabled': False, 'source': 'segmentation',
         'props': {'area_um2': 12.5}},
        {'x': 3.0, 'y': 4.0},
    ])

    assert count == 2
    first, second = ctrl.getTargets()
    assert first['id'] == 1 and first['enabled'] is False
    assert first['source'] == 'segmentation'
    assert first['props'] == {'area_um2': 12.5}
    assert second == {
        'id': 2, 'stage_xy': [3.0, 4.0], 'source': 'imported', 'enabled': True,
        'row_col': None, 'z_um': None, 'props': {},
    }
    # What came out goes back in unchanged.
    ctrl.setTargets(ctrl.getTargets())
    assert ctrl.getTargets() == [first, second]
    assert ctrl._widget.targets[-1] == [first, second]


def test_set_targets_refuses_a_record_without_a_position():
    ctrl, _ = _controller()
    with pytest.raises(ValueError, match='stage_xy'):
        ctrl.setTargets([{'enabled': True}])


def test_set_targets_refuses_an_unknown_source():
    ctrl, _ = _controller()
    with pytest.raises(ValueError, match='source'):
        ctrl.setTargets([{'stage_xy': (0.0, 0.0), 'source': 'guess'}])


def test_add_enable_remove_and_clear():
    ctrl, _ = _controller()

    first = ctrl.addTarget(1.0, 2.0)
    second = ctrl.addTarget(3.0, 4.0, enabled=False, source='segmentation')
    assert (first, second) == (1, 2)

    assert ctrl.setTargetEnabled(second, True) is True
    assert ctrl.setTargetEnabled(99, True) is False
    assert [t['enabled'] for t in ctrl.getTargets()] == [True, True]

    assert ctrl.removeTarget(first) is True
    assert ctrl.removeTarget(first) is False
    # The survivor keeps its id: a script holding it is not pointed elsewhere.
    assert [t['id'] for t in ctrl.getTargets()] == [second]
    assert ctrl.addTarget(5.0, 6.0) == 3

    ctrl.clearTargets()
    assert ctrl.getTargets() == []
    assert ctrl._widget.targets[-1] == []


def test_go_to_target_moves_the_stage():
    ctrl, positioner = _controller()
    target_id = ctrl.addTarget(1010.0, 2004.0)

    assert ctrl.goToTarget(target_id) == (1010.0, 2004.0)

    assert positioner.setPosition.call_args_list == [
        ((1010.0, 'X'),), ((2004.0, 'Y'),),
    ]


def test_go_to_target_is_refused_while_tiling_owns_the_stage():
    ctrl, positioner = _controller()
    target_id = ctrl.addTarget(1010.0, 2004.0)
    ctrl._scanning = True

    assert ctrl.goToTarget(target_id) is None

    positioner.setPosition.assert_not_called()


def test_go_to_an_unknown_target_raises():
    ctrl, _ = _controller()
    with pytest.raises(ValueError, match='No target'):
        ctrl.goToTarget(42)


def test_save_and_load_targets(tmp_path):
    ctrl, _ = _controller()
    ctrl.addTarget(1.0, 2.0)
    ctrl.addTarget(3.0, 4.0, enabled=False)
    before = ctrl.getTargets()

    written = ctrl.saveTargets(str(tmp_path / 'targets'))

    assert written.endswith('.json')
    with open(written, encoding='utf-8') as handle:
        assert len(json.load(handle)['targets']) == 2

    ctrl.clearTargets()
    assert ctrl.loadTargets(written) == 2
    assert ctrl.getTargets() == before


def test_list_mutators_run_on_the_ui_thread_and_reads_do_not():
    """The convention of the rest of the API: anything that changes what the
    widget shows is marshalled to the GUI thread; a read is not."""
    for name in ('setTargets', 'addTarget', 'removeTarget', 'clearTargets',
                 'setTargetEnabled', 'goToTarget', 'loadTargets'):
        assert getattr(TilingController, name)._APIRunOnUIThread is True, name
    assert TilingController.getTargets._APIRunOnUIThread is False
    assert TilingController.saveTargets._APIRunOnUIThread is False


def test_the_cell_detection_api_is_gone():
    for name in ('detectCellTargets', 'runCellTargeting'):
        assert not hasattr(TilingController, name)


# ----------------------------------------------------------------------
# The widget's table
# ----------------------------------------------------------------------


RECORDS = [
    {'id': 1, 'stage_xy': [10.0, 20.0], 'enabled': True, 'source': 'manual'},
    {'id': 2, 'stage_xy': [30.0, 40.0], 'enabled': False, 'source': 'imported'},
]


@pytest.fixture
def widget(qtbot):
    from imswitch.imcontrol.view.widgets.TilingWidget import TilingWidget

    widget = TilingWidget(None)
    qtbot.addWidget(widget)
    return widget


def test_table_lists_targets_and_counts_the_enabled_ones(widget):
    widget.setTargets(RECORDS)

    assert widget.targetsTable.rowCount() == 2
    assert widget.targetsTable.item(1, 1).text() == '2'
    assert widget.targetsTable.item(1, 2).text() == '30.0'
    assert widget.targetsTable.item(1, 4).text() == 'imported'
    assert widget.targetsCountLabel.text() == '1 of 2 targets on'

    widget.setTargets([])
    assert widget.targetsTable.rowCount() == 0
    assert widget.targetsCountLabel.text() == ''


def test_ticking_a_target_off_reports_its_id_not_its_row(widget):
    seen = []
    widget.sigTargetEnabledChanged.connect(lambda i, on: seen.append((i, on)))
    widget.setTargets(RECORDS)
    assert seen == [], 'filling the table is not the operator ticking boxes'

    widget.targetsTable.item(0, 0).setCheckState(QtCore.Qt.Unchecked)

    assert seen == [(1, False)]


def test_selecting_a_row_enables_the_row_buttons_and_rings_its_marker(widget):
    widget.setTargets(RECORDS)
    widget.setTargetMarkers({2: (3.0, 4.0)})
    assert not widget.goToTargetButton.isEnabled()
    assert len(widget.targetMarkers.points()) == 1
    assert len(widget.selectedTargetMarker.points()) == 0

    widget.targetsTable.selectRow(1)

    assert widget.selectedTargetId() == 2
    assert widget.goToTargetButton.isEnabled()
    assert widget.removeTargetButton.isEnabled()
    assert len(widget.selectedTargetMarker.points()) == 1


def test_row_buttons_emit_the_selected_id(widget):
    seen = []
    widget.sigGoToTarget.connect(lambda i: seen.append(('go', i)))
    widget.sigRemoveTarget.connect(lambda i: seen.append(('remove', i)))
    widget.setTargets(RECORDS)
    widget.targetsTable.selectRow(0)

    widget.goToTargetButton.click()
    widget.removeTargetButton.click()

    assert seen == [('go', 1), ('remove', 1)]


def test_clicks_do_nothing_until_a_click_mode_is_chosen(widget):
    assert widget.getClickMode() == CLICK_NONE
    widget.setClickMode(CLICK_ADD_TARGET)
    assert widget.getClickMode() == CLICK_ADD_TARGET


def test_the_detection_controls_are_gone(widget):
    for name in ('tuneSegmentationButton', 'runCellTargetingButton',
                 'navigateToggle', 'setCellTargetingEnabled'):
        assert not hasattr(widget, name), name
