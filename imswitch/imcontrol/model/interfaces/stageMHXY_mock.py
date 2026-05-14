from imswitch.imcommon.model import initLogger


class MockMHXYStage:

    def __init__(self, SerialDriver=0):
        self.__logger = initLogger(self, tryInheritParent=True)
        self.__logger.debug('Simulated Marzhauser XY-stage')
        self._absX = 0.0
        self._absY = 0.0

    @property
    def idn(self):
        """Get information of device"""
        return 'Marzhauser XY-stage mock'

    # XY-POSITION READING AND MOVEMENT

    @property
    def absX(self):
        """ Read absolute X position, in um. """
        self.__logger.debug("Mock MHXY: Absolute position, X.")
        return self._absX

    @property
    def absY(self):
        """ Read absolute Y position, in um. """
        self.__logger.debug("Absolute position, Y.")
        return self._absY

    def move_relX(self, value):
        """ Relative X position movement, in um. """
        self.__logger.debug(f"Move relative, X: {value} um.")

    def move_relY(self, value):
        """ Relative Y position movement, in um. """
        self.__logger.debug(f"Move relative, Y: {value} um.")

    def move_absX(self, value):
        """ Absolute X position movement, in um. """
        self.__logger.debug(f"Set position, X: {value} um.")

    def move_absY(self, value):
        """ Absolute Y position movement, in um. """
        self.__logger.debug(f"Set position, Y: {value} um.")

    # CONTROL/STATUS/LIMITS

    @property
    def circLimit(self):
        """ Circular limits, in terms of X,Y center and radius. """
        self.__logger.debug("Ask circular limits.")

    @circLimit.setter
    def circLimit(self, value):
        self.__logger.debug(f"Set circular limits: {value}.")

    def function_press(self):
        """ Check function button presses. """
        self.__logger.debug("Check button presses.")

    def initialize(self):
        pass

    def finalize(self):
        pass

    def close(self):
        pass


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
