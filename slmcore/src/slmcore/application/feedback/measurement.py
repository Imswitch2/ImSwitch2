"""Measurement acquisition and source-selection workflow."""

from __future__ import annotations

from typing import Any,Callable,Mapping,Sequence,TYPE_CHECKING

from ...core.measurement import ImageMeasurement
from .component import FeedbackComponent
from .contracts import MeasurementDispatcher,MeasurementRequest

if TYPE_CHECKING:
    from .service import SLMFeedbackService


class MeasurementService(FeedbackComponent):
    """Own the measurement dispatcher and in-flight acquisition requests."""

    def __init__(
        self,feedback: "SLMFeedbackService",
        dispatcher: MeasurementDispatcher | None=None,
    ) -> None:
        super().__init__(feedback)
        self._measurement_dispatcher = dispatcher
        self._measurement_requests: dict[str,MeasurementRequest] = {}

    @property
    def dispatcher(self) -> MeasurementDispatcher | None:
        return self._measurement_dispatcher

    def set_dispatcher(self,dispatcher: MeasurementDispatcher | None) -> bool:
        if dispatcher is self._measurement_dispatcher:
            return False
        self._measurement_dispatcher = dispatcher
        return True

    def available_sources(self,section_key: str) -> Sequence[str]:
        measurements = self._measurement_dispatcher
        if measurements is None:
            return ()
        return tuple(measurements.available_sources(section_key))

    def preferred_source(
        self,section_key: str,available: Sequence[str],
    ) -> str | None:
        measurements = self._measurement_dispatcher
        if measurements is None:
            return None
        return measurements.preferred_source(section_key,available)

    def request_measurement(
        self,
        section_key: str,
        source: str,
        *,
        metadata: Mapping[str,Any] | None,
        on_result: Callable[[ImageMeasurement],None],
        on_error: Callable[[Exception],None],
    ) -> None:
        """Replace a section's pending acquisition; the dispatcher owns late-result guards."""
        self._require_editor_mode()
        self.cancel_measurement(section_key)
        self._callbacks.on_measurement_busy_changed(
            section_key,True,"Waiting for %s..." % source,
        )
        measurements = self._measurement_dispatcher
        if measurements is None:
            error = RuntimeError("No host measurement provider is configured.")
            self._callbacks.on_measurement_busy_changed(section_key,False,"")
            self._callbacks.on_measurement_error(section_key,error)
            on_error(error)
            return

        def result_callback(measurement: ImageMeasurement) -> None:
            self._measurement_requests.pop(section_key,None)
            self._callbacks.on_measurement_busy_changed(section_key,False,"")
            if not self._application_session.editor_writes_allowed:
                return
            on_result(measurement)

        def error_callback(error: Exception) -> None:
            self._measurement_requests.pop(section_key,None)
            self._callbacks.on_measurement_busy_changed(section_key,False,"")
            if not isinstance(error,Exception):
                error = RuntimeError(str(error))
            self._callbacks.on_measurement_error(section_key,error)
            on_error(error)

        try:
            request = measurements.acquire(
                section_key,
                source,
                metadata=metadata,
                on_result=result_callback,
                on_error=error_callback,
            )
        except Exception as error:
            self._callbacks.on_measurement_busy_changed(section_key,False,"")
            self._callbacks.on_measurement_error(section_key,error)
            on_error(error)
            return
        if request.active:
            self._measurement_requests[section_key] = request

    def cancel_measurement(self,section_key: str) -> None:
        """Cancel a tracked request and report idle, leaving committed measurements intact."""
        request = self._measurement_requests.pop(section_key,None)
        if request is not None:
            request.cancel()
        self._callbacks.on_measurement_busy_changed(section_key,False,"")

    def acquire(
        self,
        section_key: str,
        source: str,
        *,
        reuse_previous_localization: bool=False,
    ) -> None:
        self._require_editor_mode()

        def commit_result(measurement: ImageMeasurement) -> None:
            try:
                self.commit_measurement(
                    section_key,measurement,
                    reuse_previous_localization=reuse_previous_localization,
                )
            except Exception as error:
                self._callbacks.on_measurement_error(section_key,error)

        self.request_measurement(
            section_key,
            source,
            metadata=self.feedback_measurement_metadata(section_key),
            on_result=commit_result,
            on_error=lambda _error:None,
        )

    def feedback_measurement_metadata(self,section_key: str) -> dict[str,Any]:
        status = self._application_session.runtime.get_section_cgh_status(section_key)
        return {
            "slm_key":self._application_session.runtime.identity.key,
            "section_key":section_key,
            "cgh_state":status.result_state.value,
            "cgh_generation":status.result_generation,
            "target_type":status.target_type,
        }

    def cancel_all(self) -> None:
        for section_key in tuple(self._measurement_requests):
            self.cancel_measurement(section_key)
