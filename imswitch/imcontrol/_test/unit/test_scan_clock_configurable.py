"""The scan clock is configurable (scan timing plan, S1 and S2):
``scan.sampleRate`` is the pixel clock, the AO and DO sample clock and the
Expert engine's grid; ``nidaq.detectionRateHz`` the detectors' timer, a
whole multiple of it. At 100 kHz nothing changes; at 1 MHz a 1 µs pixel is
one sample everywhere. Hardware behaviour (the driver dividing its 100 MHz
timebase, the read-back rate, the device limits) is the rig test plan's."""
import json
import time
from types import SimpleNamespace

import numpy as np
import pytest
from qtpy import QtTest

from imswitch.imcontrol.model import SetupInfo
from imswitch.imcontrol.model.managers.NidaqManager import (
    NidaqManager, SCAN_CLOCK_RATE_HZ, SCAN_CLOCK_TERMINAL, TIMER_COUNTER_RATE_HZ,
)
from imswitch.imcontrol.model.signaldesigners.AdvancedScanTTLCycleDesigner import (
    AdvancedScanTTLCycleDesigner,
)
from imswitch.imcontrol.model.signaldesigners.GalvoScanDesigner import GalvoScanDesigner

from .test_galvo_signal_goldens import _params, _setup_sted_like
from .test_scan_controller_advanced_orders import AdvancedRig, SETUP as GALVO_SETUP


def _manager(sampleRate, detectionRateHz=None):
    nidaq = {'simulation': True, 'timerCounterChannel': 'Dev1/ctr2', 'startTrigger': False}
    if detectionRateHz is not None:
        nidaq['detectionRateHz'] = detectionRateHz
    setup = SetupInfo.from_json(json.dumps({
        'detectors': {}, 'lasers': {}, 'positioners': {},
        'scan': {'scanWidgetType': 'PointScan', 'scanDesigner': 'GalvoScanDesigner',
                 'scanDesignerParams': {}, 'TTLCycleDesigner': 'PointScanTTLCycleDesigner',
                 'TTLCycleDesignerParams': {}, 'sampleRate': sampleRate},
        'nidaq': nidaq,
    }), infer_missing=True)
    return NidaqManager(setup)


def test_the_rates_come_from_the_setup_and_default_as_before():
    manager = _manager(SCAN_CLOCK_RATE_HZ)
    assert (manager.scanRateHz, manager.timerRateHz) == (SCAN_CLOCK_RATE_HZ,
                                                         TIMER_COUNTER_RATE_HZ)
    assert manager._scanClockSource() == SCAN_CLOCK_TERMINAL       # the 100 kHz rig: as it was
    fast = _manager(1_000_000)
    assert (fast.scanRateHz, fast.timerRateHz) == (1e6, 1e6)
    assert fast._scanClockSource() == ''                             # the onboard clock, divided
    faster = _manager(1_000_000, detectionRateHz=4_000_000)
    assert faster.timerRateHz == 4e6
    with pytest.raises(Exception, match='whole multiple.*nearest scan rates the 1e\\+06 Hz '
                                        'timer divides are 250000 and 333333 Hz'):
        _manager(300_000)
    with pytest.raises(Exception, match='Raise nidaq.detectionRateHz'):
        _manager(2_000_000)


