"""A laser's power level inside a step (docs/design/plans/
scan-engines-and-expert-scan.md §7.8).

A ``level`` event was reported native on the NI-DAQ and then not played at
all: the densified scan had no place for it. It now plays on the laser's
analog channel, converted to volts as the laser is set by hand (its
calibration, or the value as the voltage); a protocol board plays it on its
DAC (test_step_protocol.py).
"""
import json

import numpy as np
import pytest

from imswitch.imcontrol.model import SetupInfo
from imswitch.imcontrol.model.managers.scan_engines.NidaqScanEngine import NidaqScanEngine
from imswitch.imcontrol.model.scan_program import (
    DeviceDrive, Loop, Motion, ScanProgram, TimelineEvent, expand, realize,
)
from imswitch.imcontrol.model.scan_program.densify import densify
from imswitch.imcontrol.model.scan_program.engine import ScanEngineRefused, setup_channels

RATE = 100_000
JUMP = Motion(transition='jump', move_s=1e-4, settle_s=1e-4, return_s=2e-4,
              origin='absolute', restore='first')


def _setup(**laser):
    return SetupInfo.from_json(json.dumps({
        'positioners': {'RO': {
            'managerName': 'NidaqPositionerManager', 'analogChannel': 'Dev1/ao0',
            'managerProperties': {'conversionFactor': 2.0, 'minVolt': -10, 'maxVolt': 10},
            'forScanning': True, 'forPositioning': True, 'axes': ['X']}},
        'lasers': {'L561': {'managerName': 'NidaqLaserManager', 'digitalLine': None,
                            'analogChannel': 'Dev1/ao3', 'wavelength': 561,
                            'valueRangeMin': 0, 'valueRangeMax': 5,
                            'managerProperties': dict(laser)}},
        'scan': {'scanWidgetType': 'Expert', 'sampleRate': RATE, 'scanDesigner': None,
                 'scanDesignerParams': {}, 'TTLCycleDesigner': None,
                 'TTLCycleDesignerParams': {}},
        'nidaq': {'simulation': True, 'timerCounterChannel': None, 'startTrigger': False},
    }), infer_missing=True)


def _program(value=2.5, shape='hold'):
    """RO steps 3 positions; L561 at ``value`` from 0.5 to 1.5 ms of each
    2 ms step."""
    return ScanProgram(
        loops=(Loop('x', 'scan_x', 3),),
        drives=(DeviceDrive('RO', weights_um={'x': 0.5}, motion=JUMP),),
        step_s=2e-3,
        timeline=(TimelineEvent('L561', 'level', 0.5e-3, 1.5e-3, value=value, shape=shape,
                                during='any'),),
        timing_policy='strict-v1')


def _compile(setup, program):
    engine = NidaqScanEngine(None, nidaqManager=None, setupInfo=setup)
    return engine.compile(program, setup_channels(setup), captured_um={'RO': 0.0})


def test_densify_gives_the_level_over_its_event_and_off_elsewhere():
    schedule = realize(expand(_program()), RATE)
    level = densify(schedule)['level']['L561']
    step = 200                                      # 2 ms at 100 kHz
    first = level[:step]
    assert set(first[50:150]) == {2.5} and not first[:50].any() and not first[150:].any()
    assert np.count_nonzero(level) == 3 * 100


def test_the_nidaq_plays_a_power_level_on_the_lasers_analog_channel():
    compiled = _compile(_setup(), _program())
    signals = compiled.signalDict['scanSignalsDict']
    assert set(signals) == {'RO', 'L561'}
    volts = signals['L561']
    assert volts.size == signals['RO'].size
    assert set(np.unique(volts)) == {0.0, 2.5}      # no calibration: the value is volts
    # the voltage ranges stay the positioners' (one per scanned axis)
    assert len(compiled.scanInfoDict['minmaxes']) == 1


def test_a_calibrated_laser_is_driven_at_its_calibrated_volts(tmp_path):
    calibration = tmp_path / 'calib.csv'
    calibration.write_text('0 0\n1 10\n2 30\n4 100\n')    # volts, measured power
    setup = _setup(calibCsvPath=str(calibration))
    setup.lasers['L561'] = setup.lasers['L561'].__class__(
        **{**setup.lasers['L561'].__dict__, 'valueRangeMax': 100})
    volts = _compile(setup, _program(value=30.0)).signalDict['scanSignalsDict']['L561']
    assert set(np.unique(volts)) == {0.0, 2.0}      # 30 % of the range is 2 V


@pytest.mark.parametrize('program, message', [
    (_program(value=6.0), 'L561 takes 0 to 5 V; the scan asks 0 to 6'),
    (_program(shape='ramp'), 'A ramped power level of L561 is not played'),
])
def test_a_level_it_cannot_play_is_refused_with_the_reason(program, message):
    with pytest.raises(ScanEngineRefused, match=message):
        _compile(_setup(), program)


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
