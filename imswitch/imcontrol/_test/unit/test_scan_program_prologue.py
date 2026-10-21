"""A prologue: a step of its own before every pass of a loop, with its own
duration, positions offset from the pass's first step, and its own pulses
(the RESOLFT cycle's activation and depletion at the resting position).

Checked at every layer: the model's validation, the interpreter's spans and
positions, the realized schedule, the protocol's lines, the executor on the
simulated board, the generic refusal on an engine without it, and the
converter's RESOLFT file against the old firmware's own timeline.
"""

import dataclasses
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.scan_program import (
    DeviceDrive, Loop, Motion, Prologue, RunContext, ScanProgram, TimelineEvent, Wait, expand,
    realize,
)
from imswitch.imcontrol.model.scan_program.conformance import engine_log, expected_log, \
    log_differences
from imswitch.imcontrol.model.scan_program.engine import DeviceChannels, EngineCapabilities, \
    IMPOSSIBLE, assess_program
from imswitch.imcontrol.model.scan_program.program import ProgramError, validate
from imswitch.imcontrol.model.scan_program.serialize import program_from_dict, program_to_dict
from imswitch.imcontrol.model.managers.scan_engines.step_protocol import lines_to_plan, \
    plan_to_lines

JUMP = Motion(transition='jump', move_s=0.0, settle_s=0.001, placement='in-step',
              return_s=0.001, origin='absolute', restore='first')


def _program(**changes):
    base = dict(
        loops=(Loop('cycle', 'cycle', 2), Loop('plane', 'plane', 2)),
        drives=(DeviceDrive('G', offset_um=-21.0, weights_um={'cycle': 0.21, 'plane': 2.1},
                            motion=JUMP),),
        step_s=0.006,
        timeline=(TimelineEvent('RO', 'ttl', 0.0, 0.001, id='ro'),
                  TimelineEvent('ON', 'ttl', 0.002, 0.020, anchor='prologue:plane',
                                id='activation'),
                  TimelineEvent('OFF', 'ttl', 0.0205, 0.0605, anchor='prologue:plane',
                                id='depletion')),
        prologues=(Prologue('plane', 0.0625, offsets_um={'G': 21.0}),),
        timing_policy='strict-v1',
    )
    base.update(changes)
    return ScanProgram(**base)


def test_a_prologue_validates_and_its_mistakes_are_named():
    validate(_program())
    with pytest.raises(ProgramError, match='unknown loop'):
        validate(_program(prologues=(Prologue('nowhere', 0.01),)))
    with pytest.raises(ProgramError, match='one prologue'):
        validate(_program(prologues=(Prologue('plane', 0.01), Prologue('plane', 0.02))))
    with pytest.raises(ProgramError, match='outside its prologue'):
        validate(_program(timeline=(TimelineEvent('ON', 'ttl', 0.0, 0.1,
                                                  anchor='prologue:plane'),)))
    with pytest.raises(ProgramError, match='has no prologue'):
        validate(_program(prologues=(), timeline=(
            TimelineEvent('ON', 'ttl', 0.0, 0.001, anchor='prologue:plane'),)))
    with pytest.raises(ProgramError, match='not a detector'):
        validate(_program(timeline=(TimelineEvent('CAM', 'detector', 0.0, 0.001,
                                                  anchor='prologue:plane'),)))
    with pytest.raises(ProgramError, match='needs a drive'):
        validate(_program(prologues=(Prologue('plane', 0.01, offsets_um={'X': 1.0}),)))


