from __future__ import annotations

from dataclasses import dataclass
from typing import Any,Callable

import numpy as np


@dataclass(frozen=True)
class DeviceConnectionResult:
    """Normalized result of a device connect/disconnect operation."""

    connected: bool
    message: str | None = None
    device_info: Any = None


class SLMDeviceProvider:
    """Callback-backed physical SLM device capability.

    The provider describes *how* to connect, disconnect and upload a frame.
    Higher-level lifecycle and UI behavior remain owned by ``SLMQtSession``.
    """

    def __init__(
        self,
        *,
        upload_frame: Callable[[np.ndarray],Any],
        connect: Callable[[], Any] | None=None,
        disconnect: Callable[[], Any] | None=None,
        requires_explicit_connection: bool=False,
    ) -> None:
        if not callable(upload_frame):
            raise TypeError("upload_frame must be callable")
        if connect is not None and not callable(connect):
            raise TypeError("connect must be callable or None")
        if disconnect is not None and not callable(disconnect):
            raise TypeError("disconnect must be callable or None")

        self._upload_frame = upload_frame
        self._connect = connect
        self._disconnect = disconnect
        self._requires_explicit_connection = bool(requires_explicit_connection)

        if self.requires_explicit_connection and (
            self._connect is None or self._disconnect is None
        ):
            raise ValueError(
                "Explicitly connected devices require both connect and disconnect callbacks"
            )

    @property
    def requires_explicit_connection(self) -> bool:
        return self._requires_explicit_connection

    @property
    def is_mock(self) -> bool:
        return False

    def connect(self) -> DeviceConnectionResult:
        callback = self._connect
        if callback is None:
            return DeviceConnectionResult(connected=True)
        return self._normalize_connect_result(callback())

    def disconnect(self) -> DeviceConnectionResult:
        callback = self._disconnect
        if callback is None:
            return DeviceConnectionResult(connected=False)
        return self._normalize_disconnect_result(callback())

    def upload_frame(self,frame: np.ndarray) -> Any:
        return self._upload_frame(frame)

    @staticmethod
    def _normalize_connect_result(value: Any) -> DeviceConnectionResult:
        if isinstance(value,DeviceConnectionResult):
            return value
        if isinstance(value,(tuple,list)):
            if not value:
                raise ValueError("Connection callback returned an empty result")
            success = bool(value[0])
            info = value[1] if len(value) > 1 else None
            message = None if success else (None if info is None else str(info))
            return DeviceConnectionResult(
                connected=success,
                message=message,
                device_info=(info if success else None),
            )
        return DeviceConnectionResult(connected=bool(value))

    @staticmethod
    def _normalize_disconnect_result(value: Any) -> DeviceConnectionResult:
        if isinstance(value,DeviceConnectionResult):
            return value
        if isinstance(value,(tuple,list)):
            if not value:
                raise ValueError("Disconnection callback returned an empty result")
            success = bool(value[0])
            detail = value[1] if len(value) > 1 else None
            return DeviceConnectionResult(
                connected=not success,
                message=None if detail is None else str(detail),
            )
        success = bool(value)
        return DeviceConnectionResult(connected=not success)


class MockSLMDeviceProvider(SLMDeviceProvider):
    """Simple in-memory SLM device provider for tests and simulations."""

    def __init__(self,*,requires_explicit_connection: bool=False) -> None:
        self.last_frame = None
        self.upload_count = 0
        self.connected = not bool(requires_explicit_connection)

        def upload(frame):
            self.last_frame = np.array(frame,copy=True)
            self.upload_count += 1

        def connect():
            self.connected = True
            return DeviceConnectionResult(connected=True,device_info="mock")

        def disconnect():
            self.connected = False
            return DeviceConnectionResult(connected=False)

        super().__init__(
            upload_frame=upload,
            connect=(connect if requires_explicit_connection else None),
            disconnect=(disconnect if requires_explicit_connection else None),
            requires_explicit_connection=requires_explicit_connection,
        )

    @property
    def is_mock(self) -> bool:
        return True
