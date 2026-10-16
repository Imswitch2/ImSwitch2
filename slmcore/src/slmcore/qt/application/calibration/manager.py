from __future__ import annotations

from typing import Any,Callable,Mapping

import numpy as np
from PIL import Image
from qtpy import QtCore,QtWidgets

from ....application.calibration import TargetCalibrationState
from ....application.configuration import CalibrationMismatchPolicy
from ....core.cgh.localization.parameters import LOCALIZATION_PARAMS
from ....core.measurement import create_image_measurement
from ....core.calibration import base_mapping_signature
from ....workspace import ObservationPlaneDefinition
from ...calibration.dialog import CalibrationDialog,TARGET_LOCALIZATION_METHOD
from ...calibration.fov_dialog import FOVCalibrationDialog
from ...calibration.geometry_dialogs import (
    CalibrationMismatchDecision,calibration_mismatch_decision,
)
from ...calibration.mapping_dialogs import (
    confirm_calibration_deletion,confirm_mapping_deletion,
    request_mapping_definition,
)
from ...calibration.management_dialogs import (
    CalibrationManagementDialog,ObservationPlaneEditorDialog,
    ObservationPlanesManagerDialog,
)
from ...calibration.observation_registration_dialog import (
    ObservationRegistrationCalibrationWidget,
)


def _default_localization_parameters() -> dict[str,Any]:
    return {
        key:spec.validate(spec.default)
        for key,spec in LOCALIZATION_PARAMS.items()
    }


