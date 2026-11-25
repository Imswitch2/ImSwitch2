"""Laser power and on/off state acting on the virtual sample.

The laser controller publishes each laser's on/off state and power as
shared attributes; the master controller hands the mock detectors a
read-only view of them, and the fluorophores answer with
saturation, a background that grows with the light, and Poisson noise, so
image quality has an optimum power. For MoNaLISA each laser's power acts on
its own step: ON fraction, OFF dose (resolution), readout signal.
"""
from types import SimpleNamespace

import numpy as np
import pytest

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


class FakeLasers:
    """What a laser controller publishes, and the lasers' ranges (0-5 V)."""

    KEYS = {'Enabled': 'enabled', 'Value': 'value'}

    def __init__(self, **fractions):
        self.levels = {name: {'enabled': True, 'value': 5.0 * f}
                       for name, f in fractions.items()}

    def __getitem__(self, key):                  # the shared-attribute view
        category, name, attribute = key
        assert category == 'Laser'
        return self.levels[name][self.KEYS[attribute]]

    def states(self):
        manager = SimpleNamespace(isBinary=False, valueRangeMin=0.0, valueRangeMax=5.0,
                                  usesCalibrationLookup=lambda: False)
        return LaserStates(self, {name: manager for name in self.levels})


def _states(**fractions):
    return FakeLasers(**fractions).states()


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
    daq = FakeLasers(**{'488': fraction})
    daq.levels['488']['enabled'] = enabled
    sample = MockSample.from_property(prop)
    signal = PointScanSignal(sample, PointScanOptics.from_property(prop), _parked(at, 4, ttl),
                             1, 1e6, states=daq.states())
    return signal(0, 4)


# ---------------------------------------------------------------------------
# How the state gets there
# ---------------------------------------------------------------------------

def test_the_published_state_and_range_make_a_power_fraction():
    attributes = {('Laser', '488', 'Enabled'): True, ('Laser', '488', 'Value'): 1.25,
                  ('Laser', 'lut', 'Value'): 40.0, ('Laser', 'binary', 'Value'): 0.0}
    managers = {
        '488': SimpleNamespace(isBinary=False, valueRangeMin=0, valueRangeMax=5,
                               usesCalibrationLookup=lambda: False),
        'lut': SimpleNamespace(isBinary=False, valueRangeMin=0, valueRangeMax=5,
                               usesCalibrationLookup=lambda: True),
        'binary': SimpleNamespace(isBinary=True, valueRangeMin=0, valueRangeMax=200),
    }
    states = LaserStates(attributes, managers)
    state = states.get('488')
    assert (state.enabled, state.fraction, state.range) == (True, 0.25, (0.0, 5.0))
    assert states.get('lut').fraction == pytest.approx(0.4)        # % of 0-100
    assert states.get('binary').fraction is None                   # no power: full
    assert states.get('unknown') == states.get('unknown').__class__()
    assert LaserStates().get('488').enabled is None                # no controller


def test_a_laser_is_lit_by_its_ttl_else_its_set_state_at_its_power():
    lasers = parse_lasers({'488': 1.0}, 'excitation')
    daq = FakeLasers(**{'488': 0.4})
    assert laser_light({}, lasers, 3, states=daq.states()) == pytest.approx([0.4] * 3)
    daq.levels['488']['enabled'] = False
    assert laser_light({}, lasers, 3, states=daq.states()) == pytest.approx([0, 0, 0])
    # The scan's TTL decides while it runs; the set power still counts.
    ttl = {'TTLCycleSignalsDict': {'488': np.array([1, 0, 1], bool)}}
    assert laser_light(ttl, lasers, 3, states=daq.states()) == pytest.approx([0.4, 0, 0.4])
    # A power waveform in the scan wins, read against the laser's range.
    ao = {'scanSignalsDict': {'488': np.array([5.0, 2.5, 0.0])}, **ttl}
    ttl['TTLCycleSignalsDict']['488'] = np.ones(3, bool)
    assert laser_light(ao, lasers, 3, states=daq.states()) == pytest.approx([1, 0.5, 0])


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

    from imswitch.imcontrol.model.managers.NidaqManager import NidaqManager
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
    apd.attachMockLaserStates(_states(**{"488": 0.0}))
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
    daq = FakeLasers(**{'488 (EXC)': 0.0})
    frames = MockLatticeFrames(lattice, 0.075, seed=0, states=daq.states())
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


def test_the_laser_widget_reaches_the_shipped_monalisa_camera(qtbot):
    """The real path: a MasterController and LaserController on the shipped
    SimpleMoNaLISA mock setup. What the Laser widget sets reaches the mock
    camera's live view through the shared attributes -- no driver involved."""
    from .test_scan_cloak_contract import CloakRig, _controllerClass
    from imswitch.imcontrol._test import optionsBasic
    from imswitch.imcontrol.view import widgets

    rig = CloakRig('SimpleMoNaLISA')
    try:
        lasers = rig.factory.createController(
            _controllerClass('LaserController'), widgets.LaserWidget(optionsBasic))
        source = rig.master.detectorsManager['Camera']._mockFrames

        def level():
            return source.lattice.expected_photons(
                (96, 96), source.pixelUm, source._live(FrameConditions())).sum()

        # The controller switches every laser off and to its minimum on start.
        assert level() == 0
        lasers.toggleLaser('488 (EXC)', True)
        assert level() == 0                     # on, at 0 V
        lasers.valueChanged('488 (EXC)', 0.5)
        low = level()
        lasers.valueChanged('488 (EXC)', 5.0)
        assert 0 < low < level()
        lasers.toggleLaser('488 (EXC)', False)
        assert level() == 0
    finally:
        rig.close()


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
