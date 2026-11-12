from imswitch.imcontrol.model.devices.graph import DeviceDescriptorSpec, DeviceRole
from imswitch.imcontrol.model.devices.status import (
    DeviceManagerStatusMixin, backend_attribute,
)
from imswitch.imcommon.model import initLogger


class SQUIDManager(DeviceManagerStatusMixin):

    def __init__(self, rs232Info, name, **_lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)
        self._settings = rs232Info.managerProperties
        self._name = name

        try:
            self._serialport = rs232Info.managerProperties['serialport']
        except KeyError:
            self._serialport = None

        def open_real():
            from imswitch.imcontrol.model.interfaces.squid import SQUID
            return SQUID(port=self._serialport)

        self._installBackend(
            open_real, label=f'SQUID board {self._serialport}',
            transient=bool(getattr(rs232Info, 'transient', False)),
            connect_on_startup=bool(getattr(rs232Info, 'connectOnStartup', False)),
        )

    #: The board, read from the backend holder: a reconnect replaces it.
    _squid = backend_attribute()

    def send(self, arg: str) -> str:
        """ Sends the specified command to the RS232 device and returns a
        string encoded from the received bytes. """
        self._squid.post_json(arg)

    def reconnectTransport(self) -> bool:
        """Open the board again in place (DeviceLifecycleService §4.3)."""
        return self._replaceBackend()

    def disconnectTransport(self) -> None:
        self.backendHolder.disconnect()

    def getDeviceDescriptorSpec(self):
        return DeviceDescriptorSpec(role=DeviceRole.RESOURCE)

    def finalize(self):
        self.backendHolder.close(suppress_errors=False)
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
