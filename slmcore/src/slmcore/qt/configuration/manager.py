from __future__ import annotations

import os

from qtpy import QtCore,QtGui,QtWidgets

from ...application.configuration import (
    CalibrationMismatchPolicy,CorrectionMismatchPolicy,PreparedConfigLoad,
)
from ...workspace.config_store import SLMConfigMetadata
from ..calibration.geometry_dialogs import (
    CalibrationMismatchDecision,calibration_mismatch_decision,
    confirm_destructive_change,
)
from .controls import ConfigControls
from .dialogs import (
    confirm_delete,request_name,request_save_as,request_update_info,
    show_config_inspection,
)


class QtConfigurationManager(QtCore.QObject):
    """Qt presentation/coordinator for application-owned configuration state."""

    def __init__(
        self,
        controller,
        *,
        controls: ConfigControls | None,
        startup_preferences=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.controls = controls
        self.startup_preferences = startup_preferences
        self._disposed = False
        self._connect_controls()
        self.refresh()
        self.synchronize_current_config()

    @property
    def current_config_path(self) -> str | None:
        return self.controller.current_config_path

    def _connect_controls(self) -> None:
        controls = self.controls
        if controls is None:
            return
        controls.sigSaveAsRequested.connect(self._save_as_requested)
        controls.sigUpdateRequested.connect(self._update_requested)
        controls.sigRenameRequested.connect(self._rename_requested)
        controls.sigDuplicateRequested.connect(self._duplicate_requested)
        controls.sigDeleteRequested.connect(self._delete_requested)
        controls.sigSetStartupRequested.connect(self._set_startup_requested)
        controls.sigOpenFolderRequested.connect(self._open_folder_requested)
        controls.sigInspectRequested.connect(self._inspect_requested)

    def _disconnect_controls(self) -> None:
        controls = self.controls
        if controls is None:
            return
        pairs = (
            (controls.sigSaveAsRequested,self._save_as_requested),
            (controls.sigUpdateRequested,self._update_requested),
            (controls.sigRenameRequested,self._rename_requested),
            (controls.sigDuplicateRequested,self._duplicate_requested),
            (controls.sigDeleteRequested,self._delete_requested),
            (controls.sigSetStartupRequested,self._set_startup_requested),
            (controls.sigOpenFolderRequested,self._open_folder_requested),
            (controls.sigInspectRequested,self._inspect_requested),
        )
        for signal,slot in pairs:
            try: signal.disconnect(slot)
            except (RuntimeError,TypeError): pass

    def refresh(self) -> None:
        if self.controls is None or not self.controller.configuration_available:
            return
        entries = [
            (item.name,str(item.path),_tooltip(item))
            for item in self.controller.list_configs()
        ]
        self.controls.set_available_configs(entries)

    def synchronize_current_config(self) -> None:
        if self.controls is None:
            return
        metadata = self.controller.current_config_metadata
        self.controls.set_current_config(
            None if metadata is None else _metadata_dict(metadata)
        )

    def resolve_calibration_policy(
        self,
        prepared: PreparedConfigLoad,
        requested="prompt",
        *,
        title: str="Load SLM config",
    ) -> CalibrationMismatchPolicy | None:
        mismatches = prepared.calibration_mismatches
        if not mismatches:
            return CalibrationMismatchPolicy.REJECT
        policy_text = str(requested or "prompt").strip().lower()
        if policy_text != "prompt":
            return CalibrationMismatchPolicy.normalize(policy_text)
        if self.controls is None:
            return CalibrationMismatchPolicy.REJECT
        decision = calibration_mismatch_decision(
            self.controls,
            title=title,
            message=(
                "Some calibrations in this config were measured with a different "
                "section geometry. You may keep them intentionally, clear them, "
                "or cancel loading."
                + (
                    " The config will also replace the current section layout."
                    if prepared.layout_changed else ""
                )
            ),
            mismatches=mismatches,
            allow_clear=True,
        )
        if decision is CalibrationMismatchDecision.CANCEL:
            return None
        if decision is CalibrationMismatchDecision.CLEAR:
            return CalibrationMismatchPolicy.CLEAR
        return CalibrationMismatchPolicy.KEEP

    def resolve_correction_policy(
        self,
        prepared: PreparedConfigLoad,
        requested="prompt",
        *,
        title: str="Load SLM config",
    ) -> CorrectionMismatchPolicy | None:
        mismatches = prepared.correction_mismatches
        if not mismatches:
            return CorrectionMismatchPolicy.REJECT
        policy_text = str(requested or "prompt").strip().lower()
        if policy_text != "prompt":
            return CorrectionMismatchPolicy.normalize(policy_text)
        if self.controls is None:
            return CorrectionMismatchPolicy.REJECT

        lines = [
            "Saved corrections differ from the corrections available in the "
            "current workspace.",
            "",
        ]
        for mismatch in mismatches:
            lines.append("• " + mismatch.summary())
        lines.extend((
            "",
            "Use saved keeps the historical correction snapshot pinned while editing. "
            "Use current reconstructs with the corrections available now.",
        ))
        box = QtWidgets.QMessageBox(self.controls)
        box.setWindowTitle(title)
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setText("\n".join(lines))
        saved_button = box.addButton("Use saved",QtWidgets.QMessageBox.AcceptRole)
        current_button = box.addButton("Use current",QtWidgets.QMessageBox.ActionRole)
        cancel_button = box.addButton(QtWidgets.QMessageBox.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is saved_button:
            return CorrectionMismatchPolicy.USE_SAVED
        if clicked is current_button:
            return CorrectionMismatchPolicy.USE_CURRENT
        if clicked is cancel_button:
            return None
        return None

    def load(
        self,
        path: str,
        *,
        confirm_layout_change: bool=True,
        calibration_mismatch_policy: str="prompt",
        correction_mismatch_policy: str="prompt",
        show_error: bool=True,
        require_complete: bool=False,
    ) -> bool:
        if not self.controller.configuration_available:
            if show_error:
                self.controller._emit_exception(
                    "SLM config load failed",
                    RuntimeError("Configuration storage is not configured."),
                )
            return False
        if self.controller.automatic_operation_active:
            return False
        try:
            prepared = self.controller.prepare_config_load(path)
            policy = self.resolve_calibration_policy(
                prepared,calibration_mismatch_policy,
            )
            if policy is None:
                self._restore_current_selection()
                return False
            correction_policy = self.resolve_correction_policy(
                prepared,correction_mismatch_policy,
            )
            if correction_policy is None:
                self._restore_current_selection()
                return False
            if (
                prepared.layout_changed
                and not prepared.calibration_mismatches
                and confirm_layout_change
                and not confirm_destructive_change(
                    self.controls,
                    "Load SLM config with different layout",
                    "Loading this config will replace the SLM section layout, clear "
                    "current pending work, and rebuild the section UI. Continue?",
                )
            ):
                self._restore_current_selection()
                return False

            self.controller.prepare_config_commit(
                layout_changed=prepared.layout_changed,
            )
            self.controller.apply_prepared_config_load(
                prepared,
                calibration_mismatch_policy=policy,
                correction_mismatch_policy=correction_policy,
                require_complete=bool(require_complete),
            )
            self.synchronize_current_config()
            self.refresh()
            return True
        except Exception as error:
            self._restore_current_selection()
            if show_error:
                self.controller._emit_exception("SLM config load failed",error)
            else:
                self.controller.sigStatusChanged.emit(str(error),True)
            return False

    def _restore_current_selection(self) -> None:
        self.synchronize_current_config()

    def save(self,name: str,info: str="",*,overwrite: bool=False):
        self._require_editor_mode()
        if not self.controller.configuration_available:
            raise RuntimeError("Configuration storage is not configured")
        self.controller.flush_all(propagate=True)
        metadata = self.controller.save_application_config(
            name,info,overwrite=overwrite,
        )
        self.synchronize_current_config()
        self.refresh()
        return metadata

    @QtCore.Slot()
    def _save_as_requested(self) -> None:
        if not self.controller.editor_writes_allowed or self.controls is None:
            return
        result = request_save_as(self.controls,self.controls.existing_stems())
        if result is None:
            return
        name,info = result
        try:
            self.save(name,info,overwrite=False)
        except Exception as error:
            self.controller._emit_exception("Error Saving Configuration",error)

    @QtCore.Slot(str)
    def _update_requested(self,path: str) -> None:
        if not self.controller.editor_writes_allowed:
            return
        try:
            if self.controls is None:
                return
            self.controller.flush_all(propagate=True)
            changes = self.controller.compare_config(path)
            metadata = self.controller.read_config_metadata(path)
            info = request_update_info(
                self.controls,metadata.name,changes,metadata.info,
            )
            if info is None:
                return
            self.save(metadata.name,info,overwrite=True)
        except Exception as error:
            self.controller._emit_exception("Error Updating Configuration",error)

    @QtCore.Slot(str)
    def _rename_requested(self,path: str) -> None:
        if not self.controller.editor_writes_allowed or self.controls is None:
            return
        old = os.path.basename(path)
        new = request_name(self.controls,"Rename config %s" % old,"New name:",old)
        if not new or new == old:
            return
        try:
            was_startup = self._is_startup(path)
            metadata = self.controller.rename_config(path,new,overwrite=False)
            if was_startup and self.startup_preferences is not None:
                self.startup_preferences.set_startup_config(metadata.name)
            self.synchronize_current_config()
            self.refresh()
        except Exception as error:
            self.controller._emit_exception("Error Renaming Configuration",error)

    @QtCore.Slot(str)
    def _duplicate_requested(self,path: str) -> None:
        if not self.controller.editor_writes_allowed or self.controls is None:
            return
        old = os.path.basename(path)
        suggested = os.path.splitext(old)[0] + "_copy"
        new = request_name(self.controls,"Duplicate config %s" % old,"New name:",suggested)
        if not new:
            return
        try:
            self.controller.duplicate_config(path,new,overwrite=False)
            self.refresh()
        except Exception as error:
            self.controller._emit_exception("Error Duplicating Configuration",error)

    @QtCore.Slot(str)
    def _delete_requested(self,path: str) -> None:
        if not self.controller.editor_writes_allowed or self.controls is None:
            return
        if not confirm_delete(self.controls,os.path.basename(path)):
            return
        try:
            was_startup = self._is_startup(path)
            self.controller.delete_config(path)
            if was_startup and self.startup_preferences is not None:
                self.startup_preferences.set_startup_config(None)
            self.synchronize_current_config()
            self.refresh()
        except Exception as error:
            self.controller._emit_exception("Error Deleting Configuration",error)

    @QtCore.Slot(str)
    def _set_startup_requested(self,path: str) -> None:
        if not self.controller.editor_writes_allowed:
            return
        try:
            if not self.controller.configuration_available or self.startup_preferences is None:
                raise RuntimeError("Startup config preferences are not configured")
            metadata = self.controller.read_config_metadata(path)
            if metadata.path.parent.absolute() != self.controller.config_directory.absolute():
                raise ValueError("Startup config must be in the SLM config directory")
            self.startup_preferences.set_startup_config(metadata.name)
            self.controller.sigInfo.emit(
                "Startup config",
                "'%s' will be loaded at startup for %s."
                % (metadata.name,self.controller.display_name),
            )
        except Exception as error:
            self.controller._emit_exception("Startup config",error)

    @QtCore.Slot()
    def _open_folder_requested(self) -> None:
        if not self.controller.configuration_available:
            return
        QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(str(self.controller.config_directory.absolute()))
        )

    @QtCore.Slot()
    def _inspect_requested(self) -> None:
        if not self.controller.configuration_available or self.controls is None:
            return
        details = []
        for metadata in self.controller.list_configs():
            try:
                inspection = self.controller.inspect_config(str(metadata.path))
                details.append({
                    **_metadata_dict(inspection.metadata),
                    "tooltip":_tooltip(inspection.metadata),
                    "summary":inspection.summary,
                    "warnings":"\n".join(_warning_text(item) for item in inspection.warnings),
                })
            except Exception as error:
                details.append({
                    **_metadata_dict(metadata),
                    "tooltip":_tooltip(metadata),
                    "summary":"Could not inspect config:\n%s" % error,
                    "warnings":str(error),
                })
        show_config_inspection(
            self.controls,
            "Inspect %s configs" % self.controller.display_name,
            details,
            selected_path=self.controls.selected_path(),
        )

    def _is_startup(self,path: str) -> bool:
        if self.startup_preferences is None:
            return False
        startup = self.startup_preferences.startup_config()
        return bool(startup) and startup == os.path.basename(str(path))

    def _require_editor_mode(self) -> None:
        if not self.controller.editor_writes_allowed:
            raise RuntimeError("Operation unavailable in Fast Config mode")

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disconnect_controls()
        self._disposed = True


def _metadata_dict(metadata: SLMConfigMetadata):
    return {
        "name":metadata.name,
        "path":str(metadata.path),
        "created_at":metadata.created_at,
        "info":metadata.info,
        "schema_version":metadata.schema_version,
        "slm_key":metadata.slm_key,
        "serial_number":metadata.serial_number,
        "section_keys":tuple(metadata.section_keys),
    }


def _tooltip(metadata: SLMConfigMetadata) -> str:
    lines = [metadata.name]
    if metadata.created_at: lines.append("Created: %s" % metadata.created_at)
    if metadata.info: lines.append(metadata.info)
    if metadata.serial_number: lines.append("SLM serial: %s" % metadata.serial_number)
    return "\n\n".join(lines)


def _warning_text(warning) -> str:
    path = ".".join(getattr(warning,"path",()) or ())
    message = str(getattr(warning,"message",warning))
    return "%s: %s" % (path,message) if path else message


__all__ = ["QtConfigurationManager"]
