"""The contract every scan cloak keeps (docs/simple-point-scan-plan.md §10.7).

Run for every entry of ``SCAN_CLOAKS``, each on its shipped mock setup with a
real MasterController and simulated NI-DAQ: the cloak's dock, its controller
and the backend's own widget, all real (no napari window). A new cloak adds
its mock setup and its two edits to the tables below and passes the same
tests:

* registration follows ``SCAN_CLOAKS`` (scan manager, classes, config editor);
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
from imswitch.imcontrol.model.scan_cloak import SCAN_CLOAKS
from imswitch.imcontrol.model.scan_frame import FRAME_GEOMETRY_KEY
from imswitch.imcontrol.view import ViewSetupInfo, widgets
from imswitch.imcontrol.view.widgets.ScanCloakPanel import ScanCloakPanel

SETUPS = 'imswitch/_data/user_defaults/imcontrol_setups/'
SCAN_TEMPLATE = 'imswitch/imcontrol/view/configeditor/builtin_templates/sections/scan.json'

#: A shipped mock setup per cloak.
MOCK_SETUPS = {
    'SimplePointScan': SETUPS + 'galvo_apd_simple_mock_scan_setup.json',
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


#: Per cloak: an Advanced-page edit it can show, and one it cannot.
EDITS = {
    'SimplePointScan': (_pointScanRepresentableEdit, _pointScanUnrepresentableEdit),
}

CLOAKS = sorted(SCAN_CLOAKS)


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
    def __init__(self, scanWidgetType, setupText=None):
        self.type = scanWidgetType
        self.backendType = SCAN_CLOAKS[scanWidgetType]
        self.setup = ViewSetupInfo.from_json(setupText or open(MOCK_SETUPS[scanWidgetType]).read())
        self.main = _Main()
        self.channel = CommunicationChannel(self.main, self.setup)
        self.master = MasterController(self.setup, self.channel, self.main._moduleCommChannel)
        self.factory = ImConWidgetControllerFactory(
            self.setup, self.master, self.channel, self.main._moduleCommChannel
        )
        self.panel = getattr(widgets, f'ScanWidget{scanWidgetType}')(optionsBasic)
        self.view = self.panel.view
        self.scan = self.factory.createController(
            _controllerClass(f'ScanController{scanWidgetType}'), self.panel)
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
        """The plan the backend widget holds."""
        return self.cloak.from_backend(*self.scan._readBackendWidget(), self.limits())

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


@pytest.mark.parametrize('cloak', CLOAKS)
def test_the_classes_and_the_config_editor_follow_the_table(cloak):
    backend = SCAN_CLOAKS[cloak]
    controller = _controllerClass(f'ScanController{cloak}')
    widget = getattr(widgets, f'ScanWidget{cloak}')
    assert issubclass(controller, ScanCloakController)
    assert issubclass(controller, _controllerClass(f'ScanController{backend}'))
    assert issubclass(widget, ScanCloakPanel)
    assert widget.backendWidgetClass is getattr(widgets, f'ScanWidget{backend}')
    options = next(field['opts'] for field in json.load(open(SCAN_TEMPLATE))['fields']
                   if field['key'] == 'scanWidgetType')
    assert cloak in options


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


def test_an_edit_it_cannot_show_is_refused_unless_discarded(rig, monkeypatch):
    before = rig.acquisition()
    rig.scan.setScanPage('advanced')
    _, unrepresentable = EDITS[rig.type]
    reason = unrepresentable(rig)
    asked = []
    monkeypatch.setattr(rig.panel, 'confirmDiscardAdvanced',
                        lambda text: asked.append(text) or False)

    assert not rig.scan.setScanPage('simple')
    assert rig.scan.scanPage() == 'advanced' and rig.panel.page() == 'advanced'
    assert reason in asked[0]

    monkeypatch.setattr(rig.panel, 'confirmDiscardAdvanced', lambda text: True)
    assert rig.scan.setScanPage('simple')
    assert rig.panel.page() == 'simple'
    assert rig.acquisition() == before
    assert rig.held() == before                        # the Advanced page is the simple scan again


def test_discarding_can_be_asked_for_directly(rig):
    before = rig.acquisition()
    rig.scan.setScanPage('advanced')
    EDITS[rig.type][1](rig)
    assert rig.scan.setScanPage('simple', discardAdvanced=True)
    assert rig.acquisition() == before


def test_the_switch_waits_for_a_running_scan(rig):
    rig.view.setLive(True)
    rig.view.startButton.click()
    try:
        QtTest.QTest.qWait(300)
        assert rig.view.isRunning()
        assert not rig.scan.setScanPage('advanced')
        assert rig.panel.page() == 'simple' and rig.panel.simpleButton.isChecked()
        assert 'Stop the scan' in rig.panel.noteLabel.text()
    finally:
        rig.view.stopButton.click()
        assert rig.waitForEnd()


def test_the_page_is_saved_and_restored(rig, qtbot):
    rig.scan.setScanPage('advanced')
    EDITS[rig.type][1](rig)                             # something only Advanced shows
    held = rig.scan._readBackendWidget()
    state = rig.scan.getComponentState()
    assert state['cloak']['page'] == 'advanced'

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
