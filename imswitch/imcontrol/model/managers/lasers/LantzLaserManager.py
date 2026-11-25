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
    - ``useMockOnFailure`` -- default ``false``: a laser that cannot be opened
      is left absent (reported, commands refused, reconnectable from
      Hardware status). ``true`` runs a reported mock instead.
    """

    def __init__(self, laserInfo, name, isBinary, valueUnits, valueDecimals, driver,
                 **_lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)

        self._ports = normalise_ports(laserInfo.managerProperties['digitalPorts'])
        self._driver = driver

        # Open linked lasers atomically. A failed real port makes the complete
        # logical laser absent (or, as an explicit opt-in, a mock) rather than
        # a real/mock mixture.
        useMock = laserInfo.managerProperties.get('useMockOnFailure', False)
        if isinstance(useMock, str):
            useMock = useMock.strip().lower() in ('1', 'true', 'yes', 'on')
        self._useMockOnFailure = bool(useMock)
        self._numLasers = len(self._ports)
        self._absent = False
        self._label = f'laser {name!r} on {", ".join(map(str, self._ports))}'
        try:
            result = openLantzLaser(driver, self._ports,
                                    allowMockFallback=self._useMockOnFailure)
        except Exception as exc:
            from imswitch.imcontrol.model.devices.status import _AbsentBackend

            self.__logger.warning(
                f'Could not open {self._label}: {exc}. The laser is left absent; '
                f'reconnect it from Hardware status (useMockOnFailure true for a mock).')
            self._laser = _AbsentBackend(self, self._label)
            self._backendIsMock = False
            self._backendOpenError = exc
            self._absent = True
            self._setConnectionError(exc, summary=f'{self._label} not connected')
            super().__init__(laserInfo, name, isBinary=isBinary, valueUnits=valueUnits,
                             valueDecimals=valueDecimals)
            return
        self._laser = result.laser
        self._backendIsMock = bool(result.is_mock)
        self._backendOpenError = result.error

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

    def _requireNotAbsent(self, what):
        if self.__dict__.get('_absent', False):
            from imswitch.imcontrol.model.devices.status import DeviceNotConnectedError
            raise DeviceNotConnectedError(
                f"{getattr(self, 'name', 'laser')}: {what} refused, "
                f"{self.connectionStatusSummary or 'not connected'}")

    def finalize(self):
        if not self.__dict__.get('_absent', False):
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
