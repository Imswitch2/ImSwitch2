from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any,Callable,Mapping

from qtpy import QtCore

from ...application.runtime_factory import SLMRuntimeFactory
from ...calibration.store import SLMCalibrationStore
from ...config.loading import SLMConfigLoadReport
from ...config.repository import SLMConfigRepository
from ...cgh.propagation import simulate_propagation_fft
from ...cgh.execution.executor import CGHExecutionHandle,CGHExecutor
from ...cgh.execution.result import CGHResult
from ...cgh.execution.status import CGHResultState
from ...cgh.feedback.model import base_cgh_recompute_would_discard_feedback
from ...host.device import DeviceConnectionResult
from ...host.services import SLMHostServices
from ...engine.runtime import SLMRuntime
from ...engine.transition import SectionStateTransition
from ..cgh.presenter import CGHPresenter
from ..panel.panel import SLMPanel
from ..sections.group_views import CghAction
from .calibration.manager import CalibrationManager
from .cgh_executor import QtCGHExecutor
from .control_mode import SLMControlMode
from .interaction import (
    DEFAULT_RUNTIME_VIEW_INTERACTION_SETTINGS,
    RuntimeViewInteractionSettings,
)
from .runtime_binding import SLMRuntimeViewBinding
from .measurement_dispatcher import QtMeasurementDispatcher
from .feedback.coordinator import FeedbackCoordinator
from .section_settings import SectionSettingsManager
from .startup_preferences import _StartupPreferencesState
from ..configuration.manager import ConfigurationManager


@dataclass
class _ActiveCGHRequest:
    request_id: int
    generation: int
    handle: CGHExecutionHandle | None = None
    on_finished: Callable[[bool, Exception | None], None] | None = None


