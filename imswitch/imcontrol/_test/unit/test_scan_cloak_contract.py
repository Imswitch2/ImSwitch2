"""The contract every scan cloak keeps (docs/simple-point-scan-plan.md §10.7).

Run for every entry of ``SCAN_CLOAKS``, each on its shipped mock setup with a
real MasterController and simulated NI-DAQ: the cloak's dock, its controller
and the backend's own widget, all real (no napari window). A new cloak adds
its mock setup and its two edits to the tables below and passes the same
tests:

* registration follows ``SCAN_CLOAKS`` (backend manager, frontend classes, config editor);
* the Advanced page holds the acquisition, before and after edits;
* switching there and back changes nothing; an edit there comes back; an
  edit the cloak cannot show is refused, or discarded on request;
* the switch waits for a running scan; the page is saved and restored;
* every way into the backend widget while the simple page shows -- script
  exports, shared attributes, loaded files -- ends in the plan;
* from the Advanced page, the backend runs and builds exactly as its own
  panel does.
"""
import copy
import dataclasses
import json
from importlib import import_module

import numpy as np
import pytest
from qtpy import QtTest

from imswitch import imcontrol
from imswitch.imcommon.controller import ModuleCommunicationChannel
from imswitch.imcontrol._test import optionsBasic
from imswitch.imcontrol.controller import controllers
from imswitch.imcontrol.controller.CommunicationChannel import CommunicationChannel
from imswitch.imcontrol.controller.MasterController import MasterController
from imswitch.imcontrol.controller.basecontrollers import (
    ComponentStateApplyMode,
    ImConWidgetControllerFactory,
)
from imswitch.imcontrol.controller.controllers._scan_cloak import ScanCloakController
from imswitch.imcontrol.model.scan_cloak import SCAN_CLOAKS, resolve_scan_frontend
from imswitch.imcontrol.model.scan_frame import FRAME_GEOMETRY_KEY
from imswitch.imcontrol.view import ViewSetupInfo, widgets
from imswitch.imcontrol.view.widgets.ScanCloakPanel import ScanCloakPanel

SETUPS = 'imswitch/_data/user_defaults/imcontrol_setups/'
SCAN_TEMPLATE = 'imswitch/imcontrol/view/configeditor/builtin_templates/sections/scan.json'

#: A shipped mock setup per cloak.
MOCK_SETUPS = {
    'SimplePointScan': SETUPS + 'galvo_apd_simple_mock_scan_setup.json',
    'SimpleMoNaLISA': SETUPS + 'simple_monalisa_mock_scan_setup.json',
}


def _pointScanFastAxis(rig):
    return rig.scan.acquisitionPlan().dims[0]


def _pointScanRepresentableEdit(rig):
    """Move the acquisition's fast axis by 1 µm, on the backend widget."""
    axis = _pointScanFastAxis(rig)
    centre = rig.scan.acquisitionPlan().regions[axis].center_um
    rig.panel.backend.setScanCenterPos(axis, centre + 1.0)
    return lambda before, after: (
        after.regions[axis].center_um == pytest.approx(before.regions[axis].center_um + 1.0))


def _pointScanUnrepresentableEdit(rig):
    """A timing window inside the pixel for the fired laser."""
    backend = rig.panel.backend
    laser = rig.scan.acquisitionPlan().channels[0][0]
    backend.setAdvancedTTLMode(True)
    backend.setPulseTimes(laser, 0, [1e-5], [2e-5])
    return 'timing windows'


def _moNaLISARepresentableEdit(rig):
    before = rig.scan.acquisitionPlan().lateral_step_um
    for axis in ('X', 'Y'):
        rig.panel.backend.setScanStepSize(rig.limits().axis_device(axis), before + 0.01)
    return lambda old, new: new.lateral_step_um == pytest.approx(old.lateral_step_um + 0.01)


def _moNaLISAUnrepresentableEdit(rig):
    # X and Y together: one lateral axis alone is refused first by the coupled
    # geometry ("X and Y must use the same scan length"), not by the period.
    period = rig.limits().ulens_period_um(rig.scan.acquisitionPlan().profile_id)
    for axis in ('X', 'Y'):
        rig.panel.backend.setScanSize(rig.limits().axis_device(axis), period + 0.1)
    return 'scan length must stay'


#: Per cloak: an Advanced-page edit it can show, and one it cannot.
EDITS = {
    'SimplePointScan': (_pointScanRepresentableEdit, _pointScanUnrepresentableEdit),
    'SimpleMoNaLISA': (_moNaLISARepresentableEdit, _moNaLISAUnrepresentableEdit),
}

CLOAKS = sorted(SCAN_CLOAKS)


def _pickProfile(rig, profile_id):
    """Choose a MoNaLISA profile as a user does (the combo reacts to
    ``activated``, not to a programmatic index change)."""
    index = rig.view.profileCombo.findData(profile_id)
    assert index >= 0, profile_id
    rig.view.profileCombo.setCurrentIndex(index)
    rig.view._profileChanged(index)


def _moNaLISAPrepareRun(rig):
    """A MoNaLISA acquisition needs the camera's live view running (the
    frames arrive through it) and the Settings controller, through which it
    switches the camera's trigger; returns what ends the live view."""
    rig.main.controllers['Settings'] = rig.factory.createController(
        _controllerClass('SettingsController'), widgets.SettingsWidget(optionsBasic))
    detectors = rig.master.detectorsManager
    handle = detectors.startAcquisition(liveView=True)
    return lambda: detectors.stopAcquisition(handle, liveView=True)


#: Per cloak: what a run needs before Start, or None.
PREPARE_RUN = {
    'SimplePointScan': None,
    'SimpleMoNaLISA': _moNaLISAPrepareRun,
}


def _controllerClass(name):
    # Through its module: once a sibling imported it, the lazy package
    # attribute of that name is the module (imports of the module rely on it).
    assert name in controllers._CONTROLLER_MODULES
    module = import_module(f'imswitch.imcontrol.controller.controllers.{name}')
    return getattr(module, name)


class _Main:
    def __init__(self):
        self.controllers = {}
        self._moduleCommChannel = ModuleCommunicationChannel()
        self._moduleCommChannel.register(imcontrol)


