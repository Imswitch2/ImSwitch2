"""Thorlabs Elliptec shared bus driver for ELL14/ELL14K rotation mounts.

Provides a refcounted singleton bus manager for multidrop Elliptec devices
that share a single COM port. Also includes mock implementations for headless
operation.
"""

import threading
import time
from imswitch.imcommon.model import initLogger


class _SharedElliptecBus:
    """Internal shared COM-port manager.

    Ensures:
    - only ONE ElliptecMotor instance exists per COM port
    - multiple logical controllers can share the bus safely
    """

    _instances = {}
    _instances_lock = threading.Lock()

    @classmethod
    def get_bus(cls, port: str, scale: str | float = "stage"):
        """Get or create the shared bus instance for this port.
        
        Args:
            port: Serial port name (e.g. "COM20" or "/dev/ttyUSB0").
            scale: Pylablib scale parameter (default "stage").
        
        Returns:
            _SharedElliptecBus instance for this port.
        """
        with cls._instances_lock:
            if port not in cls._instances:
                cls._instances[port] = cls(port, scale)
            return cls._instances[port]

    def __init__(self, port: str, scale: str | float):
        """Initialize the shared bus (called only once per port).
        
        Args:
            port: Serial port name.
            scale: Pylablib scale parameter.
        """
        self.__logger = initLogger(self)
        self.port = port
        self.scale = scale

        from pylablib.devices import Thorlabs
        self.stage = Thorlabs.ElliptecMotor(port, scale=scale)

        time.sleep(0.2)

        # IMPORTANT for multidrop buses
        self.stage.update_connected_addrs()

        self.refcount = 0
        self.lock = threading.RLock()
        self.__logger.info(f"Opened shared Elliptec bus on {port}")

    def acquire(self):
        """Increment refcount; called when a manager connects."""
        with self.lock:
            self.refcount += 1
            self.__logger.debug(
                f"Acquired shared bus {self.port} (refcount={self.refcount})"
            )

    def release(self):
        """Decrement refcount; closes bus when count reaches zero."""
        with self.lock:
            self.refcount -= 1

            self.__logger.debug(
                f"Released shared bus {self.port} (refcount={self.refcount})"
            )

            if self.refcount <= 0:
                self.__logger.info(f"Closing shared Elliptec bus on {self.port}")

                try:
                    self.stage.close()
                finally:
                    with self.__class__._instances_lock:
                        self.__class__._instances.pop(self.port, None)


class MockElliptecBus:
    """Simulated Elliptec multidrop bus for headless operation.
    
    Maintains per-address position state and simulates synchronous moves.
    """

    _instances = {}
    _instances_lock = threading.Lock()

    @classmethod
    def get_bus(cls, port: str, scale: str | float = "stage"):
        """Get or create the mock bus instance for this port.
        
        Args:
            port: Serial port name (for logging).
            scale: Scale parameter (stored but not used in mock).
        
        Returns:
            MockElliptecBus instance for this port.
        """
        with cls._instances_lock:
            if port not in cls._instances:
                cls._instances[port] = cls(port, scale)
            return cls._instances[port]

    def __init__(self, port: str, scale: str | float):
        """Initialize the mock bus.
        
        Args:
            port: Serial port name.
            scale: Scale parameter (stored but not used).
        """
        self.__logger = initLogger(self)
        self.port = port
        self.scale = scale
        self._positions = {}
        self.refcount = 0
        self.lock = threading.RLock()
        self.__logger.info(f"Opened mock Elliptec bus on {port}")

    def acquire(self):
        """Increment refcount."""
        with self.lock:
            self.refcount += 1
            self.__logger.debug(
                f"Acquired mock bus {self.port} (refcount={self.refcount})"
            )

    def release(self):
        """Decrement refcount; cleans up when count reaches zero."""
        with self.lock:
            self.refcount -= 1

            self.__logger.debug(
                f"Released mock bus {self.port} (refcount={self.refcount})"
            )

            if self.refcount <= 0:
                self.__logger.info(f"Closing mock Elliptec bus on {self.port}")
                with self.__class__._instances_lock:
                    self.__class__._instances.pop(self.port, None)


class MockElliptecMotor:
    """Mock motor interface compatible with pylablib ElliptecMotor API.
    
    Used by MockElliptecBus to provide per-address motor operations.
    """

    def __init__(self, bus: MockElliptecBus):
        """Initialize the mock motor.
        
        Args:
            bus: Parent MockElliptecBus instance.
        """
        self.__logger = initLogger(self)
        self._bus = bus

    def move_to(self, position: float, addr: int | None = None) -> None:
        """Move to an absolute position in degrees (instant).
        
        Args:
            position: Target position in degrees.
            addr: Device address on the bus.
        """
        with self._bus.lock:
            self._bus._positions[addr] = position
            self.__logger.debug(
                f"Mock Elliptec port={self._bus.port} addr={addr}: moved to {position}°"
            )

    def get_position(self, addr: int | None = None) -> float:
        """Get current position in degrees.
        
        Args:
            addr: Device address on the bus.
        
        Returns:
            Current position in degrees.
        """
        with self._bus.lock:
            return self._bus._positions.get(addr, 0.0)

    def home(self, addr: int | None = None) -> None:
        """Home the device to zero position.
        
        Args:
            addr: Device address on the bus.
        """
        with self._bus.lock:
            self._bus._positions[addr] = 0.0
            self.__logger.debug(
                f"Mock Elliptec port={self._bus.port} addr={addr}: homed to 0°"
            )

    def update_connected_addrs(self) -> None:
        """No-op; mock bus doesn't need address discovery."""
        pass

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
