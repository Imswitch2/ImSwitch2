"""The Expert scan panel on the simulated rig (docs/design/plans/
scan-engines-and-expert-scan.md §4, M3).

A real MasterController on ``galvo_apd_simple_mock_scan_setup.json`` (two
APDs imaging a synthetic sample from the scan waveforms, three lasers, a
simulated NI-DAQ) with a mock camera added, the real Expert controller and
panel. Scans really run; a camera recording goes through the recording
manager and is read back by the layout resolver and SNOUTY.
"""
import copy
import functools
import importlib
import json
import time
from types import SimpleNamespace

import numpy as np
import pytest
from qtpy import QtTest

from imswitch.imcommon.model.acquisition_layout import recorded_frame_coordinates
from imswitch.imcontrol._test import optionsBasic
from imswitch.imcontrol.controller.CommunicationChannel import CommunicationChannel
from imswitch.imcontrol.controller.MasterController import MasterController
from imswitch.imcontrol.controller.basecontrollers import (
    ComponentStateApplyMode,
    ImConWidgetControllerFactory,
)
from imswitch.imcontrol.controller.controllers.ScanControllerAdvanced import (
    ScanControllerAdvanced,
)
from imswitch.imcontrol.controller.controllers.ScanControllerExpert import (
    PROGRAM_FEATURE,
    ScanControllerExpert,
)
from imswitch.imcontrol.model import RecMode, SaveFormat, SaveMode
from imswitch.imcontrol.model.managers.detectors._mock_sample import MockSample
from imswitch.imcontrol.model.scan_program import presets, program as program_model
from imswitch.imcontrol.model.scan_program.conformance import (
    engine_log, expected_log, log_differences,
)
from imswitch.imcontrol.model.scan_program.serialize import program_to_dict
from imswitch.imcontrol.view import ViewSetupInfo
from imswitch.imcontrol.view.widgets.ScanWidgetAdvanced import ScanWidgetAdvanced
from imswitch.imcontrol.view.widgets.ScanWidgetExpert import ScanWidgetExpert
from imswitch.improcess.model.acquisition_layout_resolver import resolve_acquisition_layout
from imswitch.improcess.reconstructors.snouty.metadata import recorded_snouty_geometry

from .test_mock_scan_simulation_coordinator import _recorded_dataset, _wait_for
from .test_simple_point_scan_controller import SETUP, _Main

expert_widget_module = importlib.import_module(
    'imswitch.imcontrol.view.widgets.ScanWidgetExpert')

CAMERA = {'managerName': 'HamamatsuManager',
          'managerProperties': {'cameraListIndex': 'mock', 'hamamatsu': {
              'trigger_source': 2, 'subarray_hsize': 32, 'subarray_vsize': 32}},
          'digitalLine': 'Dev1/port0/line7', 'analogChannel': None, 'forAcquisition': True}


def _setup_dict(**scan):
    setup = json.load(open(SETUP))
    setup['scan'].update({'scanWidgetType': 'Expert', 'scanDesigner': None,
                          'TTLCycleDesigner': None, **scan})
    setup['detectors']['Camera'] = copy.deepcopy(CAMERA)
    return setup


class ExpertRig:
    def __init__(self, setup=None, widget=ScanWidgetExpert, controller=ScanControllerExpert,
                 lowLevel=None):
        self.setupDict = setup or _setup_dict()
        self.setup = ViewSetupInfo.from_json(json.dumps(self.setupDict))
        self.main = _Main()
        self.channel = CommunicationChannel(self.main, self.setup)
        self.master = MasterController(self.setup, self.channel, self.main._moduleCommChannel)
        self.factory = ImConWidgetControllerFactory(
            self.setup, self.master, self.channel, self.main._moduleCommChannel)
        self.widget = widget(optionsBasic)
        self.scan = self.factory.createController(controller, self.widget)
        self.main.controllers['Scan'] = self.scan
        self.ended, self.rejected = [], []
        self.channel.sigScanEnded.connect(lambda *a: self.ended.append(1))
        self.channel.sigScanRequestRejected.connect(self.rejected.append)

    @property
    def lasers(self):
        return list(self.setupDict['lasers'])

    def run(self, timeoutS=30.0):
        self.ended.clear()
        self.scan.runScanAdvanced()
        deadline = time.monotonic() + timeoutS
        while not self.ended and time.monotonic() < deadline:
            QtTest.QTest.qWait(50)
        return bool(self.ended)

    def close(self):
        try:
            self.factory.closeAllCreatedControllers(waitTimeoutS=5.0)
        finally:
            self.master.closeEvent()


@pytest.fixture
def rig(qtbot):
    rig = ExpertRig()
    yield rig
    rig.close()


def _ms_resolft(rig, **changes):
    lasers = rig.lasers
    params = dict(device='Z', cycles=2, planes=3, cycle_step_um=0.25, start_um=1.0,
                  pulses=[[lasers[0], 1e-3, 1e-3], [lasers[1], 2e-3, 1e-3],
                          [lasers[1], 1e-3, 0.0]],
                  camera='Camera', exposure_pulse=3, timepoints=2, pause_s=0.0,
                  move_s=5e-4, settle_s=5e-4, transition='jump')
    params.update(changes)
    return presets.make('ms-resolft', params)


