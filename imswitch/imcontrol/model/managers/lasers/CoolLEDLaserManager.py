from weakref import WeakKeyDictionary

from imswitch.imcommon.model import initLogger
from .LaserManager import LaserManager
from imswitch.imcontrol.model.devices.graph import sharedRs232ComponentSpec
from imswitch.imcontrol.model.devices.status import (
    DeviceConnectionState, DeviceFailureKind, DeviceRuntimeMode,
)


# One physical CoolLED controller can be represented by several laser-channel
# managers. Probe the shared controller once per RS232 manager at startup and
# reuse that passive result for all channels.
_COOLLED_PROBE_CACHE = WeakKeyDictionary()


def _probe_coolled_controller(rs232manager):
    cached = _COOLLED_PROBE_CACHE.get(rs232manager)
    if cached is not None:
        return cached

    mode = getattr(rs232manager, "runtimeMode", DeviceRuntimeMode.REAL)
    transport_state = getattr(
        rs232manager, "connectionState", DeviceConnectionState.UNKNOWN
    )
    if mode is DeviceRuntimeMode.MOCK or transport_state in {
        DeviceConnectionState.ERROR, DeviceConnectionState.DISCONNECTED
    }:
        result = (
            DeviceConnectionState.ERROR,
            mode,
            "CoolLED transport unavailable",
            getattr(rs232manager, "connectionStatusDetails", None),
            getattr(
                rs232manager, "connectionFailureKind", DeviceFailureKind.CONNECTION_ERROR
            ) or DeviceFailureKind.CONNECTION_ERROR,
        )
    else:
        try:
            reply = rs232manager.query("CSS?")
            if reply is None or not str(reply).strip():
                raise RuntimeError("CoolLED CSS? returned no response")
            result = (
                DeviceConnectionState.CONNECTED,
                DeviceRuntimeMode.REAL,
                "CoolLED controller responded to CSS?",
                None,
                None,
            )
        except Exception as exc:
            result = (
                DeviceConnectionState.ERROR,
                DeviceRuntimeMode.REAL,
                "CoolLED controller did not respond to CSS?",
                str(exc),
                DeviceFailureKind.CONNECTION_ERROR,
            )

    _COOLLED_PROBE_CACHE[rs232manager] = result
    return result


class CoolLEDLaserManager(LaserManager):
    """ LaserManager for controlling the LEDs from CoolLED. Each LaserManager
    instance controls one LED.

    Manager properties:

    - ``rs232device`` -- name of the defined rs232 communication channel
      through which the communication should take place
    - ``channel_index`` -- laser channel (A to H)
    """

    def __init__(self, laserInfo, name, **lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)
        self._isMock = False

        try:
            self._rs232manager = lowLevelManagers['rs232sManager'][
                laserInfo.managerProperties['rs232device']
            ]
            self.__channel_index = laserInfo.managerProperties['channel_index']
            self.__digital_mod = False
        except Exception as e:
            self._isMock = True
            self.__logger.warning(
                f'Failed to initialize CoolLED hardware, running in mock mode: {e}'
            )
            self._rs232manager = None
            self._setConnectionError(
                e,
                summary="CoolLED initialization failed; mock fallback active",
                mock_active=True,
            )
            self.__channel_index = laserInfo.managerProperties.get('channel_index', 'A')
            self.__digital_mod = False

        isModulated = (True if laserInfo.freqRangeMin is not None and 
                                laserInfo.freqRangeMax is not None and
                                laserInfo.freqRangeInit is not None 
                            else False)

        super().__init__(laserInfo, name, isBinary=False, valueUnits='mW', valueDecimals=0, isModulated=isModulated)

        if not self._isMock and self._rs232manager is not None:
            state, mode, summary, details, failure_kind = _probe_coolled_controller(
                self._rs232manager
            )
            self._deviceRuntimeMode = mode
            self._setConnectionState(
                state,
                summary=summary,
                details=details,
                failure_kind=failure_kind,
            )


    def getDeviceDescriptorSpec(self):
        rs232_name = self.getProperty('rs232device')
        return sharedRs232ComponentSpec(
            category='laser',
            family='coolled',
            display_name='CoolLED controller',
            rs232_name=str(rs232_name),
        )

    def setEnabled(self, enabled):
        """Turn on (N) or off (F) laser emission"""
        if self._isMock:
            self.__logger.debug(f'Mock mode: setEnabled({enabled}) ignored')
            return
        
        if enabled:
            value = "N"
        else:
            value = "F"
        cmd = "C" + self.__channel_index + value
        self._rs232manager.query(cmd)

    def setValue(self, power, enabled=True, for_scanning=False):
        """Handles output power.
        Sends a RS232 command to the laser specifying the new intensity.
        """
        if self._isMock:
            self.__logger.debug(f'Mock mode: setValue({power}) ignored')
            return
        
        cmd = "C" + self.__channel_index + "IX" + "{0:03.0f}".format(power)
        self.__logger.debug(cmd)
        self._rs232manager.query(cmd)


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
