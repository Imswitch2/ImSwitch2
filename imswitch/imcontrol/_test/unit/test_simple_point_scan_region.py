"""The SimplePointScan rectangle in the viewer (plan P2, §5.5, D1 point 6).

The real controller and panel on the simulated rig, with napari's
``ViewerModel`` as the viewer: the panel's own Shapes layer, no OpenGL. The
rectangle is drawn the way napari reports a drawing (a shape added, or data
changed at the end of a drag), and read back from the layer. What the image
layer shows is announced through ``sigScanGeometryShown``, as
``ImageController`` does once the pixels are on the layer.
"""
import dataclasses

import numpy as np
import pytest
from qtpy import QtTest

from imswitch.imcontrol.controller.display_transform import DisplayTransform
from imswitch.imcontrol.controller.scan_region_mapping import (
    extents_to_rectangle,
    rectangle_to_extents,
)
from imswitch.imcontrol.model.scan_frame import (
    AxisGeometry,
    DisplayedScanGeometry,
    FrameGeometry,
    frame_geometry_of,
)
from imswitch.imcontrol.model.managers.detectors._mock_sample import MockSample
from imswitch.imcontrol.model.simple_scan import AxisRegion, plan_to_dicts
from .test_simple_point_scan_controller import Rig, _edit

napari = pytest.importorskip('napari')


@pytest.fixture
def viewer():
    from napari.components import ViewerModel
    return ViewerModel()


@pytest.fixture
def rig(qtbot, viewer):
    rig = Rig(viewer=viewer)
    rig.drawn = []
    rig.widget.sigRegionDrawn.connect(rig.drawn.append)
    yield rig
    rig.close()


def _overviewShown(rig, transform=DisplayTransform(), run=1):
    """The geometry an overview frame carries (checked against a real frame
    in test_a_real_overview_frame_is_what_the_rectangle_is_drawn_on)."""
    overview = rig.scan._simple()['overview']
    axes = tuple(
        AxisGeometry(name, overview.step_um, overview.pixels,
                     centre - (overview.pixels - 1) / 2.0 * overview.step_um)
        for name, centre in zip(overview.axes, overview.centres_um)
    )
    return DisplayedScanGeometry(
        FrameGeometry(axes=axes, run=run), transform, (overview.pixels, overview.pixels))


def _layer(rig):
    return rig.widget._regionOverlay.layer


def _showing(rig):
    layer = _layer(rig)
    return layer is not None and len(layer.data) > 0


def _shownExtents(rig, shown):
    layer = _layer(rig)
    assert len(layer.data) == 1
    return rectangle_to_extents(np.asarray(layer.data[0]), shown)


def _regionExtents(plan, device):
    region = plan.regions[device]
    return (region.center_um - region.length_um / 2, region.center_um + region.length_um / 2)


def _draw(rig, qtbot, extents, shown):
    """Draw a rectangle over these scanner extents, as a user would."""
    rig.widget.drawButton.click()
    corners = np.asarray(extents_to_rectangle(extents, shown))
    before = len(rig.drawn)
    _layer(rig).add_rectangles([corners])
    qtbot.waitUntil(lambda: len(rig.drawn) > before, timeout=2000)
    QtTest.QTest.qWait(20)


# ---------------------------------------------------------------------------
# When a rectangle can be drawn
# ---------------------------------------------------------------------------

def test_drawing_waits_for_the_reference_detector_to_show_a_scanned_image(rig):
    button = rig.widget.drawButton
    assert not button.isEnabled()
    assert 'start the overview' in button.toolTip()

    rig.channel.sigScanGeometryShown.emit('APD 2', _overviewShown(rig))
    assert not button.isEnabled()                # not the layer it is drawn on

    rig.channel.sigScanGeometryShown.emit('APD', _overviewShown(rig))
    assert button.isEnabled()

    rig.channel.sigScanGeometryShown.emit('APD', None)   # a frame without one
    assert not button.isEnabled()


def test_without_a_viewer_there_is_nothing_to_draw_on(qtbot):
    rig = Rig()
    try:
        rig.channel.sigScanGeometryShown.emit('APD', _overviewShown(rig))
        assert not rig.widget.drawButton.isEnabled()
        assert 'no image viewer' in rig.widget.drawButton.toolTip()
    finally:
        rig.close()


