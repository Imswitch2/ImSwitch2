from __future__ import annotations

import os
from typing import Any,Callable,Mapping

from qtpy import QtCore,QtWidgets

from ...application.session import SLMSession,SLMSessionCallbacks
from ...application.configuration import (
    CalibrationMismatchPolicy,ConfigLoadOutcome,CorrectionMismatchPolicy,
    PreparedConfigLoad,
)
from ...application.control_mode import SLMControlMode
from ...core.config.loading import SLMConfigLoadReport
from ...core.cgh.propagation import simulate_propagation_fft
from ...core.cgh.execution.status import CGHResultState
from ...core.cgh.feedback.model import base_cgh_recompute_would_discard_feedback
from ...core.engine.runtime import SLMRuntime
from ...host.services import SLMHostServices
from ...core.engine.transition import SectionStateTransition
from ..cgh.presenter import CGHPresenter
from ..panel.panel import SLMPanel
from ..sections.group_views import CghAction
from .calibration.manager import CalibrationManager
from .interaction import (
    DEFAULT_RUNTIME_VIEW_INTERACTION_SETTINGS,
    RuntimeViewInteractionSettings,
)
from .runtime_binding import SLMRuntimeViewBinding
from .measurement_dispatcher import QtMeasurementDispatcher
from .feedback.coordinator import FeedbackCoordinator
from .section_settings import SectionSettingsManager
from ..configuration.manager import QtConfigurationManager

