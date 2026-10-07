"""Base manager for measurement instruments (power meters, polarimeters, ...).

An instrument manager owns one :class:`InstrumentSession` around one driver
(``imcontrol/model/measurement/instrument.py``): the session holds the I/O
lock, timing profiles, acquisition windows and sample generations.

Instruments are usually **transient** (``"transient": true`` in the setup):
- not touched at startup (unless ``connectOnStartup``), shown as *not
  connected* -- runtime mode ``ABSENT``, no backend, no mock, no error;
- connected, disconnected and reconnected at runtime through the device
  lifecycle (Hardware status window, scripts);
- a failed connect leaves the instrument disconnected with the error -- never
  in mock mode;
- a transport fault while connected (read error, USB removed) shows as an
  error, and every window on the instrument ends.

Lifecycle transitions are admitted through the resource registry, so an
instrument reserved by a measurement run or a script cannot be disconnected
under it.

Design: ``docs/design/plans/transient-instruments-step-scans.md`` §5-6.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.devices.graph import (
    DeviceDescriptorSpec,
    DeviceRole,
    HardwareDeviceId,
)
from imswitch.imcontrol.model.devices.lifecycle import (
    DeviceLifecycleAction,
    DeviceLifecycleBlockedError,
    DeviceLifecycleCapabilities,
    DeviceLifecycleNotSupportedError,
    DeviceLifecycleResult,
)
from imswitch.imcontrol.model.devices.status import (
    DeviceId,
    DeviceManagerStatusMixin,
    DeviceRuntimeMode,
)
from imswitch.imcontrol.model.measurement.instrument import (
    InstrumentDriver,
    InstrumentSession,
)
from imswitch.imcontrol.model.resources import (
    ReservationExpiredError,
    ResourceReservedError,
    get_resource_registry,
    instrument_key,
)


class InstrumentManager(DeviceManagerStatusMixin, ABC):
    """One instrument entry of the setup's ``instruments`` section."""

    def __init__(self, instrumentInfo, name: str, **lowLevelManagers) -> None:
        self.__logger = initLogger(self, instanceName=name)
        self.__name = name
        self._instrumentInfo = instrumentInfo
        properties = dict(getattr(instrumentInfo, 'managerProperties', None) or {})
        self.transient = bool(getattr(instrumentInfo, 'transient', False))
        connectOnStartup = bool(getattr(instrumentInfo, 'connectOnStartup', False))
        self._statusListeners: List[Callable[[str], None]] = []

        self.session = InstrumentSession(name, self._createDriver(properties))
        self.session.add_fault_listener(self._onSessionFault)
        self._lifecycle = _InstrumentLifecycle(self)

        if self.transient and not connectOnStartup:
            self._setAbsent('Not connected -- connect it in Hardware status')
            return
        try:
            self._connectSession()
        except Exception as exc:
            self.__logger.warning(f'Instrument {name} did not connect at startup: {exc}')
            if self.transient:
                self._deviceRuntimeMode = DeviceRuntimeMode.ABSENT
            self._setConnectionError(exc, summary='Instrument did not connect')

    # ------------------------------------------------------------- contract
    @abstractmethod
    def _createDriver(self, properties: Dict[str, Any]) -> InstrumentDriver:
        """The driver for this instrument (not connected yet)."""

    @property
    def name(self) -> str:
        return self.__name

    @property
    def connected(self) -> bool:
        return self.session.connected and not self.session.faulted

    # ------------------------------------------------------------ lifecycle
    def _connectSession(self) -> None:
        identity = self.session.connect()
        self._setConnected(f'{identity.model} {identity.serial} connected'.strip())
        self._notifyStatus()

    def _disconnectSession(self) -> None:
        try:
            self.session.close()
        finally:
            self._setAbsent('Disconnected')
            self._notifyStatus()

    def _onSessionFault(self, _name: str, cause: str) -> None:
        # Runs on the thread that hit the fault; status only, no Qt.
        self._setConnectionError(cause, summary='Instrument fault -- reconnect it')
        self._notifyStatus()

    def addStatusListener(self, callback: Callable[[str], None]) -> None:
        """``callback(instrument name)`` after a connect, disconnect or fault
        (on the thread where it happened)."""
        self._statusListeners.append(callback)

    def removeStatusListener(self, callback) -> None:
        try:
            self._statusListeners.remove(callback)
        except ValueError:
            pass

    def _notifyStatus(self) -> None:
        for callback in list(self._statusListeners):
            try:
                callback(self.__name)
            except Exception:
                self.__logger.exception('instrument status listener failed')

    def getDeviceLifecycle(self):
        return self._lifecycle

    def getDeviceDescriptorSpec(self):
        return DeviceDescriptorSpec(
            role=DeviceRole.PRIMARY,
            hardware_id=self._lifecycle.hardware_id,
            category='instrument',
            display_name=self.__name,
        )

    def finalize(self) -> None:
        try:
            if self.session.connected:
                self.session.close()
        finally:
            self._setFinalizedStatus()