# ---------------------------------------------------------------------------
# The panel
# ---------------------------------------------------------------------------

def test_the_panel_offers_what_the_model_accepts():
    for name in ('LOOP_KINDS', 'TRAVERSALS', 'PLACEMENTS', 'ORIGINS', 'RESTORES',
                 'EVENT_SHAPES', 'DURING', 'TIMEOUT_ACTIONS', 'WAIT_HOLDS', 'ASSEMBLIES',
                 'WINDOWS'):
        assert set(getattr(expert_widget_module, name)) == set(getattr(program_model, name)), name
    assert set(expert_widget_module.EVENT_KINDS) == set(program_model.EVENT_KINDS)
    from imswitch.imcontrol.model.scan_program.motion import TRANSITIONS
    assert set(expert_widget_module.TRANSITIONS) == set(TRANSITIONS)


def test_an_empty_panel_says_what_to_do(rig):
    assert 'no loops yet' in rig.widget.reportText()
    assert rig.widget.engineLabel.text() == 'Runs on nidaq (NidaqScanEngine).'


def test_a_preset_fills_the_tables_and_the_report(rig):
    rig.scan.setProgram(_ms_resolft(rig))
    assert [row['id'] for row in rig.widget.loops.rows()] == ['time', 'cycle', 'plane']
    drive = rig.widget.drives.rows()[0]
    assert (drive['device'], drive['w:cycle'], drive['w:plane']) == ('Z', 0.25, 0.5)
    report = rig.widget.reportText()
    assert report.startswith('Runnable on nidaq.')
    assert 'Timing policy strict-v1, 10 µs grid.' in report
    assert 'Duration 88 ms, 12 steps.' in report


def _close(a, b):
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= 1e-12 * max(abs(a), abs(b), 1.0)
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_close(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_close(x, y) for x, y in zip(a, b))
    return a == b


def test_the_tables_read_back_as_the_same_program(rig):
    program = _ms_resolft(rig)
    rig.scan.setProgram(program)
    rig.scan._onEdited()          # as if a cell had been touched
    rig.scan.getParameters()
    once = rig.scan.program()
    # The tables are the request now. The preset's 6 + 0.5 + 0.5 ms step is
    # 0.007000000000000001 s in binary; the table holds what it shows, 7 ms.
    assert _close(program_to_dict(once), dict(program_to_dict(program), requested_as=None))
    rig.scan.setProgram(once)
    rig.scan._onEdited()
    rig.scan.getParameters()
    assert rig.scan.program() == once        # and reading it again changes nothing


def test_an_edit_in_a_table_changes_the_program(rig, qtbot):
    rig.scan.setProgram(_ms_resolft(rig))
    rig.widget.loops.table.item(0, 2).setText('3')        # timepoints: 2 -> 3
    qtbot.waitUntil(lambda: 'Duration 132 ms' in rig.widget.reportText(), timeout=3000)
    assert rig.scan.getNumScanPositions() == 18


def test_a_cell_that_does_not_parse_is_named(rig, qtbot):
    rig.scan.setProgram(_ms_resolft(rig))
    rig.widget.loops.table.item(1, 2).setText('two')
    qtbot.waitUntil(lambda: 'Not runnable' in rig.widget.reportText(), timeout=3000)
    assert "Count in row 2: 'two' is not a int" in rig.widget.reportText()


def test_what_the_engine_cannot_run_is_reported_with_every_reason(rig):
    program = _ms_resolft(rig, start_um=80.0)   # past the Z scanner's volts
    rig.scan.setProgram(program)
    report = rig.widget.reportText()
    assert report.startswith('Not runnable on nidaq.') and 'Z' in report


def test_the_preset_form_reopens_with_its_values_and_refuses_what_is_missing(rig, monkeypatch):
    program = _ms_resolft(rig)
    rig.scan.setProgram(program)
    seen = {}

    def ask(preset, initial, validate):
        seen['initial'] = initial
        seen['missing'] = validate(dict(initial, cycles=None))
        return dict(initial, planes=4)

    monkeypatch.setattr(rig.widget, 'askPresetParams', ask)
    rig.scan.makeFromPreset('ms-resolft')
    assert seen['initial']['cycles'] == 2
    assert 'fill in Cycles' in seen['missing']
    assert rig.scan.program().loop('plane').count == 4


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def test_an_ms_resolft_recording_reads_back_at_its_positions(rig, qtbot, tmp_path):
    rig.scan.setProgram(_ms_resolft(rig))
    recording = rig.master.recordingManager
    memory = []
    recording.sigMemoryRecordingAvailable.connect(
        lambda name, file, path, saved: memory.append((name, file, path, saved)))
    layouts = rig.scan.getAcquisitionLayouts(['Camera'])
    recording.startRecording(
        detectorNames=['Camera'], recMode=RecMode.ScanOnce, savename=str(tmp_path / 'msr'),
        saveMode=SaveMode.RAM, saveFormat=SaveFormat.HDF5, attrs={'Camera': {}},
        recFrames=rig.scan.getNumScanPositions(), numCamTTL=rig.scan.getNumCamTTL(),
        stallTimeout=2.0, acquisitionLayouts=layouts)
    assert recording.waitForAcquisitionStarted(2.0)
    try:
        assert rig.run()
        assert _wait_for(lambda: len(memory) == 1, timeout=5.0)
        memFile, h5file, dataset = _recorded_dataset(memory, 'Camera')
        try:
            assert dataset.shape[0] == 12
            resolved = resolve_acquisition_layout(dict(dataset.attrs), shape=dataset.shape,
                                                  detector='Camera')
            assert resolved.is_usable and resolved.is_authoritative
            geometry = recorded_snouty_geometry(SimpleNamespace(acquisition_layout=resolved))
            assert geometry == (2, 3, 2)
            assert recorded_frame_coordinates(resolved.layout, 4) == \
                {'time': 0, 'cycle': 1, 'plane': 1}
        finally:
            h5file.close()
            memFile.close()
    finally:
        recording.endRecording(emitSignal=False, wait=True)


