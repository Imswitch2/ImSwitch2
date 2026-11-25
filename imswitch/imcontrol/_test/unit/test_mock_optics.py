"""What the simulated point detectors see of the virtual sample (``_mock_optics``).

The scan's own signals decide it: the scanner waveforms place the beam, the
lasers' TTLs (and analog power, when the scan carries it) light the sample, a
z waveform defocuses it, and photobleaching follows the order the scan
exposes the sample in.
"""
import numpy as np
import pytest

from imswitch.imcontrol.model.managers.detectors._mock_optics import (
    Bleaching,
    PointScanOptics,
    PointScanSignal,
    defocus,
    laser_light,
    parse_lasers,
)
from imswitch.imcontrol.model.managers.detectors._mock_sample import (
    BACKGROUND,
    PSF_SIGMA_UM,
    MockSample,
)
from imswitch.imcontrol.model.managers.detectors.APDManager import APDManager
from imswitch.imcontrol.model.managers.detectors.APDManager import ScanWorker as APDScanWorker
from imswitch.imcontrol.model.managers.detectors.PMTManager import PMTManager
from imswitch.imcontrol.model.managers.detectors.PMTManager import ScanWorker as PMTScanWorker
from imswitch.imcontrol.model.managers.NidaqManager import NidaqManager

from .test_mock_scan_simulation_coordinator import (
    _minimal_simulated_setup,
    _slow_detector_scan_info,
)

AXES = {'X': 5.0, 'Y': 5.0}


@pytest.fixture(scope='module')
def sample():
    sample = MockSample.from_property({'axes': AXES, 'preset': 'beads'})
    sample.prepare()
    return sample


@pytest.fixture(scope='module')
def brightest(sample):
    """The brightest point of the sample (a bead) and a dark one, µm."""
    texture = sample.texture()
    row, col = np.unravel_index(np.argmax(texture), texture.shape)
    view = sample.view(PSF_SIGMA_UM)
    bright = (view.origin_um + col * view.texel_um, view.origin_um + row * view.texel_um)
    return bright, (29.0, -29.0)


def _parked(at_um, n, ttl=None, z_volts=None):
    """Signals that hold the beam at one point for ``n`` scan samples."""
    signals = {'scanSignalsDict': {'X': np.full(n, at_um[0] / 5.0),
                                   'Y': np.full(n, at_um[1] / 5.0)},
               'TTLCycleSignalsDict': {}}
    if ttl is not None:
        signals['TTLCycleSignalsDict']['488'] = ttl
    if z_volts is not None:
        signals['scanSignalsDict']['Z'] = np.full(n, z_volts)
    return signals


# ---------------------------------------------------------------------------
# The pieces
# ---------------------------------------------------------------------------

def test_lasers_are_named_with_or_without_efficiency_and_full_scale():
    assert [l.name for l in parse_lasers(['488', '561'], 'excitation')] == ['488', '561']
    laser, = parse_lasers({'488': {'efficiency': 0.5, 'fullScaleV': 5.0}}, 'excitation')
    assert (laser.efficiency, laser.full_scale_v) == (0.5, 5.0)
    assert parse_lasers(None, 'excitation') == ()
    with pytest.raises(ValueError, match='excitation'):
        parse_lasers(3, 'excitation')


def test_laser_light_follows_ttl_efficiency_and_analog_power():
    ttl = np.array([0, 1, 1, 0, 1, 1], dtype=bool)
    signals = {'TTLCycleSignalsDict': {'488': ttl},
               'scanSignalsDict': {'488': np.array([0, 5, 2.5, 0, 5, 5])}}
    lasers = parse_lasers({'488': {'efficiency': 0.5, 'fullScaleV': 5.0}}, 'excitation')
    assert laser_light(signals, lasers, 6) == pytest.approx([0, 0.5, 0.25, 0, 0.5, 0.5])
    # Without a full scale the power waveform cannot be read: TTL only.
    assert laser_light(signals, parse_lasers({'488': 1.0}, 'x'), 6) == \
        pytest.approx(ttl.astype(float))
    # A laser the scan does not switch: left on by hand, or a pulse not fired.
    assert laser_light({}, parse_lasers(['405'], 'x'), 3) == pytest.approx([1, 1, 1])
    assert laser_light({}, parse_lasers(['405'], 'x'), 3, unswitched_on=False) == \
        pytest.approx([0, 0, 0])
    assert laser_light(signals, (), 6) is None


