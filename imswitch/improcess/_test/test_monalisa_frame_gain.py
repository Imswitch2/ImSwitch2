"""Tests for the gain of the frames of a scan."""

import numpy as np
import pytest

from imswitch.improcess.reconstructors.monalisa.frame_gain import fit_frame_gain

SHAPE = (24, 20)


def _scan_gain(first_step=0.12, bleaching=0.08, bow=0.05):
    """What the first recordings showed: a bright first frame in every line,
    a fall over the scan, a bow along the line."""
    num_fast, num_slow = SHAPE
    frame = np.arange(num_fast * num_slow)
    fast, slow = frame % num_fast, frame // num_fast
    u = 2.0 * fast / (num_fast - 1) - 1.0
    gain = (1.0 - bow * u**2) * (1.0 - bleaching * slow / (num_slow - 1))
    gain = gain * (1.0 + first_step * (fast == 0))
    return gain / gain.mean()


def _amplitudes(gain, num_foci=600, noise=5.0, seed=0):
    rng = np.random.default_rng(seed)
    # Every focus sees its own specimen: bright, dim or none, changing with
    # the scan position.
    level = rng.gamma(1.0, 60.0, num_foci) * (rng.random(num_foci) < 0.5)
    specimen = level[None, :] * rng.gamma(4.0, 0.25, (gain.size, num_foci))
    return gain[:, None] * specimen + rng.normal(0.0, noise, specimen.shape)


class TestFitFrameGain:
    def test_recovers_the_gain_of_a_scan(self):
        truth = _scan_gain()
        fitted = fit_frame_gain(_amplitudes(truth), SHAPE)
        assert np.std(truth) > 0.03
        assert np.sqrt(np.mean((fitted.gain - truth) ** 2)) < 0.01
        assert fitted.gain.mean() == pytest.approx(1.0)
        assert fitted.rms == pytest.approx(np.std(truth), rel=0.1)

    def test_the_first_frame_of_a_line(self):
        truth = _scan_gain(first_step=0.12, bleaching=0.0, bow=0.0)
        fitted = fit_frame_gain(_amplitudes(truth), SHAPE)
        first = fitted.gain.reshape(SHAPE[1], SHAPE[0])[:, 0].mean()
        others = fitted.gain.reshape(SHAPE[1], SHAPE[0])[:, 1:].mean()
        assert first / others == pytest.approx(1.12, abs=0.01)

    def test_a_scan_without_a_gain_is_left_alone(self):
        fitted = fit_frame_gain(_amplitudes(np.ones(SHAPE[0] * SHAPE[1])), SHAPE)
        assert fitted.rms < 0.01

    def test_what_a_single_frame_holds_is_not_taken_for_gain(self):
        """The specimen that repeats with the lattice shows as frames that
        differ from their neighbours. The smooth gain does not follow them."""
        rng = np.random.default_rng(1)
        truth = _scan_gain()
        folded = 1.0 + 0.05 * rng.normal(size=truth.size)
        fitted = fit_frame_gain(_amplitudes(truth * folded, noise=1.0), SHAPE)
        assert np.sqrt(np.mean((fitted.gain - truth) ** 2)) < 0.012
        assert fitted.residual_rms == pytest.approx(0.05, abs=0.01)

    def test_foci_that_were_not_extracted_are_left_out(self):
        truth = _scan_gain()
        amplitude = _amplitudes(truth)
        amplitude[:, ::7] = np.nan
        fitted = fit_frame_gain(amplitude, SHAPE)
        assert np.sqrt(np.mean((fitted.gain - truth) ** 2)) < 0.012

    def test_frames_must_make_the_scan(self):
        with pytest.raises(ValueError, match="scan"):
            fit_frame_gain(np.ones((100, 5)), SHAPE)

    def test_no_signal(self):
        with pytest.raises(ValueError, match="signal"):
            fit_frame_gain(np.zeros((SHAPE[0] * SHAPE[1], 5)), SHAPE)


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
