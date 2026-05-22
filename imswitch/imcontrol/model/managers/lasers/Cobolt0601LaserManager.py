from imswitch.imcommon.model import initLogger
from .LantzLaserManager import LantzLaserManager


class Cobolt0601LaserManager(LantzLaserManager):
    """ LaserManager for Cobolt 06-01 lasers. Uses digital modulation mode when
    scanning. Does currently not support DPL type lasers.

    Manager properties:

    - ``digitalPorts`` -- a string array containing the COM ports to connect
      to, e.g. ``["COM4"]``
    """

    def __init__(self, laserInfo, name, **_lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)

        super().__init__(laserInfo, name, isBinary=False, valueUnits='mW', valueDecimals=0,
                         driver='cobolt.cobolt0601.Cobolt0601_f2', **_lowLevelManagers)

        self._digitalMod = False
        self._laser.enabled = False      # l0 first — ensure laser is off before any mode changes
        self._laser.digital_mod = False  # sdmes 0 — disable TTL gate
        self._laser.query('cp')          # enter constant-power mode (known clean state)
        self._laser.mode = 'APC'
        self._laser.autostart = False

    def setEnabled(self, enabled):
        self.__logger.debug(f'Laser turning {enabled}')
        self._laser.enabled = enabled

    def setValue(self, power, enabled=True, for_scanning=False):
        power = float(power)
        if self._digitalMod:
            self._setModPower(power)
        else:
            self._setBasicPower(power)

    def setScanModeActive(self, active):
        if active:
            powerQ = self._laser.power_sp * self._numLasers
            self.__logger.debug(f'setScanModeActive → active, powerQ={powerQ:.3f} mW, gam={self._laser._safe_query("gam?")}')
            self._laser.enter_mod_mode()   # em — enter modulation mode
            self.__logger.debug(f'  after em: gam={self._laser._safe_query("gam?")}')
            self._laser.digital_mod = True # sdmes 1 — enable TTL gate
            self.__logger.debug(f'  after sdmes 1: gdmes={self._laser._safe_query("gdmes?")}')
            self._setModPower(powerQ)      # slmp X — power when TTL is HIGH
            self.__logger.debug(f'  after slmp: glmp={self._laser._safe_query("glmp?")}')
        else:
            self._laser.digital_mod = False
            # we go back to the mode before the scan
            if self._laser.mode == 'ACC':
                self._laser.query('ci')
            else:
                self._laser.query('cp')
            self.__logger.debug('setScanModeActive → inactive, returned to CP/CC')

        self._digitalMod = active

    def _setBasicPower(self, power):
        if power == 0:
            self._laser.mode = 'ACC'
            self._laser.query('ci')
            self._laser.query('slc {:.1f}'.format(0))
        else:
            if self._laser.mode != 'APC':
                self._laser.power_sp = 0
                self._laser.query('cp')
                self._laser.mode = 'APC'
            self._laser.power_sp = power / self._numLasers


    def _setModPower(self, power):
        self._laser.power_mod = power / self._numLasers
        #self.__logger.debug(f'Set digital modulation mode power to: {power}')


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