def test_defocus_widens_the_psf_and_lowers_its_peak():
    sigma, peak = defocus(np.array([0.0, 0.4]), 0.1, 0.4)
    assert sigma == pytest.approx([0.1, 0.1 * np.sqrt(2)])
    assert peak == pytest.approx([1.0, 0.5])


def test_bleaching_halves_what_one_half_life_exposed():
    bleaching = Bleaching(half_life_s=2.0)
    assert bleaching.survival([0.0, 5.0], [0.0, 5.0]) == pytest.approx([1.0, 1.0])
    bleaching.expose([0.0], [0.0], [2.0])
    left = bleaching.survival([0.0, 5.0], [0.0, 5.0])
    assert left == pytest.approx([0.5, 1.0])


def test_point_scan_optics_read_their_keys():
    optics = PointScanOptics.from_property({
        'axes': AXES, 'psfFwhmUm': 0.47, 'rayleighRangeUm': 0.8,
        'excitation': ['488'], 'bleachHalfLifeS': 3, 'darkCountRate': 100})
    assert optics.psf_sigma_um == pytest.approx(0.2, rel=1e-2)
    assert (optics.rayleigh_um, optics.bleach_half_life_s, optics.dark_count_rate) == \
        (0.8, 3.0, 100.0)
    assert PointScanOptics.from_property({'axes': AXES}) == PointScanOptics()
    with pytest.raises(ValueError):
        PointScanOptics.from_property({'axes': AXES, 'bleachHalfLifeS': 0})


# ---------------------------------------------------------------------------
# The signal under the beam
# ---------------------------------------------------------------------------

def test_the_signal_is_the_sample_under_the_beam(sample):
    x = np.linspace(-20, 20, 400)
    signals = {'scanSignalsDict': {'X': x / 5.0, 'Y': np.full(400, 0.6)}}
    signal = PointScanSignal(sample, PointScanOptics(), signals, 1, 1e6)
    assert signal.usable
    assert signal(0, 400) == pytest.approx(sample.brightness(x, np.full(400, 3.0)))
    # A wider PSF is a different (blurrier) view of the same sample.
    wide = PointScanSignal(sample, PointScanOptics(psf_sigma_um=0.3), signals, 1, 1e6)
    assert wide(0, 400) == pytest.approx(sample.brightness(x, np.full(400, 3.0), 0.3))
    assert not PointScanSignal(sample, PointScanOptics(), {}, 1, 1e6).usable


def test_the_lasers_ttl_lights_the_sample(sample, brightest):
    bright, _ = brightest
    ttl = np.array([1, 1, 0, 0], dtype=bool)
    optics = PointScanOptics(excitation=parse_lasers(['488'], 'excitation'))
    signal = PointScanSignal(sample, optics, _parked(bright, 4, ttl), 1, 1e6)
    lit = signal(0, 4)
    assert lit[:2] == pytest.approx(sample.brightness(*bright))
    assert lit[2:] == pytest.approx([0.0, 0.0])


def test_defocus_dims_a_bead_as_a_confocal_does(sample, brightest):
    bright, _ = brightest
    z = MockSample.from_property({'axes': AXES, 'preset': 'beads', 'zAxis': {'Z': 1.0},
                                  'focusUm': 2.0})
    focused = PointScanSignal(z, PointScanOptics(), _parked(bright, 1, z_volts=2.0), 1, 1e6)
    away = PointScanSignal(z, PointScanOptics(), _parked(bright, 1, z_volts=2.4), 1, 1e6)
    inFocus, outOfFocus = focused(0, 1)[0], away(0, 1)[0]
    assert inFocus == pytest.approx(sample.brightness(*bright))
    # One Rayleigh range out: excitation peak and pinhole each halve it.
    assert outOfFocus - BACKGROUND == pytest.approx((inFocus - BACKGROUND) / 4, rel=0.2)


