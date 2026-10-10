"""How runs end, and what a run leaves running (review round 1, batch 1;
docs/design/plans/scan-engines-and-expert-scan.md §7.9).

Every engine ends a run in one of three ways the panels end their run on
(``ScanEngine``): done; stopped then done; failed then build-failed. A run
the host gives up on is stopped on the board and confirmed; a multi-engine
run whose part cannot start still reports one outcome; the NI-DAQ plays
what it reports native and refuses what would hang it.
"""

import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.model import SetupInfo
from imswitch.imcontrol.model.managers.NidaqManager import scan_digital_outputs
from imswitch.imcontrol.model.managers.scan_engines import conformance_runner
from imswitch.imcontrol.model.managers.scan_engines.CompositeScanEngine import (
    CompositeCompiled, CompositeScanEngine,
)
from imswitch.imcontrol.model.managers.scan_engines.NidaqScanEngine import (
    AO_TRIGGERED_DETECTORS, NidaqScanEngine,
)
from imswitch.imcontrol.model.managers.scan_engines.StepProtocolScanEngine import (
    SimulatedTransport, StepProtocolScanEngine,
)
from imswitch.imcontrol.model.managers.scan_engines.step_board import SimulatedStepBoard
from imswitch.imcontrol.model.managers.scan_engines.step_executor import StepExecutor
from imswitch.imcontrol.model.managers.scan_engines.step_protocol import BoardCaps
from imswitch.imcontrol.model.scan_program import presets
from imswitch.imcontrol.model.scan_program.engine import (
    IMPOSSIBLE, NATIVE, setup_channels,
)
from imswitch.imcontrol.model.scan_program.program import TimelineEvent, validate

from .test_pulse_generator_scan_engine import CHANNELS as PULSE_CHANNELS
from .test_pulse_generator_scan_engine import _engine as _pulse_engine
from .test_pulse_generator_scan_engine import _resolft
from .test_scan_engine_conformance import CHANNELS, GRID_NS, SETUP, _raster, _waiting

SETUPS = 'imswitch/_data/user_defaults/imcontrol_setups/'
EXPERT_MOCK = SETUPS + 'expert_mock_scan_setup.json'
STEP_BOARDS = SETUPS + 'expert_step_boards_mock_setup.json'


def _recorder(engine, names=('sigScanStarted', 'sigScanDone', 'sigScanStopped',
                             'sigScanFailed', 'sigScanBuildFailed')):
    heard = []
    for name in names:
        signal = getattr(engine, name, None)
        if signal is not None:
            signal.connect(lambda *args, n=name: heard.append(n))
    return heard


# --- the pulse generator ends its runs as every engine does ---------------------

def test_a_failed_pulse_generator_run_ends_as_failed_then_build_failed():
    engine = _pulse_engine()
    heard = _recorder(engine)
    engine.prepare(engine.compile(_resolft(), PULSE_CHANNELS))

    def broken(**_kwargs):
        raise OSError('the Teensy stopped answering')
    engine._generator.run = broken
    engine.run(blocking=True)
    assert heard == ['sigScanStarted', 'sigScanFailed', 'sigScanBuildFailed']


def test_a_stopped_pulse_generator_run_ends_as_stopped_then_done():
    engine = _pulse_engine()
    heard = _recorder(engine)
    engine.prepare(engine.compile(_resolft(), PULSE_CHANNELS))
    engine._generator.run = lambda **_kwargs: engine.abort()     # Stop arrives mid-run
    engine.run(blocking=True)
    assert heard == ['sigScanStarted', 'sigScanStopped', 'sigScanDone']