def _sampleUnder(rig, xs, ys):
    sample = MockSample.from_property(
        rig.setupDict['detectors']['APD']['managerProperties']['mockSample'])
    xx, yy = np.meshgrid(xs, ys)
    return sample.brightness(xx, yy)


def test_the_apd_images_a_raster_program_in_place(rig):
    raster = presets.make('raster', dict(
        x_device='X', y_device='Y', x_pixels=24, y_pixels=24, x_step_um=0.5, y_step_um=0.5,
        x_start_um=-6.0, y_start_um=-6.0, pulses=[[rig.lasers[1], 1e-3, 0.0]], camera='',
        exposure_pulse=1, move_s=1e-4, settle_s=1e-4, return_s=1e-3, traversal='serpentine'))
    rig.scan.setProgram(raster)
    assert rig.run()
    image = np.squeeze(np.array(rig.master.detectorsManager['APD']._image, float))
    assert image.shape == (24, 24)
    axis = -6.0 + 0.5 * np.arange(24)
    expected = _sampleUnder(rig, axis, axis)
    assert np.corrcoef(image.ravel(), expected.ravel())[0, 1] > 0.9


def test_a_program_point_detectors_cannot_assemble_is_refused_while_they_take_part(rig):
    rig.scan.setProgram(_ms_resolft(rig, pause_s=0.01))
    assert 'Point detectors cannot assemble it: The scan has wait intervals' \
        in rig.widget.reportText()
    rig.scan.runScanAdvanced()
    assert any('APD, APD 2 cannot assemble this scan' in str(r) and 'Deselect them' in str(r)
               for r in rig.rejected), rig.rejected
    assert not rig.scan.isRunning


# ---------------------------------------------------------------------------
# Saved states
# ---------------------------------------------------------------------------

def test_a_saved_program_loads_back(rig, tmp_path):
    rig.scan.setProgram(_ms_resolft(rig))
    path = tmp_path / 'expert.json'
    rig.scan.saveScanParamsToFile(str(path))
    saved = json.loads(path.read_text())
    assert saved['requiredFeatures'] == {PROGRAM_FEATURE: 'an Expert scan program (scan-program/1)'}
    assert saved['timingPolicy'] == 'strict-v1'
    rig.scan.setProgram(None)
    rig.scan.loadScanParamsFromFile(str(path))
    assert rig.scan.program() == _ms_resolft(rig)


def test_the_advanced_panel_refuses_an_expert_file_with_the_reason(qtbot, tmp_path):
    expert = ExpertRig()
    try:
        expert.scan.setProgram(_ms_resolft(expert))
        state = expert.scan.getComponentState()
    finally:
        expert.close()
    setup = _setup_dict(scanWidgetType='Advanced', scanDesigner='BetaScanDesigner',
                        TTLCycleDesigner='AdvancedScanTTLCycleDesigner',
                        scanDesignerParams={'return_time': 1e-3})
    advanced = ExpertRig(setup, ScanWidgetAdvanced, ScanControllerAdvanced)
    try:
        warnings = advanced.scan.applyComponentState(
            state, applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY)
        assert any('an Expert scan program' in w and 'cannot run' in w for w in warnings)
    finally:
        advanced.close()


def _advanced_state():
    setup = _setup_dict(scanWidgetType='Advanced', scanDesigner='BetaScanDesigner',
                        TTLCycleDesigner='AdvancedScanTTLCycleDesigner',
                        scanDesignerParams={'return_time': 1e-3, 'move_time': 1e-4,
                                            'settle_time': 1e-4})
    advanced = ExpertRig(setup, ScanWidgetAdvanced, ScanControllerAdvanced)
    try:
        advanced.scan.getParameters()
        analog = copy.deepcopy(advanced.scan._analogParameterDict)
        digital = copy.deepcopy(advanced.scan._digitalParameterDict)
        analog.update(axis_length=[3.0, 2.0, 0.0], axis_step_size=[0.5, 0.5, 1.0],
                      axis_centerpos=[0.0, 0.0, 0.0], sequence_time=1e-3)
        digital['sequence_time'] = 1e-3
        advanced.scan._scanParams.apply(advanced.widget, analog, digital,
                                        advanced.scan.positioners, advanced.scan.TTLDevices)
        state = advanced.scan.getComponentState()
        signals, _info = advanced.scan._buildScanSignals()
    finally:
        advanced.close()
    return setup, state, signals


