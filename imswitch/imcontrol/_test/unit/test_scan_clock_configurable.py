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


def _manager(sampleRate, detectionRateHz=None, timerCounterChannel='Dev1/ctr2'):
    nidaq = {'simulation': True, 'timerCounterChannel': timerCounterChannel,
             'startTrigger': False}
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
    # the analog output on the onboard clock at every rate, 100 kHz included:
    # its first sample a fixed time after the trigger the timer starts on
    assert manager._scanClockSource() == ''
    # a digital-only scan at 100 kHz on the board's 100 kHz timebase, as before
    assert manager._scanClockSource(analogOutput=False) == SCAN_CLOCK_TERMINAL
    fast = _manager(1_000_000)
    assert (fast.scanRateHz, fast.timerRateHz) == (1e6, 1e6)
    assert fast._scanClockSource() == fast._scanClockSource(analogOutput=False) == ''
    faster = _manager(1_000_000, detectionRateHz=4_000_000)
    assert faster.timerRateHz == 4e6
    with pytest.raises(Exception, match='whole multiple.*nearest scan rates the 1e\\+06 Hz '
                                        'timer divides are 250000 and 333333 Hz'):
        _manager(300_000)
    with pytest.raises(Exception, match='Raise nidaq.detectionRateHz'):
        _manager(2_000_000)


def test_a_setup_without_the_detectors_timer_is_not_held_to_its_ratio():
    """A camera rig has no timer counter: a scan rate the 1 MHz timer would
    not divide is no reason to refuse it (review 2026-10-16)."""
    manager = _manager(300_000, timerCounterChannel=None)
    assert manager.scanRateHz == 300_000
    with pytest.raises(Exception, match='must be positive'):
        _manager(-5, timerCounterChannel=None)


class _Unreported:
    """A driver property the driver does not answer."""

    def __getattr__(self, name):
        raise RuntimeError(f'DAQmx: property {name} is not supported by this device')


def _task(max_rate=2e6, rate=1e6):
    return SimpleNamespace(timing=SimpleNamespace(samp_clk_max_rate=max_rate, samp_clk_rate=rate))


def test_a_task_is_verified_against_what_the_driver_reports_for_it():
    """The configured task, not the device: its maximum for these channels
    (four AO channels update slower than one) and, for a task that makes its
    own clock, the rate the driver coerced it to. A check the driver does
    not answer refuses the scan -- never a silent pass."""
    manager = _manager(1_000_000)
    manager._verifyTaskClock(_task(), 'analog output', 1e6, generated=True)
    with pytest.raises(Exception, match='analog output task cannot run at 2e\\+06 Hz: the '
                                        'driver reports 1.25e\\+06 Hz'):
        manager._verifyTaskClock(_task(max_rate=1.25e6, rate=2e6), 'analog output', 2e6,
                                 generated=True)
    with pytest.raises(Exception, match='runs at 999000 Hz, not the 1e\\+06 Hz'):
        manager._verifyTaskClock(_task(rate=999_000.0), 'analog output', 1e6, generated=True)
    manager._verifyTaskClock(_task(rate=5.0), 'digital output', 1e6, generated=False)
    with pytest.raises(Exception, match='Cannot verify the analog input Dev1/ai0 task: the '
                                        'driver did not report its maximum'):
        manager._verifyTaskClock(SimpleNamespace(timing=_Unreported()), 'analog input Dev1/ai0',
                                 1e6, generated=False)
    with pytest.raises(Exception, match='did not report the sample clock rate it configured'):
        manager._verifyTaskClock(
            SimpleNamespace(timing=SimpleNamespace(samp_clk_max_rate=2e6, samp_clk_rate=None)),
            'analog output', 1e6, generated=True)
    # a counter input's clock is the verified timer: its own maximum is optional
    manager._verifyTaskClock(SimpleNamespace(timing=_Unreported()), 'counter input Dev1/ctr0',
                             1e6, generated=False, mandatory=False)


def _timer(timebase=100e6, freq=1e6, delay=0.0, unreported=False):
    commits = []
    channel = SimpleNamespace(co_ctr_timebase_rate=timebase, co_pulse_freq=freq,
                              co_pulse_freq_initial_delay=delay)
    return SimpleNamespace(co_channels=[] if unreported else [channel], control=commits.append,
                           commits=commits)