def test_the_overview_shows_no_rectangle_until_draw_is_clicked(rig):
    """The overview scans its own field: a region shows only once asked for."""
    rig.channel.sigScanGeometryShown.emit('APD', _overviewShown(rig))
    assert not _showing(rig)

    rig.widget.drawButton.click()
    assert _showing(rig)

    rig.widget.overviewButton.click()            # back to Overview: gone again
    assert not _showing(rig)

    rig.widget.acquisitionButton.click()         # Acquisition always shows it
    assert _showing(rig)
    rig.widget.overviewButton.click()
    assert not _showing(rig)


def test_the_reference_choice_lists_the_point_detectors(rig):
    combo = rig.widget.referenceCombo
    assert [combo.itemText(i) for i in range(combo.count())] == ['APD', 'APD 2']
    assert combo.currentText() == 'APD'
    assert not combo.isHidden()


# ---------------------------------------------------------------------------
# Rectangle -> region
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('transform', [
    DisplayTransform(),
    DisplayTransform(rotation=90, flip_x=True),
    DisplayTransform(rotation=270, flip_y=True),
], ids=['identity', 'rot90-flipx', 'rot270-flipy'])
def test_a_drawn_rectangle_becomes_the_acquisition_region(rig, qtbot, transform):
    shown = _overviewShown(rig, transform)
    rig.channel.sigScanGeometryShown.emit('APD', shown)
    step = rig.scan._simple()['acquisition'].regions['X'].step_um

    _draw(rig, qtbot, {'X': (-6.1, 2.3), 'Y': (1.05, 9.4)}, shown)

    plan = rig.scan._simple()['acquisition']
    assert plan.regions['X'].center_um == pytest.approx(-1.9, abs=1e-3)
    assert plan.regions['Y'].center_um == pytest.approx(5.225, abs=1e-3)
    for device, drawn in (('X', 8.4), ('Y', 8.35)):
        region = plan.regions[device]
        assert region.pixels * region.step_um == pytest.approx(region.length_um)
        assert abs(region.length_um - drawn) <= step / 2 + 1e-3
    assert rig.scan._simple()['mode'] == 'acquisition'
    assert rig.widget.acquisitionButton.isChecked()
    # Shown again as the region really is: snapped, one rectangle, ready to move.
    assert _shownExtents(rig, shown) == {
        device: pytest.approx(_regionExtents(plan, device)) for device in ('X', 'Y')}
    assert str(_layer(rig).mode) == 'select'


def test_moving_the_rectangle_moves_the_region(rig, qtbot):
    rig.scan.setSimpleScanMode('acquisition')
    shown = _overviewShown(rig)
    rig.channel.sigScanGeometryShown.emit('APD', shown)
    before = rig.scan._simple()['acquisition']
    corners = np.asarray(_layer(rig).data[0])

    count = len(rig.drawn)
    _layer(rig).data = [corners + np.array([2.0, -3.0])]    # a drag: data changed
    qtbot.waitUntil(lambda: len(rig.drawn) > count, timeout=2000)

    after = rig.scan._simple()['acquisition']
    assert after.regions['Y'].center_um == pytest.approx(before.regions['Y'].center_um + 2.0, abs=1e-3)
    assert after.regions['X'].center_um == pytest.approx(before.regions['X'].center_um - 3.0, abs=1e-3)
    assert after.regions['X'].length_um == before.regions['X'].length_um


def test_a_rectangle_beyond_the_scanners_reach_is_cut_to_it(rig, qtbot):
    shown = _overviewShown(rig)
    rig.channel.sigScanGeometryShown.emit('APD', shown)
    reach = rig.scan._scanLimits().axis('X').range_um

    _draw(rig, qtbot, {'X': (reach[1] - 4.0, reach[1] + 50.0), 'Y': (-2.0, 2.0)}, shown)

    low, high = _regionExtents(rig.scan._simple()['acquisition'], 'X')
    assert high <= reach[1] + rig.scan._simple()['acquisition'].regions['X'].step_um
    assert high - low == pytest.approx(4.0, abs=0.5)


