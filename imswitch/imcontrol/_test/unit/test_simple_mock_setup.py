"""The shipped ``simple_mock_setup.json``.

A hardware-free point-scanning microscope: one mock camera for live
widefield, one simulated APD imaging the synthetic sample through the
SimplePointScan panel. Checked here: what the file holds and that it runs
-- camera live view and the APD overview together, without errors in the
log.
"""
import json
import logging

import numpy as np
import pytest
from qtpy import QtTest

from imswitch.imcontrol.model.devices.status import DeviceRuntimeMode
from imswitch.imcontrol.model.managers.detectors._mock_sample import MockSample
from imswitch.imcontrol.model.scan_frame import frame_geometry_of
from .test_simple_point_scan_controller import Rig

SETUPS = 'imswitch/_data/user_defaults/imcontrol_setups/'
DEFAULT = SETUPS + 'simple_mock_setup.json'


@pytest.fixture(scope='module')
def setup():
    return json.loads(open(DEFAULT).read())


# ---------------------------------------------------------------------------
# What it is
# ---------------------------------------------------------------------------

def test_it_is_one_camera_and_one_apd_on_the_point_scan_panel(setup):
    detectors = {name: info['managerName'] for name, info in setup['detectors'].items()}
    assert sorted(detectors.values()) == ['APDManager', 'AVManager']
    camera = next(info for info in setup['detectors'].values() if info['managerName'] == 'AVManager')
    assert camera['managerProperties']['cameraListIndex'] == 'mock'
    assert setup['scan']['scanWidgetType'] == 'Advanced'
    assert setup['scan']['cloak'] == 'SimplePointScan'
    assert setup['nidaq']['simulation'] is True


def test_the_apd_images_its_sample_in_the_scanners_micrometres(setup):
    apd = next(info for info in setup['detectors'].values() if info['managerName'] == 'APDManager')
    axes = apd['managerProperties']['mockSample']['axes']
    for device, umPerVolt in axes.items():
        assert umPerVolt == setup['positioners'][device]['managerProperties']['conversionFactor']


# ---------------------------------------------------------------------------
# It runs
# ---------------------------------------------------------------------------

@pytest.fixture
def rig(qtbot):
    rig = Rig(open(DEFAULT).read())
    yield rig
    rig.close()


def test_camera_live_view_runs_beside_the_apd_without_errors(rig, caplog):
    """Live view asks every acquisition detector for its latest frame; the
    APD used to fail there until its first scan."""
    detectors = rig.master.detectorsManager
    frames = []
    detectors['Camera'].sigImageUpdated.connect(lambda im, init, scale: frames.append(im))
    with caplog.at_level(logging.WARNING):
        handle = detectors.startAcquisition(liveView=True)
        try:
            QtTest.QTest.qWait(800)
        finally:
            detectors.stopAcquisition(handle, liveView=True)
    assert frames and frames[-1].ndim == 2 and frames[-1].size > 1
    assert isinstance(detectors['APD'].getLatestFrame(is_save=False), np.ndarray)
    problems = [r for r in caplog.records if r.levelno >= logging.WARNING
                and r.name.startswith('imswitch') and 'shadowing' not in r.getMessage()]
    assert problems == []


def test_the_mock_camera_is_named_not_failed_over_to(rig, caplog):
    """``cameraListIndex: "mock"`` installs the mock as the configured
    backend; no real camera is tried, so nothing fails over or warns."""
    camera = rig.master.detectorsManager['Camera']
    assert camera.runtimeMode is DeviceRuntimeMode.MOCK
    assert camera.backendHolder.configured_mock is True
    assert camera.connectionFailureKind is None
    assert not any('Failed to initialize AV camera' in r.getMessage() for r in caplog.records)


def test_the_overview_images_the_sample(rig, qtbot, setup):
    frames = []
    rig.master.detectorsManager['APD'].sigImageUpdated.connect(
        lambda im, init, scale: frames.append(im))
    rig.widget.startButton.click()
    qtbot.waitUntil(lambda: rig.count('iteration') >= 1, timeout=15000)
    rig.widget.stopButton.click()
    assert rig.waitForEnd()

    frame = next(f for f in reversed(frames) if frame_geometry_of(f) is not None)
    geometry = frame_geometry_of(frame)
    assert geometry.axes[0].count * geometry.axes[0].step_um == pytest.approx(60.0, abs=0.5)
    apd = next(info for info in setup['detectors'].values() if info['managerName'] == 'APDManager')
    sample = MockSample.from_property(apd['managerProperties']['mockSample'])
    x, y = geometry.axes
    xx, yy = np.meshgrid(x.first_um + np.arange(x.count) * x.step_um,
                         y.first_um + np.arange(y.count) * y.step_um)
    expected = sample.brightness(xx, yy)
    assert np.corrcoef(np.asarray(frame, float).ravel(), expected.ravel())[0, 1] > 0.9


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
