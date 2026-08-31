import threading
from weakref import WeakKeyDictionary, WeakSet

from imswitch.imcommon.model import initLogger
from .LaserManager import LaserManager
from imswitch.imcontrol.model.devices.graph import (
    HardwareDeviceId, sharedRs232ComponentSpec,
)
from imswitch.imcontrol.model.devices.lifecycle import (
    DeviceLifecycleAction,
    DeviceLifecycleCapabilities,
    DeviceLifecycleNotSupportedError,
    DeviceLifecycleResult,
)
from imswitch.imcontrol.model.devices.status import (
    DeviceConnectionState, DeviceFailureKind, DeviceId, DeviceRuntimeMode,
)


# One physical CoolLED controller can be represented by several laser-channel
# managers. Probe and lifecycle state are both shared per RS232 manager.
_COOLLED_PROBE_CACHE = WeakKeyDictionary()
_COOLLED_LIFECYCLE_CACHE = WeakKeyDictionary()


def _probe_coolled_controller(rs232manager, *, force=False):
    if force:
        _COOLLED_PROBE_CACHE.pop(rs232manager, None)

    cached = _COOLLED_PROBE_CACHE.get(rs232manager)
    if cached is not None:
        return cached

    mode = getattr(rs232manager, "runtimeMode", DeviceRuntimeMode.REAL)
    transport_state = getattr(
        rs232manager, "connectionState", DeviceConnectionState.UNKNOWN
    )
    if mode is DeviceRuntimeMode.MOCK or transport_state in {
        DeviceConnectionState.ERROR, DeviceConnectionState.DISCONNECTED
    }:
        result = (
            DeviceConnectionState.ERROR,
            mode,
            "CoolLED transport unavailable",
            getattr(rs232manager, "connectionStatusDetails", None),
            getattr(
                rs232manager, "connectionFailureKind", DeviceFailureKind.CONNECTION_ERROR
            ) or DeviceFailureKind.CONNECTION_ERROR,
        )
    else:
        try:
            reply = rs232manager.query("CSS?")
            if reply is None or not str(reply).strip():
                raise RuntimeError("CoolLED CSS? returned no response")
            result = (
                DeviceConnectionState.CONNECTED,
                DeviceRuntimeMode.REAL,
                "CoolLED controller responded to CSS?",
                None,
                None,
            )
        except Exception as exc:
            result = (
                DeviceConnectionState.ERROR,
                DeviceRuntimeMode.REAL,
                "CoolLED controller did not respond to CSS?",
                str(exc),
                DeviceFailureKind.CONNECTION_ERROR,
            )

    _COOLLED_PROBE_CACHE[rs232manager] = result
    return result


class _CoolLEDLifecycle:
    """Physical-device lifecycle for all channels on one CoolLED controller."""

    capabilities = DeviceLifecycleCapabilities(probe=True, reconnect=True)

    def __init__(self, rs232manager, rs232_name: str):
        self._rs232manager = rs232manager
        self._rs232_name = str(rs232_name)
        self._channels = WeakSet()
        self._lock = threading.RLock()
        self._hardware_id = HardwareDeviceId(
            category="laser", key=f"coolled:{self._rs232_name}"
        )

    @property
    def hardware_id(self):
        return self._hardware_id

    def registerChannel(self, manager) -> None:
        self._channels.add(manager)

    def _channelManagers(self):
        return tuple(sorted(self._channels, key=lambda manager: manager.name.casefold()))

    def _channelIds(self):
        return tuple(
            DeviceId("laser", manager.name) for manager in self._channelManagers()
        )

    def _unsupported(self, action: DeviceLifecycleAction):
        raise DeviceLifecycleNotSupportedError(
            f"CoolLED lifecycle does not yet support {action.value}."
        )

    def connect(self):
        return self._unsupported(DeviceLifecycleAction.CONNECT)

    def disconnect(self):
        return self._unsupported(DeviceLifecycleAction.DISCONNECT)

    def shutdown(self):
        return self._unsupported(DeviceLifecycleAction.SHUTDOWN)

    def _applyProbeToChannels(self, probe_result) -> None:
        for manager in self._channelManagers():
            manager._applyControllerProbe(probe_result)

    def probe(self):
        with self._lock:
            result = _probe_coolled_controller(
                self._rs232manager, force=True
            )
            self._applyProbeToChannels(result)
            state, _, summary, details, _ = result
            return DeviceLifecycleResult(
                hardware_id=self.hardware_id,
                action=DeviceLifecycleAction.PROBE,
                success=state is DeviceConnectionState.CONNECTED,
                summary=summary,
                details=details,
                affected_device_ids=self._channelIds(),
            )

    def reconnect(self):
        with self._lock:
            channels = self._channelManagers()
            channel_ids = self._channelIds()

            # Best effort before touching the transport. A broken connection
            # may make this fail, but reconnect must still proceed. The same
            # channels are forced OFF again after a verified reconnect.
            for manager in channels:
                if manager._isMock:
                    continue
                try:
                    manager.setEnabled(False)
                except Exception:
                    pass

            real_transport = self._rs232manager.reconnectTransport()
            probe_result = _probe_coolled_controller(
                self._rs232manager, force=True
            )
            self._applyProbeToChannels(probe_result)
            state, _, summary, details, _ = probe_result

            if not real_transport or state is not DeviceConnectionState.CONNECTED:
                return DeviceLifecycleResult(
                    hardware_id=self.hardware_id,
                    action=DeviceLifecycleAction.RECONNECT,
                    success=False,
                    summary=summary,
                    details=details,
                    affected_device_ids=channel_ids,
                    deactivated_device_ids=channel_ids,
                )

            safe_off_errors = []
            for manager in channels:
                try:
                    manager.setEnabled(False)
                except Exception as exc:
                    safe_off_errors.append(f"{manager.name}: {exc}")

            if safe_off_errors:
                error_details = "; ".join(safe_off_errors)
                for manager in channels:
                    manager._setConnectionError(
                        error_details,
                        summary="CoolLED reconnected but safe OFF initialization failed",
                    )
                return DeviceLifecycleResult(
                    hardware_id=self.hardware_id,
                    action=DeviceLifecycleAction.RECONNECT,
                    success=False,
                    summary="CoolLED reconnected but safe OFF initialization failed",
                    details=error_details,
                    affected_device_ids=channel_ids,
                    deactivated_device_ids=channel_ids,
                )

            for manager in channels:
                manager._setConnected("CoolLED reconnected; all channels forced OFF")
            return DeviceLifecycleResult(
                hardware_id=self.hardware_id,
                action=DeviceLifecycleAction.RECONNECT,
                success=True,
                summary="CoolLED reconnected; all channels forced OFF",
                affected_device_ids=channel_ids,
                deactivated_device_ids=channel_ids,
            )


