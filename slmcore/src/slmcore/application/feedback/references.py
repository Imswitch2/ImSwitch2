"""Position-reference selection, editing, shared lifetime, persistence and preview."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any,Callable,Mapping,Sequence,TYPE_CHECKING,TypedDict

import numpy as np

from ...core.cgh.feedback import (
    EditablePositionReferenceGeometry,FeedbackCapability,PositionReference,
    PositionReferenceMode,ReferenceCorrespondence,
    editable_position_reference_geometry,
    editable_reference_center_px,editable_reference_positions,
    fit_center_reference_geometry,localization_positions_full_px,
    orient_localization,orientation_reindex_permutation,
    reference_correspondence_options,reference_positions_for_localization,
    resolve_reference_correspondence,
)
from ...core.cgh.localization import LocalizationResult,localization_site_masks
from ...workspace.position_reference_store import (
    PositionReferenceLifetime,PositionReferenceRecord,PositionReferenceStore,
)
from .component import FeedbackComponent

if TYPE_CHECKING:
    from ...core.engine.runtime import SLMRuntime
    from .contracts import SLMFeedbackCallbacks


_EDITABLE_POSITION_REFERENCE_LOCK_FIELDS = frozenset({
    "period_x_px","period_y_px","rotation_deg","lattice_angle_deg",
})
_POSITION_REFERENCE_CORRECTION_MODE_PER_SPOT = "per_spot"
_POSITION_REFERENCE_CORRECTION_MODE_TRANSLATION = "translation"
_POSITION_REFERENCE_CORRECTION_MODE_RIGID = "rigid"
_POSITION_REFERENCE_CORRECTION_MODE_AFFINE = "affine"
_POSITION_REFERENCE_CORRECTION_MODE_POLYNOMIAL = "polynomial"
_POSITION_REFERENCE_CORRECTION_MODES = frozenset({
    _POSITION_REFERENCE_CORRECTION_MODE_PER_SPOT,
    _POSITION_REFERENCE_CORRECTION_MODE_TRANSLATION,
    _POSITION_REFERENCE_CORRECTION_MODE_RIGID,
    _POSITION_REFERENCE_CORRECTION_MODE_AFFINE,
    _POSITION_REFERENCE_CORRECTION_MODE_POLYNOMIAL,
})
_UNSET = object()


def _normalize_editable_locks(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value,Mapping):
        names = {str(name) for name,locked in value.items() if bool(locked)}
    else:
        names = {str(name) for name in value}
    unknown = names-_EDITABLE_POSITION_REFERENCE_LOCK_FIELDS
    if unknown:
        raise ValueError(
            "Unknown editable reference lock(s): %s"
            % ", ".join(sorted(unknown))
        )
    return frozenset(names)


def _normalize_position_reference_correction_mode(value: Any) -> str:
    mode = str(value or _POSITION_REFERENCE_CORRECTION_MODE_PER_SPOT).strip().lower()
    if mode not in _POSITION_REFERENCE_CORRECTION_MODES:
        raise ValueError("Unknown position-reference correction mode %r" % value)
    return mode


def _normalize_position_reference_polynomial_degree(value: Any) -> int:
    degree = int(2 if value is None else value)
    if degree not in (2,3,4):
        raise ValueError("Position-reference polynomial degree must be 2, 3, or 4")
    return degree


def _normalize_affine_transform(value: Any) -> tuple[tuple[float,...],...] | None:
    if value is None:
        return None
    array = np.asarray(value,dtype=np.float64)
    if array.shape != (2,3):
        raise ValueError("Affine position-reference transform must have shape (2, 3)")
    if not np.all(np.isfinite(array)):
        raise ValueError("Affine position-reference transform must be finite")
    linear = array[:,:2]
    if abs(float(np.linalg.det(linear))) < 1e-12:
        raise ValueError("Affine position-reference transform is singular")
    return tuple(tuple(float(item) for item in row) for row in array)

@dataclass(frozen=True)
class PositionReferenceSelection:
    """Transient per-section/plane position-reference choice."""

    mode: PositionReferenceMode=PositionReferenceMode.GLOBAL_FIT
    reference_name: str | None=None
    editable_geometry: EditablePositionReferenceGeometry | None=None
    editable_locks: frozenset[str]=frozenset()
    correspondence: ReferenceCorrespondence=ReferenceCorrespondence.AUTO
    correction_mode: str=_POSITION_REFERENCE_CORRECTION_MODE_PER_SPOT
    polynomial_degree: int=2
    affine_transform: tuple[tuple[float,...],...] | None=None

    def __post_init__(self) -> None:
        mode = PositionReferenceMode.normalize(self.mode)
        reference_name = (
            None
            if self.reference_name is None or not str(self.reference_name).strip()
            else str(self.reference_name).strip()
        )
        geometry = self.editable_geometry
        if geometry is not None and not isinstance(geometry,EditablePositionReferenceGeometry):
            geometry = EditablePositionReferenceGeometry.from_mapping(geometry)
        locks = _normalize_editable_locks(self.editable_locks)
        correspondence = ReferenceCorrespondence.normalize(self.correspondence)
        correction_mode = _normalize_position_reference_correction_mode(
            self.correction_mode
        )
        polynomial_degree = _normalize_position_reference_polynomial_degree(
            self.polynomial_degree
        )
        affine_transform = _normalize_affine_transform(self.affine_transform)
        if mode is PositionReferenceMode.REFERENCE and reference_name is None:
            raise ValueError("Named position-reference mode requires a name")
        object.__setattr__(self,"mode",mode)
        object.__setattr__(self,"reference_name",reference_name)
        object.__setattr__(self,"editable_geometry",geometry)
        object.__setattr__(self,"editable_locks",locks)
        object.__setattr__(self,"correspondence",correspondence)
        object.__setattr__(self,"correction_mode",correction_mode)
        object.__setattr__(self,"polynomial_degree",polynomial_degree)
        object.__setattr__(self,"affine_transform",affine_transform)

@dataclass(frozen=True)
class PositionReferenceContext:
    """UI-facing position-reference state for one feedback section."""

    selection: PositionReferenceSelection
    observation_plane_name: str | None
    references: tuple[PositionReferenceRecord,...]=()
    persistence_available: bool=False
    compatible: bool=True
    compatibility_error: str=""
    change_allowed: bool=True
    change_unavailable_reason: str=""
    create_allowed: bool=False
    create_unavailable_reason: str=""
    fit_geometry: EditablePositionReferenceGeometry | None=None
    correspondence_options: tuple[str,...]=()
    correspondence_resolved: str="canonical"
    correspondence_diagnostics: Mapping[str,Any] | None=None

class PositionReferenceResolutionMetadata(TypedDict,total=False):
    """Description of the detector-space reference used for position feedback."""

    mode: str
    label: str
    geometry: dict[str,Any]
    name: str
    observation_plane_name: str
    created_at: str
    lifetime: str
    correspondence: dict[str,Any]
    correspondence_options: tuple[str,...]

class PositionReferencePreview(TypedDict):
    """Detector-space reference preview with legacy kxy displacement diagnostics."""

    cropped_image: np.ndarray
    measured_positions_px: np.ndarray
    global_positions_px: np.ndarray
    reference_positions_px: np.ndarray
    raw_reference_positions_px: np.ndarray
    reference_center_px: np.ndarray
    fit_center_px: np.ndarray
    fit_geometry: dict[str,Any]
    editable_geometry: dict[str,Any] | None
    ideal_positions_kxy: np.ndarray
    baseline_positions_kxy: np.ndarray
    displacement_kxy: np.ndarray
    correction_mode: str
    polynomial_degree: int
    affine_transform: np.ndarray | None
    reference: PositionReferenceResolutionMetadata

class PositionReferenceService(FeedbackComponent):
    """Own transient reference selection state and shared reference access."""

    def __init__(
        self,
        *,
        runtime_provider: Callable[[], "SLMRuntime"],
        callbacks_provider: Callable[[], "SLMFeedbackCallbacks"],
        editor_writes_allowed: Callable[[], bool],
        active_feedback_plane: Callable[[str],str | None],
        feedback_orientation: Callable[[str],Any],
        current_config_path: Callable[[],str | None],
        store: PositionReferenceStore | None=None,
    ) -> None:
        super().__init__(
            runtime_provider=runtime_provider,
            callbacks_provider=callbacks_provider,
            editor_writes_allowed=editor_writes_allowed,
        )
        self._active_feedback_plane_provider = active_feedback_plane
        self._feedback_orientation = feedback_orientation
        self._current_config_path = current_config_path
        self._position_reference_store = store
        self._position_reference_selections: dict[
            tuple[str,str | None],PositionReferenceSelection
        ] = {}
        if self._position_reference_store is not None:
            self._position_reference_store.add_listener(
                self._on_position_reference_store_changed
            )

    def _active_feedback_plane(self,section_key: str) -> str | None:
        return self._active_feedback_plane_provider(section_key)

    def _position_reference_key(
        self,section_key: str,
    ) -> tuple[str,str | None]:
        section = str(section_key)
        return section,self._active_feedback_plane(section)

    def position_reference_selection(
        self,section_key: str,
    ) -> PositionReferenceSelection:
        """Resolve the transient choice, falling back to global fit if its record is gone."""
        key = self._position_reference_key(section_key)
        selection = self._position_reference_selections.get(key)
        if selection is None:
            selection = PositionReferenceSelection()
            self._position_reference_selections[key] = selection
        if (
            selection.mode is PositionReferenceMode.REFERENCE
            and (
                key[1] is None
                or self._position_reference_store is None
                or not self._position_reference_store.exists(
                    key[1],selection.reference_name
                )
            )
        ):
            selection = PositionReferenceSelection(
                editable_geometry=selection.editable_geometry,
                editable_locks=selection.editable_locks,
                correspondence=selection.correspondence,
                correction_mode=selection.correction_mode,
                polynomial_degree=selection.polynomial_degree,
                affine_transform=selection.affine_transform,
            )
            self._position_reference_selections[key] = selection
        return selection

    def position_reference_context(
        self,section_key: str,
    ) -> PositionReferenceContext:
        """Inspect reference availability and compatibility without applying a correction."""
        section = str(section_key)
        plane = self._active_feedback_plane(section)
        selection = self.position_reference_selection(section)
        references = (
            ()
            if plane is None or self._position_reference_store is None
            else self._position_reference_store.list(plane)
        )
        compatible = True
        compatibility_error = ""
        inspection = self._runtime.get_section_feedback_inspection(section)
        has_localization = bool(
            inspection.measurement is not None
            and inspection.measurement.localization is not None
        )
        resolution_metadata = {}
        try:
            if has_localization:
                _positions,resolution_metadata = self._resolve_position_reference(section)
        except Exception as error:
            compatible = False
            compatibility_error = str(error)

        fit_geometry = None
        if has_localization:
            try:
                localization = orient_localization(
                    inspection.measurement.localization,
                    self._feedback_orientation(section),
                )
                fit_geometry = editable_position_reference_geometry(localization)
            except Exception:
                fit_geometry = None

        create_reason = self._position_reference_create_reason(section)
        return PositionReferenceContext(
            selection=selection,
            observation_plane_name=plane,
            references=tuple(references),
            persistence_available=bool(
                self._position_reference_store is not None
                and self._position_reference_store.persistence_available
            ),
            compatible=compatible,
            compatibility_error=compatibility_error,
            # Reference selection/editing is transient state for the next
            # position-feedback operation.  An applied correction keeps its own
            # provenance and therefore does not lock this state.
            change_allowed=True,
            change_unavailable_reason="",
            create_allowed=not bool(create_reason),
            create_unavailable_reason=create_reason,
            fit_geometry=fit_geometry,
            correspondence_options=tuple(
                resolution_metadata.get("correspondence_options",())
            ),
            correspondence_resolved=str(
                resolution_metadata.get("correspondence",{}).get(
                    "resolved","canonical"
                )
            ),
            correspondence_diagnostics=(
                None
                if not resolution_metadata.get("correspondence")
                else dict(resolution_metadata.get("correspondence") or {})
            ),
        )

    def set_position_reference(
        self,
        section_key: str,
        *,
        mode: PositionReferenceMode | str,
        reference_name: str | None=None,
        editable_geometry: EditablePositionReferenceGeometry | Mapping[str,Any] | None=None,
        editable_locks: Mapping[str,Any] | Sequence[str] | None=None,
        correspondence: ReferenceCorrespondence | str | None=None,
        correction_mode: str | None=None,
        polynomial_degree: int | None=None,
        affine_transform: Any=_UNSET,
    ) -> PositionReferenceSelection:
        """Select a reference for this plane without persisting or applying a correction."""
        self._require_editor_mode()
        section = str(section_key)
        normalized_mode = PositionReferenceMode.normalize(mode)
        previous = self.position_reference_selection(section)
        same_named_reference = bool(
            normalized_mode is PositionReferenceMode.REFERENCE
            and previous.mode is PositionReferenceMode.REFERENCE
            and str(previous.reference_name or "") == str(reference_name or "")
        )
        if editable_geometry is None and previous.editable_geometry is not None:
            editable_geometry = previous.editable_geometry
        if editable_locks is None:
            editable_locks = previous.editable_locks
        if correspondence is None:
            correspondence = (
                previous.correspondence
                if same_named_reference
                else ReferenceCorrespondence.AUTO
            )
        if correction_mode is None:
            correction_mode = (
                previous.correction_mode
                if same_named_reference
                else _POSITION_REFERENCE_CORRECTION_MODE_PER_SPOT
            )
        correction_mode = _normalize_position_reference_correction_mode(
            correction_mode
        )
        if polynomial_degree is None:
            polynomial_degree = previous.polynomial_degree
        polynomial_degree = _normalize_position_reference_polynomial_degree(
            polynomial_degree
        )
        if affine_transform is _UNSET:
            affine_transform = (
                previous.affine_transform
                if same_named_reference
                and correction_mode == previous.correction_mode
                else None
            )
        elif affine_transform is not None:
            affine_transform = _normalize_affine_transform(affine_transform)
        if correction_mode != _POSITION_REFERENCE_CORRECTION_MODE_AFFINE:
            # Manual transforms belong only to affine mode. Polynomial is
            # intentionally automatic-only in this experimental pass.
            affine_transform = None
        if normalized_mode is PositionReferenceMode.EDITABLE and editable_geometry is None:
            inspection = self._runtime.get_section_feedback_inspection(section)
            measurement = inspection.measurement
            if measurement is None or measurement.localization is None:
                raise RuntimeError(
                    "Accept a localization before editing its reference lattice"
                )
            localization = orient_localization(
                measurement.localization,self._feedback_orientation(section),
            )
            editable_geometry = editable_position_reference_geometry(localization)
        selection = PositionReferenceSelection(
            mode=normalized_mode,
            reference_name=reference_name,
            editable_geometry=editable_geometry,
            editable_locks=_normalize_editable_locks(editable_locks),
            correspondence=correspondence,
            correction_mode=correction_mode,
            polynomial_degree=polynomial_degree,
            affine_transform=affine_transform,
        )
        plane = self._active_feedback_plane(section)
        if selection.mode is PositionReferenceMode.REFERENCE:
            if plane is None:
                raise RuntimeError(
                    "Select a measurement plane before using a named position reference."
                )
            if self._position_reference_store is None:
                raise RuntimeError("Position-reference storage is unavailable")
            self._position_reference_store.get(plane,selection.reference_name)
        self._position_reference_selections[(section,plane)] = selection
        self._section_changed(section)
        return selection

    def fit_position_reference_center(
        self,section_key: str,
    ) -> PositionReferenceSelection:
        """Replace the editable reference with a strong center-prioritized fit."""
        self._require_editor_mode()
        section = str(section_key)
        inspection = self._runtime.get_section_feedback_inspection(section)
        measurement = inspection.measurement
        if measurement is None or measurement.localization is None:
            raise RuntimeError(
                "Accept a localization before fitting its reference lattice"
            )
        localization = orient_localization(
            measurement.localization,self._feedback_orientation(section),
        )
        previous = self.position_reference_selection(section)
        geometry = previous.editable_geometry
        if geometry is None:
            geometry = editable_position_reference_geometry(localization)
        locks = previous.editable_locks
        selection = PositionReferenceSelection(
            mode=PositionReferenceMode.EDITABLE,
            editable_geometry=fit_center_reference_geometry(
                localization,geometry=geometry,
                locks={name:(name in locks) for name in _EDITABLE_POSITION_REFERENCE_LOCK_FIELDS},
            ),
            editable_locks=locks,
            correspondence=previous.correspondence,
        )
        plane = self._active_feedback_plane(section)
        self._position_reference_selections[(section,plane)] = selection
        self._section_changed(section)
        return selection

    def _build_position_reference(
        self,section_key: str,name: str,
    ) -> PositionReference:
        section = str(section_key)
        reason = self._position_reference_create_reason(section)
        if reason:
            raise RuntimeError(reason)
        plane = self._active_feedback_plane(section)
        if plane is None:  # guarded above; retained for static clarity.
            raise RuntimeError("Select a measurement plane first")
        measurement = self._runtime.get_section_feedback_inspection(
            section
        ).measurement
        if measurement is None or measurement.localization is None:
            raise RuntimeError("Accept a localization before creating a reference")
        localization = orient_localization(
            measurement.localization,self._feedback_orientation(section),
        )
        config_path = self._current_config_path()
        matched,inferred,_resolved = localization_site_masks(localization)
        return PositionReference(
            name=name,
            observation_plane_name=plane,
            lattice_indices=localization.lattice_indices,
            positions_px=localization_positions_full_px(localization),
            image_shape=measurement.acquisition.image.shape,
            detector_name=measurement.acquisition.detector,
            metadata={
                "source_slm_key":self._runtime.identity.key,
                "source_slm_serial":self._runtime.identity.serial_number,
                "source_section":section,
                "source_target_type":localization.target_type,
                "source_config_name":(
                    None if not config_path else Path(config_path).name
                ),
                "feedback_orientation":self._feedback_orientation(section).value,
                "matched_count":int(np.count_nonzero(matched)),
                "inferred_count":int(np.count_nonzero(inferred)),
            },
        )

    def add_position_reference(
        self,
        section_key: str,
        name: str,
        *,
        lifetime: PositionReferenceLifetime | str=PositionReferenceLifetime.SESSION,
        overwrite: bool=False,
    ) -> PositionReference:
        """Store and select a complete localization; persist only for persistent lifetime."""
        self._require_editor_mode()
        store = self._position_reference_store
        if store is None:
            raise RuntimeError("Position-reference storage is unavailable")
        section = str(section_key)
        reference = self._build_position_reference(section,name)
        store.add(
            reference,lifetime=lifetime,overwrite=bool(overwrite),
        )
        previous = self.position_reference_selection(section)
        plane = self._active_feedback_plane(section)
        self._position_reference_selections[(section,plane)] = (
            PositionReferenceSelection(
                mode=PositionReferenceMode.REFERENCE,
                reference_name=reference.name,
                editable_geometry=previous.editable_geometry,
                editable_locks=previous.editable_locks,
                correspondence=ReferenceCorrespondence.AUTO,
            )
        )
        self._section_changed(section)
        return reference

    def persist_position_reference(
        self,section_key: str,name: str,
    ) -> PositionReferenceRecord:
        """Promote a shared reference to persistent storage without changing selection."""
        self._require_editor_mode()
        plane = self._active_feedback_plane(section_key)
        if plane is None:
            raise RuntimeError("Select a measurement plane first")
        if self._position_reference_store is None:
            raise RuntimeError("Position-reference storage is unavailable")
        return self._position_reference_store.persist(plane,str(name))

    def delete_position_reference(self,section_key: str,name: str) -> None:
        """Delete the active observation plane's shared record and reconcile users."""
        self._require_editor_mode()
        plane = self._active_feedback_plane(section_key)
        if plane is None:
            raise RuntimeError("Select a measurement plane first")
        if self._position_reference_store is None:
            raise RuntimeError("Position-reference storage is unavailable")
        self._position_reference_store.delete(plane,str(name))

    def position_reference_preview(
        self,section_key: str,
    ) -> PositionReferencePreview | None:
        """Return detector-space/kxy preview, or None when unavailable or incompatible."""
        try:
            status = self._runtime.get_section_feedback_status(section_key)
            if not status.localization_available:
                return None
            inspection = self._runtime.get_section_feedback_inspection(section_key)
            measurement = inspection.measurement
            if measurement is None or measurement.localization is None:
                return None
            localization = orient_localization(
                measurement.localization,self._feedback_orientation(section_key),
            )
            (
                reference_positions,raw_reference_positions,metadata,affine_transform,
            ) = self._resolve_position_reference_details(section_key)
            reference = (
                np.asarray(localization.expected_positions_px,dtype=np.float64)
                if reference_positions is None
                else np.asarray(reference_positions,dtype=np.float64)
            )
            raw_reference = (
                np.asarray(reference,dtype=np.float64)
                if raw_reference_positions is None
                else np.asarray(raw_reference_positions,dtype=np.float64)
            )
            analysis = self._runtime.compute_section_feedback_position_analysis(
                section_key,
                orientation=self._feedback_orientation(section_key),
                reference_positions_px=reference_positions,
            )
            selection = self.position_reference_selection(section_key)
            fit_geometry = editable_position_reference_geometry(localization)
            editable_geometry = selection.editable_geometry
            center = (
                editable_reference_center_px(localization,editable_geometry)
                if selection.mode is PositionReferenceMode.EDITABLE
                and editable_geometry is not None
                else np.mean(reference,axis=1)
            )
            return {
                "cropped_image":np.asarray(localization.cropped_image,dtype=np.float64),
                "measured_positions_px":np.asarray(localization.measured_positions_px,dtype=np.float64),
                "global_positions_px":np.asarray(localization.expected_positions_px,dtype=np.float64),
                "reference_positions_px":reference,
                "raw_reference_positions_px":raw_reference,
                "reference_center_px":np.asarray(center,dtype=np.float64),
                "fit_center_px":editable_reference_center_px(
                    localization,fit_geometry,
                ),
                "fit_geometry":fit_geometry.to_dict(),
                "editable_geometry":(
                    None if editable_geometry is None else editable_geometry.to_dict()
                ),
                "ideal_positions_kxy":np.asarray(
                    self._runtime.get_section_fov_mapping_context(
                        section_key
                    )["ideal_positions_kxy"]
                ),
                "baseline_positions_kxy":(
                    np.asarray(analysis.corrected_positions_kxy)
                    - np.asarray(analysis.correction_kxy)
                ),
                "displacement_kxy":np.asarray(analysis.correction_kxy),
                "correction_mode":selection.correction_mode,
                "polynomial_degree":selection.polynomial_degree,
                "affine_transform":(
                    None
                    if affine_transform is None
                    else np.asarray(affine_transform,dtype=np.float64)
                ),
                "reference":metadata,
            }
        except Exception:
            return None

    def _resolve_position_reference(
        self,section_key: str,
    ) -> tuple[np.ndarray | None,dict[str,Any]]:
        """Resolve oriented detector coordinates and provenance; None selects global fit."""
        positions,_raw,metadata,_affine = self._resolve_position_reference_details(
            section_key
        )
        return positions,metadata

    def _resolve_position_reference_details(
        self,section_key: str,
    ) -> tuple[
        np.ndarray | None,np.ndarray | None,dict[str,Any],np.ndarray | None,
    ]:
        """Resolve effective/raw detector references plus the optional affine field."""
        section = str(section_key)
        inspection = self._runtime.get_section_feedback_inspection(section)
        measurement = inspection.measurement
        if measurement is None or measurement.localization is None:
            raise RuntimeError("Accept a localization before using position feedback")
        localization = orient_localization(
            measurement.localization,self._feedback_orientation(section),
        )
        selection = self.position_reference_selection(section)
        if selection.mode is PositionReferenceMode.GLOBAL_FIT:
            return None,None,{"mode":selection.mode.value,"label":"Global fit"},None
        if selection.mode is PositionReferenceMode.EDITABLE:
            geometry = selection.editable_geometry
            if geometry is None:
                geometry = editable_position_reference_geometry(localization)
            positions = editable_reference_positions(localization,geometry)
            return positions,positions,{
                "mode":selection.mode.value,
                "label":"Editable lattice",
                "geometry":geometry.to_dict(),
            },None

        plane = self._active_feedback_plane(section)
        if plane is None:
            raise RuntimeError(
                "Named position references require an active measurement plane"
            )
        if self._position_reference_store is None:
            raise RuntimeError("Position-reference storage is unavailable")
        record = self._position_reference_store.get_record(
            plane,selection.reference_name,
        )
        reference = record.reference
        if tuple(reference.image_shape) != tuple(measurement.acquisition.image.shape):
            raise RuntimeError(
                "Reference image shape %s does not match current measurement %s"
                % (reference.image_shape,measurement.acquisition.image.shape)
            )
        current_detector = str(measurement.acquisition.detector or "").strip() or None
        if (
            reference.detector_name is not None
            and current_detector is not None
            and reference.detector_name != current_detector
        ):
            raise RuntimeError(
                'Reference detector "%s" does not match current detector "%s"'
                % (reference.detector_name,current_detector)
            )
        raw_positions = reference_positions_for_localization(reference,localization)
        source_orientation = dict(reference.metadata or {}).get(
            "feedback_orientation"
        )
        if source_orientation not in (None,""):
            raw_positions = raw_positions[:,orientation_reindex_permutation(
                localization.lattice_indices,
                source_orientation,
                self._feedback_orientation(section),
            )]
        raw_positions,correspondence = resolve_reference_correspondence(
            localization,raw_positions,selection.correspondence,
        )
        effective_positions,affine_transform = self._apply_reference_correction_model(
            localization,raw_positions,selection,
        )
        metadata = {
            "mode":selection.mode.value,
            "label":reference.name,
            "name":reference.name,
            "observation_plane_name":reference.observation_plane_name,
            "created_at":reference.created_at,
            "lifetime":record.lifetime.value,
            "correspondence":correspondence.to_dict(),
            "correspondence_options":tuple(
                item.value for item in reference_correspondence_options(localization)
            ),
            "correction_model":{
                "mode":selection.correction_mode,
                "affine_transform":(
                    None
                    if affine_transform is None
                    else tuple(
                        tuple(float(value) for value in row)
                        for row in np.asarray(affine_transform,dtype=np.float64)
                    )
                ),
                "polynomial_degree":(
                    selection.polynomial_degree
                    if selection.correction_mode
                    == _POSITION_REFERENCE_CORRECTION_MODE_POLYNOMIAL
                    else None
                ),
            },
        }
        return effective_positions,raw_positions,metadata,affine_transform

    def _apply_reference_correction_model(
        self,
        localization: LocalizationResult,
        raw_reference_positions: np.ndarray,
        selection: PositionReferenceSelection,
    ) -> tuple[np.ndarray,np.ndarray | None]:
        """Convert raw saved-reference errors into the selected correction field.

        ``per_spot`` preserves the historical behavior. ``translation`` fits one
        common detector-space shift, ``rigid`` fits translation + rotation, and
        ``affine`` fits one general linear displacement field. ``polynomial`` fits
        a detector-space polynomial displacement field of selectable total degree
        2, 3, or 4. Constrained models are sampled at every registered lattice
        site, so individual sites cannot move independently.
        """
        raw_reference = np.asarray(raw_reference_positions,dtype=np.float64)
        if selection.correction_mode == _POSITION_REFERENCE_CORRECTION_MODE_PER_SPOT:
            return raw_reference,None

        registered = np.asarray(
            localization.expected_positions_px,dtype=np.float64,
        )
        measured = np.asarray(
            localization.measured_positions_px,dtype=np.float64,
        )
        matched,_inferred,_resolved = localization_site_masks(localization)
        matched_count = int(np.count_nonzero(matched))
        displacement = raw_reference-measured

        if selection.correction_mode == _POSITION_REFERENCE_CORRECTION_MODE_TRANSLATION:
            if matched_count < 1:
                raise RuntimeError(
                    "Translation reference correction requires at least one matched spot."
                )
            common_error = np.mean(displacement[:,matched],axis=1)
            return measured+common_error[:,None],None

        if selection.correction_mode == _POSITION_REFERENCE_CORRECTION_MODE_RIGID:
            if matched_count < 2:
                raise RuntimeError(
                    "Rigid reference correction requires at least two matched spots."
                )
            desired_registered = registered+displacement
            transform = _fit_rigid_matrix(
                registered[:,matched],desired_registered[:,matched],
            )
            mapped_registered = _apply_affine_matrix(transform,registered)
            rigid_error = mapped_registered-registered
            return measured+rigid_error,None

        if selection.correction_mode == _POSITION_REFERENCE_CORRECTION_MODE_POLYNOMIAL:
            degree = selection.polynomial_degree
            minimum_points = _polynomial_term_count(degree)
            if matched_count < minimum_points:
                raise RuntimeError(
                    "Polynomial reference correction degree %d requires at least %d "
                    "matched spots; got %d."
                    % (degree,minimum_points,matched_count)
                )
            polynomial = _fit_polynomial_displacement(
                registered[:,matched],displacement[:,matched],degree=degree,
            )
            smooth_error = _apply_polynomial_displacement(polynomial,registered)
            return measured+smooth_error,None

        transform = (
            None
            if selection.affine_transform is None
            else np.asarray(selection.affine_transform,dtype=np.float64)
        )
        if transform is None:
            if int(np.count_nonzero(matched)) < 3:
                raise RuntimeError(
                    "Affine reference correction requires at least three matched spots."
                )
            desired_registered = registered + (raw_reference-measured)
            transform = _fit_affine_matrix(
                registered[:,matched],desired_registered[:,matched],
            )

        mapped_registered = _apply_affine_matrix(transform,registered)
        affine_error = mapped_registered-registered
        # analyze_position() expects an effective detector-space reference and
        # converts (reference - measured) to the SLM correction. Adding the
        # shared constrained error to each measured point retains that API.
        return measured+affine_error,transform

    def _position_reference_create_reason(self,section_key: str) -> str:
        status = self._runtime.get_section_feedback_status(section_key)
        if FeedbackCapability.POSITION_CORRECTION not in set(status.capabilities):
            return "Position references are unavailable for this target."
        if self._position_reference_store is None:
            return "Position-reference storage is unavailable."
        if self._active_feedback_plane(section_key) is None:
            return "Select a measurement plane before creating a position reference."
        inspection = self._runtime.get_section_feedback_inspection(section_key)
        measurement = inspection.measurement
        if measurement is None or measurement.localization is None:
            return "Accept a localization before creating a position reference."
        if not _localization_is_resolved(measurement.localization):
            return "Resolve every target spot before creating a reference; use Infer missing for unmatched sites."
        return ""

    def _on_position_reference_store_changed(self) -> None:
        runtime = self._runtime
        for section in tuple(runtime.section_keys):
            key = self._position_reference_key(section)
            selection = self._position_reference_selections.get(key)
            if selection is not None and selection.mode is PositionReferenceMode.REFERENCE:
                plane = key[1]
                if (
                    plane is None
                    or self._position_reference_store is None
                    or not self._position_reference_store.exists(
                        plane,selection.reference_name
                    )
                ):
                    self._position_reference_selections[key] = PositionReferenceSelection()
            self._section_changed(section)

    def refresh_editable_geometry_after_localization(
        self,section_key: str,localization: LocalizationResult,
    ) -> None:
        """Refresh transient reference edits after accepting a new localization."""
        key = self._position_reference_key(section_key)
        selection = self._position_reference_selections.get(key)
        if selection is None:
            return
        if selection.mode is PositionReferenceMode.REFERENCE:
            if selection.affine_transform is None:
                return
            # Manual affine handles describe the previous localization. Keep the
            # selected model/reference but refit its base transform to the new
            # measurement before the user makes any further manual adjustment.
            self._position_reference_selections[key] = PositionReferenceSelection(
                mode=selection.mode,
                reference_name=selection.reference_name,
                editable_geometry=selection.editable_geometry,
                editable_locks=selection.editable_locks,
                correspondence=selection.correspondence,
                correction_mode=selection.correction_mode,
                polynomial_degree=selection.polynomial_degree,
                affine_transform=None,
            )
            return
        if selection.mode is not PositionReferenceMode.EDITABLE:
            return
        oriented = orient_localization(
            localization,self._feedback_orientation(section_key),
        )
        self._position_reference_selections[key] = PositionReferenceSelection(
            mode=PositionReferenceMode.EDITABLE,
            editable_geometry=editable_position_reference_geometry(oriented),
            correspondence=selection.correspondence,
            correction_mode=selection.correction_mode,
            polynomial_degree=selection.polynomial_degree,
        )

    def dispose(self) -> None:
        if self._position_reference_store is not None:
            self._position_reference_store.remove_listener(
                self._on_position_reference_store_changed
            )
        self._position_reference_selections.clear()