class CloakRig:
    def __init__(self, cloakType, setupText=None):
        self.type = cloakType
        spec = SCAN_CLOAKS[cloakType]
        assert len(spec.compatible_scan_widget_types) == 1
        self.backendType = spec.compatible_scan_widget_types[0]
        self.setup = ViewSetupInfo.from_json(setupText or open(MOCK_SETUPS[cloakType]).read())
        resolved = resolve_scan_frontend(self.setup.scan.scanWidgetType, self.setup.scan.cloak)
        assert resolved.frontend_type == cloakType
        assert resolved.backend_type == self.backendType
        self.main = _Main()
        self.channel = CommunicationChannel(self.main, self.setup)
        self.master = MasterController(self.setup, self.channel, self.main._moduleCommChannel)
        self.factory = ImConWidgetControllerFactory(
            self.setup, self.master, self.channel, self.main._moduleCommChannel
        )
        self.panel = getattr(widgets, f'ScanWidget{cloakType}')(optionsBasic)
        self.view = self.panel.view
        self.scan = self.factory.createController(
            _controllerClass(f'ScanController{cloakType}'), self.panel)
        self.main.controllers['Scan'] = self.scan
        self.events = []
        self.channel.sigScanEnded.connect(lambda *a: self.events.append('ended'))
        self.master.nidaqManager.sigScanDone.connect(lambda *a: self.events.append('iteration'))

    @property
    def cloak(self):
        return self.scan._cloak

    def limits(self):
        return self.scan.cloakLimits()

    def held(self):
        """The plan the backend widget holds under the current Simple interpretation."""
        return self.scan.parseBackendPlan(
            *self.scan._readBackendWidget(), referencePlan=self.scan.acquisitionPlan()
        )

    def acquisition(self):
        return self.cloak.normalize(self.scan.acquisitionPlan())

    def settle(self):
        QtTest.QTest.qWait(self.scan.mirrorDelayMs + 50)

    def waitForEnd(self, timeoutS=20.0):
        import time
        deadline = time.monotonic() + timeoutS
        while time.monotonic() < deadline and 'ended' not in self.events:
            QtTest.QTest.qWait(50)
        return 'ended' in self.events

    def close(self):
        try:
            self.factory.closeAllCreatedControllers(waitTimeoutS=5.0)
        finally:
            self.master.closeEvent()


@pytest.fixture(params=CLOAKS)
def rig(request, qtbot):
    rig = CloakRig(request.param)
    yield rig
    rig.close()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_every_cloak_has_a_mock_setup_and_edits():
    assert set(MOCK_SETUPS) == set(SCAN_CLOAKS)
    assert set(EDITS) == set(SCAN_CLOAKS)


def test_scan_widget_type_is_the_backend_and_cloak_is_the_frontend():
    resolved = resolve_scan_frontend('Advanced', 'SimplePointScan')
    assert resolved.backend_type == 'Advanced'
    assert resolved.frontend_type == 'SimplePointScan'
    assert resolved.cloak_type == 'SimplePointScan'
    assert resolved.cloak_spec.has_overview is True
    assert not resolved.legacy_cloak_config


def test_the_old_cloak_as_scan_widget_type_remains_readable():
    resolved = resolve_scan_frontend('SimplePointScan')
    assert resolved.backend_type == 'Advanced'
    assert resolved.frontend_type == 'SimplePointScan'
    assert resolved.legacy_cloak_config


def test_a_cloak_refuses_an_incompatible_backend():
    with pytest.raises(ValueError, match='cannot cloak'):
        resolve_scan_frontend('PointScan', 'SimplePointScan')


@pytest.mark.parametrize('cloak', CLOAKS)
def test_the_classes_and_the_config_editor_follow_the_table(cloak):
    spec = SCAN_CLOAKS[cloak]
    assert len(spec.compatible_scan_widget_types) == 1
    backend = spec.compatible_scan_widget_types[0]
    controller = _controllerClass(f'ScanController{cloak}')
    widget = getattr(widgets, f'ScanWidget{cloak}')
    assert issubclass(controller, ScanCloakController)
    assert issubclass(controller, _controllerClass(f'ScanController{backend}'))
    assert issubclass(widget, ScanCloakPanel)
    assert widget.backendWidgetClass is getattr(widgets, f'ScanWidget{backend}')

    fields = json.load(open(SCAN_TEMPLATE))['fields']
    backendOptions = next(field['opts'] for field in fields if field['key'] == 'scanWidgetType')
    cloakOptions = next(field['opts'] for field in fields if field['key'] == 'cloak')
    assert backend in backendOptions
    assert cloak not in backendOptions
    assert cloakOptions == ['__scan_cloaks__']


def test_the_scan_manager_is_the_backends(rig):
    assert type(rig.master.scanManager).__name__ == f'ScanManager{rig.backendType}'


# ---------------------------------------------------------------------------
# The Advanced page holds the acquisition
# ---------------------------------------------------------------------------

def test_the_advanced_page_holds_the_acquisition_from_the_start(rig):
    assert rig.scan.scanPage() == 'simple' and rig.panel.page() == 'simple'
    assert rig.held() == rig.acquisition()


def test_the_mirror_follows_edits_on_the_simple_page(rig):
    before = rig.acquisition()
    edit, _ = EDITS[rig.type]
    # Make the edit on the Advanced page, bring it back, then check the
    # mirror of a simple-page change: the page's plan goes back unchanged.
    rig.scan.setScanPage('advanced')
    edit(rig)
    assert rig.scan.setScanPage('simple')
    changed = rig.acquisition()
    assert changed != before
    rig.settle()
    assert rig.held() == changed


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------

def test_there_and_back_without_edits_changes_nothing(rig):
    stored = rig.scan._simple()['acquisition'] if hasattr(rig.scan, '_simple') else None
    before = rig.acquisition()
    assert rig.scan.setScanPage('advanced')
    assert rig.panel.page() == 'advanced' and rig.panel.advancedButton.isChecked()
    assert rig.scan.setScanPage('simple')
    assert rig.panel.page() == 'simple'
    assert rig.acquisition() == before
    if stored is not None:
        assert rig.scan._simple()['acquisition'] is stored     # not even re-adopted


def test_an_edit_on_the_advanced_page_comes_back(rig):
    before = rig.acquisition()
    rig.scan.setScanPage('advanced')
    edit, _ = EDITS[rig.type]
    check = edit(rig)
    # Nothing is taken over while the Advanced page shows.
    assert rig.acquisition() == before
    assert rig.scan.setScanPage('simple')
    assert check(before, rig.acquisition())


