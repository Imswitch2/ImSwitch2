"""A scan plays a laser's power level through the calibration in force on the
laser's manager (feat/replay-settings x feat/scan-engines, on dev).

The calibration is the manager's (``powerCalibration``: whatever its
``calibrationId`` / ``calibCsvPath`` resolved to, or a swap made in the
Calibrations window), not a second reading of ``calibCsvPath``; under it a
level is a percentage, 0-100 %, and one outside the table is refused rather
than turned into NaN. A laser on raw units plays volts within its range. A
mock standing in for a device that failed at startup is not a replay target.
"""
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.model.calibrations import resolve_laser_calibration
from imswitch.imcontrol.model.devices.status import DeviceConnectionState, DeviceRuntimeMode
from imswitch.imcontrol.model.managers.lasers._power_volts import level_volts
from imswitch.imcontrol.model.managers.scan_engines.NidaqScanEngine import NidaqScanEngine
from imswitch.imcontrol.model.scan_program.engine import ScanEngineRefused, setup_channels
from imswitch.imcontrol.model import replay
from imswitch.imcontrol.model.replay_inventory import unavailableDevices

from .test_scan_program_power_levels import _program, _setup


def _table(tmp_path, rows='0 0\n1 10\n2 30\n4 100\n', name='calib.csv'):
    path = tmp_path / name
    path.write_text(rows)
    resolution = resolve_laser_calibration(laser_name='L561', drive_unit='V',
                                           calib_csv_path=str(path))
    assert resolution.table is not None, resolution.error
    return resolution.table


def _compile(setup, program, lookup):
    engine = NidaqScanEngine(None, nidaqManager=None, setupInfo=setup,
                             laserPowerCalibration=lookup)
    return engine.compile(program, setup_channels(setup), captured_um={'RO': 0.0})


def test_the_engine_uses_the_managers_calibration_not_the_setup_file(tmp_path):
    # The setup names a file mapping 30 % to 1 V; the manager has another
    # calibration in force (swapped in the Calibrations window), mapping
    # 30 % to 2 V. The scan follows the manager.
    swapped = _table(tmp_path)
    other = tmp_path / 'other.csv'
    other.write_text('0 0\n1 30\n4 100\n')
    setup = _setup(calibCsvPath=str(other))
    volts = _compile(setup, _program(value=30.0), lambda name: swapped) \
        .signalDict['scanSignalsDict']['L561']
    assert set(np.unique(volts)) == {0.0, 2.0}


def test_a_laser_whose_calibration_did_not_load_plays_volts_in_its_range(tmp_path):
    # The manager runs on volts (its calibration failed to load, or it is on
    # raw units): 30 is then 30 V, outside the laser's 0-5 V, and refused.
    setup = _setup(calibCsvPath=str(tmp_path / 'missing.csv'))
    with pytest.raises(ScanEngineRefused, match='L561 takes 0 to 5 V; the scan asks 0 to 30'):
        _compile(setup, _program(value=30.0), lambda name: None)
    volts = _compile(setup, _program(value=2.5), lambda name: None) \
        .signalDict['scanSignalsDict']['L561']
    assert set(np.unique(volts)) == {0.0, 2.5}


def test_under_a_calibration_the_range_is_percent_and_outside_it_is_refused(tmp_path):
    table = _table(tmp_path)
    info = SimpleNamespace(valueRangeMin=0, valueRangeMax=5, managerProperties={})
    volts, refusal = level_volts('L561', info, [30.0, 0.0], calibration=table)
    assert refusal == '' and np.allclose(volts, [2.0, 0.0])
    volts, refusal = level_volts('L561', info, [150.0], calibration=table)
    assert volts is None and refusal == 'L561 takes 0 to 100 %; the scan asks 150 to 150.'
    # 100 % is reachable (the shared table's rule), never NaN.
    volts, refusal = level_volts('L561', info, [100.0], calibration=table)
    assert refusal == '' and np.allclose(volts, [4.0])


def test_the_master_hands_the_engines_each_lasers_calibration_in_force(tmp_path):
    from imswitch.imcontrol.controller.MasterController import MasterController

    table = _table(tmp_path)
    calibrated = SimpleNamespace(powerCalibration=table)
    master = MasterController.__new__(MasterController)
    master.lasersManager = {'L561': calibrated, 'L488': SimpleNamespace(powerCalibration=None)}
    assert MasterController._laserPowerCalibration(master, 'L561') is table
    assert MasterController._laserPowerCalibration(master, 'L488') is None
    # a laser that failed at startup has no manager: raw units, range-checked
    assert MasterController._laserPowerCalibration(master, 'gone') is None


class _Detector:
    def __init__(self, mode, state):
        self.runtimeMode, self.connectionState = mode, state
        self.isUsable = True


def test_a_mock_standing_in_for_a_failed_camera_is_not_a_replay_target():
    master = SimpleNamespace(
        lasersManager={}, positionersManager={}, rotatorsManager={},
        detectorsManager=_Iter({
            'Fallback': _Detector(DeviceRuntimeMode.MOCK, DeviceConnectionState.ERROR),
            'Configured': _Detector(DeviceRuntimeMode.MOCK, DeviceConnectionState.CONNECTED),
        }))
    assert unavailableDevices(master) == {(replay.DETECTOR, 'Fallback')}


class _Iter(dict):
    """A manager dict that iterates as (name, manager), like MultiManager."""

    def __iter__(self):
        return iter(list(self.items()))
