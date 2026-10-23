"""The synthetic sample simulated point detectors image (``mockSample``)."""
import numpy as np
import pytest

from imswitch.imcontrol.model.managers.detectors._mock_sample import (
    BACKGROUND,
    FIELD_HALF_UM,
    MockSample,
)


@pytest.mark.parametrize('value', [None, False])
def test_absent_means_plain_noise(value):
    assert MockSample.from_property(value) is None


@pytest.mark.parametrize('value', [
    'cells', {'axes': ['X', 'Y']}, {'axes': {}}, {'axes': {'X': 0, 'Y': 1.75}},
    {'axes': {'X': 1, 'Y': 1, 'Z': 1}},
])
def test_a_malformed_property_says_what_it_needs(value):
    with pytest.raises(ValueError, match='axes'):
        MockSample.from_property(value)


def test_the_axes_keep_their_order_and_scale():
    sample = MockSample.from_property({'axes': {'Y': 2.0, 'X': 1.75}, 'seed': 3})
    assert sample.axes == (('Y', 2.0), ('X', 1.75))
    assert sample.seed == 3


def test_the_sample_has_structure_on_a_dim_background():
    sample = MockSample.from_property({'axes': {'X': 1.75, 'Y': 1.75}})
    texture = sample.texture()
    assert texture.max() == pytest.approx(1.0)
    assert texture.min() == pytest.approx(BACKGROUND)
    assert np.percentile(texture, 99) > 5 * BACKGROUND
    assert sample.texture() is texture                  # built once


def test_brightness_is_a_lookup_and_background_outside_the_field():
    sample = MockSample.from_property({'axes': {'X': 1.75, 'Y': 1.75}})
    x = np.linspace(-5, 5, 50)
    first = sample.brightness(x, 0.3 * x)
    assert np.array_equal(first, sample.brightness(x, 0.3 * x))
    assert first.shape == x.shape
    outside = sample.brightness(np.array([FIELD_HALF_UM + 1, -FIELD_HALF_UM - 1]), np.zeros(2))
    assert np.all(outside == BACKGROUND)


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