def test_an_edit_it_cannot_show_has_the_cloaks_declared_behavior(rig, monkeypatch):
    before = rig.acquisition()
    rig.scan.setScanPage('advanced')
    _, unrepresentable = EDITS[rig.type]
    reason = unrepresentable(rig)
    asked = []
    monkeypatch.setattr(rig.panel, 'confirmDiscardAdvanced',
                        lambda text: asked.append(text) or False)

    if rig.type == 'SimpleMoNaLISA':
        # Profiles are explicit bindings: Advanced may preserve or break one,
        # but Simple never guesses another profile.  A broken binding remains
        # visible as Advanced/custom and leaves the backend untouched.
        assert rig.scan.setScanPage('simple')
        assert rig.scan.scanPage() == 'simple' and rig.panel.page() == 'simple'
        assert not rig.scan.cloakOwnsBackend()
        assert not asked
        assert rig.view.profileCombo.currentData() is None
        assert rig.view.profileCombo.currentText() == 'Advanced / custom'
        assert reason in rig.scan.profileBinding().reason

        # Explicitly selecting/reapplying a profile re-establishes ownership
        # and overwrites the custom sequence/geometry with the Simple plan.
        profile_id = before.profile_id
        _pickProfile(rig, profile_id)
        rig.settle()
        assert rig.scan.cloakOwnsBackend()
        assert rig.acquisition().profile_id == profile_id
        assert rig.held() == rig.acquisition()
        return

    assert not rig.scan.setScanPage('simple')
    assert rig.scan.scanPage() == 'advanced' and rig.panel.page() == 'advanced'
    assert reason in asked[0]

    monkeypatch.setattr(rig.panel, 'confirmDiscardAdvanced', lambda text: True)
    assert rig.scan.setScanPage('simple')
    assert rig.panel.page() == 'simple'
    assert rig.acquisition() == before
    assert rig.held() == before


def test_discarding_can_be_asked_for_directly(rig):
    before = rig.acquisition()
    rig.scan.setScanPage('advanced')
    EDITS[rig.type][1](rig)
    assert rig.scan.setScanPage('simple', discardAdvanced=True)
    if rig.type == 'SimpleMoNaLISA':
        assert not rig.scan.cloakOwnsBackend()
        assert rig.acquisition() == before
    else:
        assert rig.acquisition() == before


def test_the_switch_waits_for_a_running_scan(rig):
    prepare = PREPARE_RUN[rig.type]
    finish = prepare(rig) if prepare else None
    rig.view.setLive(True)
    rig.view.startButton.click()
    try:
        QtTest.QTest.qWait(300)
        assert rig.view.isRunning()
        assert not rig.scan.setScanPage('advanced')
        assert rig.panel.page() == 'simple' and rig.panel.simpleButton.isChecked()
        assert 'Stop the scan' in rig.panel.noteLabel.text()
    finally:
        if rig.type == 'SimpleMoNaLISA':
            # MoNaLISA Stop is deliberately screening-light OFF only and is
            # disabled while Acquire owns the hardware. Abort explicitly in
            # this generic cloak lifecycle test.
            assert not rig.view.stopButton.isEnabled()
            rig.scan.abortScan()
        else:
            rig.view.stopButton.click()
        assert rig.waitForEnd()
        if finish:
            finish()


def test_private_extras_from_another_cloak_are_not_applied(rig):
    state = rig.scan.getComponentState()
    state['cloak']['type'] = 'SomeOtherCloak'
    state['cloak']['mode'] = 'not-a-point-scan-mode'
    assert rig.scan.cloakExtrasFromState(state) is None


