"""The SimplePointScan controller on the simulated NI-DAQ (plan P1, D3-D6).

A real MasterController on ``galvo_apd_simple_mock_scan_setup.json`` (two
APDs, three gated lasers, simulated NI-DAQ), the real controller and the real
panel -- everything but the napari main window, whose OpenGL cannot run
offscreen. Scans really run: the tests count NI-DAQ iterations and run ends.
"""
import copy
import dataclasses
import time

import numpy as np
import pytest
from qtpy import QtTest

from imswitch import imcontrol
from imswitch.imcommon.controller import ModuleCommunicationChannel
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
from imswitch.imcontrol.controller.controllers.ScanControllerSimplePointScan import (
    ScanControllerSimplePointScan,
)
from imswitch.imcontrol.model.scan_frame import frame_geometry_of
from imswitch.imcontrol.model.scan_parameters import AdvancedScanParameterSerializer
from imswitch.imcontrol.model.simple_scan import AxisRegion, plan_to_dicts
from imswitch.imcontrol.view import ViewSetupInfo
from imswitch.imcontrol.view.widgets.ScanWidgetAdvanced import ScanWidgetAdvanced
from imswitch.imcontrol.view.widgets.ScanWidgetSimplePointScan import (
    ScanWidgetSimplePointScan,
)

SETUP = 'imswitch/_data/user_defaults/imcontrol_setups/galvo_apd_simple_mock_scan_setup.json'


class _Main:
    def __init__(self):
        self.controllers = {}
        self._moduleCommChannel = ModuleCommunicationChannel()
        self._moduleCommChannel.register(imcontrol)


class Rig:
    def __init__(self, setupText=None, viewer=None):
        self.setup = ViewSetupInfo.from_json(setupText or open(SETUP).read())
        self.main = _Main()
        self.channel = CommunicationChannel(self.main, self.setup)
        self.master = MasterController(self.setup, self.channel, self.main._moduleCommChannel)
        self.factory = ImConWidgetControllerFactory(
            self.setup, self.master, self.channel, self.main._moduleCommChannel
        )
        self.widget = ScanWidgetSimplePointScan(optionsBasic, napariViewer=viewer)
        self.scan = self.factory.createController(ScanControllerSimplePointScan, self.widget)
        self.main.controllers['Scan'] = self.scan
        self.events = []
        for name in ('sigScanStarting', 'sigScanDone', 'sigScanEnded'):
            getattr(self.channel, name).connect(
                lambda *a, n=name: self.events.append(n))
        # One NI-DAQ completion per iteration; the channel's sigScanDone fires
        # once per run in Live mode.
        self.master.nidaqManager.sigScanDone.connect(
            lambda *a: self.events.append('iteration'))
        self.rejections = []
        self.channel.sigScanRequestRejected.connect(self.rejections.append)

    def count(self, name):
        return self.events.count(name)

    def waitForEnd(self, timeoutS=20.0):
        deadline = time.monotonic() + timeoutS
        while time.monotonic() < deadline and 'sigScanEnded' not in self.events:
            QtTest.QTest.qWait(50)
        return 'sigScanEnded' in self.events

    def close(self):
        try:
            self.factory.closeAllCreatedControllers(waitTimeoutS=5.0)
        finally:
            self.master.closeEvent()


@pytest.fixture
def rig(qtbot):
    rig = Rig()
    yield rig
    rig.close()


def _edit(rig, **changes):
    plan = dataclasses.replace(rig.scan._simple()['acquisition'], **changes)
    rig.widget.sigAcquisitionEdited.emit(plan)
    return rig.scan._simple()['acquisition']


# ---------------------------------------------------------------------------
# What the panel opens with
# ---------------------------------------------------------------------------

def test_the_overview_is_planned_within_budget_and_the_scanners_range(rig):
    overview = rig.scan._simple()['overview']
    assert overview.met is True
    assert overview.estimate_s <= 1.0
    assert overview.field_um <= 35.0                     # ±10 V at 1.75 µm/V
    assert overview.pixels * overview.step_um == pytest.approx(overview.field_um)
    assert overview.dwell_s == pytest.approx(
        rig.scan._scanLimits().min_dwell_s('X', overview.step_um))
    assert rig.widget.overviewButton.isChecked()
    assert 'Full field' in rig.widget.overviewLabel.text()