def _localization_is_resolved(localization: LocalizationResult) -> bool:
    _matched,_inferred,resolved = localization_site_masks(localization)
    return bool(np.all(resolved))


def _fit_rigid_matrix(source: np.ndarray,target: np.ndarray) -> np.ndarray:
    source = np.asarray(source,dtype=np.float64)
    target = np.asarray(target,dtype=np.float64)
    if source.shape != target.shape or source.ndim != 2 or source.shape[0] != 2:
        raise ValueError("Rigid point arrays must share shape (2, N)")
    if source.shape[1] < 2:
        raise ValueError("At least two points are required for a rigid fit")
    source_center = np.mean(source,axis=1)
    target_center = np.mean(target,axis=1)
    source_zero = source-source_center[:,None]
    target_zero = target-target_center[:,None]
    if float(np.linalg.norm(source_zero)) < 1e-12:
        raise ValueError("Rigid source points are degenerate")
    covariance = target_zero@source_zero.T
    u,_singular_values,vt = np.linalg.svd(covariance)
    rotation = u@vt
    if float(np.linalg.det(rotation)) < 0.0:
        u[:,-1] *= -1.0
        rotation = u@vt
    translation = target_center-rotation@source_center
    return np.column_stack([rotation,translation])


def _fit_affine_matrix(source: np.ndarray,target: np.ndarray) -> np.ndarray:
    source = np.asarray(source,dtype=np.float64)
    target = np.asarray(target,dtype=np.float64)
    if source.shape != target.shape or source.ndim != 2 or source.shape[0] != 2:
        raise ValueError("Affine point arrays must share shape (2, N)")
    if source.shape[1] < 3:
        raise ValueError("At least three points are required for an affine fit")
    design = np.column_stack([
        source.T,np.ones(source.shape[1],dtype=np.float64),
    ])
    coefficients,_,rank,_ = np.linalg.lstsq(design,target.T,rcond=None)
    if int(rank) < 3:
        raise ValueError("Affine reference fit is rank-deficient")
    transform = coefficients.T
    if abs(float(np.linalg.det(transform[:,:2]))) < 1e-12:
        raise ValueError("Affine reference fit is singular")
    return transform