def test_the_device_limits_and_the_configured_rate_are_checked_against_the_driver(
        monkeypatch):
    """On hardware (here a driver stand-in): a scan clock above the device's
    ao_max_rate or do_max_rate, an analog input above ai_max_single_chan_rate,
    and a task the driver configured at another rate than asked are refused
    by name."""
    import importlib
    module = importlib.import_module('imswitch.imcontrol.model.managers.NidaqManager')
    device = SimpleNamespace(ao_max_rate=2.0e6, do_max_rate=10e6, ai_max_single_chan_rate=2e6)
    monkeypatch.setattr(module, '_NIDAQMX_AVAILABLE', True)
    monkeypatch.setattr(module, 'nidaqmx', SimpleNamespace(
        system=SimpleNamespace(Device=lambda name: device)), raising=False)
    manager = _manager(1_000_000)
    manager._checkDeviceRates(['Dev1/ao0', 'Dev1/ao1'], ['Dev1/port0/line0'])
    fast = _manager(1_000_000, detectionRateHz=4_000_000)
    fast._scanRateHz = 4e6                                   # as a setup asking 4 MHz would
    with pytest.raises(Exception, match="above the 2e\\+06 Hz .* Dev1's ao_max_rate"):
        fast._checkDeviceRates(['Dev1/ao0'], [])
    with pytest.raises(Exception, match='ai_max_single_chan_rate'):
        fast._checkInputRate('Dev1/ai0', 4e6)
    manager._checkInputRate('Dev1/ai0', 1e6)
    task = SimpleNamespace(timing=SimpleNamespace(samp_clk_rate=999_000.0))
    with pytest.raises(Exception, match='runs at 999000 Hz, not the 1e\\+06 Hz'):
        manager._checkConfiguredRate(task, 'analog output')
    manager._checkConfiguredRate(SimpleNamespace(timing=SimpleNamespace(samp_clk_rate=1e6)),
                                 'analog output')


def _design(rate_hz, dwell_us, step_um):
    setup = _setup_sted_like()
    setup.scan.sampleRate = rate_hz
    setup.lasers = {'L488': SimpleNamespace(digitalLine='Dev1/port0/line0')}
    setup.detectors = {}
    params = _params(['GalvoX', 'GalvoY'], [20 * step_um, 5.0], [step_um, 1.0])
    params['sequence_time'] = dwell_us * 1e-6
    _sig, _pos, info = GalvoScanDesigner().make_signal(params, setup)
    digital = {'target_device': ['L488'], 'n_linesteps': 1, 'linestep_enable': {'L488': [True]},
               'pulse_starts_s': {'L488': [[]]}, 'pulse_ends_s': {'L488': [[]]},
               'advanced_mode': False, 'sequence_time': params['sequence_time']}
    ttl, _ = AdvancedScanTTLCycleDesigner().make_signal(digital, setup, info)
    return info, np.asarray(ttl['L488'], dtype=bool)


@pytest.mark.parametrize('rate_hz,dwell_us,step_um', [
    (100_000, 10, 1.0), (1_000_000, 1, 0.1), (1_000_000, 3, 0.3), (500_000, 2, 0.2)])
def test_at_any_clock_a_pixel_is_whole_samples_and_every_grid_agrees(rate_hz, dwell_us, step_um):
    info, ttl = _design(rate_hz, dwell_us, step_um)
    assert info['scan_time_step'] == pytest.approx(1 / rate_hz)
    assert info['samples_per_pixel'] == round(dwell_us * 1e-6 * rate_hz)
    assert info['dwell_time'] == pytest.approx(dwell_us * 1e-6)
    assert info['dwell_requested_s'] is None
    assert info['scan_samples'][1] == 20 * info['samples_per_pixel']
    on = np.flatnonzero(ttl)
    runs = np.split(on, np.flatnonzero(np.diff(on) > 1) + 1)
    assert len(runs[0]) == info['scan_samples'][1]
    assert runs[1][0] - runs[0][0] == info['scan_samples_d2_period']
    # the detectors' arithmetic: whole timer samples per scan sample, per pixel
    timer = TIMER_COUNTER_RATE_HZ
    frac_scan_det = round(timer * info['scan_time_step'])
    frac_det_dwell = round(info['dwell_time'] * timer)
    assert frac_scan_det == timer // rate_hz and frac_det_dwell == frac_scan_det * info['samples_per_pixel']


@pytest.mark.parametrize('rate_hz', [100_000, 1_000_000])
@pytest.mark.parametrize('dwell_us', [1, 10, 15, 20])
def test_every_timer_sample_of_a_line_lands_in_exactly_one_pixel(rate_hz, dwell_us):
    """Photon counts are conserved by construction (plan §8): a line's
    timer samples are a whole number of pixels, so the detector's binning
    drops none and splits none, at every dwell -- 15 µs included, which
    used to leave a partial pixel at the end of every line read."""
    step_um = 0.1 * dwell_us / 10
    info, _ttl = _design(rate_hz, dwell_us, step_um)
    timer = TIMER_COUNTER_RATE_HZ
    frac_scan_det = round(timer * info['scan_time_step'])
    frac_det_dwell = round(info['dwell_time'] * timer)
    line_timer_samples = info['scan_samples'][1] * frac_scan_det
    assert line_timer_samples % frac_det_dwell == 0
    assert line_timer_samples // frac_det_dwell == 20
    counts = np.random.default_rng(dwell_us).poisson(3.0, line_timer_samples)
    pixels = counts.reshape(-1, frac_det_dwell).sum(axis=1)   # what samples_to_pixels does
    assert pixels.sum() == counts.sum()


