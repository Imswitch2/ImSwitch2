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
                # Never report "shutters closed" for a shutter nobody closed.
                errors.append(f"{method_name}: not available on this hardware interface")
                continue
            try:
                method(0)
            except Exception as exc:
                errors.append(f"{method_name}: {exc}")
        return errors

    # Transport hooks (DeviceLifecycleService §4.3): the service reopens the
    # RS232 port once for every device on it and calls these.
    def _currentHardware(self):
        return next(
            (
                getattr(manager, "_hardware", None)
                for manager in self._managers()
                if getattr(manager, "_hardware", None) is not None
            ),
            None,
        )

    def transportSafeState(self, *, verified: bool):
        """Illumination shutters closed. Before a reconnect: best effort.
        After: errors returned (a shutter nobody closed is never reported
        closed)."""
        hardware = self._currentHardware()
        if hardware is None:
            return ["no Leica hardware interface"] if verified else []
        errors = self._forceSafeIlluminationOff(hardware)
        return errors if verified else []

    def onTransportReconnected(self, real: bool):
        """Rebuild the shared Leica interface on the reopened port, hand it to
        every component and close the shutters; the errors found, if any."""
        with self._lock:
            if not real:
                details = getattr(
                    self._rs232manager, "connectionStatusDetails",
                    "Leica RS232 reconnect failed",
                )
                self._adoptHardware(None, error=details, mock_active=True)
                return [f"Leica stand reconnect failed; mock fallback active ({details})"]
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
                return [f"Leica RS232 reconnected but stand initialization failed ({details})"]
            safe_off_errors = self._forceSafeIlluminationOff(hardware)
            self._adoptHardware(hardware)
            if safe_off_errors:
                details = "; ".join(safe_off_errors)
                for manager in self._managers():
                    setter = getattr(manager, "_setConnectionError", None)
                    if callable(setter):
                        setter(details, summary=(
                            "Leica reconnected but safe shutter initialization failed"))
                return [f"Leica reconnected but safe shutter initialization failed ({details})"]
            return []

    def reconnect(self):
        """Direct use; the service path is the same steps with every other
        device on the port re-initialised too."""
        with self._lock:
            device_ids = self._deviceIds()
            self.transportSafeState(verified=False)
            real_transport = self._rs232manager.reconnectTransport()
            errors = self.onTransportReconnected(real_transport)
            if errors:
                summary, _, details = errors[0].partition(" (")
                return DeviceLifecycleResult(
                    hardware_id=self.hardware_id,
                    action=DeviceLifecycleAction.RECONNECT,
                    success=False,
                    summary=summary,
                    details=details.rstrip(")") or None,
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