def _apply_affine_matrix(transform: np.ndarray,positions: np.ndarray) -> np.ndarray:
    matrix = np.asarray(transform,dtype=np.float64)
    points = np.asarray(positions,dtype=np.float64)
    if matrix.shape != (2,3):
        raise ValueError("Affine transform must have shape (2, 3)")
    if points.ndim != 2 or points.shape[0] != 2:
        raise ValueError("Affine positions must have shape (2, N)")
    return matrix[:,:2]@points + matrix[:,2,None]


def _polynomial_term_count(degree: int) -> int:
    degree = _normalize_position_reference_polynomial_degree(degree)
    return (degree+1)*(degree+2)//2


def _polynomial_design(
    positions: np.ndarray,
    *,
    center: np.ndarray,
    scale: np.ndarray,
    degree: int,
) -> np.ndarray:
    points = np.asarray(positions,dtype=np.float64)
    if points.ndim != 2 or points.shape[0] != 2:
        raise ValueError("Polynomial positions must have shape (2, N)")
    degree = _normalize_position_reference_polynomial_degree(degree)
    normalized = (points-center[:,None])/scale[:,None]
    x,y = normalized
    terms = [np.ones(points.shape[1],dtype=np.float64)]
    for total_degree in range(1,degree+1):
        for y_power in range(total_degree+1):
            x_power = total_degree-y_power
            terms.append((x**x_power)*(y**y_power))
    return np.column_stack(terms)