class SLMQtSession(QtCore.QObject):
    """Reusable Qt-side session for one :class:`SLMRuntime`.

    It owns runtime/view edit binding, CGH execution lifecycle, reusable CGH
    action dispatch, post-transition synchronization and optional automatic
    hardware upload.  Embedding applications provide capabilities, not SLM
    workflow callbacks.
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

    _sigExecutorResult = QtCore.Signal(str,int,object)
    _sigExecutorError = QtCore.Signal(str,int,int,object)

    def __init__(
        self,
        *,
        runtime: SLMRuntime,
        panel: SLMPanel,
        host_services: SLMHostServices | None=None,
        startup_preferences: _StartupPreferencesState | None=None,
        interaction_settings: RuntimeViewInteractionSettings=(
            DEFAULT_RUNTIME_VIEW_INTERACTION_SETTINGS
        ),
        cgh_executor: CGHExecutor | None=None,
        auto_upload_frame: bool=True,
        display_name: str="",
        calibration_store: SLMCalibrationStore | None=None,
        apply_startup_calibration_defaults: bool=False,
        runtime_factory: SLMRuntimeFactory | None=None,
        config_repository: SLMConfigRepository | None=None,
        current_config_path: str | None=None,
        cgh_presenter: CGHPresenter | None=None,
        parent: QtCore.QObject | None=None,
    ) -> None:
        super().__init__(parent)
        if not isinstance(runtime,SLMRuntime):
            raise TypeError("runtime must be an SLMRuntime")
        if not isinstance(panel,SLMPanel):
            raise TypeError("panel must be an SLMPanel")

        self.runtime = runtime
        self.panel = panel
        self.section_collection = panel.section_collection
        self.section_host = panel.section_host
        self.runtime_factory = runtime_factory
        self.config_repository = config_repository
        self._display_name = str(display_name or runtime.identity.key)
        self.host_services = host_services or SLMHostServices()
        self.startup_preferences = startup_preferences
        self.auto_upload_frame = bool(auto_upload_frame)
        self.presenter = cgh_presenter or CGHPresenter(
            display_name=self._display_name,
        )
        self._disposed = False
        self._request_counter = 0
        self._active_cgh_requests: dict[str, _ActiveCGHRequest] = {}
        self._upload_defer_depth = 0
        self._upload_pending = False
        self._last_upload_error: Exception | None = None
        self._interaction_settings = interaction_settings
        self._control_mode = SLMControlMode.EDITOR
        self._fast_config_path: str | None = None
        self._control_mode_available = True

        if cgh_executor is None:
            self._cgh_executor: CGHExecutor = QtCGHExecutor(parent=self)
            self._owns_cgh_executor = True
        else:
            self._cgh_executor = cgh_executor
            self._owns_cgh_executor = False

        self._sigExecutorResult.connect(self._on_executor_result)
        self._sigExecutorError.connect(self._on_executor_error)

        self._binding = self._create_binding(interaction_settings)
        self._connect_collection()
        self._measurements = QtMeasurementDispatcher(
            self.host_services.measurement_provider,
            parent=self,
        )
        self._feedback = FeedbackCoordinator(
            self,measurements=self._measurements,
        )
        self.sigAutomaticOperationChanged.connect(
            self._refresh_control_mode_availability,
        )
        self._calibration = CalibrationManager(
            self,
            measurements=self._measurements,
            store=calibration_store,
            preferences=self.startup_preferences,
            display_name=self._display_name,
            apply_startup_defaults=bool(apply_startup_calibration_defaults),
            parent=self,
        )
        self._section_settings = SectionSettingsManager(
            self,
            section_host=self.section_host,
            setup=(
                None if runtime_factory is None
                else runtime_factory.setup
            ),
            runtime_factory=runtime_factory,
            startup_preferences=self.startup_preferences,
            parent=self,
        )
        self._configuration = ConfigurationManager(
            self,
            repository=config_repository,
            controls=self.panel.config_controls,
            runtime_factory=runtime_factory,
            startup_preferences=self.startup_preferences,
            current_config_path=current_config_path,
            parent=self,
        )
        self._connect_panel()
        self._configure_device_control()
        self.synchronize_all_sections()

    @property
    def last_upload_error(self) -> Exception | None:
        return self._last_upload_error

    @property
    def display_name(self) -> str:
        return self._display_name

    @property
    def interaction_settings(self) -> RuntimeViewInteractionSettings:
        return self._interaction_settings

    @property
    def current_config_path(self) -> str | None:
        return self._configuration.current_config_path

    @property
    def control_mode(self) -> SLMControlMode:
        return self._control_mode

    @property
    def fast_config_path(self) -> str | None:
        return self._fast_config_path

    @property
    def editor_writes_allowed(self) -> bool:
        return self._control_mode is SLMControlMode.EDITOR

    @property
    def is_cgh_busy(self) -> bool:
        return bool(self._active_cgh_requests)

    @property
    def can_change_control_mode(self) -> bool:
        return bool(
            not self.is_cgh_busy
            and not self.automatic_operation_active
        )

    def set_control_mode(self,mode) -> bool:
        """Switch this single-SLM session between editor and fast-config mode."""
        self._require_active()
        requested = SLMControlMode.normalize(mode)
        if requested is self._control_mode:
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
        if requested is SLMControlMode.FAST_CONFIG:
            return self._enter_fast_config()
        return self._leave_fast_config()

    def activate_compiled_config(self,path: str) -> bool:
        """Directly activate a saved final frame without touching the runtime."""
        self._require_active()
        if self._control_mode is not SLMControlMode.FAST_CONFIG:
            self.sigWarning.emit(
                "Fast config activation",
                "Compiled configs can only be activated in Fast Config mode.",
            )
            return False
        try:
            resolved,frame = self._read_validated_compiled_frame(path)
            self._publish_compiled_frame(frame)
            self._fast_config_path = resolved
            self.panel.config_controls.set_selected_path(resolved)
            self.sigStatusChanged.emit("",False)
            return True
        except Exception as error:
            self._emit_exception("Fast config activation failed",error)
            self.panel.config_controls.set_selected_path(
                self._fast_config_path
            )
            return False

    def _enter_fast_config(self) -> bool:
        if self.config_repository is None:
            self.sigWarning.emit(
                "Fast config unavailable",
                "Configuration storage is not configured.",
            )
            return False

        path = self.current_config_path
        if path:
            # Validate the exact compiled output before discarding any unsaved
            # editor state. The authoritative mode remains EDITOR throughout
            # the subsequent strict synchronous restore.
            try:
                resolved,frame = self._read_validated_compiled_frame(path)
            except Exception as error:
                self._emit_exception("Fast config activation failed",error)
                return False
            if not self._configuration.load(
                resolved,
                confirm_layout_change=False,
                show_error=True,
                require_complete=True,
            ):
                return False
            try:
                self._publish_compiled_frame(frame)
            except Exception as error:
                self._emit_exception("Fast config activation failed",error)
                return False
            fast_path = resolved
        else:
            # Config-less fast mode is valid when a repository exists; the
            # currently displayed hardware frame is left untouched until the
            # user selects a compiled config.
            fast_path = None

        self._commit_control_mode(
            SLMControlMode.FAST_CONFIG,fast_config_path=fast_path,
        )
        return True

    def _leave_fast_config(self) -> bool:
        fast_path = self._fast_config_path
        current_path = self.current_config_path

        if fast_path and not _same_optional_path(fast_path,current_path):
            # This private full load is the controlled exception to the fast
            # write barrier: mode remains FAST_CONFIG so runtime-derived frame
            # publication stays blocked until the restore completes.
            if not self._configuration.load(
                fast_path,
                confirm_layout_change=False,
                show_error=True,
                require_complete=True,
            ):
                self.panel.config_controls.set_selected_path(fast_path)
                return False

        self._commit_control_mode(SLMControlMode.EDITOR)
        # Editor mode owns hardware/preview again. Republish even when the path
        # did not change because the saved compiled frame may differ from a
        # runtime reconstruction.
        self._publish_current_frame()
        return True

    def _commit_control_mode(
        self,
        mode: SLMControlMode,
        *,
        fast_config_path: str | None=None,
    ) -> None:
        self._control_mode = mode
        if mode is SLMControlMode.FAST_CONFIG:
            self._fast_config_path = fast_config_path
            self._binding.set_writes_enabled(False,restore_pending=True)
            self.panel.set_config_only_view(True)
            if fast_config_path is None:
                self.panel.clear_frame()
            self.panel.config_controls.set_selected_path(fast_config_path)
        else:
            self._fast_config_path = None
            self._binding.set_writes_enabled(True)
            self.panel.set_config_only_view(False)
            self.panel.config_controls.clear_selected_path_override()
            self._calibration.control_mode_changed()
        self._feedback.refresh_automatic_availability()
        self.sigControlModeChanged.emit(mode)

    def _read_validated_compiled_frame(self,path: str):
        repository = self.config_repository
        if repository is None:
            raise RuntimeError("Configuration storage is not configured")
        resolved = str(repository.resolve(path))
        compiled = repository.read_compiled_frame(resolved)
        if compiled.identity != self.runtime.identity:
            raise ValueError(
                "Compiled config identity does not match this SLM"
            )
        expected = self.runtime.geometry
        if (
            compiled.geometry.width != expected.width
            or compiled.geometry.height != expected.height
        ):
            raise ValueError(
                "Compiled config physical shape %s does not match SLM shape %s"
                % (compiled.geometry.shape,expected.shape)
            )
        return resolved,compiled.final_eightbit

    def _publish_compiled_frame(self,frame) -> None:
        """The sole hardware/preview publication path in FAST_CONFIG."""
        try:
            self.host_services.upload(frame)
            self._last_upload_error = None
        except Exception as error:
            self._last_upload_error = error
            self.sigUploadFailed.emit(error)
            raise
        self.sigFrameChanged.emit(frame)

    @QtCore.Slot(str)
    def _on_config_load_requested(self,path: str) -> None:
        if self._control_mode is SLMControlMode.FAST_CONFIG:
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
        self._publish_active_device_frame()
        return DeviceConnectionResult(connected=True)

    def connect_device(self,*,show_error: bool=True):
        self._require_active()
        device = self.host_services.device
        if device is None:
            raise RuntimeError("No SLM device provider is configured")
        self.panel.set_connection_busy(True)
        try:
            result = device.connect()
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
            result = device.disconnect()
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
        if self._control_mode is SLMControlMode.FAST_CONFIG:
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
        if self._control_mode is SLMControlMode.FAST_CONFIG:
            if not self._fast_config_path:
                return True
            return self.activate_compiled_config(self._fast_config_path)
        return self.upload_current_frame()

    def _create_binding(
        self,settings: RuntimeViewInteractionSettings,
    ) -> SLMRuntimeViewBinding:
        binding = SLMRuntimeViewBinding(
            runtime=self.runtime,
            section_collection=self.section_collection,
            interaction_settings=settings,
            parent=self,
        )
        binding.sigPatchApplied.connect(self._on_patch_applied)
        binding.sigPatchFailed.connect(self._on_patch_failed)
        binding.sigAutoComputeRequested.connect(self._on_auto_compute_requested)
        return binding

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
        enabled = bool(enabled)
        if self._feedback.automatic_operation_active and not enabled:
            raise RuntimeError(
                "Cannot disable automatic frame upload during automatic feedback"
            )
        self.auto_upload_frame = enabled
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
        return section_key in self._active_cgh_requests

    def cancel_cgh(self,section_key: str) -> bool:
        self._require_active()
        active = self._active_cgh_requests.pop(section_key,None)
        if active is None:
            return False
        cancel = getattr(active.handle,"cancel",None)
        if callable(cancel):
            cancel()
        try:
            self.runtime.invalidate_section_cgh_compute(section_key)
        except Exception as error:
            self._emit_exception("Invalidating CGH computation failed",error)
        self._set_cgh_computing(section_key,False)
        self.synchronize_section(section_key)
        if active.on_finished is not None:
            active.on_finished(False,RuntimeError("CGH computation cancelled"))
        return True

    def cancel_all_cgh(self) -> None:
        for section_key in tuple(self._active_cgh_requests):
            self.cancel_cgh(section_key)

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

        self._start_cgh_compute(
            section_key,
            lambda:self.runtime.prepare_section_base_cgh(section_key),
            preparation_title="CGH preparation failed",
        )

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
        return self._start_cgh_compute(
            section_key,
            lambda:self.runtime.prepare_section_adapted_cgh(section_key),
            preparation_title="Adapted CGH preparation failed",
            on_finished=on_finished,
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

        self.cancel_cgh(section_key)
        try:
            self.flush_section(section_key,propagate=True)
            transition = self.runtime.clear_section_cgh_session(section_key)
            if transition is not None:
                self.apply_transition(section_key,transition)
            else:
                self.synchronize_section(section_key)
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
        if self._feedback.automatic_operation_active:
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

    def _start_cgh_compute(
        self,
        section_key: str,
        prepare_job: Callable[[],Any],
        *,
        preparation_title: str,
        on_finished: Callable[[bool, Exception | None], None] | None=None,
    ) -> bool:
        if not self.editor_writes_allowed:
            if on_finished is not None:
                on_finished(False,RuntimeError("CGH computation is unavailable in Fast Config mode"))
            return False
        if self.is_cgh_computing(section_key):
            if on_finished is not None:
                on_finished(False,RuntimeError("CGH computation is already running"))
            return False
        try:
            self.flush_section(section_key,propagate=True)
            job = prepare_job()
        except Exception as error:
            self._emit_exception(preparation_title,error)
            if on_finished is not None:
                on_finished(False,None)
            return False

        self._request_counter += 1
        request_id = self._request_counter
        active = _ActiveCGHRequest(
            request_id=request_id,
            generation=int(job.generation),
            on_finished=on_finished,
        )
        self._active_cgh_requests[section_key] = active
        self._set_cgh_computing(section_key,True)

        try:
            handle = self._cgh_executor.submit(
                job,
                lambda result,key=section_key,rid=request_id:
                    self._sigExecutorResult.emit(key,rid,result),
                lambda error,key=section_key,rid=request_id,g=int(job.generation):
                    self._sigExecutorError.emit(key,rid,g,error),
            )
            current = self._active_cgh_requests.get(section_key)
            if current is not None and current.request_id == request_id:
                current.handle = handle
            elif handle is not None:
                cancel = getattr(handle,"cancel",None)
                if callable(cancel):
                    cancel()
        except Exception as error:
            self._finish_cgh_request(section_key,request_id)
            self._emit_exception("CGH computation failed",error)
            if on_finished is not None:
                on_finished(False,None)
            return False
        return True

    @QtCore.Slot(str,int,object)
    def _on_executor_result(
        self,section_key: str,request_id: int,result: CGHResult,
    ) -> None:
        active = self._active_cgh_requests.get(section_key)
        if active is None or active.request_id != int(request_id):
            return
        callback = active.on_finished
        if not self.editor_writes_allowed:
            self._finish_cgh_request(section_key,request_id)
            if callback is not None:
                callback(
                    False,
                    RuntimeError("CGH result ignored in Fast Config mode"),
                )
            return
        success = False
        completion_error: Exception | None = None
        try:
            transition = self.runtime.commit_section_cgh(section_key,result)
            if transition is None:
                completion_error = RuntimeError(
                    "CGH result was superseded before it could be committed"
                )
                self.synchronize_section(section_key)
            else:
                success = self.apply_transition(section_key,transition)
                if not success:
                    # Transition/UI/upload failures are already reported by the
                    # session. Preserve the upload exception only so an
                    # automatic runner can identify the hardware failure.
                    completion_error = self._last_upload_error
            if result.warnings:
                self.sigWarning.emit(
                    "CGH computation warning","\n".join(result.warnings),
                )
        except Exception as error:
            completion_error = error
            self._restore_section(section_key)
            self._emit_exception("CGH result synchronization failed",error)
        finally:
            self._finish_cgh_request(section_key,request_id)
        if callback is not None:
            callback(bool(success),completion_error)

    @QtCore.Slot(str,int,int,object)
    def _on_executor_error(
        self,
        section_key: str,
        request_id: int,
        generation: int,
        error: Exception,
    ) -> None:
        active = self._active_cgh_requests.get(section_key)
        if active is None or active.request_id != int(request_id):
            return
        callback = active.on_finished
        if not self.editor_writes_allowed:
            self._finish_cgh_request(section_key,request_id)
            if callback is not None:
                callback(
                    False,
                    RuntimeError("CGH failure ignored in Fast Config mode"),
                )
            return
        try:
            self.runtime.mark_section_cgh_compute_failed(
                section_key,int(generation),str(error),
            )
            self.synchronize_section(section_key)
        except Exception as sync_error:
            self._emit_exception(
                "CGH failure-state synchronization failed",sync_error,
            )
        self._finish_cgh_request(section_key,request_id)
        self._emit_exception("CGH computation failed",error)
        if callback is not None:
            callback(False,None)

    def _finish_cgh_request(self,section_key: str,request_id: int) -> None:
        active = self._active_cgh_requests.get(section_key)
        if active is None or active.request_id != int(request_id):
            return
        self._active_cgh_requests.pop(section_key,None)
        self._set_cgh_computing(section_key,False)

    def _set_cgh_computing(self,section_key: str,computing: bool) -> None:
        self.section_collection.set_cgh_computing(section_key,bool(computing))
        if hasattr(self,"_feedback"):
            self._feedback.set_cgh_computing(section_key,bool(computing))
        if hasattr(self,"_calibration"):
            self._calibration.set_cgh_computing(section_key,bool(computing))
        self.sigCghComputingChanged.emit(section_key,bool(computing))
        self._refresh_control_mode_availability()

    @QtCore.Slot(str)
    def _on_auto_compute_requested(self,section_key: str) -> None:
        if not self.editor_writes_allowed:
            return
        if self._feedback.automatic_operation_active:
            return
        if self.is_cgh_computing(section_key):
            return
        cgh_status = self.runtime.get_section_cgh_status(section_key)
        if getattr(cgh_status,"target_type",None) is None:
            return
        status = self.runtime.get_section_feedback_status(section_key)
        if base_cgh_recompute_would_discard_feedback(status):
            return
        self._start_cgh_compute(
            section_key,
            lambda:self.runtime.prepare_section_base_cgh(section_key),
            preparation_title="Automatic CGH preparation failed",
        )

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
        """Apply a committed runtime transition through one synchronization path."""
        if not self.editor_writes_allowed:
            return False
        if transition is None:
            return False
        try:
            self.section_collection.apply_section_transition(
                section_key,transition,
            )
        except Exception as error:
            self._restore_section(section_key)
            self._emit_exception("SLM section UI synchronization failed",error)
            return False

        self.synchronize_section(section_key)
        if bool(transition.frame_changed):
            return self._publish_current_frame()
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
        return self._upload_frame_now()

    def publish_current_frame(self) -> None:
        """Publish preview state and apply the configured auto-upload policy."""
        self._require_active()
        if not self.editor_writes_allowed:
            return
        self._publish_current_frame()

    @contextmanager
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
        if self._feedback.automatic_operation_active:
            raise RuntimeError(
                "Cannot defer frame upload during automatic feedback"
            )
        self._upload_defer_depth += 1
        try:
            yield self
        finally:
            self._upload_defer_depth -= 1
            if self._upload_defer_depth == 0 and self._upload_pending:
                self._upload_pending = False
                if self.auto_upload_frame:
                    self._upload_frame_now()

    def _publish_current_frame(self) -> bool:
        if not self.editor_writes_allowed:
            return False
        frame = self.runtime.artifacts.eightbit
        self.sigFrameChanged.emit(frame)
        if not self.auto_upload_frame:
            return True
        if self._upload_defer_depth > 0:
            self._upload_pending = True
            return True
        return self._upload_frame_now()

    def _upload_frame_now(self) -> bool:
        if not self.editor_writes_allowed:
            return False
        try:
            self.host_services.upload(self.runtime.artifacts.eightbit)
            self._last_upload_error = None
            if hasattr(self,"_calibration"):
                self._calibration.refresh_live_acquisition_all()
            return True
        except Exception as error:
            self._last_upload_error = error
            if hasattr(self,"_calibration"):
                self._calibration.refresh_live_acquisition_all()
            self.sigUploadFailed.emit(error)
            self._emit_exception("SLM frame upload failed",error)
            return False

    @property
    def can_run_automatic_feedback(self) -> bool:
        return bool(
            self.editor_writes_allowed
            and self.auto_upload_frame
            and self.host_services.can_upload_frame
            and self._measurements.available
            and self._upload_defer_depth == 0
        )

    @property
    def automatic_operation_active(self) -> bool:
        return self._feedback.automatic_operation_active

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

    def replace_runtime(self,runtime: SLMRuntime) -> None:
        """Replace runtime and retained section views through one canonical path."""
        self._require_active()
        if not isinstance(runtime,SLMRuntime):
            raise TypeError("runtime must be an SLMRuntime")

        settings = self._binding.interaction_settings
        self._feedback.prepare_runtime_replacement()
        self._calibration.prepare_runtime_replacement()
        self._section_settings.prepare_runtime_replacement()
        self.cancel_all_cgh()
        old_binding = self._binding
        old_binding.cancel_all(restore=True)
        old_binding.dispose(restore=False)
        self._disconnect_collection()

        replacement = self.panel.replace_sections(
            runtime.get_section_snapshots(),
        )

        self.runtime = runtime
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
        self._publish_current_frame()


    def runtime_layout_signature(self):
        from ...engine.section import split_layout_signature
        return split_layout_signature(
            self.runtime.geometry,
            {key:self.runtime.get_section_geometry(key) for key in self.runtime.section_keys},
        )

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
        if self._owns_cgh_executor:
            dispose = getattr(self._cgh_executor,"dispose",None)
            if callable(dispose):
                dispose()
        self._disposed = True

    def __del__(self) -> None:
        try:
            self.dispose()
        except Exception:
            pass

def _same_optional_path(first: str | None,second: str | None) -> bool:
    if not first or not second:
        return first is None and second is None
    return os.path.normcase(os.path.abspath(str(first))) == os.path.normcase(
        os.path.abspath(str(second))
    )


__all__ = ["SLMQtSession"]