def test_the_expert_panel_ends_a_pulse_generator_run_that_fails(qtbot):
    from .test_scan_controller_expert import ExpertRig, _setup_dict
    setup = _setup_dict(engine='teensy')
    setup['teensyPulse'] = {'port': None, 'useMockOnFailure': True}
    setup['scanEngines'] = {'teensy': {'managerName': 'PulseGeneratorScanEngine',
                                       'managerProperties': {}}}
    setup['lasers']['Pulsed'] = {'managerName': 'PulseGeneratorLaserManager',
                                 'managerProperties': {}, 'digitalLine': 2,
                                 'wavelength': 405, 'valueRangeMin': 0, 'valueRangeMax': 1}
    rig = ExpertRig(setup)
    try:
        rig.scan.setProgram(presets.make('pulse-sequence', dict(
            pulses=[['Pulsed', 1e-3, 2e-3]], camera='', exposure_pulse=1, frames=5,
            pause_s=0.0)))

        def broken(*_args, **_kwargs):
            raise OSError('serial link lost')
        rig.master.pulseGeneratorManager.driver.wait_done = broken
        assert rig.run(timeoutS=10.0), 'the run never ended'
        assert not rig.scan.isRunning
        assert rig.scan._scanCoordinator.tokenForOwner(rig.scan) is None
    finally:
        rig.close()


# --- a step board the host gives up on is stopped, and that is confirmed ---------

def _caps(**more):
    return BoardCaps('B', 'f', grid_ns=GRID_NS, dacs={0: (-10.0, 10.0), 1: (-10.0, 10.0)},
                     ttl_lines=3, trigger_in=True, trigger_latency_ticks=2, **more)


class _Replacing(SimulatedTransport):
    """A link that replaces the first STEP message with ``bad``."""

    def __init__(self, board, bad):
        super().__init__(board)
        self.bad, self.done = bad, False

    def read(self, timeout):
        line = super().read(timeout)
        if line and line.startswith('STEP,') and not self.done:
            self.done = True
            return self.bad
        return line


@pytest.mark.parametrize('bad, reason', [
    ('STEP,\x7f,3', 'the link failed'),          # a garbled message
    ('ERR,state,a fault the board reports', 'state: a fault the board reports'),
])
def test_a_run_the_host_gives_up_on_is_stopped_on_the_board(bad, reason):
    board = SimulatedStepBoard(_caps())
    engine = StepProtocolScanEngine(SimpleNamespace(managerProperties={}), name='Board',
                                    setupInfo=SETUP, transport=_Replacing(board, bad))
    compiled = engine.compile(_waiting(), CHANNELS)
    engine.prepare(compiled)
    engine.run(compiled, arrivals=[], blocking=False)     # it waits without bound
    engine.join(5)
    outcome, why = engine.outcome
    assert outcome == 'failed' and reason in why
    assert 'may still be running' not in why             # the board confirmed
    board.join(5)
    assert not board.running
    engine.prepare(compiled)                              # the link is clean again


class _DeafToStop(SimulatedStepBoard):
    """A board that ignores STOP."""

    def write(self, line):
        if line != 'STOP':
            super().write(line)


class _Linked(SimulatedTransport):
    reply_timeout = 0.2


def _linked_engine(transport):
    return StepProtocolScanEngine(SimpleNamespace(managerProperties={}), name='Board',
                                  setupInfo=SETUP, transport=transport)


def test_close_gives_up_on_a_board_that_ignores_stop_within_the_derived_limit():
    engine = _linked_engine(_Linked(_DeafToStop(_caps())))
    compiled = engine.compile(_waiting(), CHANNELS)
    engine.prepare(compiled)
    engine.run(compiled, arrivals=[], blocking=False)
    limit = engine._stop_limit(engine._transport)
    # a step, the return after it, and the 0.2 s reply time: no number of its own
    plan = compiled.plan
    assert limit == pytest.approx((plan.step_ticks + plan.return_ticks) * GRID_NS / 1e9 + 0.2)
    began = time.monotonic()
    engine.finalize()
    assert time.monotonic() - began < limit + 1.0


class _LinkedReplacing(_Replacing):
    reply_timeout = 0.2


def test_a_failure_on_a_board_that_ignores_stop_says_it_may_still_run():
    engine = _linked_engine(_LinkedReplacing(_DeafToStop(_caps()), 'STEP,\x7f,3'))
    compiled = engine.compile(_waiting(), CHANNELS)
    engine.prepare(compiled)
    engine.run(compiled, arrivals=[], blocking=False)
    engine.join(5)
    outcome, why = engine.outcome
    assert outcome == 'failed'
    assert why.endswith('of a STOP: it may still be running')


