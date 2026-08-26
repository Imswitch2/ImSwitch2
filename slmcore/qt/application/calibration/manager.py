from __future__ import annotations

from contextlib import nullcontext
from typing import Any,Mapping

import numpy as np
from PIL import Image
from qtpy import QtCore,QtWidgets

from ....calibration.slm_section_calibration import SLMSectionCalibration
from ....calibration.store import SLMCalibrationStore
from ....calibration.target_localization_calibration import (
    fit_target_localization_calibration,
)
from ....calibration.geometry import (
    attach_calibration_geometry,calibration_geometry_mismatches,
)
from ....cgh.execution.status import CGHResultState
from ....cgh.localization.parameters import LOCALIZATION_PARAMS
from ....cgh.localization.policy import suggest_localization_sources
from ....cgh.localization.reference import TargetLocalizationReference
from ....cgh.localization.workflow import localize_measurement
from ....host.calibration import CalibrationPreferences
from ....measurement import ImageMeasurement,create_image_measurement
from ...calibration.dialog import (
    CalibrationDialog,
    TARGET_LOCALIZATION_METHOD,
)
from ...calibration.plane_dialogs import (
    confirm_plane_deletion,
    request_plane_definition,
)
from ...calibration.geometry_dialogs import (
    CalibrationMismatchDecision,calibration_mismatch_decision,
)
from ..measurement_dispatcher import QtMeasurementDispatcher,QtMeasurementRequest
from .state import TargetCalibrationState


def _default_localization_parameters() -> dict[str, Any]:
    return {
        key:spec.validate(spec.default)
        for key,spec in LOCALIZATION_PARAMS.items()
    }


def _validated_localization_parameters(
    values: Mapping[str,Any],
) -> dict[str, Any]:
    values = dict(values or {})
    unknown = set(values) - set(LOCALIZATION_PARAMS)
    if unknown:
        raise KeyError(
            "Unknown localization parameter(s): "
            + ", ".join(sorted(unknown))
        )
    return {
        key:spec.validate(values.get(key,spec.default))
        for key,spec in LOCALIZATION_PARAMS.items()
    }