def test_scanning_twice_bleaches_in_scan_order(sample, brightest):
    bright, _ = brightest
    bleaching = Bleaching(half_life_s=1e-3)
    optics = PointScanOptics(bleach_half_life_s=1e-3)
    signal = PointScanSignal(sample, optics, _parked(bright, 2000), 1, 1e6, bleaching)
    # Each read (a line, in a scan) sees what the reads before it left.
    first = signal(0, 1000)          # 1 ms at full light: one half life
    second = signal(1000, 1000)
    assert first == pytest.approx(sample.brightness(*bright))
    emitters = (first[0] - BACKGROUND)
    assert second[0] - BACKGROUND == pytest.approx(emitters / 2, rel=0.02)


# ---------------------------------------------------------------------------
# In the detectors
# ---------------------------------------------------------------------------

def _apd(mockSample, **props):
    setup = _minimal_simulated_setup({'APD': {
        'analogChannel': None, 'digitalLine': None, 'managerName': 'APDManager',
        'managerProperties': {'ctrInputLine': 'Dev1/ctr0', 'terminal': '/Dev1/PFI0',
                              'mockRandomSeed': 1, 'mockSample': mockSample, **props},
        'forAcquisition': True}})
    nidaq = NidaqManager(setup)
    return APDManager(setup.detectors['APD'], 'APD', nidaq)


def _apd_pixels(apd, signals):
    worker = APDScanWorker(apd, _slow_detector_scan_info(), {'TTLCycleSignalsDict': {},
                                                              **signals})
    data = worker.randomInput(400)
    return worker.samples_to_pixels(np.diff(data, prepend=0))


def test_an_apd_counts_the_sample_and_its_dark_counts(brightest):
    bright, dark = brightest
    apd = _apd({'axes': AXES, 'preset': 'beads', 'excitation': ['488'],
                'darkCountRate': 2e5}, mockPhotonCountMean=1000)
    lit = _apd_pixels(apd, _parked(bright, 400, np.ones(400, bool)))
    unlit = _apd_pixels(apd, _parked(bright, 400, np.zeros(400, bool)))
    background = _apd_pixels(apd, _parked(dark, 400, np.ones(400, bool)))
    # Scatter background grows with the light; the bead still stands out.
    assert lit.mean() > 5 * background.mean()
    # No light: dark counts alone, 0.2 per microsecond sample.
    dwell = 10
    assert unlit.mean() == pytest.approx(0.2 * dwell, rel=0.3)


def test_a_pmt_sees_the_sample(brightest):
    bright, dark = brightest
    setup = _minimal_simulated_setup({'PMT': {
        'analogChannel': None, 'digitalLine': None, 'managerName': 'PMTManager',
        'managerProperties': {
            'analogInputLine': 'Dev1/ai0', 'mockRandomSeed': 1, 'mockVoltageNoiseStd': 0.1,
            'mockSample': {'axes': AXES, 'preset': 'beads', 'signalVolts': 2.0}},
        'forAcquisition': True}})
    pmt = PMTManager(setup.detectors['PMT'], 'PMT', NidaqManager(setup))

    def volts(at):
        worker = PMTScanWorker(pmt, _slow_detector_scan_info(),
                               {'TTLCycleSignalsDict': {}, **_parked(at, 400)})
        return float(worker.randomInput(400).mean())

    assert volts(bright) == pytest.approx(2.0, abs=0.2)
    assert volts(dark) == pytest.approx(2.0 * BACKGROUND, abs=0.05)


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
