"""Any pulse generator as a scan engine (docs/design/plans/
scan-engines-and-expert-scan.md §5.3, M3).

The adapter compiles a program into the generator's step table and plays
it on the mock Teensy driver, whose virtual-time pin timeline is compared
with the interpreter's realized schedule: what the board did against what
the program means. Every refusal the plan lists is checked, with the
capacity edge (256 steps fit, 257 do not).
"""

import threading
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.SetupInfo import TeensyPulseInfo
from imswitch.imcontrol.model.managers.pulsegen.TeensyPulseManager import TeensyPulseManager
from imswitch.imcontrol.model.managers.scan_engines.PulseGeneratorScanEngine import (
    PulseGeneratorScanEngine,
)
from imswitch.imcontrol.model.scan_program import (
    DeviceDrive, Loop, Motion, RunContext, ScanProgram, TimelineEvent, Wait, expand, realize,
)
from imswitch.imcontrol.model.scan_program.conformance import expected_log
from imswitch.imcontrol.model.scan_program.engine import (
    DeviceChannels, NATIVE, ScanEngineRefused, SetupError, UNKNOWN,
)

#: The Teensy's grid: whole microseconds on the wire.
RATE = 1_000_000
NS_PER_TICK = 1_000

LINES = {'L405': 0, 'L488': 1, 'Camera': 2}
CHANNELS = {name: DeviceChannels(name, digital=str(ch), kind='detector' if name == 'Camera'
                                 else 'laser') for name, ch in LINES.items()}
CHANNELS['Stage'] = DeviceChannels('Stage', analog='Dev1/ao0', kind='positioner')


def _setup(**levels):
    def device(name):
        return SimpleNamespace(digitalLine=LINES[name], ttlActiveLevel=levels.get(name, 'high'))
    return SimpleNamespace(lasers={'L405': device('L405'), 'L488': device('L488')},
                           detectors={'Camera': device('Camera')})


def _teensy(**mock):
    info = TeensyPulseInfo(port=None, useMockOnFailure=True,
                           **{'mockNChannels': 16, 'mockMinPulseUs': 1, 'mockMaxSteps': 256,
                              **mock})
    return TeensyPulseManager(info)


def _engine(generator=None, setup=None, **props):
    generator = generator or _teensy()
    return PulseGeneratorScanEngine(
        SimpleNamespace(managerProperties=props), name='Teensy',
        pulseGeneratorManager=generator, setupInfo=setup or _setup())


def _resolft(frames=4, **extra):
    """Widefield RESOLFT switching: on, off, then read out, every frame."""
    return ScanProgram(
        loops=(Loop('frame', 'time', frames),), drives=(), step_s=10e-3,
        timeline=(TimelineEvent('L405', 'ttl', 0.0, 1e-3, during='any'),
                  TimelineEvent('L488', 'ttl', 2e-3, 5e-3, during='any'),
                  TimelineEvent('Camera', 'detector', 6e-3, 9e-3, during='any')),
        timing_policy='strict-v1', **extra)


def _played(engine, program):
    driver = engine._generator.driver
    since = driver._virtual_clock_ns
    compiled = engine.compile(program, CHANNELS)
    engine.prepare(compiled)
    engine.run(blocking=True)
    return compiled, [e for e in driver.timeline if e.source == 'seq'
                      and e.virtual_time_ns >= since], since


def _pulses(transitions, since):
    """``{channel: [(start_tick, end_tick)]}`` from pin transitions."""
    out, high = {}, {}
    for event in transitions:
        tick = (event.virtual_time_ns - since) // NS_PER_TICK
        if event.level:
            high[event.channel] = tick
        else:
            out.setdefault(event.channel, []).append((high.pop(event.channel), tick))
    return out


def _meaning(program):
    """What the program means, per line, touching windows as one pulse."""
    log = expected_log(realize(expand(program, RunContext()), RATE))
    out = {}
    for _segment, start, end, device in log['on']:
        windows = out.setdefault(LINES[device], [])
        if windows and windows[-1][1] == start:
            windows[-1] = (windows[-1][0], end)
        else:
            windows.append((start, end))
    return out


# ---------------------------------------------------------------------------
# It plays what the program means
# ---------------------------------------------------------------------------

