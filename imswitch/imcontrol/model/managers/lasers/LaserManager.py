import ast
import json
from abc import ABC, abstractmethod
from typing import Optional, Union

from imswitch.imcontrol.model.calibrations import (
    CalibrationInvalid,
    CalibrationRangeError,
    CalibrationResolution,
    LaserPowerTable,
    resolve_laser_calibration,
)
from imswitch.imcontrol.model.devices.status import DeviceManagerStatusMixin


def normalise_ports(value) -> list:
    """Return *value* as a list of port strings.

    Accepts:
      - A proper Python list                    ['COM3']
      - A JSON array string                     '["COM3"]'
      - A Python repr list/tuple string         "['COM3']"  (old config-editor bug)
      - A plain comma-separated string          'COM3,COM4'
      - A bare string                           'COM3'
    """
    if isinstance(value, list):
        return value
    if not isinstance(value, str):
        return [str(value)]
    s = value.strip()
    # JSON array: ["COM3"]
    try:
        result = json.loads(s)
        if isinstance(result, list):
            return [str(p) for p in result]
        return [str(result)]
    except (json.JSONDecodeError, ValueError):
        pass
    # Python repr list/tuple: ['COM3'] or ('COM3',)
    try:
        result = ast.literal_eval(s)
        if isinstance(result, (list, tuple)):
            return [str(p) for p in result]
        return [str(result)]
    except (ValueError, SyntaxError):
        pass
    # Comma-separated or bare string
    return [p.strip() for p in s.split(',') if p.strip()]


#: ``CalibrationResolution.requested`` of a manager that does not apply
#: calibrations although its setup names a ``calibCsvPath``.
UNSUPPORTED_CALIB_CSV_PATH = 'calibCsvPath (not applied by this manager)'


class RawDriveError(RuntimeError):
    """A raw-drive command did not reach the hardware."""


