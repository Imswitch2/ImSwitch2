from __future__ import annotations
from copy import copy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any,Mapping
import numpy as np
from .artifacts import SectionArtifacts
from .context import SectionContext
from .geometry import SectionGeometry
from .model import SLMSectionState
from .presentation import SectionPresentation
from .snapshot import SLMSectionSnapshot
from .update import SectionUpdate

from ...calibration import SLMSectionCalibration
from ...config import SLMSectionConfig,SectionConfigLoadResult
from ..registry import SLMRegistries
from ..state import (
    ConfigWarning,DynamicGroupState,GroupTopology,ParamPath,
)
from ..transition import GroupStateDelta,SectionStateTransition
from ..corrections import (
    CorrectionProvider,CorrectionSourceInvalidatedError,ResolvedCorrections,
)
from ...measurement import ImageMeasurement

from ...cgh import (
    CGHJob,
    CGHResult,
    CGHSession,
    CGHSessionInspection,
    CGHSessionSnapshot,
    CGHStatus,
    FeedbackInspection,
    FeedbackStatus,
)
from ...cgh.targets.lattice import LatticeLockRequest
from ...cgh.localization import (
    TargetLocalizationReference,
    localization_context,
)

_UNSET = object()


@dataclass(frozen=True)
class _SectionTransitionPlan:
    context_changed: bool
    analytic_changed: bool
    aberrations_changed: bool
    cgh_pattern_changed: bool
    corrections_changed: bool

    @property
    def full_rebuild(self) -> bool:
        return self.context_changed

    @property
    def artifacts_recomputed(self) -> bool:
        return (
            self.full_rebuild
            or self.analytic_changed
            or self.aberrations_changed
            or self.cgh_pattern_changed
            or self.corrections_changed
        )


@dataclass(frozen=True)
class _PreparedSectionTransition:
    base_revision: int
    group_deltas: Mapping[str,GroupStateDelta]
    calibration_changed: bool
    cgh_pattern_changed: bool
    artifacts_recomputed: bool
    frame_changed: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,"group_deltas",MappingProxyType(dict(self.group_deltas)),
        )


@dataclass(frozen=True)
class _PreparedSectionConfigLoad:
    transition: _PreparedSectionTransition
    warnings: tuple[ConfigWarning, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self,"warnings",tuple(self.warnings))


