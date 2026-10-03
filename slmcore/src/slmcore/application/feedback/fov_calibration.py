"""FOV position-calibration selection, fitting and runtime application."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any,Callable,TYPE_CHECKING,TypedDict

import numpy as np

from ...core.cgh.feedback import FeedbackCapability,orient_localization
from ...core.calibration import FOVMappingCorrection,base_mapping_signature
from ...core.cgh.coordinates import reference_px_to_k,k_to_reference_px
from .component import FeedbackComponent

if TYPE_CHECKING:
    from ...core.engine.runtime import SLMRuntime
    from ..mapping import MappingService
    from ..observation_geometry import ObservationGeometryService
    from ..startup_preferences import StartupPreferencesState
    from .contracts import SLMFeedbackCallbacks


class FOVPositionCalibrationContext(TypedDict):
    """Selected/applied/default FOV state; reading it does not apply a field."""

    mapping_name: str | None
    available_names: tuple[str,...]
    selected_name: str | None
    applied: bool
    default_name: str | None
    default_compatible: bool
    compatibility_reason: str | None
    supported: bool
    calibration: FOVMappingCorrection | None
    candidate: FOVMappingCorrection | None
    extrapolated_count: int
    target_count: int
    max_extrapolation_kxy: float
    coverage_tolerance_kxy: float

class FOVPositionCalibrationPreview(TypedDict):
    """Sample and target displacement arrays for the candidate or saved field."""

    source: str
    name: str | None
    model: str
    degree: int
    sample_count: int
    rms_residual_kxy: float
    max_residual_kxy: float
    sample_positions_kxy: np.ndarray
    sample_displacements_kxy: np.ndarray
    current_positions_kxy: np.ndarray
    current_displacements_kxy: np.ndarray

@dataclass
class _FOVPositionCalibrationSelection:
    """Mutable per-section/mapping selection; separate from the persisted default."""

    selected_name: str | None=None
    applied: bool=False

class FOVPositionCalibrationService(FeedbackComponent):
    """Produce FOV candidates and manage runtime selection via MappingService."""

    def __init__(
        self,
        *,
        runtime_provider: Callable[[], "SLMRuntime"],
        callbacks_provider: Callable[[], "SLMFeedbackCallbacks"],
        editor_writes_allowed: Callable[[], bool],
        active_mapping: Callable[[str],str | None],
        active_observation_plane: Callable[[str],str | None],
        startup_preferences: Callable[[], "StartupPreferencesState | None"],
        cancel_cgh: Callable[[str],None],
        feedback_orientation: Callable[[str],Any],
        resolve_position_reference: Callable[[str],tuple[Any,dict[str,Any]]],
        mappings: "MappingService",
        observation_geometry: "ObservationGeometryService | None"=None,
    ) -> None:
        super().__init__(
            runtime_provider=runtime_provider,
            callbacks_provider=callbacks_provider,
            editor_writes_allowed=editor_writes_allowed,
        )
        self._active_mapping_provider = active_mapping
        self._active_observation_plane_provider = active_observation_plane
        self._startup_preferences_provider = startup_preferences
        self._cancel_cgh = cancel_cgh
        self._feedback_orientation = feedback_orientation
        self._resolve_position_reference_callback = resolve_position_reference
        self._mappings = mappings
        self._observation_geometry = observation_geometry
        self._fov_position_calibration_selections: dict[
            tuple[str,str | None],_FOVPositionCalibrationSelection
        ] = {}
        self._fov_position_calibration_candidates: dict[
            tuple[str,str | None],FOVMappingCorrection
        ] = {}
        if self._mappings.fov_corrections_available:
            self._mappings.add_fov_correction_listener(
                self._on_fov_mapping_correction_store_changed
            )

    def _active_mapping(self,section_key: str) -> str | None:
        return self._active_mapping_provider(section_key)

    def _active_observation_plane(self,section_key: str) -> str | None:
        return self._active_observation_plane_provider(section_key)

    def _fov_position_key(self,section_key: str) -> tuple[str,str | None]:
        section = str(section_key)
        return section,self._active_mapping(section)

    def _saved_fov_position_calibration_name(
        self,section_key: str,mapping_name: str | None,
    ) -> str | None:
        preferences = self._startup_preferences_provider()
        if preferences is None:
            return None
        return preferences.default_fov_correction(
            section_key,mapping_name,
        )

    def _load_fov_position_calibration(
        self,section_key: str,mapping_name: str,name: str,
    ) -> FOVMappingCorrection:
        return self._mappings.load_fov_correction(
            section_key,mapping_name,name,
        )

    def _current_base_mapping_signature(self,section_key: str) -> str | None:
        calibration = self._runtime.get_section_calibration_copy(str(section_key))
        if calibration is None or not calibration.is_valid():
            return None
        return base_mapping_signature(calibration)

    def _correction_compatible(
        self,section_key: str,correction: FOVMappingCorrection | None,
    ) -> bool:
        if correction is None:
            return False
        signature = self._current_base_mapping_signature(section_key)
        return bool(
            signature
            and correction.base_mapping_signature
            and correction.base_mapping_signature == signature
        )

    def fov_position_calibration_context(
        self,section_key: str,
    ) -> FOVPositionCalibrationContext:
        """Resolve mapping-scoped default and session Apply state."""
        section = str(section_key)
        mapping = self._active_mapping(section)
        key = (section,mapping)
        names = (
            () if mapping is None else self._mappings.fov_correction_names(section,mapping)
        )
        selection = self._fov_position_calibration_selections.get(key)
        default_name = self._saved_fov_position_calibration_name(section,mapping)
        default_valid = bool(default_name and default_name in names and mapping)
        default_calibration = (
            self._load_fov_position_calibration(section,mapping,default_name)
            if default_valid else None
        )
        default_compatible = self._correction_compatible(section,default_calibration)
        if selection is None:
            selection = _FOVPositionCalibrationSelection(
                selected_name=default_name if default_valid else None,
                applied=bool(default_valid and default_compatible),
            )
            self._fov_position_calibration_selections[key] = selection
        selected = str(selection.selected_name or "").strip() or None
        if selected not in names:
            selected = None
            selection.selected_name = None
        # Runtime Apply always means this mapping's authoritative default.
        if selection.applied and not (default_valid and default_compatible):
            selection.applied = False
        applied = bool(selection.applied and default_valid and default_compatible)
        runtime_context = self._runtime.get_section_fov_position_context(section)
        outside = np.asarray(runtime_context.get("outside_mask",()),dtype=bool)
        extrapolated_count = int(np.count_nonzero(outside)) if applied else 0
        calibration = (
            None if selected is None or mapping is None
            else self._load_fov_position_calibration(section,mapping,selected)
        )
        reason = None
        if default_name and not default_valid:
            reason = "The configured default FOV correction is not available for this mapping."
        elif default_valid and not default_compatible:
            reason = "The default FOV correction is incompatible with the current base mapping calibration."
        elif not default_name:
            reason = "No default FOV correction is configured for this mapping."
        candidate = self._fov_position_calibration_candidates.get(key)
        return {
            "mapping_name":mapping,
            "available_names":tuple(names),
            "selected_name":selected,
            "applied":applied,
            "default_name":default_name,
            "default_compatible":bool(default_valid and default_compatible),
            "compatibility_reason":reason,
            "supported":bool(runtime_context.get("supported",False)),
            "calibration":calibration,
            "candidate":candidate,
            "extrapolated_count":extrapolated_count,
            "target_count":int(np.asarray(runtime_context["ideal_positions_kxy"]).shape[1]),
            "max_extrapolation_kxy":float(runtime_context.get("max_extrapolation_kxy",0.0)),
            "coverage_tolerance_kxy":float(runtime_context.get("coverage_tolerance_kxy",0.0)),
        }

    def fov_position_calibration_preview(
        self,section_key: str,
    ) -> FOVPositionCalibrationPreview | None:
        """Preview the candidate or selected field, falling back to its recorded samples."""
        context = self.fov_position_calibration_context(section_key)
        key = self._fov_position_key(section_key)
        candidate = self._fov_position_calibration_candidates.get(key)
        calibration = candidate if candidate is not None else context.get("calibration")
        if calibration is None:
            return None
        current = self._runtime.get_section_fov_position_context(section_key)
        ideal = np.asarray(current["ideal_positions_kxy"],dtype=np.float64)
        # Inspect the field at the current target when it is meaningful.  With
        # no target (or an incompatible target), fall back to the calibration's
        # own sample positions so the read-only FOV view remains usable.
        if bool(current.get("supported",False)) and ideal.shape[1] > 0:
            preview_positions = ideal
        else:
            preview_positions = np.asarray(
                calibration.sample_positions_kxy,dtype=np.float64,
            )
        predicted = calibration.evaluate(preview_positions)
        return {
            "source":"candidate" if candidate is not None else "applied",
            "name":None if candidate is not None else calibration.name,
            "model":calibration.model,
            "degree":calibration.degree,
            "sample_count":calibration.sample_count,
            "rms_residual_kxy":calibration.rms_residual_kxy,
            "max_residual_kxy":calibration.max_residual_kxy,
            "sample_positions_kxy":np.asarray(calibration.sample_positions_kxy),
            "sample_displacements_kxy":np.asarray(calibration.sample_displacements_kxy),
            "current_positions_kxy":preview_positions,
            "current_displacements_kxy":predicted,
        }

    def select_fov_position_calibration(
        self,section_key: str,name: str | None,
    ) -> None:
        """Select a saved correction for management/inspection only.

        Runtime Apply is intentionally independent and always refers to the
        mapping's persistent default correction.
        """
        self._require_editor_mode()
        section = str(section_key)
        mapping = self._active_mapping(section)
        if mapping is None:
            raise RuntimeError("Select a mapping before choosing an FOV correction")
        key = (section,mapping)
        selected = str(name or "").strip() or None
        if selected is not None:
            self._load_fov_position_calibration(section,mapping,selected)
        selection = self._fov_position_calibration_selections.setdefault(
            key,_FOVPositionCalibrationSelection(),
        )
        selection.selected_name = selected
        self._section_changed(section)

    def set_fov_position_calibration_applied(
        self,section_key: str,applied: bool,
    ) -> None:
        """Enable/disable this mapping's compatible default correction for the session."""
        self._require_editor_mode()
        section = str(section_key)
        mapping = self._active_mapping(section)
        if mapping is None:
            raise RuntimeError("Select a mapping before applying an FOV correction")
        key = (section,mapping)
        selection = self._fov_position_calibration_selections.setdefault(
            key,_FOVPositionCalibrationSelection(),
        )
        applied = bool(applied)
        calibration = None
        if applied:
            default_name = self._saved_fov_position_calibration_name(section,mapping)
            if not default_name or not self._mappings.fov_correction_exists(
                section,mapping,default_name
            ):
                raise RuntimeError("No default FOV correction is available for this mapping")
            calibration = self._load_fov_position_calibration(
                section,mapping,default_name
            )
            if not self._correction_compatible(section,calibration):
                raise RuntimeError(
                    "The default FOV correction is incompatible with the current base mapping calibration"
                )
            if selection.selected_name is None:
                selection.selected_name = default_name
        selection.applied = applied
        self._set_runtime_fov_position_calibration(section,calibration)
        self._section_changed(section)
        self._warn_fov_extrapolation(section)

    def set_default_fov_position_calibration(self,section_key: str) -> None:
        """Persist the selected compatible correction as this mapping's default."""
        self._require_editor_mode()
        section = str(section_key)
        mapping = self._active_mapping(section)
        if mapping is None:
            raise RuntimeError("Select a mapping before setting an FOV default")
        context = self.fov_position_calibration_context(section)
        name = context.get("selected_name")
        if not name:
            raise RuntimeError("Choose an FOV calibration before setting the default")
        calibration = self._load_fov_position_calibration(section,mapping,name)
        if not self._correction_compatible(section,calibration):
            raise RuntimeError(
                "The selected FOV correction is incompatible with the current base mapping calibration"
            )
        preferences = self._startup_preferences_provider()
        if preferences is None:
            raise RuntimeError("Startup preferences are unavailable")
        preferences.set_default_fov_correction(section,mapping,name)
        self.set_fov_position_calibration_applied(section,True)

    def delete_fov_position_calibration(self,section_key: str) -> str:
        """Delete the selected record first, then clear matching default and draft state."""
        self._require_editor_mode()
        section = str(section_key)
        context = self.fov_position_calibration_context(section)
        mapping = context.get("mapping_name")
        name = context.get("selected_name")
        if not mapping or not name:
            raise RuntimeError("Choose an FOV calibration before deleting it")
        # Delete first so a filesystem failure leaves both selection and
        # startup default untouched. The synchronous mapping listener clears
        # the selected/applied runtime correction.
        self._mappings.delete_fov_correction(section,mapping,name)
        preferences = self._startup_preferences_provider()
        if (
            preferences is not None
            and preferences.default_fov_correction(section,mapping) == name
        ):
            preferences.set_default_fov_correction(section,mapping,None)
        self._fov_position_calibration_candidates.pop((section,mapping),None)
        self._section_changed(section)
        return str(name)

    def fit_fov_position_calibration(
        self,section_key: str,*,model: str="polynomial",degree: int=2,
    ) -> FOVMappingCorrection:
        """Build an unsaved field from accepted correction or current position analysis."""
        self._require_editor_mode()
        section = str(section_key)
        mapping = self._active_mapping(section)
        if mapping is None:
            raise RuntimeError("Select a mapping before fitting an FOV correction")
        status = self._runtime.get_section_feedback_status(section)
        if FeedbackCapability.POSITION_CORRECTION not in set(status.capabilities):
            raise RuntimeError("The current target does not support position correction")

        # Prefer the accepted transient correction.  It remains authoritative
        # after computing an adapted hologram, which lets users verify the
        # correction experimentally and only then promote it to a persistent
        # FOV calibration.  Fall back to a fresh analysis only before a
        # correction has been accepted.
        session_inspection = self._runtime.get_section_cgh_session_inspection(
            section
        )
        correction = session_inspection.position_correction
        if correction is not None:
            ideal = np.asarray(
                correction.ideal_positions_kxy,dtype=np.float64,
            )
            total_displacement = (
                np.asarray(correction.corrected_positions_kxy,dtype=np.float64)
                - ideal
            )
            reference_metadata = dict(correction.reference or {})
            acquisition = correction.measurement.acquisition
            detector = acquisition.detector
            source = "accepted_position_correction"
        else:
            reference_positions,reference_metadata = self._resolve_position_reference_callback(
                section
            )
            analysis = self._runtime.compute_section_feedback_position_analysis(
                section,
                orientation=self._feedback_orientation(section),
                reference_positions_px=reference_positions,
            )
            current = self._runtime.get_section_fov_position_context(section)
            ideal = np.asarray(current["ideal_positions_kxy"],dtype=np.float64)
            total_displacement = (
                np.asarray(analysis.corrected_positions_kxy,dtype=np.float64)-ideal
            )
            inspection = self._runtime.get_section_feedback_inspection(section)
            acquisition = None if inspection.measurement is None else inspection.measurement.acquisition
            detector = None if acquisition is None else acquisition.detector
            source = "current_position_analysis"

        observation_plane = self._active_observation_plane(section)
        geometry_provenance = {}
        if observation_plane and self._observation_geometry is not None:
            geometry = self._observation_geometry.calibration(section,observation_plane)
            if geometry is not None:
                geometry_provenance = {
                    "orientation":geometry.orientation.value,
                    "protocol":geometry.protocol,
                    "created_at":geometry.created_at,
                }

        base_signature = self._current_base_mapping_signature(section)
        if base_signature is None:
            raise RuntimeError(
                "A valid base mapping calibration is required before fitting an FOV correction"
            )
        calibration = FOVMappingCorrection.fit(
            name="Current candidate",
            slm_serial=self._runtime.identity.serial_number,
            section_key=section,mapping_name=mapping,
            base_mapping_signature=base_signature,
            ideal_positions_kxy=ideal,
            total_displacements_kxy=total_displacement,
            model=model,degree=degree,detector_name=detector,
            provenance={
                "reference":dict(reference_metadata or {}),
                "source":source,
                "observation_plane":observation_plane,
                "detector":None if acquisition is None else acquisition.detector,
                "measurement_source":None if acquisition is None else acquisition.source,
                "measurement_pixel_size_um":None if acquisition is None else acquisition.pixel_size_um,
                "geometry_calibration":geometry_provenance or None,
                "source_applied_correction":self.fov_position_calibration_context(
                    section
                ).get("selected_name"),
            },
        )
        self._fov_position_calibration_candidates[(section,mapping)] = calibration
        self._section_changed(section)
        return calibration

    def save_fov_position_calibration(
        self,section_key: str,name: str,*,overwrite: bool=False,
    ) -> FOVMappingCorrection:
        """Persist the current candidate; selection/application follow store notifications."""
        self._require_editor_mode()
        section = str(section_key)
        mapping = self._active_mapping(section)
        if mapping is None:
            raise RuntimeError("Select a mapping before saving an FOV correction")
        candidate = self._fov_position_calibration_candidates.get((section,mapping))
        if candidate is None:
            raise RuntimeError("Build an FOV calibration fit before saving")
        saved = candidate.with_name(str(name))
        self._mappings.save_fov_correction(saved,overwrite=overwrite)
        self._section_changed(section)
        return saved

    def fov_position_calibration_replacement_warning(
        self,section_key: str,
    ) -> str:
        """Describe coverage changes before replacing an applied field; empty means none."""
        section = str(section_key)
        context = self.fov_position_calibration_context(section)
        mapping = context.get("mapping_name")
        name = context.get("default_name")
        if not mapping or not name or not context.get("applied"):
            return ""
        candidate = self._fov_position_calibration_candidates.get((section,mapping))
        if candidate is None:
            return ""
        old = self._load_fov_position_calibration(section,mapping,name)
        old_area = float(old.coverage_area)
        new_area = float(candidate.coverage_area)
        if old_area <= 0 or new_area <= 0:
            return (
                "Candidate and applied FOV calibration coverage could not be "
                "compared reliably because one convex hull is degenerate."
            ) if old_area != new_area else ""

        old_outside_new,_,_ = candidate.coverage_extrapolation(
            old.sample_positions_kxy
        )
        new_outside_old,_,_ = old.coverage_extrapolation(
            candidate.sample_positions_kxy
        )
        lost = int(np.count_nonzero(old_outside_new))
        added = int(np.count_nonzero(new_outside_old))
        area_ratio = new_area/old_area
        area_changed = area_ratio < 0.98 or area_ratio > 1.02
        if lost or added or area_changed:
            details = [
                "candidate area %.1f%% of the previous convex-hull area"
                % (100.0*area_ratio)
            ]
            if lost:
                details.append(
                    "%d/%d previous samples lie outside the candidate coverage"
                    % (lost,old.sample_count)
                )
            if added:
                details.append(
                    "%d/%d candidate samples lie outside the previous coverage"
                    % (added,candidate.sample_count)
                )
            return "Candidate measured coverage differs from the applied calibration (" + "; ".join(details) + ")."
        return ""

    def replace_applied_fov_position_calibration(self,section_key: str) -> str:
        """Overwrite and reapply the selected record, then consume the fitted candidate."""
        self._require_editor_mode()
        section = str(section_key)
        context = self.fov_position_calibration_context(section)
        mapping = context.get("mapping_name")
        name = context.get("default_name")
        if not mapping or not name or not context.get("applied"):
            raise RuntimeError("An applied FOV calibration is required for replacement")
        candidate = self._fov_position_calibration_candidates.get((section,mapping))
        if candidate is None:
            raise RuntimeError("Build a candidate FOV fit before replacing the applied calibration")
        saved = candidate.with_name(name)
        self._mappings.save_fov_correction(saved,overwrite=True)
        self._fov_position_calibration_candidates.pop((section,mapping),None)
        self._set_runtime_fov_position_calibration(section,saved)
        self._section_changed(section)
        self._warn_fov_extrapolation(section)
        return name

    def _set_runtime_fov_position_calibration(
        self,section_key: str,calibration: FOVMappingCorrection | None,
        *,notify: bool=True,
    ) -> None:
        # Context refreshes happen for all sections on mapping/catalog changes.
        # Do not cancel a CGH request or create redundant callbacks when this
        # section already holds the exact same persistent field.
        current = None
        current_known = False
        try:
            current = self._runtime.get_section_fov_position_context(
                section_key
            ).get("calibration")
            current_known = True
        except Exception:
            # If the current target is temporarily invalid, fall back to the
            # conservative path below; the runtime setter remains authoritative.
            pass
        same = (
            current is calibration
            or (
                current_known
                and current is not None
                and calibration is not None
                and current.to_dict() == calibration.to_dict()
            )
            or (current_known and current is None and calibration is None)
        )
        if same:
            if notify:
                self._section_changed(section_key)
            return

        self._cancel_cgh(section_key)
        transition = self._runtime.set_section_fov_position_calibration(
            section_key,calibration,
        )
        if notify:
            if transition is not None:
                self._callbacks.on_transition_committed(section_key,transition)
            else:
                self._section_changed(section_key)

    def _warn_fov_extrapolation(self,section_key: str) -> None:
        context = self.fov_position_calibration_context(section_key)
        count = int(context.get("extrapolated_count",0) or 0)
        if count:
            self._warning(
                "FOV calibration extrapolation",
                "%d of %d current target positions lie outside the measured FOV "
                "coverage (beyond numerical tolerance). The polynomial calibration "
                "will be extrapolated at those positions."
                % (count,int(context.get("target_count",0) or 0)),
            )

    def apply_startup_fov_position_calibration_defaults(self) -> None:
        """Explicitly reconcile default selections with the runtime after startup."""
        self.refresh_fov_position_calibration_contexts()

    def refresh_fov_position_calibration_contexts(self) -> None:
        """Reconcile mapping defaults, compatibility and per-session Apply state."""
        runtime = self._runtime
        active_keys = set()
        for section_key in tuple(runtime.section_keys):
            section = str(section_key)
            mapping = self._active_mapping(section)
            key = (section,mapping)
            active_keys.add(key)
            default_name = self._saved_fov_position_calibration_name(section,mapping)
            default_exists = bool(
                default_name and mapping
                and self._mappings.fov_correction_exists(section,mapping,default_name)
            )
            default_calibration = (
                self._load_fov_position_calibration(section,mapping,default_name)
                if default_exists else None
            )
            compatible = self._correction_compatible(section,default_calibration)
            selection = self._fov_position_calibration_selections.get(key)
            if selection is None:
                selection = _FOVPositionCalibrationSelection(
                    selected_name=default_name if default_exists else None,
                    applied=bool(default_exists and compatible),
                )
                self._fov_position_calibration_selections[key] = selection
            if selection.applied and not (default_exists and compatible):
                selection.applied = False
            calibration = default_calibration if selection.applied else None
            self._set_runtime_fov_position_calibration(
                section,calibration,notify=True,
            )
        for key in tuple(self._fov_position_calibration_candidates):
            if key not in active_keys and key[0] not in runtime.section_keys:
                self._fov_position_calibration_candidates.pop(key,None)

    def _on_fov_mapping_correction_store_changed(self) -> None:
        self.refresh_fov_position_calibration_contexts()

    def dispose(self) -> None:
        self._mappings.remove_fov_correction_listener(
            self._on_fov_mapping_correction_store_changed
        )
        self._fov_position_calibration_selections.clear()
        self._fov_position_calibration_candidates.clear()
