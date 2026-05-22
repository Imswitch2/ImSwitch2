from imswitch.imcommon.model import initLogger
from .LaserManager import LaserManager


class TriggerScopeLaserManager(LaserManager):
    """ LaserManager for lasers wired to a TriggerScope DAC/TTL channel.

    Delegates analog voltage and digital enable/disable to the board-level
    ``TriggerScopeManager`` via ``lowLevelManagers['triggerScopeManager']``.

    Manager properties:

    - ``conversionFactor`` -- reserved for future use (not currently applied)
    - ``minVolt`` -- minimum allowed DAC voltage
    - ``maxVolt`` -- maximum allowed DAC voltage
    """

    def __init__(self, laserInfo, name, **lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)
        self._triggerScopeManager = lowLevelManagers['triggerScopeManager']
        self._minVolt = laserInfo.managerProperties['minVolt']
        self._maxVolt = laserInfo.managerProperties['maxVolt']
        super().__init__(laserInfo, name,
                         isBinary=laserInfo.getAnalogChannel() is None,
                         valueUnits='V', valueDecimals=2)

    def setEnabled(self, enabled):
        try:
            self._triggerScopeManager.setDigital(self.name, enabled)
        except Exception as e:
            self.__logger.error(f'Error enabling laser "{self.name}": {e}')

    def setValue(self, voltage):
        if self.isBinary:
            return
        try:
            self._triggerScopeManager.setAnalog(target=self.name, voltage=voltage)
        except Exception as e:
            self.__logger.error(f'Error setting voltage for laser "{self.name}": {e}')

    def setScanModeActive(self, active):
        if active:
            self.setEnabled(False)


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
