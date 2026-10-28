"""Several engines in one scan (docs/design/plans/scan-engines-and-expert-scan.md
§5.4, M6): a leader runs the whole program and pulses a wired line at every
step; a follower waits for the pulse before every step and runs its devices'
part shifted by its trigger latency.

Conformance across engines: every device's DAC writes and line edges, in
absolute time, are where the interpreter puts them -- whichever engine drives
the device, on whichever grid. The follower's arrivals are the leader's
pulses (a simulated wire), not anything the test hands it.
"""

import threading
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.managers.scan_engines import (
    SimulatedStepEngine, StepProtocolScanEngine,
)
from imswitch.imcontrol.model.managers.scan_engines.CompositeScanEngine import (
    CompositeCompiled, CompositeScanEngine,
)
from imswitch.imcontrol.model.managers.scan_engines.StepProtocolScanEngine import (
    SimulatedTransport,
)
from imswitch.imcontrol.model.managers.scan_engines.loader import build_scan_engines
from imswitch.imcontrol.model.managers.scan_engines.step_board import SimulatedStepBoard
from imswitch.imcontrol.model.managers.scan_engines.step_protocol import BoardCaps
from imswitch.imcontrol.model.scan_program import (
    DeviceDrive, Loop, Motion, ScanProgram, TimelineEvent, Wait, expand, realize,
)
from imswitch.imcontrol.model.scan_program.conformance import engine_log, expected_log
from imswitch.imcontrol.model.scan_program.engine import (
    DeviceChannels, IMPOSSIBLE, NATIVE, ScanEngineRefused, SetupError, UNKNOWN,
)
from imswitch.imcontrol.model.scan_program.program import Separation

RATE = 100_000                 # the leader's grid, 10 µs
GRID_S = 1 / RATE
JUMP = Motion(transition='jump', move_s=1e-3, settle_s=1e-3, return_s=2e-3,
              origin='absolute', restore='first')
PULSE_LINE = 'Lead/ttl3'

CHANNELS = {
    'X': DeviceChannels('X', analog='Lead/dac0', kind='positioner'),
    'L405': DeviceChannels('L405', digital='Lead/ttl0', kind='laser'),
    'RO': DeviceChannels('RO', analog='Follow/dac0', kind='positioner'),
    'L488': DeviceChannels('L488', digital='Follow/ttl0', kind='laser'),
    'Camera': DeviceChannels('Camera', digital='Follow/ttl1', kind='detector'),
}
SETUP = SimpleNamespace(
    positioners={'X': SimpleNamespace(managerProperties={'conversionFactor': 2.0}),
                 'RO': SimpleNamespace(managerProperties={'conversionFactor': 0.5})},
    lasers={'L405': SimpleNamespace(ttlActiveLevel='high'),
            'L488': SimpleNamespace(ttlActiveLevel='high')},
    detectors={'Camera': SimpleNamespace(ttlActiveLevel='low')})


def _leader(**props):
    props = dict({'timingGridS': GRID_S, 'analogChannels': {'Lead/dac0': [-10, 10]},
                  'digitalLines': ['Lead/ttl0', PULSE_LINE], 'triggerLatencyS': 2 * GRID_S,
                  'clockAccuracyPpm': 20.0}, **props)
    return SimulatedStepEngine(SimpleNamespace(managerProperties=props), name='Lead')


def _protocol_follower(grid_ns=5_000, latency_ticks=3, board_cls=SimulatedStepBoard, **caps):
    """A protocol board on a 5 µs grid: two of its ticks to the leader's one."""
    caps = BoardCaps(**{**dict(board='Simulated', firmware='test', grid_ns=grid_ns,
                               dacs={0: (-10.0, 10.0)}, ttl_lines=2, trigger_in=True,
                               trigger_latency_ticks=latency_ticks, trigger_min_ticks=4,
                               clock_ppm=20.0), **caps})
    board = board_cls(caps, latency_ticks=latency_ticks)
    return StepProtocolScanEngine(SimpleNamespace(managerProperties={}), name='Follow',
                                  setupInfo=SETUP, transport=SimulatedTransport(board))


