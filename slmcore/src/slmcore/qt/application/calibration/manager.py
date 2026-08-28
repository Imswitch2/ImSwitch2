from __future__ import annotations

from typing import Any,Mapping

import numpy as np
from PIL import Image
from qtpy import QtCore,QtWidgets

from ....application.calibration import TargetCalibrationState
from ....application.configuration import CalibrationMismatchPolicy
from ....cgh.localization.parameters import LOCALIZATION_PARAMS
from ....measurement import create_image_measurement
from ...calibration.dialog import CalibrationDialog,TARGET_LOCALIZATION_METHOD
from ...calibration.geometry_dialogs import (
    CalibrationMismatchDecision,calibration_mismatch_decision,
)
from ...calibration.plane_dialogs import (
    confirm_plane_deletion,request_plane_definition,
)


def _default_localization_parameters() -> dict[str,Any]:
    return {
        key:spec.validate(spec.default)
        for key,spec in LOCALIZATION_PARAMS.items()
    }


class CalibrationManager(QtCore.QObject):
    """Qt presentation adapter for the application calibration service."""

    def __init__(
        self,
        controller: Any,
        *,
        parent: QtCore.QObject | None=None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.service = controller.calibration_service
        self.display_name = str(controller.display_name)
        self._dialogs: dict[str,CalibrationDialog] = {}
        self._disposed = False
        load_error = self.service.last_store_load_error
        if load_error is not None:
            self.controller.sigWarning.emit(
                "SLM calibration",
                "Could not load plane definitions: %s" % load_error,
            )
        self._connect_collection()
        self.controller.sigAutomaticOperationChanged.connect(
            self._on_automatic_operation_changed,
        )
        self.refresh_planes()

    # ------------------------------------------------------------------
    # Plane catalog / selection presentation
    # ------------------------------------------------------------------

    def _connect_collection(self) -> None:
        collection = self.controller.section_collection
        collection.sigActivePlaneRequested.connect(self._on_active_plane_requested)
        collection.sigAddPlaneRequested.connect(self._on_add_plane_requested)
        collection.sigDeletePlaneRequested.connect(self._on_delete_plane_requested)
        collection.sigCalibrationRequested.connect(self.open_dialog)

    def _disconnect_collection(self) -> None:
        collection = self.controller.section_collection
        for signal,slot in (
            (collection.sigActivePlaneRequested,self._on_active_plane_requested),
            (collection.sigAddPlaneRequested,self._on_add_plane_requested),
            (collection.sigDeletePlaneRequested,self._on_delete_plane_requested),
            (collection.sigCalibrationRequested,self.open_dialog),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError,TypeError):
                pass

    def refresh_planes(self) -> None:
        names = self.service.plane_names
        for section_key in self.controller.runtime.section_keys:
            self.controller.section_collection.set_available_planes(
                section_key,names,self.service.active_plane(section_key),
            )

    @QtCore.Slot(str,str)
    def _on_active_plane_requested(
        self,section_key: str,plane_name: str,
    ) -> None:
        if (
            not self.controller.editor_writes_allowed
            or self.controller.automatic_operation_active
        ):
            return
        self.set_active_plane(section_key,str(plane_name or "").strip() or None)

    def set_active_plane(
        self,section_key: str,plane_name: str | None,
    ) -> bool:
        if not self.controller.editor_writes_allowed:
            return False
        previous_active = self.service.active_plane(section_key)
        try:
            self.controller.flush_section(section_key,propagate=True)
            prepared = self.service.prepare_plane_selection(section_key,plane_name)
            policy = CalibrationMismatchPolicy.REJECT
            if prepared.calibration_mismatches:
                decision = calibration_mismatch_decision(
                    self.controller.section_collection.section_view(section_key),
                    title="Use calibration with different section geometry",
                    message=(
                        "This calibration was measured with a different section geometry. "
                        "You can use it intentionally, but its accuracy is not guaranteed."
                    ),
                    mismatches=prepared.calibration_mismatches,
                    allow_clear=False,
                )
                if decision is not CalibrationMismatchDecision.KEEP:
                    self.refresh_planes()
                    return False
                policy = CalibrationMismatchPolicy.KEEP
            changed = self.service.select_plane(
                prepared,calibration_mismatch_policy=policy,
            )
            self.refresh_planes()
            if changed and prepared.plane_name != previous_active:
                self.close_dialog(section_key)
            return bool(changed)
        except Exception as error:
            self.controller._restore_section(section_key)
            self.refresh_planes()
            self._error("SLM plane selection failed",error)
            return False

    @QtCore.Slot(str)
    def _on_add_plane_requested(self,section_key: str) -> None:
        if (
            not self.controller.editor_writes_allowed
            or self.controller.automatic_operation_active
        ):
            return
        parent = self.controller.section_collection.section_view(section_key)
        definition = request_plane_definition(parent)
        if definition is None:
            return
        try:
            plane = self.service.add_plane(definition)
            if self.set_active_plane(section_key,plane):
                self.controller.sigInfo.emit("SLM Plane",'Added plane "%s".' % plane)
        except Exception as error:
            self.refresh_planes()
            self._error("Add SLM Plane Failed",error)

    @QtCore.Slot(str,str)
    def _on_delete_plane_requested(
        self,section_key: str,plane_name: str,
    ) -> None:
        if (
            not self.controller.editor_writes_allowed
            or self.controller.automatic_operation_active
        ):
            return
        plane = str(plane_name or "").strip()
        if not plane:
            return
        parent = self.controller.section_collection.section_view(section_key)
        if not confirm_plane_deletion(plane,parent):
            return
        try:
            deleted = self.service.delete_plane(plane)
            self.controller.sigInfo.emit(
                "SLM Plane",
                'Deleted plane "%s" and %d calibration file(s).'
                % (plane,len(deleted)),
            )
            self.refresh_planes()
        except Exception as error:
            self.refresh_planes()
            self._error("Delete SLM Plane Failed",error)

    # ------------------------------------------------------------------
    # Calibration dialog / target workflow presentation
    # ------------------------------------------------------------------

    @QtCore.Slot(str)
    def open_dialog(self,section_key: str) -> None:
        if (
            not self.controller.editor_writes_allowed
            or self.controller.automatic_operation_active
        ):
            return
        plane = self.service.active_plane(section_key)
        if not plane:
            self._error(
                "SLM Section Calibration",
                RuntimeError("Select or add a plane before saving calibration."),
            )
            return
        try:
            definition = self.service.plane_definition(plane)
        except Exception as error:
            self._error("SLM Section Calibration",error)
            return
        detector = str(definition.get("detector_name") or "").strip()

        dialog = self._dialogs.get(section_key)
        if dialog is None:
            dialog = CalibrationDialog(
                plane_name=plane,
                localization_parameters=_default_localization_parameters(),
                detectors=(detector,) if detector else (),
                current_detector=detector or None,
                title="Calibration - %s/%s" % (self.display_name,section_key),
                parent=self.controller.section_collection.section_view(section_key),
            )
            self._dialogs[section_key] = dialog
            dialog.set_bound_detector(detector)
            dialog.sigAcquireRequested.connect(
                lambda _source,key=section_key:self.acquire_target_measurement(key)
            )
            dialog.sigLoadRequested.connect(
                lambda key=section_key:self.load_target_measurement(key)
            )
            dialog.sigLocalizationRunRequested.connect(
                lambda parameters,key=section_key:self.run_target_localization(
                    key,dict(parameters or {})
                )
            )
            dialog.sigMethodChanged.connect(
                lambda method,key=section_key:self._on_method_changed(key,str(method))
            )
            dialog.sigCalibrationRequested.connect(
                lambda method,values,key=section_key:self._on_calibration_requested(
                    key,str(method),dict(values or {})
                )
            )
            dialog.finished.connect(
                lambda _result,key=section_key:self._dialog_finished(key)
            )
            dialog.destroyed.connect(
                lambda _obj=None,key=section_key:self._dialog_destroyed(key)
            )
        else:
            dialog.set_plane_name(plane)
            dialog.set_bound_detector(detector)

        self.render_target_state(section_key)
        self._refresh_live_acquisition(section_key)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _dialog_finished(self,section_key: str) -> None:
        self.service.discard_target_state(section_key)

    def _dialog_destroyed(self,section_key: str) -> None:
        self._dialogs.pop(section_key,None)

    def close_dialog(self,section_key: str) -> None:
        self.service.discard_target_state(section_key)
        dialog = self._dialogs.pop(section_key,None)
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()

    def close_all_dialogs(self) -> None:
        for section_key in tuple(self._dialogs):
            self.close_dialog(section_key)

    def _on_method_changed(self,section_key: str,method: str) -> None:
        if method == TARGET_LOCALIZATION_METHOD:
            self.ensure_target_reference(section_key)

    def ensure_target_reference(
        self,section_key: str,
    ) -> TargetCalibrationState | None:
        if not self.controller.editor_writes_allowed:
            return None
        dialog = self._dialogs.get(section_key)
        previous = self.service.target_state(section_key)
        previous_signature = None if previous is None else previous.target_signature
        try:
            self.controller.flush_section(section_key,propagate=True)
            state = self.service.ensure_target_reference(section_key)
            clear_measurement = previous_signature != state.target_signature
            if dialog is not None and state.reference is not None:
                dialog.set_target_reference(
                    context=state.reference.localization_context,
                    parameters=state.localization_parameters,
                    status="Target localization ready.",
                    clear_measurement=clear_measurement,
                )
                self.render_target_state(section_key)
                self._refresh_live_acquisition(section_key)
            return state
        except Exception as error:
            if dialog is not None:
                dialog.set_target_reference_error(error)
            else:
                self._error("Target localization reference failed",error)
            return None

    def acquire_target_measurement(self,section_key: str) -> None:
        if not self.controller.editor_writes_allowed:
            return
        dialog = self._dialogs.get(section_key)
        try:
            self.controller.flush_section(section_key,propagate=True)
            self.service.ensure_target_reference(section_key)
            self.service.acquire_target_measurement(section_key)
        except Exception as error:
            if dialog is not None:
                dialog.set_target_measurement_error(error)
                self._refresh_live_acquisition(section_key)
            else:
                self._error("Target calibration acquisition failed",error)

    def load_target_measurement(self,section_key: str) -> None:
        if not self.controller.editor_writes_allowed:
            return
        state = self.ensure_target_reference(section_key)
        if state is None:
            return
        dialog = self._dialogs.get(section_key)
        try:
            path,_selected = QtWidgets.QFileDialog.getOpenFileName(
                dialog,
                "Select target calibration image",
                "",
                "Images (*.tif *.tiff *.png *.jpg *.jpeg *.bmp);;All files (*)",
            )
            if not path:
                return
            image = np.asarray(Image.open(path).convert("F"),dtype=np.float64)
            measurement = create_image_measurement(
                image,
                source="file",
                metadata={
                    "path":str(path),
                    "slm_key":self.controller.runtime.identity.key,
                    "section_key":section_key,
                    "plane_name":self.service.active_plane(section_key),
                    "calibration_method":TARGET_LOCALIZATION_METHOD,
                },
            )
            self.service.commit_target_measurement(section_key,measurement)
            self.render_target_state(section_key)
        except Exception as error:
            if dialog is not None:
                dialog.set_target_measurement_error(error)
            else:
                self._error("Target calibration image load failed",error)

    def run_target_localization(
        self,section_key: str,parameters: Mapping[str,Any],
    ) -> None:
        if not self.controller.editor_writes_allowed:
            return
        dialog = self._dialogs.get(section_key)
        try:
            self.controller.flush_section(section_key,propagate=True)
            self.service.run_target_localization(section_key,parameters)
            self.render_target_state(section_key)
        except Exception as error:
            if dialog is not None:
                dialog.set_target_localization_error(error)
            else:
                self._error("Target localization failed",error)

    def render_target_state(self,section_key: str) -> None:
        dialog = self._dialogs.get(section_key)
        if dialog is None:
            return
        state = self.service.target_state(section_key)
        if state is None:
            self._refresh_live_acquisition(section_key)
            return
        if state.reference is not None:
            dialog.set_target_reference(
                context=state.reference.localization_context,
                parameters=state.localization_parameters,
                status="Target localization ready.",
                clear_measurement=False,
            )
        if state.measurement is not None and state.reference is not None:
            dialog.set_target_measurement(
                state.measurement,
                parameters=state.localization_parameters,
                context=state.reference.localization_context,
            )
        if state.localization is not None:
            dialog.set_target_localization_result(
                state.localization,state.localization_parameters,
            )
        if state.calibration_candidate is not None:
            dialog.set_target_calibration_candidate(state.calibration_candidate)
        self._refresh_live_acquisition(section_key)

    def _on_calibration_requested(
        self,section_key: str,method: str,values: Mapping[str,Any],
    ) -> None:
        if not self.controller.editor_writes_allowed:
            return
        plane = str(values.get("plane_name") or "").strip()
        if not plane:
            self._error(
                "SLM Section Calibration",
                RuntimeError("Calibration request is not bound to a plane."),
            )
            return
        try:
            self.controller.flush_section(section_key,propagate=True)
            if method == TARGET_LOCALIZATION_METHOD:
                value = self.service.apply_target_candidate(section_key,plane)
            elif method == CalibrationDialog.LINEAR_PHASE:
                calibration = self.service.linear_phase_calibration(
                    period_x_px=values.get("period_x_px"),
                    measured_dx_um=values.get("measured_dx_um"),
                    period_y_px=values.get("period_y_px"),
                    measured_dy_um=values.get("measured_dy_um"),
                )
                value = self.service.apply_and_save_calibration(
                    section_key,plane,calibration,
                )
            else:
                raise ValueError("Unknown calibration method %r" % method)
            self.refresh_planes()
            self.controller.sigInfo.emit(
                "SLM Section Calibration",
                "Saved calibration for %s/%s (%s):\n"
                "kx_per_um = %.6g\nky_per_um = %.6g"
                % (
                    self.display_name,section_key,plane,
                    value.kx_per_um,value.ky_per_um,
                ),
            )
        except Exception as error:
            self.controller._restore_section(section_key)
            self.refresh_planes()
            self._error("SLM Section Calibration Failed",error)

    # ------------------------------------------------------------------
    # Application callback rendering / lifecycle
    # ------------------------------------------------------------------

    def _refresh_live_acquisition(self,section_key: str) -> None:
        dialog = self._dialogs.get(section_key)
        if dialog is None:
            return
        availability = self.service.acquisition_availability(section_key)
        dialog.set_live_acquisition_available(
            availability.available,availability.reason,
        )

    def on_measurement_busy_changed(
        self,section_key: str,busy: bool,message: str,
    ) -> None:
        dialog = self._dialogs.get(section_key)
        if dialog is not None:
            dialog.set_target_measurement_busy(bool(busy),str(message or ""))
            self._refresh_live_acquisition(section_key)

    def on_measurement_error(self,section_key: str,error: Exception) -> None:
        dialog = self._dialogs.get(section_key)
        if dialog is not None:
            dialog.set_target_measurement_error(error)
        else:
            self._error("Target calibration acquisition failed",error)

    def synchronize_section(self,section_key: str) -> None:
        self._refresh_live_acquisition(section_key)

    def refresh_live_acquisition_all(self) -> None:
        for section_key in tuple(self._dialogs):
            self._refresh_live_acquisition(section_key)

    def set_cgh_computing(self,section_key: str,_computing: bool) -> None:
        self._refresh_live_acquisition(section_key)

    def cancel_request(self,section_key: str) -> None:
        self.service.cancel_request(section_key)

    def discard_target_state(self,section_key: str) -> None:
        self.service.discard_target_state(section_key)

    def prepare_runtime_state_change(self) -> None:
        # Application state is cleared by SLMSession before authoritative runtime
        # changes. Qt only owns the corresponding dialog lifetime.
        self.close_all_dialogs()

    def prepare_runtime_replacement(self) -> None:
        self.close_all_dialogs()
        self._disconnect_collection()

    def runtime_replaced(self) -> None:
        self._connect_collection()
        self.refresh_planes()

    @QtCore.Slot(bool)
    def _on_automatic_operation_changed(self,active: bool) -> None:
        if active:
            self.close_all_dialogs()
        self.refresh_planes()
        self.refresh_live_acquisition_all()

    def control_mode_changed(self) -> None:
        self.refresh_planes()
        self.refresh_live_acquisition_all()

    def dispose(self) -> None:
        if self._disposed:
            return
        self.close_all_dialogs()
        self._disconnect_collection()
        try:
            self.controller.sigAutomaticOperationChanged.disconnect(
                self._on_automatic_operation_changed,
            )
        except (RuntimeError,TypeError):
            pass
        self._disposed = True

    def _error(self,title: str,error: Any) -> None:
        if not isinstance(error,Exception):
            error = RuntimeError(str(error))
        self.controller._emit_exception(title,error)


__all__ = ["CalibrationManager"]