class _LateDone(SimulatedTransport):
    """DONE reaches the host late, as over a serial link."""

    def __init__(self, board):
        super().__init__(board)
        self.gate = threading.Event()

    def read(self, timeout):
        line = super().read(timeout)
        if line is not None and line.startswith('DONE'):
            self.gate.wait(5)
        return line


def test_a_stop_that_crosses_the_end_of_the_run_leaves_the_log_readable():
    transport = _LateDone(SimulatedStepBoard(_caps()))
    engine = StepProtocolScanEngine(SimpleNamespace(managerProperties={}), name='Board',
                                    setupInfo=SETUP, transport=transport)
    compiled = engine.compile(_raster(), CHANNELS)
    engine.prepare(compiled)
    engine.run(compiled, blocking=False)
    deadline = time.monotonic() + 5
    while not engine.board.log.of('end') and time.monotonic() < deadline:
        time.sleep(0.001)
    engine.abort()                     # the Stop, a moment too late: OK,STOP waits
    transport.gate.set()
    engine.join(5)
    assert engine.outcome == ('done', '')
    assert engine.log.entries          # the late OK,STOP is passed over


def test_an_error_inside_a_run_fails_it_instead_of_killing_the_board(monkeypatch):
    def broken(self, *args):
        raise RuntimeError('a bug in the walk')
    monkeypatch.setattr(StepExecutor, '_walk', broken)
    board = SimulatedStepBoard(_caps())
    engine = StepProtocolScanEngine(SimpleNamespace(managerProperties={}), name='Board',
                                    setupInfo=SETUP, transport=SimulatedTransport(board))
    compiled = engine.compile(_raster(), CHANNELS)
    engine.prepare(compiled)
    engine.run(compiled, blocking=True)
    assert engine.outcome == ('failed', 'failed at step 0: RuntimeError: a bug in the walk')


# --- a multi-engine run always reports one outcome ------------------------------

def test_a_follower_that_cannot_start_fails_the_run_by_name(qtbot):
    from imswitch.imcontrol.model.managers.scan_engines.loader import build_scan_engines
    from imswitch.imcontrol.view import ViewSetupInfo
    setup = ViewSetupInfo.from_json(open(STEP_BOARDS).read())
    engines = build_scan_engines(setup, nidaqManager=None)
    composite = engines.engine
    try:
        program = presets.make('pulse-sequence', dict(
            pulses=[['405 (ON)', 1e-3, 1e-3], ['561 (EXC)', 2e-3, 1e-3]], camera='Camera',
            exposure_pulse=1, frames=3, pause_s=0.0))
        compiled = composite.compile(program, setup_channels(setup), captured_um={},
                                     devices={})
        composite.prepare(compiled)
        follower = engines.engines['aux']
        follower.board._run = lambda: follower.board._error('state', 'no program to run')
        heard, failures = _recorder(composite), []
        composite.sigScanFailed.connect(failures.append)
        composite.run(compiled, blocking=True)
        assert heard[-2:] == ['sigScanFailed', 'sigScanBuildFailed']
        assert failures[0].startswith('aux: it did not start: StepBoardError')
        assert set(composite.partsOutcome()) == {'triggerscope', 'aux'}
    finally:
        engines.finalize()


class _Stub:
    """A member of a connected (real-board) multi-engine run."""

    def __init__(self, name, fails=False):
        self.name, self.fails = name, fails
        self.started = self.aborted = False
        self.outcome, self._onEnd = None, None
        for signal in ('sigScanStarted', 'sigScanDone', 'sigScanStopped', 'sigScanFailed',
                       'sigScanBuildFailed'):
            setattr(self, signal, _Emitter())

    def run(self, part, *, quiet, on_end, blocking=False, **_kwargs):
        if self.fails:
            raise OSError(f'{self.name} is unplugged')
        self.started, self._onEnd = True, on_end

    def abort(self):
        self.aborted = True
        if self.started and self.outcome is None:
            self.outcome = ('stopped', '')
            self._onEnd(self)
        return 'step-boundary'