class CoolLEDLaserManager(LaserManager):
    """LaserManager for controlling one LED channel on a CoolLED controller.

    Manager properties:

    - ``rs232device`` -- configured RS232 communication resource
    - ``channel_index`` -- laser channel (A to H)
    """

    def __init__(self, laserInfo, name, **lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)
        self._rs232manager = None

        try:
            self._rs232manager = lowLevelManagers['rs232sManager'][
                laserInfo.managerProperties['rs232device']
            ]
            self.__channel_index = laserInfo.managerProperties['channel_index']
            self.__digital_mod = False
        except Exception as exc:
            self.__logger.warning(
                f'Failed to initialize CoolLED hardware, running in mock mode: {exc}'
            )
            self._rs232manager = None
            self._setConnectionError(
                exc,
                summary="CoolLED initialization failed; mock fallback active",
                mock_active=True,
            )
            self.__channel_index = laserInfo.managerProperties.get('channel_index', 'A')
            self.__digital_mod = False

        isModulated = (
            laserInfo.freqRangeMin is not None
            and laserInfo.freqRangeMax is not None
            and laserInfo.freqRangeInit is not None
        )

        super().__init__(
            laserInfo,
            name,
            isBinary=False,
            valueUnits='mW',
            valueDecimals=0,
            isModulated=isModulated,
        )

        if self._rs232manager is not None:
            self._applyControllerProbe(
                _probe_coolled_controller(self._rs232manager)
            )

    @property
    def _isMock(self):
        """Dynamic transport mode; reconnect may replace mock with real hardware."""
        if self._rs232manager is None:
            return True
        return self._rs232manager.runtimeMode is DeviceRuntimeMode.MOCK

    def _applyControllerProbe(self, result) -> None:
        state, mode, summary, details, failure_kind = result
        self._deviceRuntimeMode = mode
        self._setConnectionState(
            state,
            summary=summary,
            details=details,
            failure_kind=failure_kind,
        )

    def getDeviceDescriptorSpec(self):
        rs232_name = self.getProperty('rs232device')
        return sharedRs232ComponentSpec(
            category='laser',
            family='coolled',
            display_name='CoolLED controller',
            rs232_name=str(rs232_name),
        )

    def getDeviceLifecycle(self):
        """Return the shared physical lifecycle without performing hardware I/O."""
        if self._rs232manager is None:
            return None
        lifecycle = _COOLLED_LIFECYCLE_CACHE.get(self._rs232manager)
        if lifecycle is None:
            lifecycle = _CoolLEDLifecycle(
                self._rs232manager, self.getProperty('rs232device')
            )
            _COOLLED_LIFECYCLE_CACHE[self._rs232manager] = lifecycle
        lifecycle.registerChannel(self)
        return lifecycle

    def setEnabled(self, enabled):
        """Turn on (N) or off (F) laser emission."""
        if self._isMock:
            self.__logger.debug(f'Mock mode: setEnabled({enabled}) ignored')
            return

        value = "N" if enabled else "F"
        cmd = "C" + self.__channel_index + value
        self._rs232manager.query(cmd)

    def setValue(self, power, enabled=True, for_scanning=False):
        """Set the channel intensity."""
        if self._isMock:
            self.__logger.debug(f'Mock mode: setValue({power}) ignored')
            return

        cmd = "C" + self.__channel_index + "IX" + "{0:03.0f}".format(power)
        self.__logger.debug(cmd)
        self._rs232manager.query(cmd)


# Copyright (C) 2020-2021 ImSwitch developers
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
