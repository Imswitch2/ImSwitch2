"""Interface to Thorlabs MLS203 Kinesis XY stage via pylablib.

Provides real and mock driver classes for headless operation.
"""

from imswitch.imcommon.model import initLogger


class KinesisStage:
    """Thin wrapper around pylablib's KinesisMotor device for two-axis stages."""

    def __init__(self, snr: str, scale: str = "MLS203", is_rack_system: bool = True):
        """
        Args:
            snr: Device serial number.
            scale: Stage scale identifier (e.g., "MLS203").
            is_rack_system: Whether the device is a rack-mounted system.
        """
        self.__logger = initLogger(self)
        from pylablib.devices.Thorlabs import KinesisMotor as _KinesisMotor
        self._stage = _KinesisMotor(snr, scale=scale, is_rack_system=is_rack_system)
        self.__logger.info(f'Initialized Thorlabs Kinesis stage {snr} (scale={scale})')

    def get_position(self, channel: int) -> float:
        """Return current position in mm for the specified channel.
        
        Args:
            channel: 1 for X, 2 for Y.
        """
        return self._stage.get_position(channel=channel)

    def move_to(self, position: float, channel: int) -> None:
        """Move to an absolute position in mm.
        
        Args:
            position: Target position in mm.
            channel: 1 for X, 2 for Y.
        """
        self._stage.move_to(position, channel=channel)

    def move_by(self, delta: float, channel: int) -> None:
        """Move by a relative displacement in mm.
        
        Args:
            delta: Relative displacement in mm.
            channel: 1 for X, 2 for Y.
        """
        self._stage.move_by(delta, channel=channel)

    def jog(self, direction: str, channel: int, kind: str = "continuous") -> None:
        """Start jogging in the specified direction.
        
        Args:
            direction: '+' for positive, '-' for negative.
            channel: 1 for X, 2 for Y.
            kind: 'continuous' for continuous jogging.
        """
        self._stage.jog(direction, channel, kind=kind)

    def stop(self, channel: int) -> None:
        """Stop motion on the specified channel.
        
        Args:
            channel: 1 for X, 2 for Y.
        """
        self._stage.stop(channel=channel)

    def home(self, channel: int, sync: bool = False, force: bool = True) -> None:
        """Home the specified channel.
        
        Args:
            channel: 1 for X, 2 for Y.
            sync: Whether to wait for homing to complete.
            force: Whether to force homing even if already homed.
        """
        self._stage.home(sync=sync, force=force, channel=channel)

    def close(self) -> None:
        """Disconnect from the device."""
        self._stage.close()


class MockKinesisStage:
    """Simulated Kinesis XY stage for headless operation."""

    def __init__(self, snr: str, scale: str = "MLS203", is_rack_system: bool = True):
        """
        Args:
            snr: Device serial number (for logging).
            scale: Stage scale identifier (ignored in mock).
            is_rack_system: Whether the device is a rack-mounted system (ignored in mock).
        """
        self.__logger = initLogger(self)
        self._snr = snr
        self._position = {1: 0.0, 2: 0.0}  # channel -> position in mm
        self._jogging = {1: False, 2: False}  # channel -> jogging state
        self.__logger.info(f'Initialized mock Kinesis stage {snr} (scale={scale})')

    def get_position(self, channel: int) -> float:
        """Return current position in mm for the specified channel.
        
        Args:
            channel: 1 for X, 2 for Y.
        """
        return self._position.get(channel, 0.0)

    def move_to(self, position: float, channel: int) -> None:
        """Move to an absolute position in mm (instant).
        
        Args:
            position: Target position in mm.
            channel: 1 for X, 2 for Y.
        """
        self._position[channel] = position
        self.__logger.debug(f'Mock stage {self._snr} ch{channel}: moved to {position:.3f} mm')

    def move_by(self, delta: float, channel: int) -> None:
        """Move by a relative displacement in mm (instant).
        
        Args:
            delta: Relative displacement in mm.
            channel: 1 for X, 2 for Y.
        """
        self._position[channel] += delta
        self.__logger.debug(
            f'Mock stage {self._snr} ch{channel}: moved by {delta:.3f} mm '
            f'to {self._position[channel]:.3f} mm'
        )

    def jog(self, direction: str, channel: int, kind: str = "continuous") -> None:
        """Start jogging in the specified direction (simulated).
        
        Args:
            direction: '+' for positive, '-' for negative.
            channel: 1 for X, 2 for Y.
            kind: 'continuous' for continuous jogging.
        """
        self._jogging[channel] = True
        self.__logger.debug(
            f'Mock stage {self._snr} ch{channel}: started jogging {direction} ({kind})'
        )

    def stop(self, channel: int) -> None:
        """Stop motion on the specified channel.
        
        Args:
            channel: 1 for X, 2 for Y.
        """
        self._jogging[channel] = False
        self.__logger.debug(f'Mock stage {self._snr} ch{channel}: stopped')

    def home(self, channel: int, sync: bool = False, force: bool = True) -> None:
        """Home the specified channel (instant).
        
        Args:
            channel: 1 for X, 2 for Y.
            sync: Whether to wait for homing to complete (ignored).
            force: Whether to force homing (ignored).
        """
        self._position[channel] = 0.0
        self.__logger.debug(f'Mock stage {self._snr} ch{channel}: homed to 0.0 mm')

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