def test_the_page_is_saved_and_restored(rig, qtbot):
    rig.scan.setScanPage('advanced')
    EDITS[rig.type][1](rig)                             # something only Advanced shows
    held = rig.scan._readBackendWidget()
    state = rig.scan.getComponentState()
    assert state['cloak']['page'] == 'advanced'
    assert state['cloak']['type'] == rig.type

    other = CloakRig(rig.type)
    try:
        warnings = other.scan.applyComponentState(
            state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
        assert not [w for w in warnings if 'not applied' in w]
        assert other.scan.scanPage() == 'advanced' and other.panel.page() == 'advanced'
        assert other.scan._readBackendWidget() == held
    finally:
        other.close()


# ---------------------------------------------------------------------------
# Every way into the backend widget, while the simple page shows
# ---------------------------------------------------------------------------

def _onlyCentreMoved(rig, axis, target):
    """The acquisition with ``axis`` centred at ``target`` and nothing else
    changed -- not, say, the overview's size taking its place."""
    before = rig.acquisition()
    region = dataclasses.replace(before.regions[axis], center_um=target)
    expected = rig.cloak.normalize(dataclasses.replace(
        before, regions={**before.regions, axis: region}))
    return lambda plan: plan == expected


def _export(rig):
    axis = _pointScanFastAxis(rig)
    centre = rig.scan.acquisitionPlan().regions[axis].center_um
    check = _onlyCentreMoved(rig, axis, centre + 1.5)
    rig.scan.changeScanCenterPos(axis, centre + 1.5)
    return check


def _sharedAttribute(rig):
    axis = _pointScanFastAxis(rig)
    analog, _ = rig.cloak.to_backend(rig.scan.acquisitionPlan(), rig.limits())
    centres = list(analog['axis_centerpos'])
    index = analog['target_device'].index(axis)
    centres[index] += 2.0
    check = _onlyCentreMoved(rig, axis, centres[index])
    rig.channel.sharedAttrs[('ScanStage', 'axis_centerpos')] = centres
    return check


def _loadedFile(rig, tmp_path):
    axis = _pointScanFastAxis(rig)
    state = rig.scan.getComponentState()
    state.pop('cloak', None)                           # a file the Advanced panel wrote
    index = state['analogParameterDict']['target_device'].index(axis)
    state['analogParameterDict']['axis_centerpos'][index] += 2.5
    check = _onlyCentreMoved(rig, axis, state['analogParameterDict']['axis_centerpos'][index])
    path = tmp_path / 'scan.json'
    path.write_text(json.dumps(state))
    rig.scan.loadScanParamsFromFile(str(path))
    return check


@pytest.mark.parametrize('write', [_export, _sharedAttribute, _loadedFile],
                         ids=['script-export', 'shared-attribute', 'loaded-file'])
def test_a_write_into_the_backend_widget_ends_in_the_plan(rig, write, tmp_path):
    if rig.type != 'SimplePointScan':
        pytest.skip('These write paths currently exercise PointScan centre-position semantics.')
    check = write(rig, tmp_path) if write is _loadedFile else write(rig)
    QtTest.QTest.qWait(50)                              # the deferred adoption
    assert rig.scan.scanPage() == 'simple'
    assert check(rig.acquisition())
    rig.settle()
    assert rig.held() == rig.acquisition()


def test_a_loaded_file_it_cannot_show_opens_the_advanced_page(rig, tmp_path):
    rig.scan.setScanPage('advanced')
    EDITS[rig.type][1](rig)
    state = rig.scan.getComponentState()
    state.pop('cloak')
    rig.scan.setScanPage('simple', discardAdvanced=True)
    path = tmp_path / 'advanced.json'
    path.write_text(json.dumps(state))

    rig.scan.loadScanParamsFromFile(str(path))

    assert rig.scan.scanPage() == 'advanced' and rig.panel.page() == 'advanced'
    assert 'Advanced page' in rig.panel.noteLabel.text()


# ---------------------------------------------------------------------------
# From the Advanced page, the backend is its own panel
# ---------------------------------------------------------------------------

def test_the_advanced_page_builds_what_the_backend_builds(rig):
    rig.scan.setScanPage('advanced')
    analog, digital = rig.scan._readBackendWidget()
    backend = _controllerClass(f'ScanController{rig.backendType}')

    ownSignals, ownInfo = backend._make_full_scan(
        rig.scan, copy.deepcopy(analog), copy.deepcopy(digital))
    signals, info = rig.scan._make_full_scan(copy.deepcopy(analog), copy.deepcopy(digital))

    assert FRAME_GEOMETRY_KEY not in info
    assert sorted(info) == sorted(ownInfo)
    for kind in ('scanSignalsDict', 'TTLCycleSignalsDict'):
        assert sorted(signals[kind]) == sorted(ownSignals[kind])
        for name in signals[kind]:
            np.testing.assert_array_equal(signals[kind][name], ownSignals[kind][name])


def test_a_design_made_on_one_page_is_not_run_from_the_other(rig):
    if rig.type != 'SimplePointScan':
        pytest.skip('Frame-geometry injection is a PointScan overview/viewer contract.')
    """The backend reuses its last design while its dicts stay the same, but
    the pages design the same dicts differently (the frame geometry here).
    The pages hand over differently shaped dicts today; held to the same
    dicts, a design still does not cross pages."""
    rig.scan.getParameters()
    dicts = copy.deepcopy((rig.scan._analogParameterDict, rig.scan._digitalParameterDict))

    def sameDicts():
        rig.scan._analogParameterDict, rig.scan._digitalParameterDict = copy.deepcopy(dicts)

    rig.scan.getParameters = sameDicts
    _, info = rig.scan._buildScanSignals()
    assert FRAME_GEOMETRY_KEY in info

    rig.scan.setScanPage('advanced')
    _, info = rig.scan._buildScanSignals()
    assert FRAME_GEOMETRY_KEY not in info

    rig.scan.setScanPage('simple')
    _, info = rig.scan._buildScanSignals()
    assert FRAME_GEOMETRY_KEY in info


def test_the_advanced_page_runs_with_its_own_repeat_box(rig):
    rig.scan.setScanPage('advanced')
    backend = rig.panel.backend
    backend.setRepeatEnabled(False)
    backend.scanButton.click()
    assert rig.waitForEnd()
    assert rig.events.count('iteration') == 1
    # ...and both pages showed the run.
    assert not rig.view.isRunning() and backend.scanButton.isEnabled()


def test_a_run_shows_on_the_simple_page_whichever_page_started_it(rig):
    rig.scan.setScanPage('advanced')
    backend = rig.panel.backend
    backend.setRepeatEnabled(True)
    backend.scanButton.click()
    try:
        QtTest.QTest.qWait(300)
        assert rig.view.isRunning()
    finally:
        backend.setRepeatEnabled(False)
        assert rig.waitForEnd()
    assert not rig.view.isRunning()


def test_blocking_the_scan_panel_blocks_both_pages(rig):
    rig.channel.sigToggleBlockScanWidget.emit(False)
    assert not rig.panel.isEnabled()
    rig.channel.sigToggleBlockScanWidget.emit(True)
    assert rig.panel.isEnabled()


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




def _monalisaSetupWithPowerMode(mode):
    data = json.load(open(MOCK_SETUPS['SimpleMoNaLISA']))
    data.setdefault('deviceCategorySettings', {}).setdefault('lasers', {})[
        'powerMode'
    ] = mode
    return json.dumps(data)


def _monalisaSetupWithThreeProfiles():
    """The mock setup with the three-profile set the model tests use (green,
    dual OFF, dual colour), on its own laser names plus two more lasers."""
    data = json.load(open(MOCK_SETUPS['SimpleMoNaLISA']))
    template = copy.deepcopy(data['lasers']['488 (OFF)'])
    for name, line, wavelength in (('488 (OFF) 2', 'Dev1/port0/line6', 488),
                                   ('590', 'Dev1/port0/line7', 590)):
        laser = copy.deepcopy(template)
        laser['digitalLine'] = line
        laser['wavelength'] = wavelength
        data['lasers'][name] = laser
    on, off, off2, exc, red, cam = '405 (ON)', '488 (OFF)', '488 (OFF) 2', '488 (EXC)', '590', 'Camera'
    readout = {'id': 'readout', 'label': 'Readout', 'role': 'readout', 'ttlDevices': [exc, cam],
               'defaultDurationMs': 1, 'durationEditable': False}
    simple = dict(data['scan'].get('simpleMoNaLISA') or {})
    simple['defaultProfile'] = 'green'
    simple['profiles'] = {
        'green': {
            'label': 'Green', 'defaultMode': 'resolft',
            'steps': [
                {'id': 'on', 'label': 'ON', 'role': 'on', 'ttlDevices': [on], 'defaultDurationMs': 1},
                {'id': 'off', 'label': 'OFF', 'role': 'off', 'ttlDevices': [off], 'defaultDurationMs': 2},
                readout,
            ],
            'modes': {
                'resolft': {'label': 'RESOLFT', 'enabledSteps': ['on', 'off', 'readout']},
                'confocal': {'label': 'Confocal', 'enabledSteps': ['on', 'readout']},
            },
        },
        'dual_off': {
            'label': 'Dual OFF', 'defaultMode': 'resolft',
            'steps': [
                {'id': 'on', 'role': 'on', 'ttlDevices': [on], 'defaultDurationMs': 1},
                {'id': 'off', 'role': 'off', 'ttlDevices': [off, off2], 'defaultDurationMs': 2},
                readout,
            ],
            'modes': {'resolft': {'enabledSteps': ['on', 'off', 'readout']}},
        },
        'dual_color': {
            'label': 'Dual colour', 'defaultMode': 'resolft',
            'steps': [
                {'id': 'on', 'role': 'on', 'ttlDevices': [on], 'defaultDurationMs': 1},
                {'id': 'green_off', 'role': 'off', 'ttlDevices': [off], 'defaultDurationMs': 2},
                {'id': 'green_ro', 'role': 'readout', 'ttlDevices': [exc, cam],
                 'defaultDurationMs': 1, 'durationEditable': False},
                {'id': 'red_off', 'role': 'off', 'ttlDevices': [red], 'defaultDurationMs': 2.5},
                {'id': 'red_ro', 'role': 'readout', 'ttlDevices': [red, cam],
                 'defaultDurationMs': 1, 'durationEditable': False},
            ],
            'modes': {
                'resolft': {'label': 'RESOLFT',
                            'enabledSteps': ['on', 'green_off', 'green_ro', 'red_off', 'red_ro']},
                'confocal': {'label': 'Confocal', 'enabledSteps': ['on', 'green_ro', 'red_ro']},
                'green': {'label': 'Green only', 'enabledSteps': ['on', 'green_off', 'green_ro']},
            },
        },
    }
    data['scan']['simpleMoNaLISA'] = simple
    return json.dumps(data)


def _monalisaSetupWithoutProfiles():
    data = json.load(open(MOCK_SETUPS['SimpleMoNaLISA']))
    simple = dict(data['scan'].get('simpleMoNaLISA') or {})
    simple['profiles'] = {}
    simple.pop('defaultProfile', None)
    data['scan']['simpleMoNaLISA'] = simple
    data.setdefault('deviceCategorySettings', {}).setdefault('lasers', {})[
        'powerMode'
    ] = 'separated'
    return json.dumps(data)


def _monalisaSetupWithCustomAlignmentProfile():
    data = json.load(open(MOCK_SETUPS['SimpleMoNaLISA']))
    simple = dict(data['scan'].get('simpleMoNaLISA') or {})
    simple['defaultProfile'] = 'align'
    simple['profiles'] = {
        'align': {
            'label': 'Align',
            'defaultMode': 'exc',
            'scanExtentSource': 'ulens',
            'steps': [
                {
                    'id': 'align',
                    'label': 'Align',
                    'role': 'custom',
                    'ttlDevices': [
                        '405 (ON)',
                        '488 (EXC)',
                        '488 (OFF)',
                        'Camera',
                    ],
                    'defaultDurationMs': 10.0,
                }
            ],
            'modes': {
                'exc': {
                    'label': 'Exc',
                    'enabledSteps': ['align'],
                    'enabledDevices': {
                        'align': ['488 (EXC)', 'Camera'],
                    },
                },
                'off': {
                    'label': 'OFF',
                    'enabledSteps': ['align'],
                    'enabledDevices': {
                        'align': ['488 (OFF)', 'Camera'],
                    },
                },
            },
        }
    }
    data['scan']['simpleMoNaLISA'] = simple
    data.setdefault('deviceCategorySettings', {}).setdefault('lasers', {})[
        'powerMode'
    ] = 'separated'
    return json.dumps(data)


def test_monalisa_without_profiles_starts_restricted_and_does_not_adopt_advanced(qtbot):
    rig = CloakRig('SimpleMoNaLISA', _monalisaSetupWithoutProfiles())
    try:
        assert rig.scan.acquisitionPlan() is None
        assert not rig.scan.cloakOwnsBackend()
        assert rig.view.profileCombo.currentData() is None
        assert rig.view.profileCombo.currentText() == 'No profile configured'
        assert not rig.view.profileCombo.isEnabled()
        assert rig.view.newProfileAction.isEnabled()
        assert not rig.view.editProfileAction.isEnabled()
        assert not rig.view.deleteProfileAction.isEnabled()
        assert not rig.view.startButton.isEnabled()
        assert not rig.view.screeningButton.isEnabled()
        assert 'Create a profile' in rig.view.bindingLabel.text()

        assert rig.scan.setScanPage('advanced')
        assert rig.scan.setScanPage('simple')
        assert rig.scan.acquisitionPlan() is None
        assert not rig.scan.cloakOwnsBackend()
    finally:
        rig.close()


def test_monalisa_live_is_disabled_when_live_and_scan_power_are_coupled(qtbot):
    rig = CloakRig('SimpleMoNaLISA')
    try:
        assert not rig.view.screeningButton.isEnabled()
        assert 'separated Live/Scan laser powers' in rig.view.screeningButton.toolTip()
    finally:
        rig.close()


def test_monalisa_legacy_live_sources_are_derived_and_default_to_on_readout(qtbot):
    rig = CloakRig(
        'SimpleMoNaLISA', _monalisaSetupWithPowerMode('separated')
    )
    try:
        assert rig.view.screeningButton.isEnabled()
        assert rig.view.selectedLiveSources() == {'405 (ON)', '488 (EXC)'}
        assert set(rig.view._liveSourceChecks) == {
            '405 (ON)', '488 (OFF)', '488 (EXC)'
        }
        assert not rig.view._liveSourceChecks['488 (OFF)'].isChecked()
    finally:
        rig.close()


def test_monalisa_custom_profile_seeds_global_live_source_choices(qtbot):
    rig = CloakRig(
        'SimpleMoNaLISA', _monalisaSetupWithCustomAlignmentProfile()
    )
    try:
        assert rig.view.screeningButton.isEnabled()
        expected = {
            '405 (ON)',
            '488 (EXC)',
            '488 (OFF)',
        }
        assert set(rig.view._liveSourceChecks) == expected
        assert {check.text() for check in rig.view._liveSourceChecks.values()} == {
            '405 (ON)', '488 (EXC)', '488 (OFF)'
        }
        assert all(
            'Camera' not in check.text()
            for check in rig.view._liveSourceChecks.values()
        )

        # The current mode only enables 488 (EXC), but the legacy migration
        # seeds all laser sources declared by the profile, independently of
        # the active mode.
        assert rig.acquisition().mode_id == 'exc'
        assert set(rig.scan._availableLiveSources()) == expected
    finally:
        rig.close()


def test_monalisa_explicit_global_live_sources_override_profile_sources(qtbot):
    data = json.loads(_monalisaSetupWithCustomAlignmentProfile())
    data['scan']['simpleMoNaLISA']['liveSources'] = ['488 (OFF)']
    rig = CloakRig('SimpleMoNaLISA', json.dumps(data))
    try:
        assert rig.scan._availableLiveSources() == ('488 (OFF)',)
        assert set(rig.view._liveSourceChecks) == {'488 (OFF)'}
        rig.scan._setSelectedLiveSources({'488 (OFF)'})
        rig.scan._refreshLiveControls()
        assert rig.view.selectedLiveSources() == {'488 (OFF)'}

        # Changing acquisition mode must not repopulate Live sources from the
        # profile or reset the current Live selection now that the setup-level
        # list exists.
        mode_index = rig.view.modeCombo.findData('off')
        rig.view.modeCombo.setCurrentIndex(mode_index)
        rig.settle()
        assert rig.scan._availableLiveSources() == ('488 (OFF)',)
        assert set(rig.view._liveSourceChecks) == {'488 (OFF)'}
        assert rig.view.selectedLiveSources() == {'488 (OFF)'}
    finally:
        rig.close()


def test_monalisa_live_sources_migrate_to_setup_level_on_next_save(qtbot):
    rig = CloakRig(
        'SimpleMoNaLISA', _monalisaSetupWithPowerMode('separated')
    )
    try:
        raw = copy.deepcopy(rig.setup.scan.simpleMoNaLISA)
        assert 'liveSources' not in raw
        legacy = rig.scan._legacyLiveSourcesFromConfig(raw)

        migrated = rig.scan._normalizeLiveSourcesForSave(raw)
        assert migrated['liveSources'] == list(legacy)
    finally:
        rig.close()


def test_monalisa_transitional_profile_live_sources_are_read_and_migrated(qtbot):
    rig = CloakRig(
        'SimpleMoNaLISA', _monalisaSetupWithPowerMode('separated')
    )
    try:
        source = rig.scan._configuredLaserNames()[0]
        raw = {
            'profiles': {
                'legacy': {
                    'liveSources': [source],
                    'steps': [],
                }
            }
        }
        assert rig.scan._legacyLiveSourcesFromConfig(raw) == (source,)
        migrated = rig.scan._normalizeLiveSourcesForSave(raw)
        assert migrated['liveSources'] == [source]
        assert 'liveSources' not in migrated['profiles']['legacy']
    finally:
        rig.close()
def test_monalisa_profiles_and_modes_are_explicit_simple_choices(qtbot):
    rig = CloakRig('SimpleMoNaLISA', _monalisaSetupWithThreeProfiles())
    try:
        assert [rig.view.profileCombo.itemData(i) for i in range(rig.view.profileCombo.count())] == [
            'green', 'dual_off', 'dual_color'
        ]
        before = rig.acquisition()
        _pickProfile(rig, 'dual_color')
        rig.settle()
        selected = rig.acquisition()
        assert selected.profile_id == 'dual_color'
        assert selected.mode_id == 'resolft'
        assert selected.scan_type == before.scan_type
        assert rig.held() == selected

        mode_index = rig.view.modeCombo.findData('confocal')
        rig.view.modeCombo.setCurrentIndex(mode_index)
        rig.settle()
        assert rig.acquisition().profile_id == 'dual_color'
        assert rig.acquisition().mode_id == 'confocal'
        assert rig.held() == rig.acquisition()
    finally:
        rig.close()


def test_monalisa_deleting_active_profile_refreshes_combo_and_plan(qtbot, monkeypatch):
    rig = CloakRig('SimpleMoNaLISA', _monalisaSetupWithThreeProfiles())
    try:
        # Avoid touching the user's setup file; the controller still updates
        # its in-memory profile store exactly as after a successful save.
        monkeypatch.setattr(rig.scan, '_persistSimpleConfig', lambda raw: None)
        monkeypatch.setattr(rig.view, 'confirmProfileDelete', lambda label: True)

        rig.scan._onProfileRequested('dual_color')
        assert rig.acquisition().profile_id == 'dual_color'

        rig.scan._onProfileDeleteRequested('dual_color')

        profile_ids = [
            rig.view.profileCombo.itemData(i)
            for i in range(rig.view.profileCombo.count())
        ]
        replacement_id = rig.limits().profile_store.default_profile
        assert 'dual_color' not in profile_ids
        assert rig.view.profileCombo.currentData() == replacement_id
        assert rig.acquisition().profile_id == replacement_id
        assert rig.view._renderedProfileId == replacement_id
    finally:
        rig.close()


def test_monalisa_mode_change_republishes_scan_laser_participation(qtbot):
    rig = CloakRig(
        'SimpleMoNaLISA', _monalisaSetupWithCustomAlignmentProfile()
    )
    try:
        rig.settle()
        state = rig.channel.scanWorkflow.laser_scan_state()
        assert state['488 (EXC)'].active_for_scan is True
        assert state['488 (OFF)'].active_for_scan is False

        mode_index = rig.view.modeCombo.findData('off')
        rig.view.modeCombo.setCurrentIndex(mode_index)
        rig.settle()

        state = rig.channel.scanWorkflow.laser_scan_state()
        assert state['488 (EXC)'].active_for_scan is False
        assert state['488 (OFF)'].active_for_scan is True
    finally:
        rig.close()


# ---------------------------------------------------------------------------
# Simple MoNaLISA save/load provenance
# ---------------------------------------------------------------------------

def _monalisaSetupWithFixedULensPeriod(period_um):
    data = json.load(open(MOCK_SETUPS['SimpleMoNaLISA']))
    simple = dict(data['scan'].get('simpleMoNaLISA') or {})
    simple['ulensPeriodPolicy'] = 'fixed'
    simple['ulensPeriodUm'] = float(period_um)
    data['scan']['simpleMoNaLISA'] = simple
    return json.dumps(data)


def _monalisaSetupWithModifiedProfile(profile_id):
    data = json.load(open(MOCK_SETUPS['SimpleMoNaLISA']))
    simple = dict(data['scan'].get('simpleMoNaLISA') or {})
    profiles = copy.deepcopy(simple.get('profiles') or {})
    profile = dict(profiles[profile_id])
    profile['label'] = str(profile.get('label') or profile_id) + ' changed'
    profiles[profile_id] = profile
    simple['profiles'] = profiles
    data['scan']['simpleMoNaLISA'] = simple
    return json.dumps(data)


def test_monalisa_simple_save_records_binding_and_acquisition_context(qtbot):
    rig = CloakRig('SimpleMoNaLISA')
    try:
        state = rig.scan.getComponentState()
        metadata = state['cloak']['monalisa']
        plan = rig.scan.acquisitionPlan()
        profile = rig.limits().profile_store.profile(plan.profile_id)
        ulens = rig.limits().resolve_ulens_period(plan.profile_id)

        assert metadata['schemaVersion'] == rig.scan.savedMoNaLISASchemaVersion
        assert metadata['profileId'] == plan.profile_id
        assert metadata['modeId'] == plan.mode_id
        assert metadata['profileSnapshot'] == profile.to_config()
        assert metadata['profileFingerprint'] == rig.scan.profileBinding().profile_fingerprint
        assert metadata['acquisitionContext']['ulens'] == {
            'effectivePeriodUm': ulens.value_um,
            'sourceKind': ulens.source_kind,
            'sourceId': ulens.source_id,
        }
        assert metadata['acquisitionContext']['sampleRateHz'] == rig.limits().sample_rate
    finally:
        rig.close()


def test_monalisa_advanced_save_does_not_claim_simple_binding(qtbot):
    rig = CloakRig('SimpleMoNaLISA')
    try:
        assert rig.scan.setScanPage('advanced')
        state = rig.scan.getComponentState()
        assert state['cloak']['page'] == 'advanced'
        assert 'monalisa' not in state['cloak']
    finally:
        rig.close()


def test_monalisa_exact_simple_state_load_rebinds_without_changing_backend(qtbot):
    source = CloakRig('SimpleMoNaLISA')
    try:
        state = source.scan.getComponentState()
        expected_backend = (
            copy.deepcopy(state['analogParameterDict']),
            copy.deepcopy(state['digitalParameterDict']),
        )
        saved_profile = state['cloak']['monalisa']['profileId']
        saved_mode = state['cloak']['monalisa']['modeId']

        target = CloakRig('SimpleMoNaLISA')
        try:
            warnings = target.scan.applyComponentState(
                copy.deepcopy(state),
                applyMode=ComponentStateApplyMode.STARTUP_RESTORE,
            )
            assert not warnings
            assert target.scan.scanPage() == 'simple'
            assert target.scan.cloakOwnsBackend()
            assert target.scan.acquisitionPlan().profile_id == saved_profile
            assert target.scan.acquisitionPlan().mode_id == saved_mode
            assert target.scan._readBackendWidget() == expected_backend
        finally:
            target.close()
    finally:
        source.close()


def test_monalisa_load_without_binding_metadata_stays_advanced_and_unbound(qtbot):
    source = CloakRig('SimpleMoNaLISA')
    try:
        state = source.scan.getComponentState()
        state['cloak'].pop('monalisa')
        expected_backend = (
            copy.deepcopy(state['analogParameterDict']),
            copy.deepcopy(state['digitalParameterDict']),
        )

        target = CloakRig('SimpleMoNaLISA')
        try:
            warnings = target.scan.applyComponentState(
                copy.deepcopy(state),
                applyMode=ComponentStateApplyMode.STARTUP_RESTORE,
            )
            assert target.scan.scanPage() == 'advanced'
            assert target.scan.acquisitionPlan() is None
            assert not target.scan.cloakOwnsBackend()
            assert target.scan._readBackendWidget() == expected_backend
            assert 'No Simple MoNaLISA binding metadata' in target.panel.loadResolutionLabel.text()
            assert not target.panel.loadResolutionFrame.isHidden()
            assert set(target.panel.loadResolutionButtons) == {'keep_advanced'}
            assert any('No Simple MoNaLISA binding metadata' in warning for warning in warnings)
        finally:
            target.close()
    finally:
        source.close()


def test_monalisa_ulens_change_keeps_saved_scan_advanced(qtbot):
    source = CloakRig('SimpleMoNaLISA')
    try:
        state = source.scan.getComponentState()
        saved_period = state['cloak']['monalisa']['acquisitionContext']['ulens'][
            'effectivePeriodUm'
        ]
        expected_backend = (
            copy.deepcopy(state['analogParameterDict']),
            copy.deepcopy(state['digitalParameterDict']),
        )
        target = CloakRig(
            'SimpleMoNaLISA',
            _monalisaSetupWithFixedULensPeriod(float(saved_period) + 0.1),
        )
        try:
            warnings = target.scan.applyComponentState(
                copy.deepcopy(state),
                applyMode=ComponentStateApplyMode.STARTUP_RESTORE,
            )
            assert target.scan.scanPage() == 'advanced'
            assert target.scan.acquisitionPlan() is None
            assert target.scan._readBackendWidget() == expected_backend
            assert 'uLens' in target.panel.loadResolutionLabel.text()
            assert set(target.panel.loadResolutionButtons) == {
                'use_current_ulens', 'keep_advanced'
            }
            assert any('uLens' in warning for warning in warnings)
        finally:
            target.close()
    finally:
        source.close()


def test_monalisa_profile_change_keeps_saved_scan_advanced(qtbot):
    source = CloakRig('SimpleMoNaLISA')
    try:
        state = source.scan.getComponentState()
        profile_id = state['cloak']['monalisa']['profileId']
        expected_backend = (
            copy.deepcopy(state['analogParameterDict']),
            copy.deepcopy(state['digitalParameterDict']),
        )
        target = CloakRig(
            'SimpleMoNaLISA',
            _monalisaSetupWithModifiedProfile(profile_id),
        )
        try:
            warnings = target.scan.applyComponentState(
                copy.deepcopy(state),
                applyMode=ComponentStateApplyMode.STARTUP_RESTORE,
            )
            assert target.scan.scanPage() == 'advanced'
            assert target.scan.acquisitionPlan() is None
            assert target.scan._readBackendWidget() == expected_backend
            assert 'has changed since this scan was saved' in target.panel.loadResolutionLabel.text()
            assert set(target.panel.loadResolutionButtons) == {
                'use_current_profile', 'import_saved_profile', 'keep_advanced'
            }
            assert any('has changed since this scan was saved' in warning for warning in warnings)
        finally:
            target.close()
    finally:
        source.close()


def test_monalisa_ulens_load_action_explicitly_adapts_to_current_setup(qtbot):
    source = CloakRig('SimpleMoNaLISA')
    try:
        state = source.scan.getComponentState()
        saved_period = state['cloak']['monalisa']['acquisitionContext']['ulens'][
            'effectivePeriodUm'
        ]
        saved_backend = (
            copy.deepcopy(state['analogParameterDict']),
            copy.deepcopy(state['digitalParameterDict']),
        )
        current_period = float(saved_period) + 0.1
        target = CloakRig(
            'SimpleMoNaLISA', _monalisaSetupWithFixedULensPeriod(current_period)
        )
        try:
            target.scan.applyComponentState(
                copy.deepcopy(state),
                applyMode=ComponentStateApplyMode.STARTUP_RESTORE,
            )
            target.panel.loadResolutionButtons['use_current_ulens'].click()

            assert target.scan.scanPage() == 'simple'
            assert target.scan.cloakOwnsBackend()
            assert target.scan.acquisitionPlan().lateral_length_um == pytest.approx(
                current_period
            )
            assert target.scan._readBackendWidget() != saved_backend
            assert target.panel.loadResolutionFrame.isHidden()
        finally:
            target.close()
    finally:
        source.close()


def test_monalisa_changed_profile_can_explicitly_use_current_definition(qtbot):
    source = CloakRig('SimpleMoNaLISA')
    try:
        state = source.scan.getComponentState()
        profile_id = state['cloak']['monalisa']['profileId']
        saved_backend = (
            copy.deepcopy(state['analogParameterDict']),
            copy.deepcopy(state['digitalParameterDict']),
        )
        target = CloakRig(
            'SimpleMoNaLISA', _monalisaSetupWithModifiedProfile(profile_id)
        )
        try:
            target.scan.applyComponentState(
                copy.deepcopy(state),
                applyMode=ComponentStateApplyMode.STARTUP_RESTORE,
            )
            target.panel.loadResolutionButtons['use_current_profile'].click()

            assert target.scan.scanPage() == 'simple'
            assert target.scan.cloakOwnsBackend()
            assert target.scan.acquisitionPlan().profile_id == profile_id
            # This fixture changes only the profile label, so adopting the
            # current profile definition does not need to alter acquisition TTLs.
            assert target.scan._readBackendWidget() == saved_backend
            assert target.panel.loadResolutionFrame.isHidden()
        finally:
            target.close()
    finally:
        source.close()


def test_monalisa_keep_advanced_dismisses_load_resolution_without_changes(qtbot):
    source = CloakRig('SimpleMoNaLISA')
    try:
        state = source.scan.getComponentState()
        saved_period = state['cloak']['monalisa']['acquisitionContext']['ulens'][
            'effectivePeriodUm'
        ]
        saved_backend = (
            copy.deepcopy(state['analogParameterDict']),
            copy.deepcopy(state['digitalParameterDict']),
        )
        target = CloakRig(
            'SimpleMoNaLISA',
            _monalisaSetupWithFixedULensPeriod(float(saved_period) + 0.1),
        )
        try:
            target.scan.applyComponentState(
                copy.deepcopy(state),
                applyMode=ComponentStateApplyMode.STARTUP_RESTORE,
            )
            target.panel.loadResolutionButtons['keep_advanced'].click()

            assert target.scan.scanPage() == 'advanced'
            assert target.scan._readBackendWidget() == saved_backend
            assert target.panel.loadResolutionFrame.isHidden()
            assert target.scan.acquisitionPlan() is None
        finally:
            target.close()
    finally:
        source.close()


def test_monalisa_changed_profile_can_import_saved_snapshot_without_overwrite(qtbot):
    source = CloakRig('SimpleMoNaLISA')
    try:
        state = source.scan.getComponentState()
        original_id = state['cloak']['monalisa']['profileId']
        saved_backend = (
            copy.deepcopy(state['analogParameterDict']),
            copy.deepcopy(state['digitalParameterDict']),
        )
        target = CloakRig(
            'SimpleMoNaLISA', _monalisaSetupWithModifiedProfile(original_id)
        )
        try:
            # Exercise the import/resolution logic without writing the test
            # setup to the user's configuration directory.
            def install_store(store, *, refresh_view=True):
                monalisa = target.scan._monalisa()
                monalisa['limits'] = dataclasses.replace(
                    monalisa['limits'], profile_store=store
                )
                target.scan._refreshResolvedSetup(refreshView=refresh_view)
                return True

            target.scan._saveProfileStoreOrReport = install_store
            target.scan.applyComponentState(
                copy.deepcopy(state),
                applyMode=ComponentStateApplyMode.STARTUP_RESTORE,
            )
            target.panel.loadResolutionButtons['import_saved_profile'].click()

            imported_id = target.scan.acquisitionPlan().profile_id
            assert imported_id != original_id
            assert original_id in target.limits().profile_store.profiles
            assert imported_id in target.limits().profile_store.profiles
            assert target.scan.scanPage() == 'simple'
            assert target.scan._readBackendWidget() == saved_backend
            assert target.panel.loadResolutionFrame.isHidden()
        finally:
            target.close()
    finally:
        source.close()


def test_monalisa_manual_advanced_edit_dismisses_stale_load_actions(qtbot):
    source = CloakRig('SimpleMoNaLISA')
    try:
        state = source.scan.getComponentState()
        saved_period = state['cloak']['monalisa']['acquisitionContext']['ulens'][
            'effectivePeriodUm'
        ]
        target = CloakRig(
            'SimpleMoNaLISA',
            _monalisaSetupWithFixedULensPeriod(float(saved_period) + 0.1),
        )
        try:
            target.scan.applyComponentState(
                copy.deepcopy(state),
                applyMode=ComponentStateApplyMode.STARTUP_RESTORE,
            )
            assert not target.panel.loadResolutionFrame.isHidden()

            axis = target.limits().axis_device('X')
            target.panel.backend.setScanStepSize(axis, 0.123)

            assert target.panel.loadResolutionFrame.isHidden()
            assert target.scan.scanPage() == 'advanced'
        finally:
            target.close()
    finally:
        source.close()

# ---------------------------------------------------------------------------
# Simple MoNaLISA acquire/save orchestration
# ---------------------------------------------------------------------------

def test_monalisa_save_acquire_delegates_one_scan_to_recording(qtbot, monkeypatch):
    rig = CloakRig('SimpleMoNaLISA')
    try:
        calls = []

        class Result:
            accepted = True
            reason = ''

        def record_scan_series(source, detector_names, frames=1):
            calls.append((source, tuple(detector_names), frames))
            return Result()

        monkeypatch.setattr(
            rig.channel.recordingWorkflow,
            'record_scan_series',
            record_scan_series,
        )
        rig.view.saveAcquireCheck.setChecked(True)

        result = rig.scan.runScan()

        assert result.accepted
        assert calls == [(
            rig.scan,
            rig.scan._activeProfileDetectors(),
            1,
        )]
        assert not rig.scan._isBusy()
    finally:
        rig.close()


def test_monalisa_stop_prepares_external_frame_trigger(qtbot, monkeypatch):
    rig = CloakRig('SimpleMoNaLISA')
    try:
        calls = []
        monkeypatch.setattr(rig.scan, '_stopScreening', lambda: True)
        monkeypatch.setattr(
            rig.scan,
            '_profileScreeningDetectors',
            lambda: ('Camera',),
        )
        monkeypatch.setattr(
            rig.scan,
            '_ensureSimpleCameraTriggerOverride',
            lambda detectors, mode: calls.append((tuple(detectors), mode)),
        )

        rig.scan._onStopClicked()

        assert calls == [(('Camera',), 'external_frame')]
    finally:
        rig.close()


def test_monalisa_coupled_yz_exposes_editable_y_lateral_geometry(qtbot):
    rig = CloakRig('SimpleMoNaLISA')
    try:
        profile = rig.limits().profile_store.profile(rig.scan.acquisitionPlan().profile_id)
        assert profile.lateral_geometry_mode == 'coupled'

        rig.view.scanTypeCombo.setCurrentText('YZ')
        qtbot.wait(10)

        assert rig.scan.acquisitionPlan().scan_type == 'YZ'
        assert not rig.view.pixelField.isHidden()
        assert rig.view.pixelCaption.text() == 'Y pixel size'
        assert rig.view.lateralStepControl.isEnabled()
        assert rig.view.yStepField.isHidden()

        rig.view.lateralStepControl.spin.setValue(73.0)
        qtbot.wait(10)
        assert rig.scan.acquisitionPlan().step_um_for_axis('Y') == pytest.approx(0.073)
        assert rig.scan.acquisitionPlan().step_um_for_axis('X') == pytest.approx(0.073)
    finally:
        rig.close()
