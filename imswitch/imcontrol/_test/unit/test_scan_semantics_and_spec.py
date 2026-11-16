"""Semantics and the step-engine spec (review round 1, batch 3;
docs/design/plans/scan-engines-and-expert-scan.md §7.9).

A run tells the positioners where it left them; a pulse-generator laser
switched by hand follows its polarity; no engine takes an undeclared
conversion factor as 1; the raster's move shape is the form's; a board's
log says what level each edge wrote, so conformance catches a firmware that
ignores the host's polarity or volts; ``ALIVE`` keeps a busy board from
being taken for a silent one.
"""

import copy
import dataclasses
import json
import threading
import time
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model import SetupInfo
from imswitch.imcontrol.model.SetupInfo import LaserInfo, TeensyPulseInfo
from imswitch.imcontrol.model.managers.lasers.PulseGeneratorLaserManager import (
    PulseGeneratorLaserManager,
)
from imswitch.imcontrol.model.managers.pulsegen import TeensyPulseManager
from imswitch.imcontrol.model.managers.scan_engines import conformance_runner
from imswitch.imcontrol.model.managers.scan_engines.NidaqScanEngine import NidaqScanEngine
from imswitch.imcontrol.model.managers.scan_engines.StepProtocolScanEngine import (
    SimulatedTransport, StepProtocolScanEngine,
)
from imswitch.imcontrol.model.managers.scan_engines.step_board import SimulatedStepBoard
from imswitch.imcontrol.model.managers.scan_engines.step_protocol import BoardCaps
from imswitch.imcontrol.model.scan_program import Wait, presets
from imswitch.imcontrol.model.scan_program.engine import (
    ScanEngineRefused, UNKNOWN, setup_channels,
)

SETUPS = 'imswitch/_data/user_defaults/imcontrol_setups/'
EXPERT_MOCK = SETUPS + 'expert_mock_scan_setup.json'


# --- where a run leaves the positioners ---------------------------------------------

def _z(rig):
    return rig.master.positionersManager['Z'].position['Z']


def test_a_finished_run_tells_the_positioner_where_it_left_it(qtbot):
    from .test_scan_controller_expert import ExpertRig, _ms_resolft, _setup_dict, STEP_BOARD
    setup = _setup_dict(engine='board')
    setup['scanEngines'] = {'board': copy.deepcopy(STEP_BOARD)}
    # a trigger input, so a run can be stopped while it waits; the lasers'
    # preparation after it declared
    setup['scanEngines']['board']['managerProperties']['simulatedBoard'].update(
        triggerIn=True, triggerLatencyTicks=2)
    setup['positioners']['Z']['analogChannel'] = 'board/dac0'
    for laser, line in zip(list(setup['lasers'])[:2], ('board/ttl0', 'board/ttl1')):
        setup['lasers'][laser]['digitalLine'] = line
        setup['lasers'][laser]['preparation'] = 'notRequired'
    setup['detectors']['Camera']['digitalLine'] = 'board/ttl2'
    rig = ExpertRig(setup)
    try:
        rig.master.positionersManager['Z'].updateTrackedPosition({'Z': 7.0})
        rig.scan.setProgram(_ms_resolft(rig, timepoints=1, start_um=1.0))
        assert rig.run(timeoutS=10.0)
        assert _z(rig) == 1.0            # restore 'first': the first position, not 7
        # stopped while it waits for a trigger that never comes: the board's
        # last write, from its log
        rig.master.positionersManager['Z'].updateTrackedPosition({'Z': 7.0})
        program = _ms_resolft(rig, timepoints=2, start_um=2.0)
        # restored to where it was captured (7) had it finished: so the 2
        # below is the board's last write, not the program's end
        drives = tuple(dataclasses.replace(d, motion=dataclasses.replace(
            d.motion, restore='captured')) for d in program.drives)
        rig.scan.setProgram(dataclasses.replace(
            program, drives=drives, waits=(Wait('time', 'cameraReady'),)))
        rig.ended.clear()
        rig.scan.runScanAdvanced()
        qtbot.waitUntil(lambda: rig.master.scanEngine.board.running, timeout=5000)
        rig.scan.abortScan()
        qtbot.waitUntil(lambda: bool(rig.ended), timeout=5000)
        assert _z(rig) == 2.0            # it reached its first position, then waited
    finally:
        rig.close()


def test_where_a_stopped_run_left_a_device_unknown_is_said_and_nothing_changes():
    from imswitch.imcontrol.controller.controllers import ScanControllerExpert as module

    class _Unreadable:
        name, outcome = 'board', ('stopped', '')

        @property
        def log(self):
            raise OSError('the link is gone')

    assert module._logged_positions([_Unreadable()]) == {}
    assert module._run_outcomes(_Unreadable(), SimpleNamespace()) == {
        'board': ('stopped', '')}
    # an engine that keeps no outcome (the NI-DAQ) ran to its end
    assert module._run_outcomes(SimpleNamespace(name='nidaq'), SimpleNamespace()) == {
        'nidaq': ('done', '')}


# --- a pulse-generator laser by hand follows its polarity ---------------------------

def test_a_pulse_generator_laser_switched_by_hand_follows_its_polarity():
    generator = TeensyPulseManager(TeensyPulseInfo(port=None, useMockOnFailure=True))
    info = LaserInfo(analogChannel=None, digitalLine=3, managerName='PulseGeneratorLaserManager',
                     managerProperties={}, valueRangeMin=0, valueRangeMax=1, wavelength=488,
                     ttlActiveLevel='low')
    laser = PulseGeneratorLaserManager(info, 'L488', pulseGeneratorManager=generator)
    laser.setEnabled(True)
    assert generator.driver.pin_states[3] is False      # on is low
    laser.setEnabled(False)
    assert generator.driver.pin_states[3] is True


