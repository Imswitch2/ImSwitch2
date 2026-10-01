from dataclasses import replace
import os

import numpy as np
import pytest

from imswitch.imcontrol._test import setupInfoBasic
from imswitch.imcontrol.model import ScanDesignRefusedError, ScanManagerBase


def _stage_parameters(z_center):
    # Z is a 0-10 V piezo (conv 10) -- its 5 um scan must be centered inside
    # the travel to be physically representable; X/Y are +-10 V galvos and can
    # stay centered on 0.
    return {'target_device': ['X', 'Y', 'Z'],
            'axis_length': [5, 5, 5],
            'axis_step_size': [1, 1, 1],
            'axis_centerpos': [0, 0, z_center],
            'axis_startpos': [[0], [0], [z_center]],
            'sequence_time': 0.005,
            'return_time': 0.001,
            'phase_delay': 40}


def test_scan_signals():
    stageParameters = _stage_parameters(z_center=25)
    TTLParameters = {'target_device': ['405', '488'],
                     'TTL_start': [[0.0001, 0.004], [0, 0]],
                     'TTL_end': [[0.0015, 0.005], [0, 0]],
                     'sequence_time': 0.005}

    sh = ScanManagerBase(setupInfo=setupInfoBasic)
    fullsig, _ = sh.makeFullScan(stageParameters, TTLParameters)

    # All required dicts exist
    assert 'scanSignalsDict' in fullsig
    assert 'TTLCycleSignalsDict' in fullsig

    # All targets match
    assert set(fullsig['scanSignalsDict'].keys()) == set(stageParameters['target_device'])
    assert set(fullsig['TTLCycleSignalsDict'].keys()) == set(TTLParameters['target_device'])

    # Lengths of signal arrays match
    for device in fullsig['scanSignalsDict']:
        assert len(fullsig['scanSignalsDict'][device]) == 65000

    for device in fullsig['TTLCycleSignalsDict']:
        assert len(fullsig['TTLCycleSignalsDict'][device]) == 65000

    # Basic stage signal checks. The scan is centered on the ROI center (Center
    # moves the scan) with a truthful per-pixel step (pitch == step, span
    # (N-1)*step). Previously it ran [0, extent] from the voltage rail,
    # ignoring Center. See scan-beta-center-ignored / scan-realized-step-spacing.
    for dev in ('X', 'Y'):
        s = fullsig['scanSignalsDict'][dev]
        assert np.isclose(s.min(), -s.max())   # symmetric about center 0
        assert s.max() > 0
    # X and Y share a convFactor -> identical span
    assert np.isclose(fullsig['scanSignalsDict']['X'].max(),
                      fullsig['scanSignalsDict']['Y'].max())
    # Z: 5 um span centered on 25 um at conv 10 -> [2.3, 2.7] V, inside [0, 10]
    z = fullsig['scanSignalsDict']['Z']
    assert np.isclose((z.min() + z.max()) / 2, 2.5)
    assert z.min() >= 0

    # Basic TTL signal checks
    assert np.count_nonzero(fullsig['TTLCycleSignalsDict']['405']) == 30000
    assert np.all(~fullsig['TTLCycleSignalsDict']['488'])


def test_scan_rejected_when_signal_leaves_voltage_range():
    """makeFullScan refuses a scan whose waveform leaves a positioner's
    [minVolt, maxVolt]. BetaScanDesigner.checkSignalComp was a ``return True``
    stub, so this Z scan centered on 0 um -- dipping to -0.2 V on the 0-10 V Z
    piezo -- used to sail through to the DAQ."""
    stageParameters = _stage_parameters(z_center=0)
    TTLParameters = {'target_device': ['405', '488'],
                     'TTL_start': [[0.0001, 0.004], [0, 0]],
                     'TTL_end': [[0.0015, 0.005], [0, 0]],
                     'sequence_time': 0.005}

    sh = ScanManagerBase(setupInfo=setupInfoBasic)
    with pytest.raises(ScanDesignRefusedError, match='voltages outside') as error:
        sh.makeFullScan(stageParameters, TTLParameters)
    message = str(error.value)
    assert 'Z (Z)' in message
    assert 'outside configured 0...10 V range' in message
    assert 'at least +2 um' in message



def test_beta_scan_uses_position_before_scan_as_relative_center():
    stageParameters = _stage_parameters(z_center=0)
    stageParameters['axis_position_before_scan'] = [[0], [0], [25]]
    TTLParameters = {'target_device': ['405', '488'],
                     'TTL_start': [[0.0001, 0.004], [0, 0]],
                     'TTL_end': [[0.0015, 0.005], [0, 0]],
                     'sequence_time': 0.005}

    sh = ScanManagerBase(setupInfo=setupInfoBasic)
    fullsig, _ = sh.makeFullScan(stageParameters, TTLParameters)

    z = fullsig['scanSignalsDict']['Z']
    # Z conversionFactor = 10 um/V. A five-pixel 1 um-pitch scan centered at
    # 25 um starts at 23 um (2.3 V), and the final flyback returns to 25 um.
    assert np.isclose(z[0], 2.3)
    assert np.isclose(z[-1], 2.5)
    assert z.min() >= 0


