from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import threading

from imswitch.imcommon.model import initLogger

from .acquisition_gate import MaintenanceBlockedError, get_acquisition_gate
from .graph import DeviceRelationKind, HardwareDeviceId
from .status import DeviceId
from .lifecycle import (
    DeviceHandle,
    DeviceLifecycleAction,
    DeviceLifecycleBlockedError,
    DeviceLifecycleBusyError,
    DeviceLifecycleCapabilities,
    DeviceLifecycleError,
    DeviceLifecycleNotSupportedError,
    DeviceLifecycleResult,
)


class _TransportBackedLifecycle:
    """The lifecycle of a device that has no adapter of its own but uses a
    transport that can be reopened in place. ``reconnect`` is never called
    on it: the service runs the transport reconnect instead."""

    capabilities = DeviceLifecycleCapabilities(reconnect=True)

    def __init__(self, hardware_id) -> None:
        self.hardware_id = hardware_id

    def reconnect(self):
        raise DeviceLifecycleNotSupportedError(
            "A transport-backed reconnect runs through the lifecycle service.")

    def connect(self):
        raise DeviceLifecycleNotSupportedError("Not supported.")

    disconnect = probe = shutdown = connect


#: Runtime transitions the service runs (probe and shutdown are not offered).
_TRANSITIONS = (
    DeviceLifecycleAction.CONNECT,
    DeviceLifecycleAction.DISCONNECT,
    DeviceLifecycleAction.RECONNECT,
)


