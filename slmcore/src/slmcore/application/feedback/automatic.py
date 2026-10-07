"""Automatic multi-round feedback sequencing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any,Callable,TYPE_CHECKING

from ...core.cgh.execution.status import CGHResultState
from ...core.measurement import ImageMeasurement
from .contracts import AutomaticFeedbackState


AUTOMATIC_MAX_MISSING_FRACTION = 0.10

if TYPE_CHECKING:
    from ...core.engine.runtime import SLMRuntime
    from .contracts import SLMFeedbackCallbacks


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

    def __init__(
        self,
        *,
        runtime_provider: Callable[[], "SLMRuntime"] | None=None,
        can_run: Callable[[],bool] | None=None,
        unavailable_reason: Callable[[],str] | None=None,
        is_cgh_computing: Callable[[str],bool] | None=None,
        compute_adapted_cgh: Callable[...,bool] | None=None,
        last_upload_error: Callable[[],Exception | None] | None=None,
        callbacks_provider: Callable[[], "SLMFeedbackCallbacks"] | None=None,
        request_measurement: Callable[...,None] | None=None,
        cancel_measurement: Callable[[str],None] | None=None,
        feedback_measurement_metadata: Callable[[str],dict[str,Any]] | None=None,
        commit_measurement: Callable[...,bool] | None=None,
        reuse_localization: Callable[...,Any] | None=None,
        localize_and_commit: Callable[[str],None] | None=None,
        apply_intensity_feedback: Callable[...,tuple[bool,Any]] | None=None,
        report_error: Callable[[str,Exception],None] | None=None,
        report_warning: Callable[[str,Any],None] | None=None,
    ) -> None:
        required = {
            "runtime_provider":runtime_provider,
            "can_run":can_run,
            "unavailable_reason":unavailable_reason,
            "is_cgh_computing":is_cgh_computing,
            "compute_adapted_cgh":compute_adapted_cgh,
            "last_upload_error":last_upload_error,
            "callbacks_provider":callbacks_provider,
            "request_measurement":request_measurement,
            "cancel_measurement":cancel_measurement,
            "feedback_measurement_metadata":feedback_measurement_metadata,
            "commit_measurement":commit_measurement,
            "reuse_localization":reuse_localization,
            "localize_and_commit":localize_and_commit,
            "apply_intensity_feedback":apply_intensity_feedback,
            "report_error":report_error,
            "report_warning":report_warning,
        }
        missing = [name for name,value in required.items() if value is None]
        if missing:
            raise TypeError(
                "AutomaticFeedbackRunner missing capabilities: "
                + ", ".join(missing)
            )
        self._runtime_provider = runtime_provider
        self._can_run = can_run
        self._unavailable_reason = unavailable_reason
        self._is_cgh_computing = is_cgh_computing
        self._compute_adapted_cgh = compute_adapted_cgh
        self._last_upload_error = last_upload_error
        self._callbacks_provider = callbacks_provider
        self._request_measurement = request_measurement
        self._cancel_measurement = cancel_measurement
        self._feedback_measurement_metadata = feedback_measurement_metadata
        self._commit_measurement = commit_measurement
        self._reuse_localization = reuse_localization
        self._localize_and_commit = localize_and_commit
        self._apply_intensity_feedback = apply_intensity_feedback
        self._report_error = report_error
        self._report_warning = report_warning
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
        if not self._can_run():
            self._report_error(
                "Automatic feedback unavailable",
                RuntimeError(self._unavailable_reason()),
            )
            return False
        rounds = int(rounds)
        if rounds <= 0:
            self._report_error(
                "Automatic feedback failed",ValueError("Rounds must be > 0"),
            )
            return False
        source = str(source or "").strip()
        if not source:
            self._report_error(
                "Automatic feedback failed",
                ValueError("Select a detector before starting automatic feedback."),
            )
            return False
        if self._is_cgh_computing(section_key):
            self._report_error(
                "Automatic feedback failed",
                RuntimeError("Wait for the current CGH computation to finish."),
            )
            return False
        runtime = self._runtime_provider()
        feedback_status = runtime.get_section_feedback_status(section_key)
        if bool(getattr(feedback_status,"position_stale",False)):
            self._report_error(
                "Automatic feedback failed",
                RuntimeError(
                    str(getattr(feedback_status,"position_stale_reason","") or
                        "Clear or replace the stale position correction before "
                        "starting automatic feedback.")
                ),
            )
            return False
        cgh_status = runtime.get_section_cgh_status(section_key)
        if cgh_status.result_state is not CGHResultState.CURRENT:
            self._report_error(
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
            self._cancel_measurement(run.section_key)
            self._finish(cancelled=True)
            return
        self._emit_state()

    def cancel_for_runtime_change(self) -> None:
        run = self._run
        if run is None:
            return
        self._cancel_measurement(run.section_key)
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
        self._request_measurement(
            run.section_key,
            run.source,
            metadata=self._feedback_measurement_metadata(run.section_key),
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
            previous_available = self._commit_measurement(
                run.section_key,measurement,reuse_previous_localization=False,
            )
            run.stage = "localizing"
            self._emit_state()
            if run.reuse_previous_localization:
                if not previous_available:
                    raise RuntimeError(
                        "Previous localization reuse was requested, but no "
                        "previous localization is available."
                    )
                try:
                    self._reuse_localization(
                        run.section_key,raise_errors=True,
                    )
                except Exception as error:
                    raise RuntimeError(
                        "Previous localization could not be reused. Automatic "
                        "feedback will not fall back to fresh localization when "
                        "reuse is enabled."
                    ) from error
            else:
                self._localize_and_commit(
                    run.section_key,
                    infer_missing_fraction=AUTOMATIC_MAX_MISSING_FRACTION,
                )

            if run.stop_requested:
                self._finish(cancelled=True)
                return

            run.stage = "adapting"
            self._emit_state()
            ok,_transition = self._apply_intensity_feedback(
                run.section_key,raise_errors=True,
            )
            if not ok:
                raise RuntimeError("Intensity adaptation was not applied")

            run.stage = "computing"
            self._emit_state()
            started = self._compute_adapted_cgh(
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
            if error is None or error is self._last_upload_error():
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
        self._report_error("Automatic feedback failed",error)
        self._finish(cancelled=False,failed=True)

    def _finish(self,*,cancelled: bool,failed: bool=False) -> None:
        run = self._run
        if run is None:
            return
        self._cancel_measurement(run.section_key)
        self._run = None
        self._emit_state()
        if failed:
            text = "Automatic feedback stopped after an error."
        elif cancelled:
            text = "Automatic feedback stopped."
        else:
            text = "Automatic feedback completed (%d round(s))." % run.completed_rounds
        self._callbacks_provider().on_automatic_finished(run.section_key,text)

    def _current(self,run_id: int) -> _AutomaticRun | None:
        run = self._run
        if run is None or run.run_id != int(run_id):
            return None
        return run

    def _emit_state(self) -> None:
        self._callbacks_provider().on_automatic_state_changed(self.state)

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