def _simulated_follower(cls=SimulatedStepEngine, **props):
    props = dict({'timingGridS': GRID_S / 2, 'analogChannels': {'Follow/dac0': [-10, 10]},
                  'digitalLines': ['Follow/ttl0', 'Follow/ttl1'],
                  'triggerLatencyS': 3 * GRID_S / 2, 'triggerMinPulseS': 2 * GRID_S,
                  'clockAccuracyPpm': 20.0}, **props)
    return cls(SimpleNamespace(managerProperties=props), name='Follow')


FOLLOWERS = {'protocol board': _protocol_follower, 'simulated board': _simulated_follower}


@pytest.fixture(params=sorted(FOLLOWERS))
def follower(request):
    return FOLLOWERS[request.param]


def _composite(follower, leader=None):
    return CompositeScanEngine(leader or _leader(), {'Follow': (follower, PULSE_LINE)})


def _resolft(**extra):
    """2 cycles x 3 planes: X (leader) steps with the cycle, RO (follower)
    with the plane; one laser on each engine, the camera on the follower."""
    return ScanProgram(**{**dict(
        loops=(Loop('cycle', 'cycle', 2), Loop('plane', 'plane', 3)),
        drives=(DeviceDrive('X', weights_um={'cycle': 5.0}, motion=JUMP),
                DeviceDrive('RO', weights_um={'plane': 0.5}, motion=JUMP)),
        step_s=20e-3,
        timeline=(TimelineEvent('L405', 'ttl', 0.0, 2e-3, during='any', id='on'),
                  TimelineEvent('L488', 'ttl', 3e-3, 8e-3, during='any', id='off'),
                  TimelineEvent('Camera', 'detector', 9e-3, 15e-3, during='any', id='ro'))),
        **extra})


def _absolute(log, grid_s, own=None):
    """A run log's DAC writes and line intervals in leader ticks since the
    run started: ``{(device, value or None, start, end or None)}``."""
    starts = {0: 0}
    for _, segment, wall, _arrival in log.of('release'):
        starts[segment] = wall
    records = engine_log(log)
    return _in_leader_ticks(records, starts, grid_s, own)


def _in_leader_ticks(records, starts, grid_s, own):
    def at(segment, tick):
        seconds = (starts[segment] + tick) * grid_s
        ticks = seconds / GRID_S
        assert abs(ticks - round(ticks)) < 1e-6, f'{seconds} s is off the leader grid'
        return round(ticks)
    out = set()
    for segment, tick, device, value in records['dac']:
        if own is None or device in own:
            out.add((device, round(value, 9), at(segment, tick), None))
    for segment, start, end, device in records['on']:
        if own is None or device in own:
            out.add((device, None, at(segment, start), at(segment, end)))
    return out


def _expected(program, leader_log):
    """Where the interpreter puts every write and edge, on the leader's
    segments (its releases)."""
    starts = {0: 0}
    for _, segment, wall, _arrival in leader_log.of('release'):
        starts[segment] = wall
    return _in_leader_ticks(expected_log(realize(expand(program), RATE)), starts, GRID_S, None)


def _run(composite, program, **run):
    compiled = composite.compile(program, CHANNELS)
    composite.prepare(compiled)
    composite.run(compiled, blocking=True, **run)
    return compiled


def _played(composite):
    """Every device's writes and edges, from both engines' logs."""
    leader, (follower, _) = composite.leader, composite.followers['Follow']
    grid = follower.capabilities().timing_grid_s
    return (_absolute(leader.log, GRID_S, own={'X', 'L405'})
            | _absolute(follower.log, grid, own={'RO', 'L488', 'Camera'}))


# ---------------------------------------------------------------------------
# Two engines play one program
# ---------------------------------------------------------------------------

def test_two_engines_play_the_program_as_written(follower):
    composite = _composite(follower())
    program = _resolft()
    compiled = _run(composite, program)
    assert isinstance(compiled, CompositeCompiled)
    assert composite.partsOutcome() == {'Lead': ('done', ''), 'Follow': ('done', '')}
    assert _played(composite) == _expected(program, composite.leader.log)