def test_the_default_acquisition_is_inside_the_overview(rig):
    state = rig.scan._simple()
    plan = state['acquisition']
    assert plan.dims == ('X', 'Y')
    assert plan.channels == (('405 (ON)',),)
    for region in plan.regions.values():
        assert region.length_um < state['overview'].field_um
        assert region.pixels * region.step_um == pytest.approx(region.length_um)


# ---------------------------------------------------------------------------
# Running (D3)
# ---------------------------------------------------------------------------

def test_the_overview_runs_live_until_stop_and_ends_once(rig):
    frames = []
    rig.master.detectorsManager['APD'].sigImageUpdated.connect(
        lambda im, init, scale: frames.append(im))
    rig.widget.scanButton.click()
    QtTest.QTest.qWait(2500)
    assert rig.scan.isRunning or rig.scan.__dict__.get('_repeatPending')
    rig.widget.stopButton.click()

    assert rig.waitForEnd()
    assert rig.count('iteration') >= 2
    assert rig.count('sigScanEnded') == 1
    assert frames and all(frame_geometry_of(f) is not None for f in frames)


def test_an_acquisition_runs_one_frame(rig):
    rig.scan.setSimpleScanMode('acquisition')
    rig.widget.scanButton.click()

    assert rig.waitForEnd()
    assert rig.count('iteration') == 1
    assert rig.count('sigScanEnded') == 1


def test_an_external_start_runs_exactly_one_iteration_even_in_live_overview(rig):
    """A recording or script owns any series (plan D3): an external start runs
    one iteration whatever the panel says -- even after a Live run left the
    panel's own mode behind, and even if Live is ticked during the run."""
    rig.widget.scanButton.click()                 # a Live overview first
    QtTest.QTest.qWait(500)
    rig.widget.stopButton.click()
    assert rig.waitForEnd()
    rig.events.clear()

    rig.scan.runScanExternal(True, False)
    rig.widget.setRepeatEnabled(True)             # Live ticked mid-run
    ended = rig.waitForEnd(timeoutS=8.0)
    if not ended:                                 # clean up a looping mutant
        rig.widget.stopButton.click()
        rig.waitForEnd()
    assert ended
    QtTest.QTest.qWait(300)
    assert rig.count('iteration') == 1
    assert rig.count('sigScanEnded') == 1


def test_modes_cannot_be_switched_while_running(rig):
    rig.widget.scanButton.click()
    QtTest.QTest.qWait(300)
    rig.scan.setSimpleScanMode('acquisition')
    assert rig.scan._simple()['mode'] == 'overview'
    assert 'Stop the scan' in rig.widget.messageLabel.text()
    rig.widget.stopButton.click()
    assert rig.waitForEnd()


# ---------------------------------------------------------------------------
# Channel power (D5)
# ---------------------------------------------------------------------------

def test_a_power_setting_that_cannot_be_built_refuses_the_start(rig):
    _edit(rig, channel_power_on=True, channel_power={'405 (ON)': (50.0,)})
    rig.scan.setSimpleScanMode('acquisition')
    rig.widget.scanButton.click()
    QtTest.QTest.qWait(300)

    assert rig.rejections and 'no analog channel' in rig.rejections[0]
    assert 'sigScanStarting' not in rig.events
    assert not rig.scan.isRunning
    assert 'Not started' in rig.widget.messageLabel.text()


def test_a_power_waveform_that_is_not_built_refuses_the_start(rig):
    """Advanced swallows a failed power injection and scans without it; this
    panel checks the built waveforms and refuses instead."""
    state = rig.scan._simple()
    limits = state['limits']
    state['limits'] = dataclasses.replace(limits, gates=tuple(
        dataclasses.replace(g, power_capable=True) for g in limits.gates))
    _edit(rig, channel_power_on=True, channel_power={'405 (ON)': (50.0,)})
    rig.scan.setSimpleScanMode('acquisition')
    rig.widget.scanButton.click()
    QtTest.QTest.qWait(300)

    assert rig.rejections and 'could not be built' in rig.rejections[0]
    assert 'sigScanStarting' not in rig.events


# ---------------------------------------------------------------------------
# Edits and readouts
# ---------------------------------------------------------------------------

def test_the_dwell_never_goes_below_what_the_scanner_allows(rig):
    regions = {axis: AxisRegion(0.0, 20.0, 5.0) for axis in ('X', 'Y')}
    plan = _edit(rig, regions=regions, dwell_s=1e-6)
    assert plan.dwell_s == pytest.approx(50e-6)      # 5 µm at 0.1 µm/µs


