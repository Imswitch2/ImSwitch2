"""Scan engines in a setup (docs/design/plans/scan-engines-and-expert-scan.md
§3, §5.0, M1).

A setup always has the NI-DAQ engine; ``scanEngines`` adds others through the
device-plugin registry; ``scan.engine`` chooses the one a panel runs on; a
channel two engines claim is a setup error. The panels grey out what the
engine cannot drive and the run refuses it -- a laser on another board's line
used to be dropped silently. ``ttlActiveLevel`` maps logical on/off to the
line's level on every path that drives it.
"""

import json
from types import SimpleNamespace

import numpy as np
import pytest
from qtpy import QtTest

from imswitch.imcontrol.model import ScanDesignRefusedError
from imswitch.imcontrol.model.managers.NidaqManager import _ttl_level, scan_digital_outputs
from imswitch.imcontrol.model.managers.scan_engines.loader import build_scan_engines
from imswitch.imcontrol.model.plugins.registry import get_default_registry
from imswitch.imcontrol.model.scan_program.engine import SetupError
from imswitch.imcontrol.view import ViewSetupInfo
from .test_simple_point_scan_controller import Rig

SETUPS = 'imswitch/_data/user_defaults/imcontrol_setups/'
SIMPLE_MOCK = SETUPS + 'simple_mock_setup.json'


def _setup_dict(**changes):
    setup = json.loads(open(SIMPLE_MOCK).read())
    for path, value in changes.items():
        node = setup
        *parents, last = path.split('.')
        for key in parents:
            node = node[key]
        if value is None:
            node.pop(last, None)
        else:
            node[last] = value
    return setup


def _setup(**changes):
    return ViewSetupInfo.from_json(json.dumps(_setup_dict(**changes)))


class _FakeNidaq:
    sigScanBuilt = sigScanStarted = sigScanDone = sigScanBuildFailed = None


# ---------------------------------------------------------------------------
# Building the engines
# ---------------------------------------------------------------------------

def test_a_setup_without_scan_engines_runs_on_the_nidaq():
    engines = build_scan_engines(_setup(), nidaqManager=_FakeNidaq())
    assert list(engines.engines) == ['nidaq'] and engines.selected == 'nidaq'
    lasers = _setup().lasers
    for name, info in lasers.items():
        assert engines.owner_of(info.getDigitalLine()) == 'nidaq', name


def test_engines_come_from_the_registry_by_name_or_alias():
    registry = get_default_registry()
    assert registry.load_manager_class('scan_engine', 'builtin.simulated-step-board').__name__ \
        == 'SimulatedStepEngine'
    setup = _setup(scanEngines={'board': {'managerName': 'SimulatedStepEngine',
                                          'managerProperties': {'timingGridS': 1e-5,
                                                                'digitalLines': ['Sim/TTL0']}}})
    engines = build_scan_engines(setup, nidaqManager=_FakeNidaq())
    assert sorted(engines.engines) == ['board', 'nidaq']
    assert engines.engines['board'].owns('Sim/TTL0')


def test_a_channel_two_engines_claim_fails_the_setup_by_name():
    line = _setup().lasers['488 (EXC)'].getDigitalLine()
    setup = _setup(scanEngines={'board': {'managerName': 'SimulatedStepEngine',
                                          'managerProperties': {'timingGridS': 1e-5,
                                                                'digitalLines': [line]}}})
    with pytest.raises(SetupError, match=rf'{line} \(488 \(EXC\)\) is claimed by nidaq and board'):
        build_scan_engines(setup, nidaqManager=_FakeNidaq())


def test_an_unknown_scan_engine_choice_is_refused():
    with pytest.raises(SetupError, match="scan.engine names 'nope'"):
        build_scan_engines(_setup(**{'scan.engine': 'nope'}), nidaqManager=_FakeNidaq())


def test_an_unknown_engine_manager_names_what_is_installed():
    setup = _setup(scanEngines={'x': {'managerName': 'acme.board', 'managerProperties': {}}})
    with pytest.raises(SetupError, match='acme.board'):
        build_scan_engines(setup, nidaqManager=_FakeNidaq())


