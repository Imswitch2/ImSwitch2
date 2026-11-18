"""The polarization rig's Nucleo firmware (firmware/nucleo_step), run on this
computer as a fake board: its board layer and the step-engine core -- the
code the board runs -- over a fake of its hardware (host/nucleo_host.cpp),
spoken to over a pipe as the board is over USB.

It must pass the step-engine protocol's conformance programs, run the rig's
four modes (``legacy_microscope_gui``) as the scan-program interpreter says,
and put a window that runs over many steps on its line as one pulse: the
writes that reach the pins are counted by the fake hardware.

Built with the system C++ compiler, or taken prebuilt from
``IMSWITCH_NUCLEO_HOST`` (on Windows: firmware/nucleo_step/host/build_msvc.cmd).
Skipped when neither is there.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.managers.scan_engines import conformance_runner
from imswitch.imcontrol.model.managers.scan_engines.StepProtocolScanEngine import (
    StepProtocolScanEngine,
)
from imswitch.imcontrol.model.scan_program import legacy_microscope_gui as lm
from imswitch.imcontrol.model.scan_program.conformance import (
    engine_log, expected_log, log_differences,
)
from imswitch.imcontrol.model.scan_program.engine import setup_channels
from imswitch.imcontrol.view import ViewSetupInfo

from .test_step_board_firmware_host import PipeTransport

REPO = Path(__file__).resolve().parents[4]
FIRMWARE = REPO / 'firmware' / 'nucleo_step'
CORE = REPO / 'firmware' / 'triggerscope_step' / 'TriggerScopeStep'
SETUP = REPO / 'imswitch/_data/user_defaults/imcontrol_setups/nucleo_polarization_mock_setup.json'


@pytest.fixture(scope='module')
def binary(tmp_path_factory):
    prebuilt = os.environ.get('IMSWITCH_NUCLEO_HOST')
    if prebuilt:
        return Path(prebuilt)
    compiler = shutil.which('c++') or shutil.which('g++') or shutil.which('clang++')
    if compiler is None or sys.platform == 'win32':
        pytest.skip('no C++ compiler to build the fake Nucleo (on Windows: '
                    'firmware/nucleo_step/host/build_msvc.cmd, then IMSWITCH_NUCLEO_HOST)')
    out = tmp_path_factory.mktemp('nucleo') / 'nucleo_host'
    subprocess.run([compiler, '-std=c++11', '-O1', '-Wall', '-pthread',
                    f'-I{FIRMWARE / "NucleoStep"}', f'-I{CORE}', '-o', str(out),
                    str(FIRMWARE / 'host' / 'nucleo_host.cpp'),
                    str(FIRMWARE / 'NucleoStep' / 'nucleo_step.cpp')], check=True)
    return out


@pytest.fixture
def transport(binary):
    t = PipeTransport(binary)
    t.run_timeout = 30.0                  # the Line Scan is 10 s of the board's time
    yield t
    t.close()


@pytest.fixture
def rig(transport):
    setup = ViewSetupInfo.from_json(SETUP.read_text())
    engine = StepProtocolScanEngine(SimpleNamespace(managerProperties={}), name='nucleo',
                                    setupInfo=setup, transport=transport)
    return engine, setup_channels(setup), lm.Wiring.from_setup(setup)


def _run(engine, channels, program):
    compiled = engine.compile(program, channels)
    assert compiled.report.runnable
    engine.prepare(compiled)
    engine.run(compiled)
    engine.join(60)
    assert engine.outcome == ('done', '')
    return compiled


def _changes(transport):
    """How often each output changed on the fake board's pins since #RESET."""
    answer = transport.hook('#CHANGES?')
    assert answer.startswith('OK,#CHANGES,')
    return dict(zip(('dac0', 'dac1', 'ttl0', 'ttl1', 'ttl2'),
                    (int(n) for n in answer.split(',')[2:])))


def test_the_board_says_what_it_is(rig):
    engine, _, _ = rig
    caps = engine.capabilities()
    assert caps.timing_grid_s == 1e-6
    assert sorted(caps.analog) == ['nucleo/dac0', 'nucleo/dac1']
    assert caps.analog['nucleo/dac0'].min_v == 0.0
    assert caps.analog['nucleo/dac0'].max_v == pytest.approx(3.3)
    assert caps.digital == ('nucleo/ttl0', 'nucleo/ttl1', 'nucleo/ttl2')
    assert engine._caps.dac_bits == {0: 12, 1: 12}


def test_the_conformance_programs_pass(transport):
    results = conformance_runner.run(transport, report=lambda line: None)
    assert [r.program for r in results if not r.passed] == [], \
        '\n'.join(f'{r.program}: {r.problems}' for r in results if not r.passed)
    assert len(results) == 6       # the five, and a power level on the second DAC


@pytest.mark.parametrize('mode', ['wide_field', 'wide_field_2d', 'dark_frames', 'galvo_scan',
                                  'line_scan_2d'])
def test_the_rig_runs_its_modes_as_the_interpreter_says(rig, mode):
    engine, channels, wiring = rig
    params = lm.LegacyParams()
    program = {
        'wide_field': lambda: lm.wide_field(params, wiring, frames=2),
        'wide_field_2d': lambda: lm.wide_field_2d(params, wiring, pairs=2),
        'dark_frames': lambda: lm.dark_frames(params, wiring),
        'galvo_scan': lambda: lm.galvo_scan(params, wiring, frames=2),
        'line_scan_2d': lambda: lm.line_scan_2d(params, wiring, pairs=1),
    }[mode]()
    compiled = _run(engine, channels, program)
    code_um = 3.3 / 4095 * wiring.um_per_volt_x
    differences = log_differences(engine_log(engine.log), expected_log(compiled.schedule),
                                  {wiring.galvo_x: code_um, wiring.galvo_y: code_um})
    assert differences == []


def test_a_window_over_many_steps_is_one_pulse_on_its_line(rig, transport):
    """The Galvo Scan's windows run over ~250 steps of its table, each step
    timing the window again. On the pins each line must change exactly twice
    per frame, on and off: an off and an on written at every step boundary
    would be a glitch a camera can end its exposure on."""
    engine, channels, wiring = rig
    frames = 2
    program = lm.galvo_scan(lm.LegacyParams(), wiring, frames=frames)
    assert transport.hook('#RESET') == 'OK,#RESET'
    _run(engine, channels, program)
    changes = _changes(transport)
    for line in ('ttl0', 'ttl1', 'ttl2'):
        assert changes[line] == 2 * frames, changes
    # galvo Y moves through the sweep (a code every second step, about); X holds
    assert changes['dac1'] > 2 * 400
    assert changes['dac0'] <= 1