class _Emitter:
    def __init__(self):
        self.heard = []

    def emit(self, *args):
        self.heard.append(args)

    def connect(self, slot):
        pass


def test_a_connected_run_stops_the_armed_followers_and_never_starts_the_leader():
    leader, armed, unplugged = _Stub('Lead'), _Stub('First'), _Stub('Second', fails=True)
    composite = CompositeScanEngine(leader, {'First': (armed, 'Lead/ttl1'),
                                             'Second': (unplugged, 'Lead/ttl2')})
    compiled = CompositeCompiled(program=None, report=None, leader='lead part',
                                 followers={'First': 'p1', 'Second': 'p2'}, wires={})
    composite.run(compiled, blocking=True)
    assert armed.started and armed.aborted
    assert not leader.started
    assert leader.sigScanFailed.heard == [('Second: it did not start: OSError: Second is '
                                           'unplugged',)]
    assert leader.sigScanBuildFailed.heard == [()]
    assert composite.partsOutcome()['Lead'] == ('stopped', 'not started: Second did not start')


def test_a_run_that_raises_ends_every_part_failed():
    leader, follower = _Stub('Lead'), _Stub('Follow')
    composite = CompositeScanEngine(leader, {'Follow': (follower, 'Lead/ttl1')})
    composite._run = {'parts': {'Lead': leader, 'Follow': follower}, 'ended': {},
                      'stopping': False, 'finished': False}

    def broken():
        raise KeyError('a bug in the split')
    composite._guarded(broken)
    assert composite.partsOutcome() == {
        'Lead': ('failed', "the run failed: KeyError: 'a bug in the split'"),
        'Follow': ('failed', "the run failed: KeyError: 'a bug in the split'")}
    assert leader.sigScanBuildFailed.heard == [()]


# --- a follower wired to the NI-DAQ ------------------------------------------------

def _follower_of_nidaq(setup):
    setup['scanEngines'] = {'follow': {
        'managerName': 'SimulatedStepEngine', 'triggerInput': 'Dev1/port0/line6',
        'managerProperties': {'timingGridS': 1e-5, 'digitalLines': ['follow/ttl0'],
                              'triggerLatencyS': 2e-5, 'triggerMinPulseS': 2e-5,
                              'clockAccuracyPpm': 20.0}}}
    return setup


def test_nidaq_programs_still_run_with_a_follower_wired_to_it(qtbot):
    from .test_scan_controller_expert import ExpertRig, _ms_resolft, _setup_dict
    rig = ExpertRig(_follower_of_nidaq(_setup_dict()))
    try:
        assert isinstance(rig.master.scanEngine, CompositeScanEngine)
        iterations = []
        rig.master.nidaqManager.sigScanDone.connect(lambda *a: iterations.append(1))
        rig.scan.setProgram(_ms_resolft(rig, timepoints=1))
        assert rig.run(timeoutS=10.0) and not rig.rejected
        assert iterations == [1], 'the NI-DAQ never played the program'
    finally:
        rig.close()