def test_the_nidaq_declares_what_it_drives_and_where_that_came_from():
    engines = build_scan_engines(_setup(), nidaqManager=_FakeNidaq())
    caps = engines.engine.capabilities()
    assert caps.streaming and caps.stepping == 'emulated' and caps.abort == 'none'
    assert caps.timing_grid_s == pytest.approx(1e-5)
    assert 'runScan drives lines' in caps.sources['digital']
    nidaq = engines.engine
    assert nidaq.owns('Dev1/port0/line3') and nidaq.owns('Dev1/ao0')
    assert nidaq.owns('PXI1Slot2/ao1')
    assert not nidaq.owns('Triggerscope/TTL1') and not nidaq.owns('Triggerscope/DAC1')


# ---------------------------------------------------------------------------
# ttlActiveLevel on every path that drives a line
# ---------------------------------------------------------------------------

def test_an_inverted_line_is_driven_low_for_on_in_a_scan():
    setup = _setup(**{'lasers.488 (EXC).ttlActiveLevel': 'low'})
    ttl = {'488 (EXC)': np.array([True, False, True]), '405 (ON)': np.array([True, False, False])}
    pairs = [(name, info.getDigitalLine()) for name, info in setup.lasers.items()
             if name in ttl]
    devices, signals, _ = scan_digital_outputs(ttl, pairs, setup.getDevice)
    by_name = dict(zip(devices, signals))
    assert list(by_name['488 (EXC)']) == [False, True, False]
    assert list(by_name['405 (ON)']) == [True, False, False]


def test_an_inverted_line_is_driven_low_for_on_by_hand(qtbot):
    setupText = json.dumps(_setup_dict(**{'lasers.488 (EXC).ttlActiveLevel': 'low'}))
    rig = Rig(setupText)
    try:
        nidaq = rig.master.nidaqManager
        written = []
        nidaq._runOneShotOutput = lambda name, create, signal: written.append(np.asarray(signal))
        nidaq.setDigital('488 (EXC)', True)
        nidaq.setDigital('488 (EXC)', False)
        nidaq.setDigital('405 (ON)', True)
        assert [bool(w.all()) if w.any() else False for w in written] == [False, True, True]
    finally:
        rig.close()


def test_the_triggerscope_maps_the_level_too():
    from imswitch.imcontrol.model.managers.TriggerScopeManager import TriggerScopeManager
    sent = []
    board = TriggerScopeManager.__new__(TriggerScopeManager)
    board.send = sent.append
    info = SimpleNamespace(ttlLevel=lambda on: not on)
    board._deviceInfo = {'L561': {'TTLLine': '3', 'ttlLevel': info.ttlLevel}}
    board.setDigital('L561', True)
    board.setDigital('L561', False)
    assert sent == ['TTL3,0', 'TTL3,1']


def test_an_undeclared_polarity_is_active_high_and_a_bad_one_is_refused():
    assert _ttl_level(SimpleNamespace(), True) is True
    with pytest.raises(ValueError, match='ttlActiveLevel must be "high" or "low"'):
        _setup(**{'lasers.488 (EXC).ttlActiveLevel': 'inverted'}).lasers['488 (EXC)'].ttlLevel(True)


# ---------------------------------------------------------------------------
# The panels grey out what the engine cannot drive; the run refuses it
# ---------------------------------------------------------------------------

@pytest.fixture
def foreign_line_rig(qtbot):
    """488 is wired to a TriggerScope line the NI-DAQ does not drive."""
    rig = Rig(json.dumps(_setup_dict(**{'lasers.488 (EXC).digitalLine': 'Triggerscope/TTL1'})))
    yield rig
    rig.close()


def test_advanced_greys_a_laser_on_another_boards_line(foreign_line_rig):
    advanced = foreign_line_rig.panel.backend
    label, row = advanced._line_step_device_widgets['488 (EXC)']
    assert not label.isEnabled() and 'Triggerscope/TTL1' in label.toolTip()
    other_label, _ = advanced._line_step_device_widgets['405 (ON)']
    assert other_label.isEnabled()
    assert advanced.advancedOptionsBox.isEnabled()     # other lasers remain
    assert "which the scan engine 'nidaq' does not drive" in advanced.ttlNoteLabel.text()


