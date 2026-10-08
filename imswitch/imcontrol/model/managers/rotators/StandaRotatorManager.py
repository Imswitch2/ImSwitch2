from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.devices.status import backend_attribute
from .RotatorManager import RotatorManager


class StandaRotatorManager(RotatorManager):
    """ StandaMotorManager that deals with a Standa-branded motor controller,
    for example 8SMC5 for a motorized rotation mount. 
    """
    def __init__(self, rotatorInfo, name, *args, **kwargs):
        super().__init__(rotatorInfo, name, *args, **kwargs)
        self.__logger = initLogger(self)

        if rotatorInfo is None:
            return
        self._device_id = rotatorInfo.managerProperties['motorListIndex']
        self._lib_loc = rotatorInfo.managerProperties['ximcLibLocation']
        self._steps_per_turn = rotatorInfo.managerProperties['stepsPerTurn']
        self._microsteps_per_step = rotatorInfo.managerProperties['microstepsPerStep']

        properties = rotatorInfo.managerProperties
        self._installBackend(
            lambda: self._getMotorObj(self._device_id, self._lib_loc,
                                      self._steps_per_turn, self._microsteps_per_step),
            lambda: self._getMockMotorObj(self._lib_loc),
            label=f'Standa motor {self._device_id}',
            use_mock_on_failure=bool(properties.get('useMockOnFailure', False)),
            transient=bool(getattr(rotatorInfo, 'transient', False)),
            connect_on_startup=bool(getattr(rotatorInfo, 'connectOnStartup', False)),
        )
        if self.backendHolder.backend:
            self.get_pos()

    def readPosition(self):
        """ Fresh position in degrees (waits until the motor has stopped). """
        self.get_pos()
        return self._position

    #: The driver, read from the backend holder: a reconnect replaces it.
    _motor = backend_attribute()

    @property
    def isSimulated(self) -> bool:
        """ True unless a real controller is connected: a mock, an absent
        device, and also libximc's virtual controller (opened silently when
        no controller is found) or a library that did not load -- none of
        them moves a real mount. """
        if not self.backendIsReal:
            return True
        motor = self._motor
        return bool(getattr(motor, 'emulated', False)
                    or not getattr(motor, '_imported', True))

    def get_info(self):
        info = self._motor.test_info()
        for info_piece in info:
            self.__logger.debug(f'{info_piece}: {info[info_piece]}')

    def get_pos(self):
        self._position = self._motor.get_pos()

    def move_rel(self, move_dist):
        self._motor.moverel(move_dist)
        self.get_pos()

    def move_abs(self, move_pos):
        self._motor.moveabs(move_pos)
        self.get_pos()

    def set_zero_pos(self):
        self._motor.set_zero_pos()
        self.get_pos()

    def set_rot_speed(self, speed):
        self._motor.set_rot_speed(speed)

    def start_cont_rot(self):
        self._motor.start_cont_rot()

    def stop_cont_rot(self):
        self._motor.stop_cont_rot()

    def set_sync_in_set(self, abs_pos_deg, rel_shift, enabled):
        self._motor.set_sync_in_settings(abs_pos_deg, rel_shift, enabled)

    def _getMotorObj(self, device_id, lib_loc, steps_per_turn, microsteps_per_step):
        """Open the real motor; raises when the library or controller is
        absent (check that ``ximcLibLocation`` is reachable -- often a
        network drive)."""
        from imswitch.imcontrol.model.interfaces.standamotor import StandaMotor
        motor = StandaMotor(device_id, lib_loc, steps_per_turn, microsteps_per_step)
        if not getattr(motor, '_imported', True):
            motor.close()
            raise RuntimeError(f'pyximc did not load from {lib_loc!r}')
        self.__logger.info(f'Initialized Standa motor {device_id}')
        return motor

    @staticmethod
    def _getMockMotorObj(lib_loc):
        from imswitch.imcontrol.model.interfaces.standamotor import MockStandaMotor
        return MockStandaMotor(lib_loc)

    def _lifecycleReinitialise(self) -> None:
        self.get_pos()

    def close(self):
        self.backendHolder.close(suppress_errors=False)
        self._setFinalizedStatus()


# Copyright (C) 2020-2023 ImSwitch developers
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
