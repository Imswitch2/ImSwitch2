"""Manager for Thorlabs K10CR1 Kinesis rotation mounts."""

from imswitch.imcommon.model import initLogger
from .RotatorManager import RotatorManager


class KinesisRotatorManager(RotatorManager):
    """RotatorManager for Thorlabs K10CR1 motorized rotation mounts.
    
    Supports both real hardware (via pylablib) and headless mock operation.
    
    Manager properties:
        - snr (str, required): Device serial number.
        - unitsPerDegree (float, default 136533.33): Encoder units per degree.
        - homeOnInit (bool, default False): If True, home the motor on init.
    """

    def __init__(self, rotatorInfo, name: str, *args, **kwargs):
        super().__init__(rotatorInfo, name, *args, **kwargs)
        self.__logger = initLogger(self)

        if rotatorInfo is None:
            return

        self._snr = rotatorInfo.managerProperties['snr']
        self._units_per_dg = rotatorInfo.managerProperties.get('unitsPerDegree', 136533.33)
        home_on_init = rotatorInfo.managerProperties.get('homeOnInit', False)

        self._motor = self._getMotorObj(self._snr, self._units_per_dg)

        if home_on_init:
            self.__logger.info(f'Homing Kinesis rotator {self._snr}')
            self._motor.home()

        self._update_position()

    def move_abs(self, pos_deg: float) -> None:
        """Move to an absolute position in degrees.
        
        Args:
            pos_deg: Target position in degrees.
        """
        encoder_pos = int(pos_deg * self._units_per_dg)
        self._motor.move_to(encoder_pos)
        self._motor.wait_move()
        self._update_position()

    def move_rel(self, d_deg: float) -> None:
        """Move by a relative displacement in degrees.
        
        Args:
            d_deg: Relative displacement in degrees.
        """
        encoder_delta = int(d_deg * self._units_per_dg)
        self._motor.move_by(encoder_delta)
        self._motor.wait_move()
        self._update_position()

    def _update_position(self) -> None:
        """Read current encoder position and convert to degrees."""
        raw_pos = self._motor.get_position()
        self._position = raw_pos / self._units_per_dg

    def _getMotorObj(self, snr: str, units_per_dg: float):
        """Instantiate the motor driver with real-then-mock fallback."""
        try:
            from imswitch.imcontrol.model.interfaces.kinesisrotator import KinesisMotor
            motor = KinesisMotor(snr, units_per_dg)
            self.__logger.info(f'Initialized Thorlabs Kinesis rotator {snr}')
        except Exception as e:
            self.__logger.warning(
                f'Failed to initialize Kinesis rotator {snr} (real hardware): {e}'
            )
            self.__logger.warning('Loading mock Kinesis motor for headless operation')
            from imswitch.imcontrol.model.interfaces.kinesisrotator import MockKinesisMotor
            motor = MockKinesisMotor(snr, units_per_dg)
        return motor

    def finalize(self) -> None:
        """Close the motor connection."""
        self._motor.close()


# Copyright (C) 2020-2026 ImSwitch developers
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
