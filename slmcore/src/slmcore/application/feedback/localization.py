"""Localization commit, reuse and parameter-update workflow."""

from __future__ import annotations

from typing import Any,Callable,Mapping,TYPE_CHECKING

import numpy as np

from ...core.cgh.execution.status import CGHResultState
from ...core.cgh.feedback import IntensityAnalysis
from ...core.cgh.localization import LocalizationResult,infer_missing_localization
from ...core.cgh.localization.policy import suggest_localization_sources
from ...core.measurement import ImageMeasurement
from .component import FeedbackComponent
from .contracts import FeedbackParameterUpdateResult

if TYPE_CHECKING:
    from ...core.engine.runtime import SLMRuntime
    from .contracts import SLMFeedbackCallbacks


class LocalizationService(FeedbackComponent):
    """Own localization computation/commit policy without owning acquisition."""

    def __init__(
        self,
        *,
        runtime_provider: Callable[[], "SLMRuntime"],
        callbacks_provider: Callable[[], "SLMFeedbackCallbacks"],
        editor_writes_allowed: Callable[[], bool],
        feedback_orientation: Callable[[str],Any],
        update_committed_analysis: Callable[[str,LocalizationResult | None],None],
        refresh_editable_reference: Callable[[str,LocalizationResult],None],
        pixel_size_um: Callable[[str],float | None] | None=None,
    ) -> None:
        super().__init__(
            runtime_provider=runtime_provider,
            callbacks_provider=callbacks_provider,
            editor_writes_allowed=editor_writes_allowed,
        )
        self._feedback_orientation = feedback_orientation
        self._update_committed_analysis_callback = update_committed_analysis
        self._refresh_editable_reference = refresh_editable_reference
        self._pixel_size_um = pixel_size_um or (lambda _section:None)

    def commit_measurement(
        self,
        section_key: str,
        measurement: ImageMeasurement,
        *,
        reuse_previous_localization: bool=False,
    ) -> bool:
        self._require_editor_mode()
        runtime = self._runtime
        previous_available = bool(
            runtime.get_section_feedback_status(
                section_key
            ).previous_localization_available
        )
        runtime.set_section_feedback_measurement(section_key,measurement)
        cgh_status = runtime.get_section_cgh_status(section_key)
        target_hints_allowed = cgh_status.result_state is CGHResultState.CURRENT
        context = (
            runtime.get_section_feedback_localization_context(
                section_key,pixel_size_um=measurement.pixel_size_um,
            )
            if target_hints_allowed else {}
        )
        defaults = suggest_localization_sources(
            measurement,context,allow_target_hints=target_hints_allowed,
        )
        runtime.update_section_feedback_parameters(
            section_key,"localization",defaults,
        )
        if reuse_previous_localization and previous_available:
            try:
                self.reuse_localization(
                    section_key,raise_errors=True,notify=False,
                )
            except Exception as error:
                self._warning(
                    "Feedback localization",
                    "Previous localization could not be reused: %s" % error,
                )
        self._feedback_state_changed(section_key)
        return previous_available

    def localization_candidate(
        self,section_key: str,parameters: Mapping[str,Any],
    ) -> tuple[LocalizationResult,IntensityAnalysis | None]:
        self.validate_target_hint_use(section_key,parameters)
        runtime = self._runtime
        candidate = runtime.compute_section_feedback_localization_candidate(
            section_key,parameters,
        )
        metrics = None
        try:
            metrics = runtime.compute_section_feedback_intensity_analysis(
                section_key,candidate,orientation=self._feedback_orientation(section_key),
            )
        except Exception as error:
            self._warning(
                "Measurement metrics",
                "Measurement metrics are unavailable: %s" % error,
            )
        return candidate,metrics

    def infer_missing_localization_candidate(
        self,section_key: str,localization: LocalizationResult,
    ) -> tuple[LocalizationResult,IntensityAnalysis | None]:
        """Return a candidate with unresolved lattice sites explicitly inferred."""
        self._require_editor_mode()
        candidate = infer_missing_localization(localization)
        metrics = None
        try:
            metrics = self._runtime.compute_section_feedback_intensity_analysis(
                section_key,candidate,orientation=self._feedback_orientation(section_key),
            )
        except Exception as error:
            self._warning(
                "Measurement metrics",
                "Measurement metrics are unavailable: %s" % error,
            )
        return candidate,metrics

    def accept_localization(
        self,
        section_key: str,
        localization: LocalizationResult,
        parameters: Mapping[str,Any],
        *,
        raise_errors: bool=False,
    ) -> bool:
        try:
            self._require_editor_mode()
            self.require_current_cgh_for_localization_commit(section_key)
            self.validate_target_hint_use(section_key,parameters)
            runtime = self._runtime
            runtime.commit_section_feedback_localization(
                section_key,localization,parameters,
            )
            self._refresh_editable_reference(section_key,localization)
            self._update_committed_analysis_callback(section_key,localization)
            self._feedback_state_changed(section_key)
            return True
        except Exception as error:
            if raise_errors:
                raise
            self._callbacks.on_localization_error(section_key,error)
            return False

    def reuse_localization(
        self,section_key: str,*,raise_errors: bool=False,notify: bool=True,
    ) -> LocalizationResult | None:
        try:
            self._require_editor_mode()
            self.require_current_cgh_for_localization_commit(section_key)
            localization = self._runtime.reuse_section_feedback_localization(
                section_key
            )
            self._update_committed_analysis_callback(section_key,localization)
            if notify:
                self._feedback_state_changed(section_key)
            return localization
        except Exception as error:
            if raise_errors:
                raise
            self._callbacks.on_localization_error(section_key,error)
            return None

    def localize_and_commit(
        self,section_key: str,*,infer_missing_fraction: float | None=None,
    ) -> LocalizationResult:
        status = self._runtime.get_section_feedback_status(section_key)
        parameters = dict(status.localization_params)
        candidate,_metrics = self.localization_candidate(section_key,parameters)
        if infer_missing_fraction is not None:
            candidate = self._infer_missing_within_fraction(
                candidate,infer_missing_fraction,
            )
        self.accept_localization(
            section_key,candidate,parameters,raise_errors=True,
        )
        return candidate

    @staticmethod
    def _infer_missing_within_fraction(
        localization: LocalizationResult,max_missing_fraction: float,
    ) -> LocalizationResult:
        """Infer unresolved sites only within an explicit automatic limit."""
        limit = float(max_missing_fraction)
        if not 0.0 <= limit <= 1.0:
            raise ValueError("infer_missing_fraction must be between 0 and 1")

        total = int(localization.lattice_indices.shape[1])
        if total <= 0:
            return localization
        diagnostics = dict(localization.diagnostics or {})
        matched_value = diagnostics.get("matched_mask")
        if matched_value is None:
            # Older localization results were complete by construction.
            return localization

        matched = np.asarray(matched_value,dtype=bool)
        if matched.shape != (total,):
            raise RuntimeError("Localization matched-mask shape is inconsistent")
        inferred_value = diagnostics.get("inferred_mask")
        inferred = (
            np.zeros(total,dtype=bool)
            if inferred_value is None
            else np.asarray(inferred_value,dtype=bool)
        )
        if inferred.shape != (total,):
            raise RuntimeError("Localization inferred-mask shape is inconsistent")
        if np.any(matched & inferred):
            raise RuntimeError("Localization matched and inferred masks overlap")
        unresolved = ~(matched | inferred)
        missing = int(np.count_nonzero(unresolved))
        if missing == 0:
            return localization

        missing_fraction = float(missing) / float(total)
        if missing_fraction > limit:
            raise RuntimeError(
                "Automatic feedback localization is incomplete: "
                f"{missing}/{total} spots are unresolved "
                f"({100.0*missing_fraction:.1f}%), exceeding the "
                f"{100.0*limit:.1f}% automatic inference limit."
            )
        return infer_missing_localization(localization)

    def update_parameters(
        self,
        section_key: str,
        group: str,
        changes: Mapping[str,Any],
        *,
        localization: LocalizationResult | None=None,
    ) -> FeedbackParameterUpdateResult:
        self._require_editor_mode()
        runtime = self._runtime
        changed = runtime.update_section_feedback_parameters(
            section_key,group,dict(changes or {}),
        )
        candidate_analysis = None
        if changed and str(group) in ("intensity","intensity_analysis"):
            if localization is not None:
                try:
                    candidate_analysis = runtime.compute_section_feedback_intensity_analysis(
                        section_key,localization,
                        orientation=self._feedback_orientation(section_key),
                    )
                except Exception as error:
                    self._warning(
                        "Intensity analysis",
                        "Candidate intensity analysis is unavailable: %s" % error,
                    )
            elif runtime.get_section_feedback_status(
                section_key
            ).localization_available:
                self._update_committed_analysis_callback(section_key,None)
        if changed:
            self._feedback_state_changed(section_key)
        return FeedbackParameterUpdateResult(
            changed=bool(changed),candidate_analysis=candidate_analysis,
        )

    def localization_context(self,section_key: str) -> Mapping[str,Any]:
        runtime = self._runtime
        status = runtime.get_section_cgh_status(section_key)
        if status.result_state is not CGHResultState.CURRENT:
            return {}
        return runtime.get_section_feedback_localization_context(
            section_key,pixel_size_um=self._pixel_size_um(section_key),
        )

    def require_current_cgh_for_localization_commit(self,section_key: str) -> None:
        status = self._runtime.get_section_cgh_status(section_key)
        if status.result_state is CGHResultState.MISSING:
            raise RuntimeError(
                "No CGH has been computed yet. Compute the CGH before accepting "
                "feedback localization."
            )
        if status.result_state is CGHResultState.STALE:
            raise RuntimeError(
                "The computed CGH is stale. Recompute it before accepting "
                "feedback localization."
            )

    def validate_target_hint_use(
        self,section_key: str,parameters: Mapping[str,Any],
    ) -> None:
        uses_target = any(
            str(parameters.get(key,"auto")).strip().lower() == "target"
            for key in (
                "period_prior_mode","stagger_prior_mode",
                "lattice_size_prior_mode",
            )
        )
        if not uses_target:
            return
        status = self._runtime.get_section_cgh_status(section_key)
        if status.result_state is not CGHResultState.CURRENT:
            raise RuntimeError(
                "Target localization guidance requires a current CGH result. "
                "Use automatic/manual localization or recompute the CGH first."
            )