def test_an_xz_scan_drawn_on_the_xy_overview_parks_y_at_the_rectangle(rig, qtbot):
    plan = rig.scan._simple()['acquisition']
    z = AxisRegion(0.0, 2.0, 0.5)
    _edit(rig, dims=('X', 'Z'), regions={'X': plan.regions['X'], 'Z': z})
    rig.scan.setSimpleScanMode('acquisition')
    shown = _overviewShown(rig)
    rig.channel.sigScanGeometryShown.emit('APD', shown)
    # Y is not scanned: the rectangle is a line at Y's current position.
    ((_, y0), ) = [(d, e) for d, e in _shownExtents(rig, shown).items() if d == 'Y']
    assert y0[0] == pytest.approx(y0[1])

    _draw(rig, qtbot, {'X': (-4.0, 4.0), 'Y': (3.0, 7.0)}, shown)

    plan = rig.scan._simple()['acquisition']
    assert plan.dims == ('X', 'Z')
    assert plan.regions['X'].center_um == pytest.approx(0.0, abs=1e-3)
    assert plan.regions['Z'] == z
    assert plan.park['Y'] == pytest.approx(5.0)
    assert rig.scan.currentPlan().park['Y'] == pytest.approx(5.0)
    analog, _ = plan_to_dicts(rig.scan.currentPlan(), rig.scan._scanLimits())
    y = analog['target_device'].index('Y')
    assert analog['axis_centerpos'][y] == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Region -> rectangle
# ---------------------------------------------------------------------------

def test_editing_the_numbers_moves_the_rectangle(rig):
    rig.scan.setSimpleScanMode('acquisition')
    shown = _overviewShown(rig)
    rig.channel.sigScanGeometryShown.emit('APD', shown)
    plan = rig.scan._simple()['acquisition']
    x = plan.regions['X']

    _edit(rig, regions={**plan.regions, 'X': dataclasses.replace(x, center_um=x.center_um + 3.0)})

    plan = rig.scan._simple()['acquisition']
    assert _shownExtents(rig, shown)['X'] == pytest.approx(_regionExtents(plan, 'X'))
    assert rig.drawn == []                  # showing it is not drawing it


def test_the_rectangle_stays_put_when_the_image_under_it_changes(rig):
    """Overview, then the acquired region itself: other world corners, the
    same scanner positions."""
    rig.scan.setSimpleScanMode('acquisition')
    overview = _overviewShown(rig)
    rig.channel.sigScanGeometryShown.emit('APD', overview)
    before = np.asarray(_layer(rig).data[0])
    plan = rig.scan._simple()['acquisition']
    x, y = plan.regions['X'], plan.regions['Y']
    acquired = DisplayedScanGeometry(FrameGeometry(axes=(
        AxisGeometry('X', x.step_um, x.pixels, x.center_um - (x.pixels - 1) / 2 * x.step_um),
        AxisGeometry('Y', y.step_um, y.pixels, y.center_um - (y.pixels - 1) / 2 * y.step_um),
    )), DisplayTransform(), (y.pixels, x.pixels))

    rig.channel.sigScanGeometryShown.emit('APD', acquired)

    after = np.asarray(_layer(rig).data[0])
    assert not np.allclose(before, after)
    extents = _shownExtents(rig, acquired)
    assert extents['X'] == pytest.approx(_regionExtents(plan, 'X'))
    assert extents['Y'] == pytest.approx(_regionExtents(plan, 'Y'))
    # The whole acquired image: from the first pixel's outer edge.
    assert after[:, 1].min() == pytest.approx(-x.step_um / 2)


def test_changing_the_reference_redraws_with_that_layers_geometry(rig):
    rig.scan.setSimpleScanMode('acquisition')
    plain = _overviewShown(rig)
    turned = _overviewShown(rig, DisplayTransform(rotation=90))
    rig.channel.sigScanGeometryShown.emit('APD', plain)
    rig.channel.sigScanGeometryShown.emit('APD 2', turned)
    onApd = np.asarray(_layer(rig).data[0])

    rig.widget.referenceCombo.setCurrentIndex(1)

    assert rig.scan._simple()['reference'] == 'APD 2'
    onApd2 = np.asarray(_layer(rig).data[0])
    assert not np.allclose(onApd, onApd2)
    plan = rig.scan._simple()['acquisition']
    assert _shownExtents(rig, turned) == {
        device: pytest.approx(_regionExtents(plan, device)) for device in ('X', 'Y')}


def test_a_new_live_frame_in_the_same_place_does_not_redraw(rig, monkeypatch):
    rig.channel.sigScanGeometryShown.emit('APD', _overviewShown(rig, run=1))
    calls = []
    monkeypatch.setattr(rig.widget, 'showRegion', calls.append)
    for run in (2, 3, 4):
        rig.channel.sigScanGeometryShown.emit('APD', _overviewShown(rig, run=run))
    assert calls == []


def test_the_reference_is_saved_with_the_panel(qtbot, rig):
    rig.widget.referenceCombo.setCurrentIndex(1)
    state = rig.scan.getComponentState()
    assert state['simplePlan']['reference'] == 'APD 2'


