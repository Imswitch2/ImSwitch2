"""Hardware → What this setup can scan… (docs/design/plans/
scan-engines-and-expert-scan.md §3.3): every engine of the setup, what the
panel does with it, each feature's verdict and why, what it declares and
where that came from, the devices on it, and the devices on none.
"""

import json

import pytest

from imswitch.imcontrol.model.managers.scan_engines.capability_report import (
    setup_capabilities,
)
from imswitch.imcontrol.model.managers.scan_engines.loader import build_scan_engines
from imswitch.imcontrol.model.scan_program.engine import (
    EMULATED, IMPOSSIBLE, NATIVE, UNKNOWN,
)
from imswitch.imcontrol.model.scan_program.split import Wire, drift, max_pulse_interval_s
from imswitch.imcontrol.view import ViewSetupInfo

SETUPS = 'imswitch/_data/user_defaults/imcontrol_setups/'
STEP_BOARDS = SETUPS + 'expert_step_boards_mock_setup.json'
SIMPLE_MOCK = SETUPS + 'simple_mock_setup.json'


class _FakeNidaq:
    sigScanBuilt = sigScanStarted = sigScanDone = sigScanBuildFailed = None


def _report(path=STEP_BOARDS, edit=None):
    setup = json.loads(open(path).read())
    if edit is not None:
        edit(setup)
    setup = ViewSetupInfo.from_json(json.dumps(setup))
    engines = build_scan_engines(setup, nidaqManager=_FakeNidaq())
    try:
        return setup_capabilities(engines, setup)
    finally:
        engines.finalize()


def _entry(entries, item):
    return next(e for e in entries if e.item == item)


def _on_a_waveform_panel(setup):
    # The Expert panel runs a program where its devices are (§7.10); a
    # waveform panel on scan.engine.
    setup['scan']['scanWidgetType'] = 'Advanced'


def test_the_leader_follower_and_idle_engine_each_say_what_the_panel_does_with_them():
    report = _report(edit=_on_a_waveform_panel)
    assert [s.engine for s in report.engines] == ['nidaq', 'triggerscope', 'aux']
    assert report.section('triggerscope').role.startswith('The scan panel runs on it')
    assert 'It leads aux' in report.section('triggerscope').role
    assert 'wired to triggerscope/ttl15' in report.section('aux').role
    assert 'No device of the setup is on its channels' in report.section('nidaq').role
    assert not report.unscanned


def _assessed(engine, program, channels):
    """The worst verdict the engine's own assessment gives a program."""
    from imswitch.imcontrol.model.managers.scan_engines.capability_report import _worst
    report = engine.assess(program, channels)
    new = [v for v in report.verdicts if v.verdict != NATIVE] or report.verdicts
    worst = _worst(new)
    return worst.verdict, worst.reason or worst.cost


def _program(**changes):
    from imswitch.imcontrol.model.scan_program import (
        DeviceDrive, Loop, Motion, ScanProgram, TimelineEvent,
    )
    base = dict(loops=(Loop('x', 'scan_x', 3),), step_s=10e-3, timing_policy='strict-v1',
                drives=(DeviceDrive('Z', weights_um={'x': 0.5}, motion=Motion(
                    transition='jump', origin='absolute', restore='first')),),
                timeline=(TimelineEvent('405 (ON)', 'ttl', 0.0, 1e-3, during='any'),))
    base.update(changes)
    return ScanProgram(**base)


def test_every_feature_is_the_verdict_the_engine_itself_gives():
    """Not a restatement (review round 1): each entry is what the engine's
    assess says of a program that uses the feature -- written here apart from
    the report's own probes."""
    import dataclasses
    from imswitch.imcontrol.model.scan_program import Wait
    from imswitch.imcontrol.model.scan_program.engine import setup_channels
    setup = ViewSetupInfo.from_json(open(STEP_BOARDS).read())
    engines = build_scan_engines(setup, nidaqManager=_FakeNidaq())
    try:
        report = setup_capabilities(engines, setup)
        board, channels = engines.engines['triggerscope'], setup_channels(setup)
        entry = _entry(report.section('triggerscope').features, 'linear transition')
        linear = _program(drives=(dataclasses.replace(
            _program().drives[0], motion=dataclasses.replace(
                _program().drives[0].motion, transition='linear')),))
        assert (entry.verdict, entry.text) == _assessed(board, linear, channels)
        assert entry.verdict == IMPOSSIBLE
        entry = _entry(report.section('triggerscope').features, 'trigger wait between steps')
        assert entry.verdict == _assessed(board, _program(waits=(Wait('step', 'external'),)),
                                          channels)[0] == NATIVE
        assert _entry(report.section('triggerscope').features,
                      'stepped positions').verdict == NATIVE
    finally:
        engines.finalize()

    report = _report(SIMPLE_MOCK)
    nidaq = report.section('nidaq')
    stepped = _entry(nidaq.features, 'stepped positions')
    assert (stepped.verdict, stepped.text) == (EMULATED, 'played as sampled waveforms')
    assert _entry(nidaq.features, 'swept axes and trajectories').verdict == NATIVE
    trigger = _entry(nidaq.features, 'trigger wait between steps')
    assert trigger.verdict == IMPOSSIBLE and 'cannot wait for an external trigger' in trigger.text
    stop = _entry(nidaq.features, 'stop mid-scan')
    assert stop.verdict == NATIVE and 'stopScan' in stop.text      # plan scan-stop-now


