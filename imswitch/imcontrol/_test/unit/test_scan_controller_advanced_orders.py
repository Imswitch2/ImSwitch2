"""Visit orders and strict timing on the Advanced panel
(docs/design/plans/scan-engines-and-expert-scan.md §2.6, M2).

A real MasterController on the simulated NI-DAQ (``galvo_apd_simple_mock``
with the Beta designer: two APDs imaging a synthetic sample from the scan
waveforms), the real Advanced controller and panel. Scans really run.
"""
import copy
import json
import time

import numpy as np
import pytest
from qtpy import QtTest

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
from imswitch.imcontrol.model.errors import ScanDesignRefusedError
from imswitch.imcontrol.view import ViewSetupInfo
from imswitch.imcontrol.view.widgets.ScanWidgetAdvanced import ScanWidgetAdvanced

from .test_simple_point_scan_controller import SETUP, _Main

#: A simulated stepped stage: Beta's move and settle, short enough that a
#: 24 x 24 scan at 1 ms runs in about a second.
MOCK_STAGE = {'return_time': 1e-3, 'move_time': 1e-4, 'settle_time': 1e-4}
DWELL_S = 1e-3


class AdvancedRig:
    def __init__(self, designer='BetaScanDesigner'):
        setup = json.load(open(SETUP))
        setup['scan'].update(scanWidgetType='Advanced', scanDesigner=designer)
        if designer == 'BetaScanDesigner':
            setup['scan']['scanDesignerParams'] = dict(MOCK_STAGE)
        self.setup = ViewSetupInfo.from_json(json.dumps(setup))
        self.main = _Main()
        self.channel = CommunicationChannel(self.main, self.setup)
        self.master = MasterController(self.setup, self.channel, self.main._moduleCommChannel)
        self.factory = ImConWidgetControllerFactory(
            self.setup, self.master, self.channel, self.main._moduleCommChannel)
        self.widget = ScanWidgetAdvanced(optionsBasic)
        self.scan = self.factory.createController(ScanControllerAdvanced, self.widget)
        self.main.controllers['Scan'] = self.scan
        self.ended = []
        self.channel.sigScanEnded.connect(lambda *a: self.ended.append(1))

    def load(self, *, lengths, steps, orders=None, dwell=DWELL_S, ttl=False):
        """Put a scan into the panel the way a file does."""
        self.scan.getParameters()
        analog = copy.deepcopy(self.scan._analogParameterDict)
        digital = copy.deepcopy(self.scan._digitalParameterDict)
        analog.update(axis_length=list(lengths), axis_step_size=list(steps),
                      axis_centerpos=[0.0, 0.0, 0.0], sequence_time=dwell)
        analog.pop('axis_order', None)
        if orders is not None:
            analog['axis_order'] = list(orders)
        digital['sequence_time'] = dwell
        if not ttl:
            digital['linestep_enable'] = {name: [False] for name in digital['target_device']}
        self.scan._scanParams.apply(self.widget, analog, digital,
                                    self.scan.positioners, self.scan.TTLDevices)
        self.scan.updatePixels()

    def run(self, timeoutS=30.0):
        self.ended.clear()
        self.scan.runScanAdvanced()
        deadline = time.monotonic() + timeoutS
        while not self.ended and time.monotonic() < deadline:
            QtTest.QTest.qWait(50)
        assert self.ended, 'the scan did not end'
        return np.array(self.master.detectorsManager['APD']._image, dtype=float)

    def close(self):
        try:
            self.factory.closeAllCreatedControllers(waitTimeoutS=5.0)
        finally:
            self.master.closeEvent()


@pytest.fixture
def rig(qtbot):
    rig = AdvancedRig()
    yield rig
    rig.close()


def _orders(widget, name):
    combo = widget.scanPar['order' + name]
    return [combo.itemData(i) for i in range(combo.count())]


# ---------------------------------------------------------------------------
# The panel
# ---------------------------------------------------------------------------

def test_the_order_column_offers_the_interleaves_the_pixel_count_allows(rig):
    rig.load(lengths=[3.0, 6.0, 0.0], steps=[0.5, 0.5, 1.0])
    assert _orders(rig.widget, 'X') == ['forward', 'serpentine', 'interleave:2', 'interleave:3']
    assert _orders(rig.widget, 'Y') == ['forward', 'serpentine', 'interleave:2', 'interleave:3',
                                        'interleave:4', 'interleave:6']
    assert rig.widget.scanPar['orderX'].isEnabled()


