from imswitch.imcommon.model import initLogger


class MockPCZPiezo:
    """Mock driver for the PiezoConcept Z-piezo."""

    def __init__(self):
        self.__logger = initLogger(self, tryInheritParent=True)
        self._absZ = 2.0
        self._timeStep = 1

    @property
    def idn(self):
        """Get information of device"""
        return 'dummy zpiezo answer'

    def initialize(self):
        pass

    def finalize(self):
        pass

    def close(self):
        pass

    # Z-MOVEMENT

    @property
    def absZ(self):
        """ Absolute Z position. """
        return self._absZ

    @absZ.setter
    def absZ(self, value):
        """ Absolute Z position movement, in um. """
        self.__logger.debug(f"setting Z position to {value} um")
        self._absZ = value

    def relZ(self, value):
        """ Relative Z position movement, in um. """
        self.__logger.debug(f"Moving Z position {value} um")
        if abs(float(value)) > 0.5:
            self.__logger.warning('Warning: Step bigger than 500 nm')

    def move_relZ(self, value):
        """ Relative Z position movement, in um. """
        self.__logger.debug(f"Moving Z position {value} um")
        if abs(float(value)) > 0.5:
            self.__logger.warning('Warning: Step bigger than 500 nm')

    def move_absZ(self, value):
        """ Absolute Z position movement, in um. """
        self.__logger.debug(f"Setting Z position to {value} um")

    # CONTROL/STATUS

    @property
    def timeStep(self):
        """ Time between points sent by the USB interface, in ms. """
        return self._timeStep

    @timeStep.setter
    def timeStep(self, value):
        self._timeStep = value


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
