"""Laser power and on/off state acting on the virtual sample.

The simulated NI-DAQ keeps the level each laser's lines were last set to;
the mock detectors read it (read-only) and the fluorophores answer with
saturation, a background that grows with the light, and Poisson noise, so
image quality has an optimum power. For MoNaLISA each laser's power acts on
its own step: ON fraction, OFF dose (resolution), readout signal.
"""
import json

import numpy as np
import pytest

from imswitch.imcontrol.model import DetectorInfo, SetupInfo
from imswitch.imcontrol.model.managers.NidaqManager import NidaqManager
from imswitch.imcontrol.model.managers.detectors.HamamatsuManager import HamamatsuManager
from imswitch.imcontrol.model.managers.detectors._mock_lattice import (
    FrameConditions,
    MockLattice,
    MockLatticeFrames,
    trigger_conditions,
)
from imswitch.imcontrol.model.managers.detectors._mock_optics import (
    LaserStates,
    Photophysics,
    PointScanOptics,
    PointScanSignal,
    laser_light,
    parse_lasers,
)
from imswitch.imcontrol.model.managers.detectors._mock_sample import (
    BACKGROUND,
    PSF_SIGMA_UM,
    MockSample,
)

AXES = {'X': 5.0, 'Y': 5.0}
RATE = 100_000


class FakeDaq:
    """What a simulated NI-DAQ reports after the lasers were set."""

    def __init__(self, **fractions):
        self.levels = {name.replace('_', ' '): {'enabled': True, 'value': 5.0 * f,
                                                 'range': (0.0, 5.0)}
                       for name, f in fractions.items()}

    def simulatedOutput(self, name):
        return dict(self.levels.get(name, {}))


def _states(**fractions):
    return LaserStates(FakeDaq(**fractions))


@pytest.fixture(scope='module')
def bead():
    """The brightest point of the beads preset (a bead), µm."""
    sample = MockSample.from_property({'axes': AXES, 'preset': 'beads'})
    texture = sample.texture()
    row, col = np.unravel_index(np.argmax(texture), texture.shape)
    view = sample.view(PSF_SIGMA_UM)
    return view.origin_um + col * view.texel_um, view.origin_um + row * view.texel_um


def _parked(at, n, ttl=None):
    signals = {'scanSignalsDict': {'X': np.full(n, at[0] / 5.0), 'Y': np.full(n, at[1] / 5.0)},
               'TTLCycleSignalsDict': {}}
    if ttl is not None:
        signals['TTLCycleSignalsDict']['488'] = ttl
    return signals


def _point_signal(at, fraction, prop=None, enabled=True, ttl=None):
    prop = {'axes': AXES, 'preset': 'beads', 'excitation': ['488'], **(prop or {})}
    daq = FakeDaq(**{'488': fraction})
    daq.levels['488']['enabled'] = enabled
    sample = MockSample.from_property(prop)
    signal = PointScanSignal(sample, PointScanOptics.from_property(prop), _parked(at, 4, ttl),
                             1, 1e6, states=LaserStates(daq))
    return signal(0, 4)


# ---------------------------------------------------------------------------
# How the state gets there
# ---------------------------------------------------------------------------

def _nidaq(simulation):
    return NidaqManager(SetupInfo.from_json(json.dumps({
        'detectors': {}, 'positioners': {},
        'lasers': {'488': {'analogChannel': 'Dev1/ao3', 'digitalLine': 'Dev1/port0/line1',
                           'managerName': 'NidaqLaserManager', 'managerProperties': {},
                           'wavelength': 488, 'valueRangeMin': 0, 'valueRangeMax': 5}},
        'nidaq': {'simulation': simulation, 'timerCounterChannel': None,
                  'startTrigger': False},
    }), infer_missing=True))


