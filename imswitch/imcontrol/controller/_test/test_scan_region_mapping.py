"""Viewer points <-> scanner positions for a live point-scan layer (plan §5.5).

Every rotation and flip of the detector's display transform; a check against
napari's own Image layer transform, so the pixel-centre convention is the
viewer's and not an assumption; rectangles to per-device extents and back.
"""
import itertools

import numpy as np
import pytest

from imswitch.imcontrol.controller.display_transform import (
    DisplayTransform,
    apply_display_transform,
    raw_to_display_point,
)
from imswitch.imcontrol.controller.scan_region_mapping import (
    extents_to_rectangle,
    rectangle_to_extents,
    scanner_to_world,
    world_to_scanner,
)
from imswitch.imcontrol.model.scan_frame import (
    AxisGeometry,
    DisplayedScanGeometry,
    FrameGeometry,
)

X = AxisGeometry('GalvoX', 0.5, 20, -4.75)
Y = AxisGeometry('GalvoY', 0.25, 8, 1.0)
GEOMETRY = FrameGeometry(axes=(X, Y))
RAW_SHAPE = (8, 20)

TRANSFORMS = [
    DisplayTransform(rotation=r, flip_x=fx, flip_y=fy)
    for r, fx, fy in itertools.product((0, 90, 180, 270), (False, True), (False, True))
]


def _label(t):
    return f'rot{t.rotation}-fx{int(t.flip_x)}-fy{int(t.flip_y)}'


def _shown(transform):
    return DisplayedScanGeometry(GEOMETRY, transform, RAW_SHAPE)


def _displayScale(transform):
    scale = [Y.step_um, X.step_um]
    _, displayed = apply_display_transform(np.zeros(RAW_SHAPE), scale, transform)
    return displayed


@pytest.mark.parametrize('transform', TRANSFORMS, ids=_label)
def test_every_pixel_centre_maps_to_its_scanner_position(transform):
    shown = _shown(transform)
    rowScale, columnScale = _displayScale(transform)
    for rawRow, rawColumn in np.ndindex(*RAW_SHAPE):
        displayRow, displayColumn = raw_to_display_point(rawRow, rawColumn, RAW_SHAPE, transform)
        world = (displayRow * rowScale, displayColumn * columnScale)
        positions = world_to_scanner(world, shown)
        assert positions['GalvoX'] == pytest.approx(X.position_um(rawColumn))
        assert positions['GalvoY'] == pytest.approx(Y.position_um(rawRow))


@pytest.mark.parametrize('transform', TRANSFORMS, ids=_label)
def test_the_world_convention_is_napaaris(transform):
    """The world coordinate of a displayed pixel is napari's, not ours."""
    napari = pytest.importorskip('napari')
    raw = np.arange(np.prod(RAW_SHAPE)).reshape(RAW_SHAPE)
    displayed, scale = apply_display_transform(raw, [Y.step_um, X.step_um], transform)
    layer = napari.layers.Image(displayed, scale=scale)
    shown = _shown(transform)
    for index in [(0, 0), (displayed.shape[0] - 1, displayed.shape[1] - 1), (2, 3)]:
        world = layer.data_to_world(index)
        rawRow, rawColumn = np.argwhere(raw == displayed[index])[0]
        positions = world_to_scanner(world, shown)
        assert positions['GalvoX'] == pytest.approx(X.position_um(rawColumn))
        assert positions['GalvoY'] == pytest.approx(Y.position_um(rawRow))


@pytest.mark.parametrize('transform', TRANSFORMS, ids=_label)
def test_scanner_positions_round_trip(transform):
    shown = _shown(transform)
    rng = np.random.default_rng(1)
    for x, y in zip(rng.uniform(-5, 5, 20), rng.uniform(0.9, 2.9, 20)):
        back = world_to_scanner(scanner_to_world({'GalvoX': x, 'GalvoY': y}, shown), shown)
        assert back['GalvoX'] == pytest.approx(x)
        assert back['GalvoY'] == pytest.approx(y)


@pytest.mark.parametrize('transform', TRANSFORMS, ids=_label)
def test_a_rectangle_over_pixels_spans_their_edges(transform):
    """Drawn over pixels 2..5 (columns) and 1..3 (rows), the region spans the
    pixels' outer edges, whatever the display rotation or flip."""
    shown = _shown(transform)
    extents = {
        'GalvoX': (X.position_um(2 - 0.5), X.position_um(5 + 0.5)),
        'GalvoY': (Y.position_um(1 - 0.5), Y.position_um(3 + 0.5)),
    }
    corners = extents_to_rectangle(extents, shown)
    back = rectangle_to_extents(corners, shown)
    for device in extents:
        assert back[device] == pytest.approx(extents[device])


def test_a_single_axis_frame_maps_its_one_axis():
    line = FrameGeometry(axes=(X,))
    shown = DisplayedScanGeometry(line, DisplayTransform(), (1, 20))
    assert world_to_scanner((0.0, 3 * X.step_um), shown) == {'GalvoX': X.position_um(3)}
    corners = extents_to_rectangle({'GalvoX': (-1.0, 2.0)}, shown)
    assert rectangle_to_extents(corners, shown)['GalvoX'] == pytest.approx((-1.0, 2.0))


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
