"""FOV correction selection, inspection and runtime application."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable,TYPE_CHECKING,TypedDict

import numpy as np

from ..core.calibration import FOVMappingCorrection,base_mapping_signature

if TYPE_CHECKING:
    from ..core.engine.runtime import SLMRuntime
    from .mapping import MappingService
    from .startup_preferences import StartupPreferencesState


class FOVMappingRuntimeContext(TypedDict):
    """Selected/applied/default FOV state; reading it does not apply a field."""

    mapping_name: str | None
    available_names: tuple[str,...]
    selected_name: str | None
    selected_compatible: bool
    applied_name: str | None
    applied: bool
    default_name: str | None
    default_compatible: bool
    compatibility_reason: str | None
    supported: bool
    correction: FOVMappingCorrection | None
    extrapolated_count: int
    target_count: int
    max_extrapolation_kxy: float
    coverage_tolerance_kxy: float


class FOVMappingRuntimePreview(TypedDict):
    """Sample and target displacement arrays for the selected saved correction."""

    source: str
    name: str | None
    model: str
    degree: int | None
    sample_count: int
    rms_residual_kxy: float
    max_residual_kxy: float
    sample_positions_kxy: np.ndarray
    sample_displacements_kxy: np.ndarray
    current_positions_kxy: np.ndarray
    current_displacements_kxy: np.ndarray


@dataclass
class _FOVMappingRuntimeSelection:
    """Mutable per-section/mapping selection; separate from the persisted default."""

    selected_name: str | None=None
    applied_name: str | None=None
    applied: bool=False

class FOVMappingRuntimeService:
    """Manage saved FOV correction selection and runtime application."""

    def __init__(
        self,
        *,
        runtime_provider: Callable[[], "SLMRuntime"],
        editor_writes_allowed: Callable[[], bool],
        active_mapping: Callable[[str],str | None],
        startup_preferences: Callable[[], "StartupPreferencesState | None"],
        cancel_cgh: Callable[[str],None],
        section_changed: Callable[[str],None],
        transition_committed: Callable[[str,object],None],
        mappings: "MappingService",
    ) -> None:
        self._runtime_provider = runtime_provider
        self._editor_writes_allowed = editor_writes_allowed
        self._section_changed_callback = section_changed
        self._transition_committed = transition_committed
        self._active_mapping_provider = active_mapping
        self._startup_preferences_provider = startup_preferences
        self._cancel_cgh = cancel_cgh
        self._mappings = mappings
        self._fov_mapping_selections: dict[
            tuple[str,str | None],_FOVMappingRuntimeSelection
        ] = {}
        if self._mappings.fov_corrections_available:
            self._mappings.add_fov_correction_listener(
                self._on_fov_mapping_correction_store_changed
            )

    @property
    def _runtime(self) -> "SLMRuntime":
        return self._runtime_provider()

    def _require_editor_mode(self) -> None:
        if not self._editor_writes_allowed():
            raise RuntimeError("Operation unavailable in Fast Config mode")

    def _section_changed(self,section_key: str) -> None:
        self._section_changed_callback(str(section_key))

    def _active_mapping(self,section_key: str) -> str | None:
        return self._active_mapping_provider(section_key)

    def _saved_fov_mapping_correction_name(
        self,section_key: str,mapping_name: str | None,
    ) -> str | None:
        preferences = self._startup_preferences_provider()
        if preferences is None:
            return None
        return preferences.default_fov_correction(
            section_key,mapping_name,
        )

    def _load_fov_mapping_correction(
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

    def fov_mapping_context(
        self,section_key: str,
    ) -> FOVMappingRuntimeContext:
        """Resolve mapping-scoped default and session Apply state."""
        section = str(section_key)
        mapping = self._active_mapping(section)
        key = (section,mapping)
        names = (
            () if mapping is None else self._mappings.fov_correction_names(section,mapping)
        )
        selection = self._fov_mapping_selections.get(key)
        default_name = self._saved_fov_mapping_correction_name(section,mapping)
        default_valid = bool(default_name and default_name in names and mapping)
        default_calibration = (
            self._load_fov_mapping_correction(section,mapping,default_name)
            if default_valid else None
        )
        default_compatible = self._correction_compatible(section,default_calibration)
        if selection is None:
            selection = _FOVMappingRuntimeSelection(
                selected_name=default_name if default_valid else None,
                applied_name=(
                    default_name if default_valid and default_compatible else None
                ),
                applied=bool(default_valid and default_compatible),
            )
            self._fov_mapping_selections[key] = selection
        selected = str(selection.selected_name or "").strip() or None
        if selected not in names:
            selected = None
            selection.selected_name = None
        applied_name = str(selection.applied_name or "").strip() or None
        applied_calibration = None
        if applied_name in names and mapping:
            applied_calibration = self._load_fov_mapping_correction(
                section,mapping,applied_name
            )
        applied_compatible = self._correction_compatible(
            section,applied_calibration
        )
        if selection.applied and not (applied_name and applied_compatible):
            selection.applied = False
            selection.applied_name = None
            applied_name = None
            applied_calibration = None
        applied = bool(selection.applied and applied_name and applied_compatible)
        runtime_context = self._runtime.get_section_fov_mapping_context(section)
        outside = np.asarray(runtime_context.get("outside_mask",()),dtype=bool)
        extrapolated_count = int(np.count_nonzero(outside)) if applied else 0
        calibration = (
            None if selected is None or mapping is None
            else self._load_fov_mapping_correction(section,mapping,selected)
        )
        selected_compatible = self._correction_compatible(section,calibration)
        reason = None
        if selected is not None and not selected_compatible:
            reason = "The selected FOV correction is incompatible with the current base mapping calibration."
        elif selected is None and default_name and not default_valid:
            reason = "The configured default FOV correction is not available for this mapping."
        elif selected is None and default_valid and not default_compatible:
            reason = "The default FOV correction is incompatible with the current base mapping calibration."
        elif selected is None and not default_name:
            reason = "No default FOV correction is configured for this mapping."
        return {
            "mapping_name":mapping,
            "available_names":tuple(names),
            "selected_name":selected,
            "selected_compatible":bool(selected is not None and selected_compatible),
            "applied_name":applied_name,
            "applied":applied,
            "default_name":default_name,
            "default_compatible":bool(default_valid and default_compatible),
            "compatibility_reason":reason,
            "supported":bool(runtime_context.get("supported",False)),
            "correction":calibration,
            "extrapolated_count":extrapolated_count,
            "target_count":int(np.asarray(runtime_context["ideal_positions_kxy"]).shape[1]),
            "max_extrapolation_kxy":float(runtime_context.get("max_extrapolation_kxy",0.0)),
            "coverage_tolerance_kxy":float(runtime_context.get("coverage_tolerance_kxy",0.0)),
        }

    def fov_mapping_preview(
        self,section_key: str,
    ) -> FOVMappingRuntimePreview | None:
        """Preview the selected saved correction at the current target when possible."""
        context = self.fov_mapping_context(section_key)
        calibration = context.get("correction")
        if calibration is None:
            return None
        current = self._runtime.get_section_fov_mapping_context(section_key)
        ideal = np.asarray(current["ideal_positions_kxy"],dtype=np.float64)
        # Inspect the field at the current target when meaningful. With no target
        # (or an unsupported target), fall back to the correction's saved samples.
        if bool(current.get("supported",False)) and ideal.shape[1] > 0:
            preview_positions = ideal
        else:
            preview_positions = np.asarray(
                calibration.sample_positions_kxy,dtype=np.float64,
            )
        predicted = calibration.evaluate(preview_positions)
        return {
            "source":"selected",
            "name":calibration.name,
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

    def select_fov_mapping_correction(
        self,section_key: str,name: str | None,
    ) -> None:
        """Select a saved correction for inspection and a later Apply action.

        Selecting alone does not change the runtime correction; Apply uses the
        selected compatible correction.  The mapping default remains the
        initial/default selection only.
        """
        self._require_editor_mode()
        section = str(section_key)
        mapping = self._active_mapping(section)
        if mapping is None:
            raise RuntimeError("Select a mapping before choosing an FOV correction")
        key = (section,mapping)
        selected = str(name or "").strip() or None
        if selected is not None:
            self._load_fov_mapping_correction(section,mapping,selected)
        selection = self._fov_mapping_selections.setdefault(
            key,_FOVMappingRuntimeSelection(),
        )
        selection.selected_name = selected
        self._section_changed(section)

    def set_fov_mapping_correction_applied(
        self,section_key: str,applied: bool,
    ) -> None:
        """Enable/disable the selected compatible correction for the session."""
        self._require_editor_mode()
        section = str(section_key)
        mapping = self._active_mapping(section)
        if mapping is None:
            raise RuntimeError("Select a mapping before applying an FOV correction")
        key = (section,mapping)
        selection = self._fov_mapping_selections.setdefault(
            key,_FOVMappingRuntimeSelection(),
        )
        applied = bool(applied)
        calibration = None
        if applied:
            name = str(selection.selected_name or "").strip() or None
            if name is None:
                name = self._saved_fov_mapping_correction_name(section,mapping)
            if not name or not self._mappings.fov_correction_exists(
                section,mapping,name
            ):
                raise RuntimeError("No selected FOV correction is available for this mapping")
            calibration = self._load_fov_mapping_correction(
                section,mapping,name
            )
            if not self._correction_compatible(section,calibration):
                raise RuntimeError(
                    "The selected FOV correction is incompatible with the current base mapping calibration"
                )
            if selection.selected_name is None:
                selection.selected_name = name
            selection.applied_name = name
        else:
            selection.applied_name = None
        selection.applied = applied
        self._set_runtime_fov_mapping_correction(section,calibration)
        self._section_changed(section)

    def restore_config_fov_mapping_correction(
        self,section_key: str,name: str | None,applied: bool,
    ) -> str | None:
        """Restore one config's named FOV intent without invalidating saved CGH.

        Returns a user-facing warning when the saved correction cannot be
        restored.  No default substitution is performed.
        """
        section = str(section_key)
        mapping = self._active_mapping(section)
        requested = str(name or "").strip() or None
        key = (section,mapping)
        selection = self._fov_mapping_selections.setdefault(
            key,_FOVMappingRuntimeSelection(),
        )
        selection.selected_name = None
        selection.applied_name = None
        selection.applied = False

        if mapping is None:
            self._runtime.restore_section_fov_mapping_correction(section,None)
            if requested is None:
                return None
            return (
                'Saved FOV correction "%s" could not be restored because no '
                "mapping is active for this section." % requested
            )

        if requested is None:
            self._runtime.restore_section_fov_mapping_correction(section,None)
            return None

        if not self._mappings.fov_correction_exists(section,mapping,requested):
            self._runtime.restore_section_fov_mapping_correction(section,None)
            return (
                'Saved FOV correction "%s" is not available for mapping "%s". '
                "No FOV correction was applied; select one manually if desired."
                % (requested,mapping)
            )

        calibration = self._load_fov_mapping_correction(
            section,mapping,requested
        )
        selection.selected_name = requested
        if applied:
            if not self._correction_compatible(section,calibration):
                self._runtime.restore_section_fov_mapping_correction(section,None)
                return (
                    'Saved FOV correction "%s" is incompatible with the current '
                    'base mapping "%s". No FOV correction was applied.'
                    % (requested,mapping)
                )
            selection.applied = True
            selection.applied_name = requested
            self._runtime.restore_section_fov_mapping_correction(
                section,calibration
            )
        else:
            self._runtime.restore_section_fov_mapping_correction(section,None)
        return None

    def set_default_fov_mapping_correction(self,section_key: str) -> None:
        """Persist the selected compatible correction as this mapping's default."""
        self._require_editor_mode()
        section = str(section_key)
        mapping = self._active_mapping(section)
        if mapping is None:
            raise RuntimeError("Select a mapping before setting an FOV default")
        context = self.fov_mapping_context(section)
        name = context.get("selected_name")
        if not name:
            raise RuntimeError("Choose an FOV correction before setting the default")
        calibration = self._load_fov_mapping_correction(section,mapping,name)
        if not self._correction_compatible(section,calibration):
            raise RuntimeError(
                "The selected FOV correction is incompatible with the current base mapping calibration"
            )
        preferences = self._startup_preferences_provider()
        if preferences is None:
            raise RuntimeError("Startup preferences are unavailable")
        preferences.set_default_fov_correction(section,mapping,name)
        self.set_fov_mapping_correction_applied(section,True)

    def delete_fov_mapping_correction(self,section_key: str) -> str:
        """Delete the selected record first, then clear matching default and draft state."""
        self._require_editor_mode()
        section = str(section_key)
        context = self.fov_mapping_context(section)
        mapping = context.get("mapping_name")
        name = context.get("selected_name")
        if not mapping or not name:
            raise RuntimeError("Choose an FOV correction before deleting it")
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
        self._section_changed(section)
        return str(name)

    def _set_runtime_fov_mapping_correction(
        self,section_key: str,calibration: FOVMappingCorrection | None,
        *,notify: bool=True,
    ) -> None:
        # Context refreshes happen for all sections on mapping/catalog changes.
        # Do not cancel a CGH request or create redundant callbacks when this
        # section already holds the exact same persistent field.
        current = None
        current_known = False
        try:
            current = self._runtime.get_section_fov_mapping_context(
                section_key
            ).get("correction")
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
        transition = self._runtime.set_section_fov_mapping_correction(
            section_key,calibration,
        )
        if notify:
            if transition is not None:
                self._transition_committed(section_key,transition)
            else:
                self._section_changed(section_key)

    def apply_startup_fov_mapping_defaults(self) -> None:
        """Explicitly reconcile default selections with the runtime after startup."""
        self.refresh_fov_mapping_contexts()

    def refresh_fov_mapping_contexts(self) -> None:
        """Reconcile mapping defaults, compatibility and per-session Apply state."""
        runtime = self._runtime
        for section_key in tuple(runtime.section_keys):
            section = str(section_key)
            mapping = self._active_mapping(section)
            key = (section,mapping)
            default_name = self._saved_fov_mapping_correction_name(section,mapping)
            default_exists = bool(
                default_name and mapping
                and self._mappings.fov_correction_exists(section,mapping,default_name)
            )
            default_calibration = (
                self._load_fov_mapping_correction(section,mapping,default_name)
                if default_exists else None
            )
            compatible = self._correction_compatible(section,default_calibration)
            selection = self._fov_mapping_selections.get(key)
            if selection is None:
                selection = _FOVMappingRuntimeSelection(
                    selected_name=default_name if default_exists else None,
                    applied_name=(
                        default_name if default_exists and compatible else None
                    ),
                    applied=bool(default_exists and compatible),
                )
                self._fov_mapping_selections[key] = selection
            applied_name = str(selection.applied_name or "").strip() or None
            calibration = None
            if applied_name and mapping and self._mappings.fov_correction_exists(
                section,mapping,applied_name
            ):
                candidate = self._load_fov_mapping_correction(
                    section,mapping,applied_name
                )
                if self._correction_compatible(section,candidate):
                    calibration = candidate
            if selection.applied and calibration is None:
                selection.applied = False
                selection.applied_name = None
            calibration = calibration if selection.applied else None
            self._set_runtime_fov_mapping_correction(
                section,calibration,notify=True,
            )

    def _on_fov_mapping_correction_store_changed(self) -> None:
        self.refresh_fov_mapping_contexts()

    def dispose(self) -> None:
        self._mappings.remove_fov_correction_listener(
            self._on_fov_mapping_correction_store_changed
        )
        self._fov_mapping_selections.clear()
