"""Mapping points through a detector's display transform, both ways.

A rectangle drawn over a rotated or flipped live image has to be mapped back
to the raw scan pixels before it means anything to the scanners
(docs/simple-point-scan-plan.md, D1). These tests hold the point mappings to
``apply_display_transform`` itself: every pixel of a test image must be found
where the forward mapping says, and the inverse must return it.
"""
import itertools

import numpy as np
import pytest

from imswitch.imcontrol.controller.display_transform import (
    DisplayTransform,
    apply_display_transform,
    display_to_raw_point,
    raw_to_display_point,
)

TRANSFORMS = [
    DisplayTransform(rotation=rotation, flip_x=flip_x, flip_y=flip_y)
    for rotation, flip_x, flip_y in itertools.product(
        (0, 90, 180, 270), (False, True), (False, True)
    )
]


def _label(transform):
    return f'rot{transform.rotation}-fx{int(transform.flip_x)}-fy{int(transform.flip_y)}'


@pytest.mark.parametrize('transform', TRANSFORMS, ids=_label)
@pytest.mark.parametrize('shape', [(3, 5), (4, 4), (1, 6), (2, 3, 5)])
def test_every_pixel_is_where_the_forward_mapping_says(transform, shape):
    raw = np.arange(int(np.prod(shape))).reshape(shape)
    displayed, _ = apply_display_transform(raw, None, transform)

    for index in np.ndindex(*shape[-2:]):
        r, c = raw_to_display_point(*index, shape, transform)
        assert (r, c) == (int(r), int(c))
        assert displayed[..., int(r), int(c)].tolist() == raw[..., index[0], index[1]].tolist()


@pytest.mark.parametrize('transform', TRANSFORMS, ids=_label)
@pytest.mark.parametrize('shape', [(3, 5), (4, 4), (1, 6)])
def test_every_displayed_pixel_maps_back_to_its_raw_pixel(transform, shape):
    raw = np.arange(int(np.prod(shape))).reshape(shape)
    displayed, _ = apply_display_transform(raw, None, transform)

    for index in np.ndindex(*displayed.shape[-2:]):
        r, c = display_to_raw_point(*index, shape, transform)
        assert displayed[index] == raw[int(r), int(c)]


@pytest.mark.parametrize('transform', TRANSFORMS, ids=_label)
def test_continuous_points_round_trip(transform):
    """A rectangle corner lies anywhere inside a pixel, not on its centre."""
    shape = (7, 11)
    rng = np.random.default_rng(0)
    for row, col in rng.uniform(-0.5, [6.5, 10.5], size=(50, 2)):
        back = display_to_raw_point(
            *raw_to_display_point(row, col, shape, transform), shape, transform
        )
        assert back == pytest.approx((row, col), abs=1e-12)


def test_identity_leaves_points_alone():
    assert raw_to_display_point(1.25, 3.5, (4, 6), DisplayTransform()) == (1.25, 3.5)
    assert display_to_raw_point(1.25, 3.5, (4, 6), DisplayTransform()) == (1.25, 3.5)


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
