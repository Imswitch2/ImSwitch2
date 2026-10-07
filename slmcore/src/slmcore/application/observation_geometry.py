"""Shared application ownership of observation-plane geometry calibration."""

from __future__ import annotations

from typing import Callable

from ..core.cgh.feedback import FeedbackOrientation, GeometryOrientationCalibration
from ..core.engine.device import SLMIdentity
from ..workspace.geometry_orientation_calibration_store import (
    SLMGeometryOrientationCalibrationStore,
)


class ObservationGeometryService:
    """Resolve saved geometry for one SLM by section and observation plane.

    The service owns persistent geometry access only. Calibration acquisition,
    localization and analysis remain in the dedicated geometry-calibration
    workflow; feedback consumes the saved result read-only.
    """

    def __init__(
        self,
        identity: SLMIdentity | Callable[[], SLMIdentity],
        store: SLMGeometryOrientationCalibrationStore | None = None,
    ) -> None:
        self._identity_provider = identity if callable(identity) else lambda: identity
        self.store = store

    @property
    def identity(self) -> SLMIdentity:
        return self._identity_provider()

    def exists(self, section_key: str, observation_plane_name: str) -> bool:
        store = self.store
        return False if store is None else store.exists(
            self.identity, section_key, observation_plane_name,
        )

    def is_calibrated(self, section_key: str, observation_plane_name: str) -> bool:
        """Return whether a saved calibration exists for this exact scope."""
        return self.exists(section_key, observation_plane_name)

    def load(
        self, section_key: str, observation_plane_name: str,
    ) -> GeometryOrientationCalibration:
        return self._require_store().load(
            self.identity, section_key, observation_plane_name,
        )

    def calibration(
        self, section_key: str, observation_plane_name: str,
    ) -> GeometryOrientationCalibration | None:
        """Resolve the saved calibration, or ``None`` when this scope is uncalibrated."""
        if not self.exists(section_key, observation_plane_name):
            return None
        return self.load(section_key, observation_plane_name)

    def calibrated_orientation(
        self, section_key: str, observation_plane_name: str,
    ) -> FeedbackOrientation | None:
        calibration = self.calibration(section_key, observation_plane_name)
        return None if calibration is None else calibration.orientation

    def save(
        self, calibration: GeometryOrientationCalibration,
    ) -> GeometryOrientationCalibration:
        if not isinstance(calibration, GeometryOrientationCalibration):
            raise TypeError("calibration must be a GeometryOrientationCalibration")
        if calibration.slm_serial != self.identity.serial_number:
            raise ValueError("Geometry calibration belongs to a different SLM")
        self._require_store().save(calibration)
        return calibration

    def delete(self, section_key: str, observation_plane_name: str) -> None:
        self._require_store().delete(
            self.identity, section_key, observation_plane_name,
        )

    def add_listener(self, callback: Callable[[], None]) -> None:
        if self.store is not None:
            self.store.add_listener(callback)

    def remove_listener(self, callback: Callable[[], None]) -> None:
        if self.store is not None:
            self.store.remove_listener(callback)

    def _require_store(self) -> SLMGeometryOrientationCalibrationStore:
        if self.store is None:
            raise RuntimeError("Geometry orientation-calibration storage is unavailable.")
        return self.store


__all__ = ["ObservationGeometryService"]