def test_a_region_beyond_the_scanners_range_is_explained(rig):
    rig.scan.setSimpleScanMode('acquisition')
    regions = {axis: AxisRegion(0.0, 40.0, 0.5) for axis in ('X', 'Y')}
    _edit(rig, regions=regions)
    rig.scan._refreshEstimate()
    assert 'outside' in rig.widget.estimateNote.text()


def test_unscanned_positioners_stay_where_they_are(rig):
    rig.master.positionersManager['Z'].setPosition(3.0, 'Z')
    analog = rig.scan.currentPlan()
    assert analog.park['Z'] == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Saved state (D4)
# ---------------------------------------------------------------------------

def test_the_saved_dicts_are_the_acquisition_even_in_overview_mode(rig):
    state = rig.scan.getComponentState()
    acquisition = rig.scan._simple()['acquisition']
    assert state['simplePlan']['mode'] == 'overview'
    lengths = state['analogParameterDict']['axis_length'][:2]
    assert lengths == [acquisition.regions['X'].length_um, acquisition.regions['Y'].length_um]


def test_a_saved_state_restores_the_panel(qtbot):
    first = Rig()
    try:
        _edit(first, channels=(('405 (ON)',), ('561 (EXC)',)), dwell_s=80e-6)
        first.scan.setSimpleScanMode('acquisition')
        saved = first.scan.getComponentState()
    finally:
        first.close()
    second = Rig()
    try:
        warnings = second.scan.applyComponentState(
            saved, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
        restored = second.scan._simple()
        assert warnings == []
        assert restored['mode'] == 'acquisition'
        assert restored['acquisition'].channels == (('405 (ON)',), ('561 (EXC)',))
        assert restored['acquisition'].dwell_s == pytest.approx(80e-6)
    finally:
        second.close()


def test_an_advanced_scan_it_cannot_show_is_refused_and_changes_nothing(rig):
    before = rig.scan._simple()['acquisition']
    analog, digital = plan_to_dicts(before, rig.scan._scanLimits())
    digital.update(advanced_mode=True,
                   pulse_starts_s={'405 (ON)': [[1e-5]]},
                   pulse_ends_s={'405 (ON)': [[2e-5]]})
    warnings = rig.scan.applyComponentState(
        {'analogParameterDict': analog, 'digitalParameterDict': digital},
        applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY)

    assert len(warnings) == 1 and 'timing windows' in warnings[0]
    assert rig.scan._simple()['acquisition'] == before


def test_the_same_dicts_build_the_same_waveforms_in_advanced(rig, qtbot):
    """Plan D4, claim 1: a Simple scan loaded into the Advanced panel (its
    widget and serializer) builds identical AO and DO waveforms."""
    # a parked position off Advanced's 1 nm grid
    rig.master.positionersManager['Z'].setPosition(3.00049, 'Z')
    plan = _edit(rig, channels=(('405 (ON)',), ('488 (EXC)', '561 (EXC)')))
    simpleAnalog, simpleDigital = plan_to_dicts(rig.scan._withCurrentPark(plan),
                                                rig.scan._scanLimits())

    positioners = rig.scan.positioners
    ttl = rig.scan.TTLDevices
    advanced = ScanWidgetAdvanced(optionsBasic)
    advanced.initControls(positioners.keys(), ttl.keys(), 'ms')
    serializer = AdvancedScanParameterSerializer()
    serializer.apply(advanced, copy.deepcopy(simpleAnalog), copy.deepcopy(simpleDigital),
                     positioners, ttl)
    advancedAnalog, _ = serializer.build_analog(advanced, positioners)
    advancedDigital = serializer.build_digital(advanced, advancedAnalog, positioners, ttl)

    simpleSignals, _ = ScanControllerAdvanced._make_full_scan(
        rig.scan, simpleAnalog, simpleDigital)
    advancedSignals, _ = ScanControllerAdvanced._make_full_scan(
        rig.scan, advancedAnalog, advancedDigital)
    # parking is part of the scan too: runScanAdvanced moves unscanned axes
    assert advancedAnalog['axis_centerpos'] == simpleAnalog['axis_centerpos']
    for kind in ('scanSignalsDict', 'TTLCycleSignalsDict'):
        assert sorted(simpleSignals[kind]) == sorted(advancedSignals[kind])
        for name in simpleSignals[kind]:
            np.testing.assert_array_equal(simpleSignals[kind][name],
                                          advancedSignals[kind][name], err_msg=name)


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
