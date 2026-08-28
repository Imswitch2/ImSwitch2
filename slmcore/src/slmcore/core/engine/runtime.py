from __future__ import annotations

from dataclasses import dataclass
from typing import Any,Mapping

import numpy as np

from ..cgh.targets.lattice import LatticeLockRequest
from ..cgh import (
    CGHJob,
    CGHResult,
    CGHSessionInspection,
    CGHStatus,
    FeedbackInspection,
    FeedbackStatus,
)
from ..cgh.localization import TargetLocalizationReference
from ..config import (
    SLM_CONFIG_SCHEMA_VERSION,
    SLMConfig,
    SLMConfigLoadReport,
    SLMSectionConfig,
    SectionConfigLoadResult,
)
from ..measurement import ImageMeasurement
from .registry import SLMRegistries
from .corrections import CorrectionProvider,ResolvedCorrections
from .section import (
    SectionGeometry,
    SectionPresentation,
    SectionUpdate,
    SLMSectionRuntime,
    SLMSectionSnapshot,
    SLMSectionState,
    SectionArtifacts
)
from .state import GroupTopology,ParamPath
from .transition import SectionStateTransition
from .device import SLMIdentity,SLMGeometry
from ..calibration import SLMSectionCalibration

@dataclass(frozen=True)
class SLMArtifacts:
    eightbit: np.ndarray

    def __post_init__(self) -> None:
        eightbit = np.array(self.eightbit,dtype=np.uint8,copy=True)
        eightbit.setflags(write=False)

        object.__setattr__(self,"eightbit",eightbit)

    @classmethod
    def from_owned(cls,eightbit: np.ndarray) -> "SLMArtifacts":
        frame = np.asarray(eightbit,dtype=np.uint8)
        frame.setflags(write=False)
        instance = object.__new__(cls)
        object.__setattr__(instance,"eightbit",frame)
        return instance

