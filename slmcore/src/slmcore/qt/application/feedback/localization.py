"""Qt-side localization interaction for feedback workflows."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any,Mapping,TYPE_CHECKING

if TYPE_CHECKING:
    from ....application.feedback import SLMFeedbackService
    from ....core.cgh.localization import LocalizationResult
    from ....core.cgh.measurement_metrics import IntensityAnalysis
    from ...cgh.session_window import CGHSessionWindow


class LocalizationCoordinator:
    """Present localization candidates and commit localization UI actions."""

    def __init__(
        self,
        feedback_service: "SLMFeedbackService",
        *,
        window_for: Callable[[str],"CGHSessionWindow | None"],
        synchronize_section: Callable[[str],None],
        report_error: Callable[[str,Exception],None],
    ) -> None:
        self._feedback = feedback_service
        self._window_for = window_for
        self._synchronize_section = synchronize_section
        self._report_error = report_error

    def localization_candidate(
        self,section_key: str,parameters: Mapping[str,Any],
    ) -> tuple[LocalizationResult,IntensityAnalysis | None]:
        return self._feedback.localization_candidate(section_key,parameters)

    def run_localization_candidate(
        self,section_key: str,parameters: Mapping[str,Any],
    ) -> None:
        try:
            candidate,metrics = self._feedback.localization_candidate(
                section_key,parameters,
            )
            window = self._window_for(section_key)
            if window is not None:
                window.set_localization_result(
                    candidate,parameters,metrics=metrics,
                )
        except Exception as error:
            self.set_error(section_key,error)

    def infer_missing_localization_candidate(
        self,section_key: str,localization: LocalizationResult,parameters: Mapping[str,Any],
    ) -> None:
        try:
            candidate,metrics = self._feedback.infer_missing_localization_candidate(
                section_key,localization,
            )
            window = self._window_for(section_key)
            if window is not None:
                window.set_localization_result(
                    candidate,parameters,metrics=metrics,
                )
        except Exception as error:
            self.set_error(section_key,error)

    def accept_localization(
        self,
        section_key: str,
        localization: LocalizationResult,
        parameters: Mapping[str,Any],
        *,
        raise_errors: bool=False,
    ) -> None:
        self._feedback.accept_localization(
            section_key,localization,parameters,raise_errors=raise_errors,
        )

    def reuse_localization(
        self,section_key: str,*,raise_errors: bool=False,
    ) -> LocalizationResult | None:
        return self._feedback.reuse_localization(
            section_key,raise_errors=raise_errors,
        )

    def localize_and_commit(self,section_key: str) -> None:
        self._feedback.localize_and_commit(section_key)

    def update_parameters(
        self,
        section_key: str,
        group: str,
        changes: Mapping[str,Any],
        *,
        localization: LocalizationResult | None=None,
        localization_parameters: Mapping[str,Any] | None=None,
    ) -> None:
        try:
            result = self._feedback.update_parameters(
                section_key,group,changes,localization=localization,
            )
            window = self._window_for(section_key)
            if (
                window is not None
                and localization is not None
                and result.candidate_analysis is not None
            ):
                window.set_localization_result(
                    localization,dict(localization_parameters or {}),
                    metrics=result.candidate_analysis,
                )
        except Exception as error:
            self._report_error("Feedback parameter update failed",error)
            self._synchronize_section(section_key)

    def localization_context(self,section_key: str) -> Mapping[str,Any]:
        return self._feedback.localization_context(section_key)

    def require_current_cgh_for_localization_commit(self,section_key: str) -> None:
        self._feedback.require_current_cgh_for_localization_commit(section_key)

    def validate_target_hint_use(
        self,section_key: str,parameters: Mapping[str,Any],
    ) -> None:
        self._feedback.validate_target_hint_use(section_key,parameters)

    def on_error(self,section_key: str,error: Exception) -> None:
        self.set_error(section_key,error)

    def set_error(self,section_key: str,error: Exception) -> None:
        window = self._window_for(section_key)
        if window is not None:
            window.set_localization_error(error)
        else:
            self._report_error("Localization failed",error)
