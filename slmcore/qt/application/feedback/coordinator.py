from __future__ import annotations

from typing import Any,Mapping,Sequence

import numpy as np
from PIL import Image
from qtpy import QtCore,QtWidgets

from ....cgh.propagation import simulate_propagation_fft
from ....cgh.execution.status import CGHResultState
from ....cgh.localization.policy import suggest_localization_sources
from ....measurement import ImageMeasurement,create_image_measurement
from ...cgh.session_window import CGHSessionWindow,MeasurementsAction
from ..measurement_dispatcher import QtMeasurementDispatcher,QtMeasurementRequest
from .automatic import AutomaticFeedbackRunner


class FeedbackCoordinator(QtCore.QObject):
    """Reusable measurement/localization/feedback application workflow."""

    def __init__(
        self,controller,*,measurements: QtMeasurementDispatcher,
    ) -> None:
        super().__init__(controller)
        self.controller = controller
        self.measurements = measurements
        self._windows: dict[str, CGHSessionWindow] = {}
        self._measurement_requests: dict[str, QtMeasurementRequest] = {}
        self._automatic = AutomaticFeedbackRunner(self)

    @property
    def automatic_operation_active(self) -> bool:
        return self._automatic.active

    def window(self,section_key: str) -> CGHSessionWindow | None:
        return self._windows.get(section_key)

    def open_window(self,section_key: str) -> None:
        if not self.controller.editor_writes_allowed:
            return
        self.controller._require_section(section_key)
        try:
            self.controller.flush_section(section_key,propagate=True)
            window = self._windows.get(section_key)
            sources = self.available_sources(section_key)
            current = self.preferred_source(section_key,sources)
            if window is None:
                parent = self.controller.section_collection.section_view(
                    section_key
                ).window()
                window = CGHSessionWindow(
                    status=self.controller.runtime.get_section_feedback_status(
                        section_key
                    ),
                    inspection=(
                        self.controller.runtime.get_section_feedback_inspection(
                            section_key
                        )
                    ),
                    session_inspection=(
                        self.controller.runtime.get_section_cgh_session_inspection(
                            section_key
                        )
                    ),
                    cgh_status=self.controller.runtime.get_section_cgh_status(
                        section_key
                    ),
                    localization_context=self.localization_context(section_key),
                    detectors=sources,
                    current_detector=current,
                    cgh_summary=self.measurements_cgh_summary(section_key),
                    title="CGH Session - %s/%s" % (
                        self.controller.presenter.display_name,section_key,
                    ),
                    parent=parent,
                )
                self._windows[section_key] = window
                window.sigActionRequested.connect(
                    lambda action,options,key=section_key:
                        self._on_action(key,action,options)
                )
                window.destroyed.connect(
                    lambda _obj=None,key=section_key:
                        self._windows.pop(key,None)
                )
            else:
                window.configure_detectors(sources,current)
                self.synchronize_section(section_key)

            self._configure_automatic_availability(window)
            self._apply_automatic_state_to_window(section_key,window)
            window.set_cgh_computing(
                self.controller.is_cgh_computing(section_key)
            )
            window.show()
            window.raise_()
            window.activateWindow()
        except Exception as error:
            self._error("Measurements & Corrections failed",error)

    def close_windows(self) -> None:
        for window in tuple(self._windows.values()):
            window.close()
            window.deleteLater()
        self._windows.clear()

    def synchronize_section(self,section_key: str) -> None:
        window = self._windows.get(section_key)
        if window is None:
            return
        runtime = self.controller.runtime
        window.set_session_state(
            runtime.get_section_feedback_status(section_key),
            runtime.get_section_feedback_inspection(section_key),
            runtime.get_section_cgh_session_inspection(section_key),
            runtime.get_section_cgh_status(section_key),
            self.localization_context(section_key),
            self.measurements_cgh_summary(section_key),
        )
        self._configure_automatic_availability(window)
        self._apply_automatic_state_to_window(section_key,window)

    def set_cgh_computing(self,section_key: str,computing: bool) -> None:
        window = self._windows.get(section_key)
        if window is not None:
            window.set_cgh_computing(bool(computing))
            self._apply_automatic_state_to_window(section_key,window)

    def available_sources(self,section_key: str) -> Sequence[str]:
        return self.measurements.available_sources(section_key)

    def preferred_source(
        self,section_key: str,available: Sequence[str],
    ) -> str | None:
        return self.measurements.preferred_source(section_key,available)

    def request_measurement(
        self,
        section_key: str,
        source: str,
        *,
        metadata: Mapping[str, Any] | None,
        on_result,
        on_error,
    ) -> None:
        self.cancel_measurement(section_key)
        window = self._windows.get(section_key)
        if window is not None:
            window.set_measurement_busy(True,"Waiting for %s..." % source)

        def result_callback(measurement):
            self._measurement_requests.pop(section_key,None)
            current_window = self._windows.get(section_key)
            if current_window is not None:
                current_window.set_measurement_busy(False)
                self._apply_automatic_state_to_window(
                    section_key,current_window,
                )
            if not self.controller.editor_writes_allowed:
                return
            on_result(measurement)

        def error_callback(error):
            self._measurement_requests.pop(section_key,None)
            current_window = self._windows.get(section_key)
            if current_window is not None:
                current_window.set_measurement_error(error)
                self._apply_automatic_state_to_window(
                    section_key,current_window,
                )
            on_error(error)

        request = self.measurements.acquire(
            section_key,
            source,
            metadata=metadata,
            on_result=result_callback,
            on_error=error_callback,
        )
        # A provider may complete immediately. The dispatcher queues that
        # completion, and ``active`` prevents keeping an already-finished request.
        if request.active:
            self._measurement_requests[section_key] = request

    def cancel_measurement(self,section_key: str) -> None:
        request = self._measurement_requests.pop(section_key,None)
        if request is not None:
            request.cancel()
        window = self._windows.get(section_key)
        if window is not None:
            window.set_measurement_busy(False)

    def acquire(
        self,section_key: str,source: str,*,reuse_previous_localization: bool,
    ) -> None:
        if not self.controller.editor_writes_allowed:
            return
        try:
            self.controller.flush_section(section_key,propagate=True)
        except Exception as error:
            self._set_measurement_error(section_key,error)
            return
        def commit_result(measurement):
            try:
                self.commit_measurement(
                    section_key,measurement,
                    reuse_previous_localization=reuse_previous_localization,
                )
            except Exception as error:
                self._set_measurement_error(section_key,error)

        self.request_measurement(
            section_key,
            source,
            metadata=self.feedback_measurement_metadata(section_key),
            on_result=commit_result,
            on_error=lambda error:None,
        )

    def load(
        self,section_key: str,*,reuse_previous_localization: bool,
    ) -> None:
        if not self.controller.editor_writes_allowed:
            return
        try:
            self.controller.flush_section(section_key,propagate=True)
            parent = self._windows.get(section_key)
            path,_selected = QtWidgets.QFileDialog.getOpenFileName(
                parent,"Select feedback image","",
                "Images (*.tif *.tiff *.png *.jpg *.jpeg *.bmp);;All files (*)",
            )
            if not path:
                return
            image = np.asarray(Image.open(path).convert("F"),dtype=np.float64)
            measurement = create_image_measurement(
                image,source="file",metadata={"path":str(path)},
            )
            self.commit_measurement(
                section_key,measurement,
                reuse_previous_localization=reuse_previous_localization,
            )
        except Exception as error:
            self._set_measurement_error(section_key,error)

    def commit_measurement(
        self,
        section_key: str,
        measurement: ImageMeasurement,
        *,
        reuse_previous_localization: bool=False,
    ) -> bool:
        self._require_editor_mode()
        runtime = self.controller.runtime
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
        self.controller.synchronize_section(section_key)
        return previous_available

    def localization_candidate(
        self,section_key: str,parameters: Mapping[str,Any],
    ):
        self.validate_target_hint_use(section_key,parameters)
        runtime = self.controller.runtime
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

    def run_localization_candidate(
        self,section_key: str,parameters: Mapping[str,Any],
    ) -> None:
        try:
            candidate,metrics = self.localization_candidate(
                section_key,parameters,
            )
            window = self._windows.get(section_key)
            if window is not None:
                window.set_localization_result(
                    candidate,parameters,metrics=metrics,
                )
        except Exception as error:
            self._set_localization_error(section_key,error)

    def accept_localization(
        self,
        section_key: str,
        localization: Any,
        parameters: Mapping[str,Any],
        *,
        raise_errors: bool=False,
    ) -> None:
        try:
            self._require_editor_mode()
            self.require_current_cgh_for_localization_commit(section_key)
            self.validate_target_hint_use(section_key,parameters)
            runtime = self.controller.runtime
            runtime.commit_section_feedback_localization(
                section_key,localization,parameters,
            )
            self._update_committed_analysis(section_key,localization)
            self.controller.synchronize_section(section_key)
        except Exception as error:
            if raise_errors:
                raise
            self._set_localization_error(section_key,error)

    def reuse_localization(
        self,section_key: str,*,raise_errors: bool=False,
    ):
        try:
            self._require_editor_mode()
            self.require_current_cgh_for_localization_commit(section_key)
            localization = self.controller.runtime.reuse_section_feedback_localization(
                section_key
            )
            self._update_committed_analysis(section_key,localization)
            self.controller.synchronize_section(section_key)
            return localization
        except Exception as error:
            if raise_errors:
                raise
            self._set_localization_error(section_key,error)
            return None

    def localize_and_commit(self,section_key: str) -> None:
        status = self.controller.runtime.get_section_feedback_status(section_key)
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
        localization_parameters: Mapping[str, Any] | None=None,
    ) -> None:
        try:
            self._require_editor_mode()
            runtime = self.controller.runtime
            changed = runtime.update_section_feedback_parameters(
                section_key,group,dict(changes or {}),
            )
            candidate_analysis = None
            if changed and str(group) in ("intensity","intensity_analysis"):
                if localization is not None:
                    try:
                        candidate_analysis = (
                            runtime.compute_section_feedback_intensity_analysis(
                                section_key,localization,
                            )
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
            self.controller.synchronize_section(section_key)
            window = self._windows.get(section_key)
            if (
                window is not None
                and localization is not None
                and candidate_analysis is not None
            ):
                window.set_localization_result(
                    localization,dict(localization_parameters or {}),
                    metrics=candidate_analysis,
                )
        except Exception as error:
            self._error("Feedback parameter update failed",error)
            self.controller.synchronize_section(section_key)

    def apply_intensity_feedback(
        self,section_key: str,*,raise_errors: bool=False,
    ):
        return self._apply_resolution_operation(
            section_key,
            lambda runtime:runtime.apply_section_intensity_feedback(section_key),
            "Applying intensity feedback failed",
            raise_errors=raise_errors,
        )

    def reset_intensity_feedback(self,section_key: str) -> None:
        self._apply_resolution_operation(
            section_key,
            lambda runtime:runtime.reset_section_intensity_feedback(section_key),
            "Resetting intensity feedback failed",
        )

    def apply_position_correction(self,section_key: str) -> None:
        if not self.controller.editor_writes_allowed:
            return
        reset = self._confirm_position_intensity_reset(section_key)
        if reset is None:
            return
        self._apply_resolution_operation(
            section_key,
            lambda runtime:runtime.apply_section_position_correction(
                section_key,reset_intensity=reset,
            ),
            "Applying position correction failed",
        )

    def set_position_active(self,section_key: str,active: bool) -> None:
        if not self.controller.editor_writes_allowed:
            return
        reset = self._confirm_position_intensity_reset(section_key)
        if reset is None:
            return
        self._apply_resolution_operation(
            section_key,
            lambda runtime:runtime.set_section_position_correction_active(
                section_key,bool(active),reset_intensity=reset,
            ),
            "Changing position correction failed",
        )

    def clear_position_correction(self,section_key: str) -> None:
        if not self.controller.editor_writes_allowed:
            return
        reset = self._confirm_position_intensity_reset(section_key)
        if reset is None:
            return
        self._apply_resolution_operation(
            section_key,
            lambda runtime:runtime.clear_section_position_correction(
                section_key,reset_intensity=reset,
            ),
            "Clearing position correction failed",
        )

    def reset_to_round(self,section_key: str,round_index: int) -> None:
        if not self.controller.editor_writes_allowed:
            return
        runtime = self.controller.runtime
        inspection = runtime.get_section_cgh_session_inspection(section_key)
        rounds = {item.index:item for item in inspection.rounds}
        if int(round_index) not in rounds:
            self._warning("Reset CGH round","Only computed rounds can be restored.")
            return
        later = [
            item.index for item in inspection.rounds
            if item.index > int(round_index)
        ]
        if not later and inspection.working_round is None:
            return
        if not self._confirm_round_reset(
            section_key,int(round_index),len(later),
            inspection.working_round is not None,
        ):
            return
        self.controller.cancel_cgh(section_key)
        try:
            transition = runtime.reset_section_cgh_to_round(
                section_key,int(round_index),
            )
            if transition is not None:
                self.controller.apply_transition(section_key,transition)
            else:
                self.controller.synchronize_section(section_key)
        except Exception as error:
            self._error("Reset CGH round failed",error)

    def propagate_round(
        self,
        section_key: str,
        round_index: int,
        *,
        position_context: str="corrected",
        pad_size: Any=1024,
    ) -> None:
        window = self._windows.get(section_key)
        try:
            pad_size = int(pad_size)
            if pad_size <= 0:
                raise ValueError("CGH propagation pad size must be > 0")
            inspection = self.controller.runtime.get_section_cgh_session_inspection(
                section_key
            )
            if str(position_context) == "not_corrected":
                selected = inspection.position_reference_round
            elif str(position_context) == "corrected":
                selected = next(
                    (item for item in inspection.rounds
                     if item.index == int(round_index)),None,
                )
            else:
                raise ValueError(
                    "Unknown position history context: %r" % position_context
                )
            if selected is None or selected.result is None:
                raise RuntimeError("The selected round has no computed CGH result")
            intensity = simulate_propagation_fft(
                selected.result.pattern,padding=True,pad_size=pad_size,
            )
            if window is not None:
                window.set_propagation_result(int(round_index),intensity)
        except Exception as error:
            if window is not None:
                window.set_propagation_error(error)
            else:
                self._error("CGH propagation failed",error)

    def feedback_measurement_metadata(self,section_key: str) -> dict[str, Any]:
        status = self.controller.runtime.get_section_cgh_status(section_key)
        return {
            "slm_key":self.controller.runtime.identity.key,
            "section_key":section_key,
            "cgh_state":status.result_state.value,
            "cgh_generation":status.result_generation,
            "target_type":status.target_type,
        }

    def localization_context(self,section_key: str) -> Mapping[str,Any]:
        runtime = self.controller.runtime
        status = runtime.get_section_cgh_status(section_key)
        if status.result_state is not CGHResultState.CURRENT:
            return {}
        return runtime.get_section_feedback_localization_context(section_key)

    def measurements_cgh_summary(self,section_key: str) -> Mapping[str,Any]:
        runtime = self.controller.runtime
        inspection = runtime.get_section_cgh_session_inspection(section_key)
        target_state = None
        working = inspection.working_round
        if working is not None and working.target_state is not None:
            target_state = working.target_state
        elif inspection.committed_target is not None:
            target_state = inspection.committed_target

        state = runtime.get_section_state_copy(section_key).cgh
        if target_state is None:
            target_key = state.selected_target
            if target_key is None or target_key not in state.items:
                return {}
            target_params = state.items[target_key].params.values
        else:
            target_key = target_state.target_type
            target_params = target_state.canonical_params

        summary = dict(self.controller._target_presentation_summary(
            section_key,target_key,target_params,
        ))
        if state.selected_target in state.items:
            target = state.items[state.selected_target]
            summary.update({
                "algorithm":str(target.algorithm),
                "parameters":dict(target.computation.params.values),
            })
        return summary

    def require_current_cgh_for_localization_commit(self,section_key: str) -> None:
        status = self.controller.runtime.get_section_cgh_status(section_key)
        if status.result_state is CGHResultState.MISSING:
            raise RuntimeError(
                "No CGH has been computed yet. Compute the CGH before "
                "accepting feedback localization."
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
        status = self.controller.runtime.get_section_cgh_status(section_key)
        if status.result_state is not CGHResultState.CURRENT:
            raise RuntimeError(
                "Target localization guidance requires a current CGH result. "
                "Use automatic/manual localization or recompute the CGH first."
            )

    def _update_committed_analysis(
        self,section_key: str,localization: Any=None,
    ) -> None:
        self._require_editor_mode()
        try:
            runtime = self.controller.runtime
            analysis = runtime.compute_section_feedback_intensity_analysis(
                section_key,localization,
            )
            runtime.set_section_feedback_intensity_analysis(
                section_key,analysis,
            )
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
        if not self.controller.editor_writes_allowed:
            if raise_errors:
                raise RuntimeError(
                    "Feedback changes are unavailable in Fast Config mode"
                )
            return False,None
        self.controller.cancel_cgh(section_key)
        runtime = self.controller.runtime
        try:
            self.controller.flush_section(section_key,propagate=True)
            transition = operation(runtime)
            if transition is not None:
                self.controller.apply_transition(section_key,transition)
            else:
                self.controller.synchronize_section(section_key)
            return True,transition
        except Exception as error:
            self.controller._restore_section(section_key)
            self.controller.synchronize_section(section_key)
            if raise_errors:
                raise
            self._error(error_title,error)
            return False,None

    def _confirm_position_intensity_reset(
        self,section_key: str,
    ) -> bool | None:
        count = int(
            self.controller.runtime.get_section_feedback_status(
                section_key
            ).intensity_count
        )
        if count <= 0:
            return False
        parent = self._windows.get(section_key)
        dialog = QtWidgets.QMessageBox(parent)
        dialog.setIcon(QtWidgets.QMessageBox.Warning)
        dialog.setWindowTitle("Position correction")
        dialog.setText(
            "Changing spot positions resets the current CGH intensity "
            "sequence (%d feedback round(s))." % count
        )
        dialog.setInformativeText(
            "The previously applied hologram will remain displayed and be "
            "marked stale until a new Round 0 CGH is computed."
        )
        reset_button = dialog.addButton(
            "Reset intensity and apply",QtWidgets.QMessageBox.AcceptRole,
        )
        dialog.addButton(QtWidgets.QMessageBox.Cancel)
        dialog.setDefaultButton(reset_button)
        dialog.exec_() if hasattr(dialog,"exec_") else dialog.exec()
        return True if dialog.clickedButton() is reset_button else None

    def _confirm_round_reset(
        self,
        section_key: str,
        round_index: int,
        later_round_count: int,
        has_working_round: bool,
    ) -> bool:
        consequences = []
        if later_round_count:
            consequences.append("%d later computed round(s)" % later_round_count)
        if has_working_round:
            consequences.append("the current uncomputed working round")
        detail = " and ".join(consequences) or "later session state"
        parent = self._windows.get(section_key)
        result = QtWidgets.QMessageBox.question(
            parent,
            "Reset CGH round",
            "Reset the session to Round %d? This will permanently discard %s."
            % (round_index,detail),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        return result == QtWidgets.QMessageBox.Yes

    def _on_action(
        self,section_key: str,action: str,options: Mapping[str,Any],
    ) -> None:
        if not self.controller.editor_writes_allowed:
            return
        try:
            request = MeasurementsAction(str(action))
        except Exception:
            self._error(
                "Unknown measurements action",
                RuntimeError("Unsupported measurements action: %r" % action),
            )
            return
        values = dict(options or {})
        if request is MeasurementsAction.AUTOMATIC_STOP:
            self._automatic.stop()
            return
        if self._automatic.active:
            return

        if request is MeasurementsAction.ACQUIRE:
            self.acquire(
                section_key,str(values.get("detector") or ""),
                reuse_previous_localization=bool(
                    values.get("reuse_previous_localization",False)
                ),
            )
        elif request is MeasurementsAction.LOAD:
            self.load(
                section_key,
                reuse_previous_localization=bool(
                    values.get("reuse_previous_localization",False)
                ),
            )
        elif request is MeasurementsAction.LOCALIZATION_RUN:
            self.run_localization_candidate(
                section_key,values.get("parameters",{}),
            )
        elif request is MeasurementsAction.LOCALIZATION_ACCEPT:
            self.accept_localization(
                section_key,values.get("localization"),
                values.get("parameters",{}),
            )
        elif request is MeasurementsAction.LOCALIZATION_REUSE:
            self.reuse_localization(section_key)
        elif request is MeasurementsAction.FEEDBACK_PARAMETERS:
            self.update_parameters(
                section_key,str(values.get("group") or ""),
                values.get("changes",{}),
                localization=values.get("localization"),
                localization_parameters=values.get(
                    "localization_parameters",{}
                ),
            )
        elif request is MeasurementsAction.INTENSITY_APPLY:
            self.apply_intensity_feedback(section_key)
        elif request is MeasurementsAction.INTENSITY_RESET:
            self.reset_intensity_feedback(section_key)
        elif request is MeasurementsAction.POSITION_APPLY:
            self.apply_position_correction(section_key)
        elif request is MeasurementsAction.POSITION_SET_ACTIVE:
            self.set_position_active(
                section_key,bool(values.get("active",False)),
            )
        elif request is MeasurementsAction.POSITION_CLEAR:
            self.clear_position_correction(section_key)
        elif request is MeasurementsAction.COMPUTE_ADAPTED:
            self.controller.compute_adapted_cgh(section_key)
        elif request is MeasurementsAction.RESET_TO_ROUND:
            self.reset_to_round(section_key,int(values.get("round_index")))
        elif request is MeasurementsAction.PROPAGATE_SELECTED:
            self.propagate_round(
                section_key,
                int(values.get("round_index")),
                position_context=str(
                    values.get("position_context") or "corrected"
                ),
                pad_size=values.get("pad_size",1024),
            )
        elif request is MeasurementsAction.INSPECT:
            window = self._windows.get(section_key)
            if window is not None:
                window.show_inspection(
                    self.controller.runtime.get_section_feedback_inspection(
                        section_key
                    )
                )
        elif request is MeasurementsAction.AUTOMATIC_START:
            self._automatic.start(
                section_key,
                rounds=int(values.get("rounds",1)),
                source=str(values.get("detector") or ""),
                reuse_previous_localization=bool(
                    values.get("reuse_previous_localization",False)
                ),
            )

    def _configure_automatic_availability(self,window: CGHSessionWindow) -> None:
        available = self.controller.can_run_automatic_feedback
        reason = ""
        if not available:
            if not self.controller.auto_upload_frame:
                reason = (
                    "Automatic feedback is disabled when auto_upload_frame=False."
                )
            elif not self.controller.host_services.can_upload_frame:
                reason = "Automatic feedback requires a frame-upload capability."
            elif not self.measurements.available:
                reason = "Automatic feedback requires a measurement provider."
            elif self.controller._upload_defer_depth > 0:
                reason = "Automatic feedback is unavailable while frame upload is deferred."
        window.set_automatic_feedback_available(available,reason)

    def _set_automatic_operation(
        self,
        active: bool,
        *,
        owner_section: str | None=None,
        stopping: bool=False,
        progress: str="",
    ) -> None:
        self.controller._set_section_interaction_locked(bool(active))
        for section_key,window in tuple(self._windows.items()):
            window.set_automatic_operation_state(
                bool(active),
                owner=(bool(active) and section_key == owner_section),
                stopping=bool(stopping),
                progress=progress,
            )
        self.controller.sigAutomaticOperationChanged.emit(bool(active))

    def _apply_automatic_state_to_window(
        self,section_key: str,window: CGHSessionWindow,
    ) -> None:
        run = self._automatic._run
        if run is None:
            window.set_automatic_operation_state(False)
            return
        window.set_automatic_operation_state(
            True,
            owner=section_key == run.section_key,
            stopping=run.stop_requested,
            progress=(
                "Stopping after current operation..."
                if run.stop_requested else "Automatic feedback running..."
            ),
        )

    def _set_measurement_error(self,section_key: str,error: Exception) -> None:
        window = self._windows.get(section_key)
        if window is not None:
            window.set_measurement_error(error)
        else:
            self._error("Measurement failed",error)

    def _set_localization_error(self,section_key: str,error: Exception) -> None:
        window = self._windows.get(section_key)
        if window is not None:
            window.set_localization_error(error)
        else:
            self._error("Localization failed",error)

    def _warning(self,title: str,message: Any) -> None:
        self.controller.sigWarning.emit(str(title),message)

    def _error(self,title: str,error: Exception) -> None:
        self.controller._emit_exception(str(title),error)

    def _require_editor_mode(self) -> None:
        if not self.controller.editor_writes_allowed:
            raise RuntimeError("Operation unavailable in Fast Config mode")

    def refresh_automatic_availability(self) -> None:
        for section_key,window in tuple(self._windows.items()):
            self._configure_automatic_availability(window)
            self._apply_automatic_state_to_window(section_key,window)

    def prepare_runtime_replacement(self) -> None:
        self._automatic.cancel_for_runtime_change()
        for section_key in tuple(self._measurement_requests):
            self.cancel_measurement(section_key)
        self.close_windows()

    def runtime_replaced(self) -> None:
        pass

    def dispose(self) -> None:
        self.prepare_runtime_replacement()