class SLMSectionRuntime:
    """Authoritative holder for one section."""

    def __init__(
        self,
        *,
        geometry: SectionGeometry,
        pixel_size_um: float,
        state: SLMSectionState,
        registries: SLMRegistries,
        correction_provider: CorrectionProvider | None=None,
        calibration: SLMSectionCalibration | None=None,
        presentation: SectionPresentation | None=None,
    ) -> None:
        self.geometry = geometry
        self.pixel_size_um = pixel_size_um
        self.state = state
        self.registries = registries
        self.correction_provider = correction_provider
        self._saved_correction_override: ResolvedCorrections | None = None
        self.calibration = calibration
        self.presentation = (
            SectionPresentation()
            if presentation is None else presentation.copy()
        )

        self._revision = 0
        self._initialize_runtime()

    @classmethod
    def from_config(
        cls,
        config: SLMSectionConfig,
        *,
        pixel_size_um: float,
        registries: SLMRegistries,
        correction_provider: CorrectionProvider | None=None,
        correction_source: str="workspace",
        revision: int=1,
    ) -> "SLMSectionRuntime":
        """Construct one section directly from a persisted snapshot."""
        state,warnings = SLMSectionState.from_dict(
            registries=registries,state_dict=config.state.to_dict(),
        )
        if warnings:
            raise RuntimeError(
                f"Unexpected warnings while constructing section from config: "
                f"{warnings}"
            )

        runtime = object.__new__(cls)
        runtime.geometry = config.geometry
        runtime.pixel_size_um = float(pixel_size_um)
        runtime.state = state
        runtime.registries = registries
        runtime.correction_provider = correction_provider
        runtime._saved_correction_override = (
            config.correction_snapshot
            if str(correction_source).strip().lower() == "saved" else None
        )
        runtime.calibration = (
            None if config.calibration is None else config.calibration.copy()
        )
        runtime.presentation = config.presentation.copy()
        runtime._revision = int(revision)
        runtime._initialize_runtime(config.cgh_session)
        return runtime

    def _initialize_runtime(
        self,cgh_session: CGHSessionSnapshot | None=None,
    ) -> None:
        """Initialize the CGH session and artifacts for the current snapshot.

        Normal construction initializes an empty CGH session. Config loading may
        supply a persisted CGH result belonging to the revision already assigned
        to this runtime.
        """
        self._artifacts = None
        self._restore_fresh_cgh_session(cgh_session)
        self._artifacts = self._compute_state(self.state,self._revision)

    def _restore_fresh_cgh_session(
        self,snapshot: CGHSessionSnapshot | None,
    ) -> None:
        """Reset transient CGH state and restore the persisted result."""
        self._cgh_session = CGHSession(self.registries)
        self.validate()

        context = self._build_context(self.state)
        _,prepared_definition = self.state.cgh.canonicalize_selected_target(
            {},context,force=True,require_unchanged=True,
        )
        self._cgh_session.remember_prepared_definition(
            self.state.cgh,context,prepared_definition,
        )

        # Result restoration is intentionally independent from artifact work.
        # Config loading always resets target/feedback session state, while the
        # transition planner decides whether the numerical field changed.
        self._cgh_session.restore_snapshot(snapshot,context,lambda: None)

    # ----------------------------- #
    #       Public Properties       #
    # ----------------------------- #

    @property
    def artifacts(self) -> SectionArtifacts:
        """Return artifacts matching the committed section runtime."""
        if self._artifacts is None:
            raise RuntimeError("Section has no computed artifacts")

        if self._artifacts.source_revision != self._revision:
            raise RuntimeError(
                "Section artifacts do not match the committed section runtime"
            )

        return self._artifacts

    @property
    def cgh_result(self) -> CGHResult | None:
        return self._cgh_session.result

    @property
    def cgh_status(self) -> CGHStatus:
        """Return the CGH status for the currently committed section state."""
        context = self._build_context(self.state)
        return self._cgh_session.status(self.state.cgh,context)


    @property
    def feedback_status(self) -> FeedbackStatus:
        """Return a detached UI-facing snapshot of transient feedback state."""
        return self._cgh_session.feedback_status(
            self.state.cgh,self._build_context(self.state),
        )

    def get_feedback_inspection(self) -> FeedbackInspection:
        """Return immutable measurement/history data for inspection tools."""
        return self._cgh_session.feedback_inspection()

    def get_cgh_session_inspection(self) -> CGHSessionInspection:
        """Return coherent round/session data for future session UI."""
        return self._cgh_session.session_inspection(
            self.state.cgh,self._build_context(self.state),
        )


    # ---------------------- #
    #       Public API       #
    # ---------------------- #
    def load_config(
        self,
        config: SLMSectionConfig,
        calibration_policy: str="config",
        correction_source: str="workspace",
    ) -> SectionConfigLoadResult:
        """Transactionally replace this runtime from a complete section config."""
        candidate,prepared = self.prepare_config_load(
            config,
            calibration_policy=calibration_policy,
            correction_source=correction_source,
        )
        self._adopt(candidate)
        return SectionConfigLoadResult(
            transition=self._finalize_transition(prepared.transition),
            warnings=prepared.warnings,
            cgh_session_restored=True,
        )

    def apply_topology(
        self,topologies: Mapping[str,GroupTopology],
    ) -> SectionStateTransition | None:
        """Transactionally change selected group topology for this section."""
        prepared = self.prepare_topology_change(topologies)
        if prepared is None:
            return None

        candidate,transition = prepared
        self._adopt(candidate)
        return self._finalize_transition(transition)

    def prepare_config_load(
        self,
        config: SLMSectionConfig,
        *,
        calibration_policy: str="config",
        correction_source: str="workspace",
    ) -> tuple['SLMSectionRuntime', _PreparedSectionConfigLoad]:
        """Prepare a detached config candidate without committing it."""
        if config.geometry.shape != self.geometry.shape:
            raise ValueError(
                f"Section config shape {config.geometry.shape} does not match "
                f"runtime shape {self.geometry.shape}"
            )

        if config.state.registries is self.registries:
            state = config.state
            warnings = ()
        else:
            state,warnings = SLMSectionState.from_dict(
                registries=self.registries,
                state_dict=config.state.to_dict(),
            )

        calibration = self._select_config_calibration(
            config.calibration,self.calibration,calibration_policy,
        )

        if self.state.topology_signature() != state.topology_signature():
            candidate_state = state.clone()
        else:
            changes = self.state.diff_parameter_values(state)
            if changes:
                candidate_state = self.state.clone()
                candidate_state.apply_requested_values(changes)
            else:
                # Config activation may still reset transient CGH state even when
                # the authoritative parameter state is unchanged.
                candidate_state = self.state

        candidate = copy(self)
        candidate.state = candidate_state
        candidate.calibration = calibration
        candidate.presentation = config.presentation.copy()
        source = str(correction_source or "workspace").strip().lower()
        if source not in ("workspace","saved"):
            raise ValueError("correction_source must be 'workspace' or 'saved'")
        candidate._saved_correction_override = (
            config.correction_snapshot if source == "saved" else None
        )
        candidate._revision = self._revision + 1
        candidate._artifacts = self._artifacts

        # Config activation has config-specific session semantics: transient
        # target/feedback state is reset and the persisted result is restored.
        candidate._restore_fresh_cgh_session(config.cgh_session)

        prepared = self._prepare_state_transition(candidate)
        return candidate,_PreparedSectionConfigLoad(
            transition=prepared,warnings=warnings,
        )

    def prepare_topology_change(
        self,topologies: Mapping[str,GroupTopology],
    ) -> tuple['SLMSectionRuntime', _PreparedSectionTransition] | None:
        """Prepare one live topology edit while preserving runtime session state.

        ``topologies`` is a partial mapping: groups not present are left exactly
        as they are. Dynamic groups preserve retained item state, initialize new
        items from registry defaults, and destructively clear disabled groups
        according to ``DynamicGroupState`` semantics.
        """
        if not isinstance(topologies,Mapping):
            raise TypeError("topologies must be a mapping")
        if not topologies:
            return None

        candidate = self.clone()
        before = self.state.group_topologies()

        for group_key,topology in topologies.items():
            if not isinstance(topology,GroupTopology):
                raise TypeError(
                    f"Topology for group '{group_key}' must be a GroupTopology"
                )

            group = candidate.state.group_by_key(group_key)
            current_topology = before[group_key]

            if isinstance(group,DynamicGroupState):
                group.set_enabled(topology.enabled)
                group.set_enabled_items(topology.item_keys)
            elif topology != current_topology:
                raise ValueError(
                    f"Static group '{group_key}' does not support topology changes"
                )

        candidate.validate()
        if candidate.state.topology_signature() == self.state.topology_signature():
            return None

        candidate._revision = self._revision + 1
        candidate._cgh_session.reconcile_target_definition(
            candidate.state.cgh,candidate._build_context(candidate.state),
        )
        prepared = self._prepare_state_transition(candidate)
        return candidate,prepared

    def create_snapshot(self) -> SLMSectionSnapshot:
        """Return a detached snapshot of this committed section runtime."""
        calibration = (
            None if self.calibration is None else self.calibration.copy()
        )
        return SLMSectionSnapshot(
            revision=self._revision,
            geometry=self.geometry,
            state=self.state.clone(),
            calibration=calibration,
            cgh_status=self.cgh_status,
            presentation=self.presentation.copy(),
        )

    def _finalize_transition(
        self,prepared: _PreparedSectionTransition,
    ) -> SectionStateTransition:
        """Bind prepared semantic deltas to the committed section snapshot."""
        return SectionStateTransition(
            base_revision=prepared.base_revision,
            snapshot=self.create_snapshot(),
            group_deltas=prepared.group_deltas,
            calibration_changed=prepared.calibration_changed,
            cgh_pattern_changed=prepared.cgh_pattern_changed,
            artifacts_recomputed=prepared.artifacts_recomputed,
            frame_changed=prepared.frame_changed,
        )

    def clone(self) -> "SLMSectionRuntime":
        """Return a detached runtime candidate equivalent to this runtime."""
        candidate = copy(self)

        candidate.state = self.state.clone()
        candidate.calibration = (
            None if self.calibration is None else self.calibration.copy()
        )
        candidate.presentation = self.presentation.copy()
        candidate._cgh_session = self._cgh_session.clone()

        # Safe to share because SectionArtifacts and its arrays are immutable.
        candidate._artifacts = self._artifacts

        return candidate


    def create_config(self) -> SLMSectionConfig:
        """ Create and returns detached config for this section"""
        calibration = None
        if self.calibration is not None:
            calibration = self.calibration.copy()

        config = SLMSectionConfig(
            geometry=self.geometry,
            state=self.state.clone(),
            correction_snapshot=self.artifacts.resolved_corrections,
            calibration=calibration,
            cgh_session=self._cgh_session.session_snapshot,
            presentation=self.presentation.copy(),
        )
        return config.clone(self.registries)

    def set_presentation(
        self,presentation: SectionPresentation,
    ) -> SLMSectionSnapshot | None:
        """Replace presentation preferences without touching runtime artifacts."""
        if not isinstance(presentation,SectionPresentation):
            raise TypeError(
                "presentation must be a SectionPresentation"
            )

        presentation = presentation.copy()
        if presentation == self.presentation:
            return None

        self.presentation = presentation
        return self.create_snapshot()

    def apply_patch(
        self,
        changes: Mapping[ParamPath,Any],
        *,
        lattice_lock_request: LatticeLockRequest | None=None,
    ) -> SectionUpdate | None:
        """Transactionally apply parameter and optional raster-lock changes."""

        candidate = self.state.clone()
        normalized_values = candidate.apply_requested_values(changes)

        lock_changed = False
        if lattice_lock_request is not None:
            request = lattice_lock_request
            lock_changed = candidate.cgh.set_target_lock(
                request.target_key,request.kind,request.reference,
            )

        cgh_changes = {
            path[1:]:value for path,value in normalized_values.items()
            if len(path) > 1 and path[0] == "cgh"
        }
        changed_target,changed_keys = candidate.cgh._extract_target_param_changes(
            cgh_changes,
        )
        if changed_target is not None:
            lock_changed = (
                candidate.cgh.refresh_target_lock_reference_from_changes(
                    changed_target,changed_keys,
                )
                or lock_changed
            )

        return self._apply_candidate_transaction(
            candidate,
            normalized_values=normalized_values,
            calibration=_UNSET,
            extra_change=lock_changed,
        )


    def set_calibration(
        self,calibration: SLMSectionCalibration | None,
    ) -> SectionUpdate | None:
        """Transactionally replace calibration and rebuild dependent state."""

        if calibration is not None and not isinstance(
            calibration,SLMSectionCalibration
        ):
            raise TypeError(
                "calibration must be an SLMSectionCalibration or None"
            )

        # Compare calibration normalized copies:
        # SLMSectionCalibration.copy() may normalize a diagonal mapping
        # into an explicit matrix representation.
        next_calib = None if calibration is None else calibration.copy()
        current_calib = None if self.calibration is None else self.calibration.copy()

        current_data = None if current_calib is None else current_calib.to_dict()
        next_data = None if next_calib is None else next_calib.to_dict()

        calibration_changed = current_data != next_data

        # build candidate and finalize update transactionnally
        candidate = self.state.clone()
        update = self._apply_candidate_transaction(
            candidate,
            normalized_values={},
            calibration=next_calib,
            extra_change=calibration_changed
        )

        return update

    def validate(self) -> None:
        """Validate runtime invariants and the complete section state."""
        if self.state.registries is not self.registries:
            raise RuntimeError(
                "Runtime and section state must use the same SLMRegistries instance"
            )

        self.state.validate()

    def iter_parameters(self):
        return self.state.iter_parameters()

    def resolve_parameter(self, path: ParamPath):
        return self.state.resolve_parameter(path)

    def get_parameter(self, path: ParamPath) -> Any:
        return self.state.get_parameter(path)

    def prepare_cgh(self) -> CGHJob:
        """Compatibility preparation path using session-inferred intent."""
        self.validate()
        return self._cgh_session.prepare(
            self.state.cgh,self._build_context(self.state),
        )

    def prepare_base_cgh(self) -> CGHJob:
        """Prepare a fresh base CGH without discarding the old session yet."""
        self.validate()
        return self._cgh_session.prepare_base(
            self.state.cgh,self._build_context(self.state),
        )

    def prepare_adapted_cgh(self) -> CGHJob:
        """Prepare exactly the pending feedback-adapted working round."""
        self.validate()
        return self._cgh_session.prepare_adapted(
            self.state.cgh,self._build_context(self.state),
        )

    def set_feedback_measurement(
        self,measurement: ImageMeasurement,
    ) -> None:
        """Store one host-supplied measurement after target synchronization."""
        candidate = self._cgh_session.clone()
        candidate.set_feedback_measurement(
            self.state.cgh,
            self._build_context(self.state),
            measurement,
        )
        self._cgh_session = candidate

    def update_feedback_parameters(
        self,group: str,changes: Mapping[str,Any],
    ) -> bool:
        """Update transient localization/intensity/position parameters."""
        candidate = self._cgh_session.clone()
        changed = candidate.update_feedback_parameters(group,changes)
        if changed:
            self._cgh_session = candidate
        return changed

    def get_feedback_localization_context(self):
        candidate = self._cgh_session.clone()
        return candidate.feedback_localization_context(
            self.state.cgh,self._build_context(self.state),
        )

    def get_base_target_localization_reference(
        self,
    ) -> TargetLocalizationReference:
        """Return calibration-free structural target data for localization.

        This path intentionally bypasses feedback state and uses the selected
        target's base resolution. It is suitable for calibration workflows that
        must not depend on intensity/position feedback adaptation or existing
        detector-space calibration hints.
        """
        context = self._build_context(self.state,calibration=None)
        target = self._build_base_target(self.state,context)
        target_context = localization_context(
            target_type=target.target_type,
            target_params=target.params,
            resolution=target.resolution,
            calibration=None,
        )
        return TargetLocalizationReference(
            target_type=target.target_type,
            target_params=target.params,
            resolution=target.resolution,
            localization_context=target_context,
            target_signature=target.signature,
        )

    def compute_feedback_intensity_analysis(self,localization=None):
        """Calculate centralized experimental intensity analysis without mutation."""
        candidate = self._cgh_session.clone()
        return candidate.compute_feedback_intensity_analysis(
            self.state.cgh,self._build_context(self.state),localization,
        )

    def set_feedback_intensity_analysis(self,analysis) -> None:
        """Store centralized experimental intensity analysis for the current round."""
        candidate = self._cgh_session.clone()
        candidate.set_feedback_intensity_analysis(analysis)
        self._cgh_session = candidate

    def compute_feedback_measurement_metrics(self,localization=None):
        """Calculate geometry-defined metrics without mutating section state."""
        candidate = self._cgh_session.clone()
        return candidate.compute_feedback_measurement_metrics(
            self.state.cgh,self._build_context(self.state),localization,
        )

    def set_feedback_measurement_metrics(self,metrics) -> None:
        """Store metrics on the current accepted feedback measurement."""
        candidate = self._cgh_session.clone()
        candidate.set_feedback_measurement_metrics(metrics)
        self._cgh_session = candidate

    def compute_feedback_localization_candidate(
        self,parameters: Mapping[str,Any],
    ):
        """Compute a candidate without committing transient session state."""
        candidate = self._cgh_session.clone()
        return candidate.compute_feedback_localization_candidate(
            self.state.cgh,
            self._build_context(self.state),
            parameters,
        )

    def commit_feedback_localization(
        self,localization,parameters: Mapping[str,Any],
    ):
        candidate = self._cgh_session.clone()
        result = candidate.commit_feedback_localization(
            self.state.cgh,
            self._build_context(self.state),
            localization,
            parameters,
        )
        self._cgh_session = candidate
        return result

    def reuse_feedback_localization(self):
        candidate = self._cgh_session.clone()
        result = candidate.reuse_feedback_localization(
            self.state.cgh,self._build_context(self.state),
        )
        self._cgh_session = candidate
        return result

    def localize_feedback(self):
        """Retain the existing non-interactive convenience API."""
        candidate = self._cgh_session.clone()
        result = candidate.localize_feedback(
            self.state.cgh,self._build_context(self.state),
        )
        self._cgh_session = candidate
        return result

    def apply_intensity_feedback(self) -> bool:
        """Apply one iterative intensity round and advance the session revision."""
        candidate = self._cgh_session.clone()
        candidate.apply_intensity_feedback(
            self.state.cgh,self._build_context(self.state),
        )
        self._commit_feedback_session(candidate,True)
        return True

    def reset_intensity_feedback(self) -> bool:
        """Reset iterative intensity feedback to the base target intensities."""
        next_revision = self._revision + 1
        candidate_session = self._cgh_session.clone()
        changed = candidate_session.reset_intensity_feedback(
            self.state.cgh,self._build_context(self.state),
        )
        if not changed:
            return False

        previous_session = self._cgh_session
        self._cgh_session = candidate_session
        try:
            artifacts = self._compute_cgh_changed_artifacts(next_revision)
        except Exception:
            self._cgh_session = previous_session
            raise

        self._artifacts = artifacts
        self._revision = next_revision
        return True

    def apply_position_correction(
            self, *, reset_intensity: bool=False
        ) -> bool:
        """Calculate/replace the one-shot position correction from ideal positions."""
        candidate = self._cgh_session.clone()
        _correction,effective_changed = candidate.apply_position_correction(
            self.state.cgh,
            self._build_context(self.state),
            reset_intensity=reset_intensity,
        )
        self._commit_feedback_session(candidate,effective_changed)
        return effective_changed

    def set_position_correction_active(
        self,active: bool,*,reset_intensity: bool=False,
    ) -> bool:
        """Enable/disable an existing position correction."""
        candidate = self._cgh_session.clone()
        effective_changed = candidate.set_position_correction_active(
            self.state.cgh,self._build_context(self.state),bool(active),
            reset_intensity=reset_intensity,
        )
        self._commit_feedback_session(candidate,effective_changed)
        return effective_changed

    def clear_position_correction(self,*,reset_intensity: bool=False) -> bool:
        """Forget the current position correction and restart intensity rounds."""
        candidate = self._cgh_session.clone()
        effective_changed = candidate.clear_position_correction(
            self.state.cgh,self._build_context(self.state),
            reset_intensity=reset_intensity,
        )
        # Clearing an inactive correction still changes transient session state.
        self._commit_feedback_session(
            candidate,effective_changed,commit_when_unchanged=True,
        )
        return effective_changed

    def create_target_preview(self) -> np.ndarray:
        """Return a detached target preview without advancing CGH generation."""
        self.validate()

        context = self._build_context(self.state)

        # Use a cloned session so preview generation also leaves persistent target
        # and feedback runtime state untouched.
        session = self._cgh_session.clone()
        resolution = session.create_target_resolution(self.state.cgh,context)
        return resolution.preview

    def restore_current_cgh_target(self) -> SectionUpdate | None:
        """Restore editable target parameters to the committed CGH base target.

        This is intentionally not a normal user-edit replay: the current
        lattice-lock reference is preserved while the target parameters are
        restored to the canonical values used by the committed CGH.
        """
        snapshot = self._cgh_session.session_snapshot
        committed = None if snapshot is None else snapshot.committed_target
        if committed is None:
            return None
        if committed.target_type not in self.state.cgh.items:
            raise RuntimeError(
                f"Committed CGH target '{committed.target_type}' is not available"
            )

        cgh = self.state.cgh
        changes = {
            (cgh.GROUP_KEY,) + cgh.selected_target_path():committed.target_type,
        }
        prefix = (cgh.GROUP_KEY,) + cgh.target_params_path(
            committed.target_type,
        )
        changes.update({
            prefix + (key,):value
            for key,value in committed.canonical_params.items()
        })

        candidate = self.state.clone()
        normalized_values = candidate.apply_requested_values(changes)
        return self._apply_candidate_transaction(
            candidate,
            normalized_values=normalized_values,
            calibration=_UNSET,
            expected_target_signature=committed.target_signature,
        )


    def commit_cgh(self,result: CGHResult) -> bool:
        """Commit a computed CGH result as one section-runtime transaction."""
        context = self._build_context(self.state)
        next_revision = self._revision + 1

        # CGHSession temporarily installs the candidate result before invoking
        # this callback.
        def build_artifacts() -> SectionArtifacts:
            return self._compute_cgh_changed_artifacts(next_revision)

        artifacts = self._cgh_session.commit(
            result,self.state.cgh,context,build_artifacts,
        )

        return self._commit_cgh_artifacts(artifacts,next_revision)

    def mark_cgh_compute_failed(self,generation: int,message: Any) -> bool:
        """Mark the currently prepared CGH compute as failed, if still current."""
        candidate = self._cgh_session.clone()
        changed = candidate.mark_compute_failed(generation,message)
        if changed:
            self._cgh_session = candidate
        return changed

    def invalidate_cgh_compute(self) -> bool:
        """Invalidate any outstanding prepared CGH compute request."""
        candidate = self._cgh_session.clone()
        changed = candidate.invalidate_prepared_compute()
        if changed:
            self._cgh_session = candidate
        return changed

    def discard_cgh_working_round(self) -> bool:
        """Discard any transient incomplete CGH round."""
        candidate = self._cgh_session.clone()
        changed = candidate.discard_working_round()
        if changed:
            self._cgh_session = candidate
        return changed

    def reset_cgh_to_round(self,index: int) -> bool:
        """Truncate complete CGH history after one round."""
        next_revision = self._revision + 1

        candidate_session = self._cgh_session.clone()
        if not candidate_session.reset_to_round(index):
            return False

        previous_session = self._cgh_session
        self._cgh_session = candidate_session
        try:
            artifacts = self._compute_cgh_changed_artifacts(next_revision)
        except Exception:
            self._cgh_session = previous_session
            raise

        self._artifacts = artifacts
        self._revision = next_revision
        return True

    def clear_cgh_session(self) -> bool:
        """Clear the current CGH session/result as one runtime transaction."""
        next_revision = self._revision + 1

        # CGHSession temporarily clears its result before invoking this callback.
        def build_artifacts() -> SectionArtifacts:
            return self._compute_cgh_changed_artifacts(next_revision)

        artifacts = self._cgh_session.clear(build_artifacts)

        return self._commit_cgh_artifacts(artifacts,next_revision)

    # -------------------------- #
    #      Internal Methods      #
    # -------------------------- #

    def _apply_candidate_transaction(
        self,
        candidate: SLMSectionState,
        normalized_values: Mapping[ParamPath,Any],
        calibration=_UNSET,
        extra_change: bool = False,
        expected_target_signature=None,
    ) -> SectionUpdate | None:
        """Finalize a candidate section state transaction and commit on success."""
        previous_state = self.state
        previous_context = self._build_context(previous_state)
        candidate_context = self._build_context(candidate,calibration=calibration)

        cgh_changed_values,prepared_definition = self._canonicalize_cgh(
            self.state,candidate,normalized_values,previous_context,candidate_context,
        )

        candidate.validate()
        previous_target_signature = self._canonical_target_definition_signature(
            previous_state,previous_context,
        )
        candidate_target_signature = self._canonical_target_definition_signature(
            candidate,candidate_context,
        )
        target_definition_changed = (
            previous_target_signature != candidate_target_signature
        )
        if (
            expected_target_signature is not None
            and candidate_target_signature != expected_target_signature
        ):
            raise RuntimeError(
                "Restored CGH target parameters did not reproduce the "
                "committed target definition"
            )

        # check if there is any effective change
        applied_values = dict(normalized_values)
        applied_values.update(cgh_changed_values)

        changed = extra_change or any(
            self.state.get_parameter(path) != value
            for path,value in applied_values.items()
        )

        if not changed:
            return None

        # compute state
        next_revision = self._revision + 1
        artifacts = self._compute_state(
            candidate,next_revision,calibration=calibration
        )
        frame_changed = not self._optional_arrays_equal(
            self.artifacts.eightbit,artifacts.eightbit,
        )

        # Reconcile target-dependent feedback transactionally. Parameter or
        # calibration changes start a fresh feedback runtime, while the old CGH
        # result remains available and is classified independently as stale.
        candidate_session = self._cgh_session.clone()
        candidate_session.reconcile_target_definition(
            candidate.cgh,candidate_context,
        )

        # construct SectionUpdate
        cgh_status = candidate_session.status(
            candidate.cgh,candidate_context
        )

        candidate_calibration = (
            self.calibration if calibration is _UNSET else calibration
        )
        update = SectionUpdate.from_transition(
            previous_state=self.state,
            current_state=candidate,
            base_revision=self._revision,
            revision=next_revision,
            normalized_values=normalized_values,
            cgh_changed_values=cgh_changed_values,
            cgh_status=cgh_status,
            calibration=candidate_calibration,
            target_definition_changed=target_definition_changed,
            frame_changed=frame_changed,
            warnings=(),
        )

        # Commit only after canonicalization, validation, artifact
        # construction and update construction have all succeeded.
        self.state = candidate
        if calibration is not _UNSET:
            self.calibration = calibration
        self._artifacts = artifacts
        self._revision = next_revision
        candidate_session.remember_prepared_definition(
            candidate.cgh,candidate_context,prepared_definition,
        )
        self._cgh_session = candidate_session
        return update


    def _commit_feedback_session(
        self,
        candidate_session: CGHSession,
        effective_changed: bool,
        *,
        commit_when_unchanged: bool=True,
    ) -> None:
        """Commit one validated feedback-session candidate.

        Effective target-resolution changes advance the section revision so CGH
        validity snapshots remain monotonic. The already-applied CGH artifacts
        are deliberately retained and only receive the new provenance revision.
        """
        if effective_changed:
            next_revision = self._revision + 1
            self._cgh_session = candidate_session
            self._artifacts = self.artifacts.with_revision(next_revision)
            self._revision = next_revision
        elif commit_when_unchanged:
            self._cgh_session = candidate_session

    def _adopt(self,candidate: SLMSectionRuntime) -> None:
        self.state=candidate.state
        self._revision=candidate._revision
        self.calibration=candidate.calibration
        self._saved_correction_override=candidate._saved_correction_override
        self._artifacts=candidate.artifacts
        self._cgh_session=candidate._cgh_session

    def _prepare_state_transition(
        self,candidate: "SLMSectionRuntime",
    ) -> _PreparedSectionTransition:
        """Plan and build a detached transition from effective inputs.

        Structural changes are deliberately absent from the numerical plan.
        They are reported through ``group_deltas``; recomputation is driven only
        by the effective inputs consumed by each artifact component.
        """
        candidate.validate()
        group_deltas = self._build_group_deltas(candidate.state)
        calibration_changed = not self._calibrations_equal(
            self.calibration,candidate.calibration,
        )

        previous_context = self._build_context(self.state)
        candidate_context = candidate._build_context(candidate.state)
        previous_cgh = self._effective_cgh_pattern(
            self._cgh_session,self.state,previous_context,
        )
        candidate_cgh = candidate._effective_cgh_pattern(
            candidate._cgh_session,candidate.state,candidate_context,
        )
        cgh_pattern_changed = not self._optional_arrays_equal(
            previous_cgh,candidate_cgh,
        )

        plan = _SectionTransitionPlan(
            context_changed=(
                self._context_inputs(self.state,self.calibration)
                != self._context_inputs(candidate.state,candidate.calibration)
            ),
            analytic_changed=(
                self._analytic_inputs(self.state)
                != self._analytic_inputs(candidate.state)
            ),
            aberrations_changed=(
                self._aberration_inputs(self.state)
                != self._aberration_inputs(candidate.state)
            ),
            cgh_pattern_changed=cgh_pattern_changed,
            corrections_changed=not self._effective_corrections_equal(candidate),
        )

        candidate._artifacts = candidate._build_transition_artifacts(plan)
        frame_changed = not self._optional_arrays_equal(
            self.artifacts.eightbit,candidate.artifacts.eightbit,
        )

        return _PreparedSectionTransition(
            base_revision=self._revision,
            group_deltas=group_deltas,
            calibration_changed=calibration_changed,
            cgh_pattern_changed=cgh_pattern_changed,
            artifacts_recomputed=plan.artifacts_recomputed,
            frame_changed=frame_changed,
        )

    def _build_group_deltas(
        self,candidate_state: SLMSectionState,
    ) -> Mapping[str,GroupStateDelta]:
        before_topologies = self.state.group_topologies()
        after_topologies = candidate_state.group_topologies()
        changed_values = self.state.diff_group_parameter_values(candidate_state)
        deltas = {}

        for group_key,before_topology in before_topologies.items():
            after_topology = after_topologies[group_key]
            values = changed_values.get(group_key,{})
            if before_topology != after_topology or values:
                deltas[group_key] = GroupStateDelta(
                    before_topology=before_topology,
                    after_topology=after_topology,
                    changed_values=values,
                )

        return deltas

    def _build_transition_artifacts(
        self,plan: _SectionTransitionPlan,
    ) -> SectionArtifacts:
        """Build only artifact components affected by one state transition."""
        revision = self._revision
        current = self._artifacts
        if current is None:
            raise RuntimeError("Section has no current artifacts")

        if plan.full_rebuild:
            return self._compute_state(self.state,revision)

        if not plan.artifacts_recomputed:
            return current.with_revision(revision)

        context = self._build_context(self.state)

        if (
            plan.corrections_changed
            and not plan.analytic_changed
            and not plan.aberrations_changed
            and not plan.cgh_pattern_changed
        ):
            eightbit,resolved_corrections = self._phase_to_eightbit_from_phase(
                current.phase,context,self.state,
            )
            return SectionArtifacts.from_owned(
                analytic=current.analytic,
                aberrations=current.aberrations,
                cgh=current.cgh,
                combined=current.combined,
                phase=current.phase,
                eightbit=eightbit,
                resolved_corrections=resolved_corrections,
                source_revision=revision,
            )

        analytic = (
            self._compute_analytic(context,self.state)
            if plan.analytic_changed else current.analytic
        )
        aberrations = (
            self._compute_aberrations(context,self.state)
            if plan.aberrations_changed else current.aberrations
        )

        if plan.cgh_pattern_changed:
            cgh = self._cgh_session.resolve(self.state.cgh,context)
            if cgh is None:
                cgh = self._identity_field(context)
        else:
            cgh = current.cgh

        combined = analytic*aberrations*cgh
        combined = self._apply_pupil(combined,context)
        phase,eightbit,resolved_corrections = self._phase_to_eightbit(
            combined,context,self.state,
        )

        return SectionArtifacts.from_owned(
            analytic=analytic,
            aberrations=aberrations,
            cgh=cgh,
            combined=combined,
            phase=phase,
            eightbit=eightbit,
            resolved_corrections=resolved_corrections,
            source_revision=revision,
        )

    @staticmethod
    def _calibrations_equal(
        first: SLMSectionCalibration | None,
        second: SLMSectionCalibration | None,
    ) -> bool:
        first_data = None if first is None else first.copy().to_dict()
        second_data = None if second is None else second.copy().to_dict()
        return first_data == second_data

    @staticmethod
    def _context_inputs(
        state: SLMSectionState,
        calibration: SLMSectionCalibration | None,
    ):
        """Return inputs that force a complete section artifact rebuild."""
        optics = state.optics
        calibration_data = (
            None if calibration is None else calibration.copy().to_dict()
        )
        return (
            optics.wavelength_nm,
            optics.pupil_radius_px,
            optics.center_offset_x_px,
            optics.center_offset_y_px,
            calibration_data,
        )

    @staticmethod
    def _optional_arrays_equal(
        first: np.ndarray | None,second: np.ndarray | None,
    ) -> bool:
        if first is None or second is None:
            return first is second
        return first is second or np.array_equal(first,second)

    @staticmethod
    def _effective_cgh_pattern(
        session: CGHSession,
        state: SLMSectionState,
        context: SectionContext,
    ) -> np.ndarray | None:
        return session.resolve(state.cgh,context)

    @staticmethod
    def _analytic_inputs(state: SLMSectionState):
        group = state.patterns
        if not group.enabled or not group.active:
            return None

        active_items = []
        for key,item in group.items.items():
            values = dict(item.params.values)
            if values.pop("active",False):
                active_items.append((key,values))
        return tuple(active_items) or None

    @staticmethod
    def _aberration_inputs(state: SLMSectionState):
        group = state.aberrations
        if not group.enabled or not group.active:
            return None
        return tuple(
            (key,dict(item.params.values))
            for key,item in group.items.items()
        ) or None

    def _effective_corrections_equal(
        self,candidate: "SLMSectionRuntime",
    ) -> bool:
        first = self._resolve_corrections(self.state,self.geometry)
        second = candidate._resolve_corrections(candidate.state,candidate.geometry)
        first_state = self.state.corrections
        second_state = candidate.state.corrections
        first_pattern = bool(
            first_state.active and first_state.apply_correction_pattern
        )
        second_pattern = bool(
            second_state.active and second_state.apply_correction_pattern
        )
        first_twopi = bool(first_state.active and first_state.apply_twopi_value)
        second_twopi = bool(second_state.active and second_state.apply_twopi_value)
        if (first_pattern,first_twopi) != (second_pattern,second_twopi):
            return False
        return first.numerically_equal(
            second,
            compare_pattern=first_pattern,
            compare_twopi=first_twopi,
        )



    def _commit_cgh_artifacts(
        self,
        artifacts: SectionArtifacts | None,
        revision: int,
    ) -> bool:
        """Commit artifacts and revision after a successful CGHSession operation."""
        if artifacts is None:
            # The session rejected the result or the requested operation was a no-op.
            return False

        self._artifacts = artifacts
        self._revision = revision
        return True


    @staticmethod
    def _select_config_calibration(
        config_calibration: SLMSectionCalibration | None,
        runtime_calibration: SLMSectionCalibration | None,
        policy: str,
    )-> SLMSectionCalibration | None:
        if policy=="config":
            selected = config_calibration
        elif policy=="runtime":
            selected = runtime_calibration
        elif policy == "require_match":
            config_normalized = (
                None if config_calibration is None else config_calibration.copy()
            )
            runtime_normalized = (
                None if runtime_calibration is None else runtime_calibration.copy()
            )

            config_data = (
                None if config_normalized is None else config_normalized.to_dict()
            )
            runtime_data = (
                None if runtime_normalized is None else runtime_normalized.to_dict()
            )

            if config_data != runtime_data:
                raise ValueError("Config calibration doesn't match runtime calibration")

            selected = runtime_normalized
        else:
            raise ValueError(
                f"calibration_policy must be 'config','runtime' or"
                f"'require_match' but got {policy}."
            )
        return None if selected is None else selected.copy()

    def _canonicalize_cgh(
            self,
            current_state: SLMSectionState,
            candidate: SLMSectionState,
            normalized_values: Mapping[ParamPath,Any],
            previous_context: SectionContext,
            candidate_context: SectionContext,
        ):
        """Canonicalize the candidate CGH target for its candidate context."""
        cgh_changes = {
            path[1:]:value for path,value in normalized_values.items()
            if len(path) > 1 and path[0] == "cgh"
        }

        # Probe the candidate signature after target canonicalization so context
        # changes that alter the resolved target definition still force CGH
        # canonicalization. The canonicalizer below mutates/reports changes, so
        # this dry probe intentionally duplicates one deterministic call rather
        # than coupling this transaction to CGHState's internal mutation path.
        force_change = (
            self._target_definition_signature(current_state,previous_context)
            != self._target_definition_signature(
                candidate,candidate_context,cgh_changes,
            )
        )
        cgh_changed_values,prepared_definition = (
            candidate.cgh.canonicalize_selected_target(
                cgh_changes,candidate_context,force=force_change,
            )
        )
        cgh_changed_values = {
            ("cgh",) + path:value
            for path,value in cgh_changed_values.items()
        }
        return cgh_changed_values, prepared_definition

    def _canonical_target_definition_signature(
        self,state: SLMSectionState,context: SectionContext,
    ):
        """Return base-target identity from already-canonical section state."""
        target_type = state.cgh.selected_target
        if target_type is None:
            return None
        target_class = self.registries.targets[target_type].target_class
        params = dict(state.cgh.items[target_type].params.values)
        return target_class.definition_signature_for(context,params)

    def _target_definition_signature(
        self,state: SLMSectionState,context: SectionContext,changes=(),
    ):
        target_type = state.cgh.selected_target
        if target_type is None:
            return None
        _changed_target,changed_keys = state.cgh._extract_target_param_changes(
            changes,
        )
        target_class = self.registries.targets[target_type].target_class
        params = dict(state.cgh.items[target_type].params.values)
        target_state = state.cgh.items[target_type]
        canonical_params,_prepared_definition = target_class.canonicalize_params(
            params,changed_keys,context=context,
            lock_state=target_state.lock_state,
        )
        canonical_params = target_class._validated_params(canonical_params)
        target_class.validate_params(canonical_params)
        return target_class.definition_signature_for(
            context,canonical_params,
        )

    def _build_base_target(
        self,state: SLMSectionState,context: SectionContext,
    ):
        """Build the selected target without feedback-effective adaptation."""
        target_type = state.cgh.selected_target
        if target_type is None:
            raise RuntimeError("No CGH target selected")
        target_state = state.cgh.items[target_type]
        registration = self.registries.targets[target_type]
        target_class = registration.target_class

        current_params = dict(target_state.params.values)
        canonical_params,prepared_definition = target_class.canonicalize_params(
            current_params,(),context=context,
            lock_state=target_state.lock_state,
        )
        canonical_params = target_class._validated_params(canonical_params)
        target_class.validate_params(canonical_params)
        if canonical_params != current_params:
            raise RuntimeError(
                f"Canonical target '{target_type}' is stale for the current "
                "section context"
            )
        return target_class(
            context=context,
            prepared_definition=prepared_definition,
            **current_params,
        )

    def _build_context(
        self,state: SLMSectionState,calibration=_UNSET,
    ) -> SectionContext:
        """Build a detached snapshot of the section computation inputs.
        If calibration is unset, we use runtime's calibration. """
        optics = state.optics

        if calibration is _UNSET:
            calibration = self.calibration

        return SectionContext(
            geometry=self.geometry,
            pixel_size_um=self.pixel_size_um,
            wavelength_nm=optics.wavelength_nm,
            pupil_radius_px=optics.pupil_radius_px,
            center_offset_x_px=optics.center_offset_x_px,
            center_offset_y_px=optics.center_offset_y_px,
            calibration=calibration,
        )

    def _compute_state(
        self,state: SLMSectionState,revision: int,calibration=_UNSET,
    ) -> SectionArtifacts:
        """Compute artifacts from validated inputs without modifying runtime."""
        self.registries.validate()

        context = self._build_context(state,calibration=calibration)
        analytic = self._compute_analytic(context,state)
        aberrations = self._compute_aberrations(context,state)

        cgh =self._cgh_session.resolve(state.cgh,context)
        if cgh is None:
            cgh = self._identity_field(context)

        combined = analytic*aberrations*cgh
        combined = self._apply_pupil(combined,context)

        phase,eightbit,resolved_corrections = self._phase_to_eightbit(
            combined,context,state,
        )

        return SectionArtifacts.from_owned(
            analytic=analytic,
            aberrations=aberrations,
            cgh=cgh,
            combined=combined,
            phase=phase,
            eightbit=eightbit,
            resolved_corrections=resolved_corrections,
            source_revision=revision,
        )
    def _compute_cgh_changed_artifacts(
        self,revision: int,
    ) -> SectionArtifacts:
        """Rebuild only artifacts affected by a CGH result change."""
        current = self.artifacts
        context = self._build_context(self.state)

        # During the CGHSession callback, resolve() sees the temporarily installed
        # candidate result, or no result when the session is being cleared.
        cgh = self._cgh_session.resolve(self.state.cgh,context)
        if cgh is None:
            cgh = self._identity_field(context)

        # Analytic patterns and aberrations are unaffected by a CGH-only change.
        combined = current.analytic * current.aberrations * cgh
        combined = self._apply_pupil(combined,context)

        phase,eightbit,resolved_corrections = self._phase_to_eightbit(
            combined,context,self.state,
        )

        return SectionArtifacts.from_owned(
            analytic=current.analytic,
            aberrations=current.aberrations,
            cgh=cgh,
            combined=combined,
            phase=phase,
            eightbit=eightbit,
            resolved_corrections=resolved_corrections,
            source_revision=revision,
        )

    def _compute_analytic(
        self,
        context: SectionContext,
        state: SLMSectionState,
    ) -> np.ndarray:

        result = self._identity_field(context)
        group = state.patterns

        if not group.enabled or not group.active:
            return result

        for key,item in group.items.items():
            params = dict(item.params.values)

            if not params.pop("active",False):
                continue

            registration = self.registries.patterns[key]
            component = registration.function(context=context,**params)

            result *= self._validate_phase_field(key,component,context)

        return result

    def _compute_aberrations(
        self,
        context: SectionContext,
        state: SLMSectionState,
    ) -> np.ndarray:
        result = self._identity_field(context)
        group = state.aberrations

        if not group.enabled or not group.active:
            return result

        for key,item in group.items.items():
            registration = self.registries.aberrations[key]

            component = registration.function(
                context=context,**item.params.values)

            result *= self._validate_phase_field(key,component,context)

        return result

    def _apply_pupil(
        self,
        combined: np.ndarray,
        context: SectionContext
        ) -> np.ndarray :
        r = context.pupil_radius_px
        if r <= 0:
            return combined
        h,w=context.shape
        cx = w / 2 + context.center_offset_x_px
        cy = h / 2 + context.center_offset_y_px
        y,x = np.ogrid[:h,:w]
        pupil_mask = ((x-cx)**2 + (y-cy)**2 <= r**2)
        result = np.ones(context.shape,dtype=np.complex128)
        result[pupil_mask] = np.exp(1j*np.angle(combined[pupil_mask]))
        return result

    def _phase_to_eightbit(
        self,
        combined: np.ndarray,
        context: SectionContext,
        state: SLMSectionState,
    ) -> tuple[np.ndarray,np.ndarray,ResolvedCorrections]:
        """Convert a complex section field to phase and 8-bit gray values."""
        combined = self._validate_phase_field("combined",combined,context)
        phase = np.mod(np.angle(combined),2 * np.pi)
        eightbit,resolved = self._phase_to_eightbit_from_phase(
            phase,context,state,
        )
        return phase,eightbit,resolved

    def _phase_to_eightbit_from_phase(
        self,
        phase: np.ndarray,
        context: SectionContext,
        state: SLMSectionState,
    ) -> tuple[np.ndarray,ResolvedCorrections]:
        phase = np.asarray(phase,dtype=np.float64)
        if phase.shape != context.shape:
            raise ValueError(
                f"Phase has shape {phase.shape}; expected {context.shape}"
            )

        resolved = self._resolve_corrections(state,context.geometry)
        corrections = state.corrections
        two_pi_value = (
            resolved.two_pi_value
            if corrections.active and corrections.apply_twopi_value
            else 255
        )
        correction_pattern = (
            resolved.correction_pattern
            if corrections.active and corrections.apply_correction_pattern
            else None
        )

        gray = phase * two_pi_value / (2 * np.pi)
        if correction_pattern is not None:
            correction = (
                np.asarray(correction_pattern,dtype=np.float64)
                * two_pi_value / 255.0
            )
            gray = np.mod(gray + correction,255)

        return np.floor(gray).astype(np.uint8),resolved

    def _resolve_corrections(
        self,state: SLMSectionState,geometry: SectionGeometry,
    ) -> ResolvedCorrections:
        wavelength_nm = int(state.optics.wavelength_nm)
        saved = self._saved_correction_override
        if saved is not None:
            if saved.geometry != geometry or saved.wavelength_nm != wavelength_nm:
                raise CorrectionSourceInvalidatedError(
                    "Saved corrections are pinned to %snm and geometry %s; "
                    "switch to current workspace corrections before changing "
                    "wavelength or section geometry."
                    % (saved.wavelength_nm,saved.geometry)
                )
            return saved
        provider = self.correction_provider
        if provider is None:
            return ResolvedCorrections.defaults(wavelength_nm,geometry)
        resolved = provider.resolve(wavelength_nm,geometry)
        if not isinstance(resolved,ResolvedCorrections):
            raise TypeError("CorrectionProvider.resolve() must return ResolvedCorrections")
        return resolved

    @property
    def uses_saved_corrections(self) -> bool:
        return self._saved_correction_override is not None

    def use_workspace_corrections(self) -> None:
        """Switch this detached/candidate section to the current provider."""
        self._saved_correction_override = None



    @staticmethod
    def _identity_field(context: SectionContext) -> np.ndarray:
        """ Matrix of ones with complex data tuype. """
        return np.ones(context.shape,dtype=np.complex128)

    @staticmethod
    def _validate_phase_field(
        key: str,
        component: np.ndarray,
        context: SectionContext,
    ) -> np.ndarray:
        """Validate and normalize a computed complex phase field.

        The field must be a finite, complex-valued 2D NumPy array whose shape
        matches the current SLM section. A validated field is returned as
        ``np.complex128`` without copying when possible.
        """
        component = np.asarray(component)

        if component.shape != context.shape:
            raise ValueError(
                f"'{key}' returned shape {component.shape}; expected {context.shape}"
            )

        if not np.iscomplexobj(component):
            raise TypeError(
                f"'{key}' must return a complex phase field")

        if not np.all(np.isfinite(component)):
            raise ValueError(
                f"'{key}' contains non-finite values")

        return component.astype(np.complex128,copy=False)