class CalibrationManager(QtCore.QObject):
    sigFovMeasurementReceived=QtCore.Signal(str,object)
    sigFovMeasurementError=QtCore.Signal(str,object)
    sigManagementWorkflowActiveChanged=QtCore.Signal(bool)
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
        self._fov_dialogs: dict[str,FOVCalibrationDialog] = {}
        self._management_dialog: CalibrationManagementDialog | None = None
        self._management_dialog_owned = False
        self._shared_workspace_opener: Callable[[str],bool] | None = None
        self._observation_planes_dialog: ObservationPlanesManagerDialog | None = None
        self._embedded_workflow: tuple[str,str] | None = None
        self._embedded_return: tuple[str,str,QtWidgets.QWidget,str,bool] | None = None
        self._registration_widget: ObservationRegistrationCalibrationWidget | None = None
        self._registration_plane: str | None = None
        self._disposed = False
        load_error = self.service.last_mapping_store_load_error
        if load_error is not None:
            self.controller.sigWarning.emit(
                "SLM calibration",
                "Could not load mapping definitions: %s" % load_error,
            )
        self._connect_collection()
        self.controller.sigAutomaticOperationChanged.connect(
            self._on_automatic_operation_changed,
        )
        self.sigFovMeasurementReceived.connect(self._fov_measurement_received)
        self.sigFovMeasurementError.connect(self._fov_measurement_error)
        self.controller.geometry_calibration.sigSaved.connect(
            self._geometry_calibration_saved
        )
        self.refresh_mappings()

    # ------------------------------------------------------------------
    # Mapping catalog / selection presentation
    # ------------------------------------------------------------------

    def _connect_collection(self) -> None:
        collection = self.controller.section_collection
        collection.sigActiveMappingRequested.connect(self._on_active_mapping_requested)
        collection.sigCalibrationRequested.connect(self._on_management_requested)

    def _disconnect_collection(self) -> None:
        collection = self.controller.section_collection
        for signal,slot in (
            (collection.sigActiveMappingRequested,self._on_active_mapping_requested),
            (collection.sigCalibrationRequested,self._on_management_requested),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError,TypeError):
                pass

    def set_shared_workspace_opener(
        self,opener: Callable[[str],bool] | None,
    ) -> None:
        if opener is not None and not callable(opener):
            raise TypeError("workspace opener must be callable or None")
        self._shared_workspace_opener=opener

    @QtCore.Slot(str)
    def _on_management_requested(self,section_key: str) -> None:
        opener=self._shared_workspace_opener
        if opener is not None:
            opener(str(section_key))
            return
        self.open_management(section_key)

    @property
    def management_workflow_active(self) -> bool:
        return self._embedded_workflow is not None

    def _set_embedded_workflow(
        self,workflow: tuple[str,str] | None,
    ) -> None:
        previous=self._embedded_workflow is not None
        self._embedded_workflow=workflow
        active=workflow is not None
        if active != previous:
            self.sigManagementWorkflowActiveChanged.emit(active)

    def management_mutations_available(self) -> bool:
        return bool(
            self.controller.editor_writes_allowed
            and not self.controller.automatic_operation_active
        )

    def _refresh_management_availability(self) -> None:
        dialog=self._management_dialog
        if dialog is None:
            return
        enabled=self.management_mutations_available()
        reason=""
        if not enabled:
            if self.controller.automatic_operation_active:
                reason="Unavailable while an automatic SLM operation is active."
            else:
                reason="Unavailable in Fast Config mode."
        dialog.set_mutations_enabled(enabled,reason)

    def _management_mutation_allowed(self) -> bool:
        allowed=self.management_mutations_available()
        if not allowed:
            self._refresh_management_availability()
        return allowed

    def refresh_mappings(self) -> None:
        names = self.service.mapping_names
        for section_key in self.controller.runtime.section_keys:
            self.controller.section_collection.set_available_mappings(
                section_key,names,self.service.active_mapping(section_key),
            )
        if self._management_dialog is not None:
            sections=tuple(self.controller.runtime.section_keys)
            titles={key:self.controller.section_host.section_title(key) for key in sections}
            descriptions={}
            for name in names:
                try: descriptions[name]=self.service.mapping_definition(name).description
                except Exception: descriptions[name]=""
            self._management_dialog.set_catalog(
                sections,names,titles,descriptions,
            )
            self._refresh_observation_catalog()
            self._refresh_management_availability()

    @QtCore.Slot(str,str)
    def _on_active_mapping_requested(
        self,section_key: str,mapping_name: str,
    ) -> None:
        if (
            not self.controller.editor_writes_allowed
            or self.controller.automatic_operation_active
        ):
            return
        self.set_active_mapping(section_key,str(mapping_name or "").strip() or None)

    def set_active_mapping(
        self,section_key: str,mapping_name: str | None,
    ) -> bool:
        if not self.controller.editor_writes_allowed:
            return False
        previous_active = self.service.active_mapping(section_key)
        try:
            self.controller.flush_section(section_key,propagate=True)
            prepared = self.service.prepare_mapping_selection(section_key,mapping_name)
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
                    self.refresh_mappings()
                    return False
                policy = CalibrationMismatchPolicy.KEEP
            changed = self.service.select_mapping(
                prepared,calibration_mismatch_policy=policy,
            )
            self.refresh_mappings()
            if changed and prepared.mapping_name != previous_active:
                self.close_dialog(section_key)
            return bool(changed)
        except Exception as error:
            self.controller._restore_section(section_key)
            self.refresh_mappings()
            self._error("SLM mapping selection failed",error)
            return False

    # ------------------------------------------------------------------
    # Persistent calibration management
    # ------------------------------------------------------------------

    def _management_signal_bindings(self,dialog: CalibrationManagementDialog):
        return (
            (dialog.sigContextChanged,self._refresh_management_context),
            (dialog.sigMappingAddRequested,self._management_add_mapping),
            (dialog.sigMappingDeleteRequested,self._management_delete_mapping),
            (dialog.sigBaseCalibrationRequested,self._management_start_base_calibration),
            (dialog.sigBaseCalibrationDeleteRequested,self._management_delete_base_calibration),
            (dialog.sigFovCalibrationRequested,self._management_start_fov_calibration),
            (dialog.sigFovSetDefaultRequested,self._management_set_fov_default),
            (dialog.sigFovDeleteRequested,self._management_delete_fov),
            (dialog.sigFovRefitRequested,self._management_refit_fov),
            (dialog.sigFovUpdateRequested,self._management_update_fov),
            (dialog.sigFovRenameRequested,self._management_rename_fov),
            (dialog.sigGeometryCalibrationRequested,self._management_start_geometry_calibration),
            (dialog.sigObservationRegistrationRequested,self._management_start_observation_registration),
            (dialog.sigObservationRegistrationDeleteRequested,self._management_delete_observation_registration),
            (dialog.sigObservationSelectionChanged,self._refresh_observation_context),
            (dialog.sigObservationCreateRequested,self._management_create_observation),
            (dialog.sigObservationUpdateRequested,self._management_update_observation),
            (dialog.sigObservationDeleteRequested,self._management_delete_observation),
            (dialog.sigWorkspaceResetRequested,self._management_leave_embedded_workflow),
        )

    def _connect_management_view(self,dialog: CalibrationManagementDialog) -> None:
        for signal,slot in self._management_signal_bindings(dialog):
            signal.connect(slot)

    def _disconnect_management_view(self,dialog: CalibrationManagementDialog) -> None:
        for signal,slot in self._management_signal_bindings(dialog):
            try:
                signal.disconnect(slot)
            except (RuntimeError,TypeError):
                pass

    def attach_management_view(
        self,dialog: CalibrationManagementDialog,*,section_key: str | None=None,
    ) -> None:
        if not isinstance(dialog,CalibrationManagementDialog):
            raise TypeError("dialog must be a CalibrationManagementDialog")
        if self._management_dialog is not None and self._management_dialog is not dialog:
            self.detach_management_view()
        if self._management_dialog is None:
            self._management_dialog=dialog
            self._management_dialog_owned=False
            self._connect_management_view(dialog)
        self.populate_management_view(section_key=section_key)

    def detach_management_view(self) -> None:
        dialog=self._management_dialog
        if dialog is None:
            return
        self._management_leave_embedded_workflow()
        try:
            dialog.finish_observation_edit()
        except RuntimeError:
            pass
        self._disconnect_management_view(dialog)
        self._management_dialog=None
        self._management_dialog_owned=False

    def reconcile_management_section_topology(
        self,preferred_section: str | None=None,
    ) -> bool:
        """Refresh the shared management view after section topology changes.

        Any embedded calibration is explicitly cancelled before repopulating the
        view because its section geometry/topology assumptions may no longer be
        valid.  Returns whether an active workflow had to be cancelled.
        """
        if self._management_dialog is None:
            return False
        was_active=self.management_workflow_active
        if was_active:
            self._management_leave_embedded_workflow()
        self.populate_management_view(section_key=preferred_section)
        return was_active

    def populate_management_view(self,section_key: str | None=None) -> None:
        dialog=self._management_dialog
        if dialog is None:
            return
        sections=tuple(self.controller.runtime.section_keys)
        section_titles={
            key:self.controller.section_host.section_title(key)
            for key in sections
        }
        mappings=self.service.mapping_names
        descriptions={}
        for name in mappings:
            try:
                descriptions[name]=self.service.mapping_definition(name).description
            except Exception:
                descriptions[name]=""
        section=str(section_key or dialog.section_key or "").strip()
        if section not in sections:
            section=sections[0] if sections else ""
        current_mapping=self.service.active_mapping(section) if section else None
        if self._management_dialog_owned:
            identity=self.controller.runtime.identity
            slm_label=identity.display_name or identity.key
            if identity.serial_number and identity.serial_number not in slm_label:
                slm_label="%s · %s" % (slm_label,identity.serial_number)
            dialog.set_slm_catalog(
                ((identity.key,slm_label),),current_slm=identity.key,
            )
        dialog.set_catalog(
            sections,mappings,section_titles,descriptions,
        )
        if section:
            dialog.select_section(section)
        if current_mapping:
            dialog.select_mapping(current_mapping)
        self._refresh_observation_catalog()
        if dialog.mapping_name:
            self._refresh_management_context(dialog.section_key,dialog.mapping_name)
        self._refresh_management_availability()

    @QtCore.Slot(str)
    def open_management(self,section_key: str) -> None:
        dialog=self._management_dialog
        if dialog is None:
            identity=self.controller.runtime.identity
            slm_label=identity.display_name or identity.key
            if identity.serial_number and identity.serial_number not in slm_label:
                slm_label="%s · %s" % (slm_label,identity.serial_number)
            dialog=CalibrationManagementDialog(
                sections=(),mappings=(),
                slms=((identity.key,slm_label),),current_slm=identity.key,
                parent=self.controller.section_collection.section_view(section_key).window(),
            )
            self._management_dialog=dialog
            self._management_dialog_owned=True
            self._connect_management_view(dialog)
            dialog.destroyed.connect(self._management_destroyed)
        self.populate_management_view(section_key=section_key)
        dialog.show(); dialog.raise_(); dialog.activateWindow()

    def management_view_destroyed(
        self,dialog: CalibrationManagementDialog | None=None,
    ) -> None:
        if dialog is not None and self._management_dialog is not dialog:
            return
        self._management_leave_embedded_workflow()
        self._management_dialog=None
        self._management_dialog_owned=False

    def _management_destroyed(self,*_args: Any) -> None:
        self.management_view_destroyed()

    @QtCore.Slot()
    def _management_leave_embedded_workflow(self) -> None:
        workflow=self._embedded_workflow
        returned=self._embedded_return
        self._set_embedded_workflow(None)
        self._embedded_return=None
        if workflow is not None:
            kind,section=workflow
            if kind=="base":
                self.close_dialog(section)
            elif kind=="fov":
                self._discard_embedded_fov(section)
            elif kind=="geometry":
                self.controller.geometry_calibration.close(section,embedded=True)
            elif kind=="registration":
                plane=self._registration_plane
                if plane:
                    self.controller.observation_registration_calibration_service.clear(section,plane)
                widget=self._registration_widget
                self._registration_widget=None; self._registration_plane=None
                if widget is not None:
                    widget.hide(); widget.setParent(None); widget.deleteLater()
        if returned is not None:
            kind,section,_widget,_title,_observation=returned
            if kind=="fov":self._discard_embedded_fov(section)
            elif kind=="base":self.close_dialog(section)

    def _discard_embedded_fov(self,section_key: str) -> None:
        self.controller.fov_calibration_service.discard(section_key)
        dialog=self._fov_dialogs.pop(section_key,None)
        if dialog is not None:
            dialog.hide(); dialog.setParent(None); dialog.deleteLater()

    def _management_finish_embedded_workflow(self,section_key: str) -> None:
        dialog=self._management_dialog
        workflow=self._embedded_workflow
        if workflow is None or workflow[1]!=section_key:return
        returned=self._embedded_return
        self._embedded_return=None
        self._management_leave_embedded_workflow()
        if dialog is None:return
        if returned is not None:
            kind,section,widget,title,observation=returned
            if kind=="fov" and self.controller.fov_calibration_service.state(section) is not None:
                self._set_embedded_workflow((kind,section))
                dialog.mount_workspace(widget,title,observation=observation)
                self._render_fov_state(section)
                return
        if dialog.tabs.currentIndex()==0:
            self._refresh_management_context(dialog.section_key,dialog.mapping_name)
            dialog._show_current_mapping_inspection()
        else:
            self._refresh_observation_catalog(dialog.observation_plane_name or None)

    def _refresh_management_context(
        self,section_key: str | None=None,mapping_name: str | None=None,
    ) -> None:
        dialog=self._management_dialog
        if dialog is None:
            return
        section=str(section_key or dialog.section_key).strip()
        mapping=str(mapping_name or dialog.mapping_name).strip()
        if not section or not mapping:
            return
        try:
            calibration=None
            calibration_error=None
            try:
                calibration=self.service.saved_calibration(section,mapping)
            except Exception as error:
                calibration_error=error
            calibrated=bool(calibration is not None and calibration.is_valid())
            matrix=calibration._effective_matrix() if calibrated else None
            matrix_text=(
                "—" if matrix is None else
                "[[%.6g, %.6g], [%.6g, %.6g]]" % (
                    matrix[0][0],matrix[0][1],matrix[1][0],matrix[1][1]
                )
            )
            provenance="—"
            if calibrated:
                pieces=[calibration.source or "unspecified source"]
                if calibration.created_at: pieces.append(calibration.created_at)
                metadata=dict(calibration.metadata or {})
                observation=metadata.get("observation_plane")
                source=metadata.get("measurement_source") or metadata.get("detector")
                if observation: pieces.append("observation %s" % observation)
                if source: pieces.append("source %s" % source)
                provenance=" · ".join(str(v) for v in pieces if v)
            names=self.controller.mapping_service.fov_correction_names(section,mapping)
            default=self.controller.startup_preferences.default_fov_correction(section,mapping)
            signature=None
            if calibrated:
                try: signature=base_mapping_signature(calibration)
                except Exception: signature=None
            rows=[]
            for name in names:
                correction=self.controller.mapping_service.load_fov_correction(section,mapping,name)
                compatible=bool(signature and correction.base_mapping_signature==signature)
                prov=dict(correction.provenance or {})
                details=[]
                if prov.get("observation_plane"): details.append("observation %s" % prov["observation_plane"])
                if prov.get("detector"): details.append("detector %s" % prov["detector"])
                rows.append({
                    "name":name,
                    "compatibility":"Valid" if compatible else "Incompatible",
                    "default":name==default,
                    "provenance":" · ".join(details) or correction.created_at or "—",
                    "model":correction.model,
                    "degree":correction.degree,
                    "sample_count":correction.sample_count,
                    "rms_residual":"%.6g kxy" % correction.rms_residual_kxy,
                    "max_residual":"%.6g kxy" % correction.max_residual_kxy,
                    "coverage":"%.6g" % correction.coverage_area,
                    "created_at":correction.created_at or "—",
                    "correction":correction,
                })
            dialog.set_mapping_summary({
                "calibrated":calibrated,
                "load_error":None if calibration_error is None else str(calibration_error),
                "metrics":(
                    "—" if not calibrated else
                    "%.6g, %.6g" % (calibration.kx_per_um,calibration.ky_per_um)
                ),
                "matrix":matrix_text,
                "provenance":provenance,
                "default_fov":default,
                "fov_rows":rows,
            })
        except Exception as error:
            self._error("Calibration Manager refresh failed",error)

    def _refresh_observation_catalog(self,current: str | None=None) -> None:
        dialog=self._management_dialog
        if dialog is None:return
        planes=[]
        for plane in self.controller.observation_plane_service.list():
            planes.append({
                "name":plane.name,
                "preferred_detector":plane.preferred_detector,
                "default_pixel_size_um":plane.default_pixel_size_um,
                "description":plane.description,
                "created_at":plane.created_at,
            })
        dialog.set_observation_catalog(
            planes,detectors=self._observation_detector_suggestions(),current=current,
        )
        if dialog.observation_plane_name:
            self._refresh_observation_context(dialog.observation_plane_name)

    @QtCore.Slot(str)
    def _refresh_observation_context(self,plane_name: str) -> None:
        dialog=self._management_dialog
        plane=str(plane_name or "").strip()
        if dialog is None or not plane:return
        try:
            self.controller.observation_plane_service.get(plane)
            rows=[]
            for section in self.controller.runtime.section_keys:
                calibration=self.controller.observation_geometry_service.calibration(section,plane)
                rows.append({
                    "section":section,
                    "section_title":self.controller.section_host.section_title(section),
                    "status":"Calibrated" if calibration is not None else "Not calibrated",
                    "orientation":None if calibration is None else calibration.orientation.value,
                    "method":None if calibration is None else str((calibration.provenance or {}).get("method") or calibration.protocol or "—"),
                    "created_at":None if calibration is None else calibration.created_at or "—",
                    "details":"—" if calibration is None else "%s · %s" % (calibration.orientation.value,calibration.created_at or "saved"),
                })
            dialog.set_geometry_rows(rows)

            registration_service=self.controller.observation_registration_service
            registrations={}
            canonical_sections=set()
            canonical_frame=registration_service.canonical_frame(plane)
            if (
                canonical_frame is not None
                and canonical_frame.slm_serial==self.controller.runtime.identity.serial_number
            ):
                canonical_sections.add(canonical_frame.section_key)
            for section in self.controller.runtime.section_keys:
                registration=registration_service.registration(section,plane)
                registrations[section]=registration
            registration_rows=[]
            for section in self.controller.runtime.section_keys:
                registration=registrations.get(section)
                title=self.controller.section_host.section_title(section)
                if registration is not None:
                    canonical=registration.canonical_frame
                    canonical_title=(
                        self.controller.section_host.section_title(canonical.section_key)
                        if canonical.slm_serial==self.controller.runtime.identity.serial_number
                        and canonical.section_key in tuple(self.controller.runtime.section_keys)
                        else "%s / %s" % (canonical.slm_serial,canonical.section_key)
                    )
                    model=registration.transform_model
                    if registration.polynomial_degree is not None:model="%s %d" % (model,registration.polynomial_degree)
                    provenance=dict(registration.provenance or {})
                    details="%s → %s · %s · %d points" % (title,canonical_title,model,registration.point_count)
                    registration_rows.append({
                        "section":section,"section_title":title,"status":"Registered",
                        "canonical_section":canonical.section_key,"canonical_title":canonical_title,
                        "detector":registration.native_frame.detector_name or "—",
                        "image_shape":None if registration.native_frame.image_shape is None else "%d × %d" % registration.native_frame.image_shape,
                        "pixel_size_um":registration.native_frame.pixel_size_um,
                        "model":model,"point_count":registration.point_count,
                        "forward_rms":"%.6g" % registration.forward_transform.rms_residual,
                        "reverse_rms":"%.6g" % registration.reverse_transform.rms_residual,
                        "created_at":registration.created_at or "—",
                        "provenance":self._registration_provenance_text(provenance),
                        "details":details,
                    })
                else:
                    status="Canonical" if section in canonical_sections else "Not registered"
                    registration_rows.append({
                        "section":section,"section_title":title,"status":status,
                        "details":"Canonical frame for this observation plane." if status=="Canonical" else "No saved observation registration.",
                    })
            dialog.set_registration_rows(registration_rows)
        except Exception as error:
            self._error("Observation geometry refresh failed",error)

    @QtCore.Slot(str)
    def _management_add_mapping(self,section_key: str) -> None:
        if not self._management_mutation_allowed():return
        definition=request_mapping_definition(self._management_dialog)
        if definition is None:return
        try:
            name=self.service.add_mapping(definition)
            self.refresh_mappings()
            if self._management_dialog is not None:self._management_dialog.select_mapping(name)
        except Exception as error:
            self.refresh_mappings(); self._error("Add SLM Mapping Failed",error)

    @QtCore.Slot(object)
    def _management_create_observation(self,payload) -> None:
        if not self._management_mutation_allowed():return
        dialog=self._management_dialog
        try:
            values=dict(payload or {})
            value=self.controller.observation_plane_service.add(ObservationPlaneDefinition(**values))
            if dialog is not None:dialog.finish_observation_edit()
            self._observation_planes_changed()
            self._refresh_observation_catalog(value.name)
        except Exception as error:self._error("Add observation plane failed",error)

    @QtCore.Slot(str,object)
    def _management_update_observation(self,plane_name: str,payload) -> None:
        if not self._management_mutation_allowed():return
        dialog=self._management_dialog
        try:
            values=dict(payload or {})
            values.pop("name",None)
            self.controller.observation_plane_service.update(plane_name,**values)
            if dialog is not None:dialog.finish_observation_edit()
            self._observation_planes_changed()
            self._refresh_observation_catalog(plane_name)
        except Exception as error:self._error("Update observation plane failed",error)

    @QtCore.Slot(str)
    def _management_delete_observation(self,plane_name: str) -> None:
        if not self._management_mutation_allowed():return
        plane=str(plane_name or "").strip()
        if not plane:return
        answer=QtWidgets.QMessageBox.question(
            self._management_dialog,"Delete observation plane",
            'Delete observation plane "%s"?\n\nOrientation and observation-registration calibrations are separate resources and are not deleted.' % plane,
            QtWidgets.QMessageBox.Yes|QtWidgets.QMessageBox.No,QtWidgets.QMessageBox.No,
        )
        if answer!=QtWidgets.QMessageBox.Yes:return
        try:
            self.controller.observation_plane_service.delete(plane)
            self._observation_planes_changed(); self._refresh_observation_catalog()
        except Exception as error:self._error("Delete observation plane failed",error)

    @QtCore.Slot(str,str)
    def _management_start_base_calibration(self,section_key: str,mapping_name: str) -> None:
        if not self._management_mutation_allowed():return
        if self.service.active_mapping(section_key) != mapping_name:
            if not self.set_active_mapping(section_key,mapping_name):
                return
        self._management_leave_embedded_workflow()
        self.open_dialog(section_key,embedded=True)

    @QtCore.Slot(str,str)
    def _management_delete_mapping(self,section_key: str,mapping_name: str) -> None:
        if not self._management_mutation_allowed():return
        mapping=str(mapping_name or "").strip()
        if not mapping:
            return
        if not confirm_mapping_deletion(mapping,self._management_dialog):
            return
        try:
            deleted=self.service.delete_mapping(mapping)
            self.refresh_mappings()
            self.controller.sigInfo.emit(
                "SLM Mapping",
                'Deleted mapping "%s" and %d calibration file(s).'
                % (mapping,len(deleted)),
            )
        except Exception as error:
            self.refresh_mappings()
            self._error("Delete SLM Mapping Failed",error)

    @QtCore.Slot(str,str)
    def _management_delete_base_calibration(self,section_key: str,mapping_name: str) -> None:
        if not self._management_mutation_allowed():return
        parent=self._management_dialog
        if not confirm_calibration_deletion(mapping_name,parent):
            return
        try:
            self.service.delete_mapping_calibration(section_key,mapping_name)
            self.refresh_mappings(); self._refresh_management_context(section_key,mapping_name)
        except Exception as error:
            self._error("Delete SLM Calibration Failed",error)

    @QtCore.Slot(str,str)
    def _management_start_fov_calibration(self,section_key: str,mapping_name: str) -> None:
        if not self._management_mutation_allowed():return
        if self.service.active_mapping(section_key) != mapping_name:
            if not self.set_active_mapping(section_key,mapping_name):
                return
        self._management_leave_embedded_workflow()
        self.open_fov_calibration(section_key,mapping_name,embedded=True)

    def open_fov_calibration(self,section_key: str,mapping_name: str,*,embedded: bool=False) -> None:
        service=self.controller.fov_calibration_service
        try:
            self.controller.flush_section(section_key,propagate=True)
            state=service.ensure_reference(section_key,mapping_name)
            dialog=self._fov_dialogs.get(section_key)
            if dialog is not None and bool(getattr(dialog,"_embedded",False)) != bool(embedded):
                self.controller.fov_calibration_service.discard(section_key)
                dialog.hide(); dialog.setParent(None); dialog.deleteLater()
                self._fov_dialogs.pop(section_key,None); dialog=None
                state=service.ensure_reference(section_key,mapping_name)
            sources,current=self._fov_measurement_sources(section_key,state)
            if dialog is None:
                dialog=FOVCalibrationDialog(
                    section_key=section_key,mapping_name=mapping_name,
                    section_title=self.controller.section_host.section_title(section_key),
                    observation_planes=service.observation_plane_names(),
                    current_observation_plane=state.observation_plane_name,
                    localization_parameters=state.localization_parameters,
                    detectors=sources,current_detector=current,
                    parent=self._management_dialog,embedded=embedded,
                )
                self._fov_dialogs[section_key]=dialog
                dialog.sigObservationPlaneChanged.connect(
                    lambda plane,key=section_key:self._fov_observation_changed(key,plane)
                )
                dialog.sigAcquireRequested.connect(
                    lambda source,pixel,key=section_key:self._fov_acquire(key,source,pixel)
                )
                dialog.sigLoadRequested.connect(
                    lambda pixel,key=section_key:self._fov_load(key,pixel)
                )
                dialog.sigLocalizationRequested.connect(
                    lambda params,key=section_key:self._fov_localize(key,dict(params or {}))
                )
                dialog.sigGeometryChanged.connect(
                    lambda values,locks,key=section_key:self._fov_geometry_changed(key,values,locks)
                )
                dialog.sigSquareStraightRequested.connect(
                    lambda key=section_key:self._fov_square_straight(key)
                )
                dialog.sigFitCenterRequested.connect(lambda key=section_key:self._fov_fit_center(key))
                dialog.sigResetFromFitRequested.connect(lambda key=section_key:self._fov_reset_fit(key))
                dialog.sigFitCandidateRequested.connect(
                    lambda model,degree,key=section_key:self._fov_fit_candidate(
                        key,model,degree
                    )
                )
                dialog.sigSaveRequested.connect(
                    lambda name,default,key=section_key:self._fov_save(key,name,default)
                )
                dialog.sigGeometryCalibrationRequested.connect(
                    lambda plane,key=section_key:self._management_start_geometry_calibration(key,plane)
                )
                if not embedded:
                    dialog.finished.connect(lambda _result,key=section_key:self._fov_dialog_finished(key))
                dialog.destroyed.connect(lambda _obj=None,key=section_key:self._fov_dialogs.pop(key,None))
            else:
                dialog.configure_observation_planes(service.observation_plane_names(),state.observation_plane_name)
                dialog.configure_detectors(sources,current)
            dialog.measurement_view.set_context(state.reference.localization_context)
            dialog.measurement_view.set_parameters(state.localization_parameters,invalidate_candidate=False)
            self._render_fov_state(section_key)
            if embedded and self._management_dialog is not None:
                self._set_embedded_workflow(("fov",section_key))
                self._management_dialog.mount_workspace(
                    dialog,"FOV calibration — %s" % mapping_name,observation=False,
                )
            else:
                dialog.show(); dialog.raise_(); dialog.activateWindow()
        except Exception as error:
            self._error("FOV calibration failed",error)

    def _fov_measurement_sources(self,section_key: str,state) -> tuple[tuple[str,...],str | None]:
        service=self.controller.fov_calibration_service
        sources=service.available_sources(section_key)
        current=service.preferred_source(section_key,state.mapping_name)
        return sources,current

    def _fov_observation_changed(self,section_key: str,plane_name: str | None) -> None:
        service=self.controller.fov_calibration_service; state=service.state(section_key)
        if state is None: return
        try:
            state=service.select_observation_plane(section_key,state.mapping_name,plane_name)
            dialog=self._fov_dialogs.get(section_key)
            if dialog is not None:
                sources,current=self._fov_measurement_sources(section_key,state)
                dialog.configure_detectors(sources,current); dialog.clear_measurement()
            self._render_fov_state(section_key)
        except Exception as error: self._fov_error(section_key,error)

    def _fov_acquire(self,section_key: str,source: str,pixel_size_um: float | None) -> None:
        service=self.controller.fov_calibration_service; state=service.state(section_key)
        if state is None: return
        dialog=self._fov_dialogs.get(section_key)
        try:
            if dialog is not None: dialog.measurement_view.set_measurement_busy(True,"Acquiring…")
            service.acquire(
                section_key,state.mapping_name,source,pixel_size_um=pixel_size_um,
                on_result=lambda value,key=section_key:self.sigFovMeasurementReceived.emit(key,value),
                on_error=lambda error,key=section_key:self.sigFovMeasurementError.emit(key,error),
            )
        except Exception as error:
            if dialog is not None: dialog.measurement_view.set_measurement_busy(False)
            self._fov_error(section_key,error)

    @QtCore.Slot(str,object)
    def _fov_measurement_received(self,section_key: str,_state) -> None:
        dialog=self._fov_dialogs.get(section_key)
        if dialog is not None: dialog.measurement_view.set_measurement_busy(False)
        self._render_fov_state(section_key)

    @QtCore.Slot(str,object)
    def _fov_measurement_error(self,section_key: str,error) -> None:
        dialog=self._fov_dialogs.get(section_key)
        if dialog is not None: dialog.measurement_view.set_measurement_busy(False)
        self._fov_error(section_key,error)

    def _fov_load(self,section_key: str,pixel_size_um: float | None) -> None:
        service=self.controller.fov_calibration_service; state=service.state(section_key); dialog=self._fov_dialogs.get(section_key)
        if state is None: return
        try:
            path,_=QtWidgets.QFileDialog.getOpenFileName(
                dialog,"Select FOV calibration image","",
                "Images (*.tif *.tiff *.png *.jpg *.jpeg *.bmp);;All files (*)",
            )
            if not path: return
            image=np.asarray(Image.open(path).convert("F"),dtype=np.float64)
            measurement=create_image_measurement(
                image,source="file",pixel_size_um=pixel_size_um,
                metadata={"path":str(path),"workflow":"fov_mapping_calibration"},
            )
            service.commit_measurement(section_key,state.mapping_name,measurement,pixel_size_um=pixel_size_um)
            self._render_fov_state(section_key)
        except Exception as error: self._fov_error(section_key,error)

    def _fov_localize(self,section_key: str,parameters: Mapping[str,Any]) -> None:
        service=self.controller.fov_calibration_service; state=service.state(section_key)
        if state is None: return
        try:
            service.run_localization(section_key,state.mapping_name,parameters); self._render_fov_state(section_key)
        except Exception as error:
            self._render_fov_state(section_key); self._fov_error(section_key,error)

    def _fov_geometry_changed(self,section_key: str,values,locks) -> None:
        service=self.controller.fov_calibration_service; state=service.state(section_key)
        if state is None: return
        try:
            service.set_editable_geometry(section_key,state.mapping_name,dict(values),dict(locks)); self._render_fov_state(section_key)
        except Exception as error: self._fov_error(section_key,error)

    def _fov_square_straight(self,section_key: str) -> None:
        self._fov_state_action(section_key,"square_and_straight")

    def _fov_fit_center(self,section_key: str) -> None:
        self._fov_state_action(section_key,"fit_center")

    def _fov_reset_fit(self,section_key: str) -> None:
        self._fov_state_action(section_key,"reset_from_fit")

    def _fov_state_action(self,section_key: str,method: str) -> None:
        service=self.controller.fov_calibration_service; state=service.state(section_key)
        if state is None: return
        try:
            getattr(service,method)(section_key,state.mapping_name); self._render_fov_state(section_key)
        except Exception as error: self._fov_error(section_key,error)

    def _fov_fit_candidate(
        self,section_key: str,model: str,degree: int | None,
    ) -> None:
        service=self.controller.fov_calibration_service; state=service.state(section_key)
        if state is None: return
        try:
            service.fit_candidate(
                section_key,state.mapping_name,model=str(model),
                degree=2 if degree is None else int(degree),
            )
            self._render_fov_state(section_key)
        except Exception as error: self._fov_error(section_key,error)

    def _fov_save(self,section_key: str,name: str,set_default: bool) -> None:
        service=self.controller.fov_calibration_service; state=service.state(section_key)
        if state is None: return
        try:
            overwrite=name in self.controller.mapping_service.fov_correction_names(section_key,state.mapping_name)
            if overwrite:
                answer=QtWidgets.QMessageBox.question(
                    self._fov_dialogs.get(section_key),"Replace FOV correction",
                    'Replace existing FOV correction "%s"?' % name,
                    QtWidgets.QMessageBox.Yes|QtWidgets.QMessageBox.No,QtWidgets.QMessageBox.No,
                )
                if answer!=QtWidgets.QMessageBox.Yes: return
            service.save_candidate(section_key,state.mapping_name,name,overwrite=overwrite,set_default=set_default)
            self.controller.fov_mapping_runtime_service.refresh_fov_mapping_contexts()
            self._refresh_management_context(section_key,state.mapping_name)
            if self._embedded_workflow==("fov",section_key):
                self._management_finish_embedded_workflow(section_key)
        except Exception as error: self._fov_error(section_key,error)

    def _render_fov_state(self,section_key: str) -> None:
        service=self.controller.fov_calibration_service; state=service.state(section_key); dialog=self._fov_dialogs.get(section_key)
        if state is None or dialog is None: return
        dialog.configure_observation_planes(service.observation_plane_names(),state.observation_plane_name)
        displayed_measurement = dialog.measurement_view.measurement
        if state.measurement is None:
            if displayed_measurement is not None:
                dialog.clear_measurement()
        elif displayed_measurement is not state.measurement:
            dialog.set_measurement(state.measurement)
        if state.localization is not None:
            dialog.set_localization(state.localization,state.localization_parameters)
        dialog.set_geometry(None if state.editable_geometry is None else state.editable_geometry.to_dict(),state.locks)
        dialog.set_reference_preview(service.preview(section_key,state.mapping_name))
        dialog.set_candidate(state.candidate)
        missing=False
        if state.observation_plane_name:
            missing=self.controller.observation_geometry_service.calibration(section_key,state.observation_plane_name) is None
        dialog.set_geometry_missing(state.observation_plane_name,missing)

    def _fov_error(self,section_key: str,error: Exception) -> None:
        dialog=self._fov_dialogs.get(section_key)
        if dialog is not None: dialog.set_error(error)
        else: self._error("FOV calibration failed",error)

    def _fov_dialog_finished(self,section_key: str) -> None:
        self.controller.fov_calibration_service.discard(section_key)
        self._fov_dialogs.pop(section_key,None)

    @QtCore.Slot(str,str,str,str,object)
    def _management_refit_fov(
        self,section_key: str,mapping_name: str,name: str,
        model: str,degree,
    ) -> None:
        if not self._management_mutation_allowed():return
        try:
            correction=self.controller.mapping_service.refit_fov_correction(
                section_key,mapping_name,name,model=str(model),
                degree=None if degree is None else int(degree),
            )
            if self._management_dialog is not None:
                self._management_dialog.set_fov_refit_preview(correction)
        except Exception as error:
            self._error("Refit FOV correction failed",error)

    @QtCore.Slot(str,str,str,object)
    def _management_update_fov(
        self,section_key: str,mapping_name: str,name: str,correction,
    ) -> None:
        if not self._management_mutation_allowed():return
        try:
            if correction is None or str(getattr(correction,"name","") or "")!=str(name):
                raise RuntimeError("Refit the selected FOV correction before saving an update.")
            self.controller.mapping_service.save_fov_correction(correction,overwrite=True)
            self.controller.fov_mapping_runtime_service.refresh_fov_mapping_contexts()
            self._refresh_management_context(section_key,mapping_name)
            if self._management_dialog is not None:
                self._management_dialog.select_fov(name)
            self.controller.sigInfo.emit(
                "FOV Calibration",'Updated FOV correction "%s" from its saved samples.' % name,
            )
        except Exception as error:
            self._error("Update FOV correction failed",error)

    @QtCore.Slot(str,str,str,str)
    def _management_rename_fov(
        self,section_key: str,mapping_name: str,old_name: str,new_name: str,
    ) -> None:
        if not self._management_mutation_allowed():return
        old=str(old_name or "").strip(); new=str(new_name or "").strip()
        if not old or not new or old==new:return
        try:
            if old.casefold()==new.casefold():
                raise ValueError("Case-only FOV correction renames are not supported.")
            mappings=self.controller.mapping_service
            was_default=(
                self.controller.startup_preferences.default_fov_correction(
                    section_key,mapping_name
                )==old
            )
            mappings.rename_fov_correction(section_key,mapping_name,old,new)
            try:
                if was_default:
                    self.controller.startup_preferences.set_default_fov_correction(
                        section_key,mapping_name,new
                    )
            except Exception:
                try:mappings.rename_fov_correction(section_key,mapping_name,new,old)
                except Exception:pass
                raise
            self.controller.fov_mapping_runtime_service.refresh_fov_mapping_contexts()
            self._refresh_management_context(section_key,mapping_name)
            if self._management_dialog is not None:
                self._management_dialog.select_fov(new)
            self.controller.sigInfo.emit(
                "FOV Calibration",'Renamed FOV correction "%s" to "%s".' % (old,new),
            )
        except Exception as error:
            self._error("Rename FOV correction failed",error)

    @QtCore.Slot(str,str,str)
    def _management_set_fov_default(self,section_key: str,mapping_name: str,name: str) -> None:
        if not self._management_mutation_allowed():return
        try:
            calibration=self.service.saved_calibration(section_key,mapping_name)
            correction=self.controller.mapping_service.load_fov_correction(section_key,mapping_name,name)
            if not calibration.is_valid() or correction.base_mapping_signature != base_mapping_signature(calibration):
                raise RuntimeError("Incompatible FOV corrections cannot be set as default.")
            self.controller.startup_preferences.set_default_fov_correction(section_key,mapping_name,name)
            self.controller.fov_mapping_runtime_service.refresh_fov_mapping_contexts()
            self._refresh_management_context(section_key,mapping_name)
        except Exception as error:
            self._error("Set default FOV correction failed",error)

    @QtCore.Slot(str,str,str)
    def _management_delete_fov(self,section_key: str,mapping_name: str,name: str) -> None:
        if not self._management_mutation_allowed():return
        answer=QtWidgets.QMessageBox.question(
            self._management_dialog,"Delete FOV correction",
            'Delete FOV correction "%s"?' % name,
            QtWidgets.QMessageBox.Yes|QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        try:
            self.controller.mapping_service.delete_fov_correction(section_key,mapping_name,name)
            if self.controller.startup_preferences.default_fov_correction(section_key,mapping_name)==name:
                self.controller.startup_preferences.set_default_fov_correction(section_key,mapping_name,None)
            self.controller.fov_mapping_runtime_service.refresh_fov_mapping_contexts()
            self._refresh_management_context(section_key,mapping_name)
        except Exception as error:
            self._error("Delete FOV correction failed",error)

    @staticmethod
    def _registration_provenance_text(provenance: Mapping[str,Any]) -> str:
        if not provenance:return "—"
        preferred=("workflow","correspondence","sample_policy","native_measurement_id","canonical_reference_name")
        parts=["%s=%s" % (key,provenance[key]) for key in preferred if key in provenance]
        return "; ".join(parts) if parts else str(dict(provenance))

    @QtCore.Slot(str,str)
    def _management_start_observation_registration(self,section_key: str,plane_name: str) -> None:
        if not self._management_mutation_allowed():return
        manager=self._management_dialog
        if manager is None:return
        registration_service=self.controller.observation_registration_service
        canonical_frame=registration_service.canonical_frame(plane_name)
        if (
            canonical_frame is not None
            and canonical_frame.slm_serial==self.controller.runtime.identity.serial_number
            and canonical_frame.section_key==section_key
        ):
            self._error(
                "Observation registration",
                RuntimeError("The canonical section defines the observation-plane coordinate frame and does not require a registration."),
            )
            return
        calibration=self.controller.observation_registration_calibration_service
        references=calibration.reference_records(section_key,plane_name)
        saved_registration=registration_service.registration(section_key,plane_name)
        if not references and saved_registration is None:
            self._error(
                "Observation registration",
                RuntimeError(
                    "No saved/session position reference is available for this observation plane. "
                    "Localize the canonical pattern and create a position reference first."
                ),
            )
            return
        self._management_leave_embedded_workflow()
        try:
            initial=calibration.context(section_key,plane_name)
            selected=initial.canonical_reference_name
        except Exception:
            selected=references[0].name if references else ""
        widget=ObservationRegistrationCalibrationWidget(
            native_section_key=section_key,
            native_section_title=self.controller.section_host.section_title(section_key),
            observation_plane_name=plane_name,references=references,
            canonical_reference_name=selected,parent=manager,
        )
        self._registration_widget=widget; self._registration_plane=plane_name
        widget.sigReferenceChanged.connect(
            lambda _reference,key=section_key,plane=plane_name:self._render_observation_registration(key,plane)
        )
        widget.sigUseLatestRequested.connect(
            lambda key=section_key,plane=plane_name:self._registration_use_latest(key,plane)
        )
        widget.sigLocalizeRequested.connect(
            lambda key=section_key,plane=plane_name:self._registration_localize(key,plane)
        )
        widget.sigCorrespondenceChanged.connect(
            lambda correspondence,key=section_key,plane=plane_name:self._registration_correspondence_changed(
                key,plane,correspondence
            )
        )
        widget.sigFitRequested.connect(
            lambda model,degree,key=section_key,plane=plane_name:self._registration_fit(key,plane,model,degree)
        )
        widget.sigSaveRequested.connect(lambda key=section_key,plane=plane_name:self._registration_save(key,plane))
        widget.sigNewCalibrationRequested.connect(
            lambda key=section_key,plane=plane_name:self._registration_start_new(key,plane)
        )
        widget.sigCancelRequested.connect(lambda key=section_key,plane=plane_name:self._registration_cancel(key,plane))
        widget.sigRefreshRequested.connect(lambda key=section_key,plane=plane_name:self._render_observation_registration(key,plane,quiet=True))
        self._set_embedded_workflow(("registration",section_key))
        manager.mount_workspace(
            widget,"Observation registration — %s / %s" % (plane_name,self.controller.section_host.section_title(section_key)),
            observation=True,
        )
        self._render_observation_registration(section_key,plane_name)

    def _render_observation_registration(self,section_key: str,plane_name: str,*,quiet: bool=False) -> None:
        widget=self._registration_widget
        if widget is None or self._embedded_workflow!=("registration",section_key):return
        reference=widget.canonical_reference_name
        try:
            service=self.controller.observation_registration_calibration_service
            try:
                context=service.context(section_key,plane_name,reference or None)
            except FileNotFoundError:
                # A session reference may be deleted while this workspace is
                # open. Fall back to the remaining reference catalog instead
                # of leaving the combo pinned to stale state.
                context=service.context(section_key,plane_name)
            widget.set_context(context)
        except Exception as error:
            if not quiet:widget.set_error(error)

    def _registration_use_latest(self,section_key: str,plane_name: str) -> None:
        widget=self._registration_widget
        if widget is None:return
        try:
            self.controller.observation_registration_calibration_service.use_latest_localization(
                section_key,plane_name,widget.canonical_reference_name,
            )
            widget.clear_error(); self._render_observation_registration(section_key,plane_name)
        except Exception as error:widget.set_error(error)

    def _registration_localize(self,section_key: str,plane_name: str) -> None:
        widget=self._registration_widget
        if widget is None:return
        try:
            self.controller.observation_registration_calibration_service.request_localization(
                section_key,plane_name,widget.canonical_reference_name,
            )
            widget.clear_error(); self._render_observation_registration(section_key,plane_name,quiet=True)
        except Exception as error:widget.set_error(error)


    def _registration_correspondence_changed(
        self,section_key: str,plane_name: str,correspondence: str,
    ) -> None:
        widget=self._registration_widget
        if widget is None:return
        try:
            self.controller.observation_registration_calibration_service.set_correspondence(
                section_key,plane_name,widget.canonical_reference_name,correspondence,
            )
            widget.clear_error(); self._render_observation_registration(section_key,plane_name)
        except Exception as error:widget.set_error(error)

    def _registration_fit(self,section_key: str,plane_name: str,model: str,degree: int) -> None:
        widget=self._registration_widget
        if widget is None:return
        try:
            self.controller.observation_registration_calibration_service.fit(
                section_key,plane_name,widget.canonical_reference_name,model=model,polynomial_degree=degree,
            )
            widget.clear_error(); self._render_observation_registration(section_key,plane_name)
        except Exception as error:widget.set_error(error)

    def _registration_save(self,section_key: str,plane_name: str) -> None:
        widget=self._registration_widget
        if widget is None:return
        try:
            self.controller.observation_registration_calibration_service.save(
                section_key,plane_name,widget.canonical_reference_name,
            )
            widget.clear_error(); self._refresh_observation_context(plane_name)
            # Stay in the workspace: after save the service switches to saved-edit
            # mode so the persisted result remains inspectable/refittable immediately.
            self._render_observation_registration(section_key,plane_name)
        except Exception as error:widget.set_error(error)

    def _registration_start_new(self,section_key: str,plane_name: str) -> None:
        widget=self._registration_widget
        if widget is None:return
        try:
            self.controller.observation_registration_calibration_service.start_new_calibration(
                section_key,plane_name,
            )
            widget.clear_error(); self._render_observation_registration(section_key,plane_name)
        except Exception as error:widget.set_error(error)

    def _registration_cancel(self,section_key: str,plane_name: str) -> None:
        self.controller.observation_registration_calibration_service.cancel(section_key,plane_name)
        self._management_finish_embedded_workflow(section_key)

    @QtCore.Slot(str,str)
    def _management_delete_observation_registration(self,section_key: str,plane_name: str) -> None:
        if not self._management_mutation_allowed():return
        registration=self.controller.observation_registration_service.registration(section_key,plane_name)
        if registration is None:return
        answer=QtWidgets.QMessageBox.question(
            self._management_dialog,"Delete observation registration",
            'Delete observation registration for "%s" in "%s"?' % (self.controller.section_host.section_title(section_key),plane_name),
            QtWidgets.QMessageBox.Yes|QtWidgets.QMessageBox.No,QtWidgets.QMessageBox.No,
        )
        if answer!=QtWidgets.QMessageBox.Yes:return
        try:
            self.controller.observation_registration_calibration_service.delete(section_key,plane_name)
            self._refresh_observation_context(plane_name)
        except Exception as error:self._error("Delete observation registration failed",error)

    @QtCore.Slot(str,str)
    def _management_start_geometry_calibration(self,section_key: str,plane_name: str) -> None:
        if not self._management_mutation_allowed():return
        manager=self._management_dialog
        if manager is None:
            self.controller.geometry_calibration.open(section_key,plane_name)
            return
        if self._embedded_workflow==("fov",section_key):
            fov=self._fov_dialogs.get(section_key)
            state=self.controller.fov_calibration_service.state(section_key)
            title="FOV calibration" if state is None else "FOV calibration — %s" % state.mapping_name
            if fov is not None:
                self._embedded_return=("fov",section_key,fov,title,False)
                self._set_embedded_workflow(None)
        else:
            self._management_leave_embedded_workflow()
        widget=self.controller.geometry_calibration.open(
            section_key,plane_name,parent=manager,embedded=True,
        )
        self._set_embedded_workflow(("geometry",section_key))
        observation=manager.tabs.currentIndex()==1
        manager.mount_workspace(
            widget,"Geometry calibration — %s / %s" % (
                plane_name,self.controller.section_host.section_title(section_key)
            ),observation=observation,
        )

    @QtCore.Slot(str,str)
    def _geometry_calibration_saved(self,section_key: str,plane_name: str) -> None:
        manager=self._management_dialog
        if manager is not None:
            self._refresh_management_context(section_key,manager.mapping_name)
            self._refresh_observation_context(plane_name)
        dialog=self._fov_dialogs.get(section_key)
        if dialog is not None:
            self._render_fov_state(section_key)
        if self._embedded_workflow==("geometry",section_key):
            self._management_finish_embedded_workflow(section_key)

    @QtCore.Slot()
    def open_observation_planes_manager(self) -> None:
        dialog=self._observation_planes_dialog
        if dialog is None:
            dialog=ObservationPlanesManagerDialog(
                self.controller.observation_plane_service,
                detectors=self._observation_detector_suggestions(),
                parent=None if self._management_dialog is None else self._management_dialog,
            )
            self._observation_planes_dialog=dialog
            dialog.sigChanged.connect(self._observation_planes_changed)
            dialog.destroyed.connect(lambda *_args:setattr(self,"_observation_planes_dialog",None))
        else:
            dialog.configure_detectors(self._observation_detector_suggestions())
            dialog.refresh()
        dialog.show(); dialog.raise_(); dialog.activateWindow()

    def define_observation_plane(
        self,*,section_key: str | None=None,parent: QtWidgets.QWidget | None=None,
    ) -> str | None:
        dialog=ObservationPlaneEditorDialog(
            detectors=self._observation_detector_suggestions(section_key),parent=parent,
        )
        if dialog.exec_()!=QtWidgets.QDialog.Accepted:
            return None
        try:
            value=self.controller.observation_plane_service.add(dialog.definition())
            self._observation_planes_changed()
            return value.name
        except Exception as error:
            self._error("Add observation plane failed",error)
            return None

    def _observation_planes_changed(self) -> None:
        valid={plane.name for plane in self.controller.observation_plane_service.list()}
        for section in tuple(self.controller.runtime.section_keys):
            current=self.controller.feedback_service.active_observation_plane(section)
            if current is not None and current not in valid:
                self.controller.feedback_service.select_observation_plane(section,None)
            state=self.service.target_state(section)
            if state is not None and state.observation_plane_name not in valid:
                self.service.select_target_observation_plane(section,None)
        for section,dialog in tuple(self._dialogs.items()):
            state=self.service.ensure_target_context(section)
            names,selected,default_pixel_size=self._observation_context(section,state)
            dialog.configure_observation_planes(names,selected,default_pixel_size)
        self.controller._feedback.refresh_observation_planes()
        for section,dialog in tuple(self._fov_dialogs.items()):
            state=self.controller.fov_calibration_service.state(section)
            current=None if state is None else state.observation_plane_name
            dialog.configure_observation_planes(tuple(sorted(valid)),current)
        if self._management_dialog is not None:
            self._refresh_management_context(
                self._management_dialog.section_key,self._management_dialog.mapping_name,
            )
            self._refresh_observation_catalog(self._management_dialog.observation_plane_name or None)

    # ------------------------------------------------------------------
    # Calibration dialog / target workflow presentation
    # ------------------------------------------------------------------

    @QtCore.Slot(str)
    def open_dialog(self,section_key: str,*,embedded: bool=False) -> None:
        if (
            not self.controller.editor_writes_allowed
            or self.controller.automatic_operation_active
        ):
            return
        mapping = self.service.active_mapping(section_key)
        if not mapping:
            self._error(
                "SLM Section Calibration",
                RuntimeError("Select or add a mapping before saving calibration."),
            )
            return
        try:
            self.service.mapping_definition(mapping)
            state = self.service.ensure_target_context(section_key)
        except Exception as error:
            self._error("SLM Section Calibration",error)
            return
        observation_planes,current_observation,default_pixel_size = (
            self._observation_context(section_key,state)
        )
        detectors,current_detector = self._measurement_sources(
            section_key,preferred=state.source,
        )

        dialog = self._dialogs.get(section_key)
        if dialog is not None and bool(getattr(dialog,"_embedded",False)) != bool(embedded):
            self.close_dialog(section_key)
            dialog=None
        if dialog is None:
            dialog = CalibrationDialog(
                mapping_name=mapping,
                section_key=section_key,
                section_title=self.controller.section_host.section_title(section_key),
                observation_planes=observation_planes,
                current_observation_plane=current_observation,
                observation_default_pixel_size_um=default_pixel_size,
                localization_parameters=_default_localization_parameters(),
                detectors=detectors,
                current_detector=current_detector,
                title="Calibration - %s/%s" % (
                    self.display_name,self.controller.section_host.section_title(section_key),
                ),
                parent=(self._management_dialog if embedded else self.controller.section_collection.section_view(section_key)),
                embedded=embedded,
            )
            self._dialogs[section_key] = dialog
            dialog.sigAcquireRequested.connect(
                lambda source,key=section_key:self.acquire_target_measurement(key,source)
            )
            dialog.sigLoadRequested.connect(
                lambda key=section_key:self.load_target_measurement(key)
            )
            dialog.sigObservationPlaneChanged.connect(
                lambda plane,key=section_key:self._on_observation_plane_changed(
                    key,plane,
                )
            )
            dialog.sigDefineObservationPlaneRequested.connect(
                lambda key=section_key:self._define_observation_for_calibration(key)
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
            if not embedded:
                dialog.finished.connect(
                    lambda _result,key=section_key:self._dialog_finished(key)
                )
            dialog.destroyed.connect(
                lambda _obj=None,key=section_key:self._dialog_destroyed(key)
            )
        else:
            dialog.set_mapping_name(mapping)
            dialog.configure_observation_planes(
                observation_planes,current_observation,default_pixel_size,
            )
            dialog.configure_detectors(detectors,current_detector)

        self.render_target_state(section_key)
        self._refresh_live_acquisition(section_key)
        if embedded and self._management_dialog is not None:
            self._set_embedded_workflow(("base",section_key))
            self._management_dialog.mount_workspace(
                dialog,"Base calibration — %s" % mapping,observation=False,
            )
        else:
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()

    def _measurement_sources(
        self,section_key: str,*,preferred: str | None=None,
    ) -> tuple[tuple[str,...],str | None]:
        measurements = self.service.measurements
        if measurements is None or not measurements.available:
            return (),None
        try:
            sources = tuple(
                name for name in (str(value).strip() for value in measurements.available_sources(section_key))
                if name
            )
        except Exception:
            sources = ()
        preferred = str(preferred or "").strip()
        if preferred and preferred in sources:
            return sources,preferred
        try:
            current = measurements.preferred_source(section_key,sources)
        except Exception:
            current = None
        current = str(current or "").strip() or None
        if current not in sources:
            current = sources[0] if sources else None
        return sources,current

    def _observation_detector_suggestions(
        self,section_key: str | None=None,
    ) -> tuple[str,...]:
        sections=(str(section_key),) if section_key else tuple(self.controller.runtime.section_keys)
        names=[]
        seen=set()
        for section in sections:
            sources,_current=self._measurement_sources(section)
            for source in sources:
                if source not in seen:
                    seen.add(source)
                    names.append(source)
        return tuple(names)

    def _observation_context(
        self,section_key: str,state: TargetCalibrationState | None=None,
    ) -> tuple[tuple[str,...],str | None,float | None]:
        state = state or self.service.ensure_target_context(section_key)
        names = self.service.observation_plane_names()
        selected = state.observation_plane_name
        default_pixel_size = None
        if selected is not None:
            default_pixel_size = self.service.observation_planes.get(
                selected
            ).default_pixel_size_um
        return names,selected,default_pixel_size

    def _define_observation_for_calibration(self,section_key: str) -> None:
        dialog=self._dialogs.get(section_key)
        name=self.define_observation_plane(section_key=section_key,parent=dialog)
        if name is not None:
            self._on_observation_plane_changed(section_key,name)
            return
        if dialog is not None:
            state=self.service.ensure_target_context(section_key)
            names,selected,default_pixel_size=self._observation_context(section_key,state)
            dialog.configure_observation_planes(names,selected,default_pixel_size)

    def _on_observation_plane_changed(
        self,section_key: str,observation_plane_name: str | None,
    ) -> None:
        if not self.controller.editor_writes_allowed:
            return
        dialog = self._dialogs.get(section_key)
        try:
            changed = self.service.select_target_observation_plane(
                section_key,observation_plane_name,
            )
            state = self.service.ensure_target_context(section_key)
            names,selected,default_pixel_size = self._observation_context(
                section_key,state,
            )
            detectors,current = self._measurement_sources(
                section_key,preferred=state.source,
            )
            if dialog is not None:
                if changed:
                    dialog.target_view.clear_measurement()
                    dialog.clear_target_calibration_candidate()
                    dialog.clear_target_pixel_size_override()
                dialog.configure_observation_planes(
                    names,selected,default_pixel_size,
                )
                dialog.configure_detectors(detectors,current)
            self.render_target_state(section_key)
            self._refresh_live_acquisition(section_key)
        except Exception as error:
            if dialog is not None:
                dialog.set_target_measurement_error(error)
            else:
                self._error("Observation-plane selection failed",error)

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
        for section_key,dialog in tuple(self._fov_dialogs.items()):
            self.controller.fov_calibration_service.discard(section_key)
            dialog.close(); dialog.deleteLater()
        self._fov_dialogs.clear()

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

    def acquire_target_measurement(
        self,section_key: str,detector: str | None=None,
    ) -> None:
        if not self.controller.editor_writes_allowed:
            return
        dialog = self._dialogs.get(section_key)
        try:
            self.controller.flush_section(section_key,propagate=True)
            self.service.ensure_target_reference(section_key)
            pixel_size_um = None if dialog is None else dialog.target_pixel_size_um
            self.service.acquire_target_measurement(
                section_key,detector,pixel_size_um=pixel_size_um,
            )
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
                    "mapping_name":self.service.active_mapping(section_key),
                    "observation_plane":state.observation_plane_name,
                    "calibration_method":TARGET_LOCALIZATION_METHOD,
                },
            )
            pixel_size_um = None if dialog is None else dialog.target_pixel_size_um
            self.service.commit_target_measurement(
                section_key,measurement,source="file",pixel_size_um=pixel_size_um,
            )
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
            pixel_size_um = None if dialog is None else dialog.target_pixel_size_um
            self.service.run_target_localization(
                section_key,parameters,pixel_size_um=pixel_size_um,
            )
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
        names,selected,default_pixel_size = self._observation_context(
            section_key,state,
        )
        dialog.configure_observation_planes(
            names,selected,default_pixel_size,
        )
        detectors,current = self._measurement_sources(
            section_key,preferred=state.source,
        )
        dialog.configure_detectors(detectors,current)
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
        mapping = str(values.get("mapping_name") or "").strip()
        if not mapping:
            self._error(
                "SLM Section Calibration",
                RuntimeError("Calibration request is not bound to a mapping."),
            )
            return
        try:
            self.controller.flush_section(section_key,propagate=True)
            if method == TARGET_LOCALIZATION_METHOD:
                value = self.service.apply_target_candidate(section_key,mapping)
            elif method == CalibrationDialog.LINEAR_PHASE:
                calibration = self.service.linear_phase_calibration(
                    period_x_px=values.get("period_x_px"),
                    measured_dx_um=values.get("measured_dx_um"),
                    period_y_px=values.get("period_y_px"),
                    measured_dy_um=values.get("measured_dy_um"),
                )
                value = self.service.apply_and_save_calibration(
                    section_key,mapping,calibration,
                )
            else:
                raise ValueError("Unknown calibration method %r" % method)
            self.refresh_mappings()
            self._refresh_management_context(section_key,mapping)
            if self._embedded_workflow==("base",section_key):
                self._management_finish_embedded_workflow(section_key)
            self.controller.sigInfo.emit(
                "SLM Section Calibration",
                "Saved calibration for %s/%s (%s):\n"
                "kx_per_um = %.6g\nky_per_um = %.6g"
                % (
                    self.display_name,section_key,mapping,
                    value.kx_per_um,value.ky_per_um,
                ),
            )
        except Exception as error:
            self.controller._restore_section(section_key)
            self.refresh_mappings()
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
        if self._management_dialog is not None:
            dialog=self._management_dialog
            if self._management_dialog_owned:
                self.detach_management_view()
                dialog.close()
            else:
                self._management_leave_embedded_workflow()
                dialog.finish_observation_edit()
        if self._observation_planes_dialog is not None:
            self._observation_planes_dialog.close(); self._observation_planes_dialog=None
        self._disconnect_collection()

    def runtime_replaced(self) -> None:
        self._connect_collection()
        self.refresh_mappings()

    @QtCore.Slot(bool)
    def _on_automatic_operation_changed(self,active: bool) -> None:
        if active:
            self.close_all_dialogs()
        self.refresh_mappings()
        self.refresh_live_acquisition_all()
        self._refresh_management_availability()

    def control_mode_changed(self) -> None:
        self.refresh_mappings()
        self.refresh_live_acquisition_all()
        self._refresh_management_availability()

    def dispose(self) -> None:
        if self._disposed:
            return
        self.close_all_dialogs()
        if self._management_dialog is not None:
            dialog=self._management_dialog
            owned=self._management_dialog_owned
            self.detach_management_view()
            if owned:
                dialog.close()
        if self._observation_planes_dialog is not None:
            self._observation_planes_dialog.close(); self._observation_planes_dialog=None
        self.set_shared_workspace_opener(None)
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
