import numpy as np
from imswitch.imcommon.model import initLogger
from .LaserManager import LaserManager


class ESP32LEDMatrixManager(LaserManager):
    """ LaserManager for an RGB LED matrix connected to a UC2/ESP32 board.

    Sends the full 3×N×N pattern array to the board on every enable/value
    change via the UC2-REST ``send_LEDMatrix_array`` call.

    Manager properties:

    - ``rs232device`` -- name of the defined RS232/ESP32 communication channel
    - ``n_leds`` -- side length of the square LED matrix (default 4, so 4×4=16 LEDs)
    """

    def __init__(self, laserInfo, name, **lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)
        self._enabled = False
        self._power = 0
        n = laserInfo.managerProperties.get('n_leds', 4)
        self._I_max = 255
        self._led_pattern = np.zeros((3, n, n), dtype=np.uint8)

        self._rs232manager = lowLevelManagers['rs232sManager'][
            laserInfo.managerProperties['rs232device']
        ]

        super().__init__(laserInfo, name, isBinary=False, valueUnits='arb', valueDecimals=0)

    def setEnabled(self, enabled):
        self._enabled = enabled
        self._send_pattern()

    def setValue(self, power):
        self._power = int(round(power))
        n = self._led_pattern.shape[1]
        self._led_pattern = np.full((3, n, n), self._power, dtype=np.uint8)
        self._send_pattern()

    def _send_pattern(self):
        pattern = self._led_pattern if self._enabled else np.zeros_like(self._led_pattern)
        self._rs232manager.send_LEDMatrix_array(pattern)


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