def test_the_interpreter_puts_a_prologue_before_every_pass_at_the_offset_position():
    expansion = expand(_program(), RunContext())
    kinds = [span.kind for span in expansion.spans]
    # The approach to the entry position, then two passes of the plane
    # loop, each: prologue, two steps, return.
    assert kinds == ['preparation', 'prologue', 'step', 'step', 'return', 'prologue', 'step',
                     'step', 'return']
    first, second = [s for s in expansion.spans if s.kind == 'prologue']
    # Pass 1 starts at -21 (cycle 0): the prologue parks G at 0 and ends
    # with the move to -21 inside its 62.5 ms.
    piece = first.pieces['G']
    assert (piece.where, piece.start_um, piece.end_um, piece.settle_s) == ('end', 0.0, -21.0,
                                                                            0.001)
    assert [(e.event.id, e.start_s, e.end_s) for e in first.events] == [
        ('activation', 0.002, 0.020), ('depletion', 0.0205, 0.0605)]
    # Pass 2 (cycle 1) starts at -20.79: the resting position is 0.21.
    assert (second.pieces['G'].start_um, second.pieces['G'].end_um) == (
        pytest.approx(0.21), pytest.approx(-20.79))
    # The return after pass 1 goes to the second prologue's position.
    ret = expansion.spans[4]
    assert ret.pieces['G'].end_um == pytest.approx(0.21)
    # The first positions are the entry: where the run starts.
    assert expansion.first_um == {'G': -21.0}


def test_the_schedule_logs_the_prologue_writes_and_pulses():
    # G captured at the resting position: no approach, the run starts there.
    schedule = realize(expand(_program(), RunContext(captured_um={'G': 0.0})), 1e6)
    log = expected_log(schedule)
    writes = [(tick, device, round(um, 6)) for _, tick, device, um in log['dac']]
    # Start at the resting position; at 61.5 ms (62.5 - 1 ms settle) to the
    # first plane; the second plane at the end of step 1's dwell; the
    # return at the end of the pass to the next cycle's resting position.
    assert writes[:4] == [(0, 'G', 0.0), (61500, 'G', -21.0), (62500 + 6000 - 1000, 'G', -18.9),
                          (62500 + 12000, 'G', pytest.approx(0.21))]
    on = [(start, end, device) for _, start, end, device in log['on']]
    assert on[:3] == [(2000, 20000, 'ON'), (20500, 60500, 'OFF'), (62500, 63500, 'RO')]


def test_the_protocol_carries_a_prologue_and_reads_it_back():
    from imswitch.imcontrol.model.managers.scan_engines.step_protocol import WirePlan
    plan = WirePlan(
        loops=[('L0', 2, 'forward', ()), ('L1', 2, 'forward', ())], phase_loop=None,
        drives=[dict(device='dac0', channel='dac0', output=True, offset=-2.1, weights={'L0': 0.021,
                     'L1': 0.21}, table_loop=None, table=None, move=0, settle=1000,
                     placement='in-step', origin='absolute', restore='first')],
        step_ticks=6000, return_ticks=0,
        events=[dict(device='ttl1', kind='event', start=2000, end=20000, anchor='prologue',
                     anchor_loop='L1', phases=None, value=None, on=1, off=0)],
        waits=[], preparation={}, captured_um={}, park_um={}, safe={'ttl1': 0},
        prologues=[dict(loop='L1', ticks=62500, offsets={'dac0': 2.1})])
    lines = plan_to_lines(plan)
    assert 'PROLOGUE,1,62500' in lines and 'PROFFSET,dac0,1,2.1' in lines
    assert 'EVENT,ttl1,2000,20000,prologue,1,-,1,0' in lines
    back = lines_to_plan(lines)
    assert back.prologues == [dict(loop='L1', ticks=62500, offsets={'dac0': 2.1})]
    assert back.events[0]['anchor'] == 'prologue' and back.events[0]['anchor_loop'] == 'L1'
    with pytest.raises(Exception, match='has none'):
        lines_to_plan([line for line in lines if not line.startswith(('PROLOGUE', 'PROFFSET'))])


