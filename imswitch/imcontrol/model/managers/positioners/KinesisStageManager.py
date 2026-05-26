"""Manager for Thorlabs MLS203 Kinesis XY motorized stages."""

from imswitch.imcommon.model import initLogger
from .PositionerManager import PositionerManager


class KinesisStageManager(PositionerManager):
    """PositionerManager for Thorlabs MLS203 two-axis motorized stages.
    
    Supports both real hardware (via pylablib) and headless mock operation.
    Implements continuous jogging for interactive positioning.
    
    Manager properties:
        - snr (str, required): Device serial number.
        - scale (str, default "MLS203"): Stage scale identifier.
        - isRackSystem (bool, default True): Whether the device is rack-mounted.
        - homeOnInit (bool, default False): If True, home both axes on init.
        - unitsPerUm (float, default 1.0): Conversion factor between the raw
          values reported/accepted by pylablib and physical micrometres.
          Set this when pylablib doesn't recognize the controller model and
          falls back to internal encoder counts (warns at startup:
          "can't recognize motor model ..., setting all scales to internal
          units"). All positions exposed to the rest of ImSwitch — `move`,
          `setPosition`, `position` — are in µm; raw values are
          (µm * unitsPerUm).
    """

    def __init__(self, positionerInfo, name: str, **lowLevelManagers):
        super().__init__(positionerInfo, name, initialPosition={
            axis: 0.0 for axis in positionerInfo.axes
        })
        self.__logger = initLogger(self, instanceName=name)

        if positionerInfo is None:
            return

        self._snr = positionerInfo.managerProperties['snr']
        self._scale = positionerInfo.managerProperties.get('scale', 'MLS203')
        self._is_rack_system = positionerInfo.managerProperties.get('isRackSystem', True)
        home_on_init = positionerInfo.managerProperties.get('homeOnInit', False)
        self._units_per_um = float(positionerInfo.managerProperties.get('unitsPerUm', 1.0))

        self._stage = self._getStageObj(self._snr, self._scale, self._is_rack_system)

        if home_on_init:
            self.__logger.info(f'Homing Kinesis stage {self._snr}')
            for axis in self.axes:
                channel = self._axis_to_channel(axis)
                self._stage.home(channel=channel)

        self._update_position()

    def move(self, dist: float, axis: str) -> None:
        """Move by a relative displacement in µm.

        Args:
            dist: Relative displacement in µm.
            axis: The axis to move ('X' or 'Y').
        """
        channel = self._axis_to_channel(axis)
        self._stage.move_by(dist * self._units_per_um, channel=channel)
        self._update_position()

    def setPosition(self, position: float, axis: str) -> None:
        """Move to an absolute position in µm.

        Args:
            position: Target position in µm.
            axis: The axis to move ('X' or 'Y').
        """
        channel = self._axis_to_channel(axis)
        self._stage.move_to(position * self._units_per_um, channel=channel)
        self._update_position()

    def jog_start(self, axis: str, sign: int) -> None:
        """Start continuous jogging on the specified axis.
        
        Args:
            axis: The axis to jog ('X' or 'Y').
            sign: Direction of motion: +1 for positive, -1 for negative.
        """
        channel = self._axis_to_channel(axis)
        direction = '+' if sign > 0 else '-'
        self._stage.jog(direction, channel=channel, kind='continuous')
        self.__logger.debug(f'Started jogging {axis} in direction {direction}')

    def jog_stop(self, axis: str) -> None:
        """Stop continuous jogging on the specified axis.
        
        Args:
            axis: The axis to stop jogging on.
        """
        channel = self._axis_to_channel(axis)
        self._stage.stop(channel=channel)
        self._update_position()
        self.__logger.debug(f'Stopped jogging {axis}')

    def _axis_to_channel(self, axis: str) -> int:
        """Map axis name to hardware channel number.
        
        Args:
            axis: Axis name ('X' or 'Y').
        
        Returns:
            Channel number (1 for X, 2 for Y).
        
        Raises:
            ValueError: If axis is not 'X' or 'Y'.
        """
        if axis == 'X':
            return 1
        elif axis == 'Y':
            return 2
        else:
            raise ValueError(f'Unknown axis: {axis}. Must be X or Y.')

    def _update_position(self) -> None:
        """Read current positions from hardware and update internal state (µm)."""
        for axis in self.axes:
            channel = self._axis_to_channel(axis)
            raw = self._stage.get_position(channel=channel)
            self._position[axis] = raw / self._units_per_um

    def _getStageObj(self, snr: str, scale: str, is_rack_system: bool):
        """Instantiate the stage driver with real-then-mock fallback."""
        try:
            from imswitch.imcontrol.model.interfaces.kinesisstage import KinesisStage
            stage = KinesisStage(snr, scale=scale, is_rack_system=is_rack_system)
            self.__logger.info(f'Initialized Thorlabs Kinesis stage {snr}')
        except Exception as e:
            self.__logger.warning(
                f'Failed to initialize Kinesis stage {snr} (real hardware): {e}'
            )
            self.__logger.warning('Loading mock Kinesis stage for headless operation')
            from imswitch.imcontrol.model.interfaces.kinesisstage import MockKinesisStage
            stage = MockKinesisStage(snr, scale=scale, is_rack_system=is_rack_system)
        return stage

    def finalize(self) -> None:
        """Close the stage connection."""
        self._stage.close()


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
