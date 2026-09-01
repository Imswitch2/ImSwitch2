from __future__ import annotations

from .graph import HardwareDeviceId
from .lifecycle import (
    DeviceLifecycleAction,
    DeviceLifecycleCapabilities,
    DeviceLifecycleNotSupportedError,
    DeviceLifecycleResult,
)
from .status import DeviceId


class SLMSessionLifecycle:
    """ImSwitch lifecycle adapter around one slmcore application session."""

    def __init__(self, *, slm_key: str, session) -> None:
        self._slm_key = str(slm_key)
        self._session = session
        self._hardware_id = HardwareDeviceId(
            category="slm", key=f"slm:{self._slm_key}"
        )
        self._device_id = DeviceId(kind="slm", name=self._slm_key)

    @property
    def hardware_id(self) -> HardwareDeviceId:
        return self._hardware_id

    @property
    def capabilities(self) -> DeviceLifecycleCapabilities:
        return DeviceLifecycleCapabilities(reconnect=True)

    def reconnect(self) -> DeviceLifecycleResult:
        result = self._session.reconnect_device()
        connected = bool(getattr(result, "connected", False))
        message = getattr(result, "message", None)
        return DeviceLifecycleResult(
            hardware_id=self.hardware_id,
            action=DeviceLifecycleAction.RECONNECT,
            success=connected,
            summary=(
                f"SLM {self._slm_key} reconnected"
                if connected
                else f"SLM {self._slm_key} reconnect failed"
            ),
            details=(None if message is None else str(message)),
            affected_device_ids=(self._device_id,),
        )

    def _unsupported(self, action: DeviceLifecycleAction):
        raise DeviceLifecycleNotSupportedError(
            f"{action.value} is not supported for SLM {self._slm_key}"
        )

    def connect(self):
        return self._unsupported(DeviceLifecycleAction.CONNECT)

    def disconnect(self):
        return self._unsupported(DeviceLifecycleAction.DISCONNECT)

    def probe(self):
        return self._unsupported(DeviceLifecycleAction.PROBE)

    def shutdown(self):
        return self._unsupported(DeviceLifecycleAction.SHUTDOWN)