def test_the_teensy_plays_what_the_program_means():
    engine = _engine()
    compiled, transitions, since = _played(engine, _resolft())
    assert _pulses(transitions, since) == _meaning(_resolft())
    # One frame's pattern, played four times: the table holds the period.
    assert compiled.n_reps == 4 and len(compiled.steps) == 6


def test_after_the_run_every_line_is_off():
    """The program ends with the camera on -- the Teensy keeps its last
    step on the wire -- so only the engine's safe state turns it off."""
    program = ScanProgram(
        loops=(Loop('frame', 'time', 3),), drives=(), step_s=10e-3,
        timeline=(TimelineEvent('L405', 'ttl', 0.0, 1e-3, during='any'),
                  TimelineEvent('Camera', 'detector', 6e-3, 10e-3, during='any')),
        timing_policy='strict-v1')
    engine = _engine()
    _played(engine, program)
    assert not any(engine._generator.driver.pin_states.values())


def test_phases_that_alternate_make_a_longer_period():
    program = ScanProgram(
        loops=(Loop('frame', 'time', 3), Loop('phase', 'condition', 2)), drives=(),
        step_s=5e-3,
        timeline=(TimelineEvent('L405', 'ttl', 0.0, 1e-3, phases=frozenset({0}), during='any'),
                  TimelineEvent('L488', 'ttl', 0.0, 2e-3, phases=frozenset({1}), during='any'),
                  TimelineEvent('Camera', 'detector', 3e-3, 4e-3, during='any')),
        timing_policy='strict-v1')
    engine = _engine()
    compiled, transitions, since = _played(engine, program)
    assert _pulses(transitions, since) == _meaning(program)
    assert compiled.n_reps == 3


def test_a_report_says_what_runs_natively():
    report = _engine().compile(_resolft(), CHANNELS).report
    assert report.runnable
    assert report.verdict('ttl L405').verdict == NATIVE
    assert report.verdict('detector Camera').verdict == NATIVE


# ---------------------------------------------------------------------------
# Refusals name the feature and the limit
# ---------------------------------------------------------------------------

def _refusal(engine, program):
    with pytest.raises(ScanEngineRefused) as caught:
        engine.compile(program, CHANNELS)
    return caught.value.report


def test_a_position_needs_an_analog_output():
    program = ScanProgram(loops=(Loop('z', 'scan_z', 3),),
                          drives=(DeviceDrive('Stage', weights_um={'z': 1.0},
                                              motion=Motion(transition='jump')),),
                          step_s=10e-3, timeline=_resolft().timeline, timing_policy='strict-v1')
    reasons = _refusal(_engine(), program).reasons()
    assert any('Stage needs an analog output; Teensy has none.' in r for r in reasons)


def test_a_power_level_is_analog():
    program = _resolft()
    program = ScanProgram(loops=program.loops, drives=(), step_s=program.step_s,
                          timeline=program.timeline + (
                              TimelineEvent('L488', 'level', 2e-3, 5e-3, value=40.0,
                                            during='any'),),
                          timing_policy='strict-v1')
    reasons = _refusal(_engine(), program).reasons()
    assert any('The L488 power level is an analog event; pulse generators play digital '
               'states only.' in r for r in reasons)


def test_waiting_before_every_step_needs_a_per_step_trigger():
    program = _resolft(waits=(Wait(at='step', trigger='external'),))
    reasons = _refusal(_engine(), program).reasons()
    assert any('Waiting before every step needs a per-step trigger; Teensy takes none.' in r
               for r in reasons)


def test_the_teensy_takes_no_start_trigger():
    program = _resolft(waits=(Wait(at='start', trigger='external'),))
    reasons = _refusal(_engine(), program).reasons()
    assert any('Teensy takes no start trigger' in r for r in reasons)


