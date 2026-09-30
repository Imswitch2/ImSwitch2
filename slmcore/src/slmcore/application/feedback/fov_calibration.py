"""FOV position-calibration selection, fitting and runtime application."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any,Callable,TYPE_CHECKING,TypedDict

import numpy as np

from ...core.cgh.feedback import (
    FeedbackCapability,FOVPositionCalibration,orient_localization,
)
from ...core.cgh.coordinates import reference_px_to_k,k_to_reference_px
from .component import FeedbackComponent

if TYPE_CHECKING:
    from ...core.engine.runtime import SLMRuntime
    from ...workspace.fov_position_calibration_store import SLMFOVPositionCalibrationStore
    from ..startup_preferences import StartupPreferencesState
    from .contracts import SLMFeedbackCallbacks


class FOVPositionCalibrationContext(TypedDict):
    """Selected/applied/default FOV state; reading it does not apply a field."""

    plane_name: str | None
    available_names: tuple[str,...]
    selected_name: str | None
    applied: bool
    default_name: str | None
    supported: bool
    calibration: FOVPositionCalibration | None
    candidate: FOVPositionCalibration | None
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
    """Mutable per-section/plane selection; separate from the persisted default."""

    selected_name: str | None=None
    applied: bool=False

class FOVPositionCalibrationService(FeedbackComponent):
    """Own FOV calibration candidates, selection state and persisted access."""

    def __init__(
        self,
        *,
        runtime_provider: Callable[[], "SLMRuntime"],
        callbacks_provider: Callable[[], "SLMFeedbackCallbacks"],
        editor_writes_allowed: Callable[[], bool],
        active_feedback_plane: Callable[[str],str | None],
        startup_preferences: Callable[[], "StartupPreferencesState | None"],
        cancel_cgh: Callable[[str],None],
        feedback_orientation: Callable[[str],Any],
        resolve_position_reference: Callable[[str],tuple[Any,dict[str,Any]]],
        store: "SLMFOVPositionCalibrationStore | None"=None,
    ) -> None:
        super().__init__(
            runtime_provider=runtime_provider,
            callbacks_provider=callbacks_provider,
            editor_writes_allowed=editor_writes_allowed,
        )
        self._active_feedback_plane_provider = active_feedback_plane
        self._startup_preferences_provider = startup_preferences
        self._cancel_cgh = cancel_cgh
        self._feedback_orientation = feedback_orientation
        self._resolve_position_reference_callback = resolve_position_reference
        self._fov_position_calibration_store = store
        self._fov_position_calibration_selections: dict[
            tuple[str,str | None],_FOVPositionCalibrationSelection
        ] = {}
        self._fov_position_calibration_candidates: dict[
            tuple[str,str | None],FOVPositionCalibration
        ] = {}
        if self._fov_position_calibration_store is not None:
            self._fov_position_calibration_store.add_listener(
                self._on_fov_position_calibration_store_changed
            )

    def _active_feedback_plane(self,section_key: str) -> str | None:
        return self._active_feedback_plane_provider(section_key)

    def _fov_position_key(self,section_key: str) -> tuple[str,str | None]:
        section = str(section_key)
        return section,self._active_feedback_plane(section)

    def _saved_fov_position_calibration_name(
        self,section_key: str,plane_name: str | None,
    ) -> str | None:
        preferences = self._startup_preferences_provider()
        if preferences is None:
            return None
        return preferences.default_fov_position_calibration(
            section_key,plane_name,
        )

    def _load_fov_position_calibration(
        self,section_key: str,plane_name: str,name: str,
    ) -> FOVPositionCalibration:
        store = self._fov_position_calibration_store
        if store is None:
            raise RuntimeError("FOV position-calibration storage is unavailable")
        return store.load(
            self._runtime.identity,section_key,plane_name,name,
        )

    def fov_position_calibration_context(
        self,section_key: str,
    ) -> FOVPositionCalibrationContext:
        """Resolve draft selection/default state without applying a field to the runtime."""
        section = str(section_key)
        plane = self._active_feedback_plane(section)
        key = (section,plane)
        store = self._fov_position_calibration_store
        names = (
            () if plane is None or store is None else
            store.list(self._runtime.identity,section,plane)
        )
        selection = self._fov_position_calibration_selections.get(key)
        if selection is None:
            default_name = self._saved_fov_position_calibration_name(section,plane)
            selected = default_name if default_name in names else None
            selection = _FOVPositionCalibrationSelection(selected,bool(selected))
            self._fov_position_calibration_selections[key] = selection
        selected = str(selection.selected_name or "").strip() or None
        if selected not in names:
            selected = None
            selection.selected_name = None
            selection.applied = False
        applied = bool(selection.applied and selected)
        runtime_context = self._runtime.get_section_fov_position_context(section)
        outside = np.asarray(runtime_context.get("outside_mask",()),dtype=bool)
        extrapolated_count = int(np.count_nonzero(outside)) if applied else 0
        calibration = (
            None if selected is None or plane is None or store is None
            else self._load_fov_position_calibration(section,plane,selected)
        )
        candidate = self._fov_position_calibration_candidates.get(key)
        return {
            "plane_name":plane,
            "available_names":tuple(names),
            "selected_name":selected,
            "applied":applied,
            "default_name":self._saved_fov_position_calibration_name(section,plane),
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
        """Select a saved field, updating the runtime only when Apply is already enabled."""
        self._require_editor_mode()
        section = str(section_key)
        plane = self._active_feedback_plane(section)
        if plane is None:
            raise RuntimeError("Select a measurement plane before choosing an FOV calibration")
        key = (section,plane)
        selected = str(name or "").strip() or None
        selection = self._fov_position_calibration_selections.setdefault(
            key,_FOVPositionCalibrationSelection(),
        )
        calibration = None
        if selected is not None:
            calibration = self._load_fov_position_calibration(section,plane,selected)
        selection.selected_name = selected
        if bool(selection.applied):
            if calibration is None:
                selection.applied = False
            self._set_runtime_fov_position_calibration(section,calibration)
            self._warn_fov_extrapolation(section)
        self._section_changed(section)

    def set_fov_position_calibration_applied(
        self,section_key: str,applied: bool,
    ) -> None:
        """Enable/disable the selected runtime field without changing the saved default."""
        self._require_editor_mode()
        section = str(section_key)
        plane = self._active_feedback_plane(section)
        if plane is None:
            raise RuntimeError("Select a measurement plane before applying an FOV calibration")
        key = (section,plane)
        selection = self._fov_position_calibration_selections.setdefault(
            key,_FOVPositionCalibrationSelection(),
        )
        name = str(selection.selected_name or "").strip() or None
        applied = bool(applied)
        if applied and name is None:
            raise RuntimeError("Choose an FOV calibration before enabling Apply")
        calibration = (
            self._load_fov_position_calibration(section,plane,name)
            if applied else None
        )
        selection.applied = applied
        self._set_runtime_fov_position_calibration(section,calibration)
        self._section_changed(section)
        self._warn_fov_extrapolation(section)

    def set_default_fov_position_calibration(self,section_key: str) -> None:
        """Persist this plane's selected default and enable it in the current runtime."""
        self._require_editor_mode()
        section = str(section_key)
        plane = self._active_feedback_plane(section)
        if plane is None:
            raise RuntimeError("Select a measurement plane before setting an FOV default")
        context = self.fov_position_calibration_context(section)
        name = context.get("selected_name")
        if not name:
            raise RuntimeError("Choose an FOV calibration before setting the default")
        preferences = self._startup_preferences_provider()
        if preferences is None:
            raise RuntimeError("Startup preferences are unavailable")
        preferences.set_default_fov_position_calibration(section,plane,name)
        self.set_fov_position_calibration_applied(section,True)

    def delete_fov_position_calibration(self,section_key: str) -> str:
        """Delete the selected record first, then clear matching default and draft state."""
        self._require_editor_mode()
        section = str(section_key)
        context = self.fov_position_calibration_context(section)
        plane = context.get("plane_name")
        name = context.get("selected_name")
        if not plane or not name:
            raise RuntimeError("Choose an FOV calibration before deleting it")
        store = self._fov_position_calibration_store
        if store is None:
            raise RuntimeError("FOV position-calibration storage is unavailable")

        # Delete first so a filesystem failure leaves both selection and
        # startup default untouched.  The synchronous store listener clears
        # the selected/applied runtime calibration.
        store.delete(self._runtime.identity,section,plane,name)
        preferences = self._startup_preferences_provider()
        if (
            preferences is not None
            and preferences.default_fov_position_calibration(section,plane) == name
        ):
            preferences.set_default_fov_position_calibration(section,plane,None)
        self._fov_position_calibration_candidates.pop((section,plane),None)
        self._section_changed(section)
        return str(name)

    def fit_fov_position_calibration(
        self,section_key: str,*,model: str="polynomial",degree: int=2,
    ) -> FOVPositionCalibration:
        """Build an unsaved field from accepted correction or current position analysis."""
        self._require_editor_mode()
        section = str(section_key)
        plane = self._active_feedback_plane(section)
        if plane is None:
            raise RuntimeError("Select a measurement plane before fitting an FOV calibration")
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
            detector = correction.measurement.acquisition.detector
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
            detector = None
            if inspection.measurement is not None:
                detector = inspection.measurement.acquisition.detector
            source = "current_position_analysis"

        calibration = FOVPositionCalibration.fit(
            name="Current candidate",
            slm_serial=self._runtime.identity.serial_number,
            section_key=section,plane_name=plane,
            ideal_positions_kxy=ideal,
            total_displacements_kxy=total_displacement,
            model=model,degree=degree,detector_name=detector,
            provenance={
                "reference":dict(reference_metadata or {}),
                "source":source,
                "source_applied_calibration":self.fov_position_calibration_context(
                    section
                ).get("selected_name"),
            },
        )
        self._fov_position_calibration_candidates[(section,plane)] = calibration
        self._section_changed(section)
        return calibration

    def save_fov_position_calibration(
        self,section_key: str,name: str,*,overwrite: bool=False,
    ) -> FOVPositionCalibration:
        """Persist the current candidate; selection/application follow store notifications."""
        self._require_editor_mode()
        section = str(section_key)
        plane = self._active_feedback_plane(section)
        if plane is None:
            raise RuntimeError("Select a measurement plane before saving an FOV calibration")
        candidate = self._fov_position_calibration_candidates.get((section,plane))
        if candidate is None:
            raise RuntimeError("Build an FOV calibration fit before saving")
        saved = candidate.with_name(str(name))
        store = self._fov_position_calibration_store
        if store is None:
            raise RuntimeError("FOV position-calibration storage is unavailable")
        store.save(saved,overwrite=overwrite)
        self._section_changed(section)
        return saved

    def fov_position_calibration_replacement_warning(
        self,section_key: str,
    ) -> str:
        """Describe coverage changes before replacing an applied field; empty means none."""
        section = str(section_key)
        context = self.fov_position_calibration_context(section)
        plane = context.get("plane_name")
        name = context.get("selected_name")
        if not plane or not name or not context.get("applied"):
            return ""
        candidate = self._fov_position_calibration_candidates.get((section,plane))
        if candidate is None:
            return ""
        old = self._load_fov_position_calibration(section,plane,name)
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
        plane = context.get("plane_name")
        name = context.get("selected_name")
        if not plane or not name or not context.get("applied"):
            raise RuntimeError("An applied FOV calibration is required for replacement")
        candidate = self._fov_position_calibration_candidates.get((section,plane))
        if candidate is None:
            raise RuntimeError("Build a candidate FOV fit before replacing the applied calibration")
        saved = candidate.with_name(name)
        self._fov_position_calibration_store.save(saved,overwrite=True)
        self._fov_position_calibration_candidates.pop((section,plane),None)
        self._set_runtime_fov_position_calibration(section,saved)
        self._section_changed(section)
        self._warn_fov_extrapolation(section)
        return name

    def _set_runtime_fov_position_calibration(
        self,section_key: str,calibration: FOVPositionCalibration | None,
        *,notify: bool=True,
    ) -> None:
        # Context refreshes happen for all sections on plane/catalog changes.
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
        """Reconcile current sections/planes with selections and saved startup defaults."""
        runtime = self._runtime
        active_keys = set()
        for section_key in tuple(runtime.section_keys):
            section = str(section_key)
            plane = self._active_feedback_plane(section)
            key = (section,plane)
            active_keys.add(key)
            selection = self._fov_position_calibration_selections.get(key)
            if selection is None:
                name = self._saved_fov_position_calibration_name(section,plane)
                valid_default = bool(
                    name and plane and self._fov_position_calibration_store is not None
                    and self._fov_position_calibration_store.exists(
                        runtime.identity,section,plane,name
                    )
                )
                selection = _FOVPositionCalibrationSelection(
                    selected_name=name if valid_default else None,
                    applied=valid_default,
                )
                self._fov_position_calibration_selections[key] = selection
            name = str(selection.selected_name or "").strip() or None
            applied = bool(selection.applied and name and plane)
            calibration = (
                self._load_fov_position_calibration(section,plane,name)
                if applied else None
            )
            self._set_runtime_fov_position_calibration(
                section,calibration,notify=True,
            )
        for key in tuple(self._fov_position_calibration_candidates):
            if key not in active_keys and key[0] not in runtime.section_keys:
                self._fov_position_calibration_candidates.pop(key,None)

    def _on_fov_position_calibration_store_changed(self) -> None:
        runtime = self._runtime
        for section_key in tuple(runtime.section_keys):
            section = str(section_key)
            plane = self._active_feedback_plane(section)
            if plane is None:
                continue
            key = (section,plane)
            selection = self._fov_position_calibration_selections.get(key)
            if selection is None:
                continue
            name = str(selection.selected_name or "").strip() or None
            exists = bool(
                name and self._fov_position_calibration_store.exists(
                    runtime.identity,section,plane,name
                )
            )
            if not exists:
                selection.selected_name = None
                selection.applied = False
                self._set_runtime_fov_position_calibration(section,None,notify=True)
            elif selection.applied:
                calibration = self._load_fov_position_calibration(section,plane,name)
                self._set_runtime_fov_position_calibration(section,calibration,notify=True)
            else:
                self._section_changed(section)

    def dispose(self) -> None:
        if self._fov_position_calibration_store is not None:
            self._fov_position_calibration_store.remove_listener(
                self._on_fov_position_calibration_store_changed
            )
        self._fov_position_calibration_selections.clear()
        self._fov_position_calibration_candidates.clear()
