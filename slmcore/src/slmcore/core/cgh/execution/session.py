from __future__ import annotations

from typing import Any,Callable,Mapping,TypeVar

import numpy as np

from ...measurement import ImageMeasurement
from ...engine.registry import SLMRegistries,TargetRegistration
from ...engine.section.components import CGHState
from ...engine.section.context import SectionContext
from ...engine.state.items import CGHTargetState
from ..feedback import (
    FeedbackCapability,
    FeedbackChangeKind,
    FeedbackInspection,
    FeedbackMeasurement,
    FeedbackStatus,
    IntensityAdaptation,
    PositionCorrection,
    RoundEvaluation,
)
from ..feedback.analysis import analyze_position
from ..feedback.parameters import POSITION_CORRECTION_PARAMS
from ..localization import (
    LOCALIZATION_PARAMS,
    LocalizationResult,
    localization_context,
    localize_measurement,
    reuse_localization,
)
from ..measurement_metrics import (
    INTENSITY_ANALYSIS_PARAMS,
    IntensityAnalysis,
    MeasurementMetrics,
    analyze_measurement_intensity,
)
from ..targets.base import Target
from ..targets.resolution import TargetResolution
from .errors import InvalidCGHResultError
from .job import CGHJob
from .result import CGHResult
from .session_model import (
    AppliedCGH,
    CGHPreparedPurpose,
    CGHPreparedRequest,
    CGHRound,
    CGHRoundInspection,
    CGHSessionInspection,
    CGHSessionSnapshot,
    CGHTargetDisplay,
    CGHWorkingRound,
    CGHWorkingRoundState,
    TargetDefinitionState,
)
from ..signature import (
    CGHSignature,
    compute_context_signature,
    compute_feedback_target_signature,
    compute_position_correction_signature,
)
from .spec import CGHSpec
from .status import CGHResultState,CGHStatus

T = TypeVar("T")


