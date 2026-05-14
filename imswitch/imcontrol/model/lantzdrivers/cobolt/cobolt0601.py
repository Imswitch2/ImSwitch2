from pyvisa import constants

from imswitch.imcontrol.model.interfaces.RS232Driver import RS232Driver


class Cobolt0601(RS232Driver):
    """Vendored Cobolt 06-01 Series laser driver (no lantz dependency).

    Communicates over RS-232 using the Cobolt ASCII protocol.
    Serial settings: 115200 baud, 8N1, CR termination.
    """

    DEFAULTS = {
        'ASRL': {
            'write_termination': '\r',
            'read_termination': '\r\n',
            'baud_rate': 115200,
            'parity': constants.Parity.none,
            'stop_bits': constants.StopBits.one,
            'encoding': 'ascii',
        }
    }

    def initialize(self):
        super().initialize()
        self._mode = None  # 'ACC' or 'APC', tracked locally

    @property
    def idn(self):
        return self.query('id?')

    @property
    def status(self):
        return self.query('?')

    # ---- enable ----

    @property
    def enabled(self):
        return self.query('l?').strip() == '1'

    @enabled.setter
    def enabled(self, value):
        self.query('l1' if value else 'l0')

    # ---- power setpoint ----

    @property
    def power_sp(self):
        return float(self.query('p?'))

    @power_sp.setter
    def power_sp(self, value):
        self.query(f'p {float(value):.4f}')

    # ---- actual power ----

    @property
    def power(self):
        return float(self.query('pa?'))

    # ---- operating mode ('ACC' / 'APC') ----

    @property
    def mode(self):
        # mode is tracked locally; hardware switches happen via ci / cp queries
        return self._mode

    @mode.setter
    def mode(self, value):
        self._mode = value

    # ---- autostart ----

    @property
    def autostart(self):
        return self.query('@cobas?').strip() == '1'

    @autostart.setter
    def autostart(self, value):
        self.query(f'@cobas {1 if value else 0}')

    # ---- digital modulation ----

    @property
    def digital_mod(self):
        return self.query('gdmes?').strip() == '1'

    @digital_mod.setter
    def digital_mod(self, value):
        self.query(f'sdmes {1 if value else 0}')

    def enter_mod_mode(self):
        self.query('em')

    @property
    def mod_mode(self):
        try:
            return int(self.query('gmod?').strip())
        except (ValueError, AttributeError):
            return 0


class Cobolt0601_f2(Cobolt0601):
    """Driver for Cobolt 06-01 Series laser, new firmware (adds power_mod)."""

    @property
    def power_mod(self):
        """Laser modulated power (mW)."""
        return float(self.query('glmp?'))

    @power_mod.setter
    def power_mod(self, value):
        self.query(f'slmp {float(value):.4f}')


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