def test_the_follower_is_released_by_each_pulse_and_runs_its_latency_early(follower):
    composite = _composite(follower())
    _run(composite, _resolft())
    engine, _ = composite.followers['Follow']
    grid = engine.capabilities().timing_grid_s
    latency = round(engine.capabilities().trigger_latency_s / grid)
    releases = engine.log.of('release')
    assert len(releases) == 6                         # one per step
    pulses = [entry[2] for entry in composite.leader.log.entries
              if entry[0] == 'on' and entry[3] == PULSE_LINE]
    ratio = round(GRID_S / grid)
    assert [arrival for *_, arrival in releases] == [p * ratio for p in pulses]
    assert all(wall == arrival + latency for _, _, wall, arrival in releases)
    # L488 is 3 ms into the step: on the follower, 3 ms less its latency
    on = [entry[2] for entry in engine.log.entries
          if entry[0] == 'on' and entry[3] == 'L488']
    assert set(on) == {round(3e-3 / grid) - latency}


def test_a_leader_wait_holds_the_follower(follower):
    """A trigger wait before every cycle: the leader takes it, sends no pulse
    until it is released, and the follower is held."""
    composite = _composite(follower())
    program = _resolft(waits=(Wait('cycle', 'cameraReady'),))
    # the first after the leader's 200-tick approach (§7.9)
    _run(composite, program, arrivals=[250, 9000])
    assert len(composite.leader.log.of('release')) == 2
    assert _played(composite) == _expected(program, composite.leader.log)


def test_a_program_on_the_leader_alone_is_the_leaders():
    composite = _composite(_protocol_follower())
    program = ScanProgram(loops=(Loop('x', 'scan_x', 3),),
                          drives=(DeviceDrive('X', weights_um={'x': 5.0}, motion=JUMP),),
                          step_s=10e-3,
                          timeline=(TimelineEvent('L405', 'ttl', 0.0, 4e-3, during='any'),))
    compiled = composite.compile(program, CHANNELS)
    assert not isinstance(compiled, CompositeCompiled)
    assert composite.assess(program, CHANNELS).verdicts == \
        composite.leader.assess(program, CHANNELS).verdicts
    composite.prepare(compiled)
    composite.run(compiled, blocking=True)
    assert composite.leader.log.of('end') and composite.partsOutcome() is None


# ---------------------------------------------------------------------------
# What the report says, and what it refuses
# ---------------------------------------------------------------------------

def test_the_report_names_the_follower_the_pulse_and_the_drift():
    report = _composite(_protocol_follower()).assess(_resolft(), CHANNELS)
    assert report.runnable
    assert report.verdict('follow Follow').verdict == NATIVE
    assert 'wired to Lead/ttl3' in report.verdict('follow Follow').reason
    # the follower's 4 ticks of 5 µs, on the leader's 10 µs grid
    assert report.verdict('pulse').reason.startswith('2e-05 s at the start of every step')
    drift = report.verdict('drift Follow')
    assert drift.verdict == NATIVE and '(20 + 20 ppm)' in drift.reason
    assert report.verdict('Follow: drive RO').verdict == NATIVE
    assert report.verdict('Lead: ttl L405').verdict == NATIVE


def test_an_event_before_the_follower_can_start_is_refused():
    program = _resolft(timeline=(TimelineEvent('L488', 'ttl', 0.0, 2e-3, during='any'),))
    with pytest.raises(ScanEngineRefused,
                       match='L488 starts 0 s into the step; Follow starts 1.5e-05 s after'):
        _composite(_protocol_follower()).compile(program, CHANNELS)


@pytest.mark.parametrize('follower, feature, verdict, words', [
    (lambda: _protocol_follower(trigger_latency_ticks=None), 'follow Follow', UNKNOWN,
     'does not declare its trigger latency'),
    (lambda: _protocol_follower(trigger_min_ticks=None), 'pulse', UNKNOWN,
     'declares no minimum trigger pulse'),
    (lambda: _protocol_follower(clock_ppm=None), 'drift Follow', UNKNOWN,
     'Follow declares no clock accuracy'),
    (lambda: _protocol_follower(clock_ppm=1e4), 'drift Follow', IMPOSSIBLE,
     'an edge would leave its tick'),
    (lambda: _protocol_follower(trigger_in=False), 'follow Follow', IMPOSSIBLE,
     'cannot wait for a pulse before every step'),
])
def test_what_following_needs_is_checked(follower, feature, verdict, words):
    report = _composite(follower()).assess(_resolft(), CHANNELS)
    assert report.verdict(feature).verdict == verdict
    assert words in report.verdict(feature).reason


