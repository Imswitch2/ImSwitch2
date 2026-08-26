from __future__ import annotations

import os
from typing import Any

from qtpy import QtCore,QtGui

from ...application.runtime_factory import SLMRuntimeFactory
from ...calibration.geometry import config_calibration_geometry_mismatches
from ...config.model import SLMConfig,SLMSectionConfig
from ...config.store import SLMConfigMetadata
from ...config.repository import SLMConfigRepository
from ..calibration.geometry_dialogs import (
    CalibrationMismatchDecision,calibration_mismatch_decision,
    confirm_destructive_change,
)
from .controls import ConfigControls
from .dialogs import (
    confirm_delete,request_name,request_save_as,request_update_info,
    show_config_inspection,
)


class ConfigurationManager(QtCore.QObject):
    """Reusable config UI/application workflow for one ``SLMQtSession``."""

    def __init__(
        self,
        controller,
        *,
        repository: SLMConfigRepository | None,
        controls: ConfigControls | None,
        runtime_factory: SLMRuntimeFactory | None,
        startup_preferences=None,
        current_config_path: str | None=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.repository = repository
        self.controls = controls
        self.runtime_factory = runtime_factory
        self.startup_preferences = startup_preferences
        self._disposed = False
        self._connect_controls()
        self.refresh()
        if current_config_path:
            self._set_current_path(current_config_path)

    @property
    def current_config_path(self) -> str | None:
        if self.controls is None:
            return None
        value = self.controls.current_config().get("path")
        return str(value) if value else None

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
        if self.repository is None or self.controls is None:
            return
        entries = [
            (item.name,str(item.path),_tooltip(item))
            for item in self.repository.list()
        ]
        self.controls.set_available_configs(entries)

    def load(
        self,
        path: str,
        *,
        confirm_layout_change: bool=True,
        calibration_mismatch_policy: str="prompt",
        show_error: bool=True,
        require_complete: bool=False,
    ) -> bool:
        if self.repository is None or self.runtime_factory is None:
            if show_error:
                self.controller._emit_exception(
                    "SLM config load failed",
                    RuntimeError("Configuration storage is not configured."),
                )
            return False
        if self.controller.automatic_operation_active:
            return False
        try:
            config,warnings = self.repository.load(path)
            self.runtime_factory.validate_config(config)
            runtime = self.controller.runtime
            current_signature = self.controller.runtime_layout_signature()
            config_signature = self.runtime_factory.setup.validate_layout(
                config.geometry,
                {key:section.geometry for key,section in config.sections.items()},
            )
            layout_changed = config_signature != current_signature
            mismatches = config_calibration_geometry_mismatches(config)

            clear_sections = ()
            if mismatches:
                policy = str(calibration_mismatch_policy or "prompt").lower()
                if policy == "reject":
                    raise ValueError(
                        "Config calibration geometry is incompatible with its section layout: "
                        + "; ".join(item.summary() for item in mismatches)
                    )
                if policy != "prompt":
                    raise ValueError("Unknown calibration mismatch policy %r" % policy)
                decision = calibration_mismatch_decision(
                    self.controls,
                    title="Load SLM config",
                    message=(
                        "Some calibrations in this config were measured with a different "
                        "section geometry. You may keep them intentionally, clear them, "
                        "or cancel loading."
                        + (
                            " The config will also replace the current section layout."
                            if layout_changed else ""
                        )
                    ),
                    mismatches=mismatches,
                    allow_clear=True,
                )
                if decision is CalibrationMismatchDecision.CANCEL:
                    self._restore_current_selection()
                    return False
                if decision is CalibrationMismatchDecision.CLEAR:
                    clear_sections = tuple(item.section_key for item in mismatches)
                    config = _config_with_cleared_calibrations(
                        config,clear_sections,self.repository.registries,
                    )
            elif layout_changed and confirm_layout_change:
                if not confirm_destructive_change(
                    self.controls,
                    "Load SLM config with different layout",
                    "Loading this config will replace the SLM section layout, clear "
                    "current pending work, and rebuild the section UI. Continue?",
                ):
                    self._restore_current_selection()
                    return False

            report = None
            ui_failures = {}
            if layout_changed:
                replacement = self.runtime_factory.create_from_config(config)
                self.controller.replace_runtime(replacement)
            else:
                previous_config = (
                    runtime.create_config() if require_complete else None
                )
                self.controller.cancel_all_cgh()
                self.controller.prepare_runtime_state_change()
                self.controller.cancel_pending_edits(restore=True)
                report = runtime.load_config(
                    config,require_complete=bool(require_complete),
                )
                failed_snapshots = {
                    key:runtime.get_section_snapshot(key)
                    for key in report.failed_sections
                }
                ui_failures = self.controller.apply_config_report(
                    report,failed_section_snapshots=failed_snapshots,
                )
                if require_complete and ui_failures:
                    load_error = RuntimeError(
                        "Complete SLM config UI synchronization failed: %s"
                        % "; ".join(
                            "%s: %s" % (key,error)
                            for key,error in ui_failures.items()
                        )
                    )
                    if previous_config is not None:
                        rollback = runtime.load_config(
                            previous_config,require_complete=True,
                        )
                        rollback_ui = self.controller.apply_config_report(
                            rollback,failed_section_snapshots={},
                        )
                        self.controller.synchronize_all_sections()
                        if rollback_ui:
                            raise RuntimeError(
                                "%s; rollback UI synchronization also failed: %s"
                                % (
                                    load_error,
                                    "; ".join(
                                        "%s: %s" % (key,error)
                                        for key,error in rollback_ui.items()
                                    ),
                                )
                            )
                    raise load_error
                self.controller.synchronize_all_sections()
                if report.frame_changed:
                    self.controller.publish_current_frame()

            metadata = self.repository.read_metadata(path)
            self._set_current_metadata(metadata)
            messages = [_warning_text(item) for item in warnings]
            if self.controller.last_upload_error is not None:
                messages.append(
                    "Runtime updated, but hardware upload failed; the displayed "
                    "hardware pattern may be stale."
                )
            if report is not None:
                messages.extend(_warning_text(item) for item in report.warnings)
                messages.extend(
                    "%s: config restore failed: %s" % (key,error)
                    for key,error in report.failed_sections.items()
                )
            messages.extend(
                "%s: UI synchronization failed: %s" % (key,error)
                for key,error in ui_failures.items()
            )
            self.controller.sigStatusChanged.emit(
                "; ".join(messages),bool(messages),
            )
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
        if self.controls is not None:
            self.controls.set_current_config(self.controls.current_config())

    def save(self,name: str,info: str="",*,overwrite: bool=False):
        self._require_editor_mode()
        if self.repository is None:
            raise RuntimeError("Configuration storage is not configured")
        self.controller.flush_all(propagate=True)
        config = self.controller.runtime.create_config()
        metadata = self.repository.save(
            self.repository.destination(name),config,info,overwrite=overwrite,
        )
        self._set_current_metadata(metadata)
        self.refresh()
        return metadata

    @QtCore.Slot()
    def _save_as_requested(self) -> None:
        if not self.controller.editor_writes_allowed:
            return
        if self.controls is None:
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
            if self.repository is None or self.controls is None:
                return
            self.controller.flush_all(propagate=True)
            config = self.controller.runtime.create_config()
            changes = self.repository.compare(path,config)
            metadata = self.repository.read_metadata(path)
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
        if not self.controller.editor_writes_allowed:
            return
        if self.repository is None or self.controls is None:
            return
        old = os.path.basename(path)
        new = request_name(self.controls,"Rename config %s" % old,"New name:",old)
        if not new or new == old:
            return
        try:
            was_startup = self._is_startup(path)
            was_current = _same_path(path,self.current_config_path)
            metadata = self.repository.rename(path,new,overwrite=False)
            if was_startup and self.startup_preferences is not None:
                self.startup_preferences.set_startup_config(metadata.name)
            if was_current:
                self._set_current_metadata(metadata)
            self.refresh()
        except Exception as error:
            self.controller._emit_exception("Error Renaming Configuration",error)

    @QtCore.Slot(str)
    def _duplicate_requested(self,path: str) -> None:
        if not self.controller.editor_writes_allowed:
            return
        if self.repository is None or self.controls is None:
            return
        old = os.path.basename(path)
        suggested = os.path.splitext(old)[0] + "_copy"
        new = request_name(self.controls,"Duplicate config %s" % old,"New name:",suggested)
        if not new:
            return
        try:
            self.repository.duplicate(path,new,overwrite=False)
            self.refresh()
        except Exception as error:
            self.controller._emit_exception("Error Duplicating Configuration",error)

    @QtCore.Slot(str)
    def _delete_requested(self,path: str) -> None:
        if not self.controller.editor_writes_allowed:
            return
        if self.repository is None or self.controls is None:
            return
        if not confirm_delete(self.controls,os.path.basename(path)):
            return
        try:
            was_startup = self._is_startup(path)
            was_current = _same_path(path,self.current_config_path)
            self.repository.delete(path)
            if was_startup and self.startup_preferences is not None:
                self.startup_preferences.set_startup_config(None)
            if was_current:
                self.controls.set_current_config(None)
            self.refresh()
        except Exception as error:
            self.controller._emit_exception("Error Deleting Configuration",error)

    @QtCore.Slot(str)
    def _set_startup_requested(self,path: str) -> None:
        if not self.controller.editor_writes_allowed:
            return
        try:
            if self.repository is None or self.startup_preferences is None:
                raise RuntimeError("Startup config preferences are not configured")
            metadata = self.repository.read_metadata(path)
            if metadata.path.parent.absolute() != self.repository.directory.absolute():
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
        if self.repository is None:
            return
        QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(str(self.repository.directory.absolute()))
        )

    @QtCore.Slot()
    def _inspect_requested(self) -> None:
        if self.repository is None or self.controls is None:
            return
        details = []
        for metadata in self.repository.list():
            try:
                inspection = self.repository.inspect(metadata.path)
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

    def _set_current_path(self,path: str) -> None:
        if self.repository is None:
            return
        try:
            self._set_current_metadata(self.repository.read_metadata(path))
        except Exception:
            if self.controls is not None:
                self.controls.set_current_config({"path":str(path)})

    def _set_current_metadata(self,metadata: SLMConfigMetadata) -> None:
        if self.controls is not None:
            self.controls.set_current_config(_metadata_dict(metadata))

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


def _config_with_cleared_calibrations(config: SLMConfig,keys,registries) -> SLMConfig:
    clear = set(keys)
    sections = {}
    for key,section in config.sections.items():
        clone = section.clone(registries)
        if key in clear:
            clone.calibration = None
        sections[key] = clone
    return SLMConfig(
        schema_version=config.schema_version,
        identity=config.identity,
        geometry=config.geometry,
        sections=sections,
        final_eightbit=config.final_eightbit,
    )


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


def _same_path(first: Any,second: Any) -> bool:
    if not first or not second:
        return False
    return os.path.normcase(os.path.abspath(str(first))) == os.path.normcase(
        os.path.abspath(str(second))
    )
