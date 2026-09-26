"""The SimplePointScan panel's own editing: channels by drag and drop, and
the controls that only the acquisition uses.

Drops are delivered as Qt drop events carrying the panel's laser payload, to
the same drop zones a mouse drag reaches; the real controller answers, so
what is checked is the plan the scan would run.
"""
import pytest
from qtpy import QtCore, QtGui

from imswitch.imcontrol.model.simple_scan import plan_to_dicts
from imswitch.imcontrol.view.widgets.ScanWidgetSimplePointScan import (
    _LaserChip,
    _LaserDropZone,
    _laserMime,
)
from .test_simple_point_scan_controller import Rig

V405, V488, V561 = '405 (ON)', '488 (EXC)', '561 (EXC)'


@pytest.fixture
def rig(qtbot):
    rig = Rig()
    rig.scan.setSimpleScanMode('acquisition')
    yield rig
    rig.close()


def _channels(rig):
    return rig.scan._simple()['acquisition'].channels


def _event(kind, mime):
    actions = QtCore.Qt.CopyAction | QtCore.Qt.MoveAction
    if kind == 'enter':
        return QtGui.QDragEnterEvent(QtCore.QPoint(4, 4), actions, mime,
                                     QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
    return QtGui.QDropEvent(QtCore.QPointF(4, 4), actions, mime,
                            QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)


def _drop(rig, laser, fromLane, zone):
    """A laser dragged from the palette (fromLane None) or a channel onto
    ``zone``: a channel index, 'new' or 'palette'."""
    widget = rig.widget
    target = {'new': lambda: widget.newChannelZone,
              'palette': lambda: widget.paletteZone}.get(zone, lambda: widget.laneZones[zone])()
    mime = _laserMime(laser, fromLane)       # the event does not own it: keep it alive
    event = _event('drop', mime)
    target.dropEvent(event)
    assert event.isAccepted()


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

def test_one_laser_in_two_channels_with_different_partners(rig):
    """405 with 488 in one line pass, 405 with 561 in the next."""
    assert _channels(rig) == ((V405,),)

    _drop(rig, V488, None, 0)
    _drop(rig, V405, None, 'new')
    _drop(rig, V561, None, 1)

    assert _channels(rig) == ((V405, V488), (V405, V561))
    _, digital = plan_to_dicts(rig.scan.currentPlan(), rig.scan._scanLimits())
    assert digital['n_linesteps'] == 2
    assert digital['linestep_enable'][V405] == [True, True]
    assert digital['linestep_enable'][V488] == [True, False]
    assert digital['linestep_enable'][V561] == [False, True]
    shown = [[chip.laser for chip in zone.findChildren(_LaserChip)]
             for zone in rig.widget.laneZones]
    assert shown == [[V405, V488], [V405, V561]]


def test_that_scan_runs_on_the_simulated_rig(rig):
    for laser, zone in ((V488, 0), (V405, 'new'), (V561, 1)):
        _drop(rig, laser, None, zone)
    rig.widget.scanButton.click()
    assert rig.waitForEnd(30)
    assert rig.rejections == []
    assert rig.count('iteration') == 1


def test_a_channel_holds_a_laser_once(rig):
    _drop(rig, V405, None, 0)
    assert _channels(rig) == ((V405,),)


def test_dragging_between_channels_moves_the_laser(rig):
    _drop(rig, V488, None, 'new')                      # ((405,), (488,))
    _drop(rig, V561, None, 1)                          # ((405,), (488, 561))

    _drop(rig, V561, 1, 0)

    assert _channels(rig) == ((V405, V561), (V488,))


def test_moving_the_last_laser_out_of_a_channel_removes_the_channel(rig):
    _drop(rig, V488, None, 'new')
    _drop(rig, V488, 1, 0)
    assert _channels(rig) == ((V405, V488),)


def test_dropping_on_the_palette_or_the_cross_takes_it_out(rig):
    _drop(rig, V488, None, 0)
    _drop(rig, V488, 0, 'palette')
    assert _channels(rig) == ((V405,),)

    (chip,) = rig.widget.laneZones[0].findChildren(_LaserChip)
    chip.removeButton.click()
    assert _channels(rig) == ()
    assert rig.widget.laneZones == []
    rig.scan._refreshEstimate()
    assert 'at least one laser' in rig.widget.estimateNote.text()


def test_a_palette_click_adds_to_the_last_channel(rig):
    _drop(rig, V488, None, 'new')                      # ((405,), (488,))
    rig.widget._dropLaser(V561, None, 'last')
    assert _channels(rig) == ((V405,), (V488, V561))


def test_all_together_and_one_per_laser_use_each_laser_once(rig):
    for laser, zone in ((V488, 0), (V405, 'new'), (V561, 1)):
        _drop(rig, laser, None, zone)

    rig.widget.togetherButton.click()
    assert _channels(rig) == ((V405, V488, V561),)
    rig.widget.separateButton.click()
    assert _channels(rig) == ((V405,), (V488,), (V561,))


def test_a_drop_during_a_drag_waits_until_the_drag_has_returned(rig):
    """The dragged chip is rebuilt by the drop; the drop must not land while
    the chip's drag is still on the stack."""
    rig.widget._setDragging(True)
    _drop(rig, V488, None, 0)
    assert _channels(rig) == ((V405,),)
    rig.widget._setDragging(False)
    assert _channels(rig) == ((V405, V488),)


def test_other_drags_are_not_accepted(qtbot):
    dropped = []
    zone = _LaserDropZone(0, lambda *a: dropped.append(a))
    mime = QtCore.QMimeData()
    mime.setText('405 (ON)')
    enter = _event('enter', mime)
    zone.dragEnterEvent(enter)
    assert not enter.isAccepted()
    drop = _event('drop', mime)
    zone.dropEvent(drop)
    assert dropped == []


# ---------------------------------------------------------------------------
# Controls that only the acquisition uses
# ---------------------------------------------------------------------------

def test_overview_greys_what_only_the_acquisition_uses(rig):
    widget = rig.widget
    rig.scan.setSimpleScanMode('overview')

    assert not widget.regionControls.isEnabled()
    assert not widget.samplingBox.isEnabled()
    assert not widget.pixelSlider.isEnabled() and not widget.dwellSlider.isEnabled()
    assert 'for the acquisition' in widget.samplingBox.title()
    assert widget.regionControls.toolTip()
    assert widget.estimateTitle.text() == 'Frame time'
    # Still usable: drawing the region, the channels, the estimate.
    assert not widget.regionControls.isAncestorOf(widget.drawButton)
    assert widget.newChannelZone.isEnabled()
    assert widget.frameTimeLabel.isEnabled()

    rig.scan.setSimpleScanMode('acquisition')

    assert widget.regionControls.isEnabled() and widget.samplingBox.isEnabled()
    assert widget.samplingBox.title() == 'Sampling'
    assert widget.estimateTitle.text() == 'Scan time'


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
