"""The program's devices choose the engine (docs/design/plans/
scan-engines-and-expert-scan.md §7.10).

Every channel is one engine's: an Expert program runs on the engine its
devices are on, on the leader with its followers when they spread over
those, on ``scan.engine`` when none is on an engine; any other spread is
refused, naming which device is on which engine. The panel hears the run on
the engine it runs on, and a run in progress keeps its engine.
"""

import json

import pytest

from imswitch.imcontrol.model.errors import ScanDesignRefusedError
from imswitch.imcontrol.model.managers.scan_engines.capability_report import (
    setup_capabilities,
)
from imswitch.imcontrol.model.managers.scan_engines.loader import build_scan_engines
from imswitch.imcontrol.model.scan_program import (
    DeviceDrive, Loop, Motion, ScanProgram, TimelineEvent, presets,
)
from imswitch.imcontrol.model.scan_program.engine import setup_channels
from imswitch.imcontrol.view import ViewSetupInfo

SETUPS = 'imswitch/_data/user_defaults/imcontrol_setups/'
STEP_BOARDS = SETUPS + 'expert_step_boards_mock_setup.json'


class _FakeNidaq:
    sigScanBuilt = sigScanStarted = sigScanDone = sigScanBuildFailed = None


@pytest.fixture
def boards():
    setup = ViewSetupInfo.from_json(open(STEP_BOARDS).read())
    engines = build_scan_engines(setup, nidaqManager=_FakeNidaq())
    yield engines, setup_channels(setup), setup
    engines.finalize()


def _program(*lasers, drive=None):
    drives = () if drive is None else (DeviceDrive(drive, weights_um={'x': 0.5}, motion=Motion(
        transition='jump', origin='absolute', restore='first')),)
    return ScanProgram(loops=(Loop('x', 'scan_x', 3),), drives=drives, step_s=10e-3,
                       timeline=tuple(TimelineEvent(laser, 'ttl', 0.0, 1e-3, during='any')
                                      for laser in lasers))


# --- the choice ----------------------------------------------------------------------

def test_a_program_runs_where_its_devices_are(boards):
    engines, channels, _ = boards
    on_leader = engines.for_program(_program('405 (ON)', drive='Z'), channels)
    assert on_leader.engine is engines.engines['triggerscope']
    assert on_leader.devices == {'triggerscope': ('405 (ON)', 'Z')}
    # the follower's devices alone: the follower, standing alone
    assert engines.for_program(_program('561 (EXC)'), channels).engine is engines.engines['aux']
    # both: the leader with its follower
    both = engines.for_program(_program('405 (ON)', '561 (EXC)'), channels)
    assert both.engine is engines.composite
    assert both.devices == {'aux': ('561 (EXC)',), 'triggerscope': ('405 (ON)',)}
    # none on an engine: scan.engine, alone
    assert engines.for_program(_program(), channels).engine is engines.engines['triggerscope']


def test_devices_on_engines_that_do_not_run_together_are_refused_by_name():
    setup = json.load(open(SETUPS + 'expert_mock_scan_setup.json'))
    setup['teensyPulse'] = {'port': None, 'useMockOnFailure': True}
    setup['scanEngines'] = {'teensy': {'managerName': 'PulseGeneratorScanEngine',
                                       'managerProperties': {}}}
    setup['lasers']['Pulsed'] = {'managerName': 'PulseGeneratorLaserManager',
                                 'managerProperties': {}, 'digitalLine': 2,
                                 'wavelength': 405, 'valueRangeMin': 0, 'valueRangeMax': 1}
    info = ViewSetupInfo.from_json(json.dumps(setup))
    from imswitch.imcontrol.model.SetupInfo import TeensyPulseInfo
    from imswitch.imcontrol.model.managers.pulsegen import TeensyPulseManager
    generator = TeensyPulseManager(TeensyPulseInfo(port=None, useMockOnFailure=True))
    engines = build_scan_engines(info, nidaqManager=_FakeNidaq(), lowLevelManagers={
        'pulseGeneratorManager': generator})
    choice = engines.for_program(_program('Pulsed', drive='Z'), setup_channels(info))
    assert choice.engine is None
    assert choice.refusal == (
        'Its devices are on engines that do not run together: Z on nidaq; Pulsed on teensy. '
        'Engines run one program together only as scan.engine leading the engines wired to '
        'follow it (triggerInput).')


# --- the panel -----------------------------------------------------------------------