def test_an_advanced_file_opens_as_a_program_that_plays_the_same(qtbot):
    setup, state, advancedSignals = _advanced_state()
    setup['scan']['scanWidgetType'] = 'Expert'
    rig = ExpertRig(setup)
    try:
        warnings = rig.scan.applyComponentState(
            state, applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY)
        assert warnings == []
        program = rig.scan.program()
        assert program.timing_policy == 'advanced-compatible-v1'
        signals, _info = rig.scan._buildScanSignals()
        for name, wave in advancedSignals['scanSignalsDict'].items():
            np.testing.assert_allclose(signals['scanSignalsDict'][name], wave, atol=1e-12,
                                       err_msg=name)
        ttl = advancedSignals['TTLCycleSignalsDict']
        assert sorted(signals['TTLCycleSignalsDict']) == sorted(ttl)
        for name, line in ttl.items():
            np.testing.assert_array_equal(signals['TTLCycleSignalsDict'][name],
                                          np.asarray(line, dtype=bool), err_msg=name)
        # Unchanged, it is saved so the Advanced panel can still open it.
        saved = rig.scan.getComponentState()
        assert 'requiredFeatures' not in saved
        assert saved['analogParameterDict'] == state['analogParameterDict']
        # Edited, it is an Expert program.
        rig.scan._onEdited()
        rig.widget.loops.table.item(0, 2).setText('5')
        rig.scan.getParameters()
        assert 'requiredFeatures' in rig.scan.getComponentState()
    finally:
        rig.close()


def test_a_file_for_another_setup_names_what_is_missing(rig):
    state = {'program': program_to_dict(_ms_resolft(rig, device='RO'))}
    warnings = rig.scan.applyComponentState(
        state, applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY)
    assert warnings == ['This setup has no RO. Scan state was not applied.']


# ---------------------------------------------------------------------------
# A step engine: the pulse generator runs the program itself
# ---------------------------------------------------------------------------

def test_a_pulse_generator_runs_a_pulse_program_from_the_panel(qtbot):
    setup = _setup_dict(engine='teensy')
    setup['teensyPulse'] = {'port': None, 'useMockOnFailure': True}
    setup['scanEngines'] = {'teensy': {'managerName': 'PulseGeneratorScanEngine',
                                       'managerProperties': {}}}
    setup['lasers']['Pulsed'] = {'managerName': 'PulseGeneratorLaserManager',
                                 'managerProperties': {}, 'digitalLine': 2,
                                 'wavelength': 405, 'valueRangeMin': 0, 'valueRangeMax': 1}
    rig = ExpertRig(setup)
    try:
        assert rig.widget.engineLabel.text() == 'Runs on teensy (PulseGeneratorScanEngine).'
        program = presets.make('pulse-sequence', dict(
            pulses=[['Pulsed', 1e-3, 2e-3]], camera='', exposure_pulse=1, frames=5,
            pause_s=0.0))
        rig.scan.setProgram(program)
        assert rig.widget.reportText().startswith('Runnable on teensy.')
        driver = rig.master.pulseGeneratorManager.driver
        since = driver._virtual_clock_ns
        assert rig.run(timeoutS=10.0)
        rises = [e for e in driver.timeline
                 if e.source == 'seq' and e.level and e.virtual_time_ns >= since]
        assert [e.channel for e in rises] == [2] * 5
        # a laser on the NI-DAQ cannot be switched by the Teensy
        nidaq = presets.make('pulse-sequence', dict(
            pulses=[[rig.lasers[0], 1e-3, 1e-3]], camera='', exposure_pulse=1, frames=2,
            pause_s=0.0))
        rig.scan.setProgram(nidaq)
        assert 'which teensy does not drive' in rig.widget.reportText()
    finally:
        rig.close()


# ---------------------------------------------------------------------------
# A step board behind the protocol (M5)
# ---------------------------------------------------------------------------

#: The simulated board on the protocol: a 10 µs grid, one DAC, four lines.
STEP_BOARD = {'managerName': 'StepProtocolScanEngine', 'managerProperties': {
    'port': 'simulated',
    'simulatedBoard': {'gridNs': 10_000, 'dacs': {'0': [0, 10]}, 'ttlLines': 4}}}


@pytest.fixture
def board_rig(qtbot):
    """Z, two lasers and the camera wired to the step board."""
    setup = _setup_dict(engine='board')
    setup['scanEngines'] = {'board': copy.deepcopy(STEP_BOARD)}
    setup['positioners']['Z']['analogChannel'] = 'board/dac0'
    for laser, line in zip(list(setup['lasers'])[:2], ('board/ttl0', 'board/ttl1')):
        setup['lasers'][laser]['digitalLine'] = line
    setup['detectors']['Camera']['digitalLine'] = 'board/ttl2'
    rig = ExpertRig(setup)
    yield rig
    rig.close()


def test_a_step_board_runs_an_ms_resolft_program_as_the_interpreter_means_it(board_rig):
    rig = board_rig
    engine = rig.master.scanEngine
    assert rig.widget.engineLabel.text() == 'Runs on board (StepProtocolScanEngine).'
    rig.scan.setProgram(_ms_resolft(rig))
    assert rig.widget.reportText().startswith('Runnable on board.'), rig.widget.reportText()
    assert rig.run(timeoutS=10.0) and rig.rejected == []
    expected = expected_log(rig.scan._scheduleOf(rig.scan._compile()))
    assert log_differences(engine_log(engine.log), expected) == []
    frames = [e for e in engine.log.entries if e[0] == 'on' and e[3] == 'Camera']
    assert len(frames) == 2 * 3 * 2           # cycles x planes x timepoints