class CalibrationManager(QtCore.QObject):
    """Reusable calibration/plane workflow for one ``SLMQtSession``."""

    def __init__(
        self,
        controller: Any,
        *,
        measurements: QtMeasurementDispatcher,
        store: SLMCalibrationStore | None,
        preferences: CalibrationPreferences | None,
        display_name: str,
        apply_startup_defaults: bool=False,
        parent: QtCore.QObject | None=None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.measurements = measurements
        self.store = store
        self.preferences = preferences
        self.display_name = str(display_name or controller.runtime.identity.key)
        self._dialogs: dict[str, CalibrationDialog] = {}
        self._states: dict[str, TargetCalibrationState] = {}
        self._requests: dict[str, QtMeasurementRequest] = {}
        # The dispatcher protects each request. This generation also rejects
        # callbacks after a broader calibration/runtime state reset.
        self._generation = 0
        self._store_change_pending = False
        self._disposed = False

        if self.store is not None:
            self.store.add_listener(self._on_store_changed)
            if self.store.last_load_error is not None:
                self.controller.sigWarning.emit(
                    "SLM calibration",
                    "Could not load plane definitions: %s"
                    % self.store.last_load_error,
                )
        self._connect_collection()
        self.controller.sigAutomaticOperationChanged.connect(
            self._on_automatic_operation_changed,
        )
        if apply_startup_defaults:
            self._apply_startup_defaults()
        self.refresh_planes()

    # ------------------------------------------------------------------
    # Plane catalog / selection
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

    def _apply_startup_defaults(self) -> None:
        if self.store is None or self.preferences is None:
            return
        runtime = self.controller.runtime
        for section_key in runtime.section_keys:
            plane = self.preferences.get(section_key)
            if not plane:
                continue
            if not self.store.has_plane(plane):
                try:
                    self.preferences.set(section_key,None)
                except Exception as error:
                    self._error("SLM calibration preference failed",error)
                continue
            try:
                calibration = self.store.load_calibration(
                    runtime.identity,section_key,plane,
                )
                mismatches = calibration_geometry_mismatches((
                    (section_key,runtime.get_section_geometry(section_key),calibration),
                ))
                if mismatches:
                    self.controller.sigWarning.emit(
                        "SLM calibration",
                        "Default calibration for %s/%s was not applied because "
                        "its recorded section geometry differs from the current layout: %s"
                        % (self.display_name,section_key,mismatches[0].summary()),
                    )
                    continue
                transition = runtime.set_section_calibration(
                    section_key,calibration,
                )
                # Startup defaults are applied before hardware connection. Keep
                # the view authoritative without publishing/uploading a frame.
                if transition is not None:
                    self.controller.section_collection.apply_section_transition(
                        section_key,transition,
                    )
                self.controller.synchronize_section(section_key)
            except Exception as error:
                self.controller.sigWarning.emit(
                    "SLM calibration",
                    "Could not apply default calibration for %s/%s: %s"
                    % (self.display_name,section_key,error),
                )

    def refresh_planes(self) -> None:
        names = () if self.store is None else self.store.plane_names
        for section_key in self.controller.runtime.section_keys:
            self.controller.section_collection.set_available_planes(
                section_key,names,self._runtime_active_plane(section_key),
            )

    def _runtime_active_plane(self,section_key: str) -> str | None:
        calibration = self.controller.runtime.get_section_calibration_copy(
            section_key
        )
        plane = str(getattr(calibration,"plane",None) or "").strip()
        if plane and self.store is not None and self.store.has_plane(plane):
            return plane
        return None

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
        if self.store is None:
            self._error(
                "SLM plane selection failed",
                RuntimeError("Calibration storage is not configured."),
            )
            return False
        runtime = self.controller.runtime
        previous_calibration = runtime.get_section_calibration_copy(section_key)
        previous_default = (
            None if self.preferences is None
            else self.preferences.get(section_key)
        )
        previous_active = self._runtime_active_plane(section_key)
        try:
            calibration = (
                None if plane_name is None
                else self.store.load_calibration(
                    runtime.identity,section_key,plane_name,
                )
            )
            mismatches = calibration_geometry_mismatches((
                (section_key,runtime.get_section_geometry(section_key),calibration),
            ))
            if mismatches:
                decision = calibration_mismatch_decision(
                    self.controller.section_collection.section_view(section_key),
                    title="Use calibration with different section geometry",
                    message=(
                        "This calibration was measured with a different section geometry. "
                        "You can use it intentionally, but its accuracy is not guaranteed."
                    ),
                    mismatches=mismatches,
                    allow_clear=False,
                )
                if decision is not CalibrationMismatchDecision.KEEP:
                    self.refresh_planes()
                    return False
            self._commit_runtime_calibration(section_key,calibration)
            if self.preferences is not None:
                self.preferences.set(section_key,plane_name)
            self.refresh_planes()
            if plane_name != previous_active:
                self.discard_target_state(section_key)
                self.close_dialog(section_key)
            return True
        except Exception as error:
            try:
                self._commit_runtime_calibration(section_key,previous_calibration)
                if self.preferences is not None:
                    self.preferences.set(section_key,previous_default)
            except Exception as rollback_error:
                self._error("SLM plane selection rollback failed",rollback_error)
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
        if self.store is None:
            self._error("Add SLM Plane Failed",RuntimeError("Calibration storage is not configured."))
            return
        try:
            plane = self.store.add_plane(definition)
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
        if self.store is None:
            self._error("Delete SLM Plane Failed",RuntimeError("Calibration storage is not configured."))
            return
        try:
            deleted = self.store.delete_plane(plane)
            self.controller.sigInfo.emit(
                "SLM Plane",
                'Deleted plane "%s" and %d calibration file(s).'
                % (plane,len(deleted)),
            )
        except Exception as error:
            self.refresh_planes()
            self._error("Delete SLM Plane Failed",error)

    def _on_store_changed(self) -> None:
        if self._disposed:
            return
        if (
            not self.controller.editor_writes_allowed
            or self.controller.automatic_operation_active
        ):
            self._store_change_pending = True
            return
        self._reconcile_plane_catalog()

    def _reconcile_plane_catalog(self) -> None:
        self._store_change_pending = False
        if self.store is None:
            return
        runtime = self.controller.runtime
        try:
            with self.controller.defer_frame_upload():
                for section_key in runtime.section_keys:
                    if self.preferences is not None:
                        preferred = self.preferences.get(section_key)
                        if preferred and not self.store.has_plane(preferred):
                            self.preferences.set(section_key,None)
                    calibration = runtime.get_section_calibration_copy(section_key)
                    active = str(getattr(calibration,"plane",None) or "").strip()
                    if active and not self.store.has_plane(active):
                        self._commit_runtime_calibration(section_key,None)
                        self.discard_target_state(section_key)
                        self.close_dialog(section_key)
        except Exception as error:
            self._error("SLM plane catalog synchronization failed",error)
        self.refresh_planes()

    # ------------------------------------------------------------------
    # Calibration dialog / target workflow
    # ------------------------------------------------------------------

    @QtCore.Slot(str)
    def open_dialog(self,section_key: str) -> None:
        if (
            not self.controller.editor_writes_allowed
            or self.controller.automatic_operation_active
        ):
            return
        plane = self._runtime_active_plane(section_key)
        if not plane:
            self._error(
                "SLM Section Calibration",
                RuntimeError("Select or add a plane before saving calibration."),
            )
            return
        if self.store is None:
            self._error("SLM Section Calibration",RuntimeError("Calibration storage is not configured."))
            return
        definition = self.store.plane_definition(plane)
        detector = str(definition.get("detector_name") or "").strip()
        state = self._states.get(section_key)
        if state is None:
            state = TargetCalibrationState(
                plane_name=plane,
                localization_parameters=_default_localization_parameters(),
            )
            self._states[section_key] = state
        else:
            state.plane_name = plane

        dialog = self._dialogs.get(section_key)
        if dialog is None:
            dialog = CalibrationDialog(
                plane_name=plane,
                localization_parameters=_default_localization_parameters(),
                detectors=(detector,),
                current_detector=detector,
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
                lambda _result,key=section_key:self.discard_target_state(key)
            )
            dialog.destroyed.connect(
                lambda _obj=None,key=section_key:self._dialogs.pop(key,None)
            )
        else:
            dialog.set_plane_name(plane)
            dialog.set_bound_detector(detector)

        self._refresh_live_acquisition(section_key)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def close_dialog(self,section_key: str) -> None:
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
        state = self._states.get(section_key)
        if state is None:
            state = TargetCalibrationState(
                plane_name=self._runtime_active_plane(section_key),
                localization_parameters=_default_localization_parameters(),
            )
            self._states[section_key] = state
        dialog = self._dialogs.get(section_key)
        try:
            self.controller.flush_section(section_key,propagate=True)
            reference = self.controller.runtime.get_section_base_target_localization_reference(
                section_key
            )
            signature = str(reference.target_signature or "")
            if state.reference is not None and state.target_signature == signature:
                if dialog is not None:
                    dialog.set_target_reference(
                        context=state.reference.localization_context,
                        parameters=state.localization_parameters,
                        status="Target localization ready.",
                        clear_measurement=False,
                    )
                    self._refresh_live_acquisition(section_key)
                return state

            state.reference = reference
            state.target_signature = signature
            state.measurement = None
            state.localization = None
            state.calibration_candidate = None
            state.localization_parameters = self._target_reference_localization_parameters(
                reference
            )
            if dialog is not None:
                dialog.set_target_reference(
                    context=reference.localization_context,
                    parameters=state.localization_parameters,
                    status="Target localization ready.",
                    clear_measurement=True,
                )
                self._refresh_live_acquisition(section_key)
            return state
        except Exception as error:
            state.reference = None
            state.target_signature = None
            state.measurement = None
            state.localization = None
            state.calibration_candidate = None
            state.localization_parameters = _default_localization_parameters()
            if dialog is not None:
                dialog.set_target_reference_error(error)
            return None

    def _target_reference_localization_parameters(
        self,reference: TargetLocalizationReference,
    ) -> dict[str, Any]:
        parameters = _default_localization_parameters()
        context = dict(reference.localization_context)
        parameters["period_prior_mode"] = "auto"
        parameters["stagger_prior_mode"] = (
            "target" if context.get("target_stagger") is not None else "auto"
        )
        parameters["lattice_size_prior_mode"] = (
            "target" if context.get("target_lattice_count") is not None else "auto"
        )
        return _validated_localization_parameters(parameters)

    def _live_acquisition_state(self,section_key: str) -> tuple[bool, str]:
        if not self.controller.editor_writes_allowed:
            return False,"Live acquisition is unavailable in Fast Config mode."
        if not self.measurements.available:
            return False,"Live acquisition requires a measurement provider."
        if self.controller.automatic_operation_active:
            return False,"Automatic feedback is currently running."
        if self.controller.is_cgh_computing(section_key):
            return False,"CGH computation is in progress."
        status = self.controller.runtime.get_section_cgh_status(section_key)
        if status.result_state is CGHResultState.MISSING:
            return False,"Live acquisition requires a computed and applied CGH target."
        if status.result_state is CGHResultState.STALE:
            return False,(
                "Live acquisition requires the current CGH target to be computed "
                "and applied. Recompute the CGH before acquiring a calibration image."
            )
        if status.result_state is not CGHResultState.CURRENT:
            return False,"Live acquisition requires a current computed and applied CGH."
        if not self.controller.auto_upload_frame:
            return False,(
                "Live acquisition is disabled when auto_upload_frame=False because "
                "slmcore cannot guarantee that the current CGH is applied to hardware."
            )
        if not self.controller.host_services.can_upload_frame:
            return False,"Live acquisition requires a frame-upload capability."
        if self.controller.last_upload_error is not None:
            return False,(
                "The last SLM frame upload failed. Upload the current frame successfully "
                "before acquiring a calibration image."
            )
        return True,""

    def _refresh_live_acquisition(self,section_key: str) -> None:
        dialog = self._dialogs.get(section_key)
        if dialog is None:
            return
        available,reason = self._live_acquisition_state(section_key)
        dialog.set_live_acquisition_available(available,reason)

    def acquire_target_measurement(self,section_key: str) -> None:
        if not self.controller.editor_writes_allowed:
            return
        state = self.ensure_target_reference(section_key)
        if state is None:
            return
        dialog = self._dialogs.get(section_key)
        available,reason = self._live_acquisition_state(section_key)
        if not available:
            if dialog is not None:
                dialog.set_target_measurement_error(reason)
                self._refresh_live_acquisition(section_key)
            return
        if self.store is None:
            return
        plane = self._runtime_active_plane(section_key)
        if not plane:
            return
        detector = str(
            self.store.plane_definition(plane).get("detector_name") or ""
        ).strip()
        self.cancel_request(section_key)
        generation = self._generation
        if dialog is not None:
            dialog.set_target_measurement_busy(
                True,"Waiting for %s..." % detector,
            )

        def on_result(measurement: ImageMeasurement) -> None:
            if generation != self._generation:
                return
            self._requests.pop(section_key,None)
            if not self.controller.editor_writes_allowed:
                return
            try:
                self._commit_target_measurement(section_key,measurement)
            except Exception as error:
                self._error("Target calibration measurement failed",error)
                current_dialog = self._dialogs.get(section_key)
                if current_dialog is not None:
                    current_dialog.set_target_measurement_error(error)

        def on_error(error: Exception) -> None:
            if generation != self._generation:
                return
            self._requests.pop(section_key,None)
            current_dialog = self._dialogs.get(section_key)
            if current_dialog is not None:
                current_dialog.set_target_measurement_error(error)
            else:
                self._error("Target calibration acquisition failed",error)

        request = self.measurements.acquire(
            section_key,
            detector,
            metadata={
                "slm_key":self.controller.runtime.identity.key,
                "section_key":section_key,
                "plane_name":plane,
                "calibration_method":TARGET_LOCALIZATION_METHOD,
            },
            on_result=on_result,
            on_error=on_error,
        )
        if request.active:
            self._requests[section_key] = request

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
                    "plane_name":self._runtime_active_plane(section_key),
                    "calibration_method":TARGET_LOCALIZATION_METHOD,
                },
            )
            self._commit_target_measurement(section_key,measurement)
        except Exception as error:
            if dialog is not None:
                dialog.set_target_measurement_error(error)
            else:
                self._error("Target calibration image load failed",error)

    def _commit_target_measurement(
        self,section_key: str,measurement: ImageMeasurement,
    ) -> None:
        self._require_editor_mode()
        state = self._states.get(section_key)
        if state is None or state.reference is None:
            raise RuntimeError("Target localization reference is not available.")
        state.measurement = measurement
        state.localization = None
        state.calibration_candidate = None
        parameters = _default_localization_parameters()
        parameters.update(
            suggest_localization_sources(
                measurement,
                state.reference.localization_context,
                allow_target_hints=(measurement.source.strip().lower() != "file"),
            )
        )
        state.localization_parameters = _validated_localization_parameters(parameters)
        dialog = self._dialogs.get(section_key)
        if dialog is not None:
            dialog.set_target_measurement(
                measurement,
                parameters=state.localization_parameters,
                context=state.reference.localization_context,
            )
            self._refresh_live_acquisition(section_key)

    def run_target_localization(
        self,section_key: str,parameters: Mapping[str,Any],
    ) -> None:
        if not self.controller.editor_writes_allowed:
            return
        state = self.ensure_target_reference(section_key)
        dialog = self._dialogs.get(section_key)
        if state is None:
            if dialog is not None:
                dialog.set_target_localization_error(
                    "Target localization is not available for this section."
                )
            return
        if state.measurement is None:
            if dialog is not None:
                dialog.set_target_localization_error(
                    "Acquire or load an image before localization."
                )
            return
        try:
            normalized = _validated_localization_parameters(parameters)
            reference = state.reference
            localization = localize_measurement(
                state.measurement,
                target_type=reference.target_type,
                target_params=reference.target_params,
                resolution=reference.resolution,
                parameters=normalized,
                calibration=None,
            )
            state.localization_parameters = normalized
            state.localization = localization
            state.calibration_candidate = None
            if dialog is not None:
                dialog.set_target_localization_result(localization,normalized)
            self._fit_target_candidate(section_key,state,localization)
        except Exception as error:
            if dialog is not None:
                dialog.set_target_localization_error(error)
            else:
                self._error("Target localization failed",error)

    def _fit_target_candidate(
        self,section_key: str,state: TargetCalibrationState,localization: Any,
    ) -> None:
        dialog = self._dialogs.get(section_key)
        try:
            if state.reference is None:
                raise RuntimeError("Target localization reference is not available.")
            plane = str(state.plane_name or self._runtime_active_plane(section_key) or "").strip()
            if not plane or self.store is None:
                raise RuntimeError("Target calibration is not bound to a plane.")
            state.plane_name = plane
            definition = self.store.plane_definition(plane)
            measurement = state.measurement
            candidate = fit_target_localization_calibration(
                resolution=state.reference.resolution,
                localization=localization,
                detector_pixel_size_um=float(definition["detector_pixel_size_um"]),
                plane=plane,
                metadata={
                    "slm_key":self.controller.runtime.identity.key,
                    "section_key":section_key,
                    "target_type":state.reference.target_type,
                    "target_signature":state.target_signature,
                    "measurement_id":getattr(measurement,"measurement_id",None),
                    "measurement_source":getattr(measurement,"source",None),
                    "measurement_detector":getattr(measurement,"detector",None),
                },
            )
            state.calibration_candidate = candidate
            if dialog is not None:
                dialog.set_target_calibration_candidate(candidate)
        except Exception as error:
            state.calibration_candidate = None
            if dialog is not None:
                dialog.set_target_calibration_candidate_error(error)
            else:
                self._error("Target calibration fit failed",error)

    def _on_calibration_requested(
        self,section_key: str,method: str,values: Mapping[str,Any],
    ) -> None:
        if not self.controller.editor_writes_allowed:
            return
        plane = str(values.get("plane_name") or "").strip()
        if not plane:
            self._error("SLM Section Calibration",RuntimeError("Calibration request is not bound to a plane."))
            return
        if method == TARGET_LOCALIZATION_METHOD:
            state = self._states.get(section_key)
            if state is None or state.calibration_candidate is None:
                self._error("SLM Section Calibration",RuntimeError("Run target localization before setting calibration."))
                return
            if state.plane_name and state.plane_name != plane:
                self._error("SLM Section Calibration",RuntimeError("Calibration candidate belongs to a different plane."))
                return
            self._apply_and_save_calibration(
                section_key,plane,state.calibration_candidate.calibration,
            )
            return
        if method == CalibrationDialog.LINEAR_PHASE:
            try:
                calibration = SLMSectionCalibration.from_linear_phase_test(
                    values.get("period_x_px"),
                    values.get("measured_dx_um"),
                    values.get("period_y_px"),
                    values.get("measured_dy_um"),
                )
            except Exception as error:
                self._error("SLM Section Calibration Failed",error)
                return
            self._apply_and_save_calibration(section_key,plane,calibration)

    def _apply_and_save_calibration(
        self,
        section_key: str,
        plane_name: str,
        calibration: SLMSectionCalibration,
    ) -> bool:
        if not self.controller.editor_writes_allowed:
            return False
        if self.store is None:
            self._error("SLM Section Calibration Failed",RuntimeError("Calibration storage is not configured."))
            return False
        runtime = self.controller.runtime
        previous_calibration = runtime.get_section_calibration_copy(section_key)
        previous_default = None if self.preferences is None else self.preferences.get(section_key)
        try:
            definition = self.store.plane_definition(plane_name)
            value = SLMSectionCalibration.from_dict(calibration).copy()
            value.plane = plane_name
            value.cam_px_size_um = float(definition["detector_pixel_size_um"])
            value = attach_calibration_geometry(
                value,runtime.get_section_geometry(section_key),
            )
            self._commit_runtime_calibration(section_key,value)
            value = self.store.save_calibration(
                runtime.identity,self.display_name,section_key,plane_name,value,
            )
            if self.preferences is not None:
                self.preferences.set(section_key,plane_name)
            self.refresh_planes()
            self.controller.sigInfo.emit(
                "SLM Section Calibration",
                "Saved calibration for %s/%s (%s):\n"
                "kx_per_um = %.6g\nky_per_um = %.6g"
                % (
                    self.display_name,section_key,plane_name,
                    value.kx_per_um,value.ky_per_um,
                ),
            )
            return True
        except Exception as error:
            try:
                self._commit_runtime_calibration(section_key,previous_calibration)
                if self.preferences is not None:
                    self.preferences.set(section_key,previous_default)
            except Exception as rollback_error:
                self._error("SLM calibration rollback failed",rollback_error)
            self.controller._restore_section(section_key)
            self.refresh_planes()
            self._error("SLM Section Calibration Failed",error)
            return False

    def _commit_runtime_calibration(
        self,section_key: str,calibration: SLMSectionCalibration | None,
    ):
        self._require_editor_mode()
        self.controller.cancel_cgh(section_key)
        self.controller.flush_section(section_key,propagate=True)
        transition = self.controller.runtime.set_section_calibration(
            section_key,calibration,
        )
        if transition is None:
            self.controller.synchronize_section(section_key)
            return None
        self.controller.apply_transition(section_key,transition)
        return transition

    # ------------------------------------------------------------------
    # Lifecycle / synchronization
    # ------------------------------------------------------------------

    def synchronize_section(self,section_key: str) -> None:
        self._refresh_live_acquisition(section_key)

    def refresh_live_acquisition_all(self) -> None:
        for section_key in tuple(self._dialogs):
            self._refresh_live_acquisition(section_key)

    def set_cgh_computing(self,section_key: str,_computing: bool) -> None:
        self._refresh_live_acquisition(section_key)

    def cancel_request(self,section_key: str) -> None:
        request = self._requests.pop(section_key,None)
        if request is not None:
            request.cancel()

    def discard_target_state(self,section_key: str) -> None:
        self.cancel_request(section_key)
        self._states.pop(section_key,None)

    def prepare_runtime_state_change(self) -> None:
        self._generation += 1
        for section_key in tuple(self._requests):
            self.cancel_request(section_key)
        self._states.clear()
        self.close_all_dialogs()

    def prepare_runtime_replacement(self) -> None:
        self.prepare_runtime_state_change()
        self._disconnect_collection()

    def runtime_replaced(self) -> None:
        self._connect_collection()
        self.refresh_planes()

    @QtCore.Slot(bool)
    def _on_automatic_operation_changed(self,active: bool) -> None:
        if active:
            self.prepare_runtime_state_change()
        elif self._store_change_pending:
            self._reconcile_plane_catalog()

    def control_mode_changed(self) -> None:
        if self.controller.editor_writes_allowed and self._store_change_pending:
            self._reconcile_plane_catalog()

    def _require_editor_mode(self) -> None:
        if not self.controller.editor_writes_allowed:
            raise RuntimeError("Operation unavailable in Fast Config mode")

    def dispose(self) -> None:
        if self._disposed:
            return
        self.prepare_runtime_state_change()
        self._disconnect_collection()
        try:
            self.controller.sigAutomaticOperationChanged.disconnect(
                self._on_automatic_operation_changed,
            )
        except (RuntimeError,TypeError):
            pass
        if self.store is not None:
            self.store.remove_listener(self._on_store_changed)
        self._disposed = True

    def _error(self,title: str,error: Any) -> None:
        if not isinstance(error,Exception):
            error = RuntimeError(str(error))
        self.controller._emit_exception(title,error)


__all__ = ["CalibrationManager"]
