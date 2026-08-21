import os

import numpy as np

from imswitch.imcontrol._test import setupInfoBasic
from imswitch.imcontrol.model import ScanManagerBase


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
    assert sh.makeFullScan(stageParameters, TTLParameters) is None

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
