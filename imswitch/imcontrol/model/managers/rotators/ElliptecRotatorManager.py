"""Manager for Thorlabs ELL14/ELL14K Elliptec rotation mounts."""

from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.devices import (
    DeviceId,
    DeviceLifecycleAction,
    DeviceLifecycleCapabilities,
    DeviceLifecycleNotSupportedError,
    DeviceLifecycleResult,
    DeviceManagerStatusMixin,
    HardwareDeviceId,
)
from imswitch.imcontrol.model.interfaces.elliptecbus import (
    _SharedElliptecBus,
    isElliptecCommunicationError,
)
from .RotatorManager import RotatorManager


class _ElliptecRotatorLifecycle:
    """Lifecycle for one Elliptec mount backed by a shared COM resource."""

    capabilities = DeviceLifecycleCapabilities(reconnect=True)

    def __init__(self, manager):
        self._manager = manager
        self._hardware_id = HardwareDeviceId(
            category="rotator", key=f"rotator:{manager.name}"
        )

    @property
    def hardware_id(self):
        return self._hardware_id

    def _unsupported(self, action):
        raise DeviceLifecycleNotSupportedError(
            f"Elliptec lifecycle does not yet support {action.value}."
        )

    def connect(self):
        return self._unsupported(DeviceLifecycleAction.CONNECT)

    def disconnect(self):
        return self._unsupported(DeviceLifecycleAction.DISCONNECT)

    def probe(self):
        return self._unsupported(DeviceLifecycleAction.PROBE)

    def shutdown(self):
        return self._unsupported(DeviceLifecycleAction.SHUTDOWN)

    def reconnect(self):
        manager = self._manager
        outcome = manager._bus.reconnect()
        affected = tuple(
            DeviceId("rotator", candidate.name)
            for candidate in manager._bus.registered_managers()
        )
        success = outcome.succeeded(manager._addr)
        details = outcome.error_for(manager._addr)

        if success:
            summary = f"Elliptec rotator {manager.name} reconnected"
        else:
            summary = (
                f"Elliptec rotator {manager.name} reconnect failed; "
                "mock fallback active"
            )

        return DeviceLifecycleResult(
            hardware_id=self.hardware_id,
            action=DeviceLifecycleAction.RECONNECT,
            success=success,
            summary=summary,
            details=details,
            affected_device_ids=affected,
        )


class ElliptecRotatorManager(DeviceManagerStatusMixin, RotatorManager):
    """Rotator manager for Thorlabs ELL14/ELL14K multidrop mounts.

    Each configured mount remains an independent logical/physical rotator in
    ImSwitch, while all mounts on the same COM port share one stable bus
    resource.  The shared resource can replace its real pylablib connection
    with virtual state (and vice versa) without replacing this manager object.

    Manager properties:
        - port (str, required): Serial port name (e.g. "COM20").
        - address (int, required): Elliptec multidrop address.
        - scale (str or float, default "stage"): pylablib scale parameter.
        - homeOnInit (bool, default False): home during initial startup only.
    """

    def __init__(self, rotatorInfo, name: str, *args, **kwargs):
        super().__init__(rotatorInfo, name, *args, **kwargs)
        self.__logger = initLogger(self)
        self._bus = None
        self._lifecycle = None

        if rotatorInfo is None:
            return

        self._port = str(rotatorInfo.managerProperties["port"])
        self._addr = int(rotatorInfo.managerProperties["address"])
        self._scale = rotatorInfo.managerProperties.get("scale", "stage")
        home_on_init = bool(
            rotatorInfo.managerProperties.get("homeOnInit", False)
        )

        # Scale conflicts are configuration errors and intentionally propagate.
        # Hardware-open/address failures, by contrast, are absorbed by the
        # stable bus resource as mock fallback state.
        self._bus = _SharedElliptecBus.get_bus(self._port, self._scale)
        self._bus.acquire(self, self._addr)
        self._lifecycle = _ElliptecRotatorLifecycle(self)

        self._position = self._bus.initialize_address(self._addr)
        self._syncStatusFromBus()

        if home_on_init:
            self.__logger.info(
                f"Homing Elliptec rotator {name} "
                f"(port={self._port} addr={self._addr})"
            )
            try:
                self._bus.home(self._addr)
            except Exception as exc:
                if isElliptecCommunicationError(exc):
                    self._bus.fallback_to_mock(exc)
                    # Preserve historical headless behavior: startup homing of
                    # a mock device simply establishes virtual zero.
                    self._bus.home(self._addr)
                else:
                    raise
            self._position = self._bus.cached_position(self._addr)
            self._syncStatusFromBus()

    @property
    def isMock(self) -> bool:
        return self._bus is None or not self._bus.is_real(self._addr)

    def getDeviceLifecycle(self):
        return self._lifecycle

    def move_abs(self, pos_deg: float) -> None:
        self._move_with_retry(float(pos_deg))
        self._update_position()

    def move_rel(self, d_deg: float) -> None:
        target = self._position + float(d_deg)
        self._move_with_retry(target)
        self._update_position()

    def _move_with_retry(self, target_deg: float) -> None:
        """Retry transient Elliptec NAKs before considering bus fallback."""
        last_error = None
        for attempt in range(6):
            try:
                self._bus.move_to(self._addr, target_deg)
                self._position = self._bus.cached_position(self._addr)
                self._syncStatusFromBus()
                return
            except Exception as exc:
                last_error = exc
                self.__logger.warning(
                    f"Elliptec move failed (port={self._port} addr={self._addr}, "
                    f"attempt {attempt + 1}/6): {exc}"
                )

        if last_error is not None and isElliptecCommunicationError(last_error):
            self._bus.fallback_to_mock(last_error)
            self._syncStatusFromBus()
        # The command which discovers connection loss must still fail once;
        # subsequent commands use the mock resource normally.
        raise last_error

    def _update_position(self) -> None:
        try:
            self._position = self._bus.get_position(self._addr)
        except Exception as exc:
            if isElliptecCommunicationError(exc):
                self._bus.fallback_to_mock(exc)
                self._syncStatusFromBus()
            raise
        self._syncStatusFromBus()
        self.__logger.debug(
            f"Elliptec position (port={self._port} addr={self._addr}): "
            f"{self._position}°"
        )

    def _syncStatusFromBus(self) -> None:
        if self._bus is None:
            return
        if self._bus.is_real(self._addr):
            self._setConnected(
                f"Elliptec address {self._addr} connected on {self._port}"
            )
            return

        error = self._bus.error_for(self._addr)
        self._setConnectionError(
            error or f"Elliptec address {self._addr} unavailable",
            summary=(
                f"Elliptec address {self._addr} unavailable; mock fallback active"
            ),
            mock_active=True,
        )

    def _onElliptecBusStateChanged(self) -> None:
        """Called by the shared bus after fallback/reconnect transitions."""
        self._position = self._bus.cached_position(self._addr)
        self._syncStatusFromBus()

    def finalize(self) -> None:
        if self._bus is not None:
            self._bus.release(self, self._addr)
            self.__logger.info(
                f"Released Elliptec rotator "
                f"(port={self._port} addr={self._addr})"
            )
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
