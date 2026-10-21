from imswitch.imcontrol.model.devices.graph import DeviceDescriptorSpec, DeviceRole
from imswitch.imcontrol.model.devices.status import (
    DeviceManagerStatusMixin, backend_attribute,
)
from imswitch.imcommon.model import initLogger


class GRBLManager(DeviceManagerStatusMixin):
    """ A general-purpose RS232 manager that together with a general-purpose
    RS232Driver interface can handle an arbitrary RS232 communication channel,
    with all the standard serial communication protocol parameters as defined
    in the hardware control configuration.

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
        try:
            self.is_home = rs232Info.managerProperties['is_home']
        except KeyError:
            self.is_home = False 
             
        def open_real():
            import imswitch.imcontrol.model.interfaces.grbldriver as grbldriver
            return grbldriver.GrblDriver(self._port)

        self._installBackend(
            open_real, label=f'GRBL board {self._port}',
            transient=bool(getattr(rs232Info, 'transient', False)),
            connect_on_startup=bool(getattr(rs232Info, 'connectOnStartup', False)),
        )
        if self.backendHolder.backend:
            self._lifecycleReinitialise()

    #: The driver, read from the backend holder: a reconnect replaces it.
    _board = backend_attribute()

    def _lifecycleReinitialise(self) -> None:
        """Program the board: at startup and again after it was reopened."""
        self._board.write_global_config()
        self._board.write_all_settings()
        #self.board.verify_settings()
        self._board.reset_stage()
        if self.is_home:
            self._board.home()

    def reconnectTransport(self) -> bool:
        """Open the board again in place (DeviceLifecycleService §4.3)."""
        real = self._replaceBackend()
        if real:
            self._lifecycleReinitialise()
        return real

    def disconnectTransport(self) -> None:
        self.backendHolder.disconnect()

    def getDeviceDescriptorSpec(self):
        return DeviceDescriptorSpec(role=DeviceRole.RESOURCE)

    def query(self, arg: str) -> str:
        """ Sends the specified command to the RS232 device and returns a
        string encoded from the received bytes. """
        return self._board._write(arg)

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