def _steps_program(distinct):
    """A pulse train with ``distinct`` steps that never repeat: every pulse
    one tick longer than the one before, then a pause as long. Returns the
    events and the train's length in ticks."""
    events, tick = [], 0
    for k in range(distinct // 2):
        width = k + 1
        events.append(TimelineEvent('L405', 'ttl', tick / RATE, (tick + width) / RATE,
                                    during='any'))
        tick += 2 * width
    return events, tick


@pytest.mark.parametrize('distinct, fits', [(256, True), (258, False)])
def test_the_capacity_edge(distinct, fits):
    events, length = _steps_program(distinct)
    program = ScanProgram(loops=(Loop('once', 'time', 1),), drives=(), step_s=length / RATE,
                          timeline=tuple(events), timing_policy='strict-v1')
    engine = _engine()
    if fits:
        compiled = engine.compile(program, CHANNELS)
        assert len(compiled.steps) == distinct
    else:
        reasons = _refusal(engine, program).reasons()
        assert any(f'This timeline needs {distinct} distinct steps; Teensy holds 256.' in r
                   for r in reasons)


def test_257_steps_do_not_fit_256():
    events, length = _steps_program(256)
    # one step more: L488 on from the end of the train to the end of the step
    program = ScanProgram(loops=(Loop('once', 'time', 1),), drives=(),
                          step_s=(length + 10) / RATE,
                          timeline=tuple(events) + (
                              TimelineEvent('L488', 'ttl', length / RATE, (length + 10) / RATE,
                                            during='any'),),
                          timing_policy='strict-v1')
    reasons = _refusal(_engine(), program).reasons()
    assert any('needs 257 distinct steps; Teensy holds 256' in r for r in reasons)


def test_a_repeated_sequence_at_the_capacity_edge_fits():
    events, length = _steps_program(256)
    program = ScanProgram(loops=(Loop('frame', 'time', 50),), drives=(), step_s=length / RATE,
                          timeline=tuple(events), timing_policy='strict-v1')
    compiled = _engine().compile(program, CHANNELS)
    assert (len(compiled.steps), compiled.n_reps) == (256, 50)


def test_a_time_off_the_microsecond_grid_is_refused_with_its_neighbours():
    program = ScanProgram(loops=(Loop('frame', 'time', 2),), drives=(), step_s=10e-3,
                          timeline=(TimelineEvent('L405', 'ttl', 0.0, 1.5e-6, during='any'),),
                          timing_policy='strict-v1')
    reasons = ' '.join(_refusal(_engine(), program).reasons())
    assert 'not on the 1 µs grid' in reasons and '1 µs or 2 µs' in reasons


def test_an_active_low_line_is_refused_because_a_stop_drives_it_low():
    engine = _engine(setup=_setup(L488='low'))
    reasons = _refusal(engine, _resolft()).reasons()
    assert any('A stop drives every Teensy channel low, which switches L488 (active low) on.'
               in r for r in reasons)


def test_a_line_on_another_engine_is_named():
    channels = dict(CHANNELS, L488=DeviceChannels('L488', digital='Dev1/port0/line1'))
    with pytest.raises(ScanEngineRefused) as caught:
        _engine().compile(_resolft(), channels)
    assert any('L488 is wired to Dev1/port0/line1, which Teensy does not drive.' in r
               for r in caught.value.report.reasons())


class _Undeclared:
    """A pulse generator that declares only the required capabilities."""
    def __init__(self):
        self.n_digital_channels = 8
        self.min_pulse_width_ns = 8
        self.max_sequence_steps = None
        self.timing_resolution_ns = None
        self.max_repeats = None
        self.run_triggers = frozenset({'none'})
        self.stop_drives_low = None
        self.stop_granularity = None
        self.sigSequenceStarted = self.sigSequenceDone = self.sigSequenceFailed = \
            SimpleNamespace(connect=lambda *slot_and_type: None)


class _HoldsOnStop(_Undeclared):
    """A generator whose stop leaves its outputs as they are, recording what
    it is given."""
    def __init__(self):
        super().__init__()
        self.timing_resolution_ns = 1_000
        self.max_sequence_steps = 256
        self.max_repeats = 1_000
        self.stop_drives_low = False
        self.stop_granularity = 'immediate'
        self.programmed = None
        self.levels = {}

    def program_sequence(self, steps):
        self.programmed = list(steps)

    def run(self, n_reps=1, blocking=False):
        pass

    def stop(self):
        pass

    def setDigital(self, channel, level):
        self.levels[channel] = level


def test_an_active_low_line_is_low_for_on_and_high_otherwise():
    generator = _HoldsOnStop()
    engine = _engine(generator=generator, setup=_setup(L488='low'))
    compiled = engine.compile(_resolft(frames=1), CHANNELS)
    engine.prepare(compiled)
    by_state = [(step.duration_ns // NS_PER_TICK, step.channel_states[1])
                for step in generator.programmed]
    # L488 is on 2-5 ms: low there, high around it
    assert by_state == [(1000, True), (1000, True), (3000, False), (1000, True),
                        (3000, True), (1000, True)]
    engine.run(blocking=True)
    assert generator.levels[1] is True and generator.levels[0] is False   # off levels


def test_what_a_generator_does_not_declare_is_unknown_never_assumed():
    engine = _engine(generator=_Undeclared())
    report = _refusal(engine, _resolft())
    assert report.verdict('timing').verdict == UNKNOWN
    assert 'does not declare its timing resolution' in report.verdict('timing').reason
    caps = engine.capabilities()
    assert caps.timing_grid_s is None and caps.max_steps is None and caps.abort == 'unknown'


def test_undeclared_capacity_and_repeats_are_unknown():
    generator = _Undeclared()
    generator.timing_resolution_ns = 1_000
    report = _refusal(_engine(generator=generator), _resolft())
    assert report.verdict('steps').verdict == UNKNOWN
    assert report.verdict('repeats').verdict == UNKNOWN


# ---------------------------------------------------------------------------
# Setup and lifecycle
# ---------------------------------------------------------------------------

def test_a_setup_runs_its_teensy_as_an_engine():
    """Through the loader, as a setup file builds it: a teensyPulse section
    for the generator, a scanEngines entry for the engine, a laser wired to
    channel 1 by its digitalLine."""
    import json
    from imswitch.imcontrol.model.managers.scan_engines.loader import build_scan_engines
    from imswitch.imcontrol.view import ViewSetupInfo
    from .test_scan_engines_setup import _FakeNidaq, _setup_dict
    setup = _setup_dict(scanEngines={'teensy': {'managerName': 'PulseGeneratorScanEngine',
                                                'managerProperties': {}}})
    setup['teensyPulse'] = {'port': None, 'useMockOnFailure': True}
    setup['lasers']['Pulsed'] = {'managerName': 'PulseGeneratorLaserManager',
                                 'managerProperties': {}, 'digitalLine': 1,
                                 'wavelength': 405, 'valueRangeMin': 0, 'valueRangeMax': 1}
    info = ViewSetupInfo.from_json(json.dumps(setup))
    engines = build_scan_engines(info, nidaqManager=_FakeNidaq(),
                                 lowLevelManagers={'pulseGeneratorManager': _teensy()})
    assert sorted(engines.engines) == ['nidaq', 'teensy']
    assert engines.owner_of('1') == 'teensy'
    for name, laser in info.lasers.items():
        if name != 'Pulsed':
            assert engines.owner_of(laser.getDigitalLine()) == 'nidaq', name


def test_it_owns_the_generators_channels_by_index():
    engine = _engine(channels=[0, 1])
    assert engine.owns('0') and engine.owns('1') and not engine.owns('2')
    assert engine.capabilities().digital == ('0', '1')
    with pytest.raises(SetupError, match='outside'):
        _engine(channels=[16])


def test_without_a_pulse_generator_the_setup_says_which():
    with pytest.raises(SetupError, match="no pulse generator 'pulseGeneratorManager'"):
        PulseGeneratorScanEngine(SimpleNamespace(managerProperties={}), name='Teensy')


def test_a_stop_leaves_every_line_off(qtbot):
    """On a generator that holds its outputs on a stop (the Teensy's own STOP
    drives every line low, which would hide the engine's part), with L405 on
    through every step: only the engine's safe state turns it off."""
    engine = _engine()
    program = ScanProgram(
        loops=(Loop('frame', 'time', 200),), drives=(), step_s=10e-3,
        timeline=(TimelineEvent('L405', 'ttl', 0.0, 10e-3, during='any'),
                  TimelineEvent('Camera', 'detector', 6e-3, 9e-3, during='any')),
        timing_policy='strict-v1')
    compiled = engine.compile(program, CHANNELS)
    engine.prepare(compiled)
    driver = engine._generator.driver
    applied = driver._set_pin
    driver._set_pin = lambda ch, wanted, source='seq': (
        None if source == 'stop' else applied(ch, wanted, source=source))
    driver.simulated_step_delay_s = 1e-3
    stopped = []
    engine.sigScanStopped.connect(lambda: stopped.append(True))
    engine.run(blocking=False)
    assert engine.abort() == 'step-boundary'
    assert engine.join(5.0)
    assert not any(driver.pin_states.values())
    qtbot.waitUntil(lambda: stopped == [True], timeout=2000)   # sent from the run thread


def _ends(engine):
    ends = []
    engine.sigScanStopped.connect(lambda: ends.append('stopped'))
    engine.sigScanDone.connect(lambda: ends.append('done'))
    engine.sigScanFailed.connect(lambda reason: ends.append(('failed', reason)))
    return ends


def _refusing(engine, channels):
    """The generator refuses to set ``channels`` (a serial write failing)."""
    setDigital = engine._generator.setDigital

    def setDigitalOr(channel, value):
        if channel in channels:
            raise OSError('serial write failed')
        return setDigital(channel, value)
    engine._generator.setDigital = setDigitalOr


def test_a_line_left_on_that_does_not_turn_off_fails_the_run(qtbot):
    """Both lasers are on through the last step; L405's off write fails. The
    run fails naming it, L488 is still turned off, the line stays unknown,
    no new run starts on it, and the close reports it until it turns off
    (review 2026-10-17, P1)."""
    engine = _engine()
    program = ScanProgram(
        loops=(Loop('frame', 'time', 1),), drives=(), step_s=1e-3,
        timeline=(TimelineEvent('L405', 'ttl', 0.0, 1e-3, during='any'),
                  TimelineEvent('L488', 'ttl', 0.0, 1e-3, during='any')),
        timing_policy='strict-v1')
    engine.prepare(engine.compile(program, CHANNELS))
    ends = _ends(engine)
    refused = {LINES['L405']}
    _refusing(engine, refused)
    engine.run(blocking=True)
    assert len(ends) == 1 and ends[0][0] == 'failed'
    assert 'line 0 (L405)' in ends[0][1] and 'unknown' in ends[0][1]
    pins = engine._generator.driver.pin_states
    assert pins[LINES['L405']] is True              # as the board left it
    assert pins[LINES['L488']] is False             # every line is tried
    assert engine.unknownLines() == {LINES['L405']: 'serial write failed'}
    with pytest.raises(RuntimeError, match='Not starting a run'):
        engine.run()
    assert engine.finalize() is False
    refused.clear()
    assert engine.finalize() is True
    assert engine.unknownLines() == {}
    assert pins[LINES['L405']] is False


def test_a_stop_refused_while_the_board_plays_is_tried_again_at_the_runs_end(qtbot):
    """A stop's own off writes may be refused while the board still plays:
    that is deferred, not a failure -- the run's end sets the lines, and the
    run ends stopped."""
    engine = _engine()
    program = ScanProgram(
        loops=(Loop('frame', 'time', 200),), drives=(), step_s=10e-3,
        timeline=(TimelineEvent('L405', 'ttl', 0.0, 10e-3, during='any'),),
        timing_policy='strict-v1')
    engine.prepare(engine.compile(program, CHANNELS))
    driver = engine._generator.driver
    applied = driver._set_pin
    driver._set_pin = lambda ch, wanted, source='seq': (
        None if source == 'stop' else applied(ch, wanted, source=source))
    driver.simulated_step_delay_s = 1e-3
    setDigital = engine._generator.setDigital

    def refusedWhilePlaying(channel, value):
        thread = engine._thread
        if (thread is not None and thread.is_alive()
                and threading.current_thread() is not thread):
            raise RuntimeError('a sequence is running')
        return setDigital(channel, value)
    engine._generator.setDigital = refusedWhilePlaying
    ends = _ends(engine)
    engine.run(blocking=False)
    engine.abort()
    assert engine.join(5.0)
    qtbot.waitUntil(lambda: 'done' in ends, timeout=2000)
    assert ends == ['stopped', 'done']
    assert not any(driver.pin_states.values())
    assert engine.unknownLines() == {}


def test_the_teensy_declares_what_the_engine_needs():
    teensy = _teensy()
    assert teensy.max_sequence_steps == 256
    assert teensy.timing_resolution_ns == 1_000
    assert teensy.max_repeats == 2 ** 32 - 1
    assert teensy.run_triggers == frozenset({'none'})
    assert teensy.stop_drives_low is True
    with pytest.raises(ValueError, match="no 'start' trigger"):
        teensy.run(trigger='start')


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