def _teensy_beside_the_nidaq():
    from .test_scan_controller_expert import _setup_dict
    setup = _setup_dict()                      # scan.engine: the NI-DAQ
    setup['teensyPulse'] = {'port': None, 'useMockOnFailure': True}
    setup['scanEngines'] = {'teensy': {'managerName': 'PulseGeneratorScanEngine',
                                       'managerProperties': {}}}
    setup['lasers']['Pulsed'] = {'managerName': 'PulseGeneratorLaserManager',
                                 'managerProperties': {}, 'digitalLine': 2,
                                 'wavelength': 405, 'valueRangeMin': 0, 'valueRangeMax': 1}
    return setup


@pytest.fixture
def rig(qtbot):
    from .test_scan_controller_expert import ExpertRig
    rig = ExpertRig(_teensy_beside_the_nidaq())
    yield rig
    rig.close()


def _pulses(frames):
    return presets.make('pulse-sequence', dict(
        pulses=[['Pulsed', 1e-3, 2e-3]], camera='', exposure_pulse=1, frames=frames,
        pause_s=0.0))


def _rises(rig, since):
    return [e for e in rig.master.pulseGeneratorManager.driver.timeline
            if e.source == 'seq' and e.level and e.virtual_time_ns >= since]


def test_one_panel_runs_each_program_on_its_devices_engine(rig):
    from .test_scan_controller_expert import _ms_resolft
    driver = rig.master.pulseGeneratorManager.driver
    iterations = []
    rig.master.nidaqManager.sigScanDone.connect(lambda *a: iterations.append(1))

    rig.scan.setProgram(_pulses(4))        # its laser is the Teensy's, not scan.engine's
    assert rig.widget.reportText().startswith('Runnable on teensy.')
    since = driver._virtual_clock_ns
    assert rig.run(timeoutS=10.0) and not rig.rejected
    assert len(_rises(rig, since)) == 4 and iterations == []

    rig.scan.setProgram(_ms_resolft(rig, timepoints=1))       # Z and the lasers: the NI-DAQ's
    assert rig.widget.reportText().startswith('Runnable on nidaq.')
    assert rig.run(timeoutS=10.0) and not rig.rejected
    assert iterations == [1]

    rig.scan.setProgram(_pulses(2))        # and back
    since = driver._virtual_clock_ns
    assert rig.run(timeoutS=10.0) and not rig.rejected
    assert len(_rises(rig, since)) == 2 and iterations == [1]


def test_the_panel_hears_only_the_engine_it_runs_on(rig):
    nidaq, teensy = rig.master.nidaqManager, rig.master.scanEngines.engines['teensy']

    def heard(source):
        return {name: source.receivers(getattr(source, name))
                for name in ('sigScanBuilt', 'sigScanStarted', 'sigScanDone', 'sigScanBuildFailed')}

    # the base's four, and the Expert's own start (its progress)
    panel = {'sigScanBuilt': 1, 'sigScanStarted': 2, 'sigScanDone': 1, 'sigScanBuildFailed': 1}
    on_nidaq = heard(nidaq)              # the panel and whoever else listens to the NI-DAQ
    assert not any(heard(teensy).values())
    rig.scan._bindScanEngine(teensy)
    assert heard(nidaq) == {name: count - panel[name] for name, count in on_nidaq.items()}
    assert heard(teensy) == panel
    assert teensy.receivers(teensy.sigScanStopped) == teensy.receivers(teensy.sigScanFailed) == 1
    rig.scan._bindScanEngine(rig.master.scanEngines.engines['nidaq'])
    assert heard(nidaq) == on_nidaq and not any(heard(teensy).values())


def test_a_run_in_progress_keeps_its_engine(rig):
    from .test_scan_controller_expert import _ms_resolft
    rig.scan.setProgram(_pulses(2))
    rig.scan._bindScanEngine(rig.master.scanEngines.engines['teensy'])   # as its run began
    rig.scan.setProgram(_ms_resolft(rig, timepoints=1))
    with pytest.raises(ScanDesignRefusedError, match='runs on nidaq, and the scan in '
                                                     'progress on teensy; stop it first'):
        rig.scan._buildScanSignals()


# --- what the setup dialog says --------------------------------------------------------

def test_the_setup_dialog_says_where_an_expert_program_runs(boards):
    engines, _, setup = boards
    report = setup_capabilities(engines, setup)
    leader = report.section('triggerscope').role
    assert leader.startswith('An Expert program runs on it when its devices are on it, or '
                             'when it has no device on an engine (scan.engine).')
    assert 'devices of aux runs on them too, with this engine leading' in leader
    assert report.section('aux').role == (
        'An Expert program runs on it when its devices are on it. One that also uses the '
        'devices of triggerscope runs on both, triggerscope leading: its trigger input is '
        'wired to triggerscope/ttl15.')
    assert report.section('nidaq').role.endswith('No device of the setup is on its channels.')


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
