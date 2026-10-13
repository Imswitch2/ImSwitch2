"""Stateful observation-registration calibration workflow.

The native side consumes the current section's accepted feedback localization.
The canonical side consumes a raw named position reference from the shared
reference store.  This intentionally reuses the existing cross-SLM reference
handoff without resolving/projecting that reference through position feedback:
registration calibration must never depend on the registration being fitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any,Callable,Mapping,TYPE_CHECKING

import numpy as np

from ..core.cgh.feedback import orient_localization
from ..core.cgh.localization import localization_site_masks
from ..core.measurement import (
    ObservationFrame,ObservationRegistrationCalibration,PositionReferenceTarget,ReferenceGeometry,
    fit_observation_registration,reference_geometry_from_localization,
    reference_geometry_from_position_reference,resolve_exact_lattice_correspondence,
)
from ..core.transform2d import TRANSFORM_AFFINE,TRANSFORM_POLYNOMIAL
from ..workspace.position_reference_store import PositionReferenceLifetime,PositionReferenceRecord

if TYPE_CHECKING:
    from .observation import ObservationPlaneService
    from .observation_registration import ObservationRegistrationService
    from .session import SLMSession


@dataclass(frozen=True)
class ObservationRegistrationLocalizationSnapshot:
    """Detached accepted localization in one native full-detector frame."""

    frame: ObservationFrame
    geometry: ReferenceGeometry
    measurement_id: str
    measurement_created_at: str
    feedback_orientation: str
    target_type: str
    matched_count: int
    total_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.frame,ObservationFrame):
            raise TypeError("frame must be an ObservationFrame")
        if not isinstance(self.geometry,ReferenceGeometry):
            raise TypeError("geometry must be a ReferenceGeometry")
        if self.geometry.site_ids is None:
            raise ValueError("Registration localization requires logical site IDs")
        object.__setattr__(self,"measurement_id",str(self.measurement_id or ""))
        object.__setattr__(self,"measurement_created_at",str(self.measurement_created_at or ""))
        object.__setattr__(self,"feedback_orientation",str(self.feedback_orientation or "identity"))
        object.__setattr__(self,"target_type",str(self.target_type or ""))
        object.__setattr__(self,"matched_count",int(self.matched_count))
        object.__setattr__(self,"total_count",int(self.total_count))


@dataclass(frozen=True)
class ObservationRegistrationReferenceSnapshot:
    """Detached raw saved/session reference used as the canonical geometry."""

    frame: ObservationFrame
    geometry: ReferenceGeometry
    reference_name: str
    reference_created_at: str
    lifetime: PositionReferenceLifetime
    target_type: str
    valid_count: int
    total_count: int
    sample_policy: str

    def __post_init__(self) -> None:
        if not isinstance(self.frame,ObservationFrame):
            raise TypeError("frame must be an ObservationFrame")
        if not isinstance(self.geometry,ReferenceGeometry):
            raise TypeError("geometry must be a ReferenceGeometry")
        if self.geometry.site_ids is None:
            raise ValueError("Registration reference requires logical site IDs")
        object.__setattr__(self,"reference_name",str(self.reference_name or ""))
        object.__setattr__(self,"reference_created_at",str(self.reference_created_at or ""))
        object.__setattr__(self,"lifetime",PositionReferenceLifetime.normalize(self.lifetime))
        object.__setattr__(self,"target_type",str(self.target_type or ""))
        object.__setattr__(self,"valid_count",int(self.valid_count))
        object.__setattr__(self,"total_count",int(self.total_count))
        object.__setattr__(self,"sample_policy",str(self.sample_policy or ""))


@dataclass(frozen=True)
class ObservationRegistrationFitDiagnostics:
    """Detached fit geometry for calibration inspection/visualization."""

    native_points_px: np.ndarray
    canonical_points_px: np.ndarray
    transformed_native_points_px: np.ndarray
    forward_residual_vectors_px: np.ndarray
    transformed_canonical_points_px: np.ndarray
    reverse_residual_vectors_px: np.ndarray
    forward_rms_residual_px: float
    forward_max_residual_px: float
    reverse_rms_residual_px: float
    reverse_max_residual_px: float

    def __post_init__(self) -> None:
        arrays={}
        for name in (
            "native_points_px","canonical_points_px",
            "transformed_native_points_px","forward_residual_vectors_px",
            "transformed_canonical_points_px","reverse_residual_vectors_px",
        ):
            value=np.asarray(getattr(self,name),dtype=np.float64)
            if value.ndim!=2 or value.shape[0]!=2 or not np.all(np.isfinite(value)):
                raise ValueError(f"{name} must be finite with shape (2, N)")
            frozen=np.array(value,copy=True); frozen.setflags(write=False)
            arrays[name]=frozen
        shape=arrays["native_points_px"].shape
        if any(value.shape!=shape for value in arrays.values()):
            raise ValueError("Registration diagnostic arrays must share one shape")
        for name,value in arrays.items():
            object.__setattr__(self,name,value)
        for name in (
            "forward_rms_residual_px","forward_max_residual_px",
            "reverse_rms_residual_px","reverse_max_residual_px",
        ):
            value=float(getattr(self,name))
            if not np.isfinite(value) or value<0:
                raise ValueError(f"{name} must be finite and >= 0")
            object.__setattr__(self,name,value)

    @property
    def point_count(self) -> int:
        return int(self.native_points_px.shape[1])


@dataclass(frozen=True)
class ObservationRegistrationCalibrationContext:
    """UI-neutral snapshot of one native-section registration workflow."""

    native_section_key: str
    canonical_reference_name: str
    observation_plane_name: str
    native_snapshot: ObservationRegistrationLocalizationSnapshot | None
    canonical_snapshot: ObservationRegistrationReferenceSnapshot | None
    available_references: tuple[PositionReferenceRecord,...]
    candidate: ObservationRegistrationCalibration | None
    diagnostics: ObservationRegistrationFitDiagnostics | None
    saved_registration: ObservationRegistrationCalibration | None
    latest_native_available: bool
    common_site_count: int

    @property
    def fit_enabled(self) -> bool:
        return self.native_snapshot is not None and self.canonical_snapshot is not None

    @property
    def save_enabled(self) -> bool:
        return self.candidate is not None


@dataclass
class _ObservationRegistrationWorkflow:
    canonical_reference_name: str
    native_snapshot: ObservationRegistrationLocalizationSnapshot | None=None
    canonical_snapshot: ObservationRegistrationReferenceSnapshot | None=None
    candidate: ObservationRegistrationCalibration | None=None
    diagnostics: ObservationRegistrationFitDiagnostics | None=None


class ObservationRegistrationCalibrationService:
    """Fit/save observation registrations from a localization and named reference."""

    def __init__(
        self,
        session: "SLMSession",
        *,
        observation_planes: "ObservationPlaneService",
        observation_registration: "ObservationRegistrationService",
        localization_requester: Callable[[str],None] | None=None,
    ) -> None:
        self.session=session
        self.observation_planes=observation_planes
        self.observation_registration=observation_registration
        self._localization_requester=localization_requester
        self._workflows: dict[tuple[str,str],_ObservationRegistrationWorkflow]={}

    def set_localization_requester(
        self,requester: Callable[[str],None] | None,
    ) -> None:
        """Install a presentation deep-link into the existing localization UI."""
        self._localization_requester=requester

    def reference_records(
        self,native_section_key: str,observation_plane_name: str,
    ) -> tuple[PositionReferenceRecord,...]:
        """Return canonical-reference choices, excluding the native frame itself."""
        native=str(native_section_key or "").strip()
        plane=self._require_plane(observation_plane_name)
        self._require_section(native)
        native_scope=(self.session.runtime.identity.serial_number,native,plane)
        records=[]
        for record in self.session.feedback.position_reference_records(plane):
            try:
                frame=self._frame_from_reference_record(record,plane)
            except (KeyError,TypeError,ValueError,RuntimeError):
                # Keep malformed/legacy references visible: selecting one yields
                # a precise compatibility error instead of silently hiding data.
                records.append(record); continue
            if frame.scope!=native_scope:
                records.append(record)
        return tuple(records)

    def context(
        self,
        native_section_key: str,
        observation_plane_name: str,
        canonical_reference_name: str | None=None,
    ) -> ObservationRegistrationCalibrationContext:
        native,plane=self._normalize_scope(native_section_key,observation_plane_name)
        records=self.reference_records(native,plane)
        reference_name=self._choose_reference_name(
            native,plane,canonical_reference_name,records,
        )
        workflow=self._workflow(native,plane,reference_name)
        if reference_name:
            workflow.canonical_snapshot=self._reference_snapshot(plane,reference_name)
        else:
            workflow.canonical_snapshot=None
        saved=self.observation_registration.registration(native,plane)
        return ObservationRegistrationCalibrationContext(
            native_section_key=native,
            canonical_reference_name=reference_name,
            observation_plane_name=plane,
            native_snapshot=workflow.native_snapshot,
            canonical_snapshot=workflow.canonical_snapshot,
            available_references=records,
            candidate=workflow.candidate,
            diagnostics=workflow.diagnostics,
            saved_registration=saved,
            latest_native_available=self.latest_localization(native,plane) is not None,
            common_site_count=self._common_site_count(
                workflow.native_snapshot,workflow.canonical_snapshot,
            ),
        )

    def latest_localization(
        self,section_key: str,observation_plane_name: str,
    ) -> ObservationRegistrationLocalizationSnapshot | None:
        """Return the latest accepted localization for section × plane, if any."""
        section=str(section_key or "").strip()
        plane=self._require_plane(observation_plane_name)
        self._require_section(section)
        if self.session.feedback.active_observation_plane(section)!=plane:
            return None
        inspection=self.session.runtime.get_section_feedback_inspection(section)
        measurement=inspection.measurement
        if measurement is None or measurement.localization is None:
            return None
        acquisition=measurement.acquisition
        measurement_plane=str(
            dict(acquisition.metadata or {}).get("observation_plane") or ""
        ).strip()
        if measurement_plane and measurement_plane!=plane:
            return None

        orientation=self.session.feedback.feedback_orientation(section)
        localization=orient_localization(measurement.localization,orientation)
        matched,_inferred,_resolved=localization_site_masks(localization)
        geometry=reference_geometry_from_localization(
            localization,
            positions="measured",
            full_image=True,
            valid_mask=matched,
            metadata={
                "observation_plane_name":plane,
                "section_key":section,
                "slm_serial":self.session.runtime.identity.serial_number,
                "measurement_id":acquisition.measurement_id,
                "feedback_orientation":orientation.value,
                "calibration_sample_policy":"genuinely_measured_only",
            },
        )
        frame=ObservationFrame(
            slm_serial=self.session.runtime.identity.serial_number,
            section_key=section,
            observation_plane_name=plane,
            detector_name=acquisition.detector,
            image_shape=tuple(int(value) for value in acquisition.image.shape),
            pixel_size_um=acquisition.pixel_size_um,
        )
        return ObservationRegistrationLocalizationSnapshot(
            frame=frame,
            geometry=geometry,
            measurement_id=acquisition.measurement_id,
            measurement_created_at=acquisition.created_at,
            feedback_orientation=orientation.value,
            target_type=localization.target_type,
            matched_count=int(np.count_nonzero(matched)),
            total_count=int(localization.lattice_indices.shape[1]),
        )

    def use_latest_localization(
        self,
        native_section_key: str,
        observation_plane_name: str,
        canonical_reference_name: str | None=None,
        *,
        role: str="native",
    ) -> ObservationRegistrationLocalizationSnapshot:
        """Snapshot the native localization; canonical geometry comes from a reference."""
        if str(role or "native").strip().lower()!="native":
            raise ValueError("Canonical registration geometry must come from a saved/session reference")
        native,plane=self._normalize_scope(native_section_key,observation_plane_name)
        records=self.reference_records(native,plane)
        reference_name=self._choose_reference_name(native,plane,canonical_reference_name,records)
        workflow=self._workflow(native,plane,reference_name)
        snapshot=self.latest_localization(native,plane)
        if snapshot is None:
            raise RuntimeError(
                f"No accepted {plane} localization is available for section {native}."
            )
        workflow.native_snapshot=snapshot
        if reference_name:
            workflow.canonical_snapshot=self._reference_snapshot(plane,reference_name)
        workflow.candidate=None
        workflow.diagnostics=None
        return snapshot

    def request_localization(
        self,
        native_section_key: str,
        observation_plane_name: str,
        canonical_reference_name: str | None=None,
        *,
        role: str="native",
    ) -> None:
        """Select the requested plane and deep-link to the native localization UI."""
        self._require_editor_mode()
        if str(role or "native").strip().lower()!="native":
            raise ValueError("Canonical registration geometry is selected from named references")
        native,plane=self._normalize_scope(native_section_key,observation_plane_name)
        if self.session.feedback.active_observation_plane(native)!=plane:
            self.session.feedback.select_observation_plane(native,plane)
        requester=self._localization_requester
        if requester is None:
            raise RuntimeError(
                "No localization navigation adapter is configured for this session."
            )
        requester(native)

    def fit(
        self,
        native_section_key: str,
        observation_plane_name: str,
        canonical_reference_name: str,
        *,
        model: str,
        polynomial_degree: int=2,
        provenance: Mapping[str,Any] | None=None,
    ) -> ObservationRegistrationCalibration:
        self._require_editor_mode()
        native,plane=self._normalize_scope(native_section_key,observation_plane_name)
        records=self.reference_records(native,plane)
        reference_name=self._choose_reference_name(native,plane,canonical_reference_name,records)
        if not reference_name:
            raise RuntimeError("Select a canonical saved/session reference before fitting registration.")
        workflow=self._workflow(native,plane,reference_name)
        workflow.canonical_snapshot=self._reference_snapshot(plane,reference_name)
        if workflow.native_snapshot is None:
            raise RuntimeError("Use the accepted native localization before fitting registration.")
        normalized_model=str(model or "").strip().lower()
        if normalized_model not in {TRANSFORM_AFFINE,TRANSFORM_POLYNOMIAL}:
            raise ValueError("Observation-registration calibration supports affine or polynomial fits")
        degree=int(polynomial_degree)
        if normalized_model==TRANSFORM_POLYNOMIAL and degree not in {2,3,4}:
            raise ValueError("Observation-registration polynomial degree must be 2, 3, or 4")
        native_points,canonical_points,site_ids,correspondence=self._matched_points(
            workflow.native_snapshot,workflow.canonical_snapshot,
        )
        canonical=workflow.canonical_snapshot
        if workflow.native_snapshot.frame.scope==canonical.frame.scope:
            raise ValueError(
                "Native section and canonical reference describe the same observation frame"
            )
        fit_provenance={
            "workflow":"observation_registration_calibration",
            "correspondence":"exact_lattice_auto",
            "resolved_correspondence":str(correspondence.get("resolved", "")),
            "correspondence_details":dict(correspondence),
            "site_ids":[_json_site_id(value) for value in site_ids],
            "native_measurement_id":workflow.native_snapshot.measurement_id,
            "native_measurement_created_at":workflow.native_snapshot.measurement_created_at,
            "native_feedback_orientation":workflow.native_snapshot.feedback_orientation,
            "native_target_type":workflow.native_snapshot.target_type,
            "canonical_reference_name":canonical.reference_name,
            "canonical_reference_lifetime":canonical.lifetime.value,
            "canonical_reference_created_at":canonical.reference_created_at,
            "canonical_target_type":canonical.target_type,
            "native_sample_policy":"genuinely_measured_only",
            "canonical_sample_policy":canonical.sample_policy,
            "sample_policy":"native_measured_plus_raw_named_reference",
        }
        fit_provenance.update(dict(provenance or {}))
        workflow.candidate=fit_observation_registration(
            workflow.native_snapshot.frame,
            canonical.frame,
            native_points,
            canonical_points,
            model=normalized_model,
            polynomial_degree=degree,
            provenance=fit_provenance,
        )
        transformed_native=workflow.candidate.native_to_canonical(native_points)
        transformed_canonical=workflow.candidate.canonical_to_native(canonical_points)
        workflow.diagnostics=ObservationRegistrationFitDiagnostics(
            native_points_px=native_points,
            canonical_points_px=canonical_points,
            transformed_native_points_px=transformed_native,
            forward_residual_vectors_px=canonical_points-transformed_native,
            transformed_canonical_points_px=transformed_canonical,
            reverse_residual_vectors_px=native_points-transformed_canonical,
            forward_rms_residual_px=workflow.candidate.forward_transform.rms_residual,
            forward_max_residual_px=workflow.candidate.forward_transform.max_residual,
            reverse_rms_residual_px=workflow.candidate.reverse_transform.rms_residual,
            reverse_max_residual_px=workflow.candidate.reverse_transform.max_residual,
        )
        return workflow.candidate

    def save(
        self,
        native_section_key: str,
        observation_plane_name: str,
        canonical_reference_name: str,
    ) -> ObservationRegistrationCalibration:
        self._require_editor_mode()
        native,plane=self._normalize_scope(native_section_key,observation_plane_name)
        workflow=self._workflow(native,plane,str(canonical_reference_name or "").strip())
        if workflow.candidate is None:
            raise RuntimeError("Fit an observation registration before saving.")
        return self.observation_registration.save(workflow.candidate)

    def delete(self,native_section_key: str,observation_plane_name: str) -> None:
        self._require_editor_mode()
        native=str(native_section_key or "").strip()
        plane=self._require_plane(observation_plane_name)
        self._require_section(native)
        self.observation_registration.delete(native,plane)

    def cancel(self,native_section_key: str,observation_plane_name: str) -> None:
        self.clear(native_section_key,observation_plane_name)

    def clear(self,native_section_key: str,observation_plane_name: str) -> None:
        native=str(native_section_key or "").strip()
        plane=self._require_plane(observation_plane_name)
        self._workflows.pop((native,plane),None)

    def prepare_runtime_change(self) -> None:
        self._workflows.clear()

    def dispose(self) -> None:
        self.prepare_runtime_change()
        self._localization_requester=None

    def _workflow(
        self,native_section_key: str,plane: str,canonical_reference_name: str,
    ) -> _ObservationRegistrationWorkflow:
        key=(native_section_key,plane)
        workflow=self._workflows.get(key)
        if workflow is None or workflow.canonical_reference_name!=canonical_reference_name:
            workflow=_ObservationRegistrationWorkflow(
                canonical_reference_name=canonical_reference_name,
            )
            self._workflows[key]=workflow
        return workflow

    def _choose_reference_name(
        self,native: str,plane: str,requested: str | None,
        records: tuple[PositionReferenceRecord,...],
    ) -> str:
        names={record.name for record in records}
        requested_name=str(requested or "").strip()
        if requested_name:
            if requested_name not in names:
                raise FileNotFoundError(
                    f'Position reference "{requested_name}" is not available for observation plane "{plane}".'
                )
            return requested_name
        existing=self._workflows.get((native,plane))
        if existing is not None and existing.canonical_reference_name in names:
            return existing.canonical_reference_name
        saved=self.observation_registration.registration(native,plane)
        if saved is not None:
            provenance=dict(saved.provenance or {})
            saved_name=str(provenance.get("canonical_reference_name") or "").strip()
            if saved_name in names:
                return saved_name
            for record in records:
                try:
                    if self._frame_from_reference_record(record,plane).scope==saved.canonical_frame.scope:
                        return record.name
                except Exception:
                    continue
        return records[0].name if records else ""

    def _reference_snapshot(
        self,plane: str,reference_name: str,
    ) -> ObservationRegistrationReferenceSnapshot:
        records={record.name:record for record in self.session.feedback.position_reference_records(plane)}
        try:
            record=records[str(reference_name)]
        except KeyError as error:
            raise FileNotFoundError(
                f'Position reference "{reference_name}" is not available for observation plane "{plane}".'
            ) from error
        reference=record.reference
        if reference.observation_plane_name!=plane:
            raise ValueError(
                f'Reference "{reference.name}" belongs to observation plane '
                f'"{reference.observation_plane_name}", not "{plane}".'
            )
        frame=self._frame_from_reference_record(record,plane)
        geometry=reference_geometry_from_position_reference(reference)
        valid_mask,sample_policy=self._reference_valid_mask(reference)
        geometry=ReferenceGeometry(
            positions_px=geometry.positions_px,
            site_ids=geometry.site_ids,
            lattice=geometry.lattice,
            valid_mask=valid_mask,
            weights=geometry.weights,
            metadata=dict(geometry.metadata),
        )
        if frame.scope==(
            self.session.runtime.identity.serial_number,
            str(reference.metadata.get("source_section") or frame.section_key),
            plane,
        ) and frame.slm_serial==self.session.runtime.identity.serial_number:
            # Self-reference is rejected later against the selected native section;
            # this block intentionally only documents that source identity is kept.
            pass
        metadata=dict(reference.metadata or {})
        return ObservationRegistrationReferenceSnapshot(
            frame=frame,
            geometry=geometry,
            reference_name=reference.name,
            reference_created_at=reference.created_at,
            lifetime=record.lifetime,
            target_type=str(metadata.get("source_target_type") or ""),
            valid_count=int(np.count_nonzero(valid_mask)),
            total_count=int(reference.positions_px.shape[1]),
            sample_policy=sample_policy,
        )

    @staticmethod
    def _reference_valid_mask(reference) -> tuple[np.ndarray,str]:
        metadata=dict(reference.metadata or {})
        matched_indices=metadata.get("matched_lattice_indices")
        count=int(reference.positions_px.shape[1])
        if matched_indices is None:
            return np.ones(count,dtype=bool),"all_stored_reference_sites_legacy"
        values=np.asarray(matched_indices)
        if values.ndim!=2 or values.shape[0]!=2:
            raise ValueError(
                f'Position reference "{reference.name}" has invalid matched_lattice_indices metadata.'
            )
        matched={tuple(values[:,index].tolist()) for index in range(values.shape[1])}
        mask=np.asarray([
            tuple(reference.lattice_indices[:,index].tolist()) in matched
            for index in range(count)
        ],dtype=bool)
        if not np.any(mask):
            raise ValueError(
                f'Position reference "{reference.name}" contains no genuinely measured lattice sites.'
            )
        return mask,"genuinely_measured_sites_from_reference"

    @staticmethod
    def _frame_from_reference_record(
        record: PositionReferenceRecord,plane: str,
    ) -> ObservationFrame:
        reference=record.reference
        metadata=dict(reference.metadata or {})
        payload=metadata.get("source_observation_frame")
        if payload is not None:
            frame=ObservationFrame.from_dict(payload)
        else:
            serial=str(metadata.get("source_slm_serial") or "").strip()
            section=str(metadata.get("source_section") or "").strip()
            if not serial or not section:
                raise RuntimeError(
                    f'Position reference "{reference.name}" lacks source SLM/section metadata. '
                    "Recreate the reference with the current SLMCore reference workflow."
                )
            frame=ObservationFrame(
                slm_serial=serial,
                section_key=section,
                observation_plane_name=reference.observation_plane_name,
                detector_name=reference.detector_name,
                image_shape=reference.image_shape,
                pixel_size_um=metadata.get("source_pixel_size_um"),
            )
        if frame.observation_plane_name!=plane or reference.observation_plane_name!=plane:
            raise ValueError(
                f'Position reference "{reference.name}" is not from observation plane "{plane}".'
            )
        # The reference record itself is authoritative for the saved detector
        # raster.  Contradictions indicate stale/corrupt provenance.
        record_frame=ObservationFrame(
            slm_serial=frame.slm_serial,
            section_key=frame.section_key,
            observation_plane_name=plane,
            detector_name=reference.detector_name,
            image_shape=reference.image_shape,
            pixel_size_um=metadata.get("source_pixel_size_um",frame.pixel_size_um),
        )
        frame.require_compatible(record_frame,label=f'Position reference "{reference.name}" source frame')
        return frame

    def _normalize_scope(
        self,native_section_key: str,observation_plane_name: str,
    ) -> tuple[str,str]:
        native=str(native_section_key or "").strip()
        if not native:
            raise ValueError("Native section key is required")
        self._require_section(native)
        return native,self._require_plane(observation_plane_name)

    def _require_section(self,section_key: str) -> None:
        if str(section_key) not in tuple(self.session.runtime.section_keys):
            raise KeyError(f"Unknown SLM section {section_key!r}")

    def _require_plane(self,observation_plane_name: str) -> str:
        plane=str(observation_plane_name or "").strip()
        if not plane:
            raise ValueError("Observation plane is required")
        self.observation_planes.get(plane)
        return plane

    def _require_editor_mode(self) -> None:
        if not self.session.editor_writes_allowed:
            raise RuntimeError("Observation registration calibration is available only in editor mode")

    @classmethod
    def _common_site_count(
        cls,
        native: ObservationRegistrationLocalizationSnapshot | None,
        canonical: ObservationRegistrationReferenceSnapshot | None,
    ) -> int:
        if native is None or canonical is None:
            return 0
        try:
            return len(cls._matched_points(native,canonical)[2])
        except (ValueError,RuntimeError):
            return 0

    @staticmethod
    def _matched_points(
        native: ObservationRegistrationLocalizationSnapshot,
        canonical: ObservationRegistrationReferenceSnapshot,
    ) -> tuple[np.ndarray,np.ndarray,tuple[Any,...],dict[str,Any]]:
        """Resolve discrete lattice correspondence before fitting the continuous transform.

        Registration is a continuous detector-coordinate calibration.  A discrete
        Flip-X/rotation relationship between two SLM lattice index conventions must
        therefore be resolved first rather than being absorbed as a reflection in
        the fitted registration itself.
        """
        native_ids=native.geometry.site_ids
        if native_ids is None or canonical.geometry.site_ids is None:
            raise ValueError("Registration geometries require logical site IDs")

        lattice_indices=(
            None if native.geometry.lattice is None
            else np.asarray(native.geometry.lattice.lattice_indices)
        )
        if lattice_indices is None:
            try:
                lattice_indices=np.asarray(native_ids,dtype=np.float64).T
            except (TypeError,ValueError):
                lattice_indices=None
        if lattice_indices is None or lattice_indices.shape!=(2,len(native_ids)):
            raise ValueError(
                "Registration AUTO correspondence requires finite lattice indices"
            )

        # Only measured native sites participate in AUTO scoring and in the final
        # calibration fit. registered_positions_px is not used by the exact-lattice
        # resolver; keeping it equal to measured geometry makes this adapter neutral.
        target=PositionReferenceTarget(
            registered_positions_px=native.geometry.positions_px,
            measured_positions_px=native.geometry.positions_px,
            matched_mask=native.geometry.valid_mask,
            site_ids=native_ids,
            lattice_indices=lattice_indices,
            lattice=native.geometry.lattice,
            metadata={"source":"observation_registration_calibration"},
        )
        resolved=resolve_exact_lattice_correspondence(
            canonical.geometry,target,"auto",
        )
        fit_mask=(
            np.asarray(native.geometry.valid_mask,dtype=bool)
            & np.asarray(resolved.valid_mask,dtype=bool)
            & (np.asarray(resolved.source_indices,dtype=np.int64)>=0)
        )
        if not np.any(fit_mask):
            raise ValueError(
                "Native localization and canonical reference have no common valid lattice sites"
            )
        indices=np.flatnonzero(fit_mask)
        return (
            np.asarray(native.geometry.positions_px[:,indices],dtype=np.float64),
            np.asarray(resolved.desired_positions_px[:,indices],dtype=np.float64),
            tuple(native_ids[index] for index in indices),
            dict(resolved.correspondence or {}),
        )


def _json_site_id(value: Any) -> Any:
    if isinstance(value,tuple):
        return [_json_site_id(item) for item in value]
    if isinstance(value,np.generic):
        return value.item()
    return value


__all__=[
    "ObservationRegistrationCalibrationContext",
    "ObservationRegistrationCalibrationService",
    "ObservationRegistrationFitDiagnostics",
    "ObservationRegistrationLocalizationSnapshot",
    "ObservationRegistrationReferenceSnapshot",
]
