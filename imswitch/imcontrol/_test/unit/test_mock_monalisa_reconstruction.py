"""A mock MoNaLISA scan reconstructs to the sample it was rendered from.

The mock camera's frames (``_mock_lattice.py``) are only useful if the real
reconstruction (``improcess.reconstructors.monalisa.pipeline``) turns a scan
of them back into the virtual sample. This runs one: frames from the mock
camera's own frame source over a raster of one lattice period, through
``reconstruct_scan`` with nothing but the nominal step, and compares the
result with the sample at the raster's positions.
"""
import numpy as np
import pytest

from imswitch.imcontrol.model.managers.detectors._mock_lattice import (
    MockLattice,
    MockLatticeFrames,
)
from imswitch.improcess.reconstructors.monalisa.pipeline import reconstruct_scan

PIXEL_UM = 0.075
SHAPE = (160, 160)
PERIOD_UM = 0.72
STEPS = 12                        # 12 x 0.06 µm = one period


def _scan(lattice, seed=0):
    step_um = PERIOD_UM / STEPS
    source = MockLatticeFrames(lattice, PIXEL_UM, seed=seed)
    # Fast axis x, then y: frame k is at step (k % STEPS, k // STEPS).
    positions = [((k % STEPS) * step_um, (k // STEPS) * step_um)
                 for k in range(STEPS * STEPS)]
    source.setScan(positions)
    frames = np.stack(source.nextFrames(len(positions), SHAPE)).astype(np.float64)
    step = np.diag([step_um / PIXEL_UM, step_um / PIXEL_UM])
    return frames, step


def _truth_on_raster(lattice, raster, truth_um):
    """The sample at every output pixel, as the reconstruction places it.

    The raster is in camera pixels at the first frame: a sample point there
    is ``focus - stage`` with the stage at 0, so camera pixel (x, y) holds the
    sample at ``(x - (cols - 1) / 2) * pixel``.
    """
    rows, cols = raster.shape
    gy, gx = np.mgrid[0:rows, 0:cols].astype(float)
    step = np.asarray(raster.step, dtype=float)
    x = raster.origin[0] + step[0, 0] * gx + step[0, 1] * gy
    y = raster.origin[1] + step[1, 0] * gx + step[1, 1] * gy
    x_um = (x - 0.5 * (SHAPE[1] - 1)) * PIXEL_UM
    y_um = (y - 0.5 * (SHAPE[0] - 1)) * PIXEL_UM
    return truth_um(x_um, y_um)


def _correlation(image, truth, border):
    inner = (slice(border, -border), slice(border, -border))
    image, truth = image[inner], truth[inner]
    finite = np.isfinite(image)
    assert finite.mean() > 0.5
    return float(np.corrcoef(image[finite], truth[finite])[0, 1])


@pytest.fixture(scope='module')
def lattice():
    lattice = MockLattice.from_property(
        {'axes': {'X': 5.0, 'Y': 5.0}, 'latticePeriodUm': PERIOD_UM, 'seed': 0}
    )
    lattice.sample.texture()
    return lattice


def test_a_mock_scan_reconstructs_to_the_sample(lattice):
    frames, step = _scan(lattice)
    result = reconstruct_scan(frames, step, (STEPS, STEPS))

    diagnostics = result.diagnostics
    assert diagnostics['orientation'] == '+x+y'
    assert result.geometry.lattice.nearest_spacing() == pytest.approx(
        PERIOD_UM / PIXEL_UM, rel=0.01
    )
    truth = _truth_on_raster(lattice, result.geometry.raster, lattice.sample.brightness)
    border = 2 * STEPS
    assert _correlation(result.amplitude.image, truth, border) > 0.9


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
