import pyvisa
from pyvisa import constants


class RS232Driver:
    """General RS232 driver backed directly by pyvisa (no lantz dependency)."""

    DEFAULTS = None  # subclasses override via generateDriverClass()

    def __init__(self, port, *args):
        self._port = port
        self._resource = None

    def initialize(self):
        rm = pyvisa.ResourceManager()
        self._resource = rm.open_resource(self._port)
        for key, value in (self.DEFAULTS or {}).get('ASRL', {}).items():
            setattr(self._resource, key, value)

    def finalize(self):
        if self._resource is not None:
            self._resource.close()
            self._resource = None

    def close(self):
        self.finalize()

    def query(self, command):
        return self._resource.query(command)

    def write(self, command):
        return self._resource.write(command)

    def read(self, *args, **kwargs):
        return self._resource.read()

    @classmethod
    def getDefaults(cls, settings):
        if settings["parity"] == 'none':
            set_par = constants.Parity.none
        if settings["stopbits"] == 1:
            set_stopb = constants.StopBits.one
        elif settings["stopbits"] == 2:
            set_stopb = constants.StopBits.two

        defaults = {'ASRL': {'write_termination': settings["send_termination"],
                             'read_termination': settings["recv_termination"],
                             'baud_rate': settings["baudrate"],
                             'bytesize': settings["bytesize"],
                             'parity': set_par,
                             'stop_bits': set_stopb,
                             'encoding': settings["encoding"],
                             }}
        return defaults


def generateDriverClass(settings):
    class GeneratedDriver(RS232Driver):
        DEFAULTS = RS232Driver.getDefaults(settings)
        try:
            del DEFAULTS['ASRL']['bytesize']
        except KeyError:
            pass

    return GeneratedDriver


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