def test_a_step_board_failure_ends_the_run_with_its_reason(board_rig, monkeypatch):
    rig = board_rig
    engine = rig.master.scanEngine
    monkeypatch.setattr(engine, 'run', functools.partial(engine.run, fail_at_step=3))
    rig.scan.setProgram(_ms_resolft(rig))
    assert rig.run(timeoutS=10.0)
    assert rig.scan._externalScanRequestFailureMessage == \
        'board: failed at step 3: injected failure at step 3'
    assert not rig.widget.scanButton.isChecked()


def test_the_report_refuses_what_the_board_does_not_have(board_rig):
    rig = board_rig
    # Y is still on the NI-DAQ: a raster over X and Y is not the board's
    params = dict(x_device='X', y_device='Y', x_pixels=3, y_pixels=2, x_step_um=1.0,
                  y_step_um=1.0, x_start_um=0.0, y_start_um=0.0,
                  pulses=[[rig.lasers[0], 1e-3, 0.0]], camera='', exposure_pulse=1,
                  move_s=5e-4, settle_s=5e-4, return_s=1e-3, traversal='forward')
    rig.scan.setProgram(presets.make('raster', params))
    report = rig.widget.reportText()
    assert 'X is wired to Dev1/ao0, which board does not drive' in report
    assert 'smooth transition' in report         # a board jumps


# ---------------------------------------------------------------------------
# Two engines in one program (M6): a leader and a follower, step by step
# ---------------------------------------------------------------------------

def test_the_panel_runs_one_program_on_a_leader_and_its_follower(qtbot):
    """A simulated step board leads (one laser, the pulse line); a protocol
    board, its trigger input wired to that line, follows with Z, the other
    laser and the camera. One run; the follower is stepped by the pulses."""
    setup = _setup_dict(engine='lead')
    setup['scanEngines'] = {
        'lead': {'managerName': 'SimulatedStepEngine', 'managerProperties': {
            'timingGridS': 1e-5, 'digitalLines': ['lead/ttl0', 'lead/ttl3'],
            'clockAccuracyPpm': 20.0}},
        'follow': {'managerName': 'StepProtocolScanEngine', 'triggerInput': 'lead/ttl3',
                   'managerProperties': {'port': 'simulated', 'simulatedBoard': {
                       'gridNs': 10_000, 'dacs': {'0': [0, 10]}, 'ttlLines': 2,
                       'triggerIn': True, 'triggerLatencyTicks': 2, 'triggerMinTicks': 1,
                       'clockPpm': 20.0}}}}
    setup['positioners']['Z']['analogChannel'] = 'follow/dac0'
    first, second = list(setup['lasers'])[:2]
    setup['lasers'][first]['digitalLine'] = 'lead/ttl0'
    setup['lasers'][second]['digitalLine'] = 'follow/ttl0'
    setup['detectors']['Camera']['digitalLine'] = 'follow/ttl1'
    rig = ExpertRig(setup)
    try:
        engine = rig.master.scanEngine
        assert rig.widget.engineLabel.text() == 'Runs on lead (CompositeScanEngine).'
        rig.scan.setProgram(_ms_resolft(rig, timepoints=1))
        report = rig.widget.reportText()
        assert report.startswith('Runnable on lead.'), report
        assert 'follow follows lead step by step' in report
        assert rig.run(timeoutS=10.0) and rig.rejected == []
        assert engine.partsOutcome() == {'lead': ('done', ''), 'follow': ('done', '')}
        follower, _ = engine.followers['follow']
        frames = [e for e in follower.log.entries if e[0] == 'on' and e[3] == 'Camera']
        assert len(frames) == 2 * 3                    # cycles x planes
        assert len(follower.log.of('release')) == 2 * 3
    finally:
        rig.close()


# ---------------------------------------------------------------------------
# Event-driven programs (M6): a detected event runs the panel's program
# ---------------------------------------------------------------------------

