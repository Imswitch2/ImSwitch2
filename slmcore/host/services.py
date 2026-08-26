from __future__ import annotations

from dataclasses import dataclass
from typing import Any,Callable

import numpy as np

from .calibration import CalibrationPreferences
from .configuration import ConfigurationPreferences,SectionViewPreferences
from .device import SLMDeviceProvider
from .measurement import MeasurementProvider


@dataclass(frozen=True)
class SLMHostServices:
    """Capabilities supplied by an embedding application.

    The service object deliberately exposes capabilities rather than workflow
    callbacks. slmcore decides *when* a frame or measurement is needed; the
    host only knows how to perform the physical operation.
    """

    device: SLMDeviceProvider | None = None
    measurement_provider: MeasurementProvider | None = None
    calibration_preferences: CalibrationPreferences | None = None
    configuration_preferences: ConfigurationPreferences | None = None
    section_view_preferences: SectionViewPreferences | None = None

    @classmethod
    def from_callbacks(
        cls,
        *,
        device: SLMDeviceProvider | None=None,
        measurement_provider: MeasurementProvider | None=None,
        get_startup_config: Callable[[], str | None] | None=None,
        set_startup_config: Callable[[str | None], None] | None=None,
        get_default_plane: Callable[[str], str | None] | None=None,
        set_default_plane: Callable[[str, str | None], None] | None=None,
        get_section_display_mode: Callable[[], Any] | None=None,
        set_section_display_mode: Callable[[Any], None] | None=None,
    ) -> "SLMHostServices":
        """Build host services directly from host callbacks.

        Preference capabilities are optional, but each getter/setter pair must
        be supplied together. This keeps host adapters concise while retaining
        the explicit capability objects internally.
        """
        configuration = cls._optional_preference_pair(
            "startup config",
            get_startup_config,
            set_startup_config,
            ConfigurationPreferences,
            "get_startup_config",
            "set_startup_config",
        )
        calibration = cls._optional_preference_pair(
            "default plane",
            get_default_plane,
            set_default_plane,
            CalibrationPreferences,
            "get_default_plane",
            "set_default_plane",
        )
        section_view = cls._optional_preference_pair(
            "section display mode",
            get_section_display_mode,
            set_section_display_mode,
            SectionViewPreferences,
            "get_display_mode",
            "set_display_mode",
        )
        return cls(
            device=device,
            measurement_provider=measurement_provider,
            calibration_preferences=calibration,
            configuration_preferences=configuration,
            section_view_preferences=section_view,
        )

    @staticmethod
    def _optional_preference_pair(
        label, getter, setter, preference_type, getter_name, setter_name,
    ):
        if getter is None and setter is None:
            return None
        if not callable(getter) or not callable(setter):
            raise TypeError(
                "%s getter and setter must be supplied together" % label
            )
        return preference_type(**{getter_name:getter,setter_name:setter})

    def __post_init__(self) -> None:
        device = self.device
        if device is not None and not isinstance(device,SLMDeviceProvider):
            raise TypeError("device must be an SLMDeviceProvider or None")
        preferences = self.calibration_preferences
        if preferences is not None and not isinstance(preferences,CalibrationPreferences):
            raise TypeError("calibration_preferences must be CalibrationPreferences or None")
        configuration = self.configuration_preferences
        if configuration is not None and not isinstance(configuration,ConfigurationPreferences):
            raise TypeError("configuration_preferences must be ConfigurationPreferences or None")
        view_preferences = self.section_view_preferences
        if view_preferences is not None and not isinstance(view_preferences,SectionViewPreferences):
            raise TypeError("section_view_preferences must be SectionViewPreferences or None")
        provider = self.measurement_provider
        if provider is not None:
            for name in ("available_sources","preferred_source","acquire"):
                if not callable(getattr(provider,name,None)):
                    raise TypeError(
                        "measurement_provider must implement %s()" % name
                    )


    def with_fallbacks(self,fallbacks: "SLMHostServices | None") -> "SLMHostServices":
        """Fill missing capabilities from fallback services.

        Explicit host capabilities always win. This lets a workspace provide
        reusable persistence defaults without overriding an embedding host.
        """
        if fallbacks is None:
            return self
        if not isinstance(fallbacks,SLMHostServices):
            raise TypeError("fallbacks must be an SLMHostServices or None")
        return type(self)(
            device=self.device if self.device is not None else fallbacks.device,
            measurement_provider=(
                self.measurement_provider
                if self.measurement_provider is not None
                else fallbacks.measurement_provider
            ),
            calibration_preferences=(
                self.calibration_preferences
                if self.calibration_preferences is not None
                else fallbacks.calibration_preferences
            ),
            configuration_preferences=(
                self.configuration_preferences
                if self.configuration_preferences is not None
                else fallbacks.configuration_preferences
            ),
            section_view_preferences=(
                self.section_view_preferences
                if self.section_view_preferences is not None
                else fallbacks.section_view_preferences
            ),
        )

    @property
    def can_upload_frame(self) -> bool:
        return self.device is not None

    def upload(self,frame: np.ndarray) -> None:
        device = self.device
        if device is not None:
            device.upload_frame(frame)