def test_a_step_board_plays_the_prologue_as_the_interpreter_means_it():
    from imswitch.imcontrol.model.managers.scan_engines.StepProtocolScanEngine import (
        StepProtocolScanEngine,
    )
    from imswitch.imcontrol.model.managers.scan_engines.conformance_runner import (
        simulated_transport,
    )
    program = _program()
    transport = simulated_transport(1000)
    setup = SimpleNamespace(
        positioners={'G': SimpleNamespace(managerProperties={'conversionFactor': 10.0})},
        lasers={n: SimpleNamespace(ttlActiveLevel='high') for n in ('RO', 'ON', 'OFF')},
        detectors={})
    engine = StepProtocolScanEngine(SimpleNamespace(managerProperties={}), name='board',
                                    setupInfo=setup, transport=transport)
    channels = {'G': DeviceChannels('G', analog='board/dac0'),
                'RO': DeviceChannels('RO', digital='board/ttl0'),
                'ON': DeviceChannels('ON', digital='board/ttl1'),
                'OFF': DeviceChannels('OFF', digital='board/ttl2')}
    try:
        assert engine.capabilities().prologues
        report = engine.assess(program, channels)
        assert report.runnable, report.reasons()
        compiled = engine.compile(program, channels, captured_um={})
        assert any(line.startswith('PROLOGUE,1,62500') for line in compiled.lines)
        assert any(line == 'PROFFSET,dac0,1,2.1' for line in compiled.lines)
        engine.prepare(compiled)
        engine.run(compiled, blocking=True)
        assert engine.outcome[0] == 'done'
        schedule = realize(expand(program, RunContext()), 1e6)
        assert log_differences(engine_log(engine.log), expected_log(schedule), None) == []
    finally:
        transport.close()


def test_an_engine_without_prologues_refuses_the_program():
    caps = EngineCapabilities(name='daq', analog={'daq/ao0': SimpleNamespace()},
                              digital=('daq/l0', 'daq/l1', 'daq/l2'), streaming=True,
                              stepping='emulated', prologues=False)
    channels = {'G': DeviceChannels('G', analog='daq/ao0'),
                'RO': DeviceChannels('RO', digital='daq/l0'),
                'ON': DeviceChannels('ON', digital='daq/l1'),
                'OFF': DeviceChannels('OFF', digital='daq/l2')}
    report = assess_program(_program(), caps, lambda c: c.startswith('daq/'), channels)
    verdict = report.verdict('prologue of plane')
    assert verdict.verdict == IMPOSSIBLE and 'before every pass' in verdict.reason


def test_a_prologue_survives_saving_and_loading():
    program = _program()
    assert program_from_dict(program_to_dict(program)) == program


def test_the_resolft_file_converts_exactly_with_a_prologue(tmp_path):
    from imswitch.imcontrol.model.scan_program.legacy_triggerscope import (
        compare, convert_legacy_scan, read_legacy_scan,
    )
    from .test_legacy_triggerscope_conversion import DEVICE_MAP, THREE_COLOURS_INI
    text = THREE_COLOURS_INI.replace('onTimeMs = 0.0', 'onTimeMs = 18.0')
    text = text.replace('offTimeMs = 0.0', 'offTimeMs = 40.0')
    text = text.replace('delayBeforeOnTimeMs = 2.0', 'delayBeforeOnTimeMs = 3.0')
    path = tmp_path / 'resolft'
    path.write_text(text, encoding='utf-8')
    legacy = read_legacy_scan(path)
    conversion = convert_legacy_scan(legacy, device_map=DEVICE_MAP)
    program = conversion.program
    (prologue,) = program.prologues
    # 3 + 18 + 0.5 + 40 + 0 ms of preamble; the 1 ms settle on the plane is
    # the delay at the start of every step.
    assert prologue.loop == 'plane' and prologue.duration_s == pytest.approx(0.0615)
    assert prologue.offsets_um == {'Galvo': 21.0}
    pulses = {e.id: (e.device, e.start_s, e.end_s) for e in program.timeline
              if e.anchor == 'prologue:plane'}
    assert pulses == {'activation': ('405', 0.003, 0.021),
                      'depletion': ('488 Off', pytest.approx(0.0215), pytest.approx(0.0615))}
    assert compare(legacy, conversion, device_map=DEVICE_MAP) == []
