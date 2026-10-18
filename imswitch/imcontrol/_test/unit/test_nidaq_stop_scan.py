"""The NI-DAQ stops a scan now (plan scan-stop-now, Lenny 2026-10-18).

A run is armed on the hardware path with stand-in tasks that play until they
are stopped, so their waiter threads block as on the driver. Stop must end it
at once: every task of the run stopped and closed, every line at its off
level (an active-low one high), a laser's analog power at its off, each
scanner ramped back to its start within its vel_max and acc_max, a scanner
without limits left where it is -- and the run ends stopped, then done.
"""
import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.model import SetupInfo
from imswitch.imcontrol.model.managers.NidaqManager import NidaqManager
from imswitch.imcontrol.model.scan_stop import axis_limits_volts

GALVO = {'conversionFactor': 17.44, 'vel_max': 0.1, 'acc_max': 1e-4}
SAMPLES = 1000
STOPPED_AT = 600


def _setup():
    def positioner(channel, props):
        return {'managerName': 'NidaqPositionerManager', 'analogChannel': channel,
                'digitalLine': None, 'axes': ['X'], 'forScanning': True,
                'forPositioning': True, 'managerProperties': props}

    def laser(digital, analog=None, level='high'):
        return {'managerName': 'NidaqLaserManager', 'digitalLine': digital,
                'analogChannel': analog, 'valueRangeMin': 0, 'valueRangeMax': 5,
                'ttlActiveLevel': level, 'managerProperties': {}}
    return SetupInfo.from_json(json.dumps({
        'positioners': {'GalvoX': positioner('Dev1/ao0', dict(GALVO)),
                        'StageY': positioner('Dev1/ao1', {'conversionFactor': 10.0})},
        'lasers': {'L488': laser('Dev1/port0/line0'),
                   'AOM775': laser('Dev1/port0/line1', 'Dev1/ao2', level='low')},
        'detectors': {},
        'scan': {'scanWidgetType': 'Advanced', 'scanDesigner': 'GalvoScanDesigner',
                 'scanDesignerParams': {}, 'TTLCycleDesigner': 'AdvancedScanTTLCycleDesigner',
                 'TTLCycleDesignerParams': {}, 'sampleRate': 100000,
                 'lineClockLine': 'Dev1/port0/line6'},
        'nidaq': {'simulation': True, 'timerCounterChannel': None, 'startTrigger': False},
    }), infer_missing=True)


class _PlayingTask:
    """A scan output task: plays until stopped; says what it generated."""

    def __init__(self, generated):
        self.writes, self.started, self.closed = [], False, False
        self._stopped = threading.Event()
        self.stopCalls = 0
        self.timing = SimpleNamespace(samp_clk_max_rate=2.0e6, samp_clk_rate=100000.0)
        self.out_stream = SimpleNamespace(total_samp_per_chan_generated=generated)

    def write(self, values, auto_start=False):
        self.writes.append(np.asarray(values))

    def start(self):
        self.started = True

    def wait_until_done(self, timeout=None):
        self._stopped.wait(10.0)

    def stop(self):
        self.stopCalls += 1
        self._stopped.set()

    def close(self):
        self.closed = True
        self._stopped.set()


class _OneShotTask(_PlayingTask):
    def __init__(self, name, channels):
        super().__init__(0)
        self.name, self.channels = name, list(np.atleast_1d(channels))

    def wait_until_done(self, timeout=None):
        self.timeout = timeout


def _manager(generated=STOPPED_AT):
    manager = NidaqManager(_setup())
    manager._NidaqManager__simulating = False
    made = {}

    def builder(kind):
        def build(name, channels, *args, **kwargs):
            task = (_PlayingTask(generated) if name.startswith('Scan')
                    else _OneShotTask(name, channels))
            made[name] = task
            return task
        return build
    manager._NidaqManager__createChanAOTask = builder('ao')
    manager._NidaqManager__createLineDOTask = builder('do')
    ends = []
    manager.sigScanStopped.connect(lambda: ends.append('stopped'))
    manager.sigScanDone.connect(lambda: ends.append('done'))
    return manager, made, ends