def _fit_polynomial_displacement(
    source: np.ndarray,displacement: np.ndarray,*,degree: int=2,
) -> tuple[np.ndarray,np.ndarray,np.ndarray,int]:
    source = np.asarray(source,dtype=np.float64)
    displacement = np.asarray(displacement,dtype=np.float64)
    degree = _normalize_position_reference_polynomial_degree(degree)
    if (
        source.shape != displacement.shape
        or source.ndim != 2
        or source.shape[0] != 2
    ):
        raise ValueError("Polynomial point arrays must share shape (2, N)")
    term_count = _polynomial_term_count(degree)
    if source.shape[1] < term_count:
        raise ValueError(
            "At least %d points are required for a degree-%d polynomial fit"
            % (term_count,degree)
        )
    center = np.mean(source,axis=1)
    scale = np.ptp(source,axis=1)
    if np.any(scale <= 1e-12):
        raise ValueError("Polynomial reference fit has degenerate coordinates")
    design = _polynomial_design(
        source,center=center,scale=scale,degree=degree,
    )
    coefficients,_,rank,_ = np.linalg.lstsq(design,displacement.T,rcond=None)
    if int(rank) < term_count:
        raise ValueError("Polynomial reference fit is rank-deficient")
    # Store output as 2 x terms. Coordinates are normalized before fitting to
    # avoid poor conditioning from high powers of detector-pixel coordinates.
    return coefficients.T,center,scale,degree