class SLMRuntime:
    """Aggregate orchestrator for independent section runtimes."""

    def __init__(
        self,
        *,
        identity: SLMIdentity,
        geometry: SLMGeometry,
        section_geometries: Mapping[str,SectionGeometry],
        registries: SLMRegistries,
        correction_provider: CorrectionProvider | None=None,
    ) -> None:
        self.identity = identity
        self.geometry = geometry
        self.registries = registries
        self.correction_provider = correction_provider

        self._revision = 0
        self._sections = self._create_sections(section_geometries)

        self._validate_layout()
        self.artifacts = self._compose()

    @classmethod
    def from_config(
        cls,
        config: SLMConfig,
        *,
        registries: SLMRegistries,
        correction_provider: CorrectionProvider | None=None,
        saved_correction_sections=(),
    ) -> "SLMRuntime":
        """Construct directly from config without computing default sections first."""
        if config.schema_version != SLM_CONFIG_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported SLM config schema version {config.schema_version}"
            )

        runtime = object.__new__(cls)
        runtime.identity = config.identity
        runtime.geometry = config.geometry
        runtime.registries = registries
        runtime.correction_provider = correction_provider
        runtime._revision = 1
        runtime._sections = {}
        saved_sections = set(saved_correction_sections or ())
        for key,section in config.sections.items():
            try:
                runtime._sections[key] = SLMSectionRuntime.from_config(
                    section,
                    pixel_size_um=config.geometry.pixel_size_um,
                    registries=registries,
                    correction_provider=correction_provider,
                    correction_source=("saved" if key in saved_sections else "workspace"),
                    revision=1,
                )
            except Exception as error:
                raise RuntimeError(
                    f"Could not construct SLM section '{key}' from config: "
                    f"{error}"
                ) from error
        runtime._validate_layout()
        runtime.artifacts = runtime._compose()
        return runtime

    # ---------------------- #
    #   Public Properties    #
    # ---------------------- #

    @property
    def revision(self) -> int:
        """Monotonic version of the committed aggregate SLM runtime."""
        return self._revision

    @property
    def section_keys(self) -> tuple[str, ...]:
        """Return the section keys in runtime order."""
        return tuple(self._sections)


    # ---------------------- #
    #       Public API       #
    # ---------------------- #

    def load_config(
        self,
        config: SLMConfig,
        *,
        require_identity_match: bool=True,
        calibration_policy: str="config",
        require_complete: bool=False,
        saved_correction_sections=(),
    ) -> SLMConfigLoadReport:
        self._validate_full_config(config,require_identity_match)

        candidates = {}
        prepared_results = {}
        saved_sections = set(saved_correction_sections or ())
        failures = {}
        warnings = []

        for key,section_config in config.sections.items():
            current = self._sections[key]

            try:
                candidate,prepared = current.prepare_config_load(
                    section_config,
                    calibration_policy=calibration_policy,
                    correction_source=(
                        "saved" if key in saved_sections else "workspace"
                    ),
                )
                candidates[key] = candidate
                prepared_results[key] = prepared
                warnings.extend(prepared.warnings)

            except Exception as error:
                failures[key] = error

        if require_complete and failures:
            details = "; ".join(
                "%s: %s" % (key,error)
                for key,error in failures.items()
            )
            raise RuntimeError(
                "Complete SLM config restore failed before commit: %s" % details
            )

        transitions = self._commit_prepared_section_transitions(
            candidates,
            {
                key:prepared.transition
                for key,prepared in prepared_results.items()
            },
        )

        section_results = {
            key:SectionConfigLoadResult(
                transition=transitions[key],
                warnings=prepared.warnings,
                cgh_session_restored=True,
            )
            for key,prepared in prepared_results.items()
        }

        return SLMConfigLoadReport(
            revision=self.revision,
            section_results=section_results,
            failed_sections=failures,
            warnings=tuple(warnings),
        )

    def load_section_config(
        self,
        key: str,
        config: SLMSectionConfig,
        *,
        calibration_policy: str="config",
        correction_source: str="workspace",
    ) -> SectionConfigLoadResult:
        current = self._get_section(key)
        candidate,prepared = current.prepare_config_load(
            config,
            calibration_policy=calibration_policy,
            correction_source=correction_source,
        )

        transitions = self._commit_prepared_section_transitions(
            {key:candidate},{key:prepared.transition},
        )

        return SectionConfigLoadResult(
            transition=transitions[key],
            warnings=prepared.warnings,
            cgh_session_restored=True,
        )

    def apply_section_topology(
        self,key: str,topologies: Mapping[str,GroupTopology],
    ) -> SectionStateTransition | None:
        """Transactionally apply a partial topology mapping to one section."""
        current = self._get_section(key)
        prepared = current.prepare_topology_change(topologies)
        if prepared is None:
            return None

        candidate,transition = prepared
        transitions = self._commit_prepared_section_transitions(
            {key:candidate},{key:transition},
        )
        return transitions[key]

    def create_config(self) -> SLMConfig:
        return SLMConfig(
            schema_version=SLM_CONFIG_SCHEMA_VERSION,
            identity=self.identity,
            geometry=self.geometry,
            sections={
                key:section.create_config()
                for key,section in self._sections.items()
            },
            final_eightbit=self.artifacts.eightbit,
        )

    def get_section_snapshot(self,key: str) -> SLMSectionSnapshot:
        """Return one detached snapshot of a committed section runtime."""
        return self._get_section(key).create_snapshot()

    def get_section_snapshots(self) -> Mapping[str,SLMSectionSnapshot]:
        """Return detached snapshots in authoritative section order."""
        return {
            key:section.create_snapshot()
            for key,section in self._sections.items()
        }

    def get_section_state_copy(self,key: str) -> SLMSectionState:
        """Return a detached copy of one section's authoritative state."""
        return self._get_section(key).state.clone()

    def get_section_calibration_copy(
        self,key: str,
    ) -> SLMSectionCalibration | None:
        """Return a detached copy of one section's calibration."""
        calibration = self._get_section(key).calibration
        return None if calibration is None else calibration.copy()

    def get_section_geometry(self,key: str) -> SectionGeometry:
        """Return the immutable geometry of one section."""
        return self._get_section(key).geometry

    def section_uses_saved_corrections(self,key: str) -> bool:
        return self._get_section(key).uses_saved_corrections

    @property
    def saved_correction_sections(self) -> tuple[str,...]:
        return tuple(
            key for key,section in self._sections.items()
            if section.uses_saved_corrections
        )

    def resolve_workspace_corrections(
        self,key: str,*,state: SLMSectionState | None=None,geometry: SectionGeometry | None=None,
    ) -> ResolvedCorrections:
        section = self._get_section(key)
        state = section.state if state is None else state
        geometry = section.geometry if geometry is None else geometry
        wavelength_nm = int(state.optics.wavelength_nm)
        provider = self.correction_provider
        if provider is None:
            return ResolvedCorrections.defaults(wavelength_nm,geometry)
        return provider.resolve(wavelength_nm,geometry)

    def iter_section_parameters(self,key: str):
        """Iterate over the parameter definitions and values of one section."""
        return self._get_section(key).iter_parameters()

    def get_section_parameter(self,key: str,path: ParamPath) -> Any:
        """Return the current authoritative value of one section parameter."""
        return self._get_section(key).get_parameter(path)

    def get_section_artifacts(self,key: str) -> SectionArtifacts:
        """Return the immutable computed artifacts of one section."""
        return self._get_section(key).artifacts

    def get_section_cgh_status(self,key: str) -> CGHStatus:
        """Return the current CGH status of one section."""
        return self._get_section(key).cgh_status

    def get_section_cgh_result_copy(
        self,key: str,
    ) -> CGHResult | None:
        """Return a detached copy of one section's committed CGH result."""
        result = self._get_section(key).cgh_result
        return None if result is None else result.clone()


    def get_section_feedback_status(self,key: str) -> FeedbackStatus:
        """Return current transient feedback status for one section."""
        return self._get_section(key).feedback_status

    def get_section_feedback_inspection(
        self,key: str,
    ) -> FeedbackInspection:
        """Return immutable acquisition/localization/history data."""
        return self._get_section(key).get_feedback_inspection()

    def get_section_cgh_session_inspection(
        self,key: str,
    ) -> CGHSessionInspection:
        """Return coherent CGH session/round inspection data."""
        return self._get_section(key).get_cgh_session_inspection()

    def set_section_feedback_measurement(
        self,key: str,measurement: ImageMeasurement,
    ) -> None:
        """Attach one generic image measurement to a section feedback session."""
        self._get_section(key).set_feedback_measurement(measurement)

    def update_section_feedback_parameters(
        self,key: str,group: str,changes: Mapping[str,Any],
    ) -> bool:
        return self._get_section(key).update_feedback_parameters(group,changes)

    def get_section_feedback_localization_context(self,key: str):
        return self._get_section(key).get_feedback_localization_context()

    def get_section_base_target_localization_reference(
        self,key: str,
    ) -> TargetLocalizationReference:
        return self._get_section(
            key
        ).get_base_target_localization_reference()

    def compute_section_feedback_intensity_analysis(
        self,key: str,localization=None,
    ):
        return self._get_section(key).compute_feedback_intensity_analysis(
            localization,
        )

    def set_section_feedback_intensity_analysis(
        self,key: str,analysis,
    ) -> None:
        self._get_section(key).set_feedback_intensity_analysis(analysis)

    def compute_section_feedback_measurement_metrics(
        self,key: str,localization=None,
    ):
        return self._get_section(key).compute_feedback_measurement_metrics(
            localization,
        )

    def set_section_feedback_measurement_metrics(self,key: str,metrics) -> None:
        self._get_section(key).set_feedback_measurement_metrics(metrics)


    def compute_section_feedback_localization_candidate(
        self,key: str,parameters: Mapping[str,Any],
    ):
        return self._get_section(key).compute_feedback_localization_candidate(
            parameters,
        )


    def commit_section_feedback_localization(
        self,key: str,localization,parameters: Mapping[str,Any],
    ):
        return self._get_section(key).commit_feedback_localization(
            localization,parameters,
        )


    def reuse_section_feedback_localization(self,key: str):
        return self._get_section(key).reuse_feedback_localization()


    def localize_section_feedback(self,key: str):
        return self._get_section(key).localize_feedback()


    def apply_section_patch(
        self,
        key: str,
        changes: Mapping[ParamPath,Any],
        *,
        lattice_lock_request: LatticeLockRequest | None=None,
        use_workspace_corrections: bool=False,
    ) -> SectionUpdate | None:
        current = self._get_section(key)
        candidate = current.clone()
        if use_workspace_corrections:
            candidate.use_workspace_corrections()

        update = candidate.apply_patch(
            changes,lattice_lock_request=lattice_lock_request,
        )

        # return without committing if no effective change
        if update is None:
            return None

        # otherwise, commit candidate
        replacement={key: candidate}
        self._commit_section_replacements(
            replacement,
            frame_changed_keys=(key,) if update.frame_changed else (),
        )

        return update

    def set_section_calibration(
        self,key: str,calibration: SLMSectionCalibration | None,
    ) -> SectionStateTransition | None:
        """Replace calibration and return a UI-ready section transition."""
        current = self._get_section(key)
        base_revision = current.create_snapshot().revision
        candidate = current.clone()
        update = candidate.set_calibration(calibration)

        if update is None:
            return None

        cgh_pattern_changed = not np.array_equal(
            current.artifacts.cgh,candidate.artifacts.cgh,
        )
        frame_changed = not np.array_equal(
            current.artifacts.eightbit,candidate.artifacts.eightbit,
        )
        self._commit_section_replacements(
            {key:candidate},
            frame_changed_keys=(key,) if frame_changed else (),
        )
        return SectionStateTransition(
            base_revision=base_revision,
            snapshot=self._sections[key].create_snapshot(),
            group_deltas={},
            calibration_changed=True,
            cgh_pattern_changed=cgh_pattern_changed,
            artifacts_recomputed=True,
            frame_changed=frame_changed,
        )

    def set_section_presentation(
        self,key: str,presentation: SectionPresentation,
    ) -> SLMSectionSnapshot | None:
        """Set persisted section presentation without changing numerical state."""
        return self._get_section(key).set_presentation(presentation)

    def restore_section_current_cgh_target(
        self,key: str,
    ) -> SectionUpdate | None:
        """Restore one section's editable target to its committed CGH target."""
        current = self._get_section(key)
        candidate = current.clone()
        update = candidate.restore_current_cgh_target()
        if update is None:
            return None
        self._commit_section_replacements(
            {key:candidate},
            frame_changed_keys=(key,) if update.frame_changed else (),
        )
        return update

    def prepare_section_cgh(self,key: str) -> CGHJob:
        """Compatibility preparation path using session-inferred intent."""
        return self._get_section(key).prepare_cgh()

    def prepare_section_base_cgh(self,key: str) -> CGHJob:
        """Prepare a fresh base CGH transactionally."""
        return self._get_section(key).prepare_base_cgh()

    def prepare_section_adapted_cgh(self,key: str) -> CGHJob:
        """Prepare exactly the pending feedback-adapted round."""
        return self._get_section(key).prepare_adapted_cgh()

    def apply_section_intensity_feedback(
        self,key: str,
    ) -> SectionStateTransition | None:
        return self._apply_feedback_resolution_operation(
            key,"apply_intensity_feedback",
        )

    def reset_section_intensity_feedback(
        self,key: str,
    ) -> SectionStateTransition | None:
        return self._apply_feedback_resolution_operation(
            key,"reset_intensity_feedback",
        )

    def apply_section_position_correction(
        self,key: str,
        *,
        reset_intensity: bool=False,
    ) -> SectionStateTransition | None:
        return self._apply_feedback_resolution_operation(
            key,"apply_position_correction",
            reset_intensity=reset_intensity,
        )

    def set_section_position_correction_active(
        self,key: str,active: bool,*,reset_intensity: bool=False,
    ) -> SectionStateTransition | None:
        return self._apply_feedback_resolution_operation(
            key,"set_position_correction_active",bool(active),
            reset_intensity=reset_intensity,
        )

    def clear_section_position_correction(
        self,key: str,*,reset_intensity: bool=False,
    ) -> SectionStateTransition | None:
        return self._apply_feedback_resolution_operation(
            key,"clear_position_correction",reset_intensity=reset_intensity,
        )

    def create_section_target_preview(self,key: str) -> np.ndarray:
        """Return one section target preview without changing runtime state."""
        return self._get_section(key).create_target_preview()

    def commit_section_cgh(
        self,key: str,result: CGHResult,
    ) -> SectionStateTransition | None:
        """Commit a CGH result and return its UI-ready section transition.

        A rejected or superseded result returns ``None`` and leaves both the
        section and aggregate runtimes unchanged. The returned transition
        advances the retained section revision even when the effective SLM
        frame is unchanged, so hosts can synchronize CGH status safely.
        """
        current = self._get_section(key)
        base_revision = current.create_snapshot().revision
        candidate = current.clone()

        if not candidate.commit_cgh(result):
            return None

        cgh_pattern_changed = not np.array_equal(
            current.artifacts.cgh,candidate.artifacts.cgh,
        )
        frame_changed = not np.array_equal(
            current.artifacts.eightbit,candidate.artifacts.eightbit,
        )

        self._commit_section_replacements(
            {key:candidate},
            frame_changed_keys=(key,) if frame_changed else (),
        )
        return SectionStateTransition(
            base_revision=base_revision,
            snapshot=self._sections[key].create_snapshot(),
            group_deltas={},
            calibration_changed=False,
            cgh_pattern_changed=cgh_pattern_changed,
            artifacts_recomputed=True,
            frame_changed=frame_changed,
        )

    def mark_section_cgh_compute_failed(
        self,key: str,generation: int,message: Any,
    ) -> bool:
        return self._get_section(key).mark_cgh_compute_failed(generation,message)

    def invalidate_section_cgh_compute(self,key: str) -> bool:
        return self._get_section(key).invalidate_cgh_compute()

    def discard_section_cgh_working_round(self,key: str) -> bool:
        return self._get_section(key).discard_cgh_working_round()

    def reset_section_cgh_to_round(
        self,key: str,index: int,
    ) -> SectionStateTransition | None:
        current = self._get_section(key)
        base_revision = current.create_snapshot().revision
        candidate = current.clone()

        if not candidate.reset_cgh_to_round(index):
            return None

        cgh_pattern_changed = not np.array_equal(
            current.artifacts.cgh,candidate.artifacts.cgh,
        )
        frame_changed = not np.array_equal(
            current.artifacts.eightbit,candidate.artifacts.eightbit,
        )

        self._commit_section_replacements(
            {key:candidate},
            frame_changed_keys=(key,) if frame_changed else (),
        )
        return SectionStateTransition(
            base_revision=base_revision,
            snapshot=self._sections[key].create_snapshot(),
            group_deltas={},
            calibration_changed=False,
            cgh_pattern_changed=cgh_pattern_changed,
            artifacts_recomputed=True,
            frame_changed=frame_changed,
        )

    def clear_section_cgh_session(
        self,key: str,
    ) -> SectionStateTransition | None:
        """Clear a CGH session and return its UI-ready section transition."""
        current = self._get_section(key)
        base_revision = current.create_snapshot().revision
        candidate = current.clone()

        if not candidate.clear_cgh_session():
            return None

        cgh_pattern_changed = not np.array_equal(
            current.artifacts.cgh,candidate.artifacts.cgh,
        )
        frame_changed = not np.array_equal(
            current.artifacts.eightbit,candidate.artifacts.eightbit,
        )

        self._commit_section_replacements(
            {key:candidate},
            frame_changed_keys=(key,) if frame_changed else (),
        )
        return SectionStateTransition(
            base_revision=base_revision,
            snapshot=self._sections[key].create_snapshot(),
            group_deltas={},
            calibration_changed=False,
            cgh_pattern_changed=cgh_pattern_changed,
            artifacts_recomputed=True,
            frame_changed=frame_changed,
        )


    def _apply_feedback_resolution_operation(
        self,key: str,method_name: str,*args: Any,**kwargs: Any,
    ) -> SectionStateTransition | None:
        """Commit one transient feedback operation with section revision semantics."""
        current = self._get_section(key)
        base_revision = current.create_snapshot().revision
        candidate = current.clone()
        effective_changed = bool(getattr(candidate,method_name)(*args,**kwargs))

        if not effective_changed:
            # Session-only changes (history, zero correction, clearing an inactive
            # correction) do not alter aggregate/config revision semantics.
            self._sections[key] = candidate
            return None

        self._commit_section_replacements(
            {key:candidate},frame_changed_keys=(),
        )
        return SectionStateTransition(
            base_revision=base_revision,
            snapshot=self._sections[key].create_snapshot(),
            group_deltas={},
            calibration_changed=False,
            cgh_pattern_changed=False,
            artifacts_recomputed=False,
            frame_changed=False,
        )

    # -------------------------- #
    #      Internal Methods      #
    # -------------------------- #

    def _create_sections(
        self,
        section_geometries: Mapping[str,SectionGeometry],
    ) -> dict[str, SLMSectionRuntime]:
        """Construct and initialize the section runtimes owned by this SLM."""
        sections = {}

        for key,section_geometry in section_geometries.items():
            state = SLMSectionState.create(self.registries)
            section = SLMSectionRuntime(
                geometry=section_geometry,
                pixel_size_um=self.geometry.pixel_size_um,
                state=state,
                registries=self.registries,
                correction_provider=self.correction_provider,
            )
            sections[key] = section

        return sections

    def _get_section(self,key: str) -> SLMSectionRuntime:
        try:
            return self._sections[key]
        except KeyError as error:
            raise KeyError(f"Unknown SLM section '{key}'") from error

    def _compose(
        self,replacements: Mapping[str, SLMSectionRuntime] | None=None,
        frame_changed_keys: tuple[str, ...] | None=None,
    ) -> SLMArtifacts:
        """Compose full-SLM artifacts from current sections and optional replacements.

        Replacement runtimes are used transiently for candidate validation without
        mutating the committed section mapping."""

        replacements = {} if replacements is None else replacements

        if frame_changed_keys is not None and hasattr(self,"artifacts"):
            frame = np.array(self.artifacts.eightbit,copy=True)
            section_items = (
                (key,replacements[key])
                for key in frame_changed_keys
            )
        else:
            frame = np.zeros(self.geometry.shape,dtype=np.uint8)
            section_items = (
                (key,replacements.get(key,current))
                for key,current in self._sections.items()
            )

        for key,section in section_items:

            artifacts = section.artifacts

            # Ensure artifact's frame matches section shape
            if artifacts.eightbit.shape != section.geometry.shape:
                raise RuntimeError(
                    f"Geometry mismatch for section '{key}': "
                    f"frame has shape {artifacts.eightbit.shape}, "
                    f"expected {section.geometry.shape}."
                )

            frame[section.geometry.slices] = artifacts.eightbit

        return SLMArtifacts.from_owned(frame)

    def _validate_layout(self) -> None:
        if not self._sections:
            raise ValueError(
                "SLM runtime must contain at least one section"
            )

        occupied = np.zeros(self.geometry.shape,dtype=bool)

        for key,section in self._sections.items():
            geometry = section.geometry

            if key != geometry.key:
                raise ValueError(
                    f"Section mapping key '{key}' does not match "
                    f"geometry key '{geometry.key}'"
                )

            if (
                geometry.x + geometry.width > self.geometry.width
                or geometry.y + geometry.height > self.geometry.height
            ):
                raise ValueError(
                    f"Section '{key}' exceeds the SLM geometry"
                )

            if np.any(occupied[geometry.slices]):
                raise ValueError(
                    f"Section '{key}' overlaps another section"
                )

            occupied[geometry.slices] = True

    def _validate_full_config(
        self,config: SLMConfig,require_identity_match: bool,
    ) -> None:
        if config.schema_version != SLM_CONFIG_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported SLM config schema version "
                f"{config.schema_version}"
            )

        if require_identity_match and config.identity != self.identity:
            raise ValueError(
                "SLM config identity does not match the runtime"
            )

        if config.geometry != self.geometry:
            raise ValueError(
                "SLM config geometry does not match the runtime"
            )

        if set(config.sections) != set(self._sections):
            raise ValueError(
                "SLM config section keys do not match the runtime"
            )

        for key,section_config in config.sections.items():
            if section_config.geometry != self._sections[key].geometry:
                raise ValueError(
                    f"SLM config geometry for section '{key}' does not match"
                )

    def _commit_prepared_section_transitions(
        self,
        replacements: Mapping[str,SLMSectionRuntime],
        prepared: Mapping[str,Any],
    ) -> Mapping[str,SectionStateTransition]:
        """Commit prepared section candidates behind one aggregate compose barrier."""
        if set(replacements) != set(prepared):
            raise ValueError(
                "Prepared transition keys must match replacement section keys"
            )

        self._commit_section_replacements(
            replacements,
            frame_changed_keys=tuple(
                key for key,transition in prepared.items()
                if transition.frame_changed
            ),
        )

        # Bind snapshots only after aggregate composition and replacement commit
        # have both succeeded.
        return {
            key:self._sections[key]._finalize_transition(prepared[key])
            for key in replacements
        }


    def _commit_section_replacements(
        self,
        replacements: Mapping[str,SLMSectionRuntime],
        *,
        frame_changed_keys: tuple[str, ...] | None=None,
    )-> SLMArtifacts:
        if not replacements:
            return self.artifacts

        if frame_changed_keys is None:
            frame_changed_keys = tuple(replacements)

        # Compose at most once. Session/state-only replacements preserve the
        # existing aggregate frame object.
        artifacts = (
            self._compose(
                replacements,frame_changed_keys=frame_changed_keys,
            )
            if frame_changed_keys else self.artifacts
        )

        # if it succeeded (ie did not raise), we mutate current runtime
        self._sections.update(replacements)
        self.artifacts=artifacts
        self._revision+=1

        return artifacts
