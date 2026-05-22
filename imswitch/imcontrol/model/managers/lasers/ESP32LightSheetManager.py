from imswitch.imcommon.model import initLogger
from .LaserManager import LaserManager


class ESP32LightSheetManager(LaserManager):
    """ LaserManager for controlling a galvo-based light sheet via a
    UC2/ESP32 board (UC2-REST interface).

    Each instance controls one galvo parameter (frequency or amplitude)
    on one DAC axis, configured by the ``axis`` manager property.

    Manager properties:

    - ``rs232device`` -- name of the defined RS232/ESP32 communication channel
    - ``axis`` -- one of:
        - ``"freq_0"`` / ``"freq_1"`` -- set galvo DAC frequency on axis 0 or 1
        - ``"amp_0"``  / ``"amp_1"``  -- set galvo DAC amplitude on axis 0 or 1

    Note: the original ``"pos_x"`` / ``"amp_y"`` axis names from old firmware
    are mapped to ``"freq_0"`` / ``"amp_1"`` respectively. Update the JSON
    config to the new names for clarity.
    """

    # Legacy axis-name aliases from old firmware REST endpoints
    _LEGACY_AXIS_MAP = {
        'pos_x': 'freq_0',
        'amp_y': 'amp_1',
    }

    def __init__(self, laserInfo, name, **lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)
        super().__init__(laserInfo, name, isBinary=False, valueUnits='arb', valueDecimals=0)
        self._rs232manager = lowLevelManagers['rs232sManager'][
            laserInfo.managerProperties['rs232device']
        ]
        axis_raw = laserInfo.managerProperties['axis']
        self.__axis = self._LEGACY_AXIS_MAP.get(axis_raw, axis_raw)
        if axis_raw in self._LEGACY_AXIS_MAP:
            self.__logger.warning(
                f'Axis name "{axis_raw}" is a legacy alias; '
                f'update config to "{self.__axis}"'
            )

    def setEnabled(self, enabled):
        pass

    def setValue(self, value=0):
        if self._rs232manager._esp32 is None:
            self.__logger.warning('ESP32 not connected, galvo update ignored')
            return
        try:
            kind, axis_str = self.__axis.split('_')
            axis = int(axis_str)
        except (ValueError, AttributeError):
            self.__logger.error(f'Unknown axis format "{self.__axis}"')
            return

        if kind == 'freq':
            self._rs232manager._esp32.set_galvo_freq(axis=axis, value=value)
        elif kind == 'amp':
            self._rs232manager._esp32.set_galvo_amp(axis=axis, value=value)
        else:
            self.__logger.error(f'Unknown axis kind "{kind}", expected "freq" or "amp"')


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
