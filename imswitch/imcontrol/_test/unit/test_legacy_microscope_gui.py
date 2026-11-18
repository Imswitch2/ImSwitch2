"""The polarization rig's old Microscope_GUI modes as scan programs, run on
its Nucleo as a simulated step board and compared with the old waveform
generator (``legacy_microscope_gui``) sample by sample."""

import pytest

from imswitch.imcontrol.model.managers.scan_engines.loader import build_scan_engines
from imswitch.imcontrol.model.scan_program import legacy_microscope_gui as lm
from imswitch.imcontrol.model.scan_program.engine import setup_channels
from imswitch.imcontrol.view import ViewSetupInfo

SETUP = 'imswitch/_data/user_defaults/imcontrol_setups/nucleo_polarization_mock_setup.json'
CHANNELS = {'ttl0': '405', 'ttl1': '488', 'ttl2': 'camera', 'dac0': 'x', 'dac1': 'y'}
SAMPLE_S = 4e-6                 # the old 250 kHz clock
SAMPLE_TICKS = 4                # on the board's 1 µs grid


class _FakeNidaq:
    sigScanBuilt = sigScanStarted = sigScanDone = sigScanBuildFailed = None


@pytest.fixture
def rig():
    setup = ViewSetupInfo.from_json(open(SETUP).read())
    engines = build_scan_engines(setup, nidaqManager=_FakeNidaq())
    yield engines.engines['nucleo'], setup_channels(setup), lm.Wiring.from_setup(setup)
    engines.finalize()


def _run(engine, channels, program):
    compiled = engine.compile(program, channels)
    assert compiled.report.runnable
    engine.prepare(compiled)
    engine.run(compiled, blocking=True)
    log = engine.board.log
    assert not log.of('failed')
    return log


def _codes(volts):
    return [round(v / lm.DAC_VOLTS_PER_CODE) for v in volts]


def _ons(log, channel):
    """The ticks where ``channel`` goes on, one per window: a window that
    continues over a step boundary goes off and on at the same tick."""
    edges = [(e[2], e[0]) for e in log.entries if e[0] in ('on', 'off') and e[3] == channel]
    offs = {tick for tick, kind in edges if kind == 'off'}
    return sorted(tick for tick, kind in edges if kind == 'on' and tick not in offs)


def test_the_nucleo_owns_the_galvos_lasers_and_camera_line(rig):
    engine, channels, wiring = rig
    for device in (wiring.laser_405, wiring.laser_488, wiring.camera):
        assert engine.owns(channels[device].digital)
    for device in (wiring.galvo_x, wiring.galvo_y):
        assert engine.owns(channels[device].analog)
    assert wiring.um_per_volt_x == wiring.um_per_volt_y == 100.0


def test_wide_field_plays_the_old_cycle_sample_for_sample(rig):
    engine, channels, wiring = rig
    log = _run(engine, channels, lm.wide_field(lm.LegacyParams(), wiring))
    old = lm.legacy_wide_field()
    played = lm.sampled(log.entries, channels=CHANNELS, grid_ns=1000, period_s=SAMPLE_S,
                        samples=len(old))
    assert [int(v) for v in played['405']] == old.ttl_405
    assert [int(v) for v in played['488']] == old.ttl_488
    assert [int(v) for v in played['camera']] == old.camera
    assert _codes(played['x']) == _codes(lm.LegacyBuffers.volts(old.galvo_x))
    assert _codes(played['y']) == _codes(lm.LegacyBuffers.volts(old.galvo_y))


def test_galvo_scan_plays_the_old_waveform(rig):
    """The windows to the sample; galvo Y within one code (a step is two old
    samples, and the old ramp moves half a code per sample)."""
    engine, channels, wiring = rig
    log = _run(engine, channels, lm.galvo_scan(lm.LegacyParams(), wiring))
    old = lm.legacy_galvo_scan()
    lay = lm._galvo_scan_layout(lm.LegacyParams())
    rest = lay['final'] - (2 * lay['half'] + 200)        # the frame's wait comes first
    played = lm.sampled(log.entries, channels=CHANNELS, grid_ns=1000, period_s=SAMPLE_S,
                        samples=len(old) - rest, start_tick=rest * SAMPLE_TICKS)
    n = len(old) - rest
    assert [int(v) for v in played['405']] == old.ttl_405[:n]
    assert [int(v) for v in played['488']] == old.ttl_488[:n]
    assert [int(v) for v in played['camera']] == old.camera[:n]
    assert _codes(played['x']) == _codes(lm.LegacyBuffers.volts(old.galvo_x[:n]))
    old_y = _codes(lm.LegacyBuffers.volts(old.galvo_y[:n]))
    assert max(abs(a - b) for a, b in zip(_codes(played['y']), old_y)) <= 1
    # each window is one pulse: no edge inside it
    assert len(_ons(log, 'ttl0')) == len(_ons(log, 'ttl2')) == 1


def test_galvo_scan_steps_divide_the_sweep():
    with pytest.raises(ValueError, match='does not divide'):
        lm.galvo_scan(step_samples=3)


def test_wide_field_2d_dark_frames_then_the_405_on_every_second_frame(rig):
    engine, channels, wiring = rig
    pairs = 2
    log = _run(engine, channels, lm.wide_field_2d(lm.LegacyParams(), wiring, pairs=pairs))
    cycle = len(lm.legacy_wide_field()) * SAMPLE_TICKS
    frames = lambda ch: [tick // cycle for tick in _ons(log, ch)]   # noqa: E731
    assert frames('ttl2') == list(range(3 + 2 * pairs))             # the camera every frame
    assert frames('ttl1') == [3, 4, 5, 6]                            # 488 after the dark frames
    assert frames('ttl0') == [4, 6]                                  # 405 on the second of a pair


def test_dark_frames_fire_only_the_camera(rig):
    engine, channels, wiring = rig
    log = _run(engine, channels, lm.dark_frames(lm.LegacyParams(), wiring, frames=3))
    assert len(_ons(log, 'ttl2')) == 3
    assert not _ons(log, 'ttl0') and not _ons(log, 'ttl1')


def test_line_scan_2d_visits_the_old_grid_y_fastest(rig):
    engine, channels, wiring = rig
    params = lm.LegacyParams()
    log = _run(engine, channels, lm.line_scan_2d(params, wiring, pairs=1))
    positions, nx, ny = lm.legacy_line_scan_positions(params)
    assert (nx, ny) == (12, 12)
    cycle = len(lm.legacy_wide_field(params)) * SAMPLE_TICKS
    steps = 2 * len(positions)
    played = lm.sampled(log.entries, channels=CHANNELS, grid_ns=1000,
                        period_s=cycle * 1e-6, samples=steps)
    for step in range(steps):
        x, y = positions[step % len(positions)]
        old = lm.LegacyBuffers.volts([int(x * lm.LEGACY_COUNTS_PER_VOLT),
                                      int(y * lm.LEGACY_COUNTS_PER_VOLT)])
        assert abs(_codes([played['x'][step]])[0] - _codes([old[0]])[0]) <= 1
        assert abs(_codes([played['y'][step]])[0] - _codes([old[1]])[0]) <= 1
    passes = sorted({tick // (cycle * len(positions)) for tick in _ons(log, 'ttl0')})
    assert passes == [1]                                             # the 405 in the second pass
    assert len(_ons(log, 'ttl2')) == steps                           # a frame at every dot