def test_beta_center_is_relative_offset_and_returns_to_previous_position():
    stageParameters = _stage_parameters(z_center=5)
    stageParameters['axis_position_before_scan'] = [[0], [0], [25]]
    TTLParameters = {'target_device': ['405', '488'],
                     'TTL_start': [[0.0001, 0.004], [0, 0]],
                     'TTL_end': [[0.0015, 0.005], [0, 0]],
                     'sequence_time': 0.005}

    sh = ScanManagerBase(setupInfo=setupInfoBasic)
    fullsig, _ = sh.makeFullScan(stageParameters, TTLParameters)

    z = fullsig['scanSignalsDict']['Z']
    # 25 um before scan + 5 um Center offset => 30 um effective center.
    assert np.isclose(z[0], 2.8)
    # Center offset never changes the position restored after the scan.
    assert np.isclose(z[-1], 2.5)


def test_beta_start_anchor_starts_from_position_before_scan():
    stageParameters = _stage_parameters(z_center=0)
    stageParameters['axis_position_before_scan'] = [[0], [0], [0]]
    TTLParameters = {'target_device': ['405', '488'],
                     'TTL_start': [[0.0001, 0.004], [0, 0]],
                     'TTL_end': [[0.0015, 0.005], [0, 0]],
                     'sequence_time': 0.005}

    scanInfo = replace(
        setupInfoBasic.scan,
        scanDesignerParams={
            **setupInfoBasic.scan.scanDesignerParams,
            'position_anchor': 'start',
        },
    )
    setupInfo = replace(setupInfoBasic, scan=scanInfo)
    sh = ScanManagerBase(setupInfo=setupInfo)
    fullsig, _ = sh.makeFullScan(stageParameters, TTLParameters)

    z = fullsig['scanSignalsDict']['Z']
    # With start anchoring and Center=0, the first pixel is exactly the
    # pre-scan position. This keeps a 0-10 V offset-controlled axis entirely
    # on the positive side while still restoring the pre-scan position.
    assert np.isclose(z[0], 0.0)
    assert np.isclose(z.max(), 0.4)
    assert np.isclose(z[-1], 0.0)


def test_beta_start_anchor_applies_center_as_positive_start_offset():
    stageParameters = _stage_parameters(z_center=5)
    stageParameters['axis_position_before_scan'] = [[0], [0], [25]]
    stageParameters['position_anchor'] = 'start'
    TTLParameters = {'target_device': ['405', '488'],
                     'TTL_start': [[0.0001, 0.004], [0, 0]],
                     'TTL_end': [[0.0015, 0.005], [0, 0]],
                     'sequence_time': 0.005}

    sh = ScanManagerBase(setupInfo=setupInfoBasic)
    fullsig, _ = sh.makeFullScan(stageParameters, TTLParameters)

    z = fullsig['scanSignalsDict']['Z']
    # 25 um before scan + 5 um Center offset => first pixel at 30 um.
    assert np.isclose(z[0], 3.0)
    # Both anchor modes restore the same pre-scan position after completion.
    assert np.isclose(z[-1], 2.5)


def test_beta_start_anchor_requires_runtime_position_snapshot():
    stageParameters = _stage_parameters(z_center=0)
    stageParameters['position_anchor'] = 'start'
    TTLParameters = {'target_device': ['405', '488'],
                     'TTL_start': [[0.0001, 0.004], [0, 0]],
                     'TTL_end': [[0.0015, 0.005], [0, 0]],
                     'sequence_time': 0.005}

    sh = ScanManagerBase(setupInfo=setupInfoBasic)
    with pytest.raises(ValueError, match='axis_position_before_scan'):
        sh.makeFullScan(stageParameters, TTLParameters)


def test_beta_center_offsets_use_each_axis_conversion_factor():
    stageParameters = _stage_parameters(z_center=0)
    x_conversion = setupInfoBasic.positioners['X'].managerProperties['conversionFactor']
    stageParameters['axis_centerpos'][0] = x_conversion  # exactly +1 V offset on X
    stageParameters['axis_position_before_scan'] = [[0], [0], [25]]
    TTLParameters = {'target_device': ['405', '488'],
                     'TTL_start': [[0.0001, 0.004], [0, 0]],
                     'TTL_end': [[0.0015, 0.005], [0, 0]],
                     'sequence_time': 0.005}

    sh = ScanManagerBase(setupInfo=setupInfoBasic)
    fullsig, _ = sh.makeFullScan(stageParameters, TTLParameters)

    x = fullsig['scanSignalsDict']['X']
    x_step_v = stageParameters['axis_step_size'][0] / x_conversion
    expected_first_pixel_v = 1.0 - 2 * x_step_v
    assert np.isclose(x[0], expected_first_pixel_v)
    assert np.isclose(x[-1], 0.0)

# Copyright (C) 2020-2021 ImSwitch developers
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