GALVO_WAVE = np.concatenate((np.linspace(0.0, 0.5, SAMPLES // 2),
                             np.linspace(0.5, 0.0, SAMPLES // 2)))
POWER_WAVE = np.concatenate((np.zeros(100), np.full(SAMPLES - 100, 2.0)))
GATE = np.concatenate((np.zeros(100, bool), np.ones(SAMPLES - 100, bool)))


def _run(manager):
    signals = {
        'scanSignalsDict': {'GalvoX': GALVO_WAVE, 'StageY': np.full(SAMPLES, 0.2),
                            'AOM775': POWER_WAVE},
        'TTLCycleSignalsDict': {'L488': GATE, 'AOM775': GATE,
                                'line_clock': np.ones(SAMPLES, bool)},
    }
    info = {'img_dims': [10, 10], 'phase_delay': 0, 'scan_time_step': 1e-5,
            'scan_samples_total': SAMPLES, 'n_linesteps': 1}
    manager.runScan(signals, info)


def test_a_stop_ends_the_run_now_and_brings_every_output_to_rest():
    manager, made, ends = _manager()
    _run(manager)
    assert made['ScanAOTask'].started and made['ScanDOTask'].started
    assert ends == [] and manager.busy

    assert manager.stopScan() is True
    for name in ('ScanAOTask', 'ScanDOTask'):
        assert made[name].stopCalls >= 1 and made[name].closed
    assert ends == ['stopped', 'done']
    assert not manager.busy and manager.runStopped

    lines = made['stopLinesTask']
    assert lines.channels == ['Dev1/port0/line0', 'Dev1/port0/line1', 'Dev1/port0/line6']
    levels = lines.writes[-1][:, 0].tolist()
    assert levels == [False, True, False]          # AOM775 is active-low: off is high

    analog = made['stopAnalogTask']
    assert analog.channels == ['Dev1/ao0', 'Dev1/ao2']   # StageY declares no limits
    galvo, power = analog.writes[-1]
    found = float(GALVO_WAVE[STOPPED_AT - 1])
    assert galvo[-1] == GALVO_WAVE[0] and abs(galvo[0] - found) < abs(found - GALVO_WAVE[0])
    v_max, a_max = axis_limits_volts(GALVO)
    rate = 100_000
    speed = np.abs(np.diff(np.concatenate(([found], galvo)))) * rate
    assert speed.max() <= v_max * (1 + 1e-6)
    assert np.all(power == 0.0)                     # the power's off: its first sample
    assert analog.timeout >= 10.0                   # never under nidaqmx's own default

    stop = manager.lastStop()
    assert stop['found']['GalvoX'] == pytest.approx(found)
    assert stop['left'] == {'GalvoX': GALVO_WAVE[0], 'AOM775': 0.0, 'StageY': 0.2}
    assert 'no vel_max' in stop['held']['StageY']
    assert stop['errors'] == []

    assert manager.stopScan() is False              # nothing left to stop
    assert ends == ['stopped', 'done']


def test_a_stop_before_any_sample_finds_the_scanners_at_their_start():
    manager, made, _ = _manager(generated=0)
    _run(manager)
    manager.stopScan()
    assert manager.lastStop()['found']['GalvoX'] == GALVO_WAVE[0]
    galvo = made['stopAnalogTask'].writes[-1][0]
    assert np.all(galvo == GALVO_WAVE[0])           # nothing to ramp: held at the start


def test_a_position_the_driver_does_not_report_is_not_guessed():
    manager, made, _ = _manager()
    _run(manager)

    class _Unreported:
        @property
        def total_samp_per_chan_generated(self):
            raise RuntimeError('property not supported')
    made['ScanAOTask'].out_stream = _Unreported()
    manager.stopScan()
    stop = manager.lastStop()
    assert stop['found']['GalvoX'] is None
    assert 'not known' in stop['held']['GalvoX']
    assert made['stopAnalogTask'].channels == ['Dev1/ao2']   # only the power's off


def test_a_run_whose_tasks_do_not_close_writes_nothing_and_stays_busy():
    manager, made, ends = _manager()
    _run(manager)

    def refuse():
        raise RuntimeError('close refused')
    made['ScanAOTask'].close = refuse
    manager.stopScan()
    assert 'stopLinesTask' not in made and 'stopAnalogTask' not in made
    assert manager.busy                             # fail-closed, as a failed rollback
    assert manager.lastStop()['errors']
    assert ends == ['stopped', 'done']


def test_a_stop_without_a_run_does_nothing():
    manager, made, ends = _manager()
    assert manager.stopScan() is False
    assert ends == [] and manager.lastStop() is None


def test_a_simulated_run_stops_and_finds_its_outputs_by_its_progress(qtbot):
    manager = NidaqManager(_setup())
    ends = []
    manager.sigScanStopped.connect(lambda: ends.append('stopped'))
    manager.sigScanDone.connect(lambda: ends.append('done'))
    _run(manager)
    simulator = manager._NidaqManager__scanSimulator
    qtbot.waitUntil(lambda: simulator.progress > 0.0, timeout=5000)
    started = time.monotonic()
    assert manager.stopScan() is True
    assert time.monotonic() - started < 2.0         # not the run's remaining time
    assert ends == ['stopped', 'done'] and not simulator.isRunning
    qtbot.waitUntil(lambda: not simulator.isActive, timeout=2000)   # its thread's end is queued
    found = manager.lastStop()['found']['GalvoX']
    assert found is not None and found != GALVO_WAVE[-1]