class LaserManager(DeviceManagerStatusMixin, ABC):
    """ Abstract base class for managers that control lasers. Each type of
    laser corresponds to a manager derived from this class. """

    #: Mutating commands admitted through the resource registry
    #: (``imcontrol/model/resources.py``): refused while another owner
    #: reserves this device; ``owner=<token>`` passes a reservation.
    _GUARDED_METHODS = ('setValue', 'setEnabled', 'applyRawDrive', 'applyEnabled', 'applyValue',
                        'setPowerCalibration')

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        from imswitch.imcontrol.model.resources import guard_methods, laser_key
        guard_methods(cls, _resource_key_for(laser_key), cls._GUARDED_METHODS)

    @abstractmethod
    def __init__(self, laserInfo, name: str, isBinary: bool, valueUnits: str,
                 valueDecimals: int, isModulated: bool = False) -> None:
        """
        Args:
            laserInfo: See setup file documentation.
            name: The unique name that the device is identified with in the
              setup file.
            isBinary: Whether the laser can only be turned on and off, and its
              value cannot be changed.
            valueUnits: The units of the laser value, e.g. "mW" or "V".
            valueDecimals: How many decimals are accepted in the laser value.
            isModulated: Whether the laser can be frequency modulated.
        """
        self._laserInfo = laserInfo
        self.__name = name
        self.__isBinary = isBinary
        self.__wavelength = laserInfo.wavelength
        self.__valueRangeMin = laserInfo.valueRangeMin
        self.__valueRangeMax = laserInfo.valueRangeMax
        self.__valueRangeStep = laserInfo.valueRangeStep
        self.__valueUnits = valueUnits
        self.__valueDecimals = valueDecimals
        self.__isModulated = isModulated
        if isModulated:
            self.__freqRangeMin = laserInfo.freqRangeMin
            self.__freqRangeMax = laserInfo.freqRangeMax
            self.__freqRangeInit = laserInfo.freqRangeInit
        else:
            self.__freqRangeMin = None
            self.__freqRangeMax = None
            self.__freqRangeInit = None

    @property
    def name(self) -> str:
        """ Unique laser name, defined in the laser's setup info. """
        return self.__name

    @property
    def isBinary(self) -> bool:
        """ Whether the laser can only be turned on and off, and its value
        cannot be changed. """
        return self.__isBinary

    @property
    def wavelength(self) -> int:
        """ The wavelength of the laser. """
        return self.__wavelength

    @property
    def valueRangeMin(self) -> float:
        """ The minimum value that the laser can be set to. """
        return self.__valueRangeMin

    @property
    def valueRangeMax(self) -> float:
        """ The maximum value that the laser can be set to. """
        return self.__valueRangeMax

    @property
    def valueRangeStep(self) -> float:
        """ The default step size of the value range that the laser can be set
        to. """
        return self.__valueRangeStep

    @property
    def valueUnits(self) -> str:
        """ The units of the laser value, e.g. "mW" or "V"; ``"%"`` while a
        power calibration is in force (:meth:`usesCalibrationLookup`). """
        return '%' if self.usesCalibrationLookup() else self.__valueUnits

    @property
    def rawValueUnits(self) -> str:
        """ The units of the raw drive (what the value means without a
        calibration), whatever is in force. """
        return self.__valueUnits

    @property
    def valueDecimals(self):
        """ How many decimals are accepted in the laser value. """
        return self.__valueDecimals
    
    @property
    def isModulated(self) -> bool:
        """ Whether the laser supports frequency modulation."""
        return self.__isModulated

    @property
    def freqRangeMin(self) -> int:
        """ The minimum frequency of the laser modulation. """
        return self.__freqRangeMin
    
    @property
    def freqRangeMax(self) -> int:
        """ The minimum frequency of the laser modulation. """
        return self.__freqRangeMax
    
    @property
    def freqRangeInit(self) -> int:
        """ The initial frequency of the laser modulation. """
        return self.__freqRangeInit

    def hasProperty(self, key: str) -> bool:
        """ Whether ``key`` is present in this laser's managerProperties.

        Public accessor so callers do not reach into ``manager._laserInfo``. """
        return key in (self._laserInfo.managerProperties or {})

    def getProperty(self, key: str, default=None):
        """ Return the managerProperty ``key`` (or ``default``). Public accessor
        so callers do not read ``manager._laserInfo.managerProperties`` directly. """
        return (self._laserInfo.managerProperties or {}).get(key, default)

    # -- power calibration (docs/devices/lasers.rst, Calibrations) ---------

    #: True for managers that convert a percent setpoint into their raw drive
    #: through a power calibration before their own set call. Every other
    #: manager runs on raw units; a calibration its setup names is reported
    #: as not applied (:meth:`powerCalibrationResolution`).
    supportsPowerCalibration: bool = False

    def _resolvePowerCalibration(self, name, rawUnits, *, calibCsvPath=None,
                                 calibrationId=None, wavelength=None, logger=None):
        """ Resolve the setup's ``calibrationId`` / ``calibCsvPath`` once at
        construction (managers call this before ``super().__init__``). A
        referenced calibration that cannot be used leaves the laser on raw
        units and is reported, never half-applied. """
        resolution = resolve_laser_calibration(
            laser_name=name, drive_unit=rawUnits, calibration_id=calibrationId,
            calib_csv_path=calibCsvPath, wavelength_nm=wavelength)
        self._powerCalibrationResolution = resolution
        if resolution.failed and logger is not None:
            logger.error(f'Laser {name}: the calibration the setup names cannot be used '
                         f'({resolution.error}); running on raw units ({rawUnits}).')
        return resolution

    def powerCalibrationResolution(self) -> CalibrationResolution:
        """ What this laser runs on: ``state`` ``'calibrated'`` (``table``
        in force), ``'raw'``, or ``'failed'`` (a calibration was referenced
        and is not applied; ``error`` says why). """
        resolution = self.__dict__.get('_powerCalibrationResolution')
        if resolution is not None:
            return resolution
        # Managers that do not apply calibrations run on raw units. A
        # calibCsvPath their setup names is flagged here (``requested``) and
        # reported by the Laser panel when it names a file; it no longer flips
        # the panel to percent. Only presence is read in the manager, as
        # before, so these managers' config-editor schemas do not change.
        if self.hasProperty("calibCsvPath"):
            return CalibrationResolution('raw', requested=UNSUPPORTED_CALIB_CSV_PATH)
        return CalibrationResolution('raw')

    @property
    def powerCalibration(self) -> Optional[LaserPowerTable]:
        """ The power calibration in force, or None (raw units). """
        resolution = self.__dict__.get('_powerCalibrationResolution')
        return resolution.table if resolution is not None else None

    def usesCalibrationLookup(self) -> bool:
        """ Whether a power calibration is in force -- then the value is a
        0-100 %% setpoint, otherwise it is the raw drive in
        :attr:`rawValueUnits`. True only when a calibration actually loaded,
        never merely because the setup names one. """
        return self.powerCalibration is not None

    def setPowerCalibration(self, table: Optional[LaserPowerTable], *, requested: str = '',
                            resolvedPath: str = '') -> None:
        """ Put ``table`` in force (None: raw units). Nothing is sent to
        the hardware: the caller decides what to do with the current value
        (``LaserController.setLaserCalibration``). """
        if not self.supportsPowerCalibration:
            raise CalibrationInvalid(
                f'{type(self).__name__} does not apply power calibrations')
        if table is not None and table.driveUnit and table.driveUnit != self.__valueUnits:
            raise CalibrationInvalid(
                f'{table.id} maps a drive in {table.driveUnit!r}; {self.name} is driven in '
                f'{self.__valueUnits!r}')
        self._powerCalibrationResolution = CalibrationResolution(
            'calibrated' if table is not None else 'raw', table=table,
            requested=requested, resolvedPath=resolvedPath)

    def calibrationDeviceIdentifier(self) -> str:
        """ A hardware identifier (serial, channel) a calibration can be
        matched against; '' when the manager has none. """
        return ''

    def driveForValue(self, value) -> Optional[float]:
        """ The raw drive a value corresponds to under the calibration in
        force (the value itself on raw units); None when it has none. """
        table = self.powerCalibration
        try:
            if table is None:
                return float(value)
            return table.drive_for_percent(value)
        except (CalibrationRangeError, TypeError, ValueError):
            return None

    #: True when :meth:`applyRawDrive` is implemented (audited for runs).
    supportsRawDrive: bool = False

    def applyRawDrive(self, value: float) -> float:
        """ Set the raw drive (volts, AOTF amplitude, ...) directly, bypassing
        any calibration lookup table, and return the raw value applied.

        Meant for calibration runs (``docs/design/plans/
        transient-instruments-step-scans.md`` §7.4): unlike :meth:`setValue`,
        a failure is never only logged — it raises :class:`RawDriveError`. """
        raise NotImplementedError(f'{type(self).__name__} has no raw-drive command')

    def applyEnabled(self, enabled: bool) -> bool:
        """ Switch emission like :meth:`setEnabled`, but raise
        :class:`RawDriveError` on any failure instead of logging it.

        Returns ``False`` when the laser has no emission switch at all (the
        command is not sent), ``True`` when the switch was set. Implemented
        with :meth:`applyRawDrive` (``supportsRawDrive``). """
        raise NotImplementedError(f'{type(self).__name__} has no checked enable command')

    def applyValue(self, value: Union[int, float]) -> float:
        """ Set the UI value (through the calibration lookup if one is
        loaded) like :meth:`setValue`, but raise :class:`RawDriveError` on any
        failure; returns the raw drive applied. """
        raise NotImplementedError(f'{type(self).__name__} has no checked value command')

    @abstractmethod
    def setEnabled(self, enabled: bool) -> None:
        """ Sets whether the laser is enabled. """
        pass

    @abstractmethod
    def setValue(self, value: Union[int, float], enabled=True, for_scanning=False) -> None:
        """ Sets the value of the laser. """
        pass

    def setModulationEnabled(self, enabled: bool) -> None:
        """ Sets wether the laser frequency modulation is enabled. """
        pass

    def setModulationFrequency(self, frequency: int) -> None:
        """ Sets the laser modulation frequency. """
        pass
    
    def setModulationDutyCycle(self, dutyCycle: int) -> None:
        """ Sets the laser modulation duty cycle. """

    def setScanModeActive(self, active: bool) -> None:
        """ Sets whether the laser should be in scan mode (if the laser
        supports it). The scan module's TTL device list is the sole
        authority for which lasers emit during a scan. """
        pass

    def finalize(self) -> None:
        """ Close/cleanup laser. """
        pass


def _resource_key_for(key_of_name):
    def key(manager):
        try:
            name = manager.name
        except Exception:
            name = f'{type(manager).__name__}@{id(manager):x}'
        return key_of_name(name)
    return key


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
