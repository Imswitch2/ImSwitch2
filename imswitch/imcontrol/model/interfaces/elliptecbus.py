"""Thorlabs Elliptec shared-bus runtime resource for ELL14/ELL14K mounts.

The public manager identity is kept stable while this resource can move between
real pylablib hardware and per-address virtual/mock state.  One resource exists
per COM port, so several logical rotators can safely share a multidrop bus.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from weakref import WeakSet

from imswitch.imcommon.model import initLogger


def _open_elliptec_stage(port: str, scale: str | float):
    """Open and initialize one pylablib Elliptec multidrop connection."""
    from pylablib.devices import Thorlabs

    stage = Thorlabs.ElliptecMotor(port, scale=scale)
    try:
        time.sleep(0.2)
        stage.update_connected_addrs()
    except Exception:
        try:
            stage.close()
        except Exception:
            pass
        raise
    return stage


def isElliptecCommunicationError(exc: BaseException) -> bool:
    """Return whether an exception looks like a broken transport/backend.

    Elliptec can NAK individual commands during normal multidrop contention.
    Those are handled by the manager's retry loop and must not automatically
    destroy a healthy bus.  Runtime mock fallback is intentionally limited to
    errors that indicate the serial/backend connection itself is unusable.
    """
    if isinstance(exc, (OSError, IOError, TimeoutError, ConnectionError)):
        return True

    type_text = f"{type(exc).__module__}.{type(exc).__name__}".casefold()
    if any(token in type_text for token in (
        "serialexception",
        "backenderror",
        "connectionerror",
        "timeouterror",
    )):
        return True

    message = str(exc).casefold()
    return any(token in message for token in (
        "serial port is closed",
        "port is closed",
        "closed port",
        "port not open",
        "not connected",
        "disconnected",
        "connection lost",
        "connection failed",
        "serial connection",
        "communication error",
        "backend error",
        "read timeout",
        "write timeout",
        "i/o error",
        "io error",
    ))


@dataclass(frozen=True)
class ElliptecBusReconnectResult:
    """Outcome of one shared-bus reconnect attempt."""

    success_by_address: dict[int, bool]
    errors_by_address: dict[int, str]
    connection_error: str | None = None

    def succeeded(self, address: int) -> bool:
        return bool(self.success_by_address.get(address, False))

    def error_for(self, address: int) -> str | None:
        return self.errors_by_address.get(address) or self.connection_error


class _SharedElliptecBus:
    """Stable real/mock runtime resource shared by one Elliptec COM bus.

    The resource object itself is never replaced while managers are alive.
    ``_stage`` is the replaceable real pylablib connection; when it is absent,
    or when a configured address is unavailable, calls operate on cached
    virtual positions instead.  This lets manager/controller identities remain
    stable through startup fallback, runtime communication loss and reconnect.
    """

    _instances: dict[str, "_SharedElliptecBus"] = {}
    _instances_lock = threading.Lock()

    @classmethod
    def get_bus(cls, port: str, scale: str | float = "stage"):
        with cls._instances_lock:
            existing = cls._instances.get(port)
            if existing is not None:
                if existing.scale != scale:
                    raise ValueError(
                        f"Elliptec bus {port!r} is already configured with "
                        f"scale={existing.scale!r}; cannot also use scale={scale!r}."
                    )
                return existing

            bus = cls(port, scale)
            cls._instances[port] = bus
            return bus

    def __init__(self, port: str, scale: str | float):
        self.__logger = initLogger(self)
        self.port = str(port)
        self.scale = scale
        self.lock = threading.RLock()
        self._transition_lock = threading.Lock()
        self.refcount = 0

        self._stage = None
        self._retired_stages = []
        self._positions: dict[int, float] = {}
        self._real_addresses: set[int] = set()
        self._address_errors: dict[int, BaseException | str] = {}
        self._connection_error: BaseException | str | None = None
        self._managers_by_address: dict[int, WeakSet] = {}

        try:
            self._stage = _open_elliptec_stage(self.port, self.scale)
        except Exception as exc:
            self._connection_error = exc
            self.__logger.warning(
                f"Failed to open Elliptec bus {self.port}; mock fallback active: {exc}"
            )
        else:
            self.__logger.info(f"Opened shared Elliptec bus on {self.port}")

    # ---- registration / cached state ---------------------------------

    def acquire(self, manager=None, address: int | None = None) -> None:
        with self.lock:
            self.refcount += 1
            if manager is not None and address is not None:
                address = int(address)
                self._managers_by_address.setdefault(address, WeakSet()).add(manager)
                self._positions.setdefault(address, 0.0)
            self.__logger.debug(
                f"Acquired shared bus {self.port} (refcount={self.refcount})"
            )

    def release(self, manager=None, address: int | None = None) -> None:
        stages_to_close = []
        remove_instance = False
        with self.lock:
            if manager is not None and address is not None:
                managers = self._managers_by_address.get(int(address))
                if managers is not None:
                    managers.discard(manager)
                    if not managers:
                        self._managers_by_address.pop(int(address), None)

            self.refcount -= 1
            self.__logger.debug(
                f"Released shared bus {self.port} (refcount={self.refcount})"
            )
            if self.refcount <= 0:
                self.refcount = 0
                if self._stage is not None:
                    stages_to_close.append(self._stage)
                    self._stage = None
                stages_to_close.extend(self._retired_stages)
                self._retired_stages = []
                self._real_addresses.clear()
                remove_instance = True

        for stage in stages_to_close:
            self._close_stage_best_effort(stage)

        if remove_instance:
            with self.__class__._instances_lock:
                if self.__class__._instances.get(self.port) is self:
                    self.__class__._instances.pop(self.port, None)
            self.__logger.info(f"Closed shared Elliptec bus on {self.port}")

    def registered_managers(self):
        with self.lock:
            managers = {
                manager
                for group in self._managers_by_address.values()
                for manager in tuple(group)
            }
        return tuple(sorted(managers, key=lambda manager: manager.name.casefold()))

    def registered_addresses(self) -> tuple[int, ...]:
        with self.lock:
            return tuple(sorted(self._managers_by_address))

    def cached_position(self, address: int) -> float:
        with self.lock:
            return float(self._positions.get(int(address), 0.0))

    def is_real(self, address: int) -> bool:
        with self.lock:
            return self._stage is not None and int(address) in self._real_addresses

    def error_for(self, address: int) -> BaseException | str | None:
        with self.lock:
            address = int(address)
            if address in self._address_errors:
                return self._address_errors[address]
            if self._stage is None:
                return self._connection_error
            if address not in self._real_addresses:
                return f"Elliptec address {address} is not responding on {self.port}"
            return None

    # ---- startup/address probing -------------------------------------

    def initialize_address(self, address: int) -> float:
        """Probe one configured address without making startup fail.

        A bus can be real while one multidrop address is absent.  Successful
        addresses therefore remain real independently; an unavailable address
        falls back to its virtual position and retains the probe diagnostic.
        """
        address = int(address)
        with self.lock:
            self._positions.setdefault(address, 0.0)
            stage = self._stage
            if stage is None:
                if self._connection_error is not None:
                    self._address_errors[address] = self._connection_error
                return self._positions[address]

            try:
                position = float(stage.get_position(addr=address))
            except Exception as exc:
                self._real_addresses.discard(address)
                self._address_errors[address] = exc
                self.__logger.warning(
                    f"Elliptec address {address} did not respond on {self.port}; "
                    f"mock fallback active: {exc}"
                )
                return self._positions[address]

            self._positions[address] = position
            self._real_addresses.add(address)
            self._address_errors.pop(address, None)
            return position

    # ---- normal bus operations --------------------------------------

    def move_to(self, address: int, position: float) -> bool:
        """Move one address; return True when physical hardware was used."""
        address = int(address)
        position = float(position)
        with self.lock:
            if self._stage is None or address not in self._real_addresses:
                self._positions[address] = position
                return False

            self._stage.move_to(position, addr=address)
            self._positions[address] = position
            return True

    def get_position(self, address: int) -> float:
        address = int(address)
        with self.lock:
            if self._stage is None or address not in self._real_addresses:
                return float(self._positions.get(address, 0.0))

            position = float(self._stage.get_position(addr=address))
            self._positions[address] = position
            return position

    def home(self, address: int) -> bool:
        """Home one address; return True when physical hardware was used."""
        address = int(address)
        with self.lock:
            if self._stage is None or address not in self._real_addresses:
                self._positions[address] = 0.0
                return False

            self._stage.home(addr=address)
            position = float(self._stage.get_position(addr=address))
            self._positions[address] = position
            return True

    # ---- runtime failure / reconnect --------------------------------

    def fallback_to_mock(self, error: BaseException | str) -> None:
        """Atomically retire a broken real bus and expose virtual state.

        Vendor teardown is deliberately deferred to the reconnect worker (or
        terminal release), so the UI thread that detected a communication loss
        is never made to wait on pylablib/serial close operations.
        """
        with self.lock:
            old_stage = self._stage
            self._stage = None
            if old_stage is not None:
                self._retired_stages.append(old_stage)
            addresses = tuple(self._managers_by_address)
            self._real_addresses.clear()
            self._connection_error = error
            for address in addresses:
                self._address_errors[address] = error
        self._notify_managers()

    def reconnect(self) -> ElliptecBusReconnectResult:
        """Reconnect the shared COM bus once, then reprobe every address."""
        with self._transition_lock:
            addresses = self.registered_addresses()

            with self.lock:
                stages_to_close = list(self._retired_stages)
                self._retired_stages = []
                if self._stage is not None:
                    stages_to_close.append(self._stage)
                self._stage = None
                self._real_addresses.clear()
                self._connection_error = "Elliptec reconnect in progress"
                for address in addresses:
                    self._address_errors[address] = self._connection_error

            self._notify_managers()
            for stage in stages_to_close:
                self._close_stage_best_effort(stage)

            try:
                candidate = _open_elliptec_stage(self.port, self.scale)
            except Exception as exc:
                with self.lock:
                    self._connection_error = exc
                    for address in addresses:
                        self._address_errors[address] = exc
                self._notify_managers()
                return ElliptecBusReconnectResult(
                    success_by_address={address: False for address in addresses},
                    errors_by_address={address: str(exc) for address in addresses},
                    connection_error=str(exc),
                )

            positions = {}
            errors = {}
            for address in addresses:
                try:
                    positions[address] = float(candidate.get_position(addr=address))
                except Exception as exc:
                    errors[address] = exc

            with self.lock:
                self._stage = candidate
                self._connection_error = None
                self._real_addresses = set(positions)
                for address, position in positions.items():
                    self._positions[address] = position
                    self._address_errors.pop(address, None)
                for address, error in errors.items():
                    self._address_errors[address] = error

            self._notify_managers()
            return ElliptecBusReconnectResult(
                success_by_address={
                    address: address in positions for address in addresses
                },
                errors_by_address={
                    address: str(error) for address, error in errors.items()
                },
            )

    def _notify_managers(self) -> None:
        for manager in self.registered_managers():
            callback = getattr(manager, "_onElliptecBusStateChanged", None)
            if callable(callback):
                try:
                    callback()
                except Exception:
                    self.__logger.warning(
                        "Failed to propagate Elliptec bus state to manager",
                        exc_info=True,
                    )

    @staticmethod
    def _close_stage_best_effort(stage) -> None:
        if stage is None:
            return
        try:
            stage.close()
        except Exception:
            pass


# Compatibility mock classes retained for external/plugin imports.  New
# ElliptecRotatorManager instances use _SharedElliptecBus directly so that the
# resource identity can survive mock <-> real transitions.
class MockElliptecBus:
    """Standalone simulated multidrop bus retained for compatibility."""

    _instances = {}
    _instances_lock = threading.Lock()

    @classmethod
    def get_bus(cls, port: str, scale: str | float = "stage"):
        with cls._instances_lock:
            if port not in cls._instances:
                cls._instances[port] = cls(port, scale)
            return cls._instances[port]

    def __init__(self, port: str, scale: str | float):
        self.__logger = initLogger(self)
        self.port = port
        self.scale = scale
        self._positions = {}
        self.refcount = 0
        self.lock = threading.RLock()
        self.stage = MockElliptecMotor(self)

    def acquire(self):
        with self.lock:
            self.refcount += 1

    def release(self):
        with self.lock:
            self.refcount -= 1
            if self.refcount <= 0:
                with self.__class__._instances_lock:
                    self.__class__._instances.pop(self.port, None)


class MockElliptecMotor:
    """Mock motor interface compatible with pylablib ElliptecMotor calls."""

    def __init__(self, bus: MockElliptecBus):
        self._bus = bus

    def move_to(self, position: float, addr: int | None = None) -> None:
        with self._bus.lock:
            self._bus._positions[addr] = float(position)

    def get_position(self, addr: int | None = None) -> float:
        with self._bus.lock:
            return float(self._bus._positions.get(addr, 0.0))

    def home(self, addr: int | None = None) -> None:
        with self._bus.lock:
            self._bus._positions[addr] = 0.0

    def update_connected_addrs(self) -> None:
        pass

    def close(self) -> None:
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
