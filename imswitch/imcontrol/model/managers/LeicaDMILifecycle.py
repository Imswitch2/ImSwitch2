from __future__ import annotations

import threading
from weakref import WeakKeyDictionary, WeakSet

from imswitch.imcontrol.model.devices.graph import HardwareDeviceId
from imswitch.imcontrol.model.devices.lifecycle import (
    DeviceLifecycleAction,
    DeviceLifecycleCapabilities,
    DeviceLifecycleNotSupportedError,
    DeviceLifecycleResult,
)
from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.interfaces.LeicaDMIHardware import (
    reconnectLeicaDMIHardware,
)


_LEICA_LIFECYCLE_CACHE = WeakKeyDictionary()
_LEICA_LIFECYCLE_CACHE_LOCK = threading.RLock()


class LeicaDMILifecycle:
    """Runtime lifecycle for one physical Leica DMI stand.

    Stand controls and objective-Z are logical components of the same physical
    controller and share one RS232 manager and one Leica hardware interface.
    Reconnect therefore replaces the transport once, revalidates the shared
    Leica interface (or creates it if startup could not), and synchronizes every
    registered component against that same interface.
    """

    capabilities = DeviceLifecycleCapabilities(reconnect=True)

    def __init__(self, rs232manager, rs232_name: str):
        self.__logger = initLogger(self)
        self._rs232manager = rs232manager
        self._rs232_name = str(rs232_name)
        self._hardware_id = HardwareDeviceId(
            category="stand", key=f"leica:{self._rs232_name}"
        )
        self._participants = WeakSet()
        self._participant_config = WeakKeyDictionary()
        self._lock = threading.RLock()

    @property
    def hardware_id(self):
        return self._hardware_id

    def registerManager(self, manager, managerProperties=None) -> None:
        with self._lock:
            self._participants.add(manager)
            self._participant_config[manager] = dict(managerProperties or {})

    def _managers(self):
        return tuple(
            sorted(
                self._participants,
                key=lambda manager: (
                    getattr(manager, "_leicaLifecycleSortKey", lambda: type(manager).__name__)()
                ).casefold(),
            )
        )

    def _deviceIds(self):
        device_ids = []
        for manager in self._managers():
            getter = getattr(manager, "_leicaLifecycleDeviceId", None)
            if callable(getter):
                device_ids.append(getter())
        return tuple(sorted(set(device_ids), key=lambda item: (item.kind, item.name.casefold())))

    def _unsupported(self, action: DeviceLifecycleAction):
        raise DeviceLifecycleNotSupportedError(
            f"Leica DMI lifecycle does not yet support {action.value}."
        )

    def connect(self):
        return self._unsupported(DeviceLifecycleAction.CONNECT)

    def disconnect(self):
        return self._unsupported(DeviceLifecycleAction.DISCONNECT)

    def probe(self):
        return self._unsupported(DeviceLifecycleAction.PROBE)

    def shutdown(self):
        return self._unsupported(DeviceLifecycleAction.SHUTDOWN)

    def _adoptHardware(self, hardware, *, error=None, mock_active=False) -> None:
        for manager in self._managers():
            adopter = getattr(manager, "_adoptLeicaHardware", None)
            if callable(adopter):
                adopter(hardware, error=error, mock_active=mock_active)

    def _rebuildHardware(self):
        configurations = [
            self._participant_config.get(manager, {}) for manager in self._managers()
        ]
        return reconnectLeicaDMIHardware(
            self._rs232manager,
            managerPropertiesSequence=configurations,
            logger=self.__logger,
        )

    @staticmethod
    def _forceSafeIlluminationOff(hardware):
        errors = []
        for method_name in ("setILshutter", "setTLshutter"):
            method = getattr(hardware, method_name, None)
            if not callable(method):
                continue
            try:
                method(0)
            except Exception as exc:
                errors.append(f"{method_name}: {exc}")
        return errors

    def reconnect(self):
        with self._lock:
            device_ids = self._deviceIds()

            # Best effort before transport replacement. A broken connection may
            # make this fail; the verified post-reconnect OFF is authoritative.
            current_hardware = next(
                (
                    getattr(manager, "_hardware", None)
                    for manager in self._managers()
                    if getattr(manager, "_hardware", None) is not None
                ),
                None,
            )
            if current_hardware is not None:
                self._forceSafeIlluminationOff(current_hardware)

            real_transport = self._rs232manager.reconnectTransport()
            if not real_transport:
                details = getattr(
                    self._rs232manager,
                    "connectionStatusDetails",
                    "Leica RS232 reconnect failed",
                )
                self._adoptHardware(None, error=details, mock_active=True)
                return DeviceLifecycleResult(
                    hardware_id=self.hardware_id,
                    action=DeviceLifecycleAction.RECONNECT,
                    success=False,
                    summary="Leica stand reconnect failed; mock fallback active",
                    details=details,
                    affected_device_ids=device_ids,
                )

            try:
                hardware = self._rebuildHardware()
            except Exception as exc:
                hardware = None
                rebuild_error = str(exc)
            else:
                rebuild_error = None

            if hardware is None:
                details = rebuild_error or "Leica DMI protocol probe failed after RS232 reconnect"
                self._adoptHardware(None, error=details, mock_active=False)
                return DeviceLifecycleResult(
                    hardware_id=self.hardware_id,
                    action=DeviceLifecycleAction.RECONNECT,
                    success=False,
                    summary="Leica RS232 reconnected but stand initialization failed",
                    details=details,
                    affected_device_ids=device_ids,
                )

            safe_off_errors = self._forceSafeIlluminationOff(hardware)
            self._adoptHardware(hardware)

            if safe_off_errors:
                details = "; ".join(safe_off_errors)
                for manager in self._managers():
                    setter = getattr(manager, "_setConnectionError", None)
                    if callable(setter):
                        setter(
                            details,
                            summary=(
                                "Leica reconnected but safe shutter initialization failed"
                            ),
                        )
                return DeviceLifecycleResult(
                    hardware_id=self.hardware_id,
                    action=DeviceLifecycleAction.RECONNECT,
                    success=False,
                    summary="Leica reconnected but safe shutter initialization failed",
                    details=details,
                    affected_device_ids=device_ids,
                )

            return DeviceLifecycleResult(
                hardware_id=self.hardware_id,
                action=DeviceLifecycleAction.RECONNECT,
                success=True,
                summary="Leica stand reconnected; illumination shutters closed",
                affected_device_ids=device_ids,
            )


def getLeicaDMILifecycle(rs232manager, rs232_name: str) -> LeicaDMILifecycle:
    """Return the one physical lifecycle associated with an RS232 manager."""
    with _LEICA_LIFECYCLE_CACHE_LOCK:
        lifecycle = _LEICA_LIFECYCLE_CACHE.get(rs232manager)
        if lifecycle is None:
            lifecycle = LeicaDMILifecycle(rs232manager, rs232_name)
            _LEICA_LIFECYCLE_CACHE[rs232manager] = lifecycle
        return lifecycle
