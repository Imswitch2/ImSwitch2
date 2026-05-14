from imswitch.imcommon.model import initLogger
from .LaserManager import LaserManager

class ESP32LEDLaserManager(LaserManager):
    """ LaserManager for controlling LEDs and Lasers connected to an 
    ESP32 exposing a REST API
    Each LaserManager instance controls one LED.

    Manager properties:

    - ``rs232device`` -- name of the defined rs232 communication channel
      through which the communication should take place
    - ``channel_index`` -- laser channel (A to H)
    """

    def __init__(self, laserInfo, name, **lowLevelManagers):
        super().__init__(laserInfo, name, isBinary=False, valueUnits='mW', valueDecimals=0)
        self.__logger = initLogger(self, instanceName=name)
        self._isMock = False
        self.power = 0
        self.enabled = False
        
        try:
            self._rs232manager = lowLevelManagers['rs232sManager'][
                laserInfo.managerProperties['rs232device']
            ]
            self.__channel_index = laserInfo.managerProperties['channel_index']
        except Exception as e:
            self._isMock = True
            self.__logger.warning(
                f'Failed to initialize ESP32LED hardware, running in mock mode: {e}'
            )
            self._rs232manager = None
            self.__channel_index = laserInfo.managerProperties.get('channel_index', 0)
        

    def setEnabled(self, enabled):
        """Turn on (N) or off (F) laser emission"""
        self.enabled = enabled
        
        if self._isMock:
            self.__logger.debug(f'Mock mode: setEnabled({enabled}) ignored')
            return
        
        self._rs232manager._squid.set_laser(self.__channel_index, self.power if self.enabled else 0)
        

    def setValue(self, power):
        """Handles output power.
        Sends a RS232 command to the laser specifying the new intensity.
        """
        self.power = power
        
        if self._isMock:
            self.__logger.debug(f'Mock mode: setValue({power}) ignored')
            return
        
        if self.enabled:
            self._rs232manager._squid.set_laser(self.__channel_index, self.power)



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