class _InstrumentLifecycle:
    capabilities = DeviceLifecycleCapabilities(connect=True, disconnect=True, reconnect=True)

    def __init__(self, manager: InstrumentManager) -> None:
        self._manager = manager
        self.hardware_id = HardwareDeviceId('instrument', f'instrument:{manager.name}')

    def _result(self, action, success, summary, details=None):
        return DeviceLifecycleResult(
            hardware_id=self.hardware_id, action=action, success=success,
            summary=summary, details=details,
            affected_device_ids=(DeviceId('instrument', self._manager.name),),
        )

    def connect(self):
        manager = self._manager
        if manager.connected:
            return self._result(DeviceLifecycleAction.CONNECT, True, 'Already connected')
        with self._admittedCommand('connect'):
            try:
                manager._connectSession()
            except Exception as exc:
                manager._setConnectionError(exc, summary='Instrument did not connect')
                manager._notifyStatus()
                return self._result(DeviceLifecycleAction.CONNECT, False,
                                    'Instrument did not connect', str(exc))
        return self._result(DeviceLifecycleAction.CONNECT, True,
                            manager.connectionStatusSummary or 'Connected')

    def disconnect(self):
        manager = self._manager
        with self._admittedCommand('disconnect'):
            manager._disconnectSession()
        return self._result(DeviceLifecycleAction.DISCONNECT, True, 'Disconnected')

    def reconnect(self):
        manager = self._manager
        with self._admittedCommand('reconnect'):
            try:
                manager.session.close()
            except Exception:
                pass
            try:
                manager._connectSession()
            except Exception as exc:
                manager._setConnectionError(exc, summary='Instrument did not reconnect')
                manager._notifyStatus()
                return self._result(DeviceLifecycleAction.RECONNECT, False,
                                    'Instrument did not reconnect', str(exc))
        return self._result(DeviceLifecycleAction.RECONNECT, True,
                            manager.connectionStatusSummary or 'Reconnected')

    def _admittedCommand(self, label):
        return _AdmittedCommand(self, label)

    def probe(self):
        raise DeviceLifecycleNotSupportedError('Instruments do not support probe.')

    def shutdown(self):
        raise DeviceLifecycleNotSupportedError('Instruments shut down with ImSwitch.')


class _AdmittedCommand:
    """``with``: one admission ticket for a lifecycle transition. Refused
    (as a blocked lifecycle operation) while a run or script holds the
    instrument or another command on it is still running."""

    def __init__(self, lifecycle: _InstrumentLifecycle, label: str) -> None:
        self._key = instrument_key(lifecycle._manager.name)
        self._label = f'{lifecycle._manager.name}: {label}'
        self._ticket = None

    def __enter__(self):
        registry = get_resource_registry()
        try:
            self._ticket = registry.admit(self._key, None, label=self._label)
        except (ResourceReservedError, ReservationExpiredError) as exc:
            raise DeviceLifecycleBlockedError(str(exc)) from None
        return self._ticket

    def __exit__(self, *exc):
        get_resource_registry().release_ticket(self._ticket)
        return False