def test_the_simulated_nidaq_keeps_what_each_laser_was_set_to():
    nidaq = _nidaq(simulation=True)
    assert nidaq.simulatedOutput('488') == {}
    assert LaserStates(nidaq).get('488').enabled is None
    nidaq.setDigital('488', True)
    nidaq.setAnalog('488', 1.25, min_val=0, max_val=5)
    assert nidaq.simulatedOutput('488') == {'enabled': True, 'value': 1.25, 'range': (0.0, 5.0)}
    state = LaserStates(nidaq).get('488')
    assert (state.enabled, state.fraction) == (True, pytest.approx(0.25))
    # A copy: the reader cannot change what the NI-DAQ keeps.
    nidaq.simulatedOutput('488')['value'] = 99.0
    assert nidaq.simulatedOutput('488')['value'] == 1.25


def test_hardware_keeps_no_simulated_levels():
    nidaq = _nidaq(simulation=False)
    nidaq.setDigital('488', True)            # no backend here: refused, as before
    assert nidaq.simulatedOutput('488') == {}
    assert LaserStates(nidaq).get('488').enabled is None


def test_a_laser_is_lit_by_its_ttl_else_its_set_state_at_its_power():
    lasers = parse_lasers({'488': 1.0}, 'excitation')
    daq = FakeDaq(**{'488': 0.4})
    assert laser_light({}, lasers, 3, states=LaserStates(daq)) == pytest.approx([0.4] * 3)
    daq.levels['488']['enabled'] = False
    assert laser_light({}, lasers, 3, states=LaserStates(daq)) == pytest.approx([0, 0, 0])
    # The scan's TTL decides while it runs; the set power still counts.
    ttl = {'TTLCycleSignalsDict': {'488': np.array([1, 0, 1], bool)}}
    assert laser_light(ttl, lasers, 3, states=LaserStates(daq)) == pytest.approx([0.4, 0, 0.4])
    # A power waveform in the scan wins, read against the laser's range.
    ao = {'scanSignalsDict': {'488': np.array([5.0, 2.5, 0.0])}, **ttl}
    ttl['TTLCycleSignalsDict']['488'] = np.ones(3, bool)
    assert laser_light(ao, lasers, 3, states=LaserStates(daq)) == pytest.approx([1, 0.5, 0])


# ---------------------------------------------------------------------------
# The photophysics
# ---------------------------------------------------------------------------

def test_emission_rises_and_saturates_background_does_not():
    physics = Photophysics()
    power = np.linspace(0, 1, 101)
    emission = physics.emission(power)
    assert emission[0] == 0 and emission[-1] == pytest.approx(1.0)
    assert np.all(np.diff(emission) > 0) and np.all(np.diff(emission, 2) < 0)
    assert physics.emission(0.5) > 0.8 * physics.emission(1.0)     # saturated by then
    assert physics.background(power) == pytest.approx((BACKGROUND + physics.scatter) * power)
    widening = physics.widening(power)
    assert widening[0] == 1.0 and np.all(np.diff(widening[1:]) > 0)
    linear = Photophysics(saturation=None, scatter=0.0)
    assert linear.emission(power) == pytest.approx(power)
    assert linear.widening(power) == pytest.approx(1.0)


def test_point_scan_signal_follows_the_power_and_saturates(bead):
    signal = [_point_signal(bead, f)[0] for f in (0.05, 0.1, 0.2, 0.4, 0.8, 1.0)]
    assert np.all(np.diff(signal) > 0)
    assert signal[1] / signal[0] > 1.5                   # nearly linear at first
    assert signal[-1] / signal[-2] < 1.1                 # saturated at the top


def test_a_laser_switched_off_lights_nothing(bead):
    assert _point_signal(bead, 1.0, enabled=False) == pytest.approx(0.0)
    assert _point_signal(bead, 0.0) == pytest.approx(0.0)
    # Off between its TTL pulses, at whatever power it is set to.
    gated = _point_signal(bead, 1.0, ttl=np.array([1, 0, 1, 0], bool))
    assert gated[1] == 0 and gated[0] > 0


