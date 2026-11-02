import threading

from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.devices.graph import (
    DeviceDescriptorSpec, DeviceRole,
)
from imswitch.imcontrol.model.devices.status import (
    DeviceManagerStatusMixin, DeviceRuntimeMode,
)


class RS232Manager(DeviceManagerStatusMixin):
    """A general-purpose RS232 manager with reversible runtime transport state.

    The manager object itself is stable for the lifetime of ImSwitch. Runtime
    reconnect replaces only the internal driver/backend so higher-level device
    managers that retain this ``RS232Manager`` do not need to be rebuilt or
    rebound.

    Manager properties:

    - ``port``
    - ``encoding``
    - ``recv_termination``
    - ``send_termination``
    - ``baudrate``
    - ``bytesize``
    - ``parity``
    - ``stopbits``
    - ``rtscts``
    - ``dsrdtr``
    - ``xonxoff``
    """

    def __init__(self, rs232Info, name, **_lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)

        self._settings = rs232Info.managerProperties
        self._name = name
        self._port = rs232Info.managerProperties['port']
        self._ioLock = threading.RLock()
        self._rs232port = self._openWithFallback(context="startup")

    def _callBackend(self, method_name, *args, **kwargs):
        with self._ioLock:
            backend = self._rs232port
            try:
                result = getattr(backend, method_name)(*args, **kwargs)
            except Exception as exc:
                if self.runtimeMode is DeviceRuntimeMode.REAL:
                    self._setConnectionError(
                        exc,
                        summary=(
                            f'RS232 transport {self._port} I/O failed during '
                            f'{method_name}'
                        ),
                    )
                raise
            else:
                # A successful real I/O is current evidence that the transport
                # is usable and can heal a previously cached transient error.
                if self.runtimeMode is DeviceRuntimeMode.REAL:
                    self._setConnected(f'RS232 transport {self._port} active')
                return result

    def query(self, arg: str) -> str:
        """Send a command and return the decoded response."""
        return self._callBackend('query', arg)

    def write(self, arg: str):
        """Send a command to the RS232 device."""
        return self._callBackend('write', arg)

    def read(self, arg: str = None) -> str:
        """Read from the RS232 device and return a decoded string."""
        return self._callBackend(
            'read', arg,
            recv_args=(self._settings["recv_termination"], None),
        )

    def setTimeout(self, timeout_ms: int):
        """Set the serial read timeout.

        :param timeout_ms: timeout in milliseconds (pyvisa convention).
        """
        with self._ioLock:
            resource = getattr(self._rs232port, '_resource', None)
            if resource is None:
                return
            if hasattr(resource, 'timeout'):
                # pyvisa resource — timeout attribute is in ms
                resource.timeout = timeout_ms
            elif hasattr(resource, '_ser'):
                # _SerialAdapter (raw pyserial fallback) — timeout is seconds
                resource._ser.timeout = timeout_ms / 1000

    def disconnectTransport(self) -> None:
        """Close the current backend without terminally finalizing the manager."""
        with self._ioLock:
            self._closeBackend(self._rs232port, suppress_errors=False)
            self._setDisconnected(f'RS232 transport {self._port} disconnected')

    def reconnectTransport(self) -> bool:
        """Replace the current backend in place and reopen the configured port.

        Returns ``True`` when a real serial backend was opened. On failure the
        normal RS232 mock fallback is installed and canonical status records
        the connection error, matching startup fallback semantics.
        """
        with self._ioLock:
            self._closeBackend(self._rs232port, suppress_errors=True)
            self._rs232port = self._openWithFallback(context="reconnect")
            return self.runtimeMode is DeviceRuntimeMode.REAL

    def finalize(self):
        with self._ioLock:
            self._closeBackend(self._rs232port, suppress_errors=False)
            self._setFinalizedStatus()

    def getDeviceDescriptorSpec(self):
        # A generic RS232Manager is communication infrastructure, not the
        # user-meaningful microscope device that happens to use it.
        return DeviceDescriptorSpec(role=DeviceRole.RESOURCE)

    @staticmethod
    def _closeBackend(backend, *, suppress_errors: bool) -> None:
        if backend is None:
            return
        close = getattr(backend, 'close', None)
        if not callable(close):
            return
        if suppress_errors:
            try:
                close()
            except Exception:
                # Runtime reconnect must still be allowed to replace a broken
                # backend. Terminal finalize deliberately does not suppress
                # this error so MasterController can retry shutdown.
                pass
            return
        close()

    def _openRealPort(self):
        from imswitch.imcontrol.model.interfaces.RS232Driver import generateDriverClass

        DriverClass = generateDriverClass(self._settings)
        rs232port = DriverClass(self._port)
        rs232port.initialize()
        return rs232port

    def _makeMockPort(self):
        from imswitch.imcontrol.model.interfaces.RS232Driver_mock import MockRS232Driver

        return MockRS232Driver(self._port, self._settings)

    def _openWithFallback(self, *, context: str):
        try:
            rs232port = self._openRealPort()
            action = 'opened' if context == 'startup' else 'reopened'
            self._setConnected(f'RS232 transport {self._port} {action}')
            return rs232port
        except Exception as exc:
            action = 'initialize' if context == 'startup' else 'reconnect'
            self.__logger.warning(
                f'Failed to {action} RS232 port {self._port}: {exc}. '
                'Initializing mock RS232 port'
            )
            summary = (
                f'RS232 transport {self._port} failed; mock fallback active'
                if context == 'startup'
                else f'RS232 transport {self._port} reconnect failed; mock fallback active'
            )
            self._setConnectionError(
                exc,
                summary=summary,
                mock_active=True,
            )
            return self._makeMockPort()


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