def test_the_setup_dialog_says_the_nidaq_cannot_lead_and_never_advises_it():
    from imswitch.imcontrol.model.managers.scan_engines.capability_report import (
        setup_capabilities,
    )
    from imswitch.imcontrol.model.managers.scan_engines.loader import build_scan_engines
    from imswitch.imcontrol.view import ViewSetupInfo
    from .test_scan_controller_expert import _setup_dict
    raw = _follower_of_nidaq(_setup_dict())
    # and a step engine that follows nothing, with a laser on it: the NI-DAQ
    # cannot lead it either, so wiring it to follow is not advised
    raw['scanEngines']['idle'] = {
        'managerName': 'SimulatedStepEngine',
        'managerProperties': {'timingGridS': 1e-5, 'digitalLines': ['idle/ttl0'],
                              'triggerLatencyS': 2e-5}}
    raw['lasers']['Idle laser'] = {'managerName': 'NidaqLaserManager', 'digitalLine': 'idle/ttl0',
                                   'analogChannel': None, 'wavelength': 640,
                                   'valueRangeMin': 0, 'valueRangeMax': 1,
                                   'managerProperties': {}}
    setup = ViewSetupInfo.from_json(json.dumps(raw))
    engines = build_scan_engines(setup, nidaqManager=SimpleNamespace(
        sigScanBuilt=None, sigScanStarted=None, sigScanDone=None, sigScanBuildFailed=None))
    report = setup_capabilities(engines, setup)
    nidaq, follow = report.section('nidaq'), report.section('follow')
    assert 'follow is wired to follow it, but nidaq cannot pulse the line' in nidaq.role
    entry = next(e for e in follow.features if e.item == 'follow step by step')
    assert entry.verdict == IMPOSSIBLE and 'nidaq cannot pulse the line' in entry.text
    idle = report.section('idle')
    assert idle.devices and idle.role.endswith('To scan its devices, make it scan.engine.')
    assert 'wire its triggerInput' not in report.text()


# --- the NI-DAQ: clock lines, markers, programs without an analog output ----------

def _ms_resolft_program():
    return presets.make('ms-resolft', dict(
        device='Z', cycles=2, planes=3, cycle_step_um=0.25, start_um=1.0,
        pulses=[['405 (ON)', 1e-3, 1e-3], ['488 (EXC)', 2e-3, 1e-3], ['488 (EXC)', 1e-3, 0.0]],
        camera='Camera', exposure_pulse=3, timepoints=1, pause_s=0.0,
        move_s=5e-4, settle_s=5e-4, transition='jump'))


def _expert_setup(edit=None):
    setup = json.load(open(EXPERT_MOCK))
    if edit is not None:
        edit(setup)
    return SetupInfo.from_json(json.dumps(setup), infer_missing=True)


def _nidaq(setup):
    return NidaqScanEngine(None, nidaqManager=None, setupInfo=setup)


def test_a_wired_clock_the_program_does_not_mark_is_held_low(qtbot):
    from .test_scan_controller_expert import ExpertRig

    def clock(setup):
        setup['scan']['lineClockLine'] = 'Dev1/port0/line6'
    setup = _expert_setup(clock)
    compiled = _nidaq(setup).compile(_ms_resolft_program(), setup_channels(setup),
                                     captured_um={'Z': 0.0})
    held = compiled.signalDict['TTLCycleSignalsDict']['line_clock']
    assert held.dtype == bool and not held.any()
    assert held.size == next(iter(compiled.signalDict['scanSignalsDict'].values())).size
    entry = compiled.report.verdict('clock line_clock')
    assert entry.verdict == NATIVE and 'held low' in entry.reason

    raw = json.load(open(EXPERT_MOCK))
    clock(raw)
    rig = ExpertRig(setup=raw)
    try:
        rig.scan.setProgram(_ms_resolft_program())
        assert rig.run(timeoutS=10.0) and not rig.rejected       # was KeyError: 'line_clock'
    finally:
        rig.close()


def _with_marker(line):
    program = _ms_resolft_program()
    marker = TimelineEvent(line, 'marker', 0.0, 1e-3, during='any')
    return validate(program.__class__(**{**program.__dict__,
                                          'timeline': program.timeline + (marker,)}))


def test_a_marker_on_a_line_of_its_own_is_played():
    setup = _expert_setup()
    compiled = _nidaq(setup).compile(_with_marker('Dev1/port0/line5'), setup_channels(setup),
                                     captured_um={'Z': 0.0})
    ttl = compiled.signalDict['TTLCycleSignalsDict']
    assert ttl['Dev1/port0/line5'].any()
    devices, signals, lines = scan_digital_outputs(
        ttl, [(name, info.getDigitalLine()) for name, info in setup.getAllDevices().items()
              if info.getDigitalLine()], setup.getDevice)
    played = dict(zip(lines, signals))
    assert np.array_equal(played['Dev1/port0/line5'], ttl['Dev1/port0/line5'])