# --- no engine takes an undeclared conversion factor as 1 ---------------------------

def _undeclared():
    raw = json.load(open(EXPERT_MOCK))
    raw['positioners']['Z']['managerProperties'].pop('conversionFactor')
    return SetupInfo.from_json(json.dumps(raw), infer_missing=True)


def test_the_nidaq_refuses_a_positioner_without_a_conversion_factor():
    setup = _undeclared()
    program = presets.make('ms-resolft', dict(
        device='Z', cycles=2, planes=2, cycle_step_um=0.25, start_um=1.0,
        pulses=[['405 (ON)', 1e-3, 0.0]], camera='', exposure_pulse=1, timepoints=1,
        pause_s=0.0, move_s=5e-4, settle_s=5e-4, transition='jump'))
    with pytest.raises(ScanEngineRefused) as refused:
        NidaqScanEngine(None, nidaqManager=None, setupInfo=setup).compile(
            program, setup_channels(setup), captured_um={'Z': 1.0})
    verdict = refused.value.report.verdict('conversion Z')
    assert verdict.verdict == UNKNOWN and 'declares no conversionFactor' in verdict.reason


# --- the raster's move shape is the form's --------------------------------------------

def _raster_params(**more):
    return dict(x_device='X', y_device='Y', x_pixels=3, y_pixels=2, x_step_um=0.5,
                y_step_um=0.5, x_start_um=0.0, y_start_um=0.0,
                pulses=[['405 (ON)', 1e-3, 0.0]], camera='', exposure_pulse=1, move_s=5e-4,
                settle_s=5e-4, return_s=1e-3, traversal='forward', **more)


def test_the_raster_moves_as_the_form_says_and_old_parameters_as_they_did():
    jumping = presets.make('raster', _raster_params(transition='jump'))
    assert {d.motion.transition for d in jumping.drives} == {'jump'}
    # saved before the field: it moved smoothly then, and means that still
    saved = presets.make('raster', _raster_params())
    assert {d.motion.transition for d in saved.drives} == {'smooth'}


# --- a board's log says what each edge wrote ------------------------------------------

class _IgnoresTheHostsLevels(SimulatedStepBoard):
    """Firmware that switches its lines on high and off low, and its DACs to
    1 and 0 V, whatever the host sent."""

    def write(self, line):
        if line.startswith('EVENT,'):
            fields = line.split(',')
            fields[-2:] = ['1', '0']
            line = ','.join(fields)
        super().write(line)


def test_the_reference_board_writes_the_levels_the_host_sent():
    results = conformance_runner.run(conformance_runner.simulated_transport(1000),
                                     report=lambda _: None)
    assert all(r.passed for r in results), [r.problems for r in results if not r.passed]


def test_a_firmware_that_ignores_the_hosts_levels_fails_conformance():
    caps = BoardCaps('Stubborn', 'f', grid_ns=1000, dacs={0: (-10.0, 10.0), 1: (0.0, 10.0)},
                     ttl_lines=4)
    results = conformance_runner.run(SimulatedTransport(_IgnoresTheHostsLevels(caps)),
                                     report=lambda _: None)
    failed = {r.program: r.problems for r in results if not r.passed}
    assert any('L1 on' in p and 'wrote 1, the host sent 0' in p for p in failed[
        'raster with line steps'])
    assert any(p.startswith('P on') for p in failed['a power level on the second DAC'])


# --- ALIVE: a busy board is not a silent one --------------------------------------------

class _Busy(SimulatedTransport):
    """A real-board link -- a reply time, reads in slices as a serial port's
    -- whose board, once running, says only ALIVE, every ``alive_s``, for
    longer than the silence limit, then DONE."""
    reply_timeout = 0.05
    run_timeout = 0.02

    def __init__(self, board, alive_s, count):
        super().__init__(board)
        self.alive_s, self.count, self.running = alive_s, count, False
        self.script, self.due = [], 0.0

    def write(self, line):
        if line == 'RUN':
            self.running = True
            self.script = (['OK,RUN', 'STARTED'] + ['ALIVE,0'] * self.count + ['DONE,1'])
            self.due = time.monotonic()
            return
        super().write(line)

    def read(self, timeout):
        if not self.running:
            return super().read(timeout)
        if not self.script:
            return None
        wait = self.due - time.monotonic()
        if wait > timeout:              # nothing within this slice
            time.sleep(timeout)
            return None
        time.sleep(max(0.0, wait))
        line = self.script.pop(0)
        self.due = time.monotonic() + (self.alive_s if line.startswith(('STARTED', 'ALIVE'))
                                       else 0.0)
        return line


def test_a_board_that_says_it_is_alive_is_not_taken_for_a_silent_one():
    from .test_scan_engine_conformance import CHANNELS, SETUP, _raster
    caps = BoardCaps('Busy', 'f', grid_ns=10_000, dacs={0: (-10.0, 10.0), 1: (-10.0, 10.0)},
                     ttl_lines=3, alive_ticks=1000)                   # 10 ms
    transport = _Busy(SimulatedStepBoard(caps), alive_s=0.03, count=6)
    engine = StepProtocolScanEngine(SimpleNamespace(managerProperties={}), name='Board',
                                    setupInfo=SETUP, transport=transport)
    compiled = engine.compile(_raster(), CHANNELS)
    engine.prepare(compiled)
    limit = engine._silence_limit(transport)
    assert limit == pytest.approx(0.06)                  # 10 ms + the 50 ms reply time
    engine.run(compiled, blocking=True)                  # 180 ms of ALIVE, no STEP
    assert engine.outcome == ('done', '')


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
