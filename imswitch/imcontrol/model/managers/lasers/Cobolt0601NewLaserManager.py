from imswitch.imcommon.model import initLogger, pythontools
from .LaserManager import LaserManager, normalise_ports
import traceback
import importlib


class Cobolt0601NewLaserManager(LaserManager):
    """ LaserManager for Cobolt 06-01 lasers. Uses digital modulation mode when
    scanning. Does currently not support DPL type lasers.

    Manager properties:

    - ``digitalPorts`` -- a string array containing the COM ports to connect
      to, e.g. ``["COM4"]``
    """

    def __init__(self, laserInfo, name, **_lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)

        # Lazy import of hardware library
        try:
            from .PyCoboltManager import Cobolt06
            self._Cobolt06 = Cobolt06
        except ImportError as e:
            self.__logger.error(f'Failed to import PyCoboltManager library: {e}')
            raise

        self._port = normalise_ports(laserInfo.managerProperties['digitalPorts'])[0]
        # self._ttlLine = laserInfo.managerProperties['digitalLine']
        self.__logger.debug(f'Initializing Cobolt0601 laser (name: {name}) on port {self._port}')
        self._is_DPL = False
        self._digitalMod = True
        self.powerQ = 0
        if 'DPL' in name:
            self._is_DPL = True
        try:
            # self._laser = CoboltLaser(port=self._port)
            self._laser = self._Cobolt06(port=self._port)
            self._digitalMod = False

            # start up by turning on modulation power -> laser is off
            self._laser.constant_current(0)
            # check mode of laser — older firmware rejects `laser:runmode?`,
            # so treat it as informational only.
            try:
                mode = self._laser.get_mode()
            except Exception:
                mode = '<unknown — firmware rejected laser:runmode?>'
            super().__init__(laserInfo, name, isBinary=False, valueUnits='mW', valueDecimals=0)

            if not self._laser.is_on():
                try: 
                    self._laser.turn_on() # turn on laser
                    self.setEnabled(False) # pause emission
                    self.__logger.debug(f'Laser {name} turned on, mode {mode} - emission paused. Might have to turn the key.')
                except Exception as e:
                    err = traceback.format_exc()
                    self.__logger.warning(f'Laser {name} could not be turned on: {err}')


        except Exception as e:
            self.__logger.error(
                f'Failed to initialize Cobolt0601 laser (name: {name}) on port {self._port}, loading mocker.')
            package = importlib.import_module(
                pythontools.joinModulePath('imswitch.imcontrol.model.lantzdrivers_mock.cobolt.', 'cobolt0601')
            )
            driver = getattr(package, 'MockCobolt06')
            self._laser = driver(self._port)
            self._laser.initialize()
            super().__init__(laserInfo, name, isBinary=False, valueUnits='mW', valueDecimals=0)
    
    def finalize(self):
        """ Turn off laser — always attempt turn_off regardless of is_on(),
        because l? is absent on older firmware and always returns False. """
        try:
            self._pause_emission_safe()   # safest first: gate off (or current=0 on older firmware)
        except Exception:
            pass
        try:
            self._laser.turn_off()
        except Exception as e:
            err = traceback.format_exc()
            self.__logger.warning(f'Laser could not be turned off properly: {err}.')

    # Cached "off method" — set on first successful shutdown so we don't
    # spam the laser with commands its firmware rejects.
    # Values: None (unknown), 'paus' (SCPI las:paus), 'power0' (set_power 0),
    # 'l0' (full turn_off — nuclear option, requires warm-up to re-enable).
    _off_method = None

    @staticmethod
    def _is_illegal(reply):
        """True if a Cobolt reply indicates the command was rejected."""
        if reply is None:
            return False
        r = str(reply).lower()
        return 'illegal command' in r or 'syntax error' in r

    def _pause_emission_safe(self):
        """Turn the beam off. Cascade through methods until one succeeds and
        cache the winner for the rest of the session."""
        # Cached path
        if self._off_method == 'paus':
            self._laser.pause_emission()
            return
        if self._off_method == 'power0':
            self._laser.set_power(0)
            self._laser.constant_power()
            return
        if self._off_method == 'l0':
            self._laser.turn_off()
            return

        # First call — try in order of "least invasive that actually works".
        # 1. SCPI las:paus (newer firmware).
        try:
            reply = self._laser.pause_emission()
            if not self._is_illegal(reply):
                self._off_method = 'paus'
                return
        except Exception as e:
            self.__logger.debug(f'pause_emission raised: {e}')

        # 2. Older 06-01: set_power(0) + constant_power. Should result in
        #    zero emission since power setpoint is zero.
        try:
            self._laser.set_power(0)
            reply = self._laser.constant_power()
            if not self._is_illegal(reply):
                self.__logger.warning(
                    'Laser firmware rejects `las:paus`; using set_power(0) to dark the beam.'
                )
                self._off_method = 'power0'
                return
        except Exception as e:
            self.__logger.debug(f'set_power(0) path raised: {e}')

        # 3. Nuclear option — full shutdown. The laser will need an autostart
        #    (`@cob1`) and warm-up to come back, but the beam will be OFF.
        self.__logger.warning(
            'Falling back to full turn_off (l0) — laser will require warm-up to re-enable.'
        )
        try:
            self._laser.turn_off()
            self._off_method = 'l0'
        except Exception as e:
            self.__logger.error(f'CRITICAL: could not turn off laser via any path: {e}')
            raise

    def _resume_emission_safe(self):
        """Resume emission. Matches whichever shutdown method was cached."""
        if self._off_method == 'paus':
            self._laser.resume_emission()
        elif self._off_method == 'l0':
            # Came from a full shutdown — restart with autostart sequence.
            try:
                self._laser.turn_on()
            except Exception as e:
                self.__logger.warning(f'turn_on after l0 failed: {e}')
        # For 'power0' and None, the constant_power() call in setEnabled(True)
        # below restores emission once the power setpoint is set.

    def setEnabled(self, enabled):  # toggle laser on or off
        if enabled:  # laser is toggled on
            self._resume_emission_safe()
            # constant_power() with no arg just enters CP mode; the actual
            # power setpoint is whatever setValue last wrote.
            self._laser.constant_power()
        else:
            self._pause_emission_safe()

    def setValue(self, power):
        power = int(power)
        self.powerQ = power
        if self._digitalMod:
            if power == 0:
                self._laser.current_modulation_mode()
                self._laser.set_modulation_current(0.1)
                self.__logger.debug(f'Modulation current in setValue is: {self._laser.get_modulation_current()}')

            else :
                self._laser.power_modulation_mode()
                self._laser.set_modulation_power(power)
                self.__logger.debug(f'Modulation power in setValue is: {self._laser.get_modulation_power()}')
        else:
            self._laser.set_power(power)
            self.__logger.debug(f'Set power to: {power}')

    def setScanModeActive(self, active):
        if not active:  # Come back to values set before scan
            self._digitalMod = False
            self._laser.constant_power()  # If laser should be disabled, turn off by setting scanmode to active -> modulation mode
            self.__logger.debug('Exited digital modulation mode')
        else:
            if self.powerQ == 0: #To be sure the laser doesn't emit
                self._laser.current_modulation_mode()
                self._laser.set_modulation_current(0.1)
            else:
                self._laser.power_modulation_mode()
                self.__logger.debug(f'Modulation power is: {self._laser.get_modulation_power()}')
                self._laser.set_modulation_power(self.powerQ)
                self.__logger.debug(f'Modulation power is: {self._laser.get_modulation_power()}')


            # self.setModulationPower(powerQ)
            # powerQ = self._laser.power_sp * self._numLasers

            self.__logger.debug('Entered digital modulation mode')
            self.__logger.debug(f'Modulation mode is: {self._laser.get_modulation_state}')
        self._digitalMod = active
        # TODO
        # this is needed when imswitch is handling the scan
        # for now, arduino is handling the scanning
        # once the camera is not exposing the laser will not be on whilst digital modulation is set
        pass

    def setModulationEnabled(self, enabled):
        if enabled:
            self._laser.power_modulation_mode(digital_enabled=True)
        else:
            self._laser.power_modulation_mode(digital_enabled=False)

    def setModulationPower(self, power):
        power = int(power)
        self._laser.set_modulation_power(power)
        self.__logger.debug(f'Set modulation power to: {power}')

    def getModulationPower(self):
        return self._laser.get_modulation_power()

    def getAllDeviceNames(self):  # wonder where thats needed
        try:
            from .PyCoboltManager import list_lasers
        except ImportError as e:
            self.__logger.error(f'Failed to import list_lasers: {e}')
            return []
        self.__logger.debug(f'Available devices: {list_lasers()}')
        return list_lasers()

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