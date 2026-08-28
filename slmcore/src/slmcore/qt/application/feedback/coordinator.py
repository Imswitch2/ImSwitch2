from __future__ import annotations

from typing import Any,Mapping,Sequence

import numpy as np
from PIL import Image
from qtpy import QtCore,QtWidgets

from ....application.feedback import AutomaticFeedbackState
from ....measurement import ImageMeasurement,create_image_measurement
from ...cgh.session_window import CGHSessionWindow,MeasurementsAction


class FeedbackCoordinator(QtCore.QObject):
    """Qt presentation adapter for the application-owned feedback service."""

    def __init__(self,controller) -> None:
        super().__init__(controller)
        self.controller = controller
        self.service = controller.feedback_service
        self._windows: dict[str,CGHSessionWindow] = {}

    @property
    def automatic_operation_active(self) -> bool:
        return self.service.automatic_operation_active

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
        return self.service.available_sources(section_key)

    def preferred_source(
        self,section_key: str,available: Sequence[str],
    ) -> str | None:
        return self.service.preferred_source(section_key,available)

    def request_measurement(
        self,
        section_key: str,
        source: str,
        *,
        metadata: Mapping[str,Any] | None,
        on_result,
        on_error,
    ) -> None:
        self.service.request_measurement(
            section_key,source,metadata=metadata,
            on_result=on_result,on_error=on_error,
        )

    def cancel_measurement(self,section_key: str) -> None:
        self.service.cancel_measurement(section_key)

    def acquire(
        self,section_key: str,source: str,*,reuse_previous_localization: bool,
    ) -> None:
        if not self.controller.editor_writes_allowed:
            return
        try:
            self.controller.flush_section(section_key,propagate=True)
            self.service.acquire(
                section_key,source,
                reuse_previous_localization=reuse_previous_localization,
            )
        except Exception as error:
            self._set_measurement_error(section_key,error)

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
            self.service.commit_measurement(
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
        return self.service.commit_measurement(
            section_key,measurement,
            reuse_previous_localization=reuse_previous_localization,
        )

    def localization_candidate(
        self,section_key: str,parameters: Mapping[str,Any],
    ):
        return self.service.localization_candidate(section_key,parameters)

    def run_localization_candidate(
        self,section_key: str,parameters: Mapping[str,Any],
    ) -> None:
        try:
            candidate,metrics = self.service.localization_candidate(
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
        self.service.accept_localization(
            section_key,localization,parameters,raise_errors=raise_errors,
        )

    def reuse_localization(
        self,section_key: str,*,raise_errors: bool=False,
    ):
        return self.service.reuse_localization(
            section_key,raise_errors=raise_errors,
        )

    def localize_and_commit(self,section_key: str) -> None:
        self.service.localize_and_commit(section_key)

    def update_parameters(
        self,
        section_key: str,
        group: str,
        changes: Mapping[str,Any],
        *,
        localization: Any=None,
        localization_parameters: Mapping[str,Any] | None=None,
    ) -> None:
        try:
            result = self.service.update_parameters(
                section_key,group,changes,localization=localization,
            )
            window = self._windows.get(section_key)
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
            self._error("Feedback parameter update failed",error)
            self.controller.synchronize_section(section_key)

    def apply_intensity_feedback(
        self,section_key: str,*,raise_errors: bool=False,
    ):
        return self._run_resolution(
            section_key,
            lambda:self.service.apply_intensity_feedback(
                section_key,raise_errors=raise_errors,
            ),
            raise_errors=raise_errors,
        )

    def reset_intensity_feedback(self,section_key: str) -> None:
        self._run_resolution(
            section_key,lambda:self.service.reset_intensity_feedback(section_key),
        )

    def apply_position_correction(self,section_key: str) -> None:
        if not self.controller.editor_writes_allowed:
            return
        reset = self._confirm_position_intensity_reset(section_key)
        if reset is None:
            return
        self._run_resolution(
            section_key,
            lambda:self.service.apply_position_correction(
                section_key,reset_intensity=reset,
            ),
        )

    def set_position_active(self,section_key: str,active: bool) -> None:
        if not self.controller.editor_writes_allowed:
            return
        reset = self._confirm_position_intensity_reset(section_key)
        if reset is None:
            return
        self._run_resolution(
            section_key,
            lambda:self.service.set_position_active(
                section_key,bool(active),reset_intensity=reset,
            ),
        )

    def clear_position_correction(self,section_key: str) -> None:
        if not self.controller.editor_writes_allowed:
            return
        reset = self._confirm_position_intensity_reset(section_key)
        if reset is None:
            return
        self._run_resolution(
            section_key,
            lambda:self.service.clear_position_correction(
                section_key,reset_intensity=reset,
            ),
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
        try:
            self.service.reset_to_round(section_key,int(round_index))
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
            intensity = self.service.propagate_round(
                section_key,int(round_index),
                position_context=position_context,pad_size=pad_size,
            )
            if window is not None:
                window.set_propagation_result(int(round_index),intensity)
        except Exception as error:
            if window is not None:
                window.set_propagation_error(error)
            else:
                self._error("CGH propagation failed",error)

    def feedback_measurement_metadata(self,section_key: str) -> dict[str,Any]:
        return self.service.feedback_measurement_metadata(section_key)

    def localization_context(self,section_key: str) -> Mapping[str,Any]:
        return self.service.localization_context(section_key)

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
        self.service.require_current_cgh_for_localization_commit(section_key)

    def validate_target_hint_use(
        self,section_key: str,parameters: Mapping[str,Any],
    ) -> None:
        self.service.validate_target_hint_use(section_key,parameters)

    def _run_resolution(
        self,section_key: str,operation,*,raise_errors: bool=False,
    ):
        if not self.controller.editor_writes_allowed:
            if raise_errors:
                raise RuntimeError(
                    "Feedback changes are unavailable in Fast Config mode"
                )
            return False,None
        try:
            self.controller.flush_section(section_key,propagate=True)
            return operation()
        except Exception as error:
            if raise_errors:
                raise
            self._error("Feedback operation failed",error)
            self.controller.synchronize_section(section_key)
            return False,None

    def _confirm_position_intensity_reset(self,section_key: str):
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
            self.service.stop_automatic_feedback()
            return
        if self.service.automatic_operation_active:
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
            try:
                self.controller.flush_section(section_key,propagate=True)
                self.service.start_automatic_feedback(
                    section_key,
                    rounds=int(values.get("rounds",1)),
                    source=str(values.get("detector") or ""),
                    reuse_previous_localization=bool(
                        values.get("reuse_previous_localization",False)
                    ),
                )
            except Exception as error:
                self._error("Automatic feedback failed",error)

    def _configure_automatic_availability(self,window: CGHSessionWindow) -> None:
        window.set_automatic_feedback_available(
            self.service.can_run_automatic_feedback,
            self.service.automatic_feedback_unavailable_reason,
        )

    def on_automatic_state_changed(self,state: AutomaticFeedbackState) -> None:
        self.controller._set_section_interaction_locked(bool(state.active))
        for section_key,window in tuple(self._windows.items()):
            self._apply_automatic_state_to_window(section_key,window,state=state)
        self.controller.sigAutomaticOperationChanged.emit(bool(state.active))

    def on_automatic_finished(self,section_key: str,message: str) -> None:
        window = self._windows.get(section_key)
        if window is not None:
            window.measurement_view.set_measurement_status(str(message))

    def on_measurement_busy_changed(
        self,section_key: str,busy: bool,message: str,
    ) -> None:
        window = self._windows.get(section_key)
        if window is None:
            return
        window.set_measurement_busy(bool(busy),str(message or ""))
        self._apply_automatic_state_to_window(section_key,window)

    def on_measurement_error(self,section_key: str,error: Exception) -> None:
        self._set_measurement_error(section_key,error)

    def on_localization_error(self,section_key: str,error: Exception) -> None:
        self._set_localization_error(section_key,error)

    def _apply_automatic_state_to_window(
        self,
        section_key: str,
        window: CGHSessionWindow,
        *,
        state: AutomaticFeedbackState | None=None,
    ) -> None:
        state = self.service.automatic_state if state is None else state
        if not state.active:
            window.set_automatic_operation_state(False)
            return
        window.set_automatic_operation_state(
            True,
            owner=section_key == state.section_key,
            stopping=state.stop_requested,
            progress=state.progress,
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

    def refresh_automatic_availability(self) -> None:
        for section_key,window in tuple(self._windows.items()):
            self._configure_automatic_availability(window)
            self._apply_automatic_state_to_window(section_key,window)

    def prepare_runtime_replacement(self) -> None:
        self.close_windows()

    def runtime_replaced(self) -> None:
        pass

    def dispose(self) -> None:
        self.close_windows()