class _FastRig(AdvancedRig):
    def __init__(self):
        setup = json.load(open(GALVO_SETUP))
        setup['scan'].update(scanWidgetType='Advanced', scanDesigner='GalvoScanDesigner',
                             sampleRate=1_000_000)
        from imswitch.imcontrol.view import ViewSetupInfo
        from imswitch.imcontrol.controller.CommunicationChannel import CommunicationChannel
        from imswitch.imcontrol.controller.MasterController import MasterController
        from imswitch.imcontrol.controller.basecontrollers import ImConWidgetControllerFactory
        from imswitch.imcontrol.controller.controllers.ScanControllerAdvanced import (
            ScanControllerAdvanced,
        )
        from imswitch.imcontrol.view.widgets.ScanWidgetAdvanced import ScanWidgetAdvanced
        from imswitch.imcontrol._test import optionsBasic
        from .test_simple_point_scan_controller import _Main
        self.setup = ViewSetupInfo.from_json(json.dumps(setup))
        self.main = _Main()
        self.channel = CommunicationChannel(self.main, self.setup)
        self.master = MasterController(self.setup, self.channel, self.main._moduleCommChannel)
        self.factory = ImConWidgetControllerFactory(
            self.setup, self.master, self.channel, self.main._moduleCommChannel)
        self.widget = ScanWidgetAdvanced(optionsBasic)
        self.scan = self.factory.createController(ScanControllerAdvanced, self.widget)
        self.main.controllers['Scan'] = self.scan
        self.ended = []
        self.channel.sigScanEnded.connect(lambda *a: self.ended.append(1))


def test_a_one_microsecond_pixel_scans_end_to_end_on_the_simulated_rig(qtbot):
    """The galvo mock rig at 1 MHz: a 1 µs dwell runs through the real
    designers, the simulated NI-DAQ and the mock APD, which bins one timer
    sample per pixel. The pixels are 0.02 µm: the electronics take any pixel
    at 1 µs, the scanner does not (plan F4) -- at its vel_max of 0.1 µm/µs
    and acc_max of 1e-4 µm/µs² the turnaround alone is v²/2a = 50 µm a side,
    past the ±10 V range, which the designer refuses."""
    rig = _FastRig()
    try:
        assert rig.master.nidaqManager.scanRateHz == 1e6
        rig.load(lengths=[0.4, 0.2, 0.0], steps=[0.02, 0.02, 1.0], dwell=1e-6)
        image = rig.run(timeoutS=60.0)
        assert image.shape[-2:] == (10, 20)
        assert np.isfinite(image).all() and image.sum() > 0
        info = rig.scan.scanInfoDict
        assert info['samples_per_pixel'] == 1 and info['scan_time_step'] == pytest.approx(1e-6)
        assert info['dwell_time'] == pytest.approx(1e-6)
        # the APD's bins: one timer sample per scan sample, one per pixel
        apd = rig.master.detectorsManager['APD']
        assert apd._detection_samplerate == 1e6
        assert round(apd._detection_samplerate * info['scan_time_step']) == 1
        assert round(apd._detection_samplerate * info['dwell_time']) == 1
    finally:
        rig.close()


def test_the_expert_engine_takes_its_grid_from_the_setup(qtbot):
    from .test_scan_controller_expert import ExpertRig, _setup_dict
    rig = ExpertRig(setup=_setup_dict(sampleRate=1_000_000))
    try:
        engine = rig.scan._engineForScans()
        assert engine.rate_hz == 1e6
        assert engine.capabilities().timing_grid_s == pytest.approx(1e-6)
        assert '1e+06 Hz' in engine.capabilities().sources['timing_grid_s'] or \
            '1000000 Hz' in engine.capabilities().sources['timing_grid_s']
    finally:
        rig.close()
