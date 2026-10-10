"""The Expert scan panel on the simulated rig (docs/design/plans/
scan-engines-and-expert-scan.md §4, M3).

A real MasterController on ``galvo_apd_simple_mock_scan_setup.json`` (two
APDs imaging a synthetic sample from the scan waveforms, three lasers, a
simulated NI-DAQ) with a mock camera added, the real Expert controller and
panel. Scans really run; a camera recording goes through the recording
manager and is read back by the layout resolver and SNOUTY.
"""
import copy
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