def test_an_etsnouty_event_runs_the_expert_program_and_the_run_carries_it(
        rig, monkeypatch, tmp_path):
    """EtSnouty with the Scan panel as its slow scan: the event runs the
    Expert program once through the scan source the channel resolves, the run
    carries the event while it lasts, and EtSnouty takes the run's end as its
    own."""
    etsnouty = importlib.import_module(
        'imswitch.imcontrol.controller.controllers.EtSnoutyController')
    from imswitch.imcontrol.model.EtSnoutyPaths import ETSNOUTY_ROOT_ENV
    from imswitch.imcontrol.view.widgets.EtSnoutyWidget import EtSnoutyWidget
    monkeypatch.setenv(ETSNOUTY_ROOT_ENV, str(tmp_path))
    monkeypatch.setattr(etsnouty, '_logsDir', str(tmp_path / 'logs'))
    (tmp_path / 'analysis_pipelines').mkdir()
    (tmp_path / 'analysis_pipelines' / 'detect_nothing.py').write_text(
        'def detect_nothing(img):\n    return [], None\n')
    widget = EtSnoutyWidget(optionsBasic)
    controller = rig.factory.createController(etsnouty.EtSnoutyController, widget)
    rig.main.controllers['EtSnouty'] = controller
    controller._refreshSlowScanSources()
    assert [widget.slowScanPar.itemText(i) for i in range(widget.slowScanPar.count())] == \
        [etsnouty.SLOW_SCAN_ROUTINE, 'Scan']
    widget.slowScanPar.setCurrentText('Scan')

    rig.scan.setProgram(_ms_resolft(rig, timepoints=1))
    controller.laserFast, controller.detectorFast = rig.lasers[0], 'Camera'
    controller._experimentActive = True
    rig.channel.sigScanStarting.connect(controller._onTriggeredScanStarting)
    rig.channel.sigScanEnded.connect(controller.scanEnded)
    during = []
    rig.channel.sigScanStarted.connect(
        lambda *a: during.append(rig.channel.sharedAttrs[etsnouty.SCAN_EVENT_KEY]))

    rig.ended.clear()
    assert controller.runSlowScan() is True
    deadline = time.monotonic() + 10.0
    while not rig.ended and time.monotonic() < deadline:
        QtTest.QTest.qWait(50)
    assert rig.ended and rig.rejected == []
    event = json.loads(during[0])
    assert event['workflow'] == 'EtSnouty' and event['scan_source'] == 'Scan'
    assert event['detector'] == 'Camera'
    assert rig.channel.sharedAttrs[etsnouty.SCAN_EVENT_KEY] == ''
    assert controller._triggeredScanInFlight is False
    assert list((tmp_path / 'logs').glob('*_log.txt'))       # its own bookkeeping ran


# ---------------------------------------------------------------------------
# Trajectories (M4): point detectors measure window by window, gridded
# ---------------------------------------------------------------------------

def _spiral(rig, **changes):
    params = dict(x_device='X', y_device='Y', center_um=[0.0, 0.0], radius_um=10.0, turns=40,
                  speed='angular', duration_s=1.0, window_s=2e-5, approach_s=5e-3,
                  pixel_um=0.25, laser=rig.lasers[1])
    params.update(changes)
    return presets.make('trajectory-spiral', params)


def _grid(rig):
    return rig.scan._scheduleOf(rig.scan._compile()).grid


def test_the_apd_images_the_sample_along_a_spiral(rig):
    rig.scan.setProgram(_spiral(rig))
    report = rig.widget.reportText()
    assert '50000 measurement windows of 20 µs' in report
    assert 'X: peak' in report and '(vel_max 0.1)' in report and '(acc_max 0.0001)' in report
    assert rig.run()
    apd = rig.master.detectorsManager['APD']
    image = np.squeeze(np.array(apd._image, float))
    grid = _grid(rig)
    assert apd._image.dtype == np.float32 and image.shape == grid.image_shape
    covered = np.isfinite(image)
    # The spiral's disc is measured, the grid's corners are not: NaN, not 0.
    assert covered.sum() > 0.7 * covered.size and not covered[0, 0] and not covered[-1, -1]
    state = apd._trajectoryState
    assert state['unit'] == 'counts/s'
    assert state['coverage'].sum() == pytest.approx(1.0)          # the time on the path
    assert np.array_equal(state['coverage'] > 0, covered)

    xs, ys = grid.axis_um(0), grid.axis_um(1)
    expected = _sampleUnder(rig, xs, ys)
    assert np.corrcoef(image[covered], expected[covered])[0, 1] > 0.9
    # ...and not merely any part of the sample: 3 µm off, it does not match.
    elsewhere = _sampleUnder(rig, xs + 3.0, ys)
    assert np.corrcoef(image[covered], elsewhere[covered])[0, 1] < 0.5


def _pmt_rig():
    setup = _setup_dict()
    setup['detectors']['PMT'] = {
        'managerName': 'PMTManager', 'analogChannel': None, 'digitalLine': None,
        'forAcquisition': True,
        'managerProperties': {'analogInputLine': 'Dev1/ai0', 'mockVoltageMean': 1.0,
                              'mockVoltageNoiseStd': 0.5, 'mockRandomSeed': 3}}
    return ExpertRig(setup)


def test_unequal_windows_give_the_pmt_the_same_voltage(qtbot):
    rig = _pmt_rig()
    try:
        means = []
        # 20 µs windows, and 70 µs ones that do not divide the 200 ms path:
        # its last window is shorter, and weighting by time keeps it right.
        for window in (2e-5, 7e-5):
            rig.scan.setProgram(_spiral(rig, duration_s=0.2, window_s=window, turns=10,
                                        pixel_um=0.5))
            assert rig.run()
            pmt = rig.master.detectorsManager['PMT']
            image = np.squeeze(np.array(pmt._image, float))
            coverage = pmt._trajectoryState['coverage']
            assert pmt._trajectoryState['unit'] == 'V'
            covered = coverage > 0
            means.append(float((image[covered] * coverage[covered]).sum() / coverage.sum()))
        # The mock's mean is 1 V with 0.5 V noise over 20000 samples: within
        # five standard errors of the mean.
        tolerance = 5 * 0.5 / np.sqrt(0.2 * 100_000)
        assert means[0] == pytest.approx(1.0, abs=tolerance)
        assert means[1] == pytest.approx(1.0, abs=tolerance)
    finally:
        rig.close()