def test_a_saved_order_the_new_count_does_not_allow_stays_and_is_refused_with_the_count(rig):
    rig.load(lengths=[3.0, 6.0, 0.0], steps=[0.5, 0.5, 1.0],
             orders=['forward', 'interleave:4', 'forward'])
    rig.load(lengths=[3.0, 5.0, 0.0], steps=[0.5, 0.5, 1.0],
             orders=['forward', 'interleave:4', 'forward'])
    assert rig.widget.getScanAxisOrder('Y') == 'interleave:4'
    rig.scan.getParameters()
    with pytest.raises(ScanDesignRefusedError, match='this axis has 10'):
        rig.scan._make_full_scan(rig.scan._analogParameterDict, rig.scan._digitalParameterDict)


def test_a_forward_scan_writes_no_order_and_its_policy_is_the_designers(rig):
    rig.load(lengths=[3.0, 3.0, 0.0], steps=[0.5, 0.5, 1.0])
    state = rig.scan.getComponentState()
    assert 'axis_order' not in state['analogParameterDict']
    assert state['timingPolicy'] == 'advanced-compatible-v1'
    assert rig.widget.timingNoteLabel.text() == ''          # on the clock: nothing to say


def test_an_order_migrates_the_scan_to_strict_timing_visibly(rig):
    rig.load(lengths=[3.0, 3.0, 0.0], steps=[0.5, 0.5, 1.0],
             orders=['serpentine', 'forward', 'forward'])
    state = rig.scan.getComponentState()
    assert state['analogParameterDict']['axis_order'] == ['serpentine', 'forward', 'forward']
    assert state['timingPolicy'] == 'strict-v1'
    assert 'Strict timing (strict-v1)' in rig.widget.timingNoteLabel.text()
    # The requested dwell is kept, never snapped.
    assert state['analogParameterDict']['sequence_time'] == DWELL_S


def test_the_panel_shows_a_rounded_dwell_beside_the_requested_one(rig):
    rate = rig.setup.scan.sampleRate
    rig.load(lengths=[3.0, 3.0, 0.0], steps=[0.5, 0.5, 1.0], dwell=DWELL_S + 0.5 / rate)
    text = rig.widget.timingNoteLabel.text()
    assert 'runs as 1.01 ms' in text and 'advanced-compatible-v1' in text
    rig.load(lengths=[3.0, 3.0, 0.0], steps=[0.5, 0.5, 1.0], dwell=DWELL_S + 0.5 / rate,
             orders=['serpentine', 'forward', 'forward'])
    text = rig.widget.timingNoteLabel.text()
    assert 'is not on the 10 µs grid' in text and '1 ms or 1.01 ms' in text


def test_the_panel_shows_a_rounded_window_edge_too(rig):
    """Not only the dwell: a line-step window that starts between two
    samples is played on one of them, and the note says which (§7.3)."""
    rig.load(lengths=[3.0, 3.0, 0.0], steps=[0.5, 0.5, 1.0], ttl=True)
    analog = copy.deepcopy(rig.scan._analogParameterDict)
    digital = copy.deepcopy(rig.scan._digitalParameterDict)
    laser = digital['target_device'][0]
    rate = rig.setup.scan.sampleRate
    digital['advanced_mode'] = True
    digital['linestep_enable'] = {name: [name == laser] for name in digital['target_device']}
    digital['pulse_starts_s'] = {name: [[1.5 / rate]] for name in digital['target_device']}
    digital['pulse_ends_s'] = {name: [[50 / rate]] for name in digital['target_device']}
    rig.scan._scanParams.apply(rig.widget, analog, digital, rig.scan.positioners,
                               rig.scan.TTLDevices)
    rig.scan._refreshTimingNote()
    text = rig.widget.timingNoteLabel.text()
    assert f'{laser} ttl start at 15 µs runs at 20 µs.' in text, text


