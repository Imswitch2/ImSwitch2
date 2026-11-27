"""A mock MoNaLISA scan reconstructs to the sample it was rendered from.

The mock camera's frames (``_mock_lattice.py``) are only useful if the real
reconstruction (``improcess.reconstructors.monalisa.pipeline``) turns a scan
of them back into the virtual sample. This runs one: frames from the mock
camera's own frame source over a raster of one lattice period, through
``reconstruct_scan`` with nothing but the nominal step, and compares the
result with the sample at the raster's positions.

The RESOLFT case runs the scan's own ON/OFF/readout TTLs through
``trigger_conditions``: with the OFF step the reconstruction matches the
sample under the narrowed effective PSF, without it the confocal one.
"""
import numpy as np
import pytest

from imswitch.imcontrol.model.managers.detectors._mock_lattice import (
    MockLattice,
    MockLatticeFrames,
    trigger_conditions,
)
from imswitch.improcess.reconstructors.monalisa.pipeline import reconstruct_scan

PIXEL_UM = 0.075
SHAPE = (160, 160)
PERIOD_UM = 0.72
STEPS = 12                        # 12 x 0.06 µm = one period
RATE = 100_000                    # scan samples per second
FWHM = 2.3548                     # per sigma


def _scan(lattice, seed=0, steps=STEPS, shape=SHAPE, conditions=None):
    step_um = PERIOD_UM / steps
    source = MockLatticeFrames(lattice, PIXEL_UM, seed=seed)
    # Fast axis x, then y: frame k is at step (k % steps, k // steps).
    positions = [((k % steps) * step_um, (k // steps) * step_um)
                 for k in range(steps * steps)]
    source.setScan(positions, conditions=conditions)
    frames = np.stack(source.nextFrames(len(positions), shape)).astype(np.float64)
    step = np.diag([step_um / PIXEL_UM, step_um / PIXEL_UM])
    return frames, step


def _cycle_signals(steps, off_ms):
    """A raster with the SimpleMoNaLISA cycle at every position: ON 1 ms,
    OFF ``off_ms`` (none: the confocal mode), readout 1 ms with the camera."""
    on_n, off_n, read_n = RATE // 1000, int(RATE * off_ms / 1000), RATE // 1000
    cycle = on_n + off_n + read_n
    k = np.arange(steps * steps)
    phase = np.arange(cycle)
    ttl = {
        '405 (ON)': np.tile(phase < on_n, k.size),
        '488 (OFF)': np.tile((phase >= on_n) & (phase < on_n + off_n), k.size),
        '488 (EXC)': np.tile(phase >= on_n + off_n, k.size),
    }
    ttl['Camera'] = ttl['488 (EXC)'].copy()
    step_v = PERIOD_UM / steps / 5.0
    return {'scanSignalsDict': {'X': np.repeat((k % steps) * step_v, cycle),
                                'Y': np.repeat((k // steps) * step_v, cycle)},
            'TTLCycleSignalsDict': ttl}


def _truth_on_raster(raster, truth_um, shape=SHAPE):
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
    x_um = (x - 0.5 * (shape[1] - 1)) * PIXEL_UM
    y_um = (y - 0.5 * (shape[0] - 1)) * PIXEL_UM
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
    # The foci are found where they are: the spots move with the specimen by
    # a fraction of a pixel; a wrong lattice fits at ~1 px.
    assert result.geometry.diagnostics['lattice_fit_residual_px'] < 0.4
    truth = _truth_on_raster(result.geometry.raster, lambda x, y: lattice.sample.brightness(
        x, y, sigma_um=lattice.excitation_sigma_um))
    border = 2 * STEPS
    assert _correlation(result.amplitude.image, truth, border) > 0.95


def test_the_resolft_mode_reconstructs_sharper_than_the_confocal_mode():
    """The Siemens star of the resolution target, scanned in both modes of
    the shipped profile. Each reconstruction matches the sample under its
    own effective PSF better than under the other mode's."""
    lattice = MockLattice.from_property({
        'axes': {'X': 5.0, 'Y': 5.0}, 'preset': 'resolution', 'latticePeriodUm': PERIOD_UM,
        'excitation': ['488 (EXC)'],
        'switching': {'on': ['405 (ON)'], 'off': ['488 (OFF)']},
    })
    steps, shape = 18, (128, 128)          # 40 nm steps, as the profile's default
    sigmas, results = {}, {}
    for mode, off_ms in (('confocal', 0.0), ('resolft', 2.0)):
        conditions = trigger_conditions(_cycle_signals(steps, off_ms), 'Camera', lattice,
                                        1.0 / RATE)
        assert len(conditions) == steps * steps
        sigmas[mode] = lattice.effective_sigma_um(conditions[0])[0]
        frames, step = _scan(lattice, steps=steps, shape=shape, conditions=conditions)
        results[mode] = reconstruct_scan(frames, step, (steps, steps))
        assert results[mode].diagnostics['orientation'] == '+x+y'
        assert results[mode].geometry.diagnostics['lattice_fit_residual_px'] < 0.4
    assert FWHM * sigmas['resolft'] < 0.09 and FWHM * sigmas['confocal'] > 0.18

    border = 2 * steps
    for mode, result in results.items():
        other = 'confocal' if mode == 'resolft' else 'resolft'

        def score(sigma):
            truth = _truth_on_raster(result.geometry.raster,
                                     lambda x, y: lattice.sample.brightness(x, y, sigma),
                                     shape)
            return _correlation(result.amplitude.image, truth, border)

        matching, crossed = score(sigmas[mode]), score(sigmas[other])
        assert matching > 0.93, mode
        assert matching > crossed + 0.02, mode


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
