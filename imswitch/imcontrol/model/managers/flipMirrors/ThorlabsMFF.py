import warnings

from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.devices import (
    DeviceId,
    DeviceLifecycleAction,
    DeviceLifecycleCapabilities,
    DeviceLifecycleNotSupportedError,
    DeviceLifecycleResult,
    HardwareDeviceId,
)


class _ThorlabsMFFLifecycle:
    """Lifecycle adapter for one physical Thorlabs MFF flip mirror."""

    capabilities = DeviceLifecycleCapabilities(reconnect=True)

    def __init__(self, manager):
        self._manager = manager
        self._hardware_id = HardwareDeviceId(
            category="flip_mirror", key=f"flip_mirror:{manager.name}"
        )

    @property
    def hardware_id(self):
        return self._hardware_id

    def _unsupported(self, action):
        raise DeviceLifecycleNotSupportedError(
            f"Thorlabs MFF lifecycle does not yet support {action.value}."
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
        device_id = DeviceId("flip_mirror", manager.name)

        manager.close()
        manager._connect()
        if not manager.is_connected():
            return DeviceLifecycleResult(
                hardware_id=self.hardware_id,
                action=DeviceLifecycleAction.RECONNECT,
                success=False,
                summary=f"Flip mirror {manager.name} reconnect failed",
                details=manager.get_last_error(),
                affected_device_ids=(device_id,),
            )

        try:
            state = manager.get_state()
        except Exception as exc:
            # Opening the vendor handle is not sufficient proof that this
            # particular device is usable. Treat a failed state query as a
            # failed reconnect and leave the manager disconnected.
            manager.close()
            manager._last_error = str(exc)
            return DeviceLifecycleResult(
                hardware_id=self.hardware_id,
                action=DeviceLifecycleAction.RECONNECT,
                success=False,
                summary=f"Flip mirror {manager.name} reconnect verification failed",
                details=str(exc),
                affected_device_ids=(device_id,),
            )

        return DeviceLifecycleResult(
            hardware_id=self.hardware_id,
            action=DeviceLifecycleAction.RECONNECT,
            success=True,
            summary=f"Flip mirror {manager.name} reconnected",
            details=f"Current physical state: {state}",
            affected_device_ids=(device_id,),
        )


class ThorlabsMFFManager:
    """Thorlabs MFF101/MFF102 flip mirror manager using pylablib.

    pylablib's MFF support is serial-number based. If a setup needs legacy
    COM-port selection, add an explicit APT backend that honors ``serial_port``
    instead of passing COM-port names into ``Thorlabs.MFF``.
    """

    def __init__(self, deviceInfo, name, **lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)

        self.name = name
        self.deviceInfo = deviceInfo
        self.serial_number = self._read_info("serial_number")
        self.invert = bool(self._read_info("invert", False))
        self.initial_state = self._read_info("initial_state", None)
        self.state_names = self._normalize_state_names(
            self._read_info("state_names", None)
        )
        self._device = None
        self._connected = False
        self._last_error = None
        self._last_state = None
        self._lifecycle = _ThorlabsMFFLifecycle(self)

        self._connect()

        if self.initial_state is not None and self.is_connected():
            self.move_to(int(self.initial_state))

    def _read_info(self, key, default=None):
        if hasattr(self.deviceInfo, key):
            value = getattr(self.deviceInfo, key)
            if value is not None:
                return value

        manager_properties = getattr(self.deviceInfo, "managerProperties", None) or {}
        return manager_properties.get(key, default)

    def _connect(self):
        self._last_error = None

        if not self.serial_number:
            self._connected = False
            self._last_error = "Missing serial_number"
            self.__logger.error(f"{self.name}: missing serial_number")
            return

        try:
            from pylablib.devices import Thorlabs

            # pylablib can warn if the reported model does not match the serial prefix.
            # For MFF002/MFF10x this can be harmless if move/get_state work.
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r"model number .* doesn't match the device ID prefix.*",
                    category=UserWarning,
                )
                self._device = Thorlabs.MFF(str(self.serial_number))

            self._connected = True
            self.__logger.info(
                f"{self.name}: connected to Thorlabs MFF serial {self.serial_number}"
            )

        except Exception as e:
            self._device = None
            self._connected = False
            self._last_error = str(e)
            self.__logger.error(
                f"{self.name}: failed to connect to Thorlabs MFF "
                f"serial {self.serial_number}: {e}"
            )

    def _normalize_state_names(self, state_names):
        default = {0: "0", 1: "1"}

        if not state_names:
            return default

        return {
            0: str(state_names.get("0", state_names.get(0, "0"))),
            1: str(state_names.get("1", state_names.get(1, "1"))),
        }

    def getDeviceLifecycle(self):
        return self._lifecycle

    def is_connected(self):
        return self._connected and self._device is not None

    def get_last_error(self):
        return self._last_error

    def get_cached_state(self):
        """Return the last successfully observed/commanded logical state."""
        return self._last_state

    def _to_hw_state(self, state):
        state = int(state)
        if state not in (0, 1):
            raise ValueError("Flip mirror state must be 0 or 1")
        return 1 - state if self.invert else state

    def _from_hw_state(self, state):
        state = int(state)
        return 1 - state if self.invert else state

    def move_to(self, state):
        if not self.is_connected():
            raise RuntimeError(f"{self.name}: flip mirror is not connected")

        state = int(state)
        hw_state = self._to_hw_state(state)
        self._device.move_to_state(hw_state)
        self._last_state = state

    def get_state(self):
        if not self.is_connected():
            raise RuntimeError(f"{self.name}: flip mirror is not connected")

        state = self._from_hw_state(self._device.get_state())
        self._last_state = state
        return state

    def get_state_names(self):
        return dict(self.state_names)

    def close(self):
        if self._device is not None:
            try:
                self._device.close()
            except Exception as e:
                self.__logger.error(f"{self.name}: error while closing: {e}")

        self._device = None
        self._connected = False

    def finalize(self):
        self.close()


# Backward-compatible setup managerName used by existing setup files.
ThorlabsMFF = ThorlabsMFFManager
