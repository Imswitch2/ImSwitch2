"""Position-reference selection, editing, shared lifetime, persistence and preview."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any,Callable,Mapping,Sequence,TYPE_CHECKING,TypedDict

import numpy as np

from ...core.cgh.feedback import (
    EditablePositionReferenceGeometry,FeedbackCapability,FeedbackOrientation,PositionReference,
    PositionReferenceMode,ReferenceCorrespondence,
    editable_position_reference_geometry,
    editable_reference_center_px,editable_reference_positions,
    fit_center_reference_geometry,localization_positions_full_px,
    orient_lattice_representation,orient_localization,orientation_permutation,
    orientation_reindex_permutation,
    reference_correspondence_options,
)
from ...core.cgh.localization import (
    LocalizationResult,SpotOrdering,localization_site_masks,spot_ordering_permutation,
)
from ...core.measurement.position_alignment import (
    PositionAlignmentResult,align_resolved_position_reference,
)
from ...core.measurement.position_reference import (
    ReferenceGeometry,position_reference_target_from_localization,
    reference_geometry_from_current_target,reference_geometry_from_editable_lattice,
    reference_geometry_from_global_fit,reference_geometry_from_localization,
    reference_geometry_from_position_reference,resolve_exact_lattice,
    resolve_exact_lattice_correspondence,
)
from ...core.transform2d import (
    FittedTransform2D,TRANSFORM_AFFINE,TRANSFORM_POLYNOMIAL,TRANSFORM_RIGID,
    TRANSFORM_TRANSLATION,fitted_matrix_transform,normalize_polynomial_degree,
)
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
_POSITION_REFERENCE_CORRECTION_MODE_TRANSLATION = TRANSFORM_TRANSLATION
_POSITION_REFERENCE_CORRECTION_MODE_RIGID = TRANSFORM_RIGID
_POSITION_REFERENCE_CORRECTION_MODE_AFFINE = TRANSFORM_AFFINE
_POSITION_REFERENCE_CORRECTION_MODE_POLYNOMIAL = TRANSFORM_POLYNOMIAL
_POSITION_REFERENCE_CORRECTION_MODES = frozenset({
    _POSITION_REFERENCE_CORRECTION_MODE_PER_SPOT,
    _POSITION_REFERENCE_CORRECTION_MODE_TRANSLATION,
    _POSITION_REFERENCE_CORRECTION_MODE_RIGID,
    _POSITION_REFERENCE_CORRECTION_MODE_AFFINE,
    _POSITION_REFERENCE_CORRECTION_MODE_POLYNOMIAL,
})
_UNSET = object()


def _spot_ordering_for_permutation(
    lattice_indices: np.ndarray,permutation: np.ndarray,
) -> SpotOrdering:
    """Identify the D4 ordering represented by one rectangular permutation."""
    expected=np.asarray(permutation,dtype=np.int64)
    for ordering in SpotOrdering:
        try:
            candidate=spot_ordering_permutation(lattice_indices,ordering)
        except ValueError:
            continue
        if np.array_equal(candidate,expected):
            return ordering
    raise ValueError("Feedback-orientation reindex is not a supported lattice ordering")


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
    return normalize_polynomial_degree(
        2 if value is None else value,allow_legacy_linear=False,
    )


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


def _normalize_manual_transform(
    value: Any,*,model: str,polynomial_degree: int,
) -> FittedTransform2D | None:
    if value is None:
        return None
    transform=(
        value if isinstance(value,FittedTransform2D)
        else FittedTransform2D.from_dict(value)
    )
    normalized_model=_normalize_position_reference_correction_mode(model)
    if normalized_model==_POSITION_REFERENCE_CORRECTION_MODE_PER_SPOT:
        raise ValueError("Per-spot correction cannot use a manual transform")
    if transform.model!=normalized_model:
        raise ValueError("Manual transform model must match correction model")
    if (
        normalized_model==_POSITION_REFERENCE_CORRECTION_MODE_POLYNOMIAL
        and int(transform.polynomial_degree)!=int(polynomial_degree)
    ):
        raise ValueError("Manual polynomial transform order must match correction order")
    return transform

@dataclass(frozen=True)
class PositionReferenceSourceSelection:
    """Transient choice of where desired detector positions come from."""

    mode: PositionReferenceMode=PositionReferenceMode.GLOBAL_FIT
    reference_name: str | None=None
    editable_geometry: EditablePositionReferenceGeometry | None=None
    editable_locks: frozenset[str]=frozenset()
    correspondence: ReferenceCorrespondence=ReferenceCorrespondence.AUTO

    def __post_init__(self) -> None:
        mode=PositionReferenceMode.normalize(self.mode)
        name=(
            None if self.reference_name is None or not str(self.reference_name).strip()
            else str(self.reference_name).strip()
        )
        geometry=self.editable_geometry
        if geometry is not None and not isinstance(geometry,EditablePositionReferenceGeometry):
            geometry=EditablePositionReferenceGeometry.from_mapping(geometry)
        if mode is PositionReferenceMode.REFERENCE and name is None:
            raise ValueError("Named position-reference mode requires a name")
        object.__setattr__(self,"mode",mode)
        object.__setattr__(self,"reference_name",name)
        object.__setattr__(self,"editable_geometry",geometry)
        object.__setattr__(self,"editable_locks",_normalize_editable_locks(self.editable_locks))
        object.__setattr__(self,"correspondence",ReferenceCorrespondence.normalize(self.correspondence))


@dataclass(frozen=True)
class PositionCorrectionSelection:
    """Transient choice of how desired positions become a correction field."""

    model: str=_POSITION_REFERENCE_CORRECTION_MODE_PER_SPOT
    polynomial_degree: int=2
    manual_transform: FittedTransform2D | None=None

    def __post_init__(self) -> None:
        model=_normalize_position_reference_correction_mode(self.model)
        degree=_normalize_position_reference_polynomial_degree(self.polynomial_degree)
        transform=self.manual_transform
        if transform is not None:
            if not isinstance(transform,FittedTransform2D):
                raise TypeError("manual_transform must be a FittedTransform2D")
            if transform.model!=model:
                raise ValueError("Manual transform model must match correction model")
        object.__setattr__(self,"model",model)
        object.__setattr__(self,"polynomial_degree",degree)


def _automatic_correction_selection(
    selection: "PositionReferenceSelection",
) -> PositionCorrectionSelection:
    """Preserve correction model settings while discarding source-bound manual edits."""
    return PositionCorrectionSelection(
        model=selection.correction_mode,
        polynomial_degree=selection.polynomial_degree,
    )


@dataclass(frozen=True,init=False)
class PositionReferenceSelection:
    """Compatibility facade over separate source and correction selections."""

    source: PositionReferenceSourceSelection
    correction: PositionCorrectionSelection

    def __init__(
        self,
        mode: PositionReferenceMode | str=PositionReferenceMode.GLOBAL_FIT,
        reference_name: str | None=None,
        editable_geometry: EditablePositionReferenceGeometry | Mapping[str,Any] | None=None,
        editable_locks: Mapping[str,Any] | Sequence[str] | None=None,
        correspondence: ReferenceCorrespondence | str=ReferenceCorrespondence.AUTO,
        correction_mode: str=_POSITION_REFERENCE_CORRECTION_MODE_PER_SPOT,
        polynomial_degree: int=2,
        affine_transform: Any=None,
        manual_transform: Any=None,
        *,
        source: PositionReferenceSourceSelection | None=None,
        correction: PositionCorrectionSelection | None=None,
    ) -> None:
        if source is None:
            source=PositionReferenceSourceSelection(
                mode=mode,reference_name=reference_name,editable_geometry=editable_geometry,
                editable_locks=_normalize_editable_locks(editable_locks),
                correspondence=correspondence,
            )
        elif not isinstance(source,PositionReferenceSourceSelection):
            raise TypeError("source must be a PositionReferenceSourceSelection")
        if correction is None:
            model=_normalize_position_reference_correction_mode(correction_mode)
            degree=_normalize_position_reference_polynomial_degree(polynomial_degree)
            if affine_transform is not None and manual_transform is not None:
                raise ValueError("Specify either affine_transform or manual_transform, not both")
            manual=None
            if affine_transform is not None:
                if model!=TRANSFORM_AFFINE:
                    raise ValueError("affine_transform is only valid for affine correction mode")
                manual=fitted_matrix_transform(
                    TRANSFORM_AFFINE,np.asarray(_normalize_affine_transform(affine_transform)),
                )
            elif manual_transform is not None:
                manual=_normalize_manual_transform(
                    manual_transform,model=model,polynomial_degree=degree,
                )
            correction=PositionCorrectionSelection(
                model=model,polynomial_degree=degree,manual_transform=manual,
            )
        elif not isinstance(correction,PositionCorrectionSelection):
            raise TypeError("correction must be a PositionCorrectionSelection")
        object.__setattr__(self,"source",source)
        object.__setattr__(self,"correction",correction)

    @property
    def mode(self): return self.source.mode
    @property
    def reference_name(self): return self.source.reference_name
    @property
    def editable_geometry(self): return self.source.editable_geometry
    @property
    def editable_locks(self): return self.source.editable_locks
    @property
    def correspondence(self): return self.source.correspondence
    @property
    def correction_mode(self): return self.correction.model
    @property
    def polynomial_degree(self): return self.correction.polynomial_degree
    @property
    def affine_transform(self):
        transform=self.correction.manual_transform
        if transform is None or transform.model!=TRANSFORM_AFFINE:
            return None
        return tuple(tuple(float(v) for v in row) for row in np.asarray(transform.matrix))
    @property
    def manual_transform(self):
        return self.correction.manual_transform

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
    crop_offset_px: np.ndarray
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
    fitted_transform: dict[str,Any] | None
    manual_transform: dict[str,Any] | None
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
        self._position_alignment_results: dict[
            tuple[str,str | None],PositionAlignmentResult
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
                correction=_automatic_correction_selection(selection),
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
        manual_transform: Any=_UNSET,
        notify: bool=True,
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
        same_source = bool(
            normalized_mode is previous.mode
            and (
                normalized_mode is not PositionReferenceMode.REFERENCE
                or same_named_reference
            )
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
            correction_mode = previous.correction_mode
        correction_mode = _normalize_position_reference_correction_mode(
            correction_mode
        )
        if polynomial_degree is None:
            polynomial_degree = previous.polynomial_degree
        polynomial_degree = _normalize_position_reference_polynomial_degree(
            polynomial_degree
        )
        if manual_transform is not _UNSET and affine_transform is not _UNSET:
            raise ValueError("Specify either affine_transform or manual_transform, not both")
        if affine_transform is not _UNSET:
            manual_transform=(
                None
                if affine_transform is None
                else fitted_matrix_transform(
                    TRANSFORM_AFFINE,
                    np.asarray(_normalize_affine_transform(affine_transform)),
                )
            )
        elif manual_transform is _UNSET:
            manual_transform=(
                previous.manual_transform
                if same_source
                and correction_mode == previous.correction_mode
                and polynomial_degree == previous.polynomial_degree
                else None
            )
        else:
            manual_transform=_normalize_manual_transform(
                manual_transform,model=correction_mode,
                polynomial_degree=polynomial_degree,
            )
        if correction_mode == _POSITION_REFERENCE_CORRECTION_MODE_PER_SPOT:
            manual_transform=None
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
            manual_transform=manual_transform,
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
        if bool(notify):
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
            correction=_automatic_correction_selection(previous),
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
        source_target=position_reference_target_from_localization(
            localization,full_image=True,
        )
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
                "lattice_representation":(
                    None if source_target.lattice is None
                    else source_target.lattice.to_dict()
                ),
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
                correction=_automatic_correction_selection(previous),
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
                reference_positions,raw_reference_positions,metadata,fitted_transform,
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
                "crop_offset_px":np.asarray(
                    [float(localization.crop_coord[2]),float(localization.crop_coord[0])],
                    dtype=np.float64,
                ),
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
                    if fitted_transform is None
                    or fitted_transform.model!=TRANSFORM_AFFINE
                    else np.asarray(fitted_transform.matrix,dtype=np.float64)
                ),
                "fitted_transform":(
                    None if fitted_transform is None else fitted_transform.to_dict()
                ),
                "manual_transform":(
                    None
                    if selection.manual_transform is None
                    else selection.manual_transform.to_dict()
                ),
                "reference":metadata,
            }
        except Exception:
            return None

    def _resolve_position_reference(
        self,section_key: str,
    ) -> tuple[np.ndarray,dict[str,Any]]:
        """Resolve oriented detector coordinates and provenance for position feedback."""
        positions,_raw,metadata,_transform = self._resolve_position_reference_details(
            section_key
        )
        return positions,metadata

    def _resolve_position_reference_details(
        self,section_key: str,
    ) -> tuple[
        np.ndarray,np.ndarray,dict[str,Any],FittedTransform2D | None,
    ]:
        """Resolve source geometry and correction through the neutral backend core."""
        section=str(section_key)
        inspection=self._runtime.get_section_feedback_inspection(section)
        measurement=inspection.measurement
        if measurement is None or measurement.localization is None:
            raise RuntimeError("Accept a localization before using position feedback")
        localization=orient_localization(
            measurement.localization,self._feedback_orientation(section),
        )
        selection=self.position_reference_selection(section)
        target=position_reference_target_from_localization(localization,full_image=True)
        crop_offset=np.asarray(
            [[float(localization.crop_coord[2])],[float(localization.crop_coord[0])]],
            dtype=np.float64,
        )
        key=self._position_reference_key(section)

        metadata: dict[str,Any]={
            "mode":selection.mode.value,
            "correspondence_options":(),
        }
        if selection.mode is PositionReferenceMode.GLOBAL_FIT:
            source=reference_geometry_from_global_fit(localization,full_image=True)
            resolved=resolve_exact_lattice(source,target)
            metadata["label"]="Global fit"
        elif selection.mode is PositionReferenceMode.CURRENT:
            source=reference_geometry_from_current_target(target)
            resolved=resolve_exact_lattice(source,target)
            metadata["label"]="Current (free edit)"
        elif selection.mode is PositionReferenceMode.EDITABLE:
            geometry=selection.editable_geometry
            if geometry is None:
                geometry=editable_position_reference_geometry(localization)
            source=reference_geometry_from_editable_lattice(
                localization,geometry,full_image=True,
            )
            resolved=resolve_exact_lattice(source,target)
            metadata.update({
                "label":"Editable lattice",
                "geometry":geometry.to_dict(),
            })
        else:
            plane=self._active_feedback_plane(section)
            if plane is None:
                raise RuntimeError(
                    "Named position references require an active measurement plane"
                )
            if self._position_reference_store is None:
                raise RuntimeError("Position-reference storage is unavailable")
            record=self._position_reference_store.get_record(
                plane,selection.reference_name
            )
            reference=record.reference
            if tuple(reference.image_shape)!=tuple(measurement.acquisition.image.shape):
                raise RuntimeError(
                    "Reference image shape %s does not match current measurement %s"
                    % (reference.image_shape,measurement.acquisition.image.shape)
                )
            current_detector=str(measurement.acquisition.detector or "").strip() or None
            if (
                reference.detector_name is not None and current_detector is not None
                and reference.detector_name!=current_detector
            ):
                raise RuntimeError(
                    'Reference detector "%s" does not match current detector "%s"'
                    % (reference.detector_name,current_detector)
                )
            if not np.array_equal(
                reference.lattice_indices,localization.lattice_indices
            ):
                raise ValueError("Reference lattice does not match the current target")

            source=reference_geometry_from_position_reference(reference)
            source_orientation=dict(reference.metadata or {}).get(
                "feedback_orientation"
            )
            source_ordering=SpotOrdering.CANONICAL
            if source.lattice is not None and source_orientation not in (None,""):
                # Older topology-aware references can carry detector positions
                # that were oriented without permuting the persisted topology.
                normalized_orientation=FeedbackOrientation.normalize(
                    source_orientation
                )
                lattice_orientation=dict(source.lattice.diagnostics or {}).get(
                    "feedback_orientation"
                )
                if (
                    normalized_orientation is not FeedbackOrientation.IDENTITY
                    and lattice_orientation!=normalized_orientation.value
                ):
                    permutation=orientation_permutation(
                        reference.lattice_indices,normalized_orientation,
                    )
                    source=ReferenceGeometry(
                        positions_px=source.positions_px,site_ids=source.site_ids,
                        lattice=orient_lattice_representation(
                            source.lattice,permutation,normalized_orientation,
                        ),
                        valid_mask=source.valid_mask,weights=source.weights,
                        metadata=dict(source.metadata),
                    )
            if source.lattice is None and source_orientation not in (None,""):
                permutation=orientation_reindex_permutation(
                    localization.lattice_indices,source_orientation,
                    self._feedback_orientation(section),
                )
                source_ordering=_spot_ordering_for_permutation(
                    localization.lattice_indices,permutation,
                )
            resolved=resolve_exact_lattice_correspondence(
                source,target,selection.correspondence.value,
                source_ordering=source_ordering.value,
            )
            correspondence=dict(resolved.correspondence or {})
            metadata.update({
                "label":reference.name,
                "name":reference.name,
                "observation_plane_name":reference.observation_plane_name,
                "created_at":reference.created_at,
                "lifetime":record.lifetime.value,
                "correspondence":correspondence,
                "correspondence_options":tuple(
                    item.value for item in reference_correspondence_options(localization)
                ),
            })

        alignment=align_resolved_position_reference(
            target,resolved,model=selection.correction_mode,
            polynomial_degree=selection.polynomial_degree,
            fitted_transform=selection.manual_transform,
        )
        self._position_alignment_results[key]=alignment
        transform=alignment.transform
        metadata["correction_model"]={
            "mode":selection.correction_mode,
            "manual":selection.manual_transform is not None,
            "transform":None if transform is None else transform.to_dict(),
            "polynomial_degree":(
                selection.polynomial_degree
                if selection.correction_mode==_POSITION_REFERENCE_CORRECTION_MODE_POLYNOMIAL
                else None
            ),
        }
        effective=np.asarray(alignment.predicted_positions_px)-crop_offset
        raw=np.asarray(resolved.desired_positions_px)-crop_offset
        return effective,raw,metadata,transform

    def position_alignment_result(
        self,section_key: str,
    ) -> PositionAlignmentResult | None:
        """Return the last neutral alignment built for the active section/plane."""
        return self._position_alignment_results.get(self._position_reference_key(section_key))

    def _apply_reference_correction_model(
        self,
        localization: LocalizationResult,
        raw_reference_positions: np.ndarray,
        selection: PositionReferenceSelection,
    ) -> tuple[np.ndarray,np.ndarray | None]:
        """Compatibility shim; all fitting now delegates to position_alignment."""
        target=position_reference_target_from_localization(localization,full_image=False)
        source=reference_geometry_from_localization(
            localization,positions="expected",full_image=False,
            valid_mask=np.ones(target.point_count,dtype=bool),
        )
        source=ReferenceGeometry(
            positions_px=np.asarray(raw_reference_positions,dtype=np.float64),
            site_ids=target.site_ids,valid_mask=np.ones(target.point_count,dtype=bool),
            metadata=dict(source.metadata),
        )
        resolved=resolve_exact_lattice_correspondence(source,target,"canonical")
        alignment=align_resolved_position_reference(
            target,resolved,model=selection.correction_mode,
            polynomial_degree=selection.polynomial_degree,
            fitted_transform=selection.correction.manual_transform,
        )
        matrix=(
            np.asarray(alignment.transform.matrix,dtype=np.float64)
            if alignment.transform is not None and alignment.transform.model==TRANSFORM_AFFINE
            else None
        )
        return np.asarray(alignment.predicted_positions_px),matrix

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
                    self._position_reference_selections[key] = PositionReferenceSelection(
                        mode=PositionReferenceMode.GLOBAL_FIT,
                        correction=_automatic_correction_selection(selection),
                    )
            self._section_changed(section)

    def refresh_editable_geometry_after_localization(
        self,section_key: str,localization: LocalizationResult,
    ) -> None:
        """Refresh transient reference edits after accepting a new localization."""
        key = self._position_reference_key(section_key)
        selection = self._position_reference_selections.get(key)
        if selection is None:
            return
        if selection.mode is PositionReferenceMode.EDITABLE:
            oriented = orient_localization(
                localization,self._feedback_orientation(section_key),
            )
            self._position_reference_selections[key] = PositionReferenceSelection(
                mode=PositionReferenceMode.EDITABLE,
                editable_geometry=editable_position_reference_geometry(oriented),
                editable_locks=selection.editable_locks,
                correspondence=selection.correspondence,
                correction=_automatic_correction_selection(selection),
            )
            return
        if selection.manual_transform is None:
            return
        # Interactive transforms are fitted against the previous localization.
        # Keep the source/model choice, but return to its automatic baseline fit
        # whenever a new localization is accepted.
        self._position_reference_selections[key] = PositionReferenceSelection(
            source=selection.source,
            correction=_automatic_correction_selection(selection),
        )

    def dispose(self) -> None:
        if self._position_reference_store is not None:
            self._position_reference_store.remove_listener(
                self._on_position_reference_store_changed
            )
        self._position_reference_selections.clear()
        self._position_alignment_results.clear()


def _localization_is_resolved(localization: LocalizationResult) -> bool:
    _matched,_inferred,resolved = localization_site_masks(localization)
    return bool(np.all(resolved))
