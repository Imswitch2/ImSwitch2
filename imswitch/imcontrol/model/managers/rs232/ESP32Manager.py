from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.devices.graph import DeviceDescriptorSpec, DeviceRole
from imswitch.imcontrol.model.devices.status import (
    DeviceManagerStatusMixin, backend_attribute,
)


class ESP32Manager(DeviceManagerStatusMixin):
    """ A low-level wrapper for TCP-IP communication (ESP32 REST API).

    The client is opened through the backend holder: a board that cannot be
    reached leaves this transport not connected (its users refuse commands)
    until it is reconnected from Hardware status, which re-initialises every
    device on it.
    """

    #: The UC2 client, read from the backend holder: a reconnect replaces it.
    _esp32 = backend_attribute()

    def __init__(self, rs232Info, name, **_lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)
        self._settings = rs232Info.managerProperties
        self._name = name
        try:
            self._host = rs232Info.managerProperties['host']
        except KeyError:
            self._host = None

        try:
            self._serialport = rs232Info.managerProperties['serialport']
        except KeyError:
            self._serialport = None

        try:
            self._identity = rs232Info.managerProperties['identity']
        except KeyError:
            self._identity = "UC2_Feather"

        def open_real():
            try:
                import uc2rest as uc2  # pip install UC2-REST
            except ImportError as exc:
                raise ImportError('uc2rest library not installed. Install with: '
                                  'pip install UC2-REST') from exc
            return uc2.UC2Client(host=self._host, port=80, identity=self._identity,
                                 serialport=self._serialport, baudrate=115200)

        self._installBackend(
            open_real, label=f'ESP32 {self._host or self._serialport}',
            transient=bool(getattr(rs232Info, 'transient', False)),
            connect_on_startup=bool(getattr(rs232Info, 'connectOnStartup', False)),
        )

    def reconnectTransport(self) -> bool:
        """Open the client again in place (DeviceLifecycleService §4.3)."""
        return self._replaceBackend()

    def disconnectTransport(self) -> None:
        self.backendHolder.disconnect()

    def getDeviceDescriptorSpec(self):
        # Communication infrastructure, not a user-meaningful device.
        return DeviceDescriptorSpec(role=DeviceRole.RESOURCE)

    def finalize(self):
        self.backendHolder.close(suppress_errors=True)
        self._setFinalizedStatus()

    def sendTrigger(self, triggerId: int):
        """Send a trigger pulse through the ESP32 device.

        Args:
            triggerId: Integer identifier for the trigger channel
        """
        if not self._esp32:
            self.__logger.warning('Cannot send trigger: ESP32 not connected')
            return None
        return self._esp32.sendTrigger(triggerId)

    def post_json(self, path: str, payload: dict = None, headers: dict = None, timeout: float = 1):
        """Send a JSON POST request to the ESP32 device.

        Args:
            path: API endpoint path
            payload: JSON payload dictionary
            headers: Optional HTTP headers
            timeout: Request timeout in seconds

        Returns:
            JSON response from the device or None if not connected
        """
        if not self._esp32:
            self.__logger.warning('Cannot post JSON: ESP32 client not initialized')
            return None
        if payload is None:
            payload = {}
        return self._esp32.post_json(path, payload=payload, headers=headers, timeout=timeout)

    def writeSerial(self, payload):
        """Write data to the ESP32 device via serial.

        Args:
            payload: Data to write (string or dict)
        """
        if not self._esp32:
            self.__logger.warning('Cannot write serial: ESP32 client not initialized')
            return
        self._esp32.writeSerial(payload)

    def readSerial(self, is_blocking: bool = True, timeout: float = 1):
        """Read data from the ESP32 device via serial.

        Args:
            is_blocking: Whether to block until data is available
            timeout: Read timeout in seconds

        Returns:
            Data read from the device
        """
        if not self._esp32:
            self.__logger.warning('Cannot read serial: ESP32 client not initialized')
            return ''
        return self._esp32.readSerial(is_blocking=is_blocking, timeout=timeout)

    def move_x(self, value, speed, is_blocking=False):
        """Move the X-axis stage motor.

        Args:
            value: Distance to move in motor steps
            speed: Motor speed
            is_blocking: Whether to wait for move completion
        """
        if not self._esp32:
            self.__logger.warning('Cannot move X: ESP32 client not initialized')
            return False
        self._esp32.move_x(value, speed, is_blocking=is_blocking)
        return True

    def move_y(self, value, speed, is_blocking=False):
        """Move the Y-axis stage motor.

        Args:
            value: Distance to move in motor steps
            speed: Motor speed
            is_blocking: Whether to wait for move completion
        """
        if not self._esp32:
            self.__logger.warning('Cannot move Y: ESP32 client not initialized')
            return False
        self._esp32.move_y(value, speed, is_blocking=is_blocking)
        return True

    def move_z(self, value, speed, is_blocking=False):
        """Move the Z-axis stage motor.

        Args:
            value: Distance to move in motor steps
            speed: Motor speed
            is_blocking: Whether to wait for move completion
        """
        if not self._esp32:
            self.__logger.warning('Cannot move Z: ESP32 client not initialized')
            return False
        self._esp32.move_z(value, speed, is_blocking=is_blocking)
        return True

    def set_galvo_freq(self, axis, value):
        """Set galvo DAC frequency on the specified axis.

        Args:
            axis: Galvo axis number (0 or 1)
            value: Frequency value to set
        """
        if not self._esp32:
            self.__logger.warning('Cannot set galvo frequency: ESP32 client not initialized')
            return
        self._esp32.set_galvo_freq(axis=axis, value=value)

    def set_galvo_amp(self, axis, value):
        """Set galvo DAC amplitude on the specified axis.

        Args:
            axis: Galvo axis number (0 or 1)
            value: Amplitude value to set
        """
        if not self._esp32:
            self.__logger.warning('Cannot set galvo amplitude: ESP32 client not initialized')
            return
        self._esp32.set_galvo_amp(axis=axis, value=value)

    def send_LEDMatrix_array(self, pattern):
        """Send LED matrix pattern array to the device.

        Args:
            pattern: 3D array of LED values (3 x N x N for RGB)
        """
        if not self._esp32:
            self.__logger.warning('Cannot send LED matrix: ESP32 client not initialized')
            return
        self._esp32.send_LEDMatrix_array(pattern)


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