# ---------------------------------------------------------------------------
# On a running overview, with real frames
# ---------------------------------------------------------------------------

def test_a_real_overview_frame_is_what_the_rectangle_is_drawn_on(rig, qtbot):
    """The frame's own geometry, announced as ImageController does; drawing
    while the overview runs sets the region and leaves the overview running."""
    frames = []
    rig.master.detectorsManager['APD'].sigImageUpdated.connect(
        lambda im, init, scale: frames.append(im))
    rig.widget.scanButton.click()
    try:
        qtbot.waitUntil(lambda: any(frame_geometry_of(f) for f in frames), timeout=10000)
        frame = next(f for f in frames if frame_geometry_of(f) is not None)
        geometry = frame_geometry_of(frame)
        for real, expected in zip(geometry.axes, _overviewShown(rig).geometry.axes):
            assert (real.device, real.count) == (expected.device, expected.count)
            assert (real.step_um, real.first_um) == pytest.approx(
                (expected.step_um, expected.first_um))
        shown = DisplayedScanGeometry(geometry, DisplayTransform(), tuple(frame.shape))
        rig.channel.sigScanGeometryShown.emit('APD', shown)

        _draw(rig, qtbot, {'X': (-3.0, 3.0), 'Y': (-2.0, 2.0)}, shown)

        plan = rig.scan._simple()['acquisition']
        assert plan.regions['X'].center_um == pytest.approx(0.0, abs=1e-3)
        assert rig.scan._simple()['mode'] == 'overview'
        assert 'Stop the overview' in rig.widget.messageLabel.text()
    finally:
        rig.widget.stopButton.click()
        assert rig.waitForEnd()


def _sampleUnder(geometry):
    """The mock sample (the setup's ``mockSample``) at each pixel's centre."""
    sample = MockSample.from_property({'axes': {'X': 1.75, 'Y': 1.75}})
    x, y = geometry.axes
    xx, yy = np.meshgrid(x.first_um + np.arange(x.count) * x.step_um,
                         y.first_um + np.arange(y.count) * y.step_um)
    return sample.brightness(xx, yy)


def _correlation(image, expected):
    return np.corrcoef(np.asarray(image, float).ravel(), np.asarray(expected, float).ravel())[0, 1]


def _lastFrame(frames):
    """The last frame published: after the run ends, a finished one."""
    return next(f for f in reversed(frames) if frame_geometry_of(f) is not None)


def test_the_acquisition_images_the_region_drawn_on_the_overview(rig, qtbot):
    """End to end on the simulated rig, whose APDs image a synthetic sample
    from the scan waveforms: the overview shows the sample, and the region
    drawn on it is the part of the sample the acquisition then shows."""
    frames = []
    rig.master.detectorsManager['APD'].sigImageUpdated.connect(
        lambda im, init, scale: frames.append(im))
    rig.widget.scanButton.click()
    qtbot.waitUntil(lambda: rig.count('iteration') >= 1, timeout=15000)
    rig.widget.stopButton.click()
    assert rig.waitForEnd()
    overview = _lastFrame(frames)
    geometry = frame_geometry_of(overview)
    assert _correlation(overview, _sampleUnder(geometry)) > 0.9
    shown = DisplayedScanGeometry(geometry, DisplayTransform(), tuple(overview.shape))
    rig.channel.sigScanGeometryShown.emit('APD', shown)

    _draw(rig, qtbot, {'X': (-6.0, 2.0), 'Y': (-9.0, -1.0)}, shown)
    rig.events.clear()
    frames.clear()
    rig.widget.scanButton.click()
    assert rig.waitForEnd(30)

    acquired = _lastFrame(frames)
    geometry = frame_geometry_of(acquired)
    for axis, (low, high) in zip(geometry.axes, ((-6.0, 2.0), (-9.0, -1.0))):
        edge = axis.first_um - axis.step_um / 2
        assert edge == pytest.approx(low, abs=axis.step_um)
        assert edge + axis.count * axis.step_um == pytest.approx(high, abs=axis.step_um)
    assert _correlation(acquired, _sampleUnder(geometry)) > 0.9
    # ...and not merely some part of the sample: 3 µm off, it does not match.
    elsewhere = FrameGeometry(axes=tuple(
        dataclasses.replace(axis, first_um=axis.first_um + 3.0) for axis in geometry.axes))
    assert _correlation(acquired, _sampleUnder(elsewhere)) < 0.5


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
