"""Application-level access to persisted observation-plane registrations."""

from __future__ import annotations

from typing import Any,Callable

import numpy as np

from ..core.engine.device import SLMIdentity
from ..core.measurement.observation_registration import (
    ObservationFrame,ObservationRegistrationCalibration,
)
from ..workspace.observation_registration_store import ObservationRegistrationStore

_UNSET=object()


class ObservationRegistrationService:
    """Resolve and evaluate saved observation registrations for one native SLM.

    Calibration fitting/acquisition is intentionally not owned here.  This
    service is the read/write persisted-resource boundary used by later
    reference-resolution and calibration-workflow layers.
    """

    def __init__(
        self,
        identity: SLMIdentity | Callable[[],SLMIdentity],
        store: ObservationRegistrationStore | None=None,
    ) -> None:
        self._identity_provider=identity if callable(identity) else lambda:identity
        self.store=store

    @property
    def identity(self) -> SLMIdentity:
        return self._identity_provider()

    def exists(self,section_key: str,observation_plane_name: str) -> bool:
        return False if self.store is None else self.store.exists(
            self.identity,section_key,observation_plane_name,
        )

    def load(
        self,section_key: str,observation_plane_name: str,
    ) -> ObservationRegistrationCalibration:
        return self._require_store().load(
            self.identity,section_key,observation_plane_name,
        )

    def registration_for_frame(
        self,frame: ObservationFrame,
    ) -> ObservationRegistrationCalibration | None:
        """Resolve a registration by the frame's own SLM/section/plane scope.

        Reads are workspace-wide so a current SLM session can project a saved
        reference originating from another SLM when both registrations share
        the same canonical observation frame.
        """
        if not isinstance(frame,ObservationFrame):
            raise TypeError("frame must be an ObservationFrame")
        store=self.store
        if store is None or not store.exists(
            frame.slm_serial,frame.section_key,frame.observation_plane_name,
        ):
            return None
        registration=store.load(
            frame.slm_serial,frame.section_key,frame.observation_plane_name,
        )
        registration.validate_native_frame(frame)
        return registration

    def registration(
        self,section_key: str,observation_plane_name: str,
    ) -> ObservationRegistrationCalibration | None:
        if not self.exists(section_key,observation_plane_name):
            return None
        return self.load(section_key,observation_plane_name)

    def canonical_frame(
        self,observation_plane_name: str,
    ) -> ObservationFrame | None:
        """Return the unique persisted canonical frame for an observation plane.

        A plane has one common coordinate frame across the workspace.  Conflicting
        legacy registrations are reported rather than silently choosing one.
        """
        plane=str(observation_plane_name or "").strip()
        if not plane:
            raise ValueError("Observation plane is required")
        store=self.store
        if store is None:
            return None
        registrations=store.registrations(plane)
        if not registrations:
            return None
        canonical=registrations[0].canonical_frame
        for registration in registrations[1:]:
            canonical.require_compatible(
                registration.canonical_frame,
                label=f'Observation plane "{plane}" has conflicting canonical frames',
            )
        return canonical

    def save(
        self,calibration: ObservationRegistrationCalibration,
    ) -> ObservationRegistrationCalibration:
        if not isinstance(calibration,ObservationRegistrationCalibration):
            raise TypeError("calibration must be an ObservationRegistrationCalibration")
        if calibration.native_frame.slm_serial!=self.identity.serial_number:
            raise ValueError("Observation registration belongs to a different native SLM")
        if calibration.native_frame.scope==calibration.canonical_frame.scope:
            raise ValueError(
                "The canonical observation frame is implicit and cannot be registered to itself"
            )

        store=self._require_store()
        native_scope=calibration.native_frame.scope
        for existing in store.registrations(calibration.native_frame.observation_plane_name):
            # Replacing the only registration is allowed to redefine the canonical
            # frame.  Every *other* registration for this plane must agree.
            if existing.native_frame.scope==native_scope:
                continue
            existing.canonical_frame.require_compatible(
                calibration.canonical_frame,
                label=(
                    'Observation plane "%s" already uses a different canonical frame'
                    % calibration.native_frame.observation_plane_name
                ),
            )
        store.save(calibration)
        return calibration

    def delete(self,section_key: str,observation_plane_name: str) -> None:
        self._require_store().delete(
            self.identity,section_key,observation_plane_name,
        )

    def native_to_canonical(
        self,frame: ObservationFrame,positions_px: Any,
    ) -> np.ndarray:
        registration=self._registration_for_native_frame(frame)
        return registration.native_to_canonical(positions_px)

    def canonical_to_native(
        self,native_frame: ObservationFrame,positions_px: Any,*,
        canonical_frame: ObservationFrame | None=None,
    ) -> np.ndarray:
        registration=self._registration_for_native_frame(native_frame)
        if canonical_frame is not None:
            registration.validate_canonical_frame(canonical_frame)
        return registration.canonical_to_native(positions_px)

    def project_points(
        self,source_frame: ObservationFrame,target_frame: ObservationFrame,
        positions_px: Any,*,source_registration: Any=_UNSET,
        target_registration: Any=_UNSET,
    ) -> np.ndarray:
        """Project full-detector points source-native -> canonical -> target-native.

        At least one saved registration must explicitly establish the bridge
        unless source and target already describe the same coordinate frame.
        An unregistered side may participate only when it is the explicit
        canonical frame named by the registration on the other side.
        """
        if not isinstance(source_frame,ObservationFrame) or not isinstance(target_frame,ObservationFrame):
            raise TypeError("source_frame and target_frame must be ObservationFrame values")
        points=_points(positions_px)
        if source_frame.is_compatible_with(target_frame):
            return points
        if source_frame.observation_plane_name!=target_frame.observation_plane_name:
            raise ValueError("Observation registration cannot bridge different observation planes")

        if source_registration is _UNSET:
            source_registration=self.registration_for_frame(source_frame)
        elif source_registration is not None:
            source_registration.validate_native_frame(source_frame)
        if target_registration is _UNSET:
            target_registration=self.registration_for_frame(target_frame)
        elif target_registration is not None:
            target_registration.validate_native_frame(target_frame)
        if source_registration is None and target_registration is None:
            raise ValueError("No observation registration explicitly bridges these frames")

        canonical=(
            source_registration.canonical_frame
            if source_registration is not None
            else target_registration.canonical_frame
        )
        if source_registration is not None and target_registration is not None:
            source_registration.canonical_frame.require_compatible(
                target_registration.canonical_frame,
                label="Observation registrations' canonical frames",
            )

        if source_registration is None:
            canonical.require_compatible(
                source_frame,label="Unregistered source is not the canonical frame",
            )
            canonical_points=points
        else:
            canonical_points=source_registration.native_to_canonical(points)

        if target_registration is None:
            canonical.require_compatible(
                target_frame,label="Unregistered target is not the canonical frame",
            )
            return canonical_points
        target_registration.validate_canonical_frame(canonical)
        return target_registration.canonical_to_native(canonical_points)

    def add_listener(self,callback: Callable[[],None]) -> None:
        if self.store is not None:
            self.store.add_listener(callback)

    def remove_listener(self,callback: Callable[[],None]) -> None:
        if self.store is not None:
            self.store.remove_listener(callback)

    def _registration_for_native_frame(
        self,frame: ObservationFrame,
    ) -> ObservationRegistrationCalibration:
        if not isinstance(frame,ObservationFrame):
            raise TypeError("frame must be an ObservationFrame")
        registration=self.registration_for_frame(frame)
        if registration is None:
            raise FileNotFoundError(
                "Observation registration does not exist for %s/%s/%s."
                % frame.scope
            )
        return registration

    def _require_store(self) -> ObservationRegistrationStore:
        if self.store is None:
            raise RuntimeError("Observation-registration storage is unavailable.")
        return self.store


def _points(value: Any) -> np.ndarray:
    array=np.asarray(value,dtype=np.float64)
    if array.ndim!=2 or array.shape[0]!=2 or array.shape[1]<1:
        raise ValueError("positions_px must have shape (2, N) with at least one point")
    if not np.all(np.isfinite(array)):
        raise ValueError("positions_px contains non-finite values")
    return np.array(array,copy=True)


__all__=["ObservationRegistrationService"]