@pytest.mark.parametrize('line, reason', [
    ('DevBoard', 'is not an NI-DAQmx line'),
])
def test_a_marker_the_nidaq_cannot_play_is_refused(line, reason):
    setup = _expert_setup()
    report = _nidaq(setup).assess(_with_marker(line), setup_channels(setup))
    verdict = report.verdict(f'marker {line}')
    assert verdict.verdict == IMPOSSIBLE and reason in verdict.reason


def test_a_marker_on_a_devices_line_is_refused_by_name():
    setup = _expert_setup()
    camera_line = setup.detectors['Camera'].digitalLine
    report = _nidaq(setup).assess(_with_marker(camera_line), setup_channels(setup))
    verdict = report.verdict(f'marker {camera_line}')
    assert verdict.verdict == IMPOSSIBLE and "is Camera's line" in verdict.reason


def _pulse_sequence():
    return presets.make('pulse-sequence', dict(
        pulses=[['405 (ON)', 1e-3, 1e-3]], camera='Camera', exposure_pulse=1, frames=2,
        pause_s=0.0))


def test_a_program_without_an_analog_output_is_refused_where_it_would_hang():
    setup = _expert_setup()                     # APDs start on the AO start trigger
    verdict = _nidaq(setup).assess(_pulse_sequence(), setup_channels(setup)).verdict(
        'analog output')
    assert verdict.verdict == IMPOSSIBLE and 'would wait for its start trigger' in verdict.reason

    def no_point_detectors(raw, start_trigger):
        raw['detectors'] = {k: v for k, v in raw['detectors'].items()
                            if v['managerName'] not in AO_TRIGGERED_DETECTORS}
        raw['nidaq']['startTrigger'] = start_trigger
    calm = _expert_setup(lambda raw: no_point_detectors(raw, False))
    assert _nidaq(calm).assess(_pulse_sequence(), setup_channels(calm)).verdict(
        'analog output') is None
    triggered = _expert_setup(lambda raw: no_point_detectors(raw, True))
    assert 'nidaq.startTrigger' in _nidaq(triggered).assess(
        _pulse_sequence(), setup_channels(triggered)).verdict('analog output').reason


def test_the_ao_triggered_detectors_are_exactly_those_that_start_on_it():
    from pathlib import Path
    import imswitch.imcontrol.model.managers.detectors as detectors
    folder = Path(detectors.__file__).parent
    starting = {path.stem for path in folder.glob('*Manager.py')
                if "ao/StartTrigger" in path.read_text(encoding='utf-8')}
    assert starting == set(AO_TRIGGERED_DETECTORS)


# --- the conformance tool does not hang on a board that never ends ---------------

class _NeverDone(SimulatedStepBoard):
    def _reply(self, line):
        if not line.startswith('DONE'):
            super()._reply(line)


def test_a_board_that_never_reports_the_end_fails_the_program(monkeypatch):
    programs = conformance_runner._programs
    monkeypatch.setattr(conformance_runner, '_programs',
                        lambda *args: dict(list(programs(*args).items())[:1]))
    caps = BoardCaps('Never', 'f', grid_ns=1000, dacs={0: (-10.0, 10.0)}, ttl_lines=3)
    results = conformance_runner.run(_Linked(_NeverDone(caps)), report=lambda _: None)
    [result] = results
    assert not result.passed
    assert result.problems[0].startswith('no end of the run reported within')


def test_trigger_waits_on_a_real_link_need_a_trigger_timeout():
    caps = BoardCaps('B', 'f', grid_ns=1000, dacs={0: (-10.0, 10.0)}, ttl_lines=3,
                     trigger_in=True, trigger_latency_ticks=2)
    with pytest.raises(ValueError, match='need trigger_timeout_s'):
        conformance_runner.run(_Linked(SimulatedStepBoard(caps)), trigger=True,
                               report=lambda _: None)


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