def test_a_pulse_generators_start_trigger_is_as_its_engine_says():
    """The report said 'unknown latency' for a generator's start trigger
    while the generator's own assessment answered for it."""
    from imswitch.imcontrol.model.SetupInfo import TeensyPulseInfo
    from imswitch.imcontrol.model.managers.pulsegen import TeensyPulseManager

    def generator(raw):
        raw['scanEngines'] = {'teensy': {'managerName': 'PulseGeneratorScanEngine',
                                         'managerProperties': {}}}
        raw['teensyPulse'] = {'port': None, 'useMockOnFailure': True}
        raw['lasers']['Pulsed'] = {'managerName': 'PulseGeneratorLaserManager',
                                   'managerProperties': {}, 'digitalLine': 1,
                                   'wavelength': 405, 'valueRangeMin': 0, 'valueRangeMax': 1}
    raw = json.loads(open(SIMPLE_MOCK).read())
    generator(raw)
    setup = ViewSetupInfo.from_json(json.dumps(raw))
    teensy = TeensyPulseManager(TeensyPulseInfo(port=None, useMockOnFailure=True))
    engines = build_scan_engines(setup, nidaqManager=_FakeNidaq(),
                                 lowLevelManagers={'pulseGeneratorManager': teensy})
    report = setup_capabilities(engines, setup)
    entry = _entry(report.section('teensy').features, 'trigger wait before the run')
    engine = engines.engines['teensy']
    from imswitch.imcontrol.model.scan_program import Loop, ScanProgram, TimelineEvent, Wait
    from imswitch.imcontrol.model.scan_program.engine import setup_channels
    program = ScanProgram(loops=(Loop('x', 'scan_x', 2),), drives=(), step_s=2e-6,
                          timeline=(TimelineEvent('Pulsed', 'ttl', 0.0, 1e-6, during='any'),),
                          waits=(Wait('start', 'external'),), timing_policy='strict-v1')
    assert (entry.verdict, entry.text) == _assessed(engine, program, setup_channels(setup))
    assert _entry(report.section('teensy').features, 'stepped positions').verdict == IMPOSSIBLE


def test_declared_numbers_say_where_they_came_from():
    board = _report().section('triggerscope')
    assert _entry(board.numbers, 'timing grid').text == \
        '1 µs (1e+06 Hz), from triggerscope *CAPS? grid_ns'
    assert _entry(board.numbers, 'clock accuracy').text == \
        '20 ppm, from triggerscope *CAPS? clock_ppm'
    devices = {e.item: e.text for e in board.devices}
    assert devices['Z (positioner)'] == 'triggerscope/dac0: its position, 0 to 10 V'
    assert devices['Camera (detector)'] == 'triggerscope/ttl2: its trigger'


def test_a_follower_is_told_how_far_apart_the_pulses_may_be():
    aux = _report().section('aux')
    assert _entry(aux.features, 'follow step by step').verdict == NATIVE
    apart = _entry(aux.features, 'pulses apart')
    # half of the 1 µs tick over 20 + 20 ppm
    assert apart.verdict == NATIVE and apart.text.startswith('at most 12.5 ms')


@pytest.mark.parametrize('scale', [0.999, 1.001])
def test_the_longest_interval_is_the_drift_rule_itself(scale):
    wire = Wire('aux', 'triggerscope/ttl15', 2e-6, 1e-6, 1e-6, 20.0)
    longest = max_pulse_interval_s(1e-6, 20.0, wire)
    verdict, _ = drift('triggerscope', 1e-6, 20.0, wire, longest * scale)
    assert verdict == (NATIVE if scale < 1 else IMPOSSIBLE)


def _drop_aux_clock(setup):
    setup['scanEngines']['aux']['managerProperties']['simulatedBoard'].pop('clockPpm')


def test_an_undeclared_clock_leaves_the_pulse_interval_unknown_by_name():
    aux = _report(edit=_drop_aux_clock).section('aux')
    apart = _entry(aux.features, 'pulses apart')
    assert apart.verdict == UNKNOWN
    assert apart.text.startswith("aux's clock accuracy not declared")
    assert _entry(aux.numbers, 'clock accuracy').verdict == UNKNOWN


def _laser_on_no_engine(setup):
    setup['lasers']['561 (EXC)']['digitalLine'] = 'elsewhere/ttl0'