def test_a_drift_too_large_is_refused_with_the_numbers():
    reason = _composite(_protocol_follower(clock_ppm=1e4)).assess(
        _resolft(), CHANNELS).verdict('drift Follow').reason
    # (20 + 10000) ppm over a 22 ms pulse interval (a step and its return)
    assert '220 µs over the longest 22 ms between pulses' in reason
    assert 'half a tick, 2.5 µs' in reason


def test_a_simulated_leader_cannot_pulse_a_real_board():
    class Link:
        reply_timeout = run_timeout = 0
        board = None

        def __init__(self):
            self.inner = SimulatedTransport(SimulatedStepBoard(BoardCaps(
                'B', 'f', grid_ns=5000, dacs={0: (-10.0, 10.0)}, ttl_lines=2, trigger_in=True,
                trigger_latency_ticks=3, trigger_min_ticks=4, clock_ppm=20.0)))

        def write(self, line):
            self.inner.write(line)

        def read(self, timeout):
            return self.inner.read(timeout)

        def close(self):
            pass

    real = StepProtocolScanEngine(SimpleNamespace(managerProperties={}), name='Follow',
                                  setupInfo=SETUP, transport=Link())
    verdict = _composite(real).assess(_resolft(), CHANNELS).verdict('follow Follow')
    assert verdict.verdict == IMPOSSIBLE and 'A simulated Lead cannot pulse a real' in \
        verdict.reason


def test_the_nidaq_cannot_lead_yet_and_says_why():
    from imswitch.imcontrol.model.managers.scan_engines.NidaqScanEngine import NidaqScanEngine
    nidaq = NidaqScanEngine(None, nidaqManager=None, setupInfo=SimpleNamespace())
    verdict = _composite(_protocol_follower(), leader=nidaq).assess(
        _resolft(), CHANNELS).verdict('lead')
    assert verdict.verdict == IMPOSSIBLE and 'its scan drives only devices' in verdict.reason


def test_a_separation_across_engines_is_refused():
    program = _resolft(separations=(Separation(('on', 'end'), ('off', 'start')),))
    with pytest.raises(ScanEngineRefused, match='spans two engines'):
        _composite(_protocol_follower()).compile(program, CHANNELS)


def test_a_simulated_wire_needs_whole_ticks_on_the_follower():
    report = _composite(_protocol_follower(grid_ns=3_000, latency_ticks=5)).assess(
        _resolft(), CHANNELS)
    assert report.verdict('wire Follow').verdict == IMPOSSIBLE


# ---------------------------------------------------------------------------
# One run: stop, failure
# ---------------------------------------------------------------------------

def _signals(engine):
    seen = []
    for name in ('sigScanStarted', 'sigScanDone', 'sigScanStopped', 'sigScanFailed',
                 'sigScanBuildFailed'):
        getattr(engine, name).connect(lambda *a, n=name: seen.append((n,) + a))
    return seen


def test_one_run_reports_once(follower, qtbot):
    composite = _composite(follower())
    seen = _signals(composite)
    _run(composite, _resolft())
    qtbot.waitUntil(lambda: ('sigScanDone',) in seen, timeout=2000)
    assert seen == [('sigScanStarted',), ('sigScanDone',)]


def test_stop_stops_every_engine(follower, qtbot):
    """The leader waits for a trigger that never comes; Stop ends it and the
    follower, waiting for its first pulse, with one outcome."""
    composite = _composite(follower())
    seen = _signals(composite)
    compiled = composite.compile(_resolft(waits=(Wait('cycle', 'cameraReady'),)), CHANNELS)
    composite.prepare(compiled)
    composite.run(compiled, arrivals=[], blocking=False)
    qtbot.wait(50)
    assert composite.abort() == 'step-boundary'
    composite.join(timeout=2)
    qtbot.waitUntil(lambda: ('sigScanDone',) in seen, timeout=2000)
    assert composite.partsOutcome() == {'Lead': ('stopped', ''), 'Follow': ('stopped', '')}
    assert seen == [('sigScanStarted',), ('sigScanStopped',), ('sigScanDone',)]