class CGHSession:
    """Authoritative CGH target, round, feedback and compute lifecycle."""

    def __init__(self,registries: SLMRegistries):
        self.registries = registries
        self._generation = 0
        self._committed_target: TargetDefinitionState | None = None
        self._target: Target | None = None
        self._prepared_definition: Any | None = None
        self._prepared_definition_signature: CGHSignature | None = None
        self._rounds: tuple[CGHRound, ...] = ()
        self._working_round: CGHWorkingRound | None = None
        self._applied: AppliedCGH | None = None
        self._prepared_request: CGHPreparedRequest | None = None
        self._localization_params = _default_values(LOCALIZATION_PARAMS)
        self._intensity_params = _default_values(INTENSITY_ANALYSIS_PARAMS)
        self._position_params = _default_values(POSITION_CORRECTION_PARAMS)
        self._localization_reference = None
        self._position_correction: PositionCorrection | None = None
        self._position_reference_round: CGHRound | None = None
        self._position_active = False
        self._prefer_previous_initialization = False
        self._requires_fresh_initialization = False

    @property
    def result(self) -> CGHResult | None:
        return None if self._applied is None else self._applied.result

    @property
    def target(self) -> Target | None:
        return self._target.clone() if self._target is not None else None

    @property
    def session_snapshot(self) -> CGHSessionSnapshot | None:
        if (
            self._committed_target is None
            and not self._rounds
            and self._position_correction is None
        ):
            return None
        return CGHSessionSnapshot(
            committed_target=self._committed_target,
            position_correction=self._position_correction,
            position_active=self._position_active,
            position_reference_round=self._position_reference_round,
            rounds=self._rounds,
            intensity_analysis_params=self._intensity_params,
        )

    def clone(self) -> "CGHSession":
        candidate = type(self)(self.registries)
        candidate._generation = self._generation
        candidate._committed_target = self._committed_target
        candidate._target = self._target.clone() if self._target is not None else None
        candidate._prepared_definition = self._prepared_definition
        candidate._prepared_definition_signature = self._prepared_definition_signature
        candidate._rounds = self._rounds
        candidate._working_round = self._working_round
        candidate._applied = self._applied
        candidate._prepared_request = self._prepared_request
        candidate._localization_params = dict(self._localization_params)
        candidate._intensity_params = dict(self._intensity_params)
        candidate._position_params = dict(self._position_params)
        candidate._localization_reference = self._localization_reference
        candidate._position_correction = self._position_correction
        candidate._position_reference_round = self._position_reference_round
        candidate._position_active = self._position_active
        candidate._prefer_previous_initialization = self._prefer_previous_initialization
        candidate._requires_fresh_initialization = self._requires_fresh_initialization
        return candidate

    def remember_prepared_definition(
        self,
        state: CGHState,
        context: SectionContext,
        prepared_definition: Any | None,
    ) -> None:
        if prepared_definition is None or state.selected_target is None:
            return
        target_state = state.items[state.selected_target]
        target_class = self.registries.targets[state.selected_target].target_class
        self._prepared_definition = prepared_definition
        self._prepared_definition_signature = target_class.definition_signature_for(
            context,target_state.params.values,
        )

    def reconcile_target_definition(
        self,state: CGHState,context: SectionContext,
    ) -> None:
        """Discard transient work that no longer belongs to the requested target."""
        try:
            target_state = self._resolve_requested_target_state(state,context)
        except Exception:
            self.discard_working_round()
            return
        self._discard_irrelevant_working_round(target_state)
        request = self._prepared_request
        if request is not None and not self._request_matches_current_inputs(
            request,state,context,
        ):
            self.invalidate_prepared_compute()

    def prepare(self,state: CGHState,context: SectionContext) -> CGHJob:
        """Compatibility preparation path using the session's inferred destination."""
        target_state = self._resolve_requested_target_state(state,context)
        self._discard_irrelevant_working_round(target_state)
        target = self._build_target(target_state,context)
        purpose,round_index,intensities,adaptation = self._prepare_destination(
            target_state,target.resolution,state,
        )
        return self._prepare_job(
            state,context,target_state,target,purpose,round_index,
            intensities,adaptation,
        )

    def prepare_base(self,state: CGHState,context: SectionContext) -> CGHJob:
        """Prepare a fresh base CGH that replaces the session only on success."""
        target_state = self._resolve_requested_target_state(state,context)
        target = self._build_target(target_state,context)
        intensities = np.asarray(
            target.resolution.spot_intensities,dtype=np.float64,
        )
        return self._prepare_job(
            state,context,target_state,target,
            CGHPreparedPurpose.TARGET_REPLACEMENT,0,intensities,None,
        )

    def prepare_adapted(self,state: CGHState,context: SectionContext) -> CGHJob:
        """Prepare exactly the pending feedback-created working round."""
        target_state = self._resolve_requested_target_state(state,context)
        committed = self._committed_target
        if committed is None or target_state.target_signature != committed.target_signature:
            raise RuntimeError(
                "Pending feedback adaptation no longer matches the selected target"
            )
        working = self._working_round_for_target(target_state)
        if (
            working is None
            or working.purpose is not CGHPreparedPurpose.WORKING_ROUND
            or working.feedback_change is None
        ):
            raise RuntimeError(
                "No feedback adaptation or position change is waiting to be computed"
            )
        if working.state is CGHWorkingRoundState.COMPUTING:
            raise RuntimeError("Feedback change is already being computed")
        target = self._build_target(target_state,context)
        return self._prepare_job(
            state,context,target_state,target,working.purpose,working.index,
            working.intensities,working.adaptation,
        )

    def _prepare_job(
        self,
        state: CGHState,
        context: SectionContext,
        target_state: TargetDefinitionState,
        target: Target,
        purpose: CGHPreparedPurpose,
        round_index: int,
        intensities: np.ndarray,
        adaptation: IntensityAdaptation | None,
    ) -> CGHJob:
        apply_position = purpose is not CGHPreparedPurpose.TARGET_REPLACEMENT
        effective_signature = self._effective_signature(
            target_state,round_index,intensities,
            include_position=apply_position,
        )
        resolution = self._effective_resolution(
            target,intensities,include_position=apply_position,
        )
        spec = self._build_spec(state,context,target_state,effective_signature)

        self._generation += 1
        request = CGHPreparedRequest(
            generation=self._generation,
            purpose=purpose,
            round_index=round_index,
            feedback_target_signature=effective_signature,
            spec_signature=spec.signature,
            target_state=(
                target_state if purpose is CGHPreparedPurpose.TARGET_REPLACEMENT
                else None
            ),
        )
        self._prepared_request = request

        if purpose in (
            CGHPreparedPurpose.TARGET_REPLACEMENT,
            CGHPreparedPurpose.WORKING_ROUND,
        ):
            existing = self._working_round
            if (
                existing is not None
                and existing.purpose is purpose
                and existing.index == round_index
                and existing.feedback_target_signature == effective_signature
            ):
                self._working_round = existing.with_state(
                    CGHWorkingRoundState.COMPUTING,
                    generation=self._generation,
                )
            else:
                self._working_round = CGHWorkingRound(
                    index=round_index,
                    intensities=intensities,
                    feedback_target_signature=effective_signature,
                    adaptation=adaptation,
                    target_state=(
                        target_state
                        if purpose is CGHPreparedPurpose.TARGET_REPLACEMENT
                        else None
                    ),
                    purpose=purpose,
                    state=CGHWorkingRoundState.COMPUTING,
                    generation=self._generation,
                )

        compute_func = self.registries.algorithms[spec.algorithm].function
        return CGHJob(
            generation=self._generation,
            spec=spec,
            target_name=str(target.name or spec.target_type),
            resolution=resolution,
            compute_func=compute_func,
            initial_field=self._initial_field_for(spec,context),
            prepared_request=request,
        )

    def create_target_resolution(
        self,state: CGHState,context: SectionContext,
    ) -> TargetResolution:
        target_state = self._resolve_requested_target_state(state,context)
        target = self._build_target(target_state,context)
        purpose,round_index,intensities,_adaptation = self._prepare_destination(
            target_state,target.resolution,state,
        )
        apply_position = purpose is not CGHPreparedPurpose.TARGET_REPLACEMENT
        return self._effective_resolution(
            target,intensities,
            include_position=apply_position,
        )

    def resolve(
        self,state: CGHState,context: SectionContext,
    ) -> np.ndarray | None:
        if not state.enabled or not state.active or state.selected_target is None:
            return None
        applied = self._applied
        if applied is None:
            return None
        if applied.result.pattern.shape != context.shape:
            return None
        return applied.result.pattern

    def commit(
        self,
        result: CGHResult,
        state: CGHState,
        context: SectionContext,
        build_candidate: Callable[[],T],
    ) -> T | None:
        request = self._prepared_request
        if request is None:
            return None
        if result.generation != self._generation:
            return None
        if result.generation != request.generation:
            return None
        if result.spec.signature != request.spec_signature:
            return None
        if result.spec.feedback_target_signature != request.feedback_target_signature:
            return None
        if not self._request_matches_current_inputs(request,state,context):
            return None

        return self._commit_result_transactionally(
            result,request,build_candidate,
        )

    def mark_compute_failed(
        self,generation: int | None,message: Any,
    ) -> bool:
        request = self._prepared_request
        if request is None:
            return False
        if generation is not None and int(generation) != request.generation:
            return False
        self._prepared_request = None
        if (
            request.purpose
            in (CGHPreparedPurpose.WORKING_ROUND,CGHPreparedPurpose.TARGET_REPLACEMENT)
            and self._working_round is not None
            and self._working_round.index == request.round_index
        ):
            self._working_round = self._working_round.with_state(
                CGHWorkingRoundState.FAILED,
                failure_message=message,
            )
        return True

    def invalidate_prepared_compute(self) -> bool:
        changed = self._prepared_request is not None
        if changed:
            self._generation += 1
            self._prepared_request = None
        if (
            self._working_round is not None
            and self._working_round.state is CGHWorkingRoundState.COMPUTING
        ):
            self._working_round = self._working_round.with_state(
                CGHWorkingRoundState.NOT_COMPUTED,
            )
            changed = True
        return changed

    def discard_working_round(self) -> bool:
        changed = self.invalidate_prepared_compute()
        if self._working_round is not None:
            self._working_round = None
            changed = True
        return changed

    def status(self,state: CGHState,context: SectionContext) -> CGHStatus:
        target_type = state.selected_target
        target_state = None
        desired_signature = None
        unavailable_reason = None
        try:
            target_state = self._resolve_requested_target_state(state,context)
            target = self._build_target(target_state,context)
            purpose,round_index,intensities,_adaptation = self._prepare_destination(
                target_state,target.resolution,state,
            )
            apply_position = purpose is not CGHPreparedPurpose.TARGET_REPLACEMENT
            desired_signature = self._effective_signature(
                target_state,round_index,intensities,
                include_position=apply_position,
            )
        except Exception as error:
            unavailable_reason = str(error)
            round_index = None

        working = self._working_round_for_target(target_state)
        implicit_working = (
            target_state is not None
            and working is None
            and not self._rounds
            and unavailable_reason is None
            and round_index is not None
        )

        applied = self._applied
        if applied is None:
            result_state = CGHResultState.MISSING
        elif desired_signature is not None and self._applied_matches(
            applied,desired_signature,state,context,
        ):
            result_state = CGHResultState.CURRENT
        else:
            result_state = CGHResultState.STALE

        return CGHStatus(
            enabled=state.enabled,
            active=state.active,
            target_type=target_type,
            result_state=result_state,
            result_generation=(
                None if applied is None else applied.result.generation
            ),
            target_name=None if applied is None else applied.result.target_name,
            committed_target_type=(
                None if self._committed_target is None
                else self._committed_target.target_type
            ),
            current_round_index=self._current_round_index,
            applied_round_index=(
                None if applied is None else applied.origin_round_index
            ),
            working_round_index=(
                round_index if implicit_working else (
                    None if working is None else working.index
                )
            ),
            working_round_state=(
                CGHWorkingRoundState.NOT_COMPUTED if implicit_working else (
                    None if working is None else working.state
                )
            ),
            intensity_count=self._committed_intensity_count,
            position_active=self._position_active,
            draft_target_changed=(
                target_state is not None
                and self._committed_target is not None
                and target_state.target_signature
                != self._committed_target.target_signature
            ),
            target_restore_available=(
                target_state is not None
                and self._committed_target is not None
                and target_state.context_signature
                == self._committed_target.context_signature
                and target_state.target_signature
                != self._committed_target.target_signature
            ),
            unavailable_reason=unavailable_reason,
        )

    def feedback_status(
        self,state: CGHState,context: SectionContext,
    ) -> FeedbackStatus:
        capabilities = self._feedback_capabilities(state)
        measurement = self._current_measurement
        localization = (
            None if measurement is None else measurement.localization
        )
        localized = localization is not None
        diagnostics = (
            {} if localization is None
            else dict(localization.diagnostics or {})
        )
        total = (
            0 if localization is None
            else int(localization.lattice_indices.shape[1])
        )
        matched = int(diagnostics.get(
            "matched_count",total if localization is not None else 0,
        ))
        missing = int(diagnostics.get(
            "missing_count",max(0,total-matched),
        ))
        unmatched = int(diagnostics.get("unmatched_detection_count",0) or 0)
        rms = diagnostics.get("rms_residual_px")
        intensity_analysis = self._current_intensity_analysis
        metrics = (
            {} if intensity_analysis is None
            else dict(intensity_analysis.values)
        )
        working_visible = self._working_matches_state(state,context)
        working = self._working_round if working_visible else None
        pending_feedback_change = (
            None if working is None
            or working.purpose is not CGHPreparedPurpose.WORKING_ROUND
            else working.feedback_change
        )
        adaptation_pending = bool(
            working is not None
            and working.purpose is CGHPreparedPurpose.WORKING_ROUND
            and working.feedback_change is FeedbackChangeKind.INTENSITY
        )
        return FeedbackStatus(
            capabilities=capabilities,
            acquisition_available=measurement is not None,
            localization_available=localized,
            intensity_count=self._committed_intensity_count,
            position_available=self._position_correction is not None,
            position_active=self._position_active,
            inspection_available=(
                measurement is not None
                or bool(self._rounds)
                or working_visible
                or self._position_correction is not None
            ),
            localization_params=self._localization_params,
            intensity_params=self._intensity_params,
            position_params=self._position_params,
            measurement_metrics=metrics,
            adaptation_pending=adaptation_pending,
            feedback_compute_pending=pending_feedback_change is not None,
            pending_feedback_change=pending_feedback_change,
            previous_localization_available=(
                self._localization_reference is not None
            ),
            localization_matched_count=matched,
            localization_total_count=total,
            localization_missing_count=missing,
            localization_unmatched_detection_count=unmatched,
            localization_rms_residual_px=(
                None if rms is None else float(rms)
            ),
            localization_reused_previous=bool(
                localization is not None
                and (
                    localization.reused_previous
                    or diagnostics.get("reused_exact",False)
                )
            ),
        )

    def feedback_inspection(self) -> FeedbackInspection:
        return FeedbackInspection(
            measurement=self._current_measurement,
            intensity_rounds=tuple(
                round_record.evaluation
                for round_record in self._rounds
                if round_record.evaluation is not None
                and round_record.evaluation.intensity_analysis is not None
            ),
            position_correction=self._position_correction,
            position_active=self._position_active,
        )

    def session_inspection(
        self,
        state: CGHState | None=None,
        context: SectionContext | None=None,
    ) -> CGHSessionInspection:
        working = self._working_round
        if state is not None and context is not None:
            try:
                target_state = self._resolve_requested_target_state(state,context)
                working = self._working_round_for_target(target_state)
                if working is None and not self._rounds:
                    working = self._implicit_round_zero_working(
                        target_state,context,state,
                    )
            except Exception:
                working = None
        return CGHSessionInspection(
            committed_target=self._committed_target,
            rounds=tuple(
                self._round_inspection(round_record,context)
                for round_record in self._rounds
            ),
            working_round=(
                None if working is None
                else self._working_round_inspection(working,context)
            ),
            applied_round_index=(
                None if self._applied is None
                else self._applied.origin_round_index
            ),
            position_correction=self._position_correction,
            position_active=self._position_active,
            position_reference_round=(
                None if self._position_reference_round is None
                else self._round_inspection(
                    self._position_reference_round,context,
                    include_position=False,
                )
            ),
            measurement=self._current_measurement,
        )

    def set_feedback_measurement(
        self,
        state: CGHState,
        context: SectionContext,
        measurement: ImageMeasurement,
    ) -> None:
        self._require_current_round_for_feedback(state,context)
        self._discard_pending_feedback_adaptation()
        if not isinstance(measurement,ImageMeasurement):
            raise TypeError("measurement must be an ImageMeasurement")
        self._set_current_round_evaluation(
            RoundEvaluation(
                index=self._current_round_index,
                measurement=FeedbackMeasurement(acquisition=measurement),
            )
        )

    def update_feedback_parameters(
        self,group: str,changes: Mapping[str,Any],
    ) -> bool:
        group = str(group)
        if group == "localization":
            values = self._localization_params
            specs = LOCALIZATION_PARAMS
        elif group in ("intensity","intensity_analysis"):
            group = "intensity_analysis"
            values = self._intensity_params
            specs = INTENSITY_ANALYSIS_PARAMS
            if self._committed_intensity_count > 0:
                raise RuntimeError(
                    "Intensity analysis parameters are locked after Round 1 "
                    "has been computed. Reset intensity feedback to Round 0 "
                    "before changing them."
                )
        elif group == "position":
            values = self._position_params
            specs = POSITION_CORRECTION_PARAMS
        else:
            raise KeyError(f"Unknown feedback parameter group '{group}'")

        normalized = dict(values)
        changed = False
        for key,value in dict(changes).items():
            if key not in specs:
                raise KeyError(f"Unknown {group} parameter '{key}'")
            converted = specs[key].validate(value)
            if normalized[key] != converted:
                normalized[key] = converted
                changed = True
        if not changed:
            return False

        if group in ("localization","intensity_analysis"):
            self._discard_pending_feedback_adaptation()

        if group == "localization":
            self._localization_params = normalized
            measurement = self._current_measurement
            if measurement is not None:
                self._set_current_round_evaluation(
                    RoundEvaluation(
                        index=self._current_round_index,
                        measurement=FeedbackMeasurement(
                            acquisition=measurement.acquisition,
                        ),
                    )
                )
        elif group == "intensity_analysis":
            self._intensity_params = normalized
            measurement = self._current_measurement
            if measurement is not None:
                self._set_current_round_evaluation(
                    RoundEvaluation(
                        index=self._current_round_index,
                        measurement=measurement,
                        intensity_analysis=None,
                    )
                )
        else:
            self._position_params = normalized
        return True

    def compute_feedback_intensity_analysis(
        self,state: CGHState,context: SectionContext,localization=None,
    ) -> IntensityAnalysis:
        """Compute experimental spot powers/metrics from one localization."""
        self._require_any_feedback_capability(state)
        resolution = self._current_round_resolution(state,context)
        measurement = self._require_current_measurement()
        result = localization or measurement.localization
        if result is None:
            raise RuntimeError(
                "Localize the measurement before calculating intensity metrics"
            )
        return analyze_measurement_intensity(
            measurement.acquisition,
            result,
            resolution,
            self._intensity_params,
        )

    def set_feedback_intensity_analysis(
        self,analysis: IntensityAnalysis,
    ) -> None:
        """Store the authoritative experimental intensity analysis for this round."""
        if not isinstance(analysis,IntensityAnalysis):
            raise TypeError("analysis must be an IntensityAnalysis")
        measurement = self._require_current_measurement()
        if measurement.localization is None:
            raise RuntimeError(
                "Accepted localization is required before storing intensity analysis"
            )
        if dict(analysis.parameters) != dict(self._intensity_params):
            raise RuntimeError(
                "Intensity analysis parameters no longer match the session settings"
            )
        self._set_current_round_evaluation(
            RoundEvaluation(
                index=self._current_round_index,
                measurement=FeedbackMeasurement(
                    acquisition=measurement.acquisition,
                    localization=measurement.localization,
                ),
                intensity_analysis=analysis,
            )
        )

    def compute_feedback_measurement_metrics(
        self,state: CGHState,context: SectionContext,localization=None,
    ) -> MeasurementMetrics:
        """Compatibility view over the centralized experimental analysis."""
        return MeasurementMetrics.from_analysis(
            self.compute_feedback_intensity_analysis(
                state,context,localization,
            )
        )

    def set_feedback_measurement_metrics(self,metrics) -> None:
        """Compatibility setter for legacy callers; metrics are no longer authoritative."""
        if metrics is None:
            return
        measurement = self._require_current_measurement()
        if measurement.localization is None:
            raise RuntimeError(
                "Accepted localization is required before storing metrics"
            )
        # Preserve legacy payloads without creating a second scientific analysis.
        current_analysis = self._current_intensity_analysis
        self._set_current_round_evaluation(
            RoundEvaluation(
                index=self._current_round_index,
                measurement=FeedbackMeasurement(
                    acquisition=measurement.acquisition,
                    localization=measurement.localization,
                    metrics=metrics,
                ),
                intensity_analysis=current_analysis,
            )
        )

    def feedback_localization_context(
        self,state: CGHState,context: SectionContext,
    ):
        self._require_any_feedback_capability(state)
        committed = self._committed_target
        if committed is None:
            raise RuntimeError("No committed target is available")
        target = self._build_target(committed,context)
        return localization_context(
            target_type=target.target_type,
            target_params=target.params,
            resolution=target.resolution,
            calibration=context.calibration,
        )

    def compute_feedback_localization_candidate(
        self,
        state: CGHState,
        context: SectionContext,
        parameters: Mapping[str,Any],
    ):
        self._require_any_feedback_capability(state)
        target = self._current_round_target(state,context)
        measurement = self._require_current_measurement()
        normalized = _validated_values(LOCALIZATION_PARAMS,parameters)
        return localize_measurement(
            measurement.acquisition,
            target_type=target.target_type,
            target_params=target.params,
            resolution=target.resolution,
            parameters=normalized,
            calibration=context.calibration,
        )

    def commit_feedback_localization(
        self,
        state: CGHState,
        context: SectionContext,
        localization,
        parameters: Mapping[str,Any],
    ):
        target = self._current_round_target(state,context)
        self._require_any_feedback_capability(state)
        measurement = self._require_current_measurement()
        self._discard_pending_feedback_adaptation()

        if not isinstance(localization,LocalizationResult):
            raise TypeError("localization must be a LocalizationResult")
        if localization.target_type != target.target_type:
            raise RuntimeError(
                "Localization candidate no longer matches the selected target"
            )
        if not np.array_equal(
            localization.lattice_indices,
            target.resolution.lattice_indices,
        ):
            raise RuntimeError(
                "Localization candidate no longer matches the target lattice"
            )

        normalized = _validated_values(LOCALIZATION_PARAMS,parameters)
        if dict(localization.parameters) != normalized:
            raise ValueError(
                "Accepted localization parameters do not match candidate parameters"
            )
        diagnostics = dict(localization.diagnostics or {})
        candidate_measurement_id = diagnostics.get("measurement_id")
        current_measurement_id = measurement.acquisition.measurement_id
        if (
            candidate_measurement_id is not None
            and str(candidate_measurement_id) != str(current_measurement_id)
        ):
            raise RuntimeError(
                "Localization candidate belongs to a different measurement"
            )

        self._localization_params = normalized
        self._set_current_round_evaluation(
            RoundEvaluation(
                index=self._current_round_index,
                measurement=FeedbackMeasurement(
                    acquisition=measurement.acquisition,
                    localization=localization,
                ),
                intensity_analysis=None,
            )
        )
        self._localization_reference = localization
        return localization

    def reuse_feedback_localization(
        self,state: CGHState,context: SectionContext,
    ):
        target = self._current_round_target(state,context)
        self._require_any_feedback_capability(state)
        measurement = self._require_current_measurement()
        self._discard_pending_feedback_adaptation()
        if self._localization_reference is None:
            raise RuntimeError("No previous localization is available to reuse")

        localization = reuse_localization(
            measurement.acquisition,
            self._localization_reference,
        )
        if localization.target_type != target.target_type:
            raise RuntimeError(
                "Previous localization does not match the selected target"
            )
        if not np.array_equal(
            localization.lattice_indices,
            target.resolution.lattice_indices,
        ):
            raise RuntimeError(
                "Previous localization does not match the target lattice"
            )
        self._set_current_round_evaluation(
            RoundEvaluation(
                index=self._current_round_index,
                measurement=FeedbackMeasurement(
                    acquisition=measurement.acquisition,
                    localization=localization,
                    metrics=None,
                ),
                intensity_analysis=self._current_intensity_analysis,
            )
        )
        self._localization_reference = localization
        return localization

    def localize_feedback(self,state: CGHState,context: SectionContext):
        status = self.feedback_status(state,context)
        candidate = self.compute_feedback_localization_candidate(
            state,context,status.localization_params,
        )
        return self.commit_feedback_localization(
            state,context,candidate,status.localization_params,
        )

    def apply_intensity_feedback(
        self,state: CGHState,context: SectionContext,
    ):
        self._require_capability(state,FeedbackCapability.INTENSITY)
        self._require_current_round_for_feedback(state,context)
        target_state = self._resolve_requested_target_state(state,context)
        self._discard_irrelevant_working_round(target_state)
        self._discard_pending_feedback_adaptation()
        current_round = self._current_round
        measurement = self._require_current_measurement()
        if measurement.localization is None:
            raise RuntimeError(
                "Localize the feedback image before applying intensity feedback"
            )

        analysis = self._current_intensity_analysis
        if analysis is None:
            analysis = self.compute_feedback_intensity_analysis(state,context)
        if analysis.matched_count != analysis.total_count:
            raise RuntimeError(
                "Intensity feedback requires a complete localization before adaptation"
            )
        current = np.asarray(current_round.intensities,dtype=np.float64)
        measured = np.asarray(analysis.spot_powers,dtype=np.float64)
        if measured.shape != current.shape:
            raise ValueError("Feedback spot count does not match target resolution")
        corrections = float(np.mean(measured)) / (measured + 1e-9)
        updated = current * corrections
        maximum = float(np.max(updated))
        if maximum <= 0:
            raise ValueError(
                "Intensity feedback produced no positive target intensities"
            )
        updated = updated / maximum
        evaluation = RoundEvaluation(
            index=current_round.index,
            measurement=measurement,
            intensity_analysis=analysis,
        )
        self._set_current_round_evaluation(evaluation)
        adaptation = IntensityAdaptation(
            source_round_index=current_round.index,
            previous_intensities=current,
            adapted_intensities=updated,
        )
        target_state = self._committed_target
        if target_state is None:
            raise RuntimeError("No committed target is available")
        signature = self._effective_signature(
            target_state,current_round.index + 1,updated,
        )
        self._working_round = CGHWorkingRound(
            index=current_round.index + 1,
            intensities=updated,
            feedback_target_signature=signature,
            adaptation=adaptation,
            feedback_change=FeedbackChangeKind.INTENSITY,
            purpose=CGHPreparedPurpose.WORKING_ROUND,
        )
        self._prefer_previous_initialization = True
        self._requires_fresh_initialization = False
        return adaptation

    def reset_intensity_feedback(
        self,state: CGHState,context: SectionContext,
    ) -> bool:
        self._require_capability(state,FeedbackCapability.INTENSITY)
        if not self._rounds:
            return False
        del state,context
        return self.reset_to_round(0)

    def apply_position_correction(
        self,state: CGHState,context: SectionContext,
        *,reset_intensity: bool=False,
    ):
        self._require_capability(state,FeedbackCapability.POSITION_CORRECTION)
        measurement = self._require_current_measurement()
        if measurement.localization is None:
            raise RuntimeError(
                "Localize the feedback image before applying position correction"
            )
        if self._committed_intensity_count > 0 and not reset_intensity:
            raise RuntimeError(
                "Position correction changes spot positions while intensity "
                "rounds exist. Explicitly reset the intensity sequence."
            )

        target = self._current_round_target(state,context)
        analysis = analyze_position(
            measurement,
            ideal_positions_kxy=target.resolution.ideal_spot_positions_kxy,
            calibration=context.calibration,
            parameters=self._position_params,
        )
        correction = PositionCorrection(
            measurement=measurement,
            analysis=analysis,
            lattice_indices=target.resolution.lattice_indices,
            ideal_positions_kxy=target.resolution.ideal_spot_positions_kxy,
            displacement_kxy=analysis.correction_kxy,
            corrected_positions_kxy=analysis.corrected_positions_kxy,
            calibration=(
                {} if context.calibration is None else context.calibration.to_dict()
            ),
        )
        committed = self._committed_target
        if committed is None:
            raise RuntimeError("No committed target is available")
        base_target = self._build_target(committed,context)
        self._validate_position_correction_for_resolution(
            correction,base_target.resolution,
        )
        changed = (
            self._position_correction is None
            or not np.array_equal(
                self._position_correction.corrected_positions_kxy,
                correction.corrected_positions_kxy,
            )
            or not self._position_active
        )
        if not changed:
            return correction,False
        # Preserve the complete pre-correction round as historical provenance.
        # Re-applying while correction is already active keeps the original
        # uncorrected reference; applying from an inactive state establishes
        # a new uncorrected reference for the replacement correction.
        if self._position_reference_round is None or not self._position_active:
            self._position_reference_round = self._current_round
        self._position_correction = correction
        self._position_active = True
        self._reset_intensity_sequence(state,context)
        return correction,changed

    def set_position_correction_active(
        self,state: CGHState,context: SectionContext,active: bool,
        *,reset_intensity: bool=False,
    ) -> bool:
        self._require_capability(state,FeedbackCapability.POSITION_CORRECTION)
        if self._position_correction is None:
            if active:
                raise RuntimeError("No position correction is available")
            return False
        active = bool(active)
        if active == self._position_active:
            return False
        if self._committed_intensity_count > 0 and not reset_intensity:
            raise RuntimeError(
                "Position correction changes require resetting the intensity "
                "round sequence"
            )
        if active:
            self._validate_position_correction_for_committed_target(context)
        self._position_active = active
        self._reset_intensity_sequence(state,context)
        return True

    def clear_position_correction(
        self,state: CGHState,context: SectionContext,
        *,reset_intensity: bool=False,
    ) -> bool:
        self._require_capability(state,FeedbackCapability.POSITION_CORRECTION)
        if self._position_correction is None:
            return False
        if self._committed_intensity_count > 0 and not reset_intensity:
            raise RuntimeError(
                "Position correction changes require resetting the intensity "
                "round sequence"
            )
        self._position_correction = None
        self._position_reference_round = None
        self._position_active = False
        self._reset_intensity_sequence(state,context)
        return True

    def reset_to_round(self,index: int) -> bool:
        index = int(index)
        if index < 0 or index >= len(self._rounds):
            raise IndexError(f"Unknown complete CGH round {index}")
        if index == len(self._rounds) - 1 and self._working_round is None:
            return False
        self.invalidate_prepared_compute()
        self._rounds = self._rounds[:index + 1]
        self._working_round = None
        current = self._rounds[-1]
        self._applied = AppliedCGH(
            result=current.result,
            origin_round_index=current.index,
            desired_signature_at_apply=current.feedback_target_signature,
        )
        self._localization_reference = self._localization_from_round(current)
        self._prefer_previous_initialization = False
        self._requires_fresh_initialization = False
        return True

    def clear(self,build_candidate: Callable[[],T]) -> T | None:
        if (
            self._committed_target is None
            and not self._rounds
            and self._working_round is None
            and self._applied is None
            and self._position_correction is None
        ):
            return None
        previous = self.clone()
        try:
            self.invalidate_prepared_compute()
            self._committed_target = None
            self._target = None
            self._rounds = ()
            self._working_round = None
            self._applied = None
            self._prepared_request = None
            self._localization_reference = None
            self._position_correction = None
            self._position_reference_round = None
            self._position_active = False
            self._prefer_previous_initialization = False
            self._requires_fresh_initialization = False
            return build_candidate()
        except Exception:
            self.__dict__.update(previous.__dict__)
            raise

    def restore_snapshot(
        self,
        snapshot: CGHSessionSnapshot | None,
        context: SectionContext,
        build_candidate: Callable[[],T],
    ) -> T:
        if snapshot is not None and not isinstance(snapshot,CGHSessionSnapshot):
            raise TypeError("snapshot must be CGHSessionSnapshot or None")
        if snapshot is not None:
            for round_record in snapshot.rounds:
                self._validate_restored_result(round_record.result,context)
            if snapshot.position_reference_round is not None:
                self._validate_restored_result(
                    snapshot.position_reference_round.result,context,
                )
        previous = self.clone()
        try:
            if snapshot is None:
                self._committed_target = None
                self._rounds = ()
                self._position_correction = None
                self._position_reference_round = None
                self._position_active = False
                self._applied = None
                self._localization_reference = None
                self._intensity_params = _default_values(
                    INTENSITY_ANALYSIS_PARAMS
                )
            else:
                self._committed_target = snapshot.committed_target
                self._rounds = snapshot.rounds
                self._position_correction = snapshot.position_correction
                self._position_reference_round = snapshot.position_reference_round
                self._position_active = bool(snapshot.position_active)
                restored_intensity_params = dict(
                    snapshot.intensity_analysis_params or {}
                )
                if not restored_intensity_params:
                    for round_record in self._rounds:
                        evaluation = round_record.evaluation
                        analysis = (
                            None if evaluation is None
                            else evaluation.intensity_analysis
                        )
                        if analysis is not None:
                            restored_intensity_params = dict(analysis.parameters)
                            break
                self._intensity_params = _validated_values(
                    INTENSITY_ANALYSIS_PARAMS,restored_intensity_params,
                )
                for round_record in self._rounds:
                    evaluation = round_record.evaluation
                    analysis = (
                        None if evaluation is None
                        else evaluation.intensity_analysis
                    )
                    if (
                        analysis is not None
                        and dict(analysis.parameters) != self._intensity_params
                    ):
                        raise ValueError(
                            "Persisted CGH rounds use inconsistent intensity "
                            "analysis parameters"
                        )
                if self._committed_target is not None:
                    self._target = None
                    self._build_target(self._committed_target,context)
                if self._position_correction is not None:
                    self._validate_position_correction_for_committed_target(
                        context,
                    )
                self._applied = (
                    None if not self._rounds else AppliedCGH(
                        result=self._rounds[-1].result,
                        origin_round_index=self._rounds[-1].index,
                        desired_signature_at_apply=(
                            self._rounds[-1].feedback_target_signature
                        ),
                    )
                )
                self._localization_reference = (
                    None if not self._rounds
                    else self._localization_from_round(self._rounds[-1])
                )
            self._working_round = (
                self._restored_round_zero_working(context)
                if self._committed_target is not None and not self._rounds
                else None
            )
            self._target = None
            self._prepared_request = None
            if self._applied is not None:
                self._generation = max(
                    self._generation,self._applied.result.generation,
                )
            return build_candidate()
        except Exception:
            self.__dict__.update(previous.__dict__)
            raise

    @property
    def _current_round(self) -> CGHRound:
        if not self._rounds:
            raise RuntimeError("No complete CGH round is available")
        return self._rounds[-1]

    @property
    def _current_round_index(self) -> int | None:
        return None if not self._rounds else self._rounds[-1].index

    @property
    def _committed_intensity_count(self) -> int:
        return max(0,len(self._rounds) - 1)

    @property
    def _current_measurement(self) -> FeedbackMeasurement | None:
        if not self._rounds:
            return None
        evaluation = self._rounds[-1].evaluation
        return None if evaluation is None else evaluation.measurement

    @property
    def _current_intensity_analysis(self):
        if not self._rounds:
            return None
        evaluation = self._rounds[-1].evaluation
        return None if evaluation is None else evaluation.intensity_analysis

    def _localization_from_round(
        self,round_record: CGHRound,
    ) -> LocalizationResult | None:
        evaluation = round_record.evaluation
        if evaluation is None or evaluation.measurement is None:
            return None
        return evaluation.measurement.localization

    def _resolve_requested_target_state(
        self,state: CGHState,context: SectionContext,
    ) -> TargetDefinitionState:
        target_type,target_state,registration = self._selected_target(state)
        canonical_params = dict(target_state.params.values)
        target_signature = registration.target_class.definition_signature_for(
            context,canonical_params,
        )
        return TargetDefinitionState(
            target_type=target_type,
            canonical_params=canonical_params,
            target_signature=target_signature,
            context_signature=compute_context_signature(context),
        )

    @staticmethod
    def _same_target_definition(
        left: TargetDefinitionState | None,
        right: TargetDefinitionState | None,
    ) -> bool:
        """Compare semantic base-target identity, not configuration objects."""
        if left is None or right is None:
            return left is right
        return (
            left.target_type == right.target_type
            and left.target_signature == right.target_signature
        )

    def _build_target(
        self,target_state: TargetDefinitionState,context: SectionContext,
    ) -> Target:
        if (
            self._target is not None
            and self._same_target_definition(
                self._committed_target,target_state,
            )
        ):
            return self._target

        registration = self.registries.targets[target_state.target_type]
        prepared_definition = None
        if self._prepared_definition_signature == target_state.target_signature:
            prepared_definition = self._prepared_definition
        target = registration.target_class(
            context=context,
            prepared_definition=prepared_definition,
            **dict(target_state.canonical_params),
        )
        if target.signature != target_state.target_signature:
            raise RuntimeError(
                "Rebuilt target signature does not match target state"
            )
        if self._same_target_definition(self._committed_target,target_state):
            self._target = target
        return target

    def _prepare_destination(
        self,
        target_state: TargetDefinitionState,
        base_resolution: TargetResolution,
        state: CGHState,
    ) -> tuple[CGHPreparedPurpose, int, np.ndarray, IntensityAdaptation | None]:
        del state
        if self._committed_target is None:
            working = self._working_round_for_target(target_state)
            if working is not None:
                return (
                    CGHPreparedPurpose.TARGET_REPLACEMENT,
                    working.index,
                    working.intensities,
                    working.adaptation,
                )
            intensities = np.asarray(base_resolution.spot_intensities,dtype=np.float64)
            return CGHPreparedPurpose.TARGET_REPLACEMENT,0,intensities,None
        if target_state.target_signature != self._committed_target.target_signature:
            working = self._working_round_for_target(target_state)
            if working is not None:
                return (
                    CGHPreparedPurpose.TARGET_REPLACEMENT,
                    working.index,
                    working.intensities,
                    working.adaptation,
                )
            intensities = np.asarray(base_resolution.spot_intensities,dtype=np.float64)
            return CGHPreparedPurpose.TARGET_REPLACEMENT,0,intensities,None
        working = self._working_round_for_target(target_state)
        if working is not None:
            return (
                working.purpose,
                working.index,
                working.intensities,
                working.adaptation,
            )
        if self._rounds:
            current = self._rounds[-1]
            return (
                CGHPreparedPurpose.RECOMPUTE_ROUND,
                current.index,
                current.intensities,
                current.adaptation,
            )
        intensities = np.asarray(base_resolution.spot_intensities,dtype=np.float64)
        return CGHPreparedPurpose.WORKING_ROUND,0,intensities,None

    def _working_round_for_target(
        self,target_state: TargetDefinitionState | None,
    ) -> CGHWorkingRound | None:
        working = self._working_round
        if working is None or target_state is None:
            return None
        if working.purpose is CGHPreparedPurpose.TARGET_REPLACEMENT:
            if working.target_state is None:
                return None
            if working.target_state.target_signature == target_state.target_signature:
                return working
            return None
        committed = self._committed_target
        if committed is None:
            return None
        if target_state.target_signature == committed.target_signature:
            return working
        return None

    def _discard_irrelevant_working_round(
        self,target_state: TargetDefinitionState | None,
    ) -> bool:
        if self._working_round is None:
            return False
        if self._working_round_for_target(target_state) is not None:
            return False
        self.invalidate_prepared_compute()
        self._working_round = None
        return True

    def _implicit_round_zero_working(
        self,
        target_state: TargetDefinitionState,
        context: SectionContext,
        state: CGHState,
    ) -> CGHWorkingRound | None:
        if self._rounds or self._working_round is not None:
            return None
        target = self._build_target(target_state,context)
        purpose,index,intensities,adaptation = self._prepare_destination(
            target_state,target.resolution,state,
        )
        if index != 0 or adaptation is not None:
            return None
        include_position = purpose is not CGHPreparedPurpose.TARGET_REPLACEMENT
        signature = self._effective_signature(
            target_state,0,intensities,
            include_position=include_position,
        )
        return CGHWorkingRound(
            index=0,
            intensities=intensities,
            feedback_target_signature=signature,
            target_state=(
                target_state
                if purpose is CGHPreparedPurpose.TARGET_REPLACEMENT
                else None
            ),
            purpose=purpose,
        )

    def _restored_round_zero_working(
        self,context: SectionContext,
    ) -> CGHWorkingRound:
        target_state = self._committed_target
        if target_state is None:
            raise RuntimeError("No committed target is available")
        target = self._build_target(target_state,context)
        intensities = np.asarray(target.resolution.spot_intensities,dtype=np.float64)
        signature = self._effective_signature(target_state,0,intensities)
        return CGHWorkingRound(
            index=0,
            intensities=intensities,
            feedback_target_signature=signature,
            feedback_change=FeedbackChangeKind.POSITION,
            purpose=CGHPreparedPurpose.WORKING_ROUND,
        )

    def _working_matches_state(
        self,state: CGHState,context: SectionContext,
    ) -> bool:
        """Return whether transient working state belongs to the base target."""
        working = self._working_round
        if working is None or state.selected_target is None:
            return False
        target_state = working.target_state or self._committed_target
        if target_state is None:
            return False
        try:
            requested = self._resolve_requested_target_state(state,context)
        except Exception:
            return False
        return (
            requested.target_type == target_state.target_type
            and requested.target_signature == target_state.target_signature
        )

    def _current_round_target(
        self,state: CGHState,context: SectionContext,
    ) -> Target:
        self._require_current_round_for_feedback(state,context)
        committed = self._committed_target
        if committed is None:
            raise RuntimeError("No committed target is available")
        base = self._build_target(committed,context)
        resolution = self._effective_resolution(
            base,self._current_round.intensities,
        )
        candidate = base.clone()
        candidate.resolution = resolution
        candidate.array = (
            resolution.target_array
            if resolution.target_array is not None else resolution.preview
        )
        return candidate

    def _current_round_resolution(
        self,state: CGHState,context: SectionContext,
    ) -> TargetResolution:
        return self._current_round_target(state,context).resolution

    def _effective_signature(
        self,
        target_state: TargetDefinitionState,
        round_index: int,
        intensities: np.ndarray,
        *,
        include_position: bool=True,
    ) -> CGHSignature:
        return compute_feedback_target_signature(
            target_signature=target_state.target_signature,
            position_signature=(
                compute_position_correction_signature(
                    self._position_correction
                ) if include_position and self._position_active else None
            ),
            round_index=round_index,
            intensities=intensities,
        )

    def _round_inspection(
        self,
        round_record: CGHRound,
        context: SectionContext | None,
        *,
        include_position: bool=True,
    ) -> CGHRoundInspection:
        preview,target_display = self._inspection_target_payload(
            self._committed_target,context,round_record.intensities,
            include_position=include_position,
        )
        return CGHRoundInspection(
            index=round_record.index,
            state="computed",
            intensities=round_record.intensities,
            result=round_record.result,
            feedback_target_signature=round_record.feedback_target_signature,
            target_preview=preview,
            target_display=target_display,
            adaptation=round_record.adaptation,
            evaluation=round_record.evaluation,
        )

    def _working_round_inspection(
        self,
        working: CGHWorkingRound,
        context: SectionContext | None,
    ) -> CGHRoundInspection:
        target_state = (
            working.target_state
            if working.purpose is CGHPreparedPurpose.TARGET_REPLACEMENT
            else self._committed_target
        )
        include_position = (
            working.purpose is not CGHPreparedPurpose.TARGET_REPLACEMENT
        )
        preview,target_display = self._inspection_target_payload(
            target_state,
            context,
            working.intensities,
            include_position=include_position,
        )
        return CGHRoundInspection(
            index=working.index,
            state=working.state.value,
            intensities=working.intensities,
            result=None,
            feedback_target_signature=working.feedback_target_signature,
            target_preview=preview,
            target_display=target_display,
            adaptation=working.adaptation,
            purpose=working.purpose,
            target_state=working.target_state,
            unavailable_reason=(
                working.failure_message
                if working.state is CGHWorkingRoundState.FAILED
                else "CGH has not been computed for this round"
            ),
        )

    def _inspection_target_preview(
        self,
        target_state: TargetDefinitionState | None,
        context: SectionContext | None,
        intensities: np.ndarray,
        *,
        include_position: bool=True,
    ) -> np.ndarray | None:
        """Build one detached effective-target preview for session inspection."""
        preview,_target_display = self._inspection_target_payload(
            target_state,context,intensities,include_position=include_position,
        )
        return preview

    def _inspection_target_payload(
        self,
        target_state: TargetDefinitionState | None,
        context: SectionContext | None,
        intensities: np.ndarray,
        *,
        include_position: bool=True,
    ) -> tuple[np.ndarray | None, CGHTargetDisplay | None]:
        """Build detached preview and resolved spot display data for inspection."""
        if target_state is None or context is None:
            return None,None
        try:
            target = self._build_target(target_state,context)
            resolution = self._effective_resolution(
                target,intensities,include_position=include_position,
            )
            preview = (
                resolution.target_array
                if resolution.target_array is not None
                else resolution.preview
            )
            return (
                np.array(preview,dtype=np.float64,copy=True),
                CGHTargetDisplay(
                    positions_kxy=resolution.spot_positions_kxy,
                    intensities=resolution.spot_intensities,
                ),
            )
        except Exception:
            return None,None

    def _effective_resolution(
        self,
        target: Target,
        intensities: np.ndarray,
        *,
        include_position: bool=True,
    ) -> TargetResolution:
        base = target.resolution
        intensities = np.asarray(intensities,dtype=np.float64)
        if intensities.shape != base.spot_intensities.shape:
            raise RuntimeError(
                "Stored intensity feedback is incompatible with target resolution"
            )
        positions = None
        if (
            include_position
            and self._position_active
            and self._position_correction is not None
        ):
            positions = self._position_correction.corrected_positions_kxy
        return target.with_resolution_updates(
            base,
            spot_positions_kxy=positions,
            spot_intensities=intensities,
        )

    @staticmethod
    def _validate_position_correction_for_resolution(
        correction: PositionCorrection,
        base: TargetResolution,
    ) -> None:
        if not isinstance(correction,PositionCorrection):
            raise TypeError("correction must be a PositionCorrection")
        if not isinstance(base,TargetResolution):
            raise TypeError("base must be a TargetResolution")
        if not np.array_equal(correction.lattice_indices,base.lattice_indices):
            raise RuntimeError(
                "Position correction lattice does not match target resolution"
            )
        if not np.array_equal(
            correction.ideal_positions_kxy,base.ideal_spot_positions_kxy,
        ):
            raise RuntimeError(
                "Position correction ideal positions do not match target resolution"
            )
        if correction.corrected_positions_kxy.shape != base.spot_positions_kxy.shape:
            raise RuntimeError(
                "Position correction spot positions are incompatible with "
                "target resolution"
            )
        if not np.all(np.isfinite(correction.corrected_positions_kxy)):
            raise RuntimeError(
                "Position correction contains non-finite corrected positions"
            )

    def _validate_position_correction_for_committed_target(
        self,context: SectionContext,
    ) -> None:
        correction = self._position_correction
        if correction is None:
            return
        target_state = self._committed_target
        if target_state is None:
            raise RuntimeError(
                "Position correction requires a committed target"
            )
        target = self._build_target(target_state,context)
        self._validate_position_correction_for_resolution(
            correction,target.resolution,
        )

    def _build_spec(
        self,
        state: CGHState,
        context: SectionContext,
        target_state: TargetDefinitionState,
        effective_signature: CGHSignature,
    ) -> CGHSpec:
        _,ui_target_state,_ = self._selected_target(state)
        return CGHSpec(
            context=context,
            target_type=target_state.target_type,
            algorithm=ui_target_state.algorithm,
            target_params=target_state.canonical_params,
            compute_params=ui_target_state.computation.params.values,
            feedback_target_signature=effective_signature,
        )

    def _initial_field_for(
        self,spec: CGHSpec,context: SectionContext,
    ) -> np.ndarray | None:
        if (
            not self._prefer_previous_initialization
            or self._requires_fresh_initialization
            or self._applied is None
        ):
            return None
        previous = self._applied.result
        if (
            previous.spec.target_type == spec.target_type
            and previous.spec.algorithm == spec.algorithm
            and previous.pattern.shape == context.shape
        ):
            return previous.pattern
        return None

    def _request_matches_current_inputs(
        self,
        request: CGHPreparedRequest,
        state: CGHState,
        context: SectionContext,
    ) -> bool:
        try:
            target_state = self._resolve_requested_target_state(state,context)
            target = self._build_target(target_state,context)
            purpose,index,intensities,_adaptation = self._prepare_destination(
                target_state,target.resolution,state,
            )
            apply_position = purpose is not CGHPreparedPurpose.TARGET_REPLACEMENT
            effective = self._effective_signature(
                target_state,index,intensities,
                include_position=apply_position,
            )
            spec = self._build_spec(state,context,target_state,effective)
        except Exception:
            return False
        return (
            request.purpose is purpose
            and request.round_index == index
            and request.feedback_target_signature == effective
            and request.spec_signature == spec.signature
        )

    def _applied_matches(
        self,
        applied: AppliedCGH,
        desired_signature: CGHSignature,
        state: CGHState,
        context: SectionContext,
    ) -> bool:
        try:
            target_state = self._resolve_requested_target_state(state,context)
            spec = self._build_spec(
                state,context,target_state,desired_signature,
            )
        except Exception:
            return False
        return applied.result.spec.has_same_inputs(spec)

    def _commit_result_transactionally(
        self,
        result: CGHResult,
        request: CGHPreparedRequest,
        build_candidate: Callable[[],T],
    ) -> T | None:
        previous = self.clone()
        try:
            self._prepared_request = None
            if request.purpose is CGHPreparedPurpose.TARGET_REPLACEMENT:
                target_state = request.target_state
                if target_state is None:
                    return None
                self._committed_target = target_state
                self._target = None
                self._rounds = (
                    CGHRound(
                        index=0,
                        intensities=self._prepared_intensities(request),
                        result=result,
                        feedback_target_signature=(
                            request.feedback_target_signature
                        ),
                    ),
                )
                self._working_round = None
                self._position_correction = None
                self._position_reference_round = None
                self._position_active = False
                self._localization_reference = None
                applied_index = 0
            elif request.purpose is CGHPreparedPurpose.WORKING_ROUND:
                working = self._working_round
                if working is None or working.index != request.round_index:
                    return None
                if request.round_index != len(self._rounds):
                    return None
                self._rounds = self._rounds + (
                    CGHRound(
                        index=working.index,
                        intensities=working.intensities,
                        result=result,
                        feedback_target_signature=(
                            working.feedback_target_signature
                        ),
                        adaptation=working.adaptation,
                    ),
                )
                self._working_round = None
                applied_index = request.round_index
            else:
                if not self._rounds:
                    return None
                current = self._rounds[-1]
                if current.index != request.round_index:
                    return None
                self._rounds = self._rounds[:-1] + (current.with_result(result),)
                applied_index = request.round_index

            self._applied = AppliedCGH(
                result=result,
                origin_round_index=applied_index,
                desired_signature_at_apply=request.feedback_target_signature,
            )
            self._prefer_previous_initialization = False
            self._requires_fresh_initialization = False
            built = build_candidate()
        except Exception:
            self.__dict__.update(previous.__dict__)
            raise
        return built

    def _prepared_intensities(
        self,request: CGHPreparedRequest,
    ) -> np.ndarray:
        if (
            self._working_round is not None
            and self._working_round.index == request.round_index
            and self._working_round.feedback_target_signature
            == request.feedback_target_signature
        ):
            return self._working_round.intensities
        raise RuntimeError("Prepared working intensities are no longer available")

    def _reset_intensity_sequence(
        self,state: CGHState,context: SectionContext,
    ) -> None:
        self.invalidate_prepared_compute()
        target_state = self._committed_target
        if target_state is None:
            target_state = self._resolve_requested_target_state(state,context)
            self._committed_target = target_state
        target = self._build_target(target_state,context)
        intensities = np.asarray(target.resolution.spot_intensities,dtype=np.float64)
        signature = self._effective_signature(target_state,0,intensities)
        self._rounds = ()
        self._working_round = CGHWorkingRound(
            index=0,
            intensities=intensities,
            feedback_target_signature=signature,
            feedback_change=FeedbackChangeKind.POSITION,
            purpose=CGHPreparedPurpose.WORKING_ROUND,
        )
        if self._applied is not None:
            self._applied = self._applied.orphaned()
        self._localization_reference = None
        self._prefer_previous_initialization = False
        self._requires_fresh_initialization = True

    def _set_current_round_evaluation(
        self,evaluation: RoundEvaluation,
    ) -> None:
        if not self._rounds:
            raise RuntimeError("No complete CGH round is available")
        current = self._rounds[-1]
        if evaluation.index != current.index:
            raise ValueError("Evaluation does not belong to current round")
        self._rounds = self._rounds[:-1] + (current.with_evaluation(evaluation),)

    def _require_current_measurement(self) -> FeedbackMeasurement:
        measurement = self._current_measurement
        if measurement is None:
            raise RuntimeError("Acquire or load a feedback image before localization")
        return measurement

    def _discard_pending_feedback_adaptation(self) -> bool:
        working = self._working_round
        if (
            working is None
            or working.purpose is not CGHPreparedPurpose.WORKING_ROUND
            or working.adaptation is None
        ):
            return False
        self.invalidate_prepared_compute()
        self._working_round = None
        return True

    def _require_current_round_for_feedback(
        self,state: CGHState,context: SectionContext,
    ) -> None:
        if not self._rounds:
            raise RuntimeError("Compute the CGH before using feedback")
        committed = self._committed_target
        if committed is None:
            raise RuntimeError("Compute the CGH before using feedback")
        try:
            requested = self._resolve_requested_target_state(state,context)
        except Exception as error:
            raise RuntimeError("Recompute the CGH before using feedback") from error
        if requested.target_signature != committed.target_signature:
            raise RuntimeError("Recompute the CGH before using feedback")

        current = self._rounds[-1]
        applied = self._applied
        if (
            applied is None
            or applied.origin_round_index != current.index
            or applied.desired_signature_at_apply
            != current.feedback_target_signature
        ):
            raise RuntimeError("Recompute the CGH before using feedback")
        spec = self._build_spec(
            state,context,committed,current.feedback_target_signature,
        )
        if not applied.result.spec.has_same_inputs(spec):
            raise RuntimeError("Recompute the CGH before using feedback")

    def _selected_target(
        self,state: CGHState,
    ) -> tuple[str, CGHTargetState, TargetRegistration]:
        target_type = state.selected_target
        if target_type is None:
            raise RuntimeError("No CGH target selected")
        return (
            target_type,
            state.items[target_type],
            self.registries.targets[target_type],
        )

    def _feedback_capabilities(
        self,state: CGHState,
    ) -> tuple[FeedbackCapability, ...]:
        if state.selected_target is None:
            return ()
        registration = self.registries.targets[state.selected_target]
        return tuple(
            FeedbackCapability(item) for item in registration.feedback_capabilities
        )

    def _require_any_feedback_capability(self,state: CGHState) -> None:
        if not self._feedback_capabilities(state):
            raise RuntimeError("Selected CGH target does not support feedback")

    def _require_capability(
        self,state: CGHState,capability: FeedbackCapability,
    ) -> None:
        if capability not in self._feedback_capabilities(state):
            raise RuntimeError(
                f"Selected CGH target does not support {capability.value}"
            )

    @staticmethod
    def _validate_restored_result(
        result: CGHResult | None,context: SectionContext,
    ) -> None:
        if result is None:
            return
        if not isinstance(result,CGHResult):
            raise TypeError("result must be a CGHResult or None")
        if result.generation < 0:
            raise InvalidCGHResultError("CGH result generation must be >= 0")
        if result.pattern.shape != context.shape:
            raise InvalidCGHResultError(
                f"Restored CGH pattern shape {result.pattern.shape}; "
                f"expected {context.shape}"
            )


def _default_values(specs) -> Mapping[str,Any]:
    return {key:spec.validate(spec.default) for key,spec in specs.items()}


def _validated_values(specs,values):
    values = dict(values or {})
    unknown = set(values) - set(specs)
    if unknown:
        raise KeyError(
            "Unknown localization parameter(s): "
            + ", ".join(sorted(unknown))
        )
    return {
        key:spec.validate(values.get(key,spec.default))
        for key,spec in specs.items()
    }