def test_the_detectors_timer_is_verified_as_the_driver_made_it():
    manager = _manager(1_000_000)
    timer = _timer()
    assert manager._verifyTimerClock(timer, 1e6) == {
        'rate_hz': 1e6, 'initial_delay_s': 0.0, 'timebase_hz': 100e6}
    assert len(timer.commits) == 1                              # committed before reading
    with pytest.raises(Exception, match='3e\\+06 Hz is not a whole division of the '
                                        "timer's 1e\\+08 Hz timebase; the nearest rates it "
                                        'makes are 2.94118e\\+06 and 3.0303e\\+06 Hz'):
        manager._verifyTimerClock(_timer(freq=3e6), 3e6)
    with pytest.raises(Exception, match='runs at 999000 Hz, not the 1e\\+06 Hz'):
        manager._verifyTimerClock(_timer(freq=999_000.0), 1e6)
    with pytest.raises(Exception, match='a whole timer period or more'):
        manager._verifyTimerClock(_timer(delay=1e-6), 1e6)
    manager._verifyTimerClock(_timer(delay=2e-8), 1e6)          # two timebase ticks: fine
    with pytest.raises(Exception, match="Cannot verify the detectors' timer"):
        manager._verifyTimerClock(_timer(unreported=True), 1e6)


def _armedScan(timer, ao=None, phase_delay=30):
    """A hardware-path scan on stand-in tasks, rolled back by a detector's
    build failure right after every task is made and verified -- before any
    starts. Returns the manager, the timer's creation arguments and the scan
    info the manager filled in."""
    from .test_nidaq_scan_transaction import _Device, _Setup, _Task
    manager = NidaqManager(_Setup({'X': _Device(analog='Dev1/ao0')}))
    manager._NidaqManager__simulating = False
    manager._NidaqManager__scanSimulator = None
    manager._NidaqManager__timerCounterChannel = 'Dev1/ctr2'      # startTrigger stays False
    made = {}

    def makeTimer(name, **kwargs):
        made.update(kwargs)
        return timer
    manager._NidaqManager__createChanCOTask = makeTimer
    ao = ao if ao is not None else _Task()
    manager._NidaqManager__createChanAOTask = lambda *args, **kwargs: ao
    manager.sigScanBuilt.connect(
        lambda *_args: manager.reportScanBuildFailure('APD.prepare', RuntimeError('stop here')))
    info = {'img_dims': [4], 'phase_delay': phase_delay, 'scan_time_step': 1e-5,
            'scan_samples_total': 4}
    signals = {'scanSignalsDict': {'X': np.zeros(4)}, 'TTLCycleSignalsDict': {}}
    with pytest.raises(Exception) as refusal:
        manager.runScan(signals, info)
    return manager, made, info, ao, refusal.value


def test_the_timer_is_armed_on_the_scan_start_whenever_there_is_analog_output():
    """The detectors are armed on ao/StartTrigger; the timer they sample on
    now is too, whatever nidaq.startTrigger says -- free-running, it put
    every bin at a random phase within one timer period (review 2026-10-16).
    What it realized, and the lag split, are in the scan info."""
    from .test_nidaq_scan_transaction import _Task

    class _Timer(_Task):
        def __init__(self):
            super().__init__()
            self.co_channels = [SimpleNamespace(co_ctr_timebase_rate=100e6, co_pulse_freq=1e6,
                                                co_pulse_freq_initial_delay=0.0)]

        def control(self, _mode):
            pass

    timer = _Timer()
    manager, made, info, _ao, refusal = _armedScan(timer)
    assert 'stop here' in str(refusal)
    assert made['starttrig'] is True and made['reference_trigger'] == 'ao/StartTrigger'
    assert info['detector_clock'] == {'rate_hz': 1e6, 'initial_delay_s': 0.0,
                                      'timebase_hz': 100e6}
    assert info['lag'] == pytest.approx({'requested_s': 30e-6, 'gate_delay_s': 30e-6,
                                         'detector_delay_s': 30e-6, 'residual_s': 0.0})
    assert timer.closeCalls == 1 and manager.tasks == {} and not manager.busy


def test_a_refused_clock_rolls_the_scan_back_and_closes_its_tasks():
    from .test_nidaq_scan_transaction import _Task

    class _CoercedTimer(_Task):
        def __init__(self):
            super().__init__()
            self.co_channels = [SimpleNamespace(co_ctr_timebase_rate=100e6, co_pulse_freq=999e3,
                                                co_pulse_freq_initial_delay=0.0)]

        def control(self, _mode):
            pass

    timer = _CoercedTimer()
    manager, _made, _info, _ao, refusal = _armedScan(timer)
    assert 'runs at 999000 Hz' in str(refusal)
    assert timer.closeCalls == 1 and manager.tasks == {} and not manager.busy

    ao = _Task()
    ao.timing = SimpleNamespace(samp_clk_max_rate=2e6, samp_clk_rate=99_999.4)
    timer = _CoercedTimer()
    timer.co_channels[0].co_pulse_freq = 1e6
    manager, _made, _info, ao, refusal = _armedScan(timer, ao=ao)
    assert 'analog output task runs at 99999.4 Hz' in str(refusal)
    assert ao.closeCalls == 1 and timer.closeCalls == 1 and not manager.busy


