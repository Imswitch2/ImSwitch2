from .LaserManager import LaserManager
from imswitch.imcommon.model import initLogger


class OxxiusCombinerLaserManager(LaserManager):
    """ LaserManager for controlling one channel of an AA Opto-Electronic
    acousto-optic modulator/tunable filter through RS232 communication.

    Manager properties:

    - ``rs232device`` -- name of the defined rs232 communication channel
      through which the communication should take place
    - ``channel`` -- index of the channel in the acousto-optic device that
      should be controlled (indexing starts at 1)
    """

    def __init__(self, laserInfo, name, **lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)
        self._channel = int(laserInfo.managerProperties['channel'])
        self._rs232manager = lowLevelManagers['rs232sManager'][
            laserInfo.managerProperties['rs232device']
        ]

        self.blankingOn()
        self.internalControl()

        super().__init__(laserInfo, name, isBinary=False, valueUnits='arb', valueDecimals=0)

    def setEnabled(self, enabled):
        """Turn on (1) or off (0) laser emission"""
        if enabled:
            value = 1
        else:
            value = 0
        cmd = 'SH' + str(self._channel) +' '+ str(value)
        self._rs232manager.query(cmd)

    def setValue(self, power):
        """Handles output power.
        Sends a RS232 command to the laser specifying the new intensity.
        """
        valueaotf = round(power)  # assuming input value is [0,100]
        cmd = 'L' + str(self._channel) + ' C ' + str(valueaotf)
        self._rs232manager.query(cmd)

    def blankingOn(self):
        """Disable digital modulation"""
        cmd = 'L' + str(self._channel) + ' CW 1'
        self._rs232manager.query(cmd)

    def blankingExt(self):
        """Switch the banking to external"""
        cmd = 'L' + str(self._channel) + ' CW 0'
        self._rs232manager.query(cmd)

    def setScanModeActive(self, active):
        if active:
            #powerQ = self._laser.power_sp * self._numLasers
            #self._laser.enter_mod_mode()
            #self._setModPower(powerQ)
            self.blankingExt()

            #self.internalControl()
            self.__logger.debug('Entered digital modulation mode')

        else:
            #self._laser.digital_mod = False
            #self._laser.query('cp')
            self.blankingOn()
            self.__logger.debug('Exited digital modulation mode')

        #self._digitalMod = active

    def internalControl(self):
        """Switch the channel to external control"""
        cmd = 'ACC' + ' 1'
        self._rs232manager.query(cmd)


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
