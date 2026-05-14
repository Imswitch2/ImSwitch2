from imswitch.imcommon.model import initLogger


class Cobolt0601_f2:
    """Mock driver for Cobolt 06-01 Series laser (no lantz / no hardware)."""

    def __init__(self, *args, **kwargs):
        self.__logger = initLogger(self, tryInheritParent=True)
        self.enabled = False
        self.power_sp = 0.0   # mW, plain float
        self._digMod = False
        self._mode = None

    def initialize(self):
        self._mode = None

    def finalize(self):
        pass

    def close(self):
        pass

    @property
    def idn(self):
        return 'Simulated laser'

    @property
    def status(self):
        return 'Simulated laser status'

    @property
    def power(self):
        return 0.0

    @property
    def mode(self):
        return self._mode

    @mode.setter
    def mode(self, value):
        self._mode = value

    @property
    def autostart(self):
        return False

    @autostart.setter
    def autostart(self, value):
        pass

    @property
    def digital_mod(self):
        return self._digMod

    @digital_mod.setter
    def digital_mod(self, value):
        self._digMod = value

    def enter_mod_mode(self):
        self._digMod = True

    @property
    def mod_mode(self):
        return 0

    @property
    def power_mod(self):
        return 0.0

    @power_mod.setter
    def power_mod(self, value):
        pass

    def query(self, text):
        self.__logger.debug(f'Mock query: {text!r}')
        return '0'


# Copyright (C) 2017 Federico Barabas
# This file is part of Tormenta.
#
# Tormenta is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Tormenta is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
