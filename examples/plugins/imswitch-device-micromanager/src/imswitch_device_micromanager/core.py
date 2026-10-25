"""MicroManagerCoreManager: the shared MMCore hub, an ``rs232``-kind contribution.

Every bridged Micro-Manager device needs the same ``CMMCorePlus``. The hub sits
in the ``rs232devices`` slot of the setup file, which ImSwitch2 already treats
as "shared communication resource referenced by name" (config editor, device
graph, transport reconnect), and owns the :class:`~._core.MMCoreHost`.

Plan ``micromanager-bridge.md`` section 4.2: the hub **never lets a startup
failure escape**. ``RS232Info`` has no ``transient`` flag and ``MultiManager``
has no ``try``/``except``, so a raising hub would abort the whole
``MasterController``. A missing install, missing adapters or a bad ``.cfg``
becomes ERROR status carrying the Micro-Manager message; the manager object
stays alive with an unconnected host, and every dependent device then fails on
its own with "hub is not connected".
"""
from __future__ import annotations

from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.devices.graph import DeviceDescriptorSpec, DeviceRole
from imswitch.imcontrol.model.devices.status import DeviceManagerStatusMixin

from ._core import (
    MMAdaptersNotFoundError,
    MMConfigFileNotFoundError,
    MMCoreHost,
)


class MicroManagerCoreManager(DeviceManagerStatusMixin):
    """Hub holding the one Micro-Manager core of a setup.

    Manager properties (all optional, see ``schemas/micromanager_core.schema.json``):

    - ``configFile``: Micro-Manager ``.cfg`` to load (system-config mode)
    - ``adapterPaths``: adapter folders (else ``MICROMANAGER_PATH``, else pymmcore-plus' search)
    - ``circularBufferMB``: MMCore circular buffer size (else MMCore's default)
    - ``logDir``: folder for MMCore's own log file
    - ``adoptExistingCore``: reuse the process-wide ``CMMCorePlus``
    """

    def __init__(self, rs232Info, name, **_lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)
        self._name = name
        properties = dict(rs232Info.managerProperties or {})
        self._host = MMCoreHost(
            name,
            config_file=properties.get('configFile'),
            adapter_paths=tuple(properties.get('adapterPaths') or ()),
            circular_buffer_mb=properties.get('circularBufferMB'),
            log_dir=properties.get('logDir'),
            adopt_existing_core=bool(properties.get('adoptExistingCore', False)),
            logger=self.__logger,
        )
        self._connect(context='startup')

    # ------------------------------------------------------------------ access
    @property
    def name(self) -> str:
        """The hub's key in ``rs232devices``."""
        return self._name

    @property
    def host(self) -> MMCoreHost:
        """The host; dependents check :attr:`isConnected` before using it."""
        return self._host

    @property
    def isConnected(self) -> bool:
        """True while the core is loaded."""
        return self._host.is_connected

    # --------------------------------------------------------------- connection
    def _connect(self, *, context: str) -> bool:
        """Connect the host and record the outcome as status; never raises."""
        verb = 'started' if context == 'startup' else 'reconnected'
        try:
            self._host.connect()
        except Exception as exc:                      # noqa: BLE001 - recorded as status, see module docstring
            cause = exc.__cause__ if isinstance(exc.__cause__, ImportError) else exc
            self.__logger.error('Micro-Manager core %s failed to %s: %s', self._name,
                                'start' if context == 'startup' else 'reconnect', exc)
            summary = f'Micro-Manager core failed to {"start" if context == "startup" else "reconnect"}'
            if isinstance(exc, (MMAdaptersNotFoundError, MMConfigFileNotFoundError)):
                self._setDeviceNotFound(exc, summary=summary)
            else:
                self._setConnectionError(cause, summary=summary)
            return False
        self._setConnected(
            f'Micro-Manager core {verb} ({self._host.version_info}; '
            f'adapters from {self._host.adapter_source or "the adopted core"})')
        return True

    def disconnectTransport(self) -> None:
        """Unload every device and release the core without finalizing the manager."""
        self._host.shutdown()
        self._setDisconnected(f'Micro-Manager core {self._name} disconnected')

    def reconnectTransport(self) -> bool:
        """Unload everything and load the core and its ``.cfg`` again.

        True when the core is connected afterwards; on failure the status holds
        the Micro-Manager message and False is returned.
        """
        try:
            self._host.shutdown()
        except Exception as exc:                      # noqa: BLE001 - the host is unconnected after shutdown either way
            self.__logger.warning('Shutting down the Micro-Manager core before reconnecting failed: %s', exc)
        connected = self._connect(context='reconnect')
        if connected:
            # The dependents re-initialise one after the other: the cameras resume together once the
            # last has re-verified its format (MMCoreHost.begin_reconnect_round).
            self._host.begin_reconnect_round(self._host.registered_cameras())
        return connected

    def finalize(self) -> None:
        """Unload every device and release the core; the dependents are finalized before the hub."""
        self._host.shutdown()
        self._setFinalizedStatus()

    def getDeviceDescriptorSpec(self):
        """Graph descriptor: a resource, not a microscope device."""
        # The hub is communication infrastructure, not a microscope device.
        return DeviceDescriptorSpec(role=DeviceRole.RESOURCE)