def test_a_scan_that_fires_it_is_refused_instead_of_dropping_it(foreign_line_rig):
    scan = foreign_line_rig.scan
    analog, digital = scan._readBackendWidget()
    digital = dict(digital, target_device=list(digital['target_device']) + ['488 (EXC)'])
    with pytest.raises(ScanDesignRefusedError, match='488 \\(EXC\\) is wired to Triggerscope/TTL1'):
        scan._make_full_scan(analog, digital)


def test_simple_does_not_offer_a_laser_it_cannot_switch(foreign_line_rig):
    gates = [gate.name for gate in foreign_line_rig.scan._scanLimits().gates]
    assert '488 (EXC)' not in gates and gates


@pytest.fixture
def analog_only_rig(qtbot):
    """A scanner that takes analog only: no laser has a digital line."""
    setup = _setup_dict()
    for laser in setup['lasers'].values():
        laser['digitalLine'] = None
    rig = Rig(json.dumps(setup))
    yield rig
    rig.close()


def test_advanced_greys_every_ttl_part_on_an_analog_only_scanner(analog_only_rig):
    advanced = analog_only_rig.panel.backend
    for widget in (advanced.advancedOptionsBox, advanced.plotIncludeTTLBox,
                   advanced.linestep_counter, advanced._advGroup):
        assert not widget.isEnabled()
    assert advanced.ttlNoteLabel.text().startswith('This scanner has no digital lines')
    assert not advanced.ttlNoteLabel.isHidden()


def test_simple_hides_its_lasers_on_an_analog_only_scanner_and_still_scans(analog_only_rig):
    view = analog_only_rig.widget
    assert view.paletteZone.isHidden() and not view.laserNote.isHidden()
    assert 'Set them in the Laser panel' in view.laserNote.text()
    view.startButton.click()
    QtTest.QTest.qWait(300)
    view.stopButton.click()
    assert analog_only_rig.waitForEnd()
    assert analog_only_rig.rejections == []



# ---------------------------------------------------------------------------
# The NI-DAQ engine's lifecycle on the mock (conformance, M1)
# ---------------------------------------------------------------------------

def test_the_nidaq_engine_runs_a_scan_through_its_lifecycle_in_order(qtbot):
    rig = Rig(open(SIMPLE_MOCK).read())
    try:
        engine = rig.master.scanEngine
        assert engine is rig.scan._engineForScans() and engine.name == 'nidaq'
        seen = []
        engine.sigScanBuilt.connect(lambda *a: seen.append('built'))
        engine.sigScanStarted.connect(lambda: seen.append('started'))
        engine.sigScanDone.connect(lambda: seen.append('done'))
        engine.sigScanBuildFailed.connect(lambda: seen.append('failed'))
        rig.scan.setSimpleScanMode('acquisition')
        rig.widget.startButton.click()
        assert rig.waitForEnd()
        assert seen == ['built', 'started', 'done']
        # Stopping an NI-DAQ scan lets the running iteration finish.
        assert engine.abort() == 'none'
    finally:
        rig.close()


def test_what_the_nidaq_cannot_do_is_refused_by_its_report():
    from imswitch.imcontrol.model.scan_program import Loop, ScanProgram, TimelineEvent, Wait
    from imswitch.imcontrol.model.scan_program.engine import IMPOSSIBLE, setup_channels
    setup = _setup()
    engine = build_scan_engines(setup, nidaqManager=_FakeNidaq()).engine
    program = ScanProgram(
        loops=(Loop('x', 'scan_x', 2),), drives=(), step_s=1e-3,
        timeline=(TimelineEvent('405 (ON)', 'ttl', 0.0, 1e-3, during='any'),),
        waits=(Wait('x', 'cameraReady'),))
    report = engine.assess(program, setup_channels(setup))
    assert report.verdict('ttl 405 (ON)').verdict == 'native'
    verdict = report.verdict('trigger wait at x')
    assert verdict.verdict == IMPOSSIBLE and 'cannot wait for an external trigger' in verdict.reason


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