@pytest.mark.parametrize('saveFormat', [SaveFormat.HDF5, SaveFormat.ZARR, SaveFormat.TIFF])
def test_a_trajectory_recording_holds_its_unit_and_coverage(rig, tmp_path, saveFormat):
    rig.scan.setProgram(_spiral(rig, duration_s=0.2, turns=10, pixel_um=0.5))
    recording = rig.master.recordingManager
    layouts = rig.scan.getAcquisitionLayouts(['APD'])
    assert [loop.kind for loop in layouts['APD'].event_loops] == ['scan_y', 'scan_x']
    savename = str(tmp_path / 'spiral')
    recording.startRecording(
        detectorNames=['APD'], recMode=RecMode.ScanOnce, savename=savename,
        saveMode=SaveMode.Disk, saveFormat=saveFormat, attrs={'APD': {}},
        recFrames=rig.scan.getNumScanPositions(), numCamTTL=rig.scan.getNumCamTTL(),
        stallTimeout=2.0, acquisitionLayouts=layouts)
    assert recording.waitForAcquisitionStarted(2.0)
    try:
        assert rig.run()
        _wait_for(lambda: not recording.record, timeout=10.0)
    finally:
        recording.endRecording(emitSignal=False, wait=True)
    image = np.squeeze(np.array(rig.master.detectorsManager['APD']._image, float))
    if saveFormat == SaveFormat.HDF5:
        import h5py
        path = next(p for p in tmp_path.rglob('*.hdf5'))
        with h5py.File(path, 'r') as f:
            group = f['APD']
            data = np.squeeze(group['data'][...])
            assert data.dtype == np.float32
            np.testing.assert_array_equal(np.isnan(data), np.isnan(image))
            assert group['data'].attrs['measurement:unit'] == 'counts/s'
            reduction = json.loads(group['data'].attrs['measurement:reduction'])
            coverage = group['coverage']
            unit, total = coverage.attrs['unit'], coverage[...].sum()
    elif saveFormat == SaveFormat.ZARR:
        import zarr
        path = next(p for p in tmp_path.rglob('*.zarr') if p.is_dir())
        root = zarr.open(str(path), mode='r')
        group = root['APD']
        assert group['data'].attrs['measurement:unit'] == 'counts/s'
        reduction = json.loads(group['data'].attrs['measurement:reduction'])
        unit, total = group['coverage'].attrs['unit'], np.asarray(group['coverage']).sum()
    else:
        import tifffile
        main = next(p for p in tmp_path.rglob('*.tif*') if '_coverage' not in p.name)
        sidecar = next(p for p in tmp_path.rglob('*_coverage.tif'))
        assert 'counts/s' in tifffile.TiffFile(str(main)).pages[0].description
        with tifffile.TiffFile(str(sidecar)) as tif:
            attrs = json.loads(tif.pages[0].description)
            unit, total = attrs['unit'], tif.asarray().sum()
            reduction = json.loads(attrs['reduction'])
    assert unit == 's' and total == pytest.approx(0.2)
    assert reduction['reduction'] == 'gridded from a trajectory'
    assert reduction['raw_measurements_stored'] is False
    assert reduction['weighting'] == 'Σ counts / Σ integration time'


_MEASUREMENT_ARRAYS = ('measurements', 'measurement_seconds', 'measurement_centre_um')


def _measurement_group(group):
    """A detector group's image, reduction, measurement arrays and the
    measurements' attributes (HDF5 or Zarr)."""
    arrays = {name: np.asarray(group[name][...]) for name in _MEASUREMENT_ARRAYS}
    return (np.asarray(group['data'][...]),
            json.loads(group['data'].attrs['measurement:reduction']), arrays,
            dict(group['measurements'].attrs))


def _read_measurement_recording(tmp_path, saveFormat):
    if saveFormat == SaveFormat.HDF5:
        import h5py
        with h5py.File(next(tmp_path.rglob('*.hdf5')), 'r') as f:
            return _measurement_group(f['APD'])
    if saveFormat == SaveFormat.ZARR:
        import zarr
        path = next(p for p in tmp_path.rglob('*.zarr') if p.is_dir())
        return _measurement_group(zarr.open(str(path), mode='r')['APD'])
    import tifffile
    sidecars = ('_coverage',) + tuple(f'_{name}' for name in _MEASUREMENT_ARRAYS)
    main = next(p for p in tmp_path.rglob('*.tif*') if not any(c in p.name for c in sidecars))
    arrays, attrs = {}, {}
    for name in _MEASUREMENT_ARRAYS:
        with tifffile.TiffFile(str(next(tmp_path.rglob(f'*_{name}.tif')))) as tif:
            arrays[name] = tif.asarray()
            if name == 'measurements':
                attrs = json.loads(tif.pages[0].description)
    with tifffile.TiffFile(str(next(tmp_path.rglob('*_coverage.tif')))) as tif:
        reduction = json.loads(json.loads(tif.pages[0].description)['reduction'])
    return tifffile.imread(str(main)), reduction, arrays, attrs


