from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .device import SLMDeviceProvider
from .measurement import MeasurementProvider


@dataclass(frozen=True)
class SLMHostServices:
    """Physical/external capabilities supplied by an embedding application."""

    device: SLMDeviceProvider | None = None
    measurement_provider: MeasurementProvider | None = None

    def __post_init__(self) -> None:
        device = self.device
        if device is not None and not isinstance(device,SLMDeviceProvider):
            raise TypeError("device must be an SLMDeviceProvider or None")
        provider = self.measurement_provider
        if provider is not None:
            for name in ("available_sources","preferred_source","acquire"):
                if not callable(getattr(provider,name,None)):
                    raise TypeError(
                        "measurement_provider must implement %s()" % name
                    )

    @property
    def can_upload_frame(self) -> bool:
        return self.device is not None

    def upload(self,frame: np.ndarray) -> None:
        device = self.device
        if device is not None:
            device.upload_frame(frame)
