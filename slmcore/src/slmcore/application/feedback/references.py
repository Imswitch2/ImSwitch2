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
from ..position_reference_store import (
    PositionReferenceLifetime,PositionReferenceRecord,PositionReferenceStore,
)
from .component import FeedbackComponent

if TYPE_CHECKING:
    from ...core.engine.runtime import SLMRuntime
    from .contracts import SLMFeedbackCallbacks


_EDITABLE_POSITION_REFERENCE_LOCK_FIELDS = frozenset({
    "period_x_px","period_y_px","rotation_deg","lattice_angle_deg",
})


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

@dataclass(frozen=True)
class PositionReferenceSelection:
    """Transient per-section/plane position-reference choice."""

    mode: PositionReferenceMode=PositionReferenceMode.GLOBAL_FIT
    reference_name: str | None=None
    editable_geometry: EditablePositionReferenceGeometry | None=None
    editable_locks: frozenset[str]=frozenset()
    correspondence: ReferenceCorrespondence=ReferenceCorrespondence.AUTO

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
        if mode is PositionReferenceMode.REFERENCE and reference_name is None:
            raise ValueError("Named position-reference mode requires a name")
        object.__setattr__(self,"mode",mode)
        object.__setattr__(self,"reference_name",reference_name)
        object.__setattr__(self,"editable_geometry",geometry)
        object.__setattr__(self,"editable_locks",locks)
        object.__setattr__(self,"correspondence",correspondence)

@dataclass(frozen=True)
class PositionReferenceContext:
    """UI-facing position-reference state for one feedback section."""

    selection: PositionReferenceSelection
    plane_name: str | None
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
    plane_name: str
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
    reference_center_px: np.ndarray
    fit_center_px: np.ndarray
    fit_geometry: dict[str,Any]
    editable_geometry: dict[str,Any] | None
    ideal_positions_kxy: np.ndarray
    baseline_positions_kxy: np.ndarray
    displacement_kxy: np.ndarray
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
            plane_name=plane,
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
    ) -> PositionReferenceSelection:
        """Select a reference for this plane without persisting or applying a correction."""
        self._require_editor_mode()
        section = str(section_key)
        normalized_mode = PositionReferenceMode.normalize(mode)
        previous = self.position_reference_selection(section)
        if editable_geometry is None and previous.editable_geometry is not None:
            editable_geometry = previous.editable_geometry
        if editable_locks is None:
            editable_locks = previous.editable_locks
        if correspondence is None:
            correspondence = (
                previous.correspondence
                if normalized_mode is PositionReferenceMode.REFERENCE
                and previous.mode is PositionReferenceMode.REFERENCE
                and str(previous.reference_name or "") == str(reference_name or "")
                else ReferenceCorrespondence.AUTO
            )
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
            plane_name=plane,
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
        """Delete the active plane's shared record and let store listeners reconcile users."""
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
            reference_positions,metadata = self._resolve_position_reference(section_key)
            reference = (
                np.asarray(localization.expected_positions_px,dtype=np.float64)
                if reference_positions is None
                else np.asarray(reference_positions,dtype=np.float64)
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
                "reference_center_px":np.asarray(center,dtype=np.float64),
                "fit_center_px":editable_reference_center_px(
                    localization,fit_geometry,
                ),
                "fit_geometry":fit_geometry.to_dict(),
                "editable_geometry":(
                    None if editable_geometry is None else editable_geometry.to_dict()
                ),
                "ideal_positions_kxy":np.asarray(
                    self._runtime.get_section_fov_position_context(
                        section_key
                    )["ideal_positions_kxy"]
                ),
                "baseline_positions_kxy":(
                    np.asarray(analysis.corrected_positions_kxy)
                    - np.asarray(analysis.correction_kxy)
                ),
                "displacement_kxy":np.asarray(analysis.correction_kxy),
                "reference":metadata,
            }
        except Exception:
            return None

    def _resolve_position_reference(
        self,section_key: str,
    ) -> tuple[np.ndarray | None,dict[str,Any]]:
        """Resolve oriented detector coordinates and provenance; None selects global fit."""
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
            return None,{"mode":selection.mode.value,"label":"Global fit"}
        if selection.mode is PositionReferenceMode.EDITABLE:
            geometry = selection.editable_geometry
            if geometry is None:
                geometry = editable_position_reference_geometry(localization)
            return editable_reference_positions(localization,geometry),{
                "mode":selection.mode.value,
                "label":"Editable lattice",
                "geometry":geometry.to_dict(),
            }

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
        positions = reference_positions_for_localization(reference,localization)
        source_orientation = dict(reference.metadata or {}).get(
            "feedback_orientation"
        )
        if source_orientation not in (None,""):
            positions = positions[:,orientation_reindex_permutation(
                localization.lattice_indices,
                source_orientation,
                self._feedback_orientation(section),
            )]
        positions,correspondence = resolve_reference_correspondence(
            localization,positions,selection.correspondence,
        )
        return positions,{
            "mode":selection.mode.value,
            "label":reference.name,
            "name":reference.name,
            "plane_name":reference.plane_name,
            "created_at":reference.created_at,
            "lifetime":record.lifetime.value,
            "correspondence":correspondence.to_dict(),
            "correspondence_options":tuple(
                item.value for item in reference_correspondence_options(localization)
            ),
        }

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
        """Reset an active editable reference to the newly accepted fit geometry."""
        key = self._position_reference_key(section_key)
        selection = self._position_reference_selections.get(key)
        if selection is None or selection.mode is not PositionReferenceMode.EDITABLE:
            return
        oriented = orient_localization(
            localization,self._feedback_orientation(section_key),
        )
        self._position_reference_selections[key] = PositionReferenceSelection(
            mode=PositionReferenceMode.EDITABLE,
            editable_geometry=editable_position_reference_geometry(oriented),
            correspondence=selection.correspondence,
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