def _rebuilt_images(reduction, arrays):
    """Each recorded row of measurements, binned again onto the grid."""
    from imswitch.imcontrol.model.scan_program.trajectory import Grid, GridAccumulator
    grid = reduction['grid']
    grid = Grid(tuple(grid['devices']), tuple(grid['first_um']), float(grid['pixel_um']),
                tuple(grid['shape']))
    bins = grid.flat_index(arrays['measurement_centre_um'])
    images = []
    for row in np.atleast_2d(arrays['measurements']):
        accumulator = GridAccumulator(grid, 'counts')
        accumulator.add(bins, row, arrays['measurement_seconds'])
        images.append(accumulator.image().ravel())
    return np.array(images)


def _record_spiral(rig, tmp_path, saveFormat, **lapse):
    recording = rig.master.recordingManager
    recording.startRecording(
        detectorNames=['APD'], recMode=RecMode.ScanLapse if lapse else RecMode.ScanOnce,
        savename=str(tmp_path / 'spiral'), saveMode=SaveMode.Disk, saveFormat=saveFormat,
        attrs={'APD': {}}, recFrames=rig.scan.getNumScanPositions(),
        numCamTTL=rig.scan.getNumCamTTL(), stallTimeout=2.0,
        acquisitionLayouts=rig.scan.getAcquisitionLayouts(['APD']), **lapse)
    assert recording.waitForAcquisitionStarted(2.0)
    try:
        assert rig.run()
        _wait_for(lambda: not recording.record, timeout=10.0)
    finally:
        recording.endRecording(emitSignal=False, wait=True)


@pytest.mark.parametrize('saveFormat', [SaveFormat.HDF5, SaveFormat.ZARR, SaveFormat.TIFF])
def test_recorded_measurements_rebuild_the_image(rig, tmp_path, saveFormat):
    """M6: every window's measurement, recorded beside the gridded image, in
    acquisition order with its time and position -- what the image was
    reduced from, binned back to the same image."""
    rig.scan.setProgram(_spiral(rig, duration_s=0.2, turns=10, pixel_um=0.5,
                                record_measurements=True))
    _record_spiral(rig, tmp_path, saveFormat)
    data, reduction, arrays, attrs = _read_measurement_recording(tmp_path, saveFormat)
    windows = len(arrays['measurement_seconds'])
    assert arrays['measurements'].shape == (1, windows)
    assert not np.isnan(arrays['measurements']).any()
    assert attrs['unit'] == 'counts' and attrs['order'].startswith('acquisition order')
    assert arrays['measurement_seconds'].sum() == pytest.approx(0.2)
    assert arrays['measurement_centre_um'].shape == (windows, 2)
    assert reduction['raw_measurements_stored'] is True
    np.testing.assert_allclose(np.reshape(data, (1, -1)), _rebuilt_images(reduction, arrays),
                               rtol=1e-6, equal_nan=True)


def test_each_scan_of_a_lapse_keeps_its_own_measurements(rig, tmp_path):
    """Two scans in one file: each group's measurements rebuild that group's
    image. The two scans of a noisy sample differ, so measurements paired with
    the other scan's frame would not."""
    import h5py
    rig.scan.setProgram(_spiral(rig, duration_s=0.2, turns=10, pixel_um=0.5,
                                record_measurements=True))
    for index in range(2):
        _record_spiral(rig, tmp_path, SaveFormat.HDF5, singleLapseFile=True, recLapseTotal=2,
                       recLapseIndex=index)
    with h5py.File(next(tmp_path.rglob('*.hdf5')), 'r') as f:
        scans = [_measurement_group(f[f'scan{index}/APD']) for index in range(2)]
    for data, reduction, arrays, _ in scans:
        np.testing.assert_allclose(np.reshape(data, (1, -1)),
                                   _rebuilt_images(reduction, arrays), rtol=1e-6,
                                   equal_nan=True)
    assert not np.array_equal(scans[0][2]['measurements'], scans[1][2]['measurements'])


def test_measurements_are_not_recorded_unless_asked(rig, tmp_path):
    import h5py
    rig.scan.setProgram(_spiral(rig, duration_s=0.2, turns=10, pixel_um=0.5))
    _record_spiral(rig, tmp_path, SaveFormat.HDF5)
    with h5py.File(next(tmp_path.rglob('*.hdf5')), 'r') as f:
        assert 'coverage' in f['APD'] and not set(_MEASUREMENT_ARRAYS) & set(f['APD'])


def test_editing_the_tables_keeps_the_trajectory(rig, qtbot):
    rig.scan.setProgram(_spiral(rig, duration_s=0.2, turns=10, pixel_um=0.5))
    rig.widget.timeline.table.item(0, 3).setText('100')       # the laser off after 100 ms
    qtbot.waitUntil(lambda: rig.scan._edited is False or 'Runnable' in rig.widget.reportText(),
                    timeout=3000)
    rig.scan.getParameters()
    program = rig.scan.program()
    assert program.trajectory is not None and program.trajectory.pattern == 'spiral'
    assert program.timeline[0].end_s == pytest.approx(0.1)


def test_a_camera_takes_no_frames_on_a_trajectory(rig):
    rig.scan.setProgram(_spiral(rig, duration_s=0.2, turns=10, pixel_um=0.5))
    with pytest.raises(ValueError, match='Camera takes no frames on a trajectory'):
        rig.scan.getAcquisitionLayouts(['Camera'])


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