class DeviceLifecycleService:
    """Runtime lifecycle coordinator for physical devices.

    ``DeviceSupervisor`` remains the read-only source of inventory/status.
    This service consumes that graph, discovers manager lifecycle providers,
    applies the application-ownership guards, and serializes transitions per
    physical device.

    A device is reconnectable when one of its managers contributes a
    lifecycle adapter, **or** when it uses a transport that can be reopened
    in place (an ``rs232devices`` manager with ``reconnectTransport``). In the
    second case the service reconnects the transport once and re-initialises
    every device on it (``docs/design/plans/device-reconnect-2.0.md`` §4.3):

    - before: each dependent's ``_lifecycleSafeState(verified=False)`` (best
      effort, errors ignored);
    - ``transport.reconnectTransport()`` once;
    - after, per dependent: its lifecycle's ``onTransportReconnected(real)``
      (one call per physical device), else each manager's
      ``_onTransportReconnected(real)``, else the default (status taken from
      the transport). A hook returns the errors it found; any error fails the
      reconnect, and the device shows it.
    """

    def __init__(self, master, supervisor):
        self.__logger = initLogger(self)
        self._master = master
        self._supervisor = supervisor
        self._graph = self._supervisor.getDeviceGraph()
        self._transportOf, self._usersOfTransport = self._findReconnectableTransports(self._graph)
        self._handles = self._buildHandles(self._graph)
        self._operationLocks = {
            hardware_id: threading.Lock() for hardware_id in self._handles
        }
        self._listeners = []
        self._statusListeners = []
        self._listenersLock = threading.RLock()
        self._subscribeStatusSources()
        self._shutdownLock = threading.Lock()
        self._shuttingDown = False
        self._inFlight = 0

    def beginShutdown(self) -> None:
        """Refuse every new lifecycle operation from now on.

        Operations already running finish; :meth:`operationsInFlight` tells
        the shutdown barrier whether they have.
        """
        with self._shutdownLock:
            self._shuttingDown = True

    def operationsInFlight(self) -> int:
        with self._shutdownLock:
            return self._inFlight

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
            elif hardware_id in self._transportOf:
                # No adapter of its own, but a transport that can be reopened
                # in place: reconnect is the transport's, with the managers'
                # re-initialisation hooks.
                lifecycle = _TransportBackedLifecycle(hardware_id)

            handles[hardware_id] = DeviceHandle(
                hardware_id=hardware_id,
                source_device_ids=source_ids,
                lifecycle=lifecycle,
            )
        return handles


    def _findReconnectableTransports(self, graph):
        """``hardware_id -> transport DeviceId`` for every device whose
        transport manager can be reopened in place, and the reverse map."""
        transport_of = {}
        users = {}
        for relation in graph.relations:
            if relation.kind is not DeviceRelationKind.USES_TRANSPORT:
                continue
            if not isinstance(relation.source, HardwareDeviceId):
                continue
            if not isinstance(relation.target, DeviceId):
                continue
            try:
                transport = self._supervisor.getManager(relation.target)
            except KeyError:
                continue
            if not callable(getattr(transport, "reconnectTransport", None)):
                continue
            transport_of[relation.source] = relation.target
            users.setdefault(relation.target, set()).add(relation.source)
        return transport_of, {t: tuple(sorted(u)) for t, u in users.items()}

    def transportOf(self, hardware_id):
        """The reconnectable transport ``hardware_id`` uses, or ``None``."""
        return self._transportOf.get(hardware_id)

    def devicesOnTransport(self, transport_id) -> tuple:
        """Every physical device that uses ``transport_id``."""
        return self._usersOfTransport.get(transport_id, ())

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

    def canPerform(self, hardware_id, action) -> bool:
        """Whether the Hardware status window may offer ``action``."""
        action = DeviceLifecycleAction(action)
        try:
            handle = self.getHandle(hardware_id)
        except KeyError:
            return False
        return bool(
            action in _TRANSITIONS
            and handle.capabilities.supports(action)
        )

    def getActionableHardwareIds(self, action) -> tuple:
        return tuple(
            sorted(
                hardware_id for hardware_id in self._handles
                if self.canPerform(hardware_id, action)
            )
        )

    def canReconnect(self, hardware_id) -> bool:
        return self.canPerform(hardware_id, DeviceLifecycleAction.RECONNECT)

    def getReconnectableHardwareIds(self) -> tuple:
        return self.getActionableHardwareIds(DeviceLifecycleAction.RECONNECT)

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

    def addStatusListener(self, callback) -> None:
        """Register ``callback(hardware_id)`` for status changes a device
        reports by itself -- an instrument fault (read error, USB removed),
        or a connect / disconnect done outside this service.

        Runs on the thread where the change happened (often a run or poller
        thread). UI consumers must marshal work onto their controller thread.
        """
        with self._listenersLock:
            if not any(candidate is callback for candidate in self._statusListeners):
                self._statusListeners.append(callback)

    def removeStatusListener(self, callback) -> None:
        with self._listenersLock:
            self._statusListeners = [
                candidate for candidate in self._statusListeners
                if candidate is not callback
            ]

    def _subscribeStatusSources(self) -> None:
        # Managers that report their own status changes (instrument managers)
        # expose addStatusListener(callback(name)); forward as hardware ids.
        for hardware_id, handle in self._handles.items():
            for source_id in handle.source_device_ids:
                try:
                    manager = self._supervisor.getManager(source_id)
                except KeyError:
                    continue
                subscribe = getattr(manager, "addStatusListener", None)
                if callable(subscribe):
                    subscribe(lambda _name, hardware_id=hardware_id:
                              self._publishStatus(hardware_id))

    def _publishStatus(self, hardware_id) -> None:
        with self._listenersLock:
            listeners = tuple(self._statusListeners)
        for callback in listeners:
            try:
                callback(hardware_id)
            except Exception:
                self.__logger.warning(
                    "Device status listener failed", exc_info=True
                )

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

    def _assertRuntimeTransitionSafe(self, verb: str = "reconnect") -> None:
        coordinator = getattr(self._master, "scanExecutionCoordinator", None)
        if coordinator is not None:
            if (
                getattr(coordinator, "activeRunToken", None) is not None
                or getattr(coordinator, "activeToken", None) is not None
            ):
                raise DeviceLifecycleBlockedError(
                    f"Device {verb} is blocked while a scan is active."
                )

        recording = getattr(self._master, "recordingManager", None)
        if recording is not None and bool(getattr(recording, "record", False)):
            raise DeviceLifecycleBlockedError(
                f"Device {verb} is blocked while a recording is active."
            )

    def reconnect(self, hardware_id) -> DeviceLifecycleResult:
        return self._transition(hardware_id, DeviceLifecycleAction.RECONNECT)

    def connect(self, hardware_id) -> DeviceLifecycleResult:
        """Connect a device that is not connected (a transient instrument)."""
        return self._transition(hardware_id, DeviceLifecycleAction.CONNECT)

    def disconnect(self, hardware_id) -> DeviceLifecycleResult:
        """Disconnect a device so it can be unplugged (a transient instrument)."""
        return self._transition(hardware_id, DeviceLifecycleAction.DISCONNECT)

    def _transition(self, hardware_id, action) -> DeviceLifecycleResult:
        """One runtime transition, with the same rules for every action:
        declared capability, shutdown refusal, per-device lock, and -- for
        devices that take part in acquisitions -- the scan / recording guard
        and acquisition-gate maintenance."""
        action = DeviceLifecycleAction(action)
        verb = action.value
        handle = self.getHandle(hardware_id)
        lifecycle = handle.lifecycle
        if lifecycle is None or not handle.capabilities.supports(action):
            raise DeviceLifecycleNotSupportedError(
                f"{verb.capitalize()} is not supported for {hardware_id!r}."
            )

        with self._shutdownLock:
            if self._shuttingDown:
                raise DeviceLifecycleBlockedError(
                    f"Device {verb} is refused: the application is shutting down."
                )
            self._inFlight += 1
        try:
            return self._transitionAdmitted(hardware_id, handle, lifecycle, action)
        finally:
            with self._shutdownLock:
                self._inFlight -= 1

    def _transitionAdmitted(self, hardware_id, handle, lifecycle, action) -> DeviceLifecycleResult:
        verb = action.value
        lock = self._operationLocks[hardware_id]
        if not lock.acquire(blocking=False):
            raise DeviceLifecycleBusyError(
                f"A lifecycle operation is already running for {hardware_id!r}."
            )
        try:
            # Instruments never take part in scans or recordings (they are
            # guarded by their reservation instead), so a lifecycle may
            # declare affectsAcquisition = False and skip the gate.
            affectsAcquisition = bool(getattr(lifecycle, "affectsAcquisition", True))
            try:
                with self._ownDevices(hardware_id, handle, verb):
                    result = self._runLifecycle(lifecycle, verb, hardware_id, affectsAcquisition)
            except MaintenanceBlockedError as exc:
                raise DeviceLifecycleBlockedError(str(exc)) from None
            except DeviceLifecycleError:
                # Expected lifecycle policy failures (for example a detector
                # still owned by an acquisition lease) should reach the caller
                # as blocked/not-supported errors, just like the service-level
                # scan/recording guards above.
                raise
            except Exception as exc:
                self.__logger.error(
                    "Unexpected %s failure for %r: %s",
                    verb,
                    hardware_id,
                    exc,
                    exc_info=True,
                )
                result = DeviceLifecycleResult(
                    hardware_id=hardware_id,
                    action=action,
                    success=False,
                    summary=f"Device {verb} failed unexpectedly",
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

    def _runLifecycle(self, lifecycle, verb, hardware_id, affectsAcquisition):
        transport_id = self._transportOf.get(hardware_id)
        if verb == DeviceLifecycleAction.RECONNECT.value and transport_id is not None:
            run = lambda: self._reconnectThroughTransport(hardware_id, transport_id)
        else:
            run = getattr(lifecycle, verb)
        if affectsAcquisition:
            self._assertRuntimeTransitionSafe(verb)
            # Held for the whole adapter call: no scan or recording can start
            # until the backend replacement has finished.
            with get_acquisition_gate().maintenance(f"{verb} of {hardware_id}"):
                return run()
        return run()

    # ------------------------------------------------- transport reconnect
    def _reconnectThroughTransport(self, hardware_id, transport_id) -> DeviceLifecycleResult:
        """Reopen the transport once; re-initialise every device on it."""
        transport = self._supervisor.getManager(transport_id)
        dependents = self.devicesOnTransport(transport_id) or (hardware_id,)
        affected = []
        for dependent in dependents:
            handle = self._handles.get(dependent)
            if handle is not None:
                affected.extend(handle.source_device_ids)
        label = transport_id.name

        # Best effort before the transport is touched: a broken connection
        # may make this fail; the verified safe state comes after.
        for dependent in dependents:
            for hook in self._transportHooks(dependent, "_lifecycleSafeState", "transportSafeState"):
                try:
                    hook(verified=False)
                except Exception:
                    pass

        try:
            real = bool(transport.reconnectTransport())
        except Exception as exc:
            real = False
            self.__logger.error("Transport %s reconnect raised: %s", label, exc, exc_info=True)

        errors = []
        for dependent in dependents:
            hooks = self._transportHooks(dependent, "_onTransportReconnected", "onTransportReconnected")
            if not hooks:
                hooks = [self._defaultTransportHook(dependent, transport, label)]
            for hook in hooks:
                try:
                    found = hook(real)
                except Exception as exc:
                    found = [f"{type(exc).__name__}: {exc}"]
                errors.extend(f"{dependent.key}: {e}" for e in (found or ()))

        if not real:
            details = getattr(transport, "connectionStatusDetails", None)
            summary = f"Transport {label!r} did not reconnect"
            if errors or details:
                summary += "; " + "; ".join(errors or [str(details)])
            success = False
        elif errors:
            summary = f"Transport {label!r} reconnected, but: " + "; ".join(errors)
            success = False
        else:
            names = ", ".join(sorted(d.key for d in dependents))
            summary = f"Transport {label!r} reconnected; re-initialised {names}"
            success = True
        return DeviceLifecycleResult(
            hardware_id=hardware_id,
            action=DeviceLifecycleAction.RECONNECT,
            success=success,
            summary=summary,
            details="; ".join(errors) or None,
            affected_device_ids=tuple(affected),
            # Lasers always come back dark: the Laser panel shows OFF.
            deactivated_device_ids=tuple(d for d in affected if d.kind == "laser"),
        )

    def _transportHooks(self, hardware_id, manager_hook, lifecycle_hook):
        """The re-initialisation hooks of one physical device: its lifecycle's
        (once for the whole device) or, failing that, each manager's."""
        handle = self._handles.get(hardware_id)
        if handle is None:
            return []
        lifecycle = handle.lifecycle
        hook = getattr(lifecycle, lifecycle_hook, None)
        if callable(hook):
            return [hook]
        hooks = []
        for source_id in handle.source_device_ids:
            try:
                manager = self._supervisor.getManager(source_id)
            except KeyError:
                continue
            hook = getattr(manager, manager_hook, None)
            if callable(hook):
                hooks.append(hook)
        return hooks

    def _defaultTransportHook(self, hardware_id, transport, label):
        """A manager with no hook of its own talks to the transport directly
        and keeps no device state: its status follows the transport's."""
        handle = self._handles.get(hardware_id)
        managers = []
        for source_id in handle.source_device_ids if handle else ():
            try:
                managers.append(self._supervisor.getManager(source_id))
            except KeyError:
                continue

        def hook(real):
            details = getattr(transport, "connectionStatusDetails", None)
            for manager in managers:
                if real:
                    setter = getattr(manager, "_setConnected", None)
                    if callable(setter):
                        setter(f"{label} reconnected")
                else:
                    setter = getattr(manager, "_setConnectionError", None)
                    if callable(setter):
                        setter(details or f"transport {label} unavailable",
                               summary=f"Transport {label} did not reconnect",
                               mock_active=getattr(transport, "runtimeMode", None) is not None
                               and transport.runtimeMode.value == "mock")
            return [] if real else [details or f"transport {label} unavailable"]
        return hook

    def _affectedDeviceIds(self, hardware_id, handle) -> set:
        """The logical devices a transition of ``hardware_id`` touches: its
        own managers, plus every device that depends on it (a component of
        it, or one that uses it as transport or control backend)."""
        affected = set(handle.source_device_ids)
        transport_id = self._transportOf.get(hardware_id)
        if transport_id is not None:
            for dependent in self.devicesOnTransport(transport_id):
                other = self._handles.get(dependent)
                if other is not None:
                    affected.update(other.source_device_ids)
        for relation in self._graph.relations:
            if relation.target != hardware_id:
                continue
            source = relation.source
            if isinstance(source, HardwareDeviceId):
                dependent = self._handles.get(source)
                if dependent is not None:
                    affected.update(dependent.source_device_ids)
            elif isinstance(source, DeviceId):
                affected.add(source)
        return affected

    @contextmanager
    def _ownDevices(self, hardware_id, handle, verb):
        """Hold an admission ticket on every affected device's resource for
        the whole transition, so it is refused while a measurement run or a
        script reservation holds one of them -- and no reservation can start
        until the transition is over (reservations wait for in-flight
        commands)."""
        from imswitch.imcontrol.model.resources import (
            ReservationExpiredError,
            ResourceReservedError,
            get_resource_registry,
            instrument_key,
            laser_key,
            positioner_key,
            rotator_key,
        )

        keyFor = {
            "rotator": rotator_key,
            "laser": laser_key,
            "positioner": positioner_key,
            "instrument": instrument_key,
        }
        keys = sorted({
            keyFor[device.kind](device.name)
            for device in self._affectedDeviceIds(hardware_id, handle)
            if device.kind in keyFor
        })
        registry = get_resource_registry()
        tickets = []
        try:
            for key in keys:
                try:
                    tickets.append(
                        registry.admit(key, None, label=f"{verb} of {hardware_id}")
                    )
                except (ResourceReservedError, ReservationExpiredError) as exc:
                    raise DeviceLifecycleBlockedError(
                        f"Device {verb} is blocked: {exc}"
                    ) from None
            yield
        finally:
            for ticket in reversed(tickets):
                registry.release_ticket(ticket)
