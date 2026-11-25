from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.calibrations import CalibrationRangeError
from .LaserManager import LaserManager


class StepBoardLaserManager(LaserManager):
    """ LaserManager for a laser on a step board: a board that speaks the
    step-engine protocol (``StepProtocolScanEngine``,
    docs/devices/step-engine-protocol.rst). Its ``digitalLine`` is the
    board's ``<engine>/ttl<j>`` and, for a laser with a power control, its
    ``analogChannel`` the board's ``<engine>/dac<i>``. In a scan the engine
    switches it; by hand this manager does (the protocol's ``SET``), between
    runs.

    On and off follow ``ttlActiveLevel``. The power is converted to volts as
    a scan converts it: through the calibration ``calibCsvPath`` names (a
    percentage, through the same calibration table the NI-DAQ lasers use) or,
    without one, the value is the voltage.

    Manager properties:

    - ``calibCsvPath`` -- the power calibration: volts and the power they
      give, one pair per row (optional)
    """

    def __init__(self, laserInfo, name, **lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)
        self._engineNamed = lowLevelManagers.get('scanEngineNamed')
        self._line = laserInfo.getDigitalLine()
        self._analog = laserInfo.getAnalogChannel()
        self._level = laserInfo.ttlLevel if laserInfo.ttlActiveLevel else (lambda on: on)
        # The calibration in force is this manager's (the scan engines play
        # its power levels through it, MasterController._laserPowerCalibration);
        # one that does not load leaves the laser on volts, and says so.
        self._resolvePowerCalibration(
            name, 'V',
            calibCsvPath=(laserInfo.managerProperties or {}).get('calibCsvPath'),
            wavelength=getattr(laserInfo, 'wavelength', None),
            logger=self.__logger,
        )
        super().__init__(laserInfo, name, isBinary=self._analog is None,
                         valueUnits='V', valueDecimals=2)

    def _set(self, channel, value):
        engineName = str(channel).split('/', 1)[0]
        engine = self._engineNamed(engineName) if callable(self._engineNamed) else None
        if engine is None:
            self.__logger.error(f'{channel}: no scan engine {engineName!r} sets it.')
            return
        try:
            engine.set(channel, value)
        except Exception as error:
            self.__logger.error(f'{channel} not set to {value:g}: {error}')

    def setEnabled(self, enabled):
        if self._line is not None:
            self._set(self._line, 1 if self._level(bool(enabled)) else 0)

    def setValue(self, value, enabled=True, for_scanning=False):
        if self.isBinary:
            return
        calibration = self.powerCalibration
        if calibration is None:
            volts = float(value)
        else:
            try:
                volts = calibration.drive_for_percent(value)
            except CalibrationRangeError as error:
                # Refused, not passed on (see NidaqLaserManager.setValue).
                self.__logger.error(f'{self.name}: {error}; nothing sent.')
                return False
        self._set(self._analog, volts)


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
