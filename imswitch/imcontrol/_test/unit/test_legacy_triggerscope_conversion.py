"""The old TriggerScope scan files become scan programs that mean the same
scan: every frame the old firmware produced -- positions, pulses relative to
the frame, frame spacing -- is in the program, checked against a line-by-line
transcription of the old routines (``legacy_triggerscope``). The three-colour
scans need an intra-step move of the multicolor galvo, which a step board
plays as absolute DAC levels; that is checked on the simulated board.
"""

from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.scan_program import RunContext, expand, realize
from imswitch.imcontrol.model.scan_program.conformance import engine_log, expected_log, \
    log_differences
from imswitch.imcontrol.model.scan_program.engine import DeviceChannels, EMULATED, \
    ScanEngineRefused
from imswitch.imcontrol.model.scan_program.legacy_triggerscope import (
    LegacyScanError, compare, convert_legacy_scan, legacy_timeline, read_legacy_scan,
)
from imswitch.imcontrol.model.scan_program.serialize import program_from_dict, program_to_dict

RASTER_INI = """[analogParameterDict]
target_device = ['Stage X', 'Stage Y', 'Stage Z', 'Galvo', 'GalvoX', 'GalvoY']
axis_length = [2.0, 1.0, 0.0, 0.0, 0.0, 0.0]
axis_step_size = [0.101, 0.101, 0.101, 1.0, 0.1, 0.1]
axis_startpos = [0, 0, 0, 0.0, -280.0, 0]

[digitalParameterDict]
target_device = ['488 Light Sheet']
TTL_start = [0.1]
TTL_end = [0.13]
sequence_time = 0.15
"""

THREE_COLOURS_INI = """[scanParameterDict]
timeLapsePoints = 3
timeLapseDelayS = 0.1
delayBeforeOnTimeMs = 2.0
onTimeMs = 0.0
delayAfterOnTimeMs = 0.5
offTimeMs = 0.0
delayAfterOffTimeMs = 0.0
delayAfterDACStepMs = 1.0
roTimeMs = 1.0
delayAfterRoMs = 1.8
roRestingPosUm = 0.0
roStartPosUm = -21.0
roStepSizeUm = 2.1
roSteps = 5
cycleStartPosUm = 0.0
cycleStepSizeUm = 0.21
cycleSteps = 2
Laser2OnMs = 2.0
DelayAfterLaser2Ms = 0.4
MulticolorScanFirstUm = -280.0
MulticolorScanSecondUm = 0.0
Laser3OnMs = 1.0
DelayAfterLaser3Ms = 0.4
MulticolorScanThirdUm = 280.0

[deviceParameterDict]
onLaser = 405
offLaser = 488 Off
roLaser = 488 Light Sheet
roScanDevice = Galvo
cycleScanDevice = Stage X
Laser2 = 561AOTF
Laser3 = 640
MulticolorScanDevice = GalvoX
CameraTTL = Orca
"""

DEVICE_MAP = {'Orca': 'OrcaFusion'}


def _file(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding='utf-8')
    return path


def test_the_raster_file_is_read_converted_and_means_the_old_scan(tmp_path):
    legacy = read_legacy_scan(_file(tmp_path, 'XY_beads', RASTER_INI))
    conversion = convert_legacy_scan(legacy, camera='OrcaFusion')
    program = conversion.program
    # Y outer, X inner: 10 x 20 pixels of 150 ms, the laser at 100-130 ms.
    assert [(lp.id, lp.kind, lp.count) for lp in program.loops] == [
        ('stage_y', 'scan_y', 10), ('stage_x', 'scan_x', 20)]
    assert program.step_s == 0.15
    assert {(e.device, e.kind, e.start_s, e.end_s) for e in program.timeline} == {
        ('488 Light Sheet', 'ttl', 0.1, 0.13), ('OrcaFusion', 'detector', 0.1, 0.13)}
    assert compare(legacy, conversion, camera='OrcaFusion') == []
    assert any('TTL lines 0-3' in note for note in conversion.notes)
    # Saved and read back, it is the same program.
    assert program_from_dict(program_to_dict(program)) == program


