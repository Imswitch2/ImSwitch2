"""The plumbing every bridged Micro-Manager device manager shares.

The camera, stage, light and flip-mirror managers all do the same things around
their own device logic (plan ``micromanager-bridge.md`` sections 4.2 and 4.7):

- find the hub in ``lowLevelManagers['rs232sManager']`` by
  ``managerProperties.rs232device``;
- parse *inline mode* (``adapter`` / ``device`` / ``parent`` /
  ``preInitProperties``), load the device, and unload it again on failure or
  finalize;
- resolve the ``label`` (a loaded device of the right type, with a "did you
  mean" message when it is not);
- build the backend holder (:meth:`~DeviceManagerStatusMixin._installBackend`)
  so a hub that is down at startup, or a ``transient`` device, leaves the
  manager absent instead of aborting the app;
- carry the hub's failure as the manager's own status;
- after the hub reloaded the core (``_onTransportReconnected``): reopen the
  backend, adopt the device again, report;
- wait for a device to go idle (:meth:`~MicroManagerDeviceMixin._wait_until_idle`),
  polling ``deviceBusy`` outside the hub lock; stage and flip-mirror moves use it.

:class:`MicroManagerDeviceMixin` holds all of that once. A manager keeps only
what is its own: the device-type rule, what it reads at adoption
(:meth:`~MicroManagerDeviceMixin._reinitialise`) and its safe state.

Why a mixin and not a base class. The managers derive from ``DetectorManager``,
``PositionerManager`` and ``LaserManager`` (the flip mirror has no ImSwitch2 base
class and takes :class:`DeviceManagerStatusMixin` directly), which are unrelated
classes; the three already inherit :class:`DeviceManagerStatusMixin`,
and the mixin only *uses* that class's methods (``_installBackend``,
``_setConnected`` ...) through ``self``. It has no ``__init__`` and is listed
first in the bases, so it never takes part in a constructor chain and the
``DeviceManagerStatusMixin`` MRO is untouched (``DetectorManager`` is a
``QObject``: a base with its own ``__init__`` would have to cooperate with
``SignalInterface``).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple

from imswitch.imcontrol.model.devices.graph import rs232BackedPrimarySpec
from imswitch.imcontrol.model.devices.status import (
    DeviceFailureKind, DeviceNotConnectedError, DeviceRuntimeMode, backend_attribute,
)

from ._core import MMCoreHostError
from .core import MicroManagerCoreManager

#: How often :meth:`MicroManagerDeviceMixin._wait_until_idle` asks the device whether it is still
#: busy. MMCore's own ``waitForDevice`` polls ``Busy()`` every 10 ms (``CMMCore::pollingIntervalMs_``);
#: the same cadence here, but between polls the hub lock is RELEASED, so a move that takes seconds
#: does not stall the camera drain thread (plan section 4.5: a stalled drain overflows the circular
#: buffer and fails the recording).
BUSY_POLL_S = 0.010


@dataclass
class _Binding:
    """What the backend holder keeps as "the connected backend": the device's label on the host."""

    label: str
    host: Any


