"""Manager for Thorlabs ELL14/ELL14K Elliptec rotation mounts."""

from imswitch.imcommon.model import initLogger
from .RotatorManager import RotatorManager


class ElliptecRotatorManager(RotatorManager):
    """RotatorManager for Thorlabs ELL14/ELL14K Elliptec rotation mounts.
    
    Multiple rotators can share one COM bus distinguished by address (multidrop).
    Uses a refcounted singleton bus manager to ensure safe bus sharing.
    
    Supports both real hardware (via pylablib) and headless mock operation.
    
    Manager properties:
        - port (str, required): Serial port name (e.g. "COM20" or "/dev/ttyUSB0").
        - address (int, required): Elliptec bus address for this device.
        - scale (str or float, default "stage"): Pylablib scale parameter.
        - homeOnInit (bool, default False): If True, home the motor on init.
    
    Example setup JSON (two rotators on same port):
        "rotators": {
            "HWP": {
                "managerName": "ElliptecRotatorManager",
                "managerProperties": {
                    "port": "COM20",
                    "address": 0,
                    "homeOnInit": false
                }
            },
            "QWP": {
                "managerName": "ElliptecRotatorManager",
                "managerProperties": {
                    "port": "COM20",
                    "address": 1,
                    "homeOnInit": false
                }
            }
        }
    """

    def __init__(self, rotatorInfo, name: str, *args, **kwargs):
        super().__init__(rotatorInfo, name, *args, **kwargs)
        self.__logger = initLogger(self)

        if rotatorInfo is None:
            return

        self._port = rotatorInfo.managerProperties['port']
        self._addr = rotatorInfo.managerProperties['address']
        self._scale = rotatorInfo.managerProperties.get('scale', 'stage')
        home_on_init = rotatorInfo.managerProperties.get('homeOnInit', False)

        self._bus = self._getBusObj(self._port, self._scale)
        self._bus.acquire()

        if home_on_init:
            self.__logger.info(f'Homing Elliptec rotator {name} (port={self._port} addr={self._addr})')
            with self._bus.lock:
                self._bus.stage.home(addr=self._addr)

        self._update_position()

    def move_abs(self, pos_deg: float) -> None:
        """Move to an absolute position in degrees.
        
        Elliptec moves are synchronous; no wait needed. Retries up to 5 times
        on failure (Elliptec NAKs on bus contention).
        
        Args:
            pos_deg: Target position in degrees.
        """
        self._move_with_retry(pos_deg, is_relative=False)
        self._update_position()

    def move_rel(self, d_deg: float) -> None:
        """Move by a relative displacement in degrees.
        
        Elliptec moves are synchronous; no wait needed. Retries up to 5 times
        on failure (Elliptec NAKs on bus contention).
        
        Args:
            d_deg: Relative displacement in degrees.
        """
        target = self._position + d_deg
        self._move_with_retry(target, is_relative=False)
        self._update_position()

    def _move_with_retry(self, target_deg: float, is_relative: bool, fails: int = 0) -> None:
        """Move to target position with retry logic for bus contention.
        
        Args:
            target_deg: Target position in degrees.
            is_relative: Unused (kept for API compatibility).
            fails: Retry counter.
        """
        with self._bus.lock:
            try:
                self._bus.stage.move_to(target_deg, addr=self._addr)
            except Exception as e:
                self.__logger.warning(
                    f"Elliptec move failed (port={self._port} addr={self._addr}, "
                    f"attempt {fails+1}/5): {e}"
                )
                if fails < 5:
                    self._move_with_retry(target_deg, is_relative, fails + 1)
                else:
                    raise

    def _update_position(self) -> None:
        """Read current position from the device."""
        with self._bus.lock:
            self._position = self._bus.stage.get_position(addr=self._addr)
            self.__logger.debug(
                f"Elliptec position (port={self._port} addr={self._addr}): {self._position}°"
            )

    def _getBusObj(self, port: str, scale: str | float):
        """Get the shared bus instance with real-then-mock fallback.
        
        Args:
            port: Serial port name.
            scale: Pylablib scale parameter.
        
        Returns:
            Shared bus instance (_SharedElliptecBus or MockElliptecBus).
        """
        try:
            from imswitch.imcontrol.model.interfaces.elliptecbus import _SharedElliptecBus
            bus = _SharedElliptecBus.get_bus(port, scale)
            self.__logger.info(f'Initialized Elliptec rotator (port={port} addr={self._addr})')
            return bus
        except Exception as e:
            self.__logger.warning(
                f'Failed to initialize Elliptec rotator (port={port} addr={self._addr}, '
                f'real hardware): {e}'
            )
            self.__logger.warning('Loading mock Elliptec bus for headless operation')
            from imswitch.imcontrol.model.interfaces.elliptecbus import MockElliptecBus, MockElliptecMotor
            mock_bus = MockElliptecBus.get_bus(port, scale)
            # Replace stage with mock motor
            mock_bus.stage = MockElliptecMotor(mock_bus)
            return mock_bus

    def finalize(self) -> None:
        """Release the shared bus (closes when refcount reaches zero)."""
        if self._bus is not None:
            self._bus.release()
            self.__logger.info(
                f"Released Elliptec rotator (port={self._port} addr={self._addr})"
            )


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
