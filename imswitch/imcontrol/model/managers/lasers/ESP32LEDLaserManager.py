from imswitch.imcommon.model import initLogger
from .LaserManager import LaserManager


class ESP32LEDLaserManager(LaserManager):
    """ LaserManager for controlling LEDs and Lasers connected to an
    ESP32 exposing a REST API
    Each LaserManager instance controls one LED.

    Manager properties:

    - ``rs232device`` -- name of the defined rs232 communication channel
      through which the communication should take place
    - ``channel_index`` -- laser channel (A to H)
    - ``useMockOnFailure`` -- default ``false``: a missing rs232 device is an
      error naming it, and a board that is not connected leaves the laser
      not connected (reported, commands refused, reconnectable from Hardware
      status). ``true`` runs a reported mock that drops commands.
    """

    def __init__(self, laserInfo, name, **lowLevelManagers):
        super().__init__(laserInfo, name, isBinary=False, valueUnits='mW', valueDecimals=0)
        self.__logger = initLogger(self, instanceName=name)
        self.power = 0
        self.enabled = False
        useMock = laserInfo.managerProperties.get('useMockOnFailure', False)
        if isinstance(useMock, str):
            useMock = useMock.strip().lower() in ('1', 'true', 'yes', 'on')
        # Only an explicit opt-in mock; not a latch: it is never set by a failure
        # that a reconnect could cure.
        self._isMock = False

        try:
            self._rs232manager = lowLevelManagers['rs232sManager'][
                laserInfo.managerProperties['rs232device']
            ]
            self.__channel_index = laserInfo.managerProperties['channel_index']
        except Exception as e:
            if not useMock:
                raise RuntimeError(
                    f"ESP32 LED laser {name!r}: RS232 device {e} is not available. "
                    f"Define it in rs232devices, or set useMockOnFailure to true "
                    f"for a mock.") from e
            self._isMock = True
            self.__logger.warning(
                f'Failed to initialize ESP32LED hardware, running in mock mode: {e}'
            )
            self._rs232manager = None
            self._setConnectionError(
                e,
                summary="ESP32 LED initialization failed; mock fallback active",
                mock_active=True,
            )
            self.__channel_index = laserInfo.managerProperties.get('channel_index', 0)

    # Transport hooks (DeviceLifecycleService): the board was reopened and
    # reset, so its output is off. The laser must agree, or the next power
    # change would switch it on under an OFF panel.
    def _lifecycleSafeState(self, *, verified: bool):
        self.enabled = False
        if self._isMock:
            return []
        try:
            self._rs232manager._squid.set_laser(self.__channel_index, 0)
        except Exception as exc:
            return [f'{self.name}: could not confirm OFF: {exc}'] if verified else []
        return []

    def _onTransportReconnected(self, real: bool):
        if not real:
            self._setConnectionError('serial port could not be reopened',
                                     summary='ESP32 board did not reconnect')
            return ['serial port could not be reopened']
        errors = self._lifecycleSafeState(verified=True)
        if errors:
            self._setConnectionError('; '.join(errors),
                                     summary='ESP32 LED reconnected but not usable')
        else:
            self._setConnected('ESP32 LED reconnected; OFF')
        return errors

    def setEnabled(self, enabled):
        """Turn on (N) or off (F) laser emission"""
        if self._isMock:
            self.enabled = enabled
            self.__logger.debug(f'Mock mode: setEnabled({enabled}) ignored')
            return

        # A board that is not there raises DeviceNotConnectedError here, before
        # any state changes.
        self._rs232manager._squid.set_laser(self.__channel_index, self.power if enabled else 0)
        self.enabled = enabled

    def setValue(self, power):
        """Handles output power.
        Sends a RS232 command to the laser specifying the new intensity.
        """
        if self._isMock:
            self.power = power
            self.__logger.debug(f'Mock mode: setValue({power}) ignored')
            return

        if self.enabled:
            self._rs232manager._squid.set_laser(self.__channel_index, power)
        self.power = power


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