def test_a_failure_of_the_leader_fails_the_run(follower, qtbot):
    composite = _composite(follower())
    seen = _signals(composite)
    _run(composite, _resolft(), fail_at_step=2)
    qtbot.waitUntil(lambda: any(s[0] == 'sigScanBuildFailed' for s in seen), timeout=2000)
    assert ('sigScanFailed', 'Lead: injected failure at step 2') in seen
    assert composite.partsOutcome()['Follow'] == ('stopped', '')


class _Deaf(SimulatedStepEngine):
    """A follower whose trigger input sees nothing."""

    def _release(self, wait_start, arrivals, timeout):
        return None


def test_a_follower_that_misses_its_pulse_fails_the_run(qtbot):
    composite = _composite(_simulated_follower(cls=_Deaf))
    seen = _signals(composite)
    _run(composite, _resolft())
    qtbot.waitUntil(lambda: any(s[0] == 'sigScanBuildFailed' for s in seen), timeout=2000)
    failed = [s for s in seen if s[0] == 'sigScanFailed']
    assert failed and failed[0][1].startswith('Follow: ')
    assert composite.partsOutcome()['Lead'] == ('done', '')


def test_the_first_failure_stops_the_others():
    """Real boards run at once: the first part to fail stops the rest, and
    the last to end reports."""
    class Part:
        def __init__(self, name):
            self.name, self.aborted, self.outcome = name, 0, None

        def abort(self):
            self.aborted += 1

    leader = _leader()
    composite = _composite(_protocol_follower(), leader=leader)
    a, b = Part('Lead'), Part('Follow')
    reported = []
    leader.sigScanFailed.connect(reported.append)
    composite._run = {'parts': {'Lead': a, 'Follow': b}, 'ended': {}, 'stopping': False,
                      'finished': False}
    b.outcome = ('failed', 'no pulse')
    composite._ended(b)
    assert a.aborted == 1 and b.aborted == 0 and reported == []
    a.outcome = ('stopped', '')
    composite._ended(a)
    assert composite.partsOutcome() == {'Follow': ('failed', 'no pulse'),
                                        'Lead': ('stopped', '')}


# ---------------------------------------------------------------------------
# The setup
# ---------------------------------------------------------------------------

def _setup(**follow):
    lead = SimpleNamespace(managerName='SimulatedStepEngine', triggerInput=None,
                           managerProperties={'timingGridS': GRID_S,
                                              'digitalLines': ['Lead/ttl0', PULSE_LINE]})
    info = SimpleNamespace(managerName='SimulatedStepEngine', **{
        'triggerInput': PULSE_LINE,
        'managerProperties': {'timingGridS': GRID_S / 2, 'digitalLines': ['Follow/ttl0']},
        **follow})
    return SimpleNamespace(scanEngines={'lead': lead, 'follow': info},
                           scan=SimpleNamespace(engine='lead'), lasers={}, detectors={},
                           positioners={})


class _NoNidaq:
    sigScanBuilt = sigScanStarted = sigScanDone = sigScanBuildFailed = None


def test_a_wired_engine_follows_the_one_the_panel_runs_on():
    engines = build_scan_engines(_setup(), nidaqManager=_NoNidaq())
    assert isinstance(engines.engine, CompositeScanEngine)
    assert list(engines.engine.followers) == ['follow']
    assert engines.engine.owns('Follow/ttl0') and engines.engine.owns(PULSE_LINE)
    # it leads with the engine the panel names, whose signals are the run's
    assert engines.engine.leader is engines.engines['lead']
    assert engines.engine.name == 'lead'


@pytest.mark.parametrize('follow, message', [
    (dict(triggerInput='Follow/ttl0'), "follow's trigger input is wired to Follow/ttl0, which "
                                       'follow drives; only lead'),
    (dict(triggerInput='Nowhere/ttl9'), 'which no engine drives'),
])
def test_a_wire_to_anything_but_the_leader_is_a_setup_error(follow, message):
    with pytest.raises(SetupError, match=message):
        build_scan_engines(_setup(**follow), nidaqManager=_NoNidaq())


def test_without_a_wire_nothing_changes():
    engines = build_scan_engines(_setup(triggerInput=None), nidaqManager=_NoNidaq())
    assert engines.composite is None and engines.engine is engines.engines['lead']


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
