from __future__ import annotations

from dataclasses import replace
import threading

from imswitch.imcommon.model import initLogger

from .graph import DeviceRelationKind, HardwareDeviceId
from .lifecycle import (
    DeviceHandle,
    DeviceLifecycleAction,
    DeviceLifecycleBlockedError,
    DeviceLifecycleBusyError,
    DeviceLifecycleError,
    DeviceLifecycleNotSupportedError,
    DeviceLifecycleResult,
)


class DeviceLifecycleService:
    """Opt-in runtime lifecycle coordinator for physical devices.

    ``DeviceSupervisor`` remains the read-only source of inventory/status.
    This service consumes that graph, discovers explicit manager lifecycle
    providers, applies coarse application-ownership guards, and serializes
    transitions per physical device.

    V1 is deliberately default-deny: a device is reconnectable only when one
    of its logical managers explicitly contributes a lifecycle adapter.
    """

    def __init__(self, master, supervisor):
        self.__logger = initLogger(self)
        self._master = master
        self._supervisor = supervisor
        self._graph = self._supervisor.getDeviceGraph()
        self._handles = self._buildHandles(self._graph)
        self._reconnectBlockReasons = self._findReconnectBlockReasons(self._graph)
        self._operationLocks = {
            hardware_id: threading.Lock() for hardware_id in self._handles
        }
        self._listeners = []
        self._listenersLock = threading.RLock()

    def _buildHandles(self, graph) -> dict:
        grouped = {}
        for descriptor in graph.descriptors:
            grouped.setdefault(descriptor.hardware_id, []).append(descriptor)

        handles = {}
        for hardware_id, descriptors in grouped.items():
            source_ids = tuple(
                sorted(
                    {descriptor.source_device_id for descriptor in descriptors},
                    key=lambda item: (item.kind, item.name.casefold()),
                )
            )
            lifecycles = []
            for source_id in source_ids:
                try:
                    manager = self._supervisor.getManager(source_id)
                except KeyError:
                    # Synthetic graph endpoints (currently NI-DAQ boards) do
                    # not correspond one-to-one with manager objects.
                    continue
                provider = getattr(type(manager), "getDeviceLifecycle", None)
                if not callable(provider):
                    continue
                try:
                    lifecycle = provider(manager)
                except Exception as exc:
                    self.__logger.warning(
                        "Could not discover lifecycle for %s/%s: %s",
                        source_id.kind,
                        source_id.name,
                        exc,
                        exc_info=True,
                    )
                    continue
                if lifecycle is None:
                    continue
                if (
                    not hasattr(lifecycle, "hardware_id")
                    or not hasattr(lifecycle, "capabilities")
                    or not callable(getattr(lifecycle, "reconnect", None))
                ):
                    self.__logger.warning(
                        "Ignoring invalid lifecycle provider on %s/%s",
                        source_id.kind,
                        source_id.name,
                    )
                    continue
                if lifecycle.hardware_id != hardware_id:
                    self.__logger.warning(
                        "Ignoring lifecycle for %s/%s: lifecycle hardware id %r "
                        "does not match device graph id %r",
                        source_id.kind,
                        source_id.name,
                        lifecycle.hardware_id,
                        hardware_id,
                    )
                    continue
                if not any(candidate is lifecycle for candidate in lifecycles):
                    lifecycles.append(lifecycle)

            lifecycle = None
            if len(lifecycles) == 1:
                lifecycle = lifecycles[0]
            elif len(lifecycles) > 1:
                # Conflicting owners for one physical device are unsafe. Keep
                # the handle visible but disable lifecycle actions.
                self.__logger.error(
                    "Multiple lifecycle owners were contributed for physical "
                    "device %r; lifecycle actions disabled",
                    hardware_id,
                )

            handles[hardware_id] = DeviceHandle(
                hardware_id=hardware_id,
                source_device_ids=source_ids,
                lifecycle=lifecycle,
            )
        return handles


    def _findReconnectBlockReasons(self, graph) -> dict:
        """Reject V1 reconnect when its transport is shared across devices.

        In-place RS232 replacement is safe for all logical components of the
        *same* physical device, but not yet for an unrelated device that also
        retains the same transport manager. Later lifecycle adapters may
        coordinate such groups explicitly; V1 fails closed.
        """
        users_by_transport = {}
        for relation in graph.relations:
            if relation.kind is not DeviceRelationKind.USES_TRANSPORT:
                continue
            if not isinstance(relation.source, HardwareDeviceId):
                continue
            users_by_transport.setdefault(relation.target, set()).add(
                relation.source
            )

        reasons = {}
        for hardware_id, handle in self._handles.items():
            if not handle.capabilities.reconnect:
                continue
            for relation in graph.relationsFrom(hardware_id):
                if relation.kind is not DeviceRelationKind.USES_TRANSPORT:
                    continue
                users = users_by_transport.get(relation.target, set())
                other_users = users - {hardware_id}
                if other_users:
                    label = relation.label or str(relation.target)
                    reasons[hardware_id] = (
                        f"Reconnect is disabled because transport {label!r} is "
                        "shared with another physical device."
                    )
                    break
        return reasons


    def registerLifecycle(self, hardware_id, lifecycle) -> None:
        """Attach a lifecycle owner created after initial manager discovery."""
        handle = self.getHandle(hardware_id)
        if getattr(lifecycle, "hardware_id", None) != hardware_id:
            raise ValueError(
                f"Lifecycle hardware id {getattr(lifecycle, 'hardware_id', None)!r} "
                f"does not match {hardware_id!r}"
            )
        current = handle.lifecycle
        if current is lifecycle:
            return
        if current is not None:
            raise DeviceLifecycleError(
                f"Physical device {hardware_id!r} already has a lifecycle owner"
            )
        self._handles[hardware_id] = replace(handle, lifecycle=lifecycle)
        self._operationLocks.setdefault(hardware_id, threading.Lock())
        self._reconnectBlockReasons = self._findReconnectBlockReasons(self._graph)

    def unregisterLifecycle(self, hardware_id, lifecycle=None) -> None:
        """Remove a previously late-registered lifecycle owner."""
        handle = self.getHandle(hardware_id)
        current = handle.lifecycle
        if current is None:
            return
        if lifecycle is not None and current is not lifecycle:
            return
        self._handles[hardware_id] = replace(handle, lifecycle=None)
        self._reconnectBlockReasons = self._findReconnectBlockReasons(self._graph)

    def getHandle(self, hardware_id) -> DeviceHandle:
        try:
            return self._handles[hardware_id]
        except KeyError as exc:
            raise KeyError(f"Unknown physical device {hardware_id!r}") from exc

    def getHandles(self) -> tuple[DeviceHandle, ...]:
        return tuple(
            self._handles[hardware_id]
            for hardware_id in sorted(self._handles)
        )

    def canReconnect(self, hardware_id) -> bool:
        try:
            handle = self.getHandle(hardware_id)
        except KeyError:
            return False
        return bool(
            handle.capabilities.reconnect
            and hardware_id not in self._reconnectBlockReasons
        )

    def getReconnectableHardwareIds(self) -> tuple:
        return tuple(
            sorted(
                handle.hardware_id
                for handle in self._handles.values()
                if (
                    handle.capabilities.reconnect
                    and handle.hardware_id not in self._reconnectBlockReasons
                )
            )
        )

    def addListener(self, callback) -> None:
        """Register a lifecycle-result listener.

        Listeners run on the thread that performs the lifecycle operation.
        UI consumers must marshal work onto their controller thread.
        """
        with self._listenersLock:
            if not any(candidate is callback for candidate in self._listeners):
                self._listeners.append(callback)

    def removeListener(self, callback) -> None:
        with self._listenersLock:
            self._listeners = [
                candidate for candidate in self._listeners if candidate is not callback
            ]

    def _publish(self, result: DeviceLifecycleResult) -> None:
        with self._listenersLock:
            listeners = tuple(self._listeners)
        for callback in listeners:
            try:
                callback(result)
            except Exception:
                self.__logger.warning(
                    "Device lifecycle listener failed", exc_info=True
                )

    def _assertRuntimeTransitionSafe(self) -> None:
        coordinator = getattr(self._master, "scanExecutionCoordinator", None)
        if coordinator is not None:
            if (
                getattr(coordinator, "activeRunToken", None) is not None
                or getattr(coordinator, "activeToken", None) is not None
            ):
                raise DeviceLifecycleBlockedError(
                    "Device reconnect is blocked while a scan is active."
                )

        recording = getattr(self._master, "recordingManager", None)
        if recording is not None and bool(getattr(recording, "record", False)):
            raise DeviceLifecycleBlockedError(
                "Device reconnect is blocked while a recording is active."
            )

    def reconnect(self, hardware_id) -> DeviceLifecycleResult:
        handle = self.getHandle(hardware_id)
        lifecycle = handle.lifecycle
        blocked_reason = self._reconnectBlockReasons.get(hardware_id)
        if blocked_reason is not None:
            raise DeviceLifecycleBlockedError(blocked_reason)
        if lifecycle is None or not handle.capabilities.reconnect:
            raise DeviceLifecycleNotSupportedError(
                f"Reconnect is not supported for {hardware_id!r}."
            )

        lock = self._operationLocks[hardware_id]
        if not lock.acquire(blocking=False):
            raise DeviceLifecycleBusyError(
                f"A lifecycle operation is already running for {hardware_id!r}."
            )
        try:
            self._assertRuntimeTransitionSafe()
            try:
                result = lifecycle.reconnect()
            except Exception as exc:
                self.__logger.error(
                    "Unexpected reconnect failure for %r: %s",
                    hardware_id,
                    exc,
                    exc_info=True,
                )
                result = DeviceLifecycleResult(
                    hardware_id=hardware_id,
                    action=DeviceLifecycleAction.RECONNECT,
                    success=False,
                    summary="Device reconnect failed unexpectedly",
                    details=str(exc),
                    affected_device_ids=handle.source_device_ids,
                )

            if result.hardware_id != hardware_id:
                result = replace(result, hardware_id=hardware_id)
            if not result.affected_device_ids:
                result = replace(
                    result, affected_device_ids=handle.source_device_ids
                )
            self._publish(result)
            return result
        finally:
            lock.release()