class MicroManagerDeviceMixin:
    """Hub, inline-mode, label and reconnect plumbing of a bridged device manager.

    A manager sets the class attributes below, calls :meth:`_bridge_identify`,
    :meth:`_bridge_attach` and :meth:`_bridge_install_backend` in its constructor
    (in that order, the device-specific checks of its own in between where it
    needs them), and implements :meth:`_reinitialise`.

    Class attributes to set:

    - ``BRIDGE_MANAGER_ID`` -- the manifest id, for "needs managerProperties.x" messages
    - ``BRIDGE_CATEGORY`` -- ``'detector'``, ``'positioner'``, ``'laser'`` or ``'flip_mirror'``: the noun
      of every setup-file error and the device-graph category
    - ``BRIDGE_DEVICE_NOUN`` -- what the status line and logs call the device
      (``'camera'``, ``'stage'`` ...)
    - ``BRIDGE_DEVICE_TYPES`` -- names of the :class:`pymmcore_plus.DeviceType` members
      the label may have; the default :meth:`_expected_device_types` reads them
    - ``BRIDGE_TYPE_NOUN`` -- the type's name in the "Micro-Manager has no <noun> device"
      message
    """

    BRIDGE_MANAGER_ID: str = ''
    BRIDGE_CATEGORY: str = ''
    BRIDGE_DEVICE_NOUN: str = 'device'
    BRIDGE_DEVICE_TYPES: Tuple[str, ...] = ()
    BRIDGE_TYPE_NOUN: str = 'device'

    #: The backend holder's current backend: the :class:`_Binding`, or the absent stand-in
    #: that raises ``DeviceNotConnectedError`` on any use.
    _binding = backend_attribute()

    # ------------------------------------------------------------------ setup
    @classmethod
    def _required_text(cls, properties, key) -> str:
        value = properties.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{cls.BRIDGE_MANAGER_ID} needs managerProperties.{key} (a non-empty string)")
        return value

    def _bridge_identify(self, properties, name: str, logger: logging.Logger) -> None:
        """Store what names this device: the properties, its setup name and its label."""
        self._bridge_logger = logger
        self._props = properties
        self._setupName = name
        self._label = self._required_text(properties, 'label')
        self._idle_poll = threading.Event()          # never set: a sleep that other code could wake

    def _bridge_attach(self, properties, lowLevelManagers) -> None:
        """Find the hub and parse inline mode."""
        self._hub = self._find_hub(properties, lowLevelManagers)
        self._host = self._hub.host
        self._inline = self._parse_inline(properties)
        self._inline_loaded = False

    def _bridge_install_backend(self, info) -> None:
        """Give the manager the standard status + reconnect lifecycle (a backend holder).

        ``open_real`` only binds to the hub; adoption (label, geometry, ...) follows in the
        manager. A hub that is not connected, or a transient device, leaves the manager
        absent instead of raising.
        """
        # Before the status mixin ran, its getattr default would raise for a QObject
        # (DetectorManager is one): give the holder's failure path a value to find.
        self.__dict__['_deviceRuntimeMode'] = DeviceRuntimeMode.REAL
        # ``FlipMirrorInfo`` is not a ``DeviceInfo``: it has neither flag. A device without them is
        # not transient; the defaults are ``DeviceInfo``'s own.
        self._installBackend(
            self._open_binding, label=f'Micro-Manager {self.BRIDGE_DEVICE_NOUN} {self._label}',
            transient=bool(getattr(info, 'transient', False)),
            connect_on_startup=bool(getattr(info, 'connectOnStartup', False)),
            close=lambda binding: None)

    def _bridge_adopt(self, adopt: Callable[[], Any]) -> Any:
        """``adopt()`` when the hub is there; else carry the hub's failure as status.

        A transient device that was left absent on purpose keeps the status the holder gave it.
        Returns what ``adopt`` returned, or ``None``.
        """
        if self.backendIsReal:
            return adopt()
        if self.runtimeMode is not DeviceRuntimeMode.ABSENT:
            self._mirror_hub_status()
        return None

    def _find_hub(self, properties, lowLevelManagers) -> MicroManagerCoreManager:
        hub_name = self._required_text(properties, 'rs232device')
        hubs = lowLevelManagers.get('rs232sManager')
        noun = self.BRIDGE_CATEGORY
        try:
            hub = hubs[hub_name]
        except (KeyError, TypeError, IndexError):
            raise ValueError(
                f"{noun} {self._setupName!r}: managerProperties.rs232device is {hub_name!r}, but no "
                f"rs232devices entry of that name exists; declare the Micro-Manager hub there "
                f"(managerName 'micromanager.core')") from None
        if not isinstance(hub, MicroManagerCoreManager):
            raise ValueError(
                f"{noun} {self._setupName!r}: rs232devices[{hub_name!r}] is a {type(hub).__name__}, not a "
                f"Micro-Manager hub; managerProperties.rs232device must name an entry with "
                f"managerName 'micromanager.core'")
        return hub

    def _parse_inline(self, properties):
        """``(adapter, device, parent, preInit)`` in inline mode, else ``None``."""
        adapter, device = properties.get('adapter'), properties.get('device')
        if bool(adapter) != bool(device):
            raise ValueError(
                f"{self.BRIDGE_CATEGORY} {self._setupName!r}: inline mode needs both managerProperties.adapter and "
                f"managerProperties.device (got adapter={adapter!r}, device={device!r})")
        if not adapter:
            return None
        return adapter, device, properties.get('parent') or None, dict(properties.get('preInitProperties') or {})

    def _open_binding(self) -> _Binding:
        """``open_real`` of the backend holder: the hub must be connected."""
        if not self._hub.isConnected:
            details = self._hub.connectionStatusDetails or self._hub.connectionStatusSummary
            raise DeviceNotConnectedError(
                f"Micro-Manager core {self._hub.name!r} is not connected"
                + (f": {details}" if details else ''))
        return _Binding(self._label, self._host)

    def _mirror_hub_status(self) -> None:
        """Carry the hub's failure (kind and message) as this device's status."""
        hub = self._hub
        details = hub.connectionStatusDetails or hub.connectionStatusSummary or 'not connected'
        summary = f'{self._label}: Micro-Manager core {hub.name} is not connected'
        if hub.connectionFailureKind is DeviceFailureKind.DEVICE_NOT_FOUND:
            self._setDeviceNotFound(details, summary=summary)
        else:
            self._setConnectionError(details, summary=summary)

    # ------------------------------------------------------------------ label
    def _expected_device_types(self) -> Tuple[Any, ...]:
        """The :class:`pymmcore_plus.DeviceType` members the label may have."""
        from pymmcore_plus import DeviceType

        return tuple(getattr(DeviceType, name) for name in self.BRIDGE_DEVICE_TYPES)

    def _expected_type_text(self) -> str:
        """The expected type(s) as the end of "... is a X, not <this>"."""
        return 'a ' + ' or '.join(self.BRIDGE_DEVICE_TYPES)

    def _missing_message(self, label: str) -> str:
        """The "did you mean" error for a label the core has no suitable device under."""
        return self._host.missing_device_message(label, self._expected_device_types()[0], self.BRIDGE_TYPE_NOUN)

    def _ensure_loaded(self) -> None:
        """Make sure ``label`` is loaded: inline mode loads it, system-config mode must find it.

        Hub lock taken here (it is reentrant). Refuses an inline device whose label the hub's
        ``.cfg`` already loaded: loading it a second time would replace the configured one.
        """
        host, label = self._host, self._label
        noun = self.BRIDGE_CATEGORY
        with host.lock:
            loaded = host.has_device(label)
            if loaded and self._inline is not None and not self._inline_loaded:
                raise ValueError(
                    f"{noun} {self._setupName!r}: Micro-Manager device {label!r} is already loaded by the "
                    f"hub's configuration, so managerProperties.adapter/device would load it a second "
                    f"time; remove adapter/device (system-config mode) or use another label")
            if loaded:
                return
            if self._inline is None:
                raise ValueError(
                    f"{noun} {self._setupName!r} (managerProperties.label): "
                    + self._missing_message(label) + "; or give adapter and device to load it inline")
            adapter, device, parent, pre_init = self._inline
            host.load_device(label, adapter, device, parent, pre_init)
            self._inline_loaded = True

    def _expect_device_type(self, types: Optional[Tuple[Any, ...]] = None, type_text: Optional[str] = None):
        """The loaded device's type, which must be one of ``types``; a ``ValueError`` naming it otherwise."""
        types = self._expected_device_types() if types is None else types
        device_type = self._host.device_type(self._label)
        if device_type not in types:
            raise ValueError(
                f"{self.BRIDGE_CATEGORY} {self._setupName!r}: Micro-Manager device {self._label!r} is a "
                f"{device_type.name.removesuffix('Device')}, not {type_text or self._expected_type_text()}")
        return device_type

    def _resolve_label(self):
        """Make sure the label is a loaded device of the right type (inline mode loads it)."""
        with self._host.lock:
            self._ensure_loaded()
            return self._expect_device_type()

    def _unload_inline(self) -> None:
        if not self._inline_loaded:
            return
        self._inline_loaded = False
        try:
            if self._host.is_connected and self._host.has_device(self._label):
                self._host.unload_device(self._label)
        except Exception as exc:                      # noqa: BLE001 - the caller is already failing or finalizing
            self._bridge_logger.warning('Unloading inline %s %s failed: %s', self.BRIDGE_DEVICE_NOUN, self._label, exc)

    # ---------------------------------------------------------------- access
    @property
    def label(self) -> str:
        """The Micro-Manager device label."""
        return self._label

    @property
    def hub(self) -> MicroManagerCoreManager:
        """The ``micromanager.core`` hub this device is bound to."""
        return self._hub

    def _use(self, what: str):
        """``(host, label)`` of the connected device; ``DeviceNotConnectedError`` when it is not there."""
        self._requireConnected(what)
        binding = self._binding                       # the absent stand-in raises on any attribute
        host = binding.host
        if not host.is_connected:
            raise DeviceNotConnectedError(
                f"{self.name}: {what} refused, Micro-Manager core {self._hub.name!r} is not connected")
        return host, binding.label

    def _wait_until_idle(self, host, label: str) -> None:
        """Block until the device reports not busy, polling OUTSIDE the hub lock.

        The deadline is MMCore's own device timeout (``getTimeoutMs``, the value ``waitForDevice``
        enforces too), so a stuck device fails the same way it would in Micro-Manager.
        """
        with host.lock:
            timeout_s = float(host.core.getTimeoutMs()) / 1000.0
        deadline = time.monotonic() + timeout_s
        while True:
            with host.lock:
                busy = host.core.deviceBusy(label)
            if not busy:
                return
            if time.monotonic() >= deadline:
                raise MMCoreHostError(
                    f"{self.BRIDGE_CATEGORY} {self._setupName!r}: device {label!r} still busy after "
                    f"{timeout_s:.1f} s (MMCore device timeout, core.setTimeoutMs)")
            self._idle_poll.wait(BUSY_POLL_S)

    def getDeviceDescriptorSpec(self):
        """Graph descriptor: a primary device that depends on its hub through ``rs232device``."""
        return rs232BackedPrimarySpec(
            category=self.BRIDGE_CATEGORY, rs232_name=str(self._props['rs232device']),
            display_name=f'{self._setupName} (Micro-Manager {self._label})')

    # --------------------------------------------------------------- lifecycle
    def _reinitialise(self) -> List[str]:
        """Put the device in the state this manager reports; the errors found. Each manager's own."""
        raise NotImplementedError

    def _reconnected_summary(self) -> str:
        """The status line after a successful transport reconnect."""
        return f'{self._label} reconnected'

    def _onTransportReconnected(self, real: bool):
        """After the hub reloaded the core: reopen the backend, adopt the device again; the errors found.

        The reload unloaded every device, an inline one included, so the "I loaded it" flag is
        reset first: left set, it would hide a ``.cfg`` that now loads the same label (the
        "already loaded by the hub's configuration" refusal is skipped for a device this manager
        thinks it loaded) and a re-adoption that fails half-way would not unload what it loaded.
        """
        self._inline_loaded = False
        if not real:
            details = self._hub.connectionStatusDetails or 'transport unavailable'
            self._setConnectionError(details, summary=f'{self._label}: Micro-Manager core did not reconnect')
            return [details]
        errors: List[str] = []
        try:
            self.backendHolder.reopen()
            if not self.backendIsReal:
                raise DeviceNotConnectedError(self.connectionStatusDetails or 'the hub is not connected')
            errors.extend(self._reinitialise())
        except Exception as exc:                          # noqa: BLE001 - reported to the lifecycle
            errors.append(f'{type(exc).__name__}: {exc}')
        if errors:
            self._setConnectionError('; '.join(errors), summary=f'{self._label}: reconnected but not usable')
            return errors
        self._setConnected(self._reconnected_summary())
        return errors

    def _lifecycleReinitialise(self) -> None:
        errors = self._reinitialise()
        if errors:
            raise RuntimeError('; '.join(errors))