def test_a_device_on_no_engine_takes_part_in_no_scan():
    report = _report(edit=_laser_on_no_engine)
    [entry] = report.unscanned
    assert entry.item == '561 (EXC) (laser)' and entry.verdict == IMPOSSIBLE
    assert 'elsewhere/ttl0 is on no scan engine' in entry.text
    assert 'On no scan engine' in report.text()


def _unplugged(setup):
    setup['scanEngines']['aux']['managerProperties'] = {
        'port': '/dev/imswitch-no-such-port', 'baudrate': 115200, 'replyTimeoutS': 0.1}


def test_a_board_that_has_not_answered_is_unknown_with_the_reason():
    aux = _report(edit=_unplugged).section('aux')
    board = _entry(aux.features, 'board')
    assert board.verdict == UNKNOWN and 'has not answered' in board.text
    assert 'imswitch-no-such-port' in board.text


def test_an_nidaq_only_setup_reports_its_devices_on_the_nidaq():
    report = _report(SIMPLE_MOCK)
    [nidaq] = report.engines
    assert nidaq.role == 'The scan panel runs on it (scan.engine).'
    assert nidaq.devices and not report.unscanned



# --- Hardware → What this setup can scan… ------------------------------------------

@pytest.fixture
def qapp():
    from qtpy import QtWidgets
    # Held at module level: dropping the last QApplication reference deletes
    # every Python-owned QObject.
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


@pytest.fixture
def mainView(qapp):
    from imswitch.imcontrol.model.Options import Options
    from imswitch.imcontrol.view import ImConMainView

    view = ImConMainView(Options(setupFileName='x.json'),
                         ViewSetupInfo.from_dict({}, infer_missing=True))
    yield view
    view.deleteLater()


def _open(mainView, scanEngines, setupInfo):
    """The controller's handler on a stub holding just what it reads."""
    from types import SimpleNamespace
    from imswitch.imcontrol.controller.ImConMainController import ImConMainController

    errors = []
    stub = SimpleNamespace(
        _ImConMainController__mainView=mainView,
        _ImConMainController__masterController=SimpleNamespace(scanEngines=scanEngines),
        _ImConMainController__setupInfo=setupInfo,
        _ImConMainController__logger=SimpleNamespace(
            error=lambda message, **_k: errors.append(message)))
    ImConMainController.openScanCapabilities.__get__(stub)()
    return errors


def test_the_hardware_entry_asks_the_controller(mainView):
    opened = []
    mainView.sigOpenScanCapabilities.connect(lambda: opened.append(True))
    mainView.scanCapabilitiesAction.trigger()
    assert opened == [True]


def test_the_dialog_shows_the_setups_report_afresh_each_time(mainView):
    setup = ViewSetupInfo.from_json(open(STEP_BOARDS).read())
    engines = build_scan_engines(setup, nidaqManager=_FakeNidaq())
    try:
        assert _open(mainView, engines, setup) == []
        dialog = mainView.scanCapabilitiesDialog
        assert dialog.isVisible() and not dialog.isModal()
        assert dialog.reportText() == setup_capabilities(engines, setup).text()
        dialog.setReport('')
        _open(mainView, engines, setup)
        assert 'pulses apart -- at most 12.5 ms' in dialog.reportText()
        dialog.copyButton.click()
        from qtpy import QtGui
        assert QtGui.QGuiApplication.clipboard().text() == dialog.reportText()
    finally:
        engines.finalize()
        mainView.scanCapabilitiesDialog.close()


def test_a_report_that_fails_says_why_in_the_dialog(mainView, monkeypatch):
    from imswitch.imcontrol.model.managers.scan_engines import capability_report

    def broken(*_args):
        raise RuntimeError('no such engine')
    monkeypatch.setattr(capability_report, 'setup_capabilities', broken)
    from types import SimpleNamespace
    errors = _open(mainView, SimpleNamespace(engines={}), None)
    assert errors and 'no such engine' in errors[0]
    assert mainView.scanCapabilitiesDialog.reportText() == \
        'The report could not be made:\nno such engine'
    mainView.scanCapabilitiesDialog.close()


def test_opening_the_dialog_asks_a_board_that_had_not_answered_again(mainView):
    from imswitch.imcontrol.model.managers.scan_engines.loader import ScanEngines
    setup = ViewSetupInfo.from_json(open(STEP_BOARDS).read())
    asked = []

    class _Absent:
        name, unanswered = 'board', 'not asked yet'
        owns = staticmethod(lambda channel: False)

        def connect(self):
            asked.append(True)
            self.unanswered = None       # it answers now

        def capabilities(self):
            from imswitch.imcontrol.model.scan_program.engine import EngineCapabilities
            return EngineCapabilities(name='board')

        def assess(self, program, channels):
            from imswitch.imcontrol.model.scan_program.engine import FeasibilityReport
            return FeasibilityReport('board')

    engines = ScanEngines({'board': _Absent()}, 'board', {})
    _open(mainView, engines, setup)
    assert asked == [True]
    assert 'has not answered' not in mainView.scanCapabilitiesDialog.reportText()
    mainView.scanCapabilitiesDialog.close()

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