def test_an_apd_scan_with_its_lasers_at_zero_says_why_it_is_dark(bead, caplog):
    """Lasers start off and at zero power, as on the instrument: a scan then
    shows dark counts only, and the mock says which lasers to raise."""
    import logging

    from imswitch.imcontrol.model.managers.detectors.APDManager import (
        APDManager,
        ScanWorker,
    )
    from .test_mock_scan_simulation_coordinator import (
        _minimal_simulated_setup,
        _slow_detector_scan_info,
    )
    setup = _minimal_simulated_setup({'APD': {
        'analogChannel': None, 'digitalLine': None, 'managerName': 'APDManager',
        'managerProperties': {
            'ctrInputLine': 'Dev1/ctr0', 'terminal': '/Dev1/PFI0', 'mockRandomSeed': 1,
            'mockSample': {'axes': AXES, 'preset': 'beads', 'excitation': ['488'],
                           'darkCountRate': 2e5}},
        'forAcquisition': True}})
    apd = APDManager(setup.detectors['APD'], 'APD', NidaqManager(setup))
    apd._mock_laser_states = _states(**{'488': 0.0})
    with caplog.at_level(logging.WARNING):
        worker = ScanWorker(apd, _slow_detector_scan_info(),
                            {'TTLCycleSignalsDict': {}, **_parked(bead, 400)})
    assert worker._mock_signal.unlit
    assert any('"488"' in r.getMessage() and 'Laser widget' in r.getMessage()
               for r in caplog.records)
    counts = worker.samples_to_pixels(np.diff(worker.randomInput(400), prepend=0))
    assert counts.mean() == pytest.approx(0.2 * 10, rel=0.3)     # dark counts only


def test_image_quality_has_an_optimum_power():
    """Too little light: shot noise. Too much: saturation widens the PSF
    while scatter keeps growing. The point-scan image of a cell best matches
    the sharp sample in between (seeded Poisson noise)."""
    prop = {'axes': AXES, 'excitation': ['488']}
    sample = MockSample.from_property(prop)
    optics = PointScanOptics.from_property(prop)
    g = np.arange(-3, 3, 0.04) + 2.0
    X, Y = np.meshgrid(g, g + 1.0)
    truth = sample.brightness(X, Y)
    signals = {'scanSignalsDict': {'X': X.ravel() / 5, 'Y': Y.ravel() / 5}}

    def fidelity(fraction):
        rng = np.random.default_rng(1)
        signal = PointScanSignal(sample, optics, signals, 1, 1e6,
                                 states=_states(**{'488': fraction}))
        image = rng.poisson(800 * signal(0, X.size))
        return np.corrcoef(image, truth.ravel())[0, 1]

    low, mid, high = fidelity(0.02), fidelity(0.25), fidelity(1.0)
    assert mid > low + 0.02 and mid > high + 0.005


# ---------------------------------------------------------------------------
# The camera, and MoNaLISA's three lasers
# ---------------------------------------------------------------------------

def _lattice(**extra):
    return MockLattice.from_property({
        'axes': AXES, 'latticePeriodUm': 0.72, 'excitation': ['488 (EXC)'],
        'switching': {'on': ['405 (ON)'], 'off': ['488 (OFF)']}, **extra})


def _cycle(n_frames):
    """ON 1 ms, OFF 2 ms, readout 1 ms with the camera, per frame."""
    phase = np.arange(400)
    ttl = {'405 (ON)': np.tile(phase < 100, n_frames),
           '488 (OFF)': np.tile((phase >= 100) & (phase < 300), n_frames),
           '488 (EXC)': np.tile(phase >= 300, n_frames)}
    ttl['Camera'] = ttl['488 (EXC)'].copy()
    return {'scanSignalsDict': {}, 'TTLCycleSignalsDict': ttl}


def _frame(lattice, **fractions):
    return trigger_conditions(_cycle(6), 'Camera', lattice, 1.0 / RATE,
                              states=_states(**fractions))[-1]


def test_the_paths_agree_on_the_same_sample(bead):
    """One sample, one PSF, one saturation: a point scan and a focus collect
    the same brightness at the same power."""
    prop = {'axes': AXES, 'preset': 'beads', 'excitation': ['488'], 'saturationPower': 0.2,
            'psfFwhmUm': 0.2, 'excitationFwhmUm': 0.2}
    lattice = MockLattice.from_property(prop)
    for fraction in (0.1, 0.6):
        point = _point_signal(bead, fraction, prop)[0]
        focus, *_ = lattice.foci([bead[0]], [bead[1]], FrameConditions(readout=fraction))
        assert focus[0] == pytest.approx(point, rel=0.02)


