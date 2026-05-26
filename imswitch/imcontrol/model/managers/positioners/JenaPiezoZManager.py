import time
from imswitch.imcommon.model import initLogger
from .PositionerManager import PositionerManager


class JenaPiezoZManager(PositionerManager):
    """PositionerManager for control of a Jena piezo Z-stage through RS232
    communication.

    Manager properties:

    - ``rs232device`` -- name of the defined rs232 communication channel
      through which the communication should take place
    - ``posRangeUm`` -- allowed position range in micrometres [min, max]
      (default: [0, 100])
    - ``waitForSettle`` -- whether to poll the position until the stage has
      settled after a move command (default: True)
    - ``settleToleranceUm`` -- maximum deviation from target position in µm
      to consider the stage settled (default: 0.1)
    - ``settleTimeoutS`` -- maximum time in seconds to wait for settling
      before raising TimeoutError (default: 1.0)
    """

    def __init__(self, positionerInfo, name, *args, **lowLevelManagers):
        if len(positionerInfo.axes) != 1:
            raise RuntimeError(f'{self.__class__.__name__} only supports one axis,'
                               f' {len(positionerInfo.axes)} provided.')

        super().__init__(positionerInfo, name, initialPosition={
            axis: 0 for axis in positionerInfo.axes
        })
        self.__logger = initLogger(self, instanceName=name)
        self._rs232Manager = lowLevelManagers['rs232sManager'][
            positionerInfo.managerProperties['rs232device']
        ]

        self._posRangeUm = positionerInfo.managerProperties.get('posRangeUm', [0, 100])
        self._waitForSettle = positionerInfo.managerProperties.get('waitForSettle', True)
        self._settleToleranceUm = positionerInfo.managerProperties.get('settleToleranceUm', 0.1)
        self._settleTimeoutS = positionerInfo.managerProperties.get('settleTimeoutS', 1.0)

        self._ext_active = False

        time.sleep(0.2)
        self._send_command('cl')

        try:
            current_pos = self._read_position_um()
            self._position[self.axes[0]] = current_pos
            self.__logger.info(f"Jena piezo initialized at {current_pos:.2f} µm")
        except Exception as e:
            self.__logger.warning(f"Could not read initial position: {e}")
            self._position[self.axes[0]] = self._posRangeUm[0]

    def move(self, dist, axis):
        """Move the positioner by the specified distance."""
        current = self._position[self.axes[0]]
        target = current + dist
        self.setPosition(target, axis)

    def setPosition(self, value, axis):
        """Set the positioner to the specified absolute position."""
        self.__logger.debug(f"Set position to: {value} µm")
        
        if not (self._posRangeUm[0] <= value <= self._posRangeUm[1]):
            self.__logger.error(f"Position {value} µm out of range {self._posRangeUm}")
            return
        
        if not self._ext_active:
            self.activate_ext_control()
        
        value = round(value, 2)
        self._send_command(f'wr, {value}')
        
        if self._waitForSettle:
            self._wait_for_settle(value)
        
        self._position[self.axes[0]] = value

    def _wait_for_settle(self, target_pos):
        """Poll position until settled or timeout, with one retry."""
        start_time = time.time()
        retry_time = start_time + self._settleTimeoutS / 2
        retried = False
        
        time.sleep(0.1)
        
        while True:
            elapsed = time.time() - start_time
            
            if elapsed >= self._settleTimeoutS:
                actual = self._read_position_um()
                raise TimeoutError(
                    f"Jena piezo settling timeout: target={target_pos:.2f} µm, "
                    f"actual={actual:.2f} µm after {self._settleTimeoutS}s"
                )
            
            try:
                actual = self._read_position_um()
                deviation = abs(actual - target_pos)
                
                if deviation <= self._settleToleranceUm:
                    self.__logger.debug(f"Settled at {actual:.2f} µm (target {target_pos:.2f} µm)")
                    return
                
                if not retried and time.time() >= retry_time:
                    self.__logger.debug(f"Retrying write command at {elapsed:.2f}s")
                    self._send_command(f'wr, {target_pos}')
                    retried = True
                
            except Exception as e:
                self.__logger.warning(f"Error reading position during settle: {e}")
            
            time.sleep(0.1)

    def _read_position_um(self):
        """Read the current position in micrometres."""
        reply = self._send_command('rd')
        parts = reply.split(',')
        if len(parts) >= 2:
            return float(parts[1].strip())
        else:
            raise ValueError(f"Unexpected position reply format: {reply}")

    def _send_command(self, cmd):
        """Send a command and return the response.

        The RS232 layer appends the configured ``send_termination`` itself,
        so callers pass the bare command. For a Jena controller this means
        ``send_termination`` must be ``"\\r"`` in the rs232 config.
        """
        return self._rs232Manager.query(cmd)

    def activate_ext_control(self):
        """Enter external control mode (enables serial commands)."""
        self._send_command('i1')
        self._ext_active = True
        self.__logger.debug("External control mode activated")

    def deactivate_ext_control(self):
        """Exit external control mode (returns control to front panel)."""
        self._send_command('i0')
        self._ext_active = False
        self.__logger.debug("External control mode deactivated")

    @property
    def position(self):
        """Return the current position of all axes."""
        try:
            actual = self._read_position_um()
            self._position[self.axes[0]] = actual
        except Exception as e:
            self.__logger.warning(f"Could not read position: {e}")
        return self._position

    def finalize(self):
        """Deactivate external control mode before closing."""
        try:
            if self._ext_active:
                self.deactivate_ext_control()
        except Exception as e:
            self.__logger.error(f"Error during finalize: {e}")


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
