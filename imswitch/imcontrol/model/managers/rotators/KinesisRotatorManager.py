"""Manager for Thorlabs K10CR1 Kinesis rotation mounts."""

from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.devices.status import backend_attribute
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

        properties = rotatorInfo.managerProperties
        is_mock = str(self._snr).upper().startswith('MOCK')
        open_motor = lambda: self._getMotorObj(self._snr, self._units_per_dg)
        self._installBackend(
            open_motor,
            # A MOCK_ serial is a configured mock, opened by the same factory
            # (which returns the mock for it); the fallback mock is separate.
            open_motor if is_mock else (
                lambda: self._getMockMotorObj(self._snr, self._units_per_dg)),
            label=f'Kinesis rotator {self._snr}',
            configured_mock=is_mock,
            use_mock_on_failure=bool(properties.get('useMockOnFailure', False)),
            transient=bool(getattr(rotatorInfo, 'transient', False)),
            connect_on_startup=bool(getattr(rotatorInfo, 'connectOnStartup', False)),
        )

        if not self.backendHolder.backend:
            return                      # not connected: nothing to home or read
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

    def readPosition(self):
        self._update_position()
        return self._position

    #: The driver, read from the backend holder: a reconnect replaces it.
    _motor = backend_attribute()

    def _update_position(self) -> None:
        """Read current encoder position and convert to degrees."""
        raw_pos = self._motor.get_position()
        self._position = raw_pos / self._units_per_dg

    def _getMotorObj(self, snr: str, units_per_dg: float):
        """Open the real motor driver; raises when the hardware is absent."""
        if str(snr).upper().startswith('MOCK'):
            return self._getMockMotorObj(snr, units_per_dg)
        from imswitch.imcontrol.model.interfaces.kinesisrotator import KinesisMotor
        motor = KinesisMotor(snr, units_per_dg)
        self.__logger.info(f'Initialized Thorlabs Kinesis rotator {snr}')
        return motor

    @staticmethod
    def _getMockMotorObj(snr: str, units_per_dg: float):
        from imswitch.imcontrol.model.interfaces.kinesisrotator import MockKinesisMotor
        return MockKinesisMotor(snr, units_per_dg)

    def _lifecycleReinitialise(self) -> None:
        self._update_position()

    def finalize(self) -> None:
        """Close the motor connection."""
        self.backendHolder.close(suppress_errors=False)
        self._setFinalizedStatus()


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