class SLMQtSession(QtCore.QObject):
    """Reusable Qt-side session for one :class:`SLMRuntime`.

    It adapts the toolkit-independent :class:`SLMSession` to Qt views,
    interaction policy, dialogs and thread-aware host integration. Embedding
    applications provide capabilities, not SLM workflow callbacks.
    """

    sigFrameChanged = QtCore.Signal(object)
    sigSectionSynchronized = QtCore.Signal(str)
    sigCghComputingChanged = QtCore.Signal(str,bool)
    sigAutomaticOperationChanged = QtCore.Signal(bool)
    sigError = QtCore.Signal(str,object)
    sigWarning = QtCore.Signal(str,object)
    sigInfo = QtCore.Signal(str,object)
    sigUploadFailed = QtCore.Signal(object)
    sigStatusChanged = QtCore.Signal(str,bool)
    sigInteractionSettingsChanged = QtCore.Signal(object)
    sigControlModeChanged = QtCore.Signal(object)
    sigControlModeAvailabilityChanged = QtCore.Signal(bool)

    def __init__(
        self,
        *,
        application_session: SLMSession,
        panel: SLMPanel,
        interaction_settings: RuntimeViewInteractionSettings=(
            DEFAULT_RUNTIME_VIEW_INTERACTION_SETTINGS
        ),
        cgh_presenter: CGHPresenter | None=None,
        parent: QtCore.QObject | None=None,
    ) -> None:
        super().__init__(parent)
        if not isinstance(application_session,SLMSession):
            raise TypeError("application_session must be an SLMSession")
        if not isinstance(panel,SLMPanel):
            raise TypeError("panel must be an SLMPanel")

        self._application = application_session
        runtime = application_session.runtime
        self.panel = panel
        self.section_collection = panel.section_collection
        self.section_host = panel.section_host
        self._display_name = str(application_session.display_name)
        self.presenter = cgh_presenter or CGHPresenter(
            display_name=self._display_name,
        )
        self._disposed = False
        self._interaction_settings = interaction_settings
        self._control_mode_available = True
        self._runtime_replacement_state = None

        callbacks = SLMSessionCallbacks(
            on_frame_changed=self.sigFrameChanged.emit,
            on_transition_committed=self._on_application_transition_committed,
            on_section_refresh_requested=self.synchronize_section,
            on_cgh_computing_changed=self._on_application_cgh_computing_changed,
            on_warning=self.sigWarning.emit,
            on_error=self.sigError.emit,
            on_upload_failed=self.sigUploadFailed.emit,
            on_upload_state_changed=self._on_application_upload_state_changed,
            on_runtime_replaced=self._on_application_runtime_replaced,
            on_config_committed=self._on_application_config_committed,
            on_control_mode_changed=self._on_application_control_mode_changed,
            on_fast_config_changed=self._on_application_fast_config_changed,
            on_calibration_planes_changed=self._on_calibration_planes_changed,
            on_calibration_state_changed=self._on_calibration_state_changed,
            on_calibration_measurement_busy_changed=(
                self._on_calibration_measurement_busy_changed
            ),
            on_calibration_measurement_error=self._on_calibration_measurement_error,
            on_feedback_measurement_busy_changed=(
                self._on_feedback_measurement_busy_changed
            ),
            on_feedback_measurement_error=self._on_feedback_measurement_error,
            on_feedback_localization_error=self._on_feedback_localization_error,
            on_automatic_feedback_changed=self._on_automatic_feedback_changed,
            on_automatic_feedback_finished=self._on_automatic_feedback_finished,
        )

        self._application.set_callbacks(callbacks)

        self._binding = self._create_binding(interaction_settings)
        self._connect_collection()
        self._measurements = QtMeasurementDispatcher(
            self.host_services.measurement_provider,
            parent=self,
        )
        self._application.set_measurement_dispatcher(self._measurements)
        self._feedback = FeedbackCoordinator(self)
        self.sigAutomaticOperationChanged.connect(
            self._refresh_control_mode_availability,
        )
        self._calibration = CalibrationManager(
            self,parent=self,
        )
        self._section_settings = SectionSettingsManager(
            self,section_host=self.section_host,parent=self,
        )
        self._configuration = QtConfigurationManager(
            self,
            controls=self.panel.config_controls,
            startup_preferences=self.startup_preferences,
            parent=self,
        )
        self._connect_panel()
        self._configure_device_control()
        self.synchronize_all_sections()

    @property
    def last_upload_error(self) -> Exception | None:
        return self._application.last_upload_error

    @property
    def runtime(self) -> SLMRuntime:
        return self._application.runtime

    @property
    def host_services(self) -> SLMHostServices:
        return self._application.host_services

    @property
    def feedback_service(self):
        return self._application.feedback

    @property
    def calibration_service(self):
        return self._application.calibration

    @property
    def auto_upload_frame(self) -> bool:
        return self._application.auto_upload_frame

    @property
    def display_name(self) -> str:
        return self._display_name

    @property
    def startup_preferences(self):
        return self._application.startup_preferences

    @property
    def section_layout_available(self) -> bool:
        return self._application.section_layout_available

    @property
    def interaction_settings(self) -> RuntimeViewInteractionSettings:
        return self._interaction_settings

    @property
    def current_config_path(self) -> str | None:
        return self._application.current_config_path

    @property
    def current_config_metadata(self):
        return self._application.current_config_metadata

    @property
    def configuration_available(self) -> bool:
        return self._application.configuration_available

    @property
    def config_directory(self):
        return self._application.config_directory

    def resolve_config_path(self,path_or_name) -> str:
        return self._application.resolve_config_path(path_or_name)

    @property
    def control_mode(self) -> SLMControlMode:
        return self._application.control_mode

    @property
    def fast_config_path(self) -> str | None:
        return self._application.fast_config_path

    @property
    def editor_writes_allowed(self) -> bool:
        return self._application.editor_writes_allowed

    @property
    def is_cgh_busy(self) -> bool:
        return self._application.is_cgh_busy

    @property
    def can_change_control_mode(self) -> bool:
        return bool(
            not self.is_cgh_busy
            and not self.automatic_operation_active
        )

    def set_control_mode(self,mode) -> bool:
        """Switch between editor and strict compiled-config operation."""
        self._require_active()
        requested = SLMControlMode.normalize(mode)
        if requested is self.control_mode:
            return True
        if self.is_cgh_busy:
            self.sigWarning.emit(
                "SLM control mode",
                "Wait for the current CGH computation to finish before changing control mode.",
            )
            return False
        if self.automatic_operation_active:
            self.sigWarning.emit(
                "SLM control mode",
                "Stop automatic feedback before changing control mode.",
            )
            return False

        prepared = None
        calibration_policy = CalibrationMismatchPolicy.REJECT
        correction_policy = CorrectionMismatchPolicy.REJECT
        try:
            if requested is SLMControlMode.FAST_CONFIG:
                path = self.current_config_path
                if path:
                    # Validate the exact compiled output before switching modes.
                    self._application.validate_compiled_config(path)
            else:
                path = self.fast_config_path
                if path:
                    # Fast -> Editor always reconstructs the active compiled config,
                    # even when it is the same path that was selected before Fast.
                    prepared = self.prepare_config_load(path)
                    resolved = self._configuration.resolve_calibration_policy(
                        prepared,"prompt",title="SLM control mode",
                    )
                    if resolved is None:
                        self._configuration.synchronize_current_config()
                        return False
                    calibration_policy = resolved
                    resolved_corrections = self._configuration.resolve_correction_policy(
                        prepared,"prompt",title="SLM control mode",
                    )
                    if resolved_corrections is None:
                        self._configuration.synchronize_current_config()
                        return False
                    correction_policy = resolved_corrections
                    self.prepare_config_commit(
                        layout_changed=prepared.layout_changed,
                    )
            return self._application.set_control_mode(
                requested,
                calibration_mismatch_policy=calibration_policy,
                correction_mismatch_policy=correction_policy,
                prepared_config_load=prepared,
            )
        except Exception as error:
            self._emit_exception("SLM control mode",error)
            self._configuration.synchronize_current_config()
            return False

    def activate_compiled_config(self,path: str) -> bool:
        self._require_active()
        try:
            self._application.activate_compiled_config(path)
            self.sigStatusChanged.emit("",False)
            return True
        except Exception as error:
            self._emit_exception("Fast config activation failed",error)
            self.panel.config_controls.set_selected_path(self.fast_config_path)
            return False

    @QtCore.Slot(str)
    def _on_config_load_requested(self,path: str) -> None:
        if self.control_mode is SLMControlMode.FAST_CONFIG:
            self.activate_compiled_config(path)
            return
        self._configuration.load(path)

    def _connect_panel(self) -> None:
        self.panel.sigConnectionRequested.connect(
            self._on_connection_requested,
        )
        self.sigFrameChanged.connect(self.panel.set_frame)
        self.sigAutomaticOperationChanged.connect(
            self.panel.set_interaction_locked,
        )
        self.sigError.connect(self.panel.show_error)
        self.sigWarning.connect(self.panel.show_warning)
        self.sigInfo.connect(self.panel.show_info)
        self.sigStatusChanged.connect(self.panel.set_status)
        self.sigUploadFailed.connect(self._on_panel_upload_failed)
        self.panel.config_controls.sigLoadRequested.connect(
            self._on_config_load_requested,
        )

    def _disconnect_panel(self) -> None:
        pairs = (
            (self.panel.sigConnectionRequested,self._on_connection_requested),
            (self.sigFrameChanged,self.panel.set_frame),
            (self.sigAutomaticOperationChanged,self.panel.set_interaction_locked),
            (self.sigError,self.panel.show_error),
            (self.sigWarning,self.panel.show_warning),
            (self.sigInfo,self.panel.show_info),
            (self.sigStatusChanged,self.panel.set_status),
            (self.sigUploadFailed,self._on_panel_upload_failed),
            (self.panel.config_controls.sigLoadRequested,self._on_config_load_requested),
        )
        for signal,slot in pairs:
            try:
                signal.disconnect(slot)
            except (RuntimeError,TypeError):
                pass

    def _configure_device_control(self) -> None:
        device = self.host_services.device
        self.panel.set_connection_control_visible(
            bool(device is not None and device.requires_explicit_connection)
        )

    def initialize_device(self,*,show_error: bool=False):
        """Initialize the configured output device and publish the current frame."""
        self._require_active()
        device = self.host_services.device
        if device is None:
            return None
        if device.requires_explicit_connection:
            return self.connect_device(show_error=show_error)
        self.panel.set_connection_state(True)
        result = self._application.initialize_device(upload_current_frame=False)
        self._publish_active_device_frame()
        return result

    def connect_device(self,*,show_error: bool=True):
        self._require_active()
        device = self.host_services.device
        if device is None:
            raise RuntimeError("No SLM device provider is configured")
        self.panel.set_connection_busy(True)
        try:
            result = self._application.connect_device(upload_current_frame=False)
            self.panel.set_connection_state(result.connected)
            if result.connected:
                self._publish_active_device_frame()
            elif show_error:
                self.sigError.emit(
                    "SLM connection failed",
                    result.message or "Could not connect to the SLM device.",
                )
            return result
        except Exception as error:
            self.panel.set_connection_state(False)
            if show_error:
                self._emit_exception("SLM connection failed",error)
            return None
        finally:
            self.panel.set_connection_busy(False)

    def disconnect_device(self,*,show_error: bool=True):
        self._require_active()
        device = self.host_services.device
        if device is None:
            raise RuntimeError("No SLM device provider is configured")
        self.panel.set_connection_busy(True)
        try:
            result = self._application.disconnect_device()
            self.panel.set_connection_state(result.connected)
            if result.connected and show_error:
                self.sigError.emit(
                    "SLM disconnection failed",
                    result.message or "Could not disconnect the SLM device.",
                )
            return result
        except Exception as error:
            self.panel.set_connection_state(True)
            if show_error:
                self._emit_exception("SLM disconnection failed",error)
            return None
        finally:
            self.panel.set_connection_busy(False)

    @QtCore.Slot(bool)
    def _on_connection_requested(self,connect: bool) -> None:
        if self.automatic_operation_active:
            return
        if connect:
            self.connect_device(show_error=True)
        else:
            self.disconnect_device(show_error=True)

    def _on_panel_upload_failed(self,_error: Any) -> None:
        if self.control_mode is SLMControlMode.FAST_CONFIG:
            message = (
                "Fast config upload failed; hardware may still display the "
                "previous compiled frame."
            )
        else:
            message = (
                "Runtime updated, but hardware upload failed; the displayed "
                "hardware pattern may be stale."
            )
        self.panel.set_status(message,error=True)

    def _publish_active_device_frame(self) -> bool:
        if self.control_mode is SLMControlMode.FAST_CONFIG:
            if not self.fast_config_path:
                return True
            return self.activate_compiled_config(self.fast_config_path)
        return self.upload_current_frame()

    def _create_binding(
        self,settings: RuntimeViewInteractionSettings,
    ) -> SLMRuntimeViewBinding:
        binding = SLMRuntimeViewBinding(
            runtime=self.runtime,
            application_session=self._application,
            section_collection=self.section_collection,
            interaction_settings=settings,
            correction_source_switch_confirm=(
                self._confirm_switch_to_current_corrections
            ),
            parent=self,
        )
        binding.sigPatchApplied.connect(self._on_patch_applied)
        binding.sigPatchFailed.connect(self._on_patch_failed)
        binding.sigAutoComputeRequested.connect(self._on_auto_compute_requested)
        return binding

    def _confirm_switch_to_current_corrections(
        self,section_key: str,error: Exception,
    ) -> bool:
        parent = self.section_collection.section_view(section_key)
        result = QtWidgets.QMessageBox.question(
            parent,
            "Switch correction source",
            "This section is using the saved correction snapshot. The requested "
            "edit is incompatible with that snapshot and requires the current "
            "workspace corrections.\n\nSwitch to current corrections and apply the edit?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Cancel,
        )
        return result == QtWidgets.QMessageBox.Yes

    def _connect_collection(self) -> None:
        self.section_collection.sigCghActionRequested.connect(
            self._on_cgh_action_requested,
        )

    def _disconnect_collection(self) -> None:
        try:
            self.section_collection.sigCghActionRequested.disconnect(
                self._on_cgh_action_requested,
            )
        except (RuntimeError,TypeError):
            pass

    def set_interaction_settings(
        self,settings: RuntimeViewInteractionSettings,
    ) -> None:
        self._require_active()
        self._interaction_settings = settings
        self._binding.set_interaction_settings(settings)

    def set_auto_upload_frame(self,enabled: bool) -> None:
        self._application.set_auto_upload_frame(bool(enabled))
        self._feedback.refresh_automatic_availability()
        self._calibration.refresh_live_acquisition_all()

    def flush_section(
        self,section_key: str,*,propagate: bool=True,
    ):
        self._require_active()
        self._require_editor_mode()
        return self._binding.flush_section(
            section_key,propagate=propagate,
        )

    def flush_all(self,*,propagate: bool=True) -> None:
        self._require_active()
        self._require_editor_mode()
        self._binding.flush_all(propagate=propagate)

    def cancel_pending_edits(self,*,restore: bool=True):
        self._require_active()
        return self._binding.cancel_all(restore=restore)

    def is_cgh_computing(self,section_key: str) -> bool:
        return self._application.is_cgh_computing(section_key)

    def cancel_cgh(self,section_key: str) -> bool:
        self._require_active()
        return self._application.cancel_cgh(section_key)

    def cancel_all_cgh(self) -> None:
        self._application.cancel_all_cgh()

    def compute_base_cgh(
        self,section_key: str,*,confirm_feedback_loss: bool=True,
    ) -> None:
        if not self.editor_writes_allowed:
            return
        self._require_section(section_key)
        if self.is_cgh_computing(section_key):
            return

        status = self.runtime.get_section_feedback_status(section_key)
        if (
            confirm_feedback_loss
            and base_cgh_recompute_would_discard_feedback(status)
            and not self.presenter.confirm_new_base_cgh(
                self.section_collection.section_view(section_key),status,
            )
        ):
            return
        try:
            self.flush_section(section_key,propagate=True)
        except Exception as error:
            self._emit_exception("CGH preparation failed",error)
            return
        self._application.compute_base_cgh(section_key)

    def compute_adapted_cgh(
        self,
        section_key: str,
        *,
        on_finished: Callable[[bool, Exception | None], None] | None=None,
    ) -> bool:
        if not self.editor_writes_allowed:
            if on_finished is not None:
                on_finished(False,RuntimeError("CGH computation is unavailable in Fast Config mode"))
            return False
        self._require_section(section_key)
        status = self.runtime.get_section_feedback_status(section_key)
        if not status.feedback_compute_pending:
            message = "No feedback adaptation is waiting to be computed."
            self.sigWarning.emit("Compute adapted hologram",message)
            if on_finished is not None:
                on_finished(False,RuntimeError(message))
            return False
        try:
            self.flush_section(section_key,propagate=True)
        except Exception as error:
            self._emit_exception("Adapted CGH preparation failed",error)
            if on_finished is not None:
                on_finished(False,error)
            return False
        return self._application.compute_adapted_cgh(
            section_key,on_finished=on_finished,
        )

    def restore_current_cgh_target(self,section_key: str) -> None:
        if not self.editor_writes_allowed:
            return
        if self.is_cgh_computing(section_key):
            return
        try:
            self._binding.restore_current_cgh_target(
                section_key,propagate=True,
            )
        except Exception as error:
            self._emit_exception("Restore current CGH target failed",error)

    def clear_cgh_session(self,section_key: str) -> None:
        if not self.editor_writes_allowed:
            return
        self._require_section(section_key)
        cgh_status = self.runtime.get_section_cgh_status(section_key)
        feedback_status = self.runtime.get_section_feedback_status(section_key)
        has_result = self._cgh_status_has_result(cgh_status)
        if (
            self._clear_cgh_session_needs_confirmation(
                cgh_status,feedback_status,
            )
            and not self.presenter.confirm_clear_cgh_session(
                self.section_collection.section_view(section_key),
                feedback_status,
                has_cgh_result=has_result,
            )
        ):
            return

        try:
            self.flush_section(section_key,propagate=True)
            self._application.clear_section_cgh_session(section_key)
        except Exception as error:
            self._restore_section(section_key)
            self._emit_exception("Clearing CGH session failed",error)

    def create_target_preview(self,section_key: str) -> None:
        if not self.editor_writes_allowed:
            return
        try:
            self.flush_section(section_key,propagate=True)
            preview = self.runtime.create_section_target_preview(section_key)
            self.presenter.plot_target_preview(section_key,preview)
        except Exception as error:
            self._emit_exception("CGH target preview failed",error)

    def show_cgh_metrics(self,section_key: str) -> None:
        try:
            result = self.runtime.get_section_cgh_result_copy(section_key)
            if result is None:
                self.sigWarning.emit(
                    "CGH performance","No CGH result is available.",
                )
                return
            if not self.presenter.plot_metrics(section_key,result.metrics):
                self.sigWarning.emit(
                    "CGH performance",
                    "No CGH iteration metrics are available.",
                )
        except Exception as error:
            self._emit_exception("CGH performance plot failed",error)

    def show_cgh_propagation(
        self,section_key: str,*,pad_size: Any=1024,
    ) -> None:
        try:
            pad_size = int(pad_size)
            if pad_size <= 0:
                raise ValueError("CGH propagation pad size must be > 0")
            result = self.runtime.get_section_cgh_result_copy(section_key)
            if result is None:
                self.sigWarning.emit(
                    "CGH propagation","No CGH result is available.",
                )
                return
            intensity = simulate_propagation_fft(
                result.pattern,padding=True,pad_size=pad_size,
            )
            self.presenter.plot_propagation(section_key,intensity)
        except Exception as error:
            self._emit_exception("CGH propagation failed",error)

    @QtCore.Slot(str,str,object)
    def _on_cgh_action_requested(
        self,section_key: str,action: str,options: Mapping[str,Any],
    ) -> None:
        if not self.editor_writes_allowed:
            return
        if self.automatic_operation_active:
            return
        try:
            cgh_action = CghAction(str(action))
        except Exception:
            self.sigError.emit(
                "Unknown CGH action",f"Unsupported CGH action: {action!r}",
            )
            return

        values = dict(options or {})
        if cgh_action is CghAction.COMPUTE:
            self.compute_base_cgh(section_key)
        elif cgh_action is CghAction.RESTORE_CURRENT_TARGET:
            self.restore_current_cgh_target(section_key)
        elif cgh_action is CghAction.TARGET_PREVIEW:
            self.create_target_preview(section_key)
        elif cgh_action is CghAction.METRICS:
            self.show_cgh_metrics(section_key)
        elif cgh_action is CghAction.PROPAGATION:
            self.show_cgh_propagation(
                section_key,pad_size=values.get("pad_size",1024),
            )
        elif cgh_action is CghAction.CLEAR_CGH_SESSION:
            self.clear_cgh_session(section_key)
        elif cgh_action is CghAction.OPEN_MEASUREMENTS_CORRECTIONS:
            self._feedback.open_window(section_key)

    def _on_application_transition_committed(
        self,section_key: str,transition: SectionStateTransition,
    ) -> None:
        """Render an already committed application transition in Qt."""
        try:
            self.section_collection.apply_section_transition(section_key,transition)
            self.synchronize_section(section_key)
        except Exception:
            self._restore_section(section_key)
            raise

    def _on_application_cgh_computing_changed(
        self,section_key: str,computing: bool,
    ) -> None:
        self.section_collection.set_cgh_computing(section_key,bool(computing))
        if hasattr(self,"_feedback"):
            self._feedback.set_cgh_computing(section_key,bool(computing))
        if hasattr(self,"_calibration"):
            self._calibration.set_cgh_computing(section_key,bool(computing))
        self.sigCghComputingChanged.emit(section_key,bool(computing))
        self._refresh_control_mode_availability()

    def _on_application_upload_state_changed(
        self,_success: bool,_error: Exception | None,
    ) -> None:
        if hasattr(self,"_calibration"):
            self._calibration.refresh_live_acquisition_all()

    def _on_application_runtime_replaced(self,runtime: SLMRuntime) -> None:
        """Rebuild Qt presentation after the application runtime is committed."""
        settings = self._binding.interaction_settings
        old_binding = self._binding
        old_binding.cancel_all(restore=False)
        old_binding.dispose(restore=False)
        self._disconnect_collection()

        replacement = self.panel.replace_sections(
            runtime.get_section_snapshots(),
        )
        self.section_collection = replacement
        self._binding = self._create_binding(settings)
        if not self.editor_writes_allowed:
            self._binding.set_writes_enabled(False,restore_pending=False)
        self._connect_collection()
        self._feedback.runtime_replaced()
        self._calibration.runtime_replaced()
        self._section_settings.runtime_replaced()
        old_binding.deleteLater()
        self.synchronize_all_sections()

    def _on_application_config_committed(
        self,outcome: ConfigLoadOutcome,
    ) -> None:
        """Render an already committed config result; never roll runtime back."""
        ui_failures = {}
        report = outcome.report
        if report is not None:
            ui_failures = self.apply_config_report(
                report,
                failed_section_snapshots=outcome.failed_section_snapshots,
            )
            self.synchronize_all_sections()

        if hasattr(self,"_configuration"):
            self._configuration.synchronize_current_config()

        messages = [_warning_text(item) for item in outcome.warnings]
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
        self.sigStatusChanged.emit("; ".join(messages),bool(messages))

    def _on_application_control_mode_changed(
        self,mode: SLMControlMode,fast_config_path: str | None,
    ) -> None:
        """Apply Qt-only visibility/write policy for application control mode."""
        if mode is SLMControlMode.FAST_CONFIG:
            self._binding.set_writes_enabled(False,restore_pending=True)
            self.panel.set_config_only_view(True)
            if fast_config_path is None:
                self.panel.clear_frame()
            self.panel.config_controls.set_selected_path(fast_config_path)
        else:
            self._binding.set_writes_enabled(True)
            self.panel.set_config_only_view(False)
            self.panel.config_controls.clear_selected_path_override()
            if hasattr(self,"_calibration"):
                self._calibration.control_mode_changed()
        if hasattr(self,"_feedback"):
            self._feedback.refresh_automatic_availability()
        self.sigControlModeChanged.emit(mode)

    def _on_application_fast_config_changed(self,path: str | None) -> None:
        if self.control_mode is SLMControlMode.FAST_CONFIG:
            self.panel.config_controls.set_selected_path(path)

    def _on_calibration_planes_changed(self) -> None:
        if hasattr(self,"_calibration"):
            self._calibration.refresh_planes()

    def _on_calibration_state_changed(self,section_key: str) -> None:
        if hasattr(self,"_calibration"):
            self._calibration.render_target_state(section_key)

    def _on_calibration_measurement_busy_changed(
        self,section_key: str,busy: bool,message: str,
    ) -> None:
        if hasattr(self,"_calibration"):
            self._calibration.on_measurement_busy_changed(
                section_key,busy,message,
            )

    def _on_calibration_measurement_error(
        self,section_key: str,error: Exception,
    ) -> None:
        if hasattr(self,"_calibration"):
            self._calibration.on_measurement_error(section_key,error)

    def _on_feedback_measurement_busy_changed(
        self,section_key: str,busy: bool,message: str,
    ) -> None:
        if hasattr(self,"_feedback"):
            self._feedback.on_measurement_busy_changed(
                section_key,busy,message,
            )

    def _on_feedback_measurement_error(
        self,section_key: str,error: Exception,
    ) -> None:
        if hasattr(self,"_feedback"):
            self._feedback.on_measurement_error(section_key,error)

    def _on_feedback_localization_error(
        self,section_key: str,error: Exception,
    ) -> None:
        if hasattr(self,"_feedback"):
            self._feedback.on_localization_error(section_key,error)

    def _on_automatic_feedback_changed(self,state) -> None:
        if hasattr(self,"_feedback"):
            self._feedback.on_automatic_state_changed(state)
        self._refresh_control_mode_availability()

    def _on_automatic_feedback_finished(
        self,section_key: str,message: str,
    ) -> None:
        if hasattr(self,"_feedback"):
            self._feedback.on_automatic_finished(section_key,message)

    @QtCore.Slot(str)
    def _on_auto_compute_requested(self,section_key: str) -> None:
        if not self.editor_writes_allowed:
            return
        if self.automatic_operation_active:
            return
        if self.is_cgh_computing(section_key):
            return
        cgh_status = self.runtime.get_section_cgh_status(section_key)
        if getattr(cgh_status,"target_type",None) is None:
            return
        status = self.runtime.get_section_feedback_status(section_key)
        if base_cgh_recompute_would_discard_feedback(status):
            return
        try:
            self.flush_section(section_key,propagate=True)
        except Exception as error:
            self._emit_exception("Automatic CGH preparation failed",error)
            return
        self._application.compute_base_cgh(section_key)

    @QtCore.Slot(str,object)
    def _on_patch_applied(self,section_key: str,update: Any) -> None:
        if not self.editor_writes_allowed:
            return
        try:
            self.synchronize_section(section_key)
            if bool(getattr(update,"frame_changed",False)):
                self._publish_current_frame()
        except Exception as error:
            self._emit_exception("SLM post-update synchronization failed",error)

    @QtCore.Slot(str,object)
    def _on_patch_failed(self,section_key: str,error: Exception) -> None:
        del section_key
        self._emit_exception("SLM parameter update failed",error)

    def apply_transition(
        self,section_key: str,transition: SectionStateTransition,
    ) -> bool:
        """Render a committed transition and publish its frame if needed.

        The runtime transition is already authoritative. A Qt presentation
        failure is reported and the section is restored from the committed
        runtime snapshot, but it does not invalidate the application change.
        """
        if not self.editor_writes_allowed:
            return False
        if transition is None:
            return False
        try:
            self._on_application_transition_committed(section_key,transition)
        except Exception as error:
            self._emit_exception("SLM section UI synchronization failed",error)
        if bool(transition.frame_changed):
            return self._application.publish_current_frame()
        return True

    def synchronize_section(self,section_key: str) -> None:
        status = self.runtime.get_section_feedback_status(section_key)
        self.section_collection.set_feedback_status(section_key,status)
        self.section_collection.set_cgh_target_presentation(
            section_key,self._main_cgh_presentation_summary(section_key),
        )
        if hasattr(self,"_feedback"):
            self._feedback.synchronize_section(section_key)
        if hasattr(self,"_calibration"):
            self._calibration.synchronize_section(section_key)
        self.sigSectionSynchronized.emit(section_key)

    def synchronize_all_sections(self) -> None:
        for section_key in self.runtime.section_keys:
            self.synchronize_section(section_key)

    def _target_presentation_summary(
        self,
        section_key: str,
        target_key: str | None,
        target_params: Mapping[str,Any],
    ) -> Mapping[str,Any]:
        if not target_key:
            return {}
        registration = self.runtime.registries.targets.get(str(target_key))
        if registration is None:
            return {}
        return {
            "target_presentation":registration.presentation,
            "target_param_specs":registration.params,
            "target_params":dict(target_params or {}),
            "unit_mode":self.section_collection.section_view(
                section_key,
            ).unit_mode,
            "conversion_context":self.runtime.get_section_calibration_copy(
                section_key,
            ),
        }

    def _main_cgh_presentation_summary(
        self,section_key: str,
    ) -> Mapping[str,Any]:
        state = self.runtime.get_section_state_copy(section_key).cgh
        target_key = state.selected_target
        summary: dict[str, Any] = {}
        if target_key is not None and target_key in state.items:
            summary.update(self._target_presentation_summary(
                section_key,target_key,state.items[target_key].params.values,
            ))

        result = self.runtime.get_section_cgh_result_copy(section_key)
        if result is not None:
            applied = self._target_presentation_summary(
                section_key,result.spec.target_type,result.spec.target_params,
            )
            for key,value in applied.items():
                summary["applied_%s" % key] = value
        return summary

    def upload_current_frame(self) -> bool:
        """Upload the current frame explicitly, independent of auto-upload policy."""
        self._require_active()
        if not self.editor_writes_allowed:
            return False
        return self._application.upload_current_frame()

    def publish_current_frame(self) -> None:
        """Publish preview state and apply the configured auto-upload policy."""
        self._require_active()
        if not self.editor_writes_allowed:
            return
        self._application.publish_current_frame()

    def defer_frame_upload(self):
        """Coalesce automatic hardware uploads across a transition batch.

        View/preview synchronization still happens immediately.  If one or
        more frame-changing transitions occur, the latest frame is uploaded
        once when the outermost defer scope exits.  Deferred upload and an
        automatic feedback loop are mutually exclusive because the loop must
        know that each adapted frame reached hardware before acquisition.
        """
        self._require_active()
        self._require_editor_mode()
        if self.automatic_operation_active:
            raise RuntimeError(
                "Cannot defer frame upload during automatic feedback"
            )
        return self._application.defer_frame_upload()

    def _publish_current_frame(self) -> bool:
        if not self.editor_writes_allowed:
            return False
        return self._application.publish_current_frame()

    def _upload_frame_now(self) -> bool:
        if not self.editor_writes_allowed:
            return False
        return self._application.upload_current_frame()

    @property
    def can_run_automatic_feedback(self) -> bool:
        return self._application.can_run_automatic_feedback

    @property
    def automatic_operation_active(self) -> bool:
        return self._application.automatic_operation_active

    @QtCore.Slot()
    @QtCore.Slot(bool)
    def _refresh_control_mode_availability(self,*_args) -> None:
        available = self.can_change_control_mode
        if available == self._control_mode_available:
            return
        self._control_mode_available = available
        self.sigControlModeAvailabilityChanged.emit(available)

    def _set_section_interaction_locked(self,locked: bool) -> None:
        """Lock the slmcore-owned section views during automatic operation."""
        enabled = not bool(locked)
        for section_key in self.section_collection.section_keys:
            self.section_collection.section_view(section_key).setEnabled(enabled)

    def open_measurements(self,section_key: str) -> None:
        if not self.editor_writes_allowed:
            return
        self._feedback.open_window(section_key)

    def _prepare_runtime_replacement(self) -> None:
        """Prepare Qt-owned transient workflows before a core runtime swap."""
        self._feedback.prepare_runtime_replacement()
        self._calibration.prepare_runtime_replacement()
        self._section_settings.prepare_runtime_replacement()
        self.cancel_all_cgh()
        self._binding.cancel_all(restore=True)

    def apply_config_report(
        self,report: SLMConfigLoadReport,*,failed_section_snapshots=None,
    ):
        failures = self.section_collection.apply_config_report(
            report,failed_section_snapshots=failed_section_snapshots,
        )
        for section_key,snapshot in self.section_collection.get_section_snapshots().items():
            self.section_host.set_section_title(
                section_key,getattr(snapshot.presentation,"title",None),
            )
        return failures

    def prepare_section_layout_change(self,layout):
        return self._application.prepare_section_layout_change(layout)

    def apply_prepared_section_layout_change(
        self,
        prepared,
        *,
        calibration_mismatch_policy=CalibrationMismatchPolicy.REJECT,
        correction_mismatch_policy=CorrectionMismatchPolicy.REJECT,
        topologies_by_section=None,
        presentations=None,
    ) -> bool:
        return self._application.apply_section_layout_change(
            prepared,
            calibration_mismatch_policy=calibration_mismatch_policy,
            correction_mismatch_policy=correction_mismatch_policy,
            topologies_by_section=topologies_by_section,
            presentations=presentations,
        )

    def prepare_config_load(self,path: str) -> PreparedConfigLoad:
        return self._application.prepare_config_load(path)

    def prepare_config_commit(self,*,layout_changed: bool) -> None:
        """Prepare only Qt-owned transient state before an application commit."""
        self._require_active()
        if layout_changed:
            self._prepare_runtime_replacement()
        else:
            self.prepare_runtime_state_change()
            self.cancel_pending_edits(restore=True)

    def apply_prepared_config_load(
        self,
        prepared: PreparedConfigLoad,
        *,
        calibration_mismatch_policy=CalibrationMismatchPolicy.REJECT,
        correction_mismatch_policy=CorrectionMismatchPolicy.REJECT,
        require_complete: bool=False,
    ) -> ConfigLoadOutcome:
        return self._application.apply_config_load(
            prepared,
            calibration_mismatch_policy=calibration_mismatch_policy,
            correction_mismatch_policy=correction_mismatch_policy,
            require_complete=bool(require_complete),
        )

    def list_configs(self):
        return self._application.list_configs()

    def save_application_config(
        self,name: str,info: str="",*,overwrite: bool=False,
    ):
        return self._application.save_config(name,info,overwrite=overwrite)

    def compare_config(self,path: str):
        return self._application.compare_config(path)

    def read_config_metadata(self,path: str):
        return self._application.read_config_metadata(path)

    def inspect_config(self,path: str):
        return self._application.inspect_config(path)

    def rename_config(self,path: str,new_name: str,*,overwrite: bool=False):
        return self._application.rename_config(path,new_name,overwrite=overwrite)

    def duplicate_config(self,path: str,new_name: str,*,overwrite: bool=False):
        return self._application.duplicate_config(path,new_name,overwrite=overwrite)

    def delete_config(self,path: str) -> None:
        self._application.delete_config(path)

    def load_config(self,path: str,**kwargs) -> bool:
        self._require_active()
        if not self.editor_writes_allowed:
            self.sigWarning.emit(
                "SLM config load",
                "Full config loading is unavailable in Fast Config mode.",
            )
            return False
        return self._configuration.load(path,**kwargs)

    def save_config(self,name: str,info: str="",*,overwrite: bool=False):
        self._require_active()
        self._require_editor_mode()
        return self._configuration.save(name,info,overwrite=overwrite)

    def prepare_runtime_state_change(self) -> None:
        """Cancel transient reusable workflows before an in-place runtime reload."""
        self._require_active()
        self._feedback.prepare_runtime_replacement()
        self._calibration.prepare_runtime_state_change()

    @staticmethod
    def _cgh_status_has_result(status: Any) -> bool:
        result_state = getattr(status,"result_state",None)
        result_value = getattr(result_state,"value",result_state)
        return result_value not in (None,CGHResultState.MISSING.value)

    @classmethod
    def _clear_cgh_session_needs_confirmation(
        cls,cgh_status: Any,feedback_status: Any,
    ) -> bool:
        return bool(
            cls._cgh_status_has_result(cgh_status)
            or base_cgh_recompute_would_discard_feedback(feedback_status)
            or getattr(feedback_status,"inspection_available",False)
        )

    def _restore_section(self,section_key: str) -> None:
        try:
            self.section_collection.restore_section(
                section_key,self.runtime.get_section_snapshot(section_key),
            )
            self.synchronize_section(section_key)
        except Exception as error:
            self._emit_exception("SLM section restore failed",error)

    def _emit_exception(self,title: str,error: Exception) -> None:
        # Keep user-facing error delivery structured. Hosts may independently
        # configure the ``slmcore`` logger namespace for diagnostics.
        self.sigError.emit(str(title),error)

    def _require_section(self,section_key: str) -> None:
        if section_key not in self.runtime.section_keys:
            raise KeyError(f"Unknown SLM section {section_key!r}")

    def _require_active(self) -> None:
        if self._disposed:
            raise RuntimeError("SLMQtSession has been disposed")

    def _require_editor_mode(self) -> None:
        if not self.editor_writes_allowed:
            raise RuntimeError("Operation unavailable in Fast Config mode")

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disconnect_panel()
        self._configuration.dispose()
        self._section_settings.dispose()
        self._calibration.dispose()
        self._feedback.dispose()
        self._measurements.dispose()
        self.cancel_all_cgh()
        self._binding.dispose(restore=False)
        self._disconnect_collection()
        self._application.dispose()
        self._disposed = True

    def __del__(self) -> None:
        try:
            self.dispose()
        except Exception:
            pass

def _warning_text(warning) -> str:
    path = ".".join(getattr(warning,"path",()) or ())
    message = str(getattr(warning,"message",warning))
    return "%s: %s" % (path,message) if path else message


def _same_optional_path(first: str | None,second: str | None) -> bool:
    if not first or not second:
        return first is None and second is None
    return os.path.normcase(os.path.abspath(str(first))) == os.path.normcase(
        os.path.abspath(str(second))
    )


__all__ = ["SLMQtSession"]
