from pyvisa import constants

from imswitch.imcontrol.model.interfaces.RS232Driver import RS232Driver


class Cobolt0601(RS232Driver):
    """Vendored Cobolt 06-01 Series laser driver (no lantz dependency).

    Communicates over RS-232 using the Cobolt ASCII protocol.
    Serial settings: 115200 baud, 8N1, CR termination.

    Older firmware revisions omit certain query commands (l?, gmod?, sn?).
    Optional queries fall back to locally tracked state only when the device
    explicitly reports an unsupported command. Transport exceptions propagate
    so callers can distinguish an old firmware limitation from a lost device.
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
        self._mode = None      # 'ACC' or 'APC', tracked locally
        self._enabled = False  # tracked locally — l? absent on older firmware
        self._digital_mod = False
        self._log_capabilities()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _unsupported_reply(result):
        text = '' if result is None else str(result).lower()
        return 'syntax error' in text or 'illegal command' in text

    def _log_capabilities(self):
        """Probe optional commands and log which are supported by this firmware."""
        import logging
        log = logging.getLogger(__name__)
        fw = self._safe_query('gfv?', default='unknown')
        probes = {
            'l?':     'emission state query',
            'sn?':    'serial number',
            'gmod?':  'modulation mode query',
            'glmp?':  'modulation power query',
        }
        unsupported = [desc for cmd, desc in probes.items()
                       if self._safe_query(cmd) is None]
        if unsupported:
            log.info(
                f'Cobolt firmware {fw}: the following optional queries are not '
                f'supported and will use local state — {", ".join(unsupported)}'
            )
        else:
            log.info(f'Cobolt firmware {fw}: all optional queries supported')

    def _safe_query(self, command, default=None):
        """Send an optional query and return its response.

        Returns ``default`` only when the firmware explicitly rejects the
        command as unsupported. Communication exceptions are deliberately not
        swallowed: callers must be able to recognize a lost serial transport.
        """
        result = self.query(command)
        if self._unsupported_reply(result):
            return default
        return result

    def _command(self, command):
        """Send a mutating command, raising if the device rejects it."""
        result = self.query(command)
        if self._unsupported_reply(result):
            raise RuntimeError(f'Cobolt rejected command {command!r}: {result}')
        return result

    # ------------------------------------------------------------------
    # Identification
    # ------------------------------------------------------------------

    @property
    def idn(self):
        # gfv? (firmware version) is universally supported; sn? is not on older FW
        return self._safe_query('gfv?', default='Cobolt (unknown)')

    @property
    def serial_number(self):
        # sn? is optional on older firmware. None means identity unavailable,
        # not a transport failure.
        return self._safe_query('sn?')

    @property
    def status(self):
        return self._safe_query('?', default='unknown')

    # ------------------------------------------------------------------
    # Enable / disable emission
    # ------------------------------------------------------------------

    @property
    def enabled(self):
        result = self._safe_query('l?')
        if result is not None:
            self._enabled = result.strip() == '1'
        return self._enabled

    @enabled.setter
    def enabled(self, value):
        self._command('l1' if value else 'l0')
        self._enabled = bool(value)

    # ------------------------------------------------------------------
    # Power
    # ------------------------------------------------------------------

    @property
    def power_sp(self):
        result = self._safe_query('p?', default='0')
        try:
            return float(result) * 1000  # W → mW
        except (ValueError, TypeError):
            return 0.0

    @power_sp.setter
    def power_sp(self, value):
        self._command(f'p {float(value) / 1000:.6f}')  # mW → W

    @property
    def power(self):
        result = self._safe_query('pa?', default='0')
        try:
            return float(result) * 1000  # W → mW
        except (ValueError, TypeError):
            return 0.0

    # ------------------------------------------------------------------
    # Operating mode ('ACC' / 'APC') — tracked locally
    # ------------------------------------------------------------------

    @property
    def mode(self):
        return self._mode

    @mode.setter
    def mode(self, value):
        self._mode = value

    # ------------------------------------------------------------------
    # Autostart
    # ------------------------------------------------------------------

    @property
    def autostart(self):
        result = self._safe_query('@cobas?')
        if result is not None:
            return result.strip() == '1'
        return False

    @autostart.setter
    def autostart(self, value):
        self._command(f'@cobas {1 if value else 0}')

    # ------------------------------------------------------------------
    # Digital modulation
    # ------------------------------------------------------------------

    @property
    def digital_mod(self):
        result = self._safe_query('gdmes?')
        if result is not None:
            self._digital_mod = result.strip() == '1'
        return self._digital_mod

    @digital_mod.setter
    def digital_mod(self, value):
        self._command(f'sdmes {1 if value else 0}')
        self._digital_mod = bool(value)

    def enter_mod_mode(self):
        self._command('em')

    @property
    def mod_mode(self):
        result = self._safe_query('gmod?')
        try:
            return int(result)
        except (ValueError, TypeError):
            return 0


class Cobolt0601_f2(Cobolt0601):
    """Driver for Cobolt 06-01 Series laser, new firmware (adds power_mod)."""

    def initialize(self):
        super().initialize()
        self._power_mod = 0.0  # tracked locally — glmp? absent on older firmware

    @property
    def power_mod(self):
        # slmp/glmp? use mW directly (same as Cobolt06MLD), unlike p/p? which use W
        result = self._safe_query('glmp?')
        try:
            val = float(result)  # already mW
            self._power_mod = val
            return val
        except (ValueError, TypeError):
            return self._power_mod

    @power_mod.setter
    def power_mod(self, value):
        # slmp/glmp? use mW directly (same as Cobolt06MLD), unlike p/p? which use W
        value = float(value)
        self._command(f'slmp {value:.4f}')  # mW, no conversion
        self._power_mod = value


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
