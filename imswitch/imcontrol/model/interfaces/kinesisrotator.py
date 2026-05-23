"""Interface to Thorlabs K10CR1 Kinesis rotation mount via pylablib.

Provides real and mock driver classes for headless operation.
"""

from imswitch.imcommon.model import initLogger


class KinesisMotor:
    """Thin wrapper around pylablib's KinesisMotor device."""

    def __init__(self, snr: str, units_per_dg: float):
        """
        Args:
            snr: Device serial number.
            units_per_dg: Encoder units per degree.
        """
        self.__logger = initLogger(self)
        from pylablib.devices.Thorlabs import KinesisMotor as _KinesisMotor
        self._stage = _KinesisMotor(snr, is_rack_system=True)
        self._units_per_dg = units_per_dg
        self.__logger.info(f'Initialized Thorlabs Kinesis rotator {snr}')

    def get_position(self) -> int:
        """Return current position in raw encoder units."""
        return self._stage.get_position()

    def move_to(self, position: int) -> None:
        """Move to an absolute encoder position."""
        self._stage.move_to(position)

    def move_by(self, delta: int) -> None:
        """Move by a relative encoder displacement."""
        self._stage.move_by(delta)

    def wait_move(self) -> None:
        """Block until the current move completes."""
        self._stage.wait_move()

    def home(self) -> None:
        """Home the motor to zero position."""
        self._stage.home()
        self._stage.wait_move()

    def close(self) -> None:
        """Disconnect from the device."""
        self._stage.close()


class MockKinesisMotor:
    """Simulated Kinesis motor for headless operation."""

    def __init__(self, snr: str, units_per_dg: float):
        """
        Args:
            snr: Device serial number (for logging).
            units_per_dg: Encoder units per degree (stored but not used).
        """
        self.__logger = initLogger(self)
        self._snr = snr
        self._units_per_dg = units_per_dg
        self._position = 0
        self.__logger.info(f'Initialized mock Kinesis rotator {snr}')

    def get_position(self) -> int:
        """Return current position in raw encoder units."""
        return self._position

    def move_to(self, position: int) -> None:
        """Move to an absolute encoder position (instant)."""
        self._position = position
        self.__logger.debug(f'Mock rotator {self._snr}: moved to {position} (encoder units)')

    def move_by(self, delta: int) -> None:
        """Move by a relative encoder displacement (instant)."""
        self._position += delta
        self.__logger.debug(f'Mock rotator {self._snr}: moved by {delta} to {self._position}')

    def wait_move(self) -> None:
        """No-op; moves are instant in the mock."""
        pass

    def home(self) -> None:
        """Home the motor to zero position."""
        self._position = 0
        self.__logger.debug(f'Mock rotator {self._snr}: homed to 0')

    def close(self) -> None:
        """No-op cleanup."""
        pass


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