def test_the_three_colour_file_means_the_old_scan_frame_by_frame(tmp_path):
    legacy = read_legacy_scan(_file(tmp_path, 'three', THREE_COLOURS_INI))
    conversion = convert_legacy_scan(legacy, device_map=DEVICE_MAP)
    program = conversion.program
    assert [(lp.id, lp.count) for lp in program.loops] == [('time', 3), ('cycle', 2),
                                                           ('plane', 5)]
    galvo = program.drives[0]
    assert galvo.device == 'Galvo' and galvo.offset_um == -21.0
    assert galvo.weights_um == {'cycle': 0.21, 'plane': 2.1}
    # One readout step: 1 ms readout, 0.4 + 2 ms laser 2, 0.4 + 1 ms laser 3,
    # 1.8 ms pause, 1 ms settle = 7.6 ms.
    assert program.step_s == pytest.approx(0.0076)
    offsets = [e for e in program.timeline if e.kind == 'offset']
    assert [(e.start_s, e.end_s, e.value) for e in offsets] == [
        (0.002, pytest.approx(0.0044), 280.0), (pytest.approx(0.0044), pytest.approx(0.0076),
                                                 560.0)]
    (prologue,) = program.prologues
    # 2 ms before, 0.5 ms after the (empty) activation: the preamble, with
    # the galvo at the resting position, 21 µm above the first plane.
    assert (prologue.loop, prologue.duration_s, prologue.offsets_um) == ('plane', 0.0025,
                                                                         {'Galvo': 21.0})
    assert compare(legacy, conversion, device_map=DEVICE_MAP) == []


def test_the_old_firmware_transcription_has_the_expected_shape(tmp_path):
    legacy = read_legacy_scan(_file(tmp_path, 'three', THREE_COLOURS_INI))
    old = legacy_timeline(legacy, device_map=DEVICE_MAP)
    frames = [w for w in old['on'] if w[2] == 'OrcaFusion']
    assert len(frames) == 3 * 2 * 5
    # Readout laser on with the camera, 1 ms; laser 3 ends with the camera.
    first = frames[0]
    assert (first[0], first[0] + 1000, '488 Light Sheet') in old['on']
    assert (first[1] - 1000, first[1], '640') in old['on']
    # Between cycles: 2 + 0.5 ms in the dark; between time points 0.1 s more.
    gaps = [b[0] - a[1] for a, b in zip(frames, frames[1:])]
    assert gaps[4] == 1800 + 2000 + 500 + 1000 and gaps[0] == 1800 + 1000
    assert gaps[9] == 1800 + 500 + 1000 + 100_000


def test_a_scan_with_activation_pulses_puts_them_in_the_prologue(tmp_path):
    text = THREE_COLOURS_INI.replace('onTimeMs = 0.0', 'onTimeMs = 18.0')
    legacy = read_legacy_scan(_file(tmp_path, 'resolft', text))
    conversion = convert_legacy_scan(legacy, device_map=DEVICE_MAP)
    activation = next(e for e in conversion.program.timeline if e.id == 'activation')
    assert (activation.device, activation.anchor, activation.start_s, activation.end_s) == (
        '405', 'prologue:plane', 0.002, 0.020)
    assert compare(legacy, conversion, device_map=DEVICE_MAP) == []


def test_a_device_the_file_names_can_be_renamed(tmp_path):
    legacy = read_legacy_scan(_file(tmp_path, 'three', THREE_COLOURS_INI))
    with_map = convert_legacy_scan(legacy, device_map={'Orca': 'Cam', 'Galvo': 'G'}).program
    assert {b.detector for b in with_map.detectors} == {'Cam'}
    assert with_map.drives[0].device == 'G'


def test_a_file_that_is_not_a_scan_is_refused(tmp_path):
    with pytest.raises(LegacyScanError, match='not a TriggerScope scan file'):
        read_legacy_scan(_file(tmp_path, 'other', '[section]\nkey = 1\n'))


# -- on a step board ----------------------------------------------------------------

