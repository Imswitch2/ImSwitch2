"""Firmware scans are checked against the axis's declared DAC range.

The TriggerScope ramps its DAC autonomously from wherever the axis is parked,
and the excursion length uploaded to it was never compared with the axis's
``minVolt``/``maxVolt`` -- the same limits every jog through the GUI is
clamped to.
"""

from types import SimpleNamespace

import pytest

from imswitch.imcontrol.controller.controllers._triggerscope_scan_geometry import (
    check_dac_range, check_firmware_scan_dac_ranges,
)


def _positioners(**ranges):
    return {
        name: SimpleNamespace(managerProperties={'conversionFactor': 17.44, **props})
        for name, props in ranges.items()
    }


def test_an_excursion_past_the_declared_range_is_refused():
    positioners = _positioners(Galvo={'minVolt': -10, 'maxVolt': 10})
    check_dac_range(positioners, 'Galvo', (0.0, 9.9), what='Raster scan')
    with pytest.raises(ValueError, match=r'"Galvo" to 11\.460 V, outside its declared \[-10, 10\] V'):
        check_dac_range(positioners, 'Galvo', (0.0, 11.46), what='Raster scan')


def test_an_undeclared_range_falls_back_to_the_manager_default():
    positioners = _positioners(Piezo={})
    check_dac_range(positioners, 'Piezo', (-10.0, 10.0), what='scan')
    with pytest.raises(ValueError, match=r'\[-10, 10\]'):
        check_dac_range(positioners, 'Piezo', (10.5,), what='scan')


def test_resolft_modes_check_the_far_end_of_every_ramp():
    positioners = _positioners(RO={'minVolt': 0, 'maxVolt': 5}, Galvo={'minVolt': -1, 'maxVolt': 1})
    devices = {'roScanDevice': 'RO', 'galvoScanDevice': 'Galvo'}
    ok = {
        'roRestingV': 0.5, 'roStartV': 1.0, 'roStepSizeV': 0.1, 'roSteps': 20,
        'cycleStartV': 1.0, 'cycleStepSizeV': 0.2, 'cycleSteps': 10,
        'galvoFirstPositionV': -0.5, 'galvoSecondPositionV': 0.0, 'galvoThirdPositionV': 0.5,
    }
    check_firmware_scan_dac_ranges(positioners, devices, ok, what='pLS-RESOLFT scan')

    too_far = dict(ok, roSteps=45)  # 1.0 + 0.1 * 45 = 5.5 V on a 5 V axis
    with pytest.raises(ValueError, match=r'"RO" to 5\.500 V'):
        check_firmware_scan_dac_ranges(positioners, devices, too_far, what='pLS-RESOLFT scan')

    galvo_out = dict(ok, galvoThirdPositionV=1.2)
    with pytest.raises(ValueError, match=r'"Galvo" to 1\.200 V'):
        check_firmware_scan_dac_ranges(positioners, devices, galvo_out, what='galvo-detection scan')


def test_a_mode_without_a_device_role_skips_that_role():
    positioners = _positioners(RO={'minVolt': 0, 'maxVolt': 5})
    check_firmware_scan_dac_ranges(
        positioners, {'roScanDevice': 'RO'},
        {'roRestingV': 1.0, 'roStartV': 1.0, 'roStepSizeV': 0.1, 'roSteps': 10,
         'galvoFirstPositionV': 99.0},
        what='scan',
    )
