"""The synthetic sample simulated point detectors image (``mockSample``)."""
import numpy as np
import pytest

from imswitch.imcontrol.model.managers.detectors._mock_sample import (
    BACKGROUND,
    FIELD_HALF_UM,
    FWHM_PER_SIGMA,
    PAIR_SEPARATIONS_UM,
    PRESETS,
    PSF_SIGMA_UM,
    TEXEL_UM,
    MockSample,
    _blur,
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


def test_brightness_interpolates_between_texels():
    sample = MockSample.from_property({'axes': {'X': 1, 'Y': 1}})
    view = sample.view(PSF_SIGMA_UM)
    texture = sample.texture()
    row, col = np.unravel_index(np.argmax(texture), texture.shape)
    x0 = view.origin_um + col * view.texel_um
    y0 = view.origin_um + row * view.texel_um
    # On a texel the lookup is the texel; halfway it is the mean of the two.
    assert sample.brightness(x0, y0) == pytest.approx(texture[row, col])
    half = sample.brightness(x0 + 0.5 * view.texel_um, y0)
    assert half == pytest.approx(0.5 * (texture[row, col] + texture[row, col + 1]))
    assert np.ndim(sample.brightness(x0, y0)) == 0


@pytest.mark.parametrize('preset', PRESETS)
def test_every_preset_is_a_sample_with_its_structures(preset):
    sample = MockSample.from_property({'axes': {'X': 1, 'Y': 1}, 'preset': preset})
    texture = sample.texture()
    assert texture.max() == pytest.approx(1.0)
    assert texture.min() == pytest.approx(BACKGROUND)
    labels, names = sample.labels()
    assert labels.shape == sample.density().shape and labels.dtype == np.uint8
    assert names[0] == 'none'
    expected = {'cells': {'membrane', 'nucleus', 'microtubule', 'vesicle', 'bead'},
                'filaments': {'microtubule', 'actin'},
                'beads': {'bead'},
                'resolution': {'target', 'bead'}}[preset]
    assert expected <= set(names)
    # Every named structure dominates somewhere.
    assert set(np.unique(labels)) == set(range(len(names)))


def test_a_preset_must_exist():
    with pytest.raises(ValueError, match='preset'):
        MockSample.from_property({'axes': {'X': 1}, 'preset': 'jellyfish'})


def test_the_seed_places_the_structures():
    one = MockSample.from_property({'axes': {'X': 1}, 'seed': 1})
    two = MockSample.from_property({'axes': {'X': 1}, 'seed': 2})
    assert not np.array_equal(one.texture(), two.texture())


def test_a_z_axis_is_named_apart_from_the_scanned_axes():
    sample = MockSample.from_property({'axes': {'X': 1, 'Y': 1}, 'zAxis': {'Z': 0.5},
                                       'focusUm': 2.0})
    assert sample.z_axis == ('Z', 0.5) and sample.focus_um == 2.0
    for bad in ({'Z': 0}, {'Z': 1, 'W': 1}, 'Z'):
        with pytest.raises(ValueError, match='zAxis'):
            MockSample.from_property({'axes': {'X': 1}, 'zAxis': bad})


def test_the_psf_is_optics_not_part_of_the_sample():
    """Line pairs of the resolution target: 200 nm apart they merge under the
    default (confocal) PSF and separate under an 80 nm one."""
    sample = MockSample.from_property({'axes': {'X': 1, 'Y': 1}, 'preset': 'resolution'})
    k = PAIR_SEPARATIONS_UM.index(0.2)
    x = -12.1 + 2.2 * k + np.linspace(-0.3, 0.3, 121)
    y = np.full_like(x, 10.0)

    def dip(sigma):
        profile = sample.brightness(x, y, sigma_um=sigma)
        return profile[60] / profile.max()

    assert dip(PSF_SIGMA_UM) > 0.9
    assert dip(0.08 / FWHM_PER_SIGMA) < 0.6


def test_excitation_is_peak_normalised():
    """A point emitter reads the same under any PSF; an extended structure
    collects more under a wider one."""
    sample = MockSample.from_property({'axes': {'X': 1, 'Y': 1}, 'preset': 'beads'})
    density = sample.density().copy()
    density[:] = 0
    n = density.shape[0]
    density[n // 2, n // 2] = 1.0              # one emitter at the origin
    density[n // 4, :] = 1.0                   # a line of them
    narrow, wide = _blur(density, 0.05), _blur(density, 0.2)
    assert narrow.sample(0.0, 0.0) == pytest.approx(wide.sample(0.0, 0.0), rel=0.02)
    y = narrow.origin_um + (n // 4) * TEXEL_UM
    assert wide.sample(0.0, y) == pytest.approx(4 * narrow.sample(0.0, y), rel=0.02)


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