def _engine_and_channels():
    from imswitch.imcontrol.model.managers.scan_engines.StepProtocolScanEngine import (
        StepProtocolScanEngine,
    )
    from imswitch.imcontrol.model.managers.scan_engines.conformance_runner import (
        simulated_transport,
    )
    transport = simulated_transport(1000)
    setup = SimpleNamespace(
        positioners={'Galvo': SimpleNamespace(managerProperties={'conversionFactor': 10.0}),
                     'GalvoX': SimpleNamespace(managerProperties={'conversionFactor': 100.0})},
        lasers={name: SimpleNamespace(ttlActiveLevel='high')
                for name in ('488 Light Sheet', '561AOTF', '640')},
        detectors={'OrcaFusion': SimpleNamespace(ttlActiveLevel='high')})
    engine = StepProtocolScanEngine(SimpleNamespace(managerProperties={}), name='board',
                                    setupInfo=setup, transport=transport)
    channels = {
        'Galvo': DeviceChannels('Galvo', analog='board/dac0'),
        'GalvoX': DeviceChannels('GalvoX', analog='board/dac1'),
        '488 Light Sheet': DeviceChannels('488 Light Sheet', digital='board/ttl0'),
        '561AOTF': DeviceChannels('561AOTF', digital='board/ttl1'),
        '640': DeviceChannels('640', digital='board/ttl2'),
        'OrcaFusion': DeviceChannels('OrcaFusion', digital='board/ttl3'),
    }
    return engine, channels, transport


def test_the_three_colour_program_runs_on_a_step_board_as_absolute_levels(tmp_path):
    text = THREE_COLOURS_INI.replace('timeLapsePoints = 3', 'timeLapsePoints = 1')
    text = text.replace('MulticolorScanFirstUm = -280.0', 'MulticolorScanFirstUm = 100.0')
    text = text.replace('MulticolorScanSecondUm = 0.0', 'MulticolorScanSecondUm = 300.0')
    text = text.replace('MulticolorScanThirdUm = 280.0', 'MulticolorScanThirdUm = 500.0')
    legacy = read_legacy_scan(_file(tmp_path, 'three', text))
    program = convert_legacy_scan(legacy, device_map=DEVICE_MAP).program
    engine, channels, transport = _engine_and_channels()
    try:
        report = engine.assess(program, channels)
        assert report.verdict('offset GalvoX').verdict == EMULATED
        compiled = engine.compile(program, channels, captured_um={})
        dac1 = [e for e in compiled.wire.events if e['device'] == 'dac1']
        # 100 µm at 100 µm/V is 1 V; the offsets take it to 3 V, then 5 V,
        # then back to 1 V when the step ends (the second edge of each pair
        # carries the next level, so the two edges at one tick agree).
        assert [(e['start'], e['end'], e['on'], e['off']) for e in dac1] == [
            (2000, 4400, 3.0, 5.0), (4400, 7600, 5.0, 1.0)]
        assert any(line.startswith('EVENT,dac1,') for line in compiled.lines)
        engine.prepare(compiled)
        engine.run(compiled, blocking=True)
        assert engine.outcome[0] == 'done'
        schedule = realize(expand(program, RunContext()), 1e6)
        assert log_differences(engine_log(engine.log), expected_log(schedule), None) == []
    finally:
        transport.close()


def test_an_offset_on_a_device_the_loops_move_is_refused(tmp_path):
    from imswitch.imcontrol.model.scan_program.program import TimelineEvent
    import dataclasses
    legacy = read_legacy_scan(_file(tmp_path, 'three', THREE_COLOURS_INI.replace(
        'timeLapsePoints = 3', 'timeLapsePoints = 1')))
    program = convert_legacy_scan(legacy, device_map=DEVICE_MAP).program
    # The offset moved onto the galvo the planes step.
    moved = dataclasses.replace(program, timeline=tuple(
        dataclasses.replace(e, device='Galvo') if e.kind == 'offset' else e
        for e in program.timeline))
    engine, channels, transport = _engine_and_channels()
    try:
        with pytest.raises(ScanEngineRefused, match='moves with the loops'):
            engine.compile(moved, channels, captured_um={})
    finally:
        transport.close()
