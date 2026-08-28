"""Toolkit-independent feedback workflow orchestration.

The feedback service owns measurement/localization/adaptation state changes and
automatic intensity-feedback sequencing. Presentation adapters may flush pending
editor drafts before invoking it, collect destructive-operation confirmations,
and render the callbacks emitted after authoritative application changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any,Callable,Mapping,Protocol,Sequence,TYPE_CHECKING

from ..core.cgh.execution.status import CGHResultState
from ..core.cgh.localization.policy import suggest_localization_sources
from ..core.cgh.propagation import simulate_propagation_fft
from ..core.measurement import ImageMeasurement

if TYPE_CHECKING:
    from .session import SLMSession


def _noop(*_args,**_kwargs) -> None:
    return None


class MeasurementRequest(Protocol):
    """Minimal cancellable measurement request consumed by feedback workflows."""

    @property
    def active(self) -> bool: ...

    def cancel(self) -> None: ...


class MeasurementDispatcher(Protocol):
    """Host-neutral asynchronous measurement dispatch contract.

    Implementations decide the execution context of completion callbacks. A Qt
    adapter can therefore marshal callbacks onto the GUI thread without making
    the application service depend on Qt.
    """

    @property
    def available(self) -> bool: ...

    def available_sources(self,section_key: str) -> Sequence[str]: ...

    def preferred_source(
        self,section_key: str,available: Sequence[str],
    ) -> str | None: ...

    def acquire(
        self,
        section_key: str,
        source: str,
        *,
        metadata: Mapping[str,Any] | None,
        on_result: Callable[[ImageMeasurement],None],
        on_error: Callable[[Exception],None],
    ) -> MeasurementRequest: ...


@dataclass(frozen=True)
class AutomaticFeedbackState:
    """Immutable observable state of the automatic intensity-feedback loop."""

    active: bool=False
    section_key: str | None=None
    requested_rounds: int=0
    completed_rounds: int=0
    stop_requested: bool=False
    stage: str="idle"
    progress: str=""


@dataclass(frozen=True)
class FeedbackParameterUpdateResult:
    changed: bool
    candidate_analysis: Any=None


@dataclass(frozen=True)
class SLMFeedbackCallbacks:
    """Presentation-neutral events emitted by :class:`SLMFeedbackService`."""

    on_section_changed: Callable[[str],None]=_noop
    on_transition_committed: Callable[[str,Any],None]=_noop
    on_measurement_busy_changed: Callable[[str,bool,str],None]=_noop
    on_measurement_error: Callable[[str,Exception],None]=_noop
    on_localization_error: Callable[[str,Exception],None]=_noop
    on_automatic_state_changed: Callable[[AutomaticFeedbackState],None]=_noop
    on_automatic_finished: Callable[[str,str],None]=_noop
    on_warning: Callable[[str,Any],None]=_noop
    on_error: Callable[[str,Exception],None]=_noop


@dataclass
class _AutomaticRun:
    run_id: int
    section_key: str
    requested_rounds: int
    source: str
    reuse_previous_localization: bool
    completed_rounds: int=0
    stop_requested: bool=False
    stage: str="starting"


class AutomaticFeedbackRunner:
    """Sequence automatic intensity feedback using application services only."""

    def __init__(self,service: "SLMFeedbackService") -> None:
        self._service = service
        self._counter = 0
        self._run: _AutomaticRun | None = None

    @property
    def active(self) -> bool:
        return self._run is not None

    @property
    def section_key(self) -> str | None:
        return None if self._run is None else self._run.section_key

    @property
    def state(self) -> AutomaticFeedbackState:
        run = self._run
        if run is None:
            return AutomaticFeedbackState()
        return AutomaticFeedbackState(
            active=True,
            section_key=run.section_key,
            requested_rounds=run.requested_rounds,
            completed_rounds=run.completed_rounds,
            stop_requested=run.stop_requested,
            stage=run.stage,
            progress=self._progress(run),
        )

    def start(
        self,
        section_key: str,
        *,
        rounds: int,
        source: str,
        reuse_previous_localization: bool,
    ) -> bool:
        if self._run is not None:
            return False
        if not self._service.can_run_automatic_feedback:
            self._service._error(
                "Automatic feedback unavailable",
                RuntimeError(self._service.automatic_feedback_unavailable_reason),
            )
            return False
        rounds = int(rounds)
        if rounds <= 0:
            self._service._error(
                "Automatic feedback failed",ValueError("Rounds must be > 0"),
            )
            return False
        source = str(source or "").strip()
        if not source:
            self._service._error(
                "Automatic feedback failed",
                ValueError("Select a detector before starting automatic feedback."),
            )
            return False
        if self._service.session.is_cgh_computing(section_key):
            self._service._error(
                "Automatic feedback failed",
                RuntimeError("Wait for the current CGH computation to finish."),
            )
            return False
        cgh_status = self._service.session.runtime.get_section_cgh_status(section_key)
        if cgh_status.result_state is not CGHResultState.CURRENT:
            self._service._error(
                "Automatic feedback failed",
                RuntimeError("Automatic feedback requires a current computed CGH."),
            )
            return False

        self._counter += 1
        self._run = _AutomaticRun(
            run_id=self._counter,
            section_key=section_key,
            requested_rounds=rounds,
            source=source,
            reuse_previous_localization=bool(reuse_previous_localization),
        )
        self._emit_state()
        self._start_next_round(self._run.run_id)
        return True

    def stop(self) -> None:
        run = self._run
        if run is None:
            return
        run.stop_requested = True
        if run.stage == "acquiring":
            self._service.cancel_measurement(run.section_key)
            self._finish(cancelled=True)
            return
        self._emit_state()

    def cancel_for_runtime_change(self) -> None:
        run = self._run
        if run is None:
            return
        self._service.cancel_measurement(run.section_key)
        self._run = None
        self._emit_state()

    def _start_next_round(self,run_id: int) -> None:
        run = self._current(run_id)
        if run is None:
            return
        if run.stop_requested:
            self._finish(cancelled=True)
            return
        if run.completed_rounds >= run.requested_rounds:
            self._finish(cancelled=False)
            return

        run.stage = "acquiring"
        self._emit_state()
        self._service.request_measurement(
            run.section_key,
            run.source,
            metadata=self._service.feedback_measurement_metadata(run.section_key),
            on_result=lambda measurement,rid=run_id:self._on_measurement(rid,measurement),
            on_error=lambda error,rid=run_id:self._fail(rid,error),
        )

    def _on_measurement(self,run_id: int,measurement: ImageMeasurement) -> None:
        run = self._current(run_id)
        if run is None:
            return
        if run.stop_requested:
            self._finish(cancelled=True)
            return
        try:
            previous_available = self._service.commit_measurement(
                run.section_key,measurement,reuse_previous_localization=False,
            )
            run.stage = "localizing"
            self._emit_state()
            localized = False
            if run.reuse_previous_localization and previous_available:
                try:
                    self._service.reuse_localization(
                        run.section_key,raise_errors=True,
                    )
                    localized = True
                except Exception as error:
                    self._service._warning(
                        "Automatic feedback",
                        "Previous localization could not be reused; running "
                        "localization instead.\n%s" % error,
                    )
            if not localized:
                self._service.localize_and_commit(run.section_key)

            if run.stop_requested:
                self._finish(cancelled=True)
                return

            run.stage = "adapting"
            self._emit_state()
            ok,_transition = self._service.apply_intensity_feedback(
                run.section_key,raise_errors=True,
            )
            if not ok:
                raise RuntimeError("Intensity adaptation was not applied")

            run.stage = "computing"
            self._emit_state()
            started = self._service.session.compute_adapted_cgh(
                run.section_key,
                on_finished=lambda success,error,rid=run_id:
                    self._on_cgh_finished(rid,success,error),
            )
            if not started and self._current(run_id) is not None:
                raise RuntimeError("Adapted CGH computation did not start")
        except Exception as error:
            self._fail(run_id,error)

    def _on_cgh_finished(
        self,run_id: int,success: bool,error: Exception | None,
    ) -> None:
        run = self._current(run_id)
        if run is None:
            return
        if not success:
            if error is None or error is self._service.session.last_upload_error:
                self._finish(cancelled=False,failed=True)
            else:
                self._fail(run_id,error)
            return
        run.completed_rounds += 1
        if run.stop_requested:
            self._finish(cancelled=True)
            return
        self._start_next_round(run_id)

    def _fail(self,run_id: int,error: Exception) -> None:
        if self._current(run_id) is None:
            return
        self._service._error("Automatic feedback failed",error)
        self._finish(cancelled=False,failed=True)

    def _finish(self,*,cancelled: bool,failed: bool=False) -> None:
        run = self._run
        if run is None:
            return
        self._service.cancel_measurement(run.section_key)
        self._run = None
        self._emit_state()
        if failed:
            text = "Automatic feedback stopped after an error."
        elif cancelled:
            text = "Automatic feedback stopped."
        else:
            text = "Automatic feedback completed (%d round(s))." % run.completed_rounds
        self._service._callbacks.on_automatic_finished(run.section_key,text)

    def _current(self,run_id: int) -> _AutomaticRun | None:
        run = self._run
        if run is None or run.run_id != int(run_id):
            return None
        return run

    def _emit_state(self) -> None:
        self._service._callbacks.on_automatic_state_changed(self.state)

    @staticmethod
    def _progress(run: _AutomaticRun) -> str:
        current = min(run.completed_rounds + 1,run.requested_rounds)
        if run.stop_requested and run.stage != "acquiring":
            return "Stopping after current operation..."
        labels = {
            "starting":"Automatic feedback starting...",
            "acquiring":"Round %d/%d · acquiring measurement..." % (
                current,run.requested_rounds,
            ),
            "localizing":"Round %d/%d · localizing..." % (
                current,run.requested_rounds,
            ),
            "adapting":"Round %d/%d · applying feedback..." % (
                current,run.requested_rounds,
            ),
            "computing":"Round %d/%d · computing adapted CGH..." % (
                current,run.requested_rounds,
            ),
        }
        return labels.get(run.stage,"Automatic feedback running...")


class SLMFeedbackService:
    """Application service for one session's measurement/feedback workflow."""

    def __init__(
        self,
        session: "SLMSession",
        *,
        measurements: MeasurementDispatcher | None=None,
        callbacks: SLMFeedbackCallbacks | None=None,
    ) -> None:
        self.session = session
        self.measurements = measurements
        self._callbacks = callbacks or SLMFeedbackCallbacks()
        self._measurement_requests: dict[str,MeasurementRequest] = {}
        self._automatic = AutomaticFeedbackRunner(self)

    def set_callbacks(self,callbacks: SLMFeedbackCallbacks | None) -> None:
        self._callbacks = callbacks or SLMFeedbackCallbacks()

    def set_measurement_dispatcher(
        self,measurements: MeasurementDispatcher | None,
    ) -> None:
        if measurements is self.measurements:
            return
        self.prepare_runtime_change()
        self.measurements = measurements

    @property
    def automatic_operation_active(self) -> bool:
        return self._automatic.active

    @property
    def automatic_state(self) -> AutomaticFeedbackState:
        return self._automatic.state

    @property
    def can_run_automatic_feedback(self) -> bool:
        measurements = self.measurements
        return bool(
            self.session.editor_writes_allowed
            and self.session.auto_upload_frame
            and self.session.host_services.can_upload_frame
            and measurements is not None
            and measurements.available
            and not self.session.upload_deferred
        )

    @property
    def automatic_feedback_unavailable_reason(self) -> str:
        if not self.session.editor_writes_allowed:
            return "Automatic feedback is unavailable in Fast Config mode."
        if not self.session.auto_upload_frame:
            return "Automatic feedback is disabled when auto_upload_frame=False."
        if not self.session.host_services.can_upload_frame:
            return "Automatic feedback requires a frame-upload capability."
        if self.measurements is None or not self.measurements.available:
            return "Automatic feedback requires a measurement provider."
        if self.session.upload_deferred:
            return "Automatic feedback is unavailable while frame upload is deferred."
        return ""

    def available_sources(self,section_key: str) -> Sequence[str]:
        measurements = self.measurements
        if measurements is None:
            return ()
        return tuple(measurements.available_sources(section_key))

    def preferred_source(
        self,section_key: str,available: Sequence[str],
    ) -> str | None:
        measurements = self.measurements
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
        self._require_editor_mode()
        self.cancel_measurement(section_key)
        self._callbacks.on_measurement_busy_changed(
            section_key,True,"Waiting for %s..." % source,
        )
        measurements = self.measurements
        if measurements is None:
            error = RuntimeError("No host measurement provider is configured.")
            self._callbacks.on_measurement_busy_changed(section_key,False,"")
            self._callbacks.on_measurement_error(section_key,error)
            on_error(error)
            return

        def result_callback(measurement):
            self._measurement_requests.pop(section_key,None)
            self._callbacks.on_measurement_busy_changed(section_key,False,"")
            if not self.session.editor_writes_allowed:
                return
            on_result(measurement)

        def error_callback(error):
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

        def commit_result(measurement):
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

    def commit_measurement(
        self,
        section_key: str,
        measurement: ImageMeasurement,
        *,
        reuse_previous_localization: bool=False,
    ) -> bool:
        self._require_editor_mode()
        runtime = self.session.runtime
        previous_available = bool(
            runtime.get_section_feedback_status(
                section_key
            ).previous_localization_available
        )
        runtime.set_section_feedback_measurement(section_key,measurement)
        cgh_status = runtime.get_section_cgh_status(section_key)
        target_hints_allowed = cgh_status.result_state is CGHResultState.CURRENT
        context = (
            runtime.get_section_feedback_localization_context(section_key)
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
                self.reuse_localization(section_key,raise_errors=True)
            except Exception as error:
                self._warning(
                    "Feedback localization",
                    "Previous localization could not be reused: %s" % error,
                )
        self._section_changed(section_key)
        return previous_available

    def localization_candidate(
        self,section_key: str,parameters: Mapping[str,Any],
    ):
        self.validate_target_hint_use(section_key,parameters)
        runtime = self.session.runtime
        candidate = runtime.compute_section_feedback_localization_candidate(
            section_key,parameters,
        )
        metrics = None
        try:
            metrics = runtime.compute_section_feedback_intensity_analysis(
                section_key,candidate,
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
        localization: Any,
        parameters: Mapping[str,Any],
        *,
        raise_errors: bool=False,
    ) -> bool:
        try:
            self._require_editor_mode()
            self.require_current_cgh_for_localization_commit(section_key)
            self.validate_target_hint_use(section_key,parameters)
            runtime = self.session.runtime
            runtime.commit_section_feedback_localization(
                section_key,localization,parameters,
            )
            self._update_committed_analysis(section_key,localization)
            self._section_changed(section_key)
            return True
        except Exception as error:
            if raise_errors:
                raise
            self._callbacks.on_localization_error(section_key,error)
            return False

    def reuse_localization(
        self,section_key: str,*,raise_errors: bool=False,
    ):
        try:
            self._require_editor_mode()
            self.require_current_cgh_for_localization_commit(section_key)
            localization = self.session.runtime.reuse_section_feedback_localization(
                section_key
            )
            self._update_committed_analysis(section_key,localization)
            self._section_changed(section_key)
            return localization
        except Exception as error:
            if raise_errors:
                raise
            self._callbacks.on_localization_error(section_key,error)
            return None

    def localize_and_commit(self,section_key: str) -> None:
        status = self.session.runtime.get_section_feedback_status(section_key)
        parameters = dict(status.localization_params)
        candidate,_metrics = self.localization_candidate(section_key,parameters)
        self.accept_localization(
            section_key,candidate,parameters,raise_errors=True,
        )

    def update_parameters(
        self,
        section_key: str,
        group: str,
        changes: Mapping[str,Any],
        *,
        localization: Any=None,
    ) -> FeedbackParameterUpdateResult:
        self._require_editor_mode()
        runtime = self.session.runtime
        changed = runtime.update_section_feedback_parameters(
            section_key,group,dict(changes or {}),
        )
        candidate_analysis = None
        if changed and str(group) in ("intensity","intensity_analysis"):
            if localization is not None:
                try:
                    candidate_analysis = runtime.compute_section_feedback_intensity_analysis(
                        section_key,localization,
                    )
                except Exception as error:
                    self._warning(
                        "Intensity analysis",
                        "Candidate intensity analysis is unavailable: %s" % error,
                    )
            elif runtime.get_section_feedback_status(
                section_key
            ).localization_available:
                self._update_committed_analysis(section_key)
        self._section_changed(section_key)
        return FeedbackParameterUpdateResult(
            changed=bool(changed),candidate_analysis=candidate_analysis,
        )

    def apply_intensity_feedback(
        self,section_key: str,*,raise_errors: bool=False,
    ):
        return self._apply_resolution_operation(
            section_key,
            lambda runtime:runtime.apply_section_intensity_feedback(section_key),
            "Applying intensity feedback failed",
            raise_errors=raise_errors,
        )

    def reset_intensity_feedback(self,section_key: str):
        return self._apply_resolution_operation(
            section_key,
            lambda runtime:runtime.reset_section_intensity_feedback(section_key),
            "Resetting intensity feedback failed",
        )

    def apply_position_correction(
        self,section_key: str,*,reset_intensity: bool=False,
    ):
        return self._apply_resolution_operation(
            section_key,
            lambda runtime:runtime.apply_section_position_correction(
                section_key,reset_intensity=bool(reset_intensity),
            ),
            "Applying position correction failed",
        )

    def set_position_active(
        self,section_key: str,active: bool,*,reset_intensity: bool=False,
    ):
        return self._apply_resolution_operation(
            section_key,
            lambda runtime:runtime.set_section_position_correction_active(
                section_key,bool(active),reset_intensity=bool(reset_intensity),
            ),
            "Changing position correction failed",
        )

    def clear_position_correction(
        self,section_key: str,*,reset_intensity: bool=False,
    ):
        return self._apply_resolution_operation(
            section_key,
            lambda runtime:runtime.clear_section_position_correction(
                section_key,reset_intensity=bool(reset_intensity),
            ),
            "Clearing position correction failed",
        )

    def reset_to_round(self,section_key: str,round_index: int):
        self._require_editor_mode()
        runtime = self.session.runtime
        inspection = runtime.get_section_cgh_session_inspection(section_key)
        rounds = {item.index:item for item in inspection.rounds}
        if int(round_index) not in rounds:
            raise ValueError("Only computed rounds can be restored.")
        self.session.cancel_cgh(section_key)
        transition = runtime.reset_section_cgh_to_round(section_key,int(round_index))
        if transition is not None:
            self._callbacks.on_transition_committed(section_key,transition)
        else:
            self._section_changed(section_key)
        return transition

    def propagate_round(
        self,
        section_key: str,
        round_index: int,
        *,
        position_context: str="corrected",
        pad_size: Any=1024,
    ):
        pad_size = int(pad_size)
        if pad_size <= 0:
            raise ValueError("CGH propagation pad size must be > 0")
        inspection = self.session.runtime.get_section_cgh_session_inspection(section_key)
        if str(position_context) == "not_corrected":
            selected = inspection.position_reference_round
        elif str(position_context) == "corrected":
            selected = next(
                (item for item in inspection.rounds if item.index == int(round_index)),
                None,
            )
        else:
            raise ValueError(
                "Unknown position history context: %r" % position_context
            )
        if selected is None or selected.result is None:
            raise RuntimeError("The selected round has no computed CGH result")
        return simulate_propagation_fft(
            selected.result.pattern,padding=True,pad_size=pad_size,
        )

    def feedback_measurement_metadata(self,section_key: str) -> dict[str,Any]:
        status = self.session.runtime.get_section_cgh_status(section_key)
        return {
            "slm_key":self.session.runtime.identity.key,
            "section_key":section_key,
            "cgh_state":status.result_state.value,
            "cgh_generation":status.result_generation,
            "target_type":status.target_type,
        }

    def localization_context(self,section_key: str) -> Mapping[str,Any]:
        runtime = self.session.runtime
        status = runtime.get_section_cgh_status(section_key)
        if status.result_state is not CGHResultState.CURRENT:
            return {}
        return runtime.get_section_feedback_localization_context(section_key)

    def require_current_cgh_for_localization_commit(self,section_key: str) -> None:
        status = self.session.runtime.get_section_cgh_status(section_key)
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
        status = self.session.runtime.get_section_cgh_status(section_key)
        if status.result_state is not CGHResultState.CURRENT:
            raise RuntimeError(
                "Target localization guidance requires a current CGH result. "
                "Use automatic/manual localization or recompute the CGH first."
            )

    def start_automatic_feedback(
        self,
        section_key: str,
        *,
        rounds: int,
        source: str,
        reuse_previous_localization: bool=False,
    ) -> bool:
        self._require_editor_mode()
        return self._automatic.start(
            section_key,
            rounds=rounds,
            source=source,
            reuse_previous_localization=reuse_previous_localization,
        )

    def stop_automatic_feedback(self) -> None:
        self._automatic.stop()

    def prepare_runtime_change(self) -> None:
        self._automatic.cancel_for_runtime_change()
        for section_key in tuple(self._measurement_requests):
            self.cancel_measurement(section_key)

    def dispose(self) -> None:
        self.prepare_runtime_change()

    def _update_committed_analysis(
        self,section_key: str,localization: Any=None,
    ) -> None:
        try:
            runtime = self.session.runtime
            analysis = runtime.compute_section_feedback_intensity_analysis(
                section_key,localization,
            )
            runtime.set_section_feedback_intensity_analysis(section_key,analysis)
        except Exception as error:
            self._warning(
                "Intensity analysis",
                "Measurement metrics are unavailable: %s" % error,
            )

    def _apply_resolution_operation(
        self,
        section_key: str,
        operation,
        error_title: str,
        *,
        raise_errors: bool=False,
    ):
        if not self.session.editor_writes_allowed:
            if raise_errors:
                raise RuntimeError(
                    "Feedback changes are unavailable in Fast Config mode"
                )
            return False,None
        self.session.cancel_cgh(section_key)
        try:
            transition = operation(self.session.runtime)
            if transition is not None:
                self._callbacks.on_transition_committed(section_key,transition)
            else:
                self._section_changed(section_key)
            return True,transition
        except Exception as error:
            self._section_changed(section_key)
            if raise_errors:
                raise
            self._error(error_title,error)
            return False,None

    def _section_changed(self,section_key: str) -> None:
        self._callbacks.on_section_changed(section_key)

    def _warning(self,title: str,message: Any) -> None:
        self._callbacks.on_warning(str(title),message)

    def _error(self,title: str,error: Exception) -> None:
        if not isinstance(error,Exception):
            error = RuntimeError(str(error))
        self._callbacks.on_error(str(title),error)

    def _require_editor_mode(self) -> None:
        if not self.session.editor_writes_allowed:
            raise RuntimeError("Operation unavailable in Fast Config mode")