def _apply_polynomial_displacement(
    polynomial: tuple[np.ndarray,np.ndarray,np.ndarray,int],
    positions: np.ndarray,
) -> np.ndarray:
    coefficients,center,scale,degree = polynomial
    degree = _normalize_position_reference_polynomial_degree(degree)
    matrix = np.asarray(coefficients,dtype=np.float64)
    term_count = _polynomial_term_count(degree)
    if matrix.shape != (2,term_count):
        raise ValueError(
            "Polynomial coefficients must have shape (2, %d)" % term_count
        )
    design = _polynomial_design(
        positions,
        center=np.asarray(center,dtype=np.float64).reshape(2),
        scale=np.asarray(scale,dtype=np.float64).reshape(2),
        degree=degree,
    )
    return matrix@design.T


def _fit_quadratic_displacement(
    source: np.ndarray,displacement: np.ndarray,
) -> tuple[np.ndarray,np.ndarray,np.ndarray,int]:
    """Backward-compatible degree-2 wrapper for the experimental helper."""
    return _fit_polynomial_displacement(source,displacement,degree=2)


def _apply_quadratic_displacement(
    polynomial: tuple[np.ndarray,np.ndarray,np.ndarray,int],
    positions: np.ndarray,
) -> np.ndarray:
    """Backward-compatible degree-2 wrapper for the experimental helper."""
    return _apply_polynomial_displacement(polynomial,positions)
