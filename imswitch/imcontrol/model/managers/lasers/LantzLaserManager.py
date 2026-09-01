from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.interfaces.lantzlasers import openLantzLaser
from .LaserManager import LaserManager, normalise_ports


class LantzLaserManager(LaserManager):
    """Base LaserManager for legacy Lantz-style digitally controlled lasers.

    The current in-tree Cobolt driver is vendored and no longer depends on the
    external Lantz package, but this manager name is retained for setup-file
    compatibility.

    Manager properties:

    - ``digitalPorts`` -- a string array containing the COM ports to connect
      to, e.g. ``["COM4"]``
    """

    def __init__(self, laserInfo, name, isBinary, valueUnits, valueDecimals, driver,
                 **_lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)

        self._ports = normalise_ports(laserInfo.managerProperties['digitalPorts'])
        self._driver = driver

        # Open linked lasers atomically. A failed real port causes the complete
        # logical laser to use mocks rather than creating a real/mock mixture.
        result = openLantzLaser(driver, self._ports, allowMockFallback=True)
        self._laser = result.laser
        self._backendIsMock = bool(result.is_mock)
        self._backendOpenError = result.error
        self._numLasers = len(self._ports)

        if self._backendIsMock:
            self._setConnectionError(
                result.error or 'Real laser backend unavailable',
                summary='Laser hardware unavailable; mock fallback active',
                mock_active=True,
            )
        else:
            self._setConnected('Laser initialized')

        try:
            self.__logger.info(f'Initialized laser, model: {self._laser.idn}')
        except Exception as exc:
            # Identification is diagnostic only here. The concrete manager's
            # safe initialization/lifecycle path will surface transport errors.
            self.__logger.warning('Could not read laser identity: %s', exc)

        super().__init__(laserInfo, name, isBinary=isBinary, valueUnits=valueUnits,
                         valueDecimals=valueDecimals)

    def finalize(self):
        self._laser.finalize()
        self._setFinalizedStatus()


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
