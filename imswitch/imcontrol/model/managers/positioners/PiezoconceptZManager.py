from imswitch.imcommon.model import initLogger
from ._PiezoconceptZSerialMixin import PiezoconceptZSerialMixin
from .PositionerManager import PositionerManager
from imswitch.imcontrol.model.devices.graph import rs232BackedPrimarySpec


class PiezoconceptZManager(PiezoconceptZSerialMixin, PositionerManager):
    """ PositionerManager for control of a Piezoconcept Z-piezo through RS232
    communication.

    Manager properties:

    - ``rs232device`` -- name of the defined rs232 communication channel
      through which the communication should take place
    """

    def __init__(self, positionerInfo, name, *args, **lowLevelManagers):
        if len(positionerInfo.axes) != 1:
            raise RuntimeError(f'{self.__class__.__name__} only supports one axis,'
                               f' {len(positionerInfo.axes)} provided.')

        super().__init__(positionerInfo, name, initialPosition={
            axis: 0 for axis in positionerInfo.axes
        })
        self.__logger = initLogger(self, instanceName=name)
        self._initPiezoconceptSerial(self.__logger)
        try:
            self._rs232Manager = lowLevelManagers['rs232sManager'][
                positionerInfo.managerProperties['rs232device']
            ]
        except Exception as e:
            self.__logger.warning(
                f'Failed to initialize rs232sManager, falling back to mock mode: {e}'
            )
            from imswitch.imcontrol.model.interfaces.RS232Driver_mock import MockRS232Driver
            self._rs232Manager = MockRS232Driver(name='mock', settings={'port': 'Mock'})
            self._setConnectionError(
                e,
                summary="Piezoconcept RS232 backend unavailable; mock fallback active",
                mock_active=True,
            )


    def getDeviceDescriptorSpec(self):
        rs232_name = (self._positionerInfo.managerProperties or {}).get('rs232device')
        return rs232BackedPrimarySpec(
            category='positioner',
            rs232_name=str(rs232_name),
        )

    def move(self, value, _):
        if float(value) > 0:
            cmd = 'MOVRZ +' + str(round(float(value), 3))[0:6] + 'u'
        elif float(value) < 0:
            cmd = 'MOVRZ -' + str(round(float(value), 3))[1:7] + 'u'
        else:
            return
        with self._piezoconceptSerialLock:
            self._queryPiezoconceptMove(cmd)
            self._position[self.axes[0]] = self._position[self.axes[0]] + value

    def setPosition(self, value, _):
        self.__logger.debug(f"Set position to: {value}")
        cmd = 'MOVEZ ' + str(round(float(value), 3)) + 'u'
        with self._piezoconceptSerialLock:
            self._queryPiezoconceptMove(cmd)
            self._position[self.axes[0]] = value

    @property
    def position(self):
        _ = self.get_abs()
        return self._position

    def get_abs(self, axis=None):
        cmd = 'GET_Z'
        with self._piezoconceptSerialLock:
            reply = self._queryPiezoconceptPosition(cmd)
            if reply is None:
                return self._position[self.axes[0]]
            self._position[self.axes[0]] = reply
            return reply


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
