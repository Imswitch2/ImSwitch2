"""A small no-hardware rig for the calibration tests: real laser managers on
fake transports, a real LaserController with a real LaserWidget, a throwaway
calibration store and a setup file on disk."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from imswitch.imcommon.model import SharedAttributes
from imswitch.imcontrol.model.calibrations import (
    CalibrationStore,
    DeviceIdentity,
    parse_legacy_csv,
)
from imswitch.imcontrol.model.managers.lasers.LaserManager import LaserManager


class RecordingDaq:
    """NI-DAQ writes as NidaqManager would make them."""

    def __init__(self):
        self.analog = []
        self.digital = []

    def setAnalog(self, target, voltage, min_val, max_val, raise_on_error=False):
        self.analog.append(float(voltage))
        return True

    def setDigital(self, target, enabled, raise_on_error=False):
        self.digital.append(bool(enabled))
        return True


class RawOnlyLaserManager(LaserManager):
    """Stands for the ~18 managers that declare ``calibCsvPath`` but never
    apply it (Cobolt, CoolLED, Oxxius, ...)."""

    def __init__(self, laserInfo, name, **_):
        self.values = []
        super().__init__(laserInfo, name, isBinary=False, valueUnits='mW', valueDecimals=1)

    def setEnabled(self, enabled):
        return True

    def setValue(self, value, enabled=True, for_scanning=False):
        self.values.append(value)
        return True


class Group:
    def __init__(self, managers):
        self._managers = dict(managers)

    def __iter__(self):
        return iter(self._managers.items())

    def __getitem__(self, name):
        return self._managers[name]

    def __contains__(self, name):
        return name in self._managers

    def getAllDeviceNames(self):
        return list(self._managers)

    def execOnAll(self, *args, **kwargs):
        pass


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot, *args, **kwargs):
        self.slots.append(slot)


def laser_info(**managerProperties):
    return SimpleNamespace(
        managerProperties=dict(managerProperties), wavelength=488, valueRangeMin=0.0,
        valueRangeMax=5.0, valueRangeStep=0.01, freqRangeMin=0, freqRangeMax=0,
        freqRangeInit=0, getAnalogChannel=lambda: 'ao0', getDigitalLine=lambda: 0,
        powerDevice=None)


def nidaq(name='488', **managerProperties):
    from imswitch.imcontrol.model.managers.lasers.NidaqLaserManager import NidaqLaserManager

    daq = RecordingDaq()
    return NidaqLaserManager(laser_info(**managerProperties), name, nidaqManager=daq), daq


CURVE_W = """# laser: 488
# setting unit: V
# laser wavelength nm: 488
# plane: back focal plane
# meter: PM100D P0011748
# created: 2026-11-20T10:00:00+01:00
# notes: ND 1.0 removed
# columns: setting power_W
0 0.0
1 0.002
2 0.006
4 0.010
5 0.0124
"""


def write_csv(folder, text=CURVE_W, name='488.calib.csv') -> str:
    path = Path(folder) / name
    path.write_text(text)
    return str(path)


def store_record(store: CalibrationStore, text=CURVE_W, **device):
    record = parse_legacy_csv(text)
    if device:
        record = record.__class__(**{**record.__dict__, 'device': DeviceIdentity(
            **{**record.device.__dict__, **device})})
    return store.add(record)


def write_setup(folder, lasers) -> str:
    path = Path(folder) / 'setup.json'
    path.write_text(json.dumps({
        'lasers': {name: {'managerName': type(m).__name__,
                          'managerProperties': dict(m._laserInfo.managerProperties),
                          'wavelength': 488, 'valueRangeMin': 0, 'valueRangeMax': 5}
                   for name, m in lasers.items()},
        'laserPresets': {},
        'availableWidgets': ['Laser'],
        '// note': 'kept as it is',
    }, indent=4))
    return str(path)


def laser_controller(managers, qtbot=None):
    """A real LaserController + LaserWidget over ``managers``."""
    from imswitch.imcontrol.controller.controllers.LaserController import LaserController
    from imswitch.imcontrol.view.widgets.LaserWidget import LaserWidget

    widget = LaserWidget(None)
    if qtbot is not None:
        qtbot.addWidget(widget)
    setupInfo = SimpleNamespace(
        lasers={name: m._laserInfo for name, m in managers.items()}, laserPresets={},
        deviceCategorySettings={'lasers': {'powerMode': 'coupled'}})
    setupInfo.setLaserPreset = lambda name, payload: setupInfo.laserPresets.__setitem__(
        name, payload)
    commChannel = SimpleNamespace(
        sharedAttrs=SharedAttributes(), sigScanStarting=_Signal(),
        sigScanDevicesResolved=_Signal(), sigScanDone=_Signal(), sigScanEnded=_Signal())
    master = SimpleNamespace(lasersManager=Group(managers))
    controller = LaserController(setupInfo, commChannel, master, widget=widget, factory=None,
                                 moduleCommChannel=None)
    return controller, widget


def drive_values(daq):
    return np.asarray(daq.analog)