def test_a_refused_input_task_is_closed_not_leaked(monkeypatch):
    """A PMT's analog input asked for more than its task allows: refused by
    name, and the task it had made is closed -- a leaked task would hold the
    device's analog input until ImSwitch exits."""
    import importlib
    import nidaqmx.constants
    module = importlib.import_module('imswitch.imcontrol.model.managers.NidaqManager')
    made = []

    class _AITask:
        def __init__(self, name):
            self.closed = False
            self.ai_channels = SimpleNamespace(add_ai_voltage_chan=lambda *a, **k: None)
            self.timing = SimpleNamespace(cfg_samp_clk_timing=lambda **k: None,
                                          samp_clk_max_rate=500e3, samp_clk_rate=1e6)
            self.triggers = SimpleNamespace(
                start_trigger=SimpleNamespace(cfg_dig_edge_start_trig=lambda source: None))
            made.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(module, 'nidaqmx', SimpleNamespace(Task=_AITask,
                                                           constants=nidaqmx.constants))
    manager = _manager(1_000_000)
    manager._NidaqManager__simulating = False
    with pytest.raises(Exception, match='analog input Dev1/ai0 task cannot run at 1e\\+06 Hz: '
                                        'the driver reports 500000 Hz'):
        manager._NidaqManager__createChanAITask(
            'PMT', 'Dev1/ai0', 'finite', '/Dev1/Ctr2InternalOutput', 1e6, -1.0, 1.0, 100, True,
            'ao/StartTrigger')
    assert made and made[0].closed


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
    def __init__(self, sampleRate=1_000_000, apdProps=None):
        setup = json.load(open(GALVO_SETUP))
        setup['scan'].update(scanWidgetType='Advanced', scanDesigner='GalvoScanDesigner',
                             sampleRate=sampleRate)
        for name, detector in setup['detectors'].items():
            if detector.get('managerName') == 'APDManager':
                detector['managerProperties'].update(apdProps or {})
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


def test_the_advanced_dwell_names_the_clock_and_simple_snaps_on_it(qtbot):
    from imswitch.imcontrol.model.simple_scan import snap_dwell_s
    slow = AdvancedRig(designer='GalvoScanDesigner')
    try:
        assert slow.widget._dwellLabel.text() == 'Dwell time (ms, 10 µs clock):'
        assert 'next one up' in slow.widget.seqTimePar.toolTip()
    finally:
        slow.close()
    fast = _FastRig()
    try:
        assert fast.widget._dwellLabel.text() == 'Dwell time (ms, 1 µs clock):'
    finally:
        fast.close()
    assert snap_dwell_s(1.5e-5, 1e5) == pytest.approx(2e-5)        # nearest, ties to even
    assert snap_dwell_s(1.2e-5, 1e5) == pytest.approx(1e-5)
    assert snap_dwell_s(1.2e-5, 1e5, minimum=1.1e-5) == pytest.approx(2e-5)   # floor: up
    assert snap_dwell_s(1e-6, 1e6) == pytest.approx(1e-6)


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


def test_the_timing_note_states_a_lag_the_two_clocks_play_differently(qtbot):
    """At 100 kHz a 3 µs phase delay is no whole scan-clock sample: the laser
    lines play none of it, the detectors (1 MHz timer) all of it. The note
    says so; at 30 µs both play it exactly and the note is silent."""
    rig = AdvancedRig(designer='GalvoScanDesigner')
    try:
        rig.widget.phaseDelayPar.setText('3')
        rig.scan._refreshTimingNote()
        note = rig.widget.timingNoteLabel.text()
        assert ('The 3 µs phase delay plays as 0 µs on the digital lines (whole 10 µs samples) '
                'and 3 µs on the detectors, 3 µs apart.') in note
        rig.widget.phaseDelayPar.setText('30')
        rig.scan._refreshTimingNote()
        assert 'phase delay plays' not in rig.widget.timingNoteLabel.text()
    finally:
        rig.close()