def test_a_legacy_file_loads_previews_and_saves_back_unchanged_but_for_its_policy(rig, tmp_path):
    """Plan §2.6: load -> preview -> save -> reload of a legacy Advanced file
    gives identical requested values and waveforms; the saved file differs
    only by the added ``timingPolicy``."""
    rig.load(lengths=[3.0, 2.0, 0.0], steps=[0.5, 0.5, 1.0], ttl=True)
    legacy = rig.scan.getComponentState()
    legacy.pop('timingPolicy')                      # what today's panel writes
    legacyFile = tmp_path / 'legacy.json'
    legacyFile.write_text(json.dumps(legacy))

    rig.load(lengths=[3.0, 3.0, 0.0], steps=[0.5, 0.5, 1.0],
             orders=['serpentine', 'interleave:2', 'forward'])     # something else first
    rig.scan.loadScanParamsFromFile(str(legacyFile))
    assert rig.widget.getScanAxisOrder('X') == 'forward'          # no stale order
    assert rig.widget.getScanAxisOrder('Y') == 'forward'
    first, _ = rig.scan._buildScanSignals()
    savedFile = tmp_path / 'saved.json'
    rig.scan.saveScanParamsToFile(str(savedFile))
    saved = json.loads(savedFile.read_text())
    assert saved.pop('timingPolicy') == 'advanced-compatible-v1'
    assert saved == json.loads(legacyFile.read_text())

    rig.scan.loadScanParamsFromFile(str(savedFile))
    second, _ = rig.scan._buildScanSignals()
    for kind in ('scanSignalsDict', 'TTLCycleSignalsDict'):
        assert sorted(first[kind]) == sorted(second[kind])
        for name in first[kind]:
            np.testing.assert_array_equal(first[kind][name], second[kind][name], err_msg=name)


def test_an_ordered_scan_saves_and_loads_back(rig, tmp_path):
    rig.load(lengths=[3.0, 3.0, 0.0], steps=[0.5, 0.5, 1.0],
             orders=['serpentine', 'interleave:3', 'forward'])
    first, _ = rig.scan._buildScanSignals()
    path = tmp_path / 'ordered.json'
    rig.scan.saveScanParamsToFile(str(path))
    rig.load(lengths=[3.0, 3.0, 0.0], steps=[0.5, 0.5, 1.0])
    rig.scan.loadScanParamsFromFile(str(path))
    assert rig.widget.getScanAxisOrder('Y') == 'interleave:3'
    second, info = rig.scan._buildScanSignals()
    assert info['timing_policy'] == 'strict-v1'
    for name in first['scanSignalsDict']:
        np.testing.assert_array_equal(first['scanSignalsDict'][name],
                                      second['scanSignalsDict'][name], err_msg=name)


def test_a_file_naming_an_unknown_timing_policy_is_not_applied(rig):
    rig.load(lengths=[3.0, 3.0, 0.0], steps=[0.5, 0.5, 1.0])
    state = rig.scan.getComponentState()
    state['timingPolicy'] = 'strict-v9'
    state['analogParameterDict']['axis_length'] = [1.0, 1.0, 0.0]
    warnings = rig.scan.applyComponentState(
        state, applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY)
    assert any('strict-v9' in warning and 'not applied' in warning for warning in warnings)
    rig.scan.getParameters()
    assert rig.scan._analogParameterDict['axis_length'][:2] == [3.0, 3.0]


def test_a_swept_designer_greys_the_order_column_with_the_reason(qtbot):
    rig = AdvancedRig(designer='GalvoScanDesigner')
    try:
        combo = rig.widget.scanPar['orderX']
        assert not combo.isEnabled()
        assert 'GalvoScanDesigner, which sweeps its fast axis' in combo.toolTip()
        assert rig.widget.getScanAxisOrder('X') == 'forward'
    finally:
        rig.close()


# ---------------------------------------------------------------------------
# End to end: the mock APD images the sample in place, whatever the order
# ---------------------------------------------------------------------------

def _correlation(a, b):
    return np.corrcoef(np.ravel(a), np.ravel(b))[0, 1]


def test_the_apd_images_the_sample_in_place_whatever_the_order(rig):
    """The simulated APD reads the synthetic sample under the beam from the
    AO waveforms, in time order. An interleaved and serpentine scan must
    assemble the same image as the forward one (up to photon noise); the same
    lines placed in time order must not."""
    size = dict(lengths=[12.0, 12.0, 0.0], steps=[0.5, 0.5, 1.0])
    rig.load(**size)
    forward = rig.run()
    rig.load(**size, orders=['serpentine', 'interleave:3', 'forward'])
    ordered = rig.run()
    info = rig.scan.scanInfoDict
    assert info['timing_policy'] == 'strict-v1'
    assert info['axis_visit_order'][1]['interleave'][:3] == [0, 3, 6]

    assert ordered.shape == forward.shape == (1, 24, 24)
    assert _correlation(forward, ordered) > 0.9
    # Undo the placement: the lines as they were read.
    visits = np.asarray(info['axis_visit_order'][1]['interleave'])
    asRead = ordered[:, visits, :].copy()
    asRead[:, 1::2, :] = asRead[:, 1::2, ::-1]
    assert _correlation(forward, asRead) < 0.5


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
