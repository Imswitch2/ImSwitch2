from imswitch.imcommon.model import initLogger


class MockCobolt06:
    """Mock driver matching the Cobolt06 API used by Cobolt0601NewLaserManager.

    Includes a ``send_cmd`` method so the manager's raw-command path
    (``Cobolt0601NewLaserManager._cmd``) can be exercised without
    hardware. Every command is appended to ``self.cmds`` (publicly
    accessible for assertions in tests).

    By default the mock simulates **legacy firmware**: it returns
    ``'Syntax error: illegal command'`` for SCPI variants
    (prefix ``LASer:`` / ``LAS:`` / ``las:``) and ``'OK'`` for legacy
    short-form commands. Set ``mock.firmware = 'scpi'`` to flip the
    behaviour for SCPI-firmware tests.
    """

    def __init__(self, *args, **kwargs):
        # ``tryInheritParent`` walks the call stack and weakrefs frame
        # locals — that fails under pytest's HookCaller. Plain initLogger
        # is safe and gives the same coloured output at runtime.
        self.__logger = initLogger(self)
        self._on = False
        self._power = 0.0
        self._mod_power = 0.0
        self._mod_current = 0.1
        self._paused = False

        # Test/observability hooks
        self.cmds = []                # ordered log of every send_cmd argument
        self.firmware = 'legacy'      # 'legacy' or 'scpi'
        self.firmware_version = 'mock'
        self.serialnumber = 'MOCK-COBOLT'
        self.modelnumber = '0561-06-01-0100-C'

    def send_cmd(self, command):
        """Record the command and return a canned reply.

        Legacy short-form commands always succeed. SCPI commands succeed
        only when ``self.firmware == 'scpi'``; otherwise they return
        ``'Syntax error: illegal command'`` so the manager's autodetect
        and fail-closed logic can be exercised against the mock.
        """
        self.cmds.append(command)
        cl = command.lower().strip()
        is_scpi = cl.startswith(('laser:', 'las:'))
        if cl == 'gfv?':
            return self.firmware_version
        if cl in ('sn?', 'gsn?'):
            return self.serialnumber
        if cl == 'glm?':
            return self.modelnumber
        if is_scpi and self.firmware != 'scpi':
            return 'Syntax error: illegal command'
        if cl == 'laser:runmode?':
            return 'ConstantPower'
        if cl == 'laser:cp:power:setpoint?':
            return f'{self._power:.6f}'
        if cl == 'laser:power:setpoint?':
            return 'Syntax error: illegal command'
        if cl.startswith('laser:power:setpoint '):
            return 'Syntax error: illegal command'
        if cl == 'laser:powermodulation:power:setpoint?':
            return f'{self._mod_power:.6f}'
        if cl.endswith('?'):
            return '0'
        if cl.startswith('laser:cp:power:setpoint '):
            self._power = float(command.split()[-1])
        if cl.startswith('laser:powermodulation:power:setpoint '):
            self._mod_power = float(command.split()[-1])
        return 'OK'

    def initialize(self):
        pass

    def finalize(self):
        pass

    def is_on(self):
        return self._on

    def turn_on(self):
        self._on = True

    def turn_off(self):
        self._on = False

    def pause_emission(self):
        self._paused = True

    def resume_emission(self):
        self._paused = False

    def constant_current(self, current=None):
        pass

    def constant_power(self, power=None):
        pass

    def get_mode(self):
        return "ConstantPower"

    def set_power(self, power):
        self._power = float(power)

    def get_power(self):
        return self._power

    def power_modulation_mode(self, digital_enabled=True, analog_enabled=False):
        pass

    def current_modulation_mode(self, digital_enabled=True, analog_enabled=False):
        pass

    def set_modulation_power(self, power):
        self._mod_power = float(power)

    def get_modulation_power(self):
        return self._mod_power

    def set_modulation_current(self, current):
        self._mod_current = float(current)

    def get_modulation_current(self):
        return self._mod_current

    def get_modulation_state(self):
        return [0, 0]


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