def test_the_live_camera_shows_the_readout_laser_as_set():
    lattice = _lattice()
    daq = FakeDaq(**{'488 (EXC)': 0.0})
    frames = MockLatticeFrames(lattice, 0.075, seed=0, states=LaserStates(daq))
    shape = (96, 96)
    dark = lattice.expected_photons(shape, 0.075, frames._live(FrameConditions()))
    assert dark.max() == 0.0
    sums = []
    for fraction in (0.1, 0.5, 1.0):
        daq.levels['488 (EXC)']['value'] = 5.0 * fraction
        sums.append(lattice.expected_photons(
            shape, 0.075, frames._live(FrameConditions())).sum())
    assert sums[0] < sums[1] < sums[2]
    daq.levels['488 (EXC)']['enabled'] = False
    assert frames.liveFrame(shape).mean() == pytest.approx(lattice.offset_counts, abs=0.5)


def test_each_monalisa_laser_acts_on_its_own_step():
    lattice = _lattice()
    # OFF power sets the resolution; none of it is confocal.
    sigmas = [lattice.effective_sigma_um(_frame(lattice, **{'488 (OFF)': f}))[0]
              for f in (0.0, 0.1, 0.5, 1.0)]
    assert np.all(np.diff(sigmas) < 0)
    assert sigmas[0] == pytest.approx(lattice.effective_sigma_um(
        FrameConditions(readout=1.0))[0])
    # ON power: how many emitters are on.
    on = [_frame(lattice, **{'405 (ON)': f}).on_fraction for f in (0.02, 0.1, 1.0)]
    assert on[0] < on[1] < on[2]
    # Readout power: the emitters it switches off while reading saturate the
    # signal, while the background keeps growing -- SNR peaks in between.
    fx, fy = lattice.foci_um((128, 128), 0.075)

    def snr(fraction):
        frame = _frame(lattice, **{'488 (EXC)': fraction})
        brightness, *_ = lattice.foci(fx, fy, frame)
        background = float(lattice.physics.background(frame.readout))
        signal = lattice.peak_counts * (brightness.max() - background)
        noise = lattice.peak_counts * brightness.max() + lattice.read_noise_counts ** 2
        return signal / np.sqrt(noise)

    values = [snr(f) for f in (0.02, 0.3, 1.0)]
    assert values[1] > values[0] and values[1] > values[2]


def test_the_shipped_monalisa_camera_follows_its_lasers():
    """A real manager on a simulated NI-DAQ: the laser's writes reach the
    live view, through the NI-DAQ only."""
    setup = json.load(open('imswitch/_data/user_defaults/imcontrol_setups/'
                           'simple_monalisa_mock_scan_setup.json', encoding='utf-8'))
    camera = setup['detectors']['Camera']
    camera['managerProperties']['hamamatsu'].update(
        subarray_hsize=96, subarray_vsize=96, image_width=96, image_height=96)
    nidaq = NidaqManager(SetupInfo.from_json(json.dumps({
        'detectors': {'Camera': camera}, 'lasers': setup['lasers'], 'positioners': {},
        'nidaq': {'simulation': True, 'timerCounterChannel': None, 'startTrigger': False},
    }), infer_missing=True))
    manager = HamamatsuManager(DetectorInfo(**camera), 'Camera', nidaqManager=nidaq)
    source = manager._mockFrames
    level = lambda: source.lattice.expected_photons(  # noqa: E731
        (96, 96), source.pixelUm, source._live(FrameConditions())).sum()
    assert level() > 0                     # never set: unknown, so lit
    nidaq.setDigital('488 (EXC)', False)
    assert level() == 0
    nidaq.setDigital('488 (EXC)', True)
    nidaq.setAnalog('488 (EXC)', 0.5, min_val=0, max_val=5)
    low = level()
    nidaq.setAnalog('488 (EXC)', 5.0, min_val=0, max_val=5)
    assert 0 < low < level()


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
