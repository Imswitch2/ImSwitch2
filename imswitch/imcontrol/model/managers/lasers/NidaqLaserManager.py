from .LaserManager import LaserManager, RawDriveError
from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.calibrations import CalibrationRangeError


class NidaqLaserManager(LaserManager):
    """ LaserManager for analog-value NI-DAQ-controlled lasers.

    Manager properties:

    - ``calibCsvPath`` -- optional power calibration file (volts in column 0,
      measured power in column 1); the value is then a 0-100 % setpoint
    - ``calibrationId`` -- optional id of a calibration in the global store
      (``<ImSwitchConfig>/calibrations``); takes precedence over
      ``calibCsvPath``. ``"raw"`` runs the laser on volts even when
      ``calibCsvPath`` is set
    """

    supportsPowerCalibration = True

    def __init__(self, laserInfo, name, **lowLevelManagers):

        self._nidaqManager = lowLevelManagers['nidaqManager']
        if getattr(self._nidaqManager, 'isSimulated', False):
            self._setMockActive("Simulated NI-DAQ laser backend")
        self.__logger = initLogger(self, tryInheritParent=True)

        getDigitalLine = getattr(laserInfo, 'getDigitalLine', None)
        #: Without a digital line, emission has no switch (analog only).
        self._hasEmissionSwitch = not callable(getDigitalLine) or getDigitalLine() is not None
        self._analogChannel = laserInfo.getAnalogChannel()
        # A calibration that does not load leaves the laser on volts, and the
        # Laser panel then says so (it shows % only while one is in force).
        self._resolvePowerCalibration(
            name, 'V',
            calibCsvPath=laserInfo.managerProperties.get("calibCsvPath"),
            calibrationId=laserInfo.managerProperties.get("calibrationId"),
            wavelength=getattr(laserInfo, 'wavelength', None),
            logger=self.__logger,
        )

        super().__init__(laserInfo, name, isBinary=self._analogChannel is None,
                         valueUnits='V', valueDecimals=2)

    def calibrationDeviceIdentifier(self):
        return f'analog {self._analogChannel}' if self._analogChannel is not None else ''

    def setEnabled(self, enabled):
        try:
            succeeded = self._nidaqManager.setDigital(self.name, enabled)
            return succeeded is not False
        except Exception as e:
            self.__logger.error(f"Error trying to enable laser: {e}")
            return False

    def setValue(self, val, enabled=True, for_scanning=False):
        if self.isBinary:
            return
        calibration = self.powerCalibration
        if for_scanning and not enabled:
            voltage = 0
        elif calibration is not None:
            try:
                voltage = calibration.drive_for_percent(val)
            except CalibrationRangeError as exc:
                # Refused, not passed on: the DAQ would get NaN (or a clamp).
                self.__logger.error(f"Laser {self.name}: {exc}; nothing sent.")
                return False
        else:
            voltage = val
        try:
            succeeded = self._nidaqManager.setAnalog(
                target=self.name, voltage=voltage,
                min_val=self.valueRangeMin, max_val=self.valueRangeMax
            )
            return succeeded is not False
        except Exception as e:
            self.__logger.error(f"Error trying to set value to laser: {e}")
            return False

    supportsRawDrive = True

    def applyRawDrive(self, value):
        """ Analog output voltage, bypassing the calibration LUT; raises on
        any DAQ failure instead of logging it. """
        if self.isBinary:
            raise RawDriveError(f'{self.name} is binary: it has no analog drive')
        voltage = float(value)
        if not (self.valueRangeMin <= voltage <= self.valueRangeMax):
            raise RawDriveError(
                f'{self.name}: {voltage:g} V outside '
                f'[{self.valueRangeMin:g}, {self.valueRangeMax:g}] V')
        try:
            ok = self._nidaqManager.setAnalog(
                target=self.name, voltage=voltage,
                min_val=self.valueRangeMin, max_val=self.valueRangeMax,
                raise_on_error=True,
            )
        except Exception as exc:
            raise RawDriveError(f'{self.name}: analog write failed: {exc}') from exc
        if ok is False:
            raise RawDriveError(f'{self.name}: analog write failed')
        return voltage

    def applyEnabled(self, enabled):
        """ Digital emission line; raises on any DAQ failure. ``False`` when
        the laser has no digital line (analog only: nothing to switch). """
        if not self._hasEmissionSwitch:
            return False
        try:
            ok = self._nidaqManager.setDigital(self.name, bool(enabled), raise_on_error=True)
        except Exception as exc:
            raise RawDriveError(
                f'{self.name}: switching emission {"on" if enabled else "off"} failed: '
                f'{exc}') from exc
        if ok is False:
            raise RawDriveError(
                f'{self.name}: switching emission {"on" if enabled else "off"} failed')
        return True

    def applyValue(self, value):
        """ The UI value through the calibration in force (if any); raises
        on a value the calibration refuses or on any DAQ failure. """
        calibration = self.powerCalibration
        if calibration is None:
            return self.applyRawDrive(value)
        try:
            voltage = calibration.drive_for_percent(value)
        except CalibrationRangeError as exc:
            raise RawDriveError(f'{self.name}: {exc}') from exc
        return self.applyRawDrive(voltage)

    def setScanModeActive(self, active):
        if active:
            self.setEnabled(False)
        else:
            # Scan end: zero the output and leave the laser off. The scan
            # module owns laser emission; the manager must never force ON.
            self.setValue(0)

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
