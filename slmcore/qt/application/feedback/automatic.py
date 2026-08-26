from __future__ import annotations

from dataclasses import dataclass


from ....cgh.execution.status import CGHResultState
from ....measurement import ImageMeasurement


@dataclass
class _AutomaticRun:
    run_id: int
    section_key: str
    requested_rounds: int
    source: str
    reuse_previous_localization: bool
    completed_rounds: int = 0
    stop_requested: bool = False
    stage: str = "starting"


class AutomaticFeedbackRunner:
    """Sequence automatic intensity feedback using normal coordinator operations."""

    def __init__(self,coordinator: "FeedbackCoordinator") -> None:
        self._coordinator = coordinator
        self._counter = 0
        self._run: _AutomaticRun | None = None

    @property
    def active(self) -> bool:
        return self._run is not None

    @property
    def section_key(self) -> str | None:
        return None if self._run is None else self._run.section_key

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
        if not self._coordinator.controller.can_run_automatic_feedback:
            self._coordinator._error(
                "Automatic feedback unavailable",
                RuntimeError(
                    "Automatic feedback requires a measurement provider and "
                    "automatic hardware frame upload."
                ),
            )
            return False
        rounds = int(rounds)
        if rounds <= 0:
            self._coordinator._error(
                "Automatic feedback failed",ValueError("Rounds must be > 0"),
            )
            return False
        source = str(source or "").strip()
        if not source:
            self._coordinator._error(
                "Automatic feedback failed",
                ValueError("Select a detector before starting automatic feedback."),
            )
            return False

        try:
            self._coordinator.controller.flush_section(
                section_key,propagate=True,
            )
        except Exception as error:
            self._coordinator._error("Automatic feedback failed",error)
            return False
        if self._coordinator.controller.is_cgh_computing(section_key):
            self._coordinator._error(
                "Automatic feedback failed",
                RuntimeError("Wait for the current CGH computation to finish."),
            )
            return False
        cgh_status = self._coordinator.controller.runtime.get_section_cgh_status(
            section_key
        )
        if cgh_status.result_state is not CGHResultState.CURRENT:
            self._coordinator._error(
                "Automatic feedback failed",
                RuntimeError(
                    "Automatic feedback requires a current computed CGH."
                ),
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
        self._coordinator._set_automatic_operation(
            True,owner_section=section_key,
            progress="Automatic feedback starting...",
        )
        self._start_next_round(self._run.run_id)
        return True

    def stop(self) -> None:
        run = self._run
        if run is None:
            return
        run.stop_requested = True
        if run.stage == "acquiring":
            self._coordinator.cancel_measurement(run.section_key)
            self._finish(cancelled=True)
            return
        self._coordinator._set_automatic_operation(
            True,
            owner_section=run.section_key,
            stopping=True,
            progress="Stopping after current operation...",
        )

    def cancel_for_runtime_change(self) -> None:
        run = self._run
        if run is None:
            return
        self._coordinator.cancel_measurement(run.section_key)
        self._run = None
        self._coordinator._set_automatic_operation(False)

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

        current = run.completed_rounds + 1
        run.stage = "acquiring"
        self._coordinator._set_automatic_operation(
            True,
            owner_section=run.section_key,
            progress="Round %d/%d · acquiring measurement..." % (
                current,run.requested_rounds,
            ),
        )
        self._coordinator.request_measurement(
            run.section_key,
            run.source,
            metadata=self._coordinator.feedback_measurement_metadata(
                run.section_key,
            ),
            on_result=lambda measurement,rid=run_id:
                self._on_measurement(rid,measurement),
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
            previous_available = self._coordinator.commit_measurement(
                run.section_key,measurement,
                reuse_previous_localization=False,
            )
            run.stage = "localizing"
            self._coordinator._set_automatic_operation(
                True,
                owner_section=run.section_key,
                progress="Round %d/%d · localizing..." % (
                    run.completed_rounds + 1,run.requested_rounds,
                ),
            )
            localized = False
            if run.reuse_previous_localization and previous_available:
                try:
                    self._coordinator.reuse_localization(
                        run.section_key,raise_errors=True,
                    )
                    localized = True
                except Exception as error:
                    self._coordinator._warning(
                        "Automatic feedback",
                        "Previous localization could not be reused; "
                        "running localization instead.\n%s" % error,
                    )
            if not localized:
                self._coordinator.localize_and_commit(run.section_key)

            if run.stop_requested:
                self._finish(cancelled=True)
                return

            run.stage = "adapting"
            ok,_transition = self._coordinator.apply_intensity_feedback(
                run.section_key,raise_errors=True,
            )
            if not ok:
                raise RuntimeError("Intensity adaptation was not applied")

            run.stage = "computing"
            self._coordinator._set_automatic_operation(
                True,
                owner_section=run.section_key,
                stopping=run.stop_requested,
                progress="Round %d/%d · computing adapted CGH..." % (
                    run.completed_rounds + 1,run.requested_rounds,
                ),
            )
            started = self._coordinator.controller.compute_adapted_cgh(
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
            if error is None or error is self._coordinator.controller.last_upload_error:
                # The reusable CGH controller already reported execution, UI,
                # or upload failures. Just stop the automatic sequence.
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
        self._coordinator._error("Automatic feedback failed",error)
        self._finish(cancelled=False,failed=True)

    def _finish(self,*,cancelled: bool,failed: bool=False) -> None:
        run = self._run
        if run is None:
            return
        self._coordinator.cancel_measurement(run.section_key)
        self._run = None
        self._coordinator._set_automatic_operation(False)
        window = self._coordinator.window(run.section_key)
        if window is not None:
            if failed:
                text = "Automatic feedback stopped after an error."
            elif cancelled:
                text = "Automatic feedback stopped."
            else:
                text = "Automatic feedback completed (%d round(s))." % (
                    run.completed_rounds,
                )
            window.measurement_view.set_measurement_status(text)

    def _current(self,run_id: int) -> _AutomaticRun | None:
        run = self._run
        if run is None or run.run_id != int(run_id):
            return None
        return run
