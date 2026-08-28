from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any,Callable,Mapping

from ..cgh.execution.executor import CGHExecutionHandle,CGHExecutor
from ..cgh.execution.result import CGHResult
from ..engine.runtime import SLMRuntime
from ..engine.section import SectionPresentation
from ..engine.state import GroupTopology
from ..engine.transition import SectionStateTransition
from ..host.device import DeviceConnectionResult
from ..host.services import SLMHostServices
from ..config.repository import SLMConfigRepository
from .configuration import (
    CalibrationMismatchPolicy,ConfigLoadOutcome,PreparedConfigLoad,
    SLMConfigurationService,
)
from .control_mode import SLMControlMode
from .runtime_factory import SLMRuntimeFactory
from .feedback import (
    AutomaticFeedbackState,MeasurementDispatcher,SLMFeedbackCallbacks,
    SLMFeedbackService,
)
from .calibration import SLMCalibrationCallbacks,SLMCalibrationService
from .section_layout import PreparedSectionLayoutChange,SLMSectionLayoutService


def _noop(*_args,**_kwargs) -> None:
    return None


@dataclass(frozen=True)
class SLMSessionCallbacks:
    """Optional application events emitted by :class:`SLMSession`.

    Callbacks run in the context used by the configured ``CGHExecutor`` or by
    the caller of the corresponding session method. Presentation adapters are
    responsible for any thread marshalling they require.
    """

    on_frame_changed: Callable[[Any],None] = _noop
    on_transition_committed: Callable[[str,SectionStateTransition],None] = _noop
    on_section_refresh_requested: Callable[[str],None] = _noop
    on_cgh_computing_changed: Callable[[str,bool],None] = _noop
    on_warning: Callable[[str,Any],None] = _noop
    on_error: Callable[[str,Exception],None] = _noop
    on_upload_failed: Callable[[Exception],None] = _noop
    on_upload_state_changed: Callable[[bool,Exception | None],None] = _noop
    on_runtime_replaced: Callable[[SLMRuntime],None] = _noop
    on_config_committed: Callable[[ConfigLoadOutcome],None] = _noop
    on_control_mode_changed: Callable[[SLMControlMode,str | None],None] = _noop
    on_fast_config_changed: Callable[[str | None],None] = _noop
    on_calibration_planes_changed: Callable[[],None] = _noop
    on_calibration_state_changed: Callable[[str],None] = _noop
    on_calibration_measurement_busy_changed: Callable[[str,bool,str],None] = _noop
    on_calibration_measurement_error: Callable[[str,Exception],None] = _noop
    on_feedback_measurement_busy_changed: Callable[[str,bool,str],None] = _noop
    on_feedback_measurement_error: Callable[[str,Exception],None] = _noop
    on_feedback_localization_error: Callable[[str,Exception],None] = _noop
    on_automatic_feedback_changed: Callable[[AutomaticFeedbackState],None] = _noop
    on_automatic_feedback_finished: Callable[[str,str],None] = _noop


@dataclass
class _ActiveCGHRequest:
    request_id: int
    generation: int
    handle: CGHExecutionHandle | None = None
    on_finished: Callable[[bool,Exception | None],None] | None = None


class SLMSession:
    """Toolkit-independent application session for one :class:`SLMRuntime`.

    The session owns application-level CGH execution/commit lifecycle,
    configuration/control-mode state and physical frame publication. UI adapters
    may flush edits, request user confirmation and render committed transitions,
    but runtime/config state remains authoritative once an operation commits.
    """

    def __init__(
        self,
        *,
        runtime: SLMRuntime,
        host_services: SLMHostServices | None=None,
        cgh_executor: CGHExecutor | None=None,
        auto_upload_frame: bool=True,
        callbacks: SLMSessionCallbacks | None=None,
        owns_cgh_executor: bool=False,
        runtime_factory: SLMRuntimeFactory | None=None,
        config_repository: SLMConfigRepository | None=None,
        current_config_path: str | None=None,
        measurement_dispatcher: MeasurementDispatcher | None=None,
        calibration_store=None,
        startup_preferences=None,
        display_name: str="",
        apply_startup_calibration_defaults: bool=False,
    ) -> None:
        if not isinstance(runtime,SLMRuntime):
            raise TypeError("runtime must be an SLMRuntime")
        self.runtime = runtime
        self.host_services = host_services or SLMHostServices()
        self.startup_preferences = startup_preferences
        self.display_name = str(display_name or runtime.identity.key)
        self._section_layout = (
            SLMSectionLayoutService(runtime_factory=runtime_factory)
            if runtime_factory is not None else None
        )
        self._cgh_executor = cgh_executor
        self._owns_cgh_executor = bool(owns_cgh_executor)
        self._callbacks = callbacks or SLMSessionCallbacks()
        self.auto_upload_frame = bool(auto_upload_frame)
        self._configuration = (
            SLMConfigurationService(
                repository=config_repository,runtime_factory=runtime_factory,
            )
            if config_repository is not None and runtime_factory is not None
            else None
        )
        self._current_config_metadata = None
        if current_config_path and self._configuration is not None:
            self._current_config_metadata = self._configuration.read_metadata(
                current_config_path
            )
        self._control_mode = SLMControlMode.EDITOR
        self._fast_config_path: str | None = None
        self._disposed = False
        self._request_counter = 0
        self._active_cgh_requests: dict[str,_ActiveCGHRequest] = {}
        self._upload_defer_depth = 0
        self._upload_pending = False
        self._last_upload_error: Exception | None = None
        self.feedback = SLMFeedbackService(
            self,
            measurements=measurement_dispatcher,
            callbacks=self._feedback_callbacks(),
        )
        self.calibration = SLMCalibrationService(
            self,
            store=calibration_store,
            preferences=startup_preferences,
            display_name=self.display_name,
            measurements=measurement_dispatcher,
            callbacks=self._calibration_callbacks(),
        )
        if apply_startup_calibration_defaults:
            self.calibration.apply_startup_defaults()

    @property
    def last_upload_error(self) -> Exception | None:
        return self._last_upload_error

    @property
    def is_cgh_busy(self) -> bool:
        return bool(self._active_cgh_requests)

    @property
    def upload_deferred(self) -> bool:
        return self._upload_defer_depth > 0

    @property
    def section_layout_available(self) -> bool:
        service = self._section_layout
        return bool(service is not None and service.customizable)

    @property
    def configuration_available(self) -> bool:
        return self._configuration is not None

    @property
    def config_repository(self):
        """Configured repository exposed read-only for host/introspection use."""
        service = self._configuration
        return None if service is None else service.repository

    @property
    def runtime_factory(self):
        """Configured runtime factory exposed read-only for host/introspection use."""
        service = self._configuration
        if service is not None:
            return service.runtime_factory
        layout_service = self._section_layout
        return None if layout_service is None else layout_service.runtime_factory

    @property
    def config_directory(self):
        service = self._require_configuration()
        return service.directory

    @property
    def current_config_metadata(self):
        return self._current_config_metadata

    @property
    def current_config_path(self) -> str | None:
        metadata = self._current_config_metadata
        return None if metadata is None else str(metadata.path)

    @property
    def control_mode(self) -> SLMControlMode:
        return self._control_mode

    @property
    def fast_config_path(self) -> str | None:
        return self._fast_config_path

    @property
    def editor_writes_allowed(self) -> bool:
        return self._control_mode is SLMControlMode.EDITOR

    def replace_runtime(self,runtime: SLMRuntime) -> None:
        self._require_active()
        if not isinstance(runtime,SLMRuntime):
            raise TypeError("runtime must be an SLMRuntime")
        if self.is_cgh_busy:
            raise RuntimeError("Cannot replace runtime while CGH computation is active")
        self.feedback.prepare_runtime_change()
        self.calibration.prepare_runtime_change()
        self.runtime = runtime
        self._upload_pending = False
        self._last_upload_error = None
        self._notify(
            "on_runtime_replaced",runtime,
            error_title="SLM runtime presentation replacement failed",
        )
        self.calibration.runtime_replaced()

    def prepare_config_load(self,path: str) -> PreparedConfigLoad:
        self._require_active()
        service = self._require_configuration()
        return service.prepare_load(self.runtime,path)

    def apply_config_load(
        self,
        prepared: PreparedConfigLoad,
        *,
        calibration_mismatch_policy: CalibrationMismatchPolicy | str=(
            CalibrationMismatchPolicy.REJECT
        ),
        require_complete: bool=False,
        allow_in_fast_mode: bool=False,
    ) -> ConfigLoadOutcome:
        self._require_active()
        if not self.editor_writes_allowed and not allow_in_fast_mode:
            raise RuntimeError("Full config loading is unavailable in Fast Config mode")
        service = self._require_configuration()
        self.feedback.prepare_runtime_change()
        self.calibration.prepare_runtime_change()
        self.cancel_all_cgh()
        commit = service.commit_load(
            self.runtime,
            prepared,
            calibration_mismatch_policy=calibration_mismatch_policy,
            require_complete=bool(require_complete),
        )
        if commit.runtime_replaced:
            self.replace_runtime(commit.runtime)

        self._current_config_metadata = prepared.metadata
        outcome = ConfigLoadOutcome(
            path=prepared.path,
            metadata=prepared.metadata,
            report=commit.report,
            runtime_replaced=commit.runtime_replaced,
            failed_section_snapshots=commit.failed_section_snapshots,
            warnings=prepared.warnings,
        )
        self._notify(
            "on_config_committed",outcome,
            error_title="SLM config presentation synchronization failed",
        )
        if outcome.frame_changed and self.editor_writes_allowed:
            self.publish_current_frame()
        return outcome

    def load_config(
        self,
        path: str,
        *,
        calibration_mismatch_policy: CalibrationMismatchPolicy | str=(
            CalibrationMismatchPolicy.REJECT
        ),
        require_complete: bool=False,
    ) -> ConfigLoadOutcome:
        prepared = self.prepare_config_load(path)
        return self.apply_config_load(
            prepared,
            calibration_mismatch_policy=calibration_mismatch_policy,
            require_complete=require_complete,
        )

    def list_configs(self):
        return self._require_configuration().list()

    def save_config(
        self,name: str,info: str="",*,overwrite: bool=False,
    ):
        self._require_active()
        self._require_editor_mode()
        metadata = self._require_configuration().save_runtime(
            self.runtime,name,info,overwrite=overwrite,
        )
        self._current_config_metadata = metadata
        return metadata

    def compare_config(self,path: str):
        self._require_active()
        self._require_editor_mode()
        return self._require_configuration().compare_runtime(self.runtime,path)

    def read_config_metadata(self,path: str):
        return self._require_configuration().read_metadata(path)

    def inspect_config(self,path: str):
        return self._require_configuration().inspect(path)

    def rename_config(self,path: str,new_name: str,*,overwrite: bool=False):
        self._require_active()
        self._require_editor_mode()
        service = self._require_configuration()
        was_current = _same_optional_path(path,self.current_config_path)
        metadata = service.rename(path,new_name,overwrite=overwrite)
        if was_current:
            self._current_config_metadata = metadata
        return metadata

    def duplicate_config(self,path: str,new_name: str,*,overwrite: bool=False):
        self._require_active()
        self._require_editor_mode()
        return self._require_configuration().duplicate(
            path,new_name,overwrite=overwrite,
        )

    def delete_config(self,path: str) -> None:
        self._require_active()
        self._require_editor_mode()
        was_current = _same_optional_path(path,self.current_config_path)
        self._require_configuration().delete(path)
        if was_current:
            self._current_config_metadata = None

    def set_control_mode(
        self,
        mode,
        *,
        calibration_mismatch_policy: CalibrationMismatchPolicy | str=(
            CalibrationMismatchPolicy.REJECT
        ),
        prepared_config_load: PreparedConfigLoad | None=None,
    ) -> bool:
        """Switch application ownership between editable runtime and compiled config."""
        self._require_active()
        requested = SLMControlMode.normalize(mode)
        if requested is self._control_mode:
            return True
        if self.is_cgh_busy:
            raise RuntimeError(
                "Wait for the current CGH computation to finish before changing control mode"
            )
        if self.feedback.automatic_operation_active:
            raise RuntimeError(
                "Stop automatic feedback before changing control mode"
            )
        if requested is SLMControlMode.FAST_CONFIG:
            return self._enter_fast_config(
                calibration_mismatch_policy=calibration_mismatch_policy,
                prepared_config_load=prepared_config_load,
            )
        return self._leave_fast_config(
            calibration_mismatch_policy=calibration_mismatch_policy,
            prepared_config_load=prepared_config_load,
        )

    def validate_compiled_config(self,path: str) -> str:
        """Validate one compiled frame without mutating runtime/device state."""
        self._require_active()
        resolved,_frame = self._read_validated_compiled_frame(path)
        return resolved

    def activate_compiled_config(self,path: str) -> str:
        self._require_active()
        if self._control_mode is not SLMControlMode.FAST_CONFIG:
            raise RuntimeError(
                "Compiled configs can only be activated in Fast Config mode"
            )
        resolved,frame = self._read_validated_compiled_frame(path)
        if not self.upload_frame(frame,report_error=False):
            error = self.last_upload_error
            if error is not None:
                raise error
            raise RuntimeError("Compiled SLM frame upload failed")
        self._notify("on_frame_changed",frame)
        self._fast_config_path = resolved
        self._notify("on_fast_config_changed",resolved)
        return resolved

    def _enter_fast_config(
        self,
        *,
        calibration_mismatch_policy: CalibrationMismatchPolicy | str,
        prepared_config_load: PreparedConfigLoad | None=None,
    ) -> bool:
        self._require_configuration()
        current = self.current_config_path
        if current:
            resolved,frame = self._read_validated_compiled_frame(current)
            prepared = prepared_config_load or self.prepare_config_load(resolved)
            if not _same_optional_path(prepared.path,resolved):
                raise ValueError("Prepared config does not match current config path")
            self.apply_config_load(
                prepared,
                calibration_mismatch_policy=calibration_mismatch_policy,
                require_complete=True,
            )
            if not self.upload_frame(frame,report_error=False):
                error = self.last_upload_error
                if error is not None:
                    raise error
                raise RuntimeError("Compiled SLM frame upload failed")
            self._notify("on_frame_changed",frame)
            fast_path = resolved
        else:
            fast_path = None
        self._control_mode = SLMControlMode.FAST_CONFIG
        self._fast_config_path = fast_path
        self._notify("on_control_mode_changed",self._control_mode,fast_path)
        self.calibration.session_state_changed()
        return True

    def _leave_fast_config(
        self,
        *,
        calibration_mismatch_policy: CalibrationMismatchPolicy | str,
        prepared_config_load: PreparedConfigLoad | None=None,
    ) -> bool:
        fast_path = self._fast_config_path
        current_path = self.current_config_path
        if fast_path and not _same_optional_path(fast_path,current_path):
            prepared = prepared_config_load or self.prepare_config_load(fast_path)
            if not _same_optional_path(prepared.path,fast_path):
                raise ValueError("Prepared config does not match active Fast Config path")
            self.apply_config_load(
                prepared,
                calibration_mismatch_policy=calibration_mismatch_policy,
                require_complete=True,
                allow_in_fast_mode=True,
            )
        self._control_mode = SLMControlMode.EDITOR
        self._fast_config_path = None
        self._notify("on_control_mode_changed",self._control_mode,None)
        self.calibration.session_state_changed()
        self.publish_current_frame()
        return True

    def _read_validated_compiled_frame(self,path: str):
        service = self._require_configuration()
        resolved = str(service.repository.resolve(path))
        compiled = service.read_compiled_frame(resolved)
        if compiled.identity != self.runtime.identity:
            raise ValueError("Compiled config identity does not match this SLM")
        if compiled.geometry != self.runtime.geometry:
            raise ValueError(
                "Compiled config geometry %s does not match SLM geometry %s"
                % (compiled.geometry,self.runtime.geometry)
            )
        return resolved,compiled.final_eightbit

    def set_auto_upload_frame(self,enabled: bool) -> None:
        self._require_active()
        enabled = bool(enabled)
        if self.feedback.automatic_operation_active and not enabled:
            raise RuntimeError(
                "Cannot disable automatic frame upload during automatic feedback"
            )
        self.auto_upload_frame = enabled

    def initialize_device(self,*,upload_current_frame: bool=True):
        """Initialize the configured device and publish the current frame."""
        self._require_active()
        device = self.host_services.device
        if device is None:
            return None
        if device.requires_explicit_connection:
            return self.connect_device(upload_current_frame=upload_current_frame)
        if upload_current_frame:
            self.upload_current_frame()
        return DeviceConnectionResult(connected=True)

    def connect_device(
        self,*,upload_current_frame: bool=True,
    ) -> DeviceConnectionResult:
        self._require_active()
        device = self.host_services.device
        if device is None:
            raise RuntimeError("No SLM device provider is configured")
        result = device.connect()
        if result.connected and upload_current_frame:
            self.upload_current_frame()
        return result

    def disconnect_device(self) -> DeviceConnectionResult:
        self._require_active()
        device = self.host_services.device
        if device is None:
            raise RuntimeError("No SLM device provider is configured")
        return device.disconnect()

    def set_measurement_dispatcher(
        self,measurements: MeasurementDispatcher | None,
    ) -> None:
        self._require_active()
        self.feedback.set_measurement_dispatcher(measurements)
        self.calibration.set_measurement_dispatcher(measurements)

    @property
    def can_run_automatic_feedback(self) -> bool:
        return self.feedback.can_run_automatic_feedback

    @property
    def automatic_operation_active(self) -> bool:
        return self.feedback.automatic_operation_active

    def prepare_section_layout_change(
        self,layout,
    ) -> PreparedSectionLayoutChange:
        self._require_active()
        self._require_editor_mode()
        return self._require_section_layout().prepare(self.runtime,layout)

    def apply_section_layout_change(
        self,
        prepared: PreparedSectionLayoutChange,
        *,
        calibration_mismatch_policy: CalibrationMismatchPolicy | str=(
            CalibrationMismatchPolicy.REJECT
        ),
        topologies_by_section=None,
        presentations=None,
    ) -> bool:
        self._require_active()
        self._require_editor_mode()
        if self.automatic_operation_active:
            raise RuntimeError(
                "Cannot change section layout during automatic feedback"
            )
        self.cancel_all_cgh()
        replacement = self._require_section_layout().create_replacement(
            self.runtime,prepared,
            calibration_mismatch_policy=calibration_mismatch_policy,
            topologies_by_section=topologies_by_section,
            presentations=presentations,
        )
        if replacement is None:
            return False
        self.replace_runtime(replacement)
        return True

    def restore_section_current_cgh_target(self,section_key: str):
        self._require_active()
        self._require_editor_mode()
        self._require_section(section_key)
        return self.runtime.restore_section_current_cgh_target(section_key)

    def set_section_calibration(
        self,section_key: str,calibration,*,publish_frame: bool=True,
    ):
        self._require_active()
        self._require_editor_mode()
        self._require_section(section_key)
        self.cancel_cgh(section_key)
        transition = self.runtime.set_section_calibration(section_key,calibration)
        if transition is None:
            self._notify("on_section_refresh_requested",section_key)
            return None
        if publish_frame:
            self._handle_committed_transition(section_key,transition)
        else:
            self._notify(
                "on_transition_committed",section_key,transition,
                error_title="SLM section presentation synchronization failed",
            )
        return transition

    def clear_section_cgh_session(self,section_key: str):
        self._require_active()
        self._require_editor_mode()
        self._require_section(section_key)
        self.cancel_cgh(section_key)
        transition = self.runtime.clear_section_cgh_session(section_key)
        if transition is None:
            self._notify("on_section_refresh_requested",section_key)
            return None
        self._handle_committed_transition(section_key,transition)
        return transition

    def apply_section_patch(
        self,section_key: str,changes: Mapping[Any,Any],*,lattice_lock_request=None,
    ):
        self._require_active()
        self._require_editor_mode()
        self._require_section(section_key)
        return self.runtime.apply_section_patch(
            section_key,changes,lattice_lock_request=lattice_lock_request,
        )

    def apply_section_topology(
        self,section_key: str,topologies: Mapping[str,GroupTopology],
    ):
        self._require_active()
        self._require_editor_mode()
        self._require_section(section_key)
        self.cancel_cgh(section_key)
        transition = self.runtime.apply_section_topology(section_key,topologies)
        if transition is not None:
            self._handle_committed_transition(section_key,transition)
        return transition

    def set_section_presentation(
        self,section_key: str,presentation: SectionPresentation,
    ):
        self._require_active()
        self._require_editor_mode()
        self._require_section(section_key)
        return self.runtime.set_section_presentation(section_key,presentation)

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
            self._notify_error("Invalidating CGH computation failed",error)
        self._notify("on_cgh_computing_changed",section_key,False)
        self._notify("on_section_refresh_requested",section_key)
        if active.on_finished is not None:
            active.on_finished(False,RuntimeError("CGH computation cancelled"))
        return True

    def cancel_all_cgh(self) -> None:
        for section_key in tuple(self._active_cgh_requests):
            self.cancel_cgh(section_key)

    def compute_base_cgh(
        self,
        section_key: str,
        *,
        on_finished: Callable[[bool,Exception | None],None] | None=None,
    ) -> bool:
        self._require_section(section_key)
        return self._start_cgh_compute(
            section_key,
            lambda:self.runtime.prepare_section_base_cgh(section_key),
            preparation_title="CGH preparation failed",
            on_finished=on_finished,
        )

    def compute_adapted_cgh(
        self,
        section_key: str,
        *,
        on_finished: Callable[[bool,Exception | None],None] | None=None,
    ) -> bool:
        self._require_section(section_key)
        status = self.runtime.get_section_feedback_status(section_key)
        if not status.feedback_compute_pending:
            message = "No feedback adaptation is waiting to be computed."
            self._notify("on_warning","Compute adapted hologram",message)
            if on_finished is not None:
                on_finished(False,RuntimeError(message))
            return False
        return self._start_cgh_compute(
            section_key,
            lambda:self.runtime.prepare_section_adapted_cgh(section_key),
            preparation_title="Adapted CGH preparation failed",
            on_finished=on_finished,
        )

    def _start_cgh_compute(
        self,
        section_key: str,
        prepare_job: Callable[[],Any],
        *,
        preparation_title: str,
        on_finished: Callable[[bool,Exception | None],None] | None=None,
    ) -> bool:
        self._require_active()
        if self.is_cgh_computing(section_key):
            if on_finished is not None:
                on_finished(False,RuntimeError("CGH computation is already running"))
            return False
        executor = self._cgh_executor
        if executor is None:
            error = RuntimeError("No CGH executor is configured")
            self._notify_error("CGH computation failed",error)
            if on_finished is not None:
                on_finished(False,error)
            return False
        try:
            job = prepare_job()
        except Exception as error:
            self._notify_error(preparation_title,error)
            if on_finished is not None:
                on_finished(False,error)
            return False

        self._request_counter += 1
        request_id = self._request_counter
        active = _ActiveCGHRequest(
            request_id=request_id,
            generation=int(job.generation),
            on_finished=on_finished,
        )
        self._active_cgh_requests[section_key] = active
        self._notify("on_cgh_computing_changed",section_key,True)

        try:
            handle = executor.submit(
                job,
                lambda result,key=section_key,rid=request_id:
                    self._on_executor_result(key,rid,result),
                lambda error,key=section_key,rid=request_id,g=int(job.generation):
                    self._on_executor_error(key,rid,g,error),
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
            self._notify_error("CGH computation failed",error)
            if on_finished is not None:
                on_finished(False,error)
            return False
        return True

    def _on_executor_result(
        self,section_key: str,request_id: int,result: CGHResult,
    ) -> None:
        active = self._active_cgh_requests.get(section_key)
        if active is None or active.request_id != int(request_id):
            return
        callback = active.on_finished
        success = False
        completion_error: Exception | None = None
        try:
            transition = self.runtime.commit_section_cgh(section_key,result)
            if transition is None:
                completion_error = RuntimeError(
                    "CGH result was superseded before it could be committed"
                )
                self._notify("on_section_refresh_requested",section_key)
            else:
                # Runtime commit is authoritative. Presentation callback errors
                # are reported but do not undo or invalidate the committed state.
                self._notify(
                    "on_transition_committed",section_key,transition,
                    error_title="SLM section presentation synchronization failed",
                )
                success = True
                if bool(transition.frame_changed):
                    success = self.publish_current_frame()
                    if not success:
                        completion_error = self._last_upload_error
            if result.warnings:
                self._notify(
                    "on_warning","CGH computation warning","\n".join(result.warnings),
                )
        except Exception as error:
            completion_error = error
            self._notify_error("CGH result commit failed",error)
        finally:
            self._finish_cgh_request(section_key,request_id)
        if callback is not None:
            callback(bool(success),completion_error)

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
        try:
            self.runtime.mark_section_cgh_compute_failed(
                section_key,int(generation),str(error),
            )
            self._notify("on_section_refresh_requested",section_key)
        except Exception as sync_error:
            self._notify_error(
                "CGH failure-state synchronization failed",sync_error,
            )
        self._finish_cgh_request(section_key,request_id)
        self._notify_error("CGH computation failed",error)
        if callback is not None:
            callback(False,None)

    def _finish_cgh_request(self,section_key: str,request_id: int) -> None:
        active = self._active_cgh_requests.get(section_key)
        if active is None or active.request_id != int(request_id):
            return
        self._active_cgh_requests.pop(section_key,None)
        self._notify("on_cgh_computing_changed",section_key,False)

    def upload_current_frame(self) -> bool:
        """Upload the current runtime frame independent of auto-upload policy."""
        self._require_active()
        return self.upload_frame(self.runtime.artifacts.eightbit)

    def upload_frame(self,frame: Any,*,report_error: bool=True) -> bool:
        """Upload one explicit frame while retaining normalized upload state."""
        self._require_active()
        return self._upload_frame(frame,report_error=report_error)

    def publish_current_frame(self) -> bool:
        """Publish preview state and apply the configured auto-upload policy."""
        self._require_active()
        frame = self.runtime.artifacts.eightbit
        self._notify("on_frame_changed",frame)
        if not self.auto_upload_frame:
            return True
        if self._upload_defer_depth > 0:
            self._upload_pending = True
            return True
        return self._upload_frame_now()

    @contextmanager
    def defer_frame_upload(self):
        """Coalesce automatic hardware uploads across a transition batch."""
        self._require_active()
        if self.feedback.automatic_operation_active:
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

    def _upload_frame_now(self) -> bool:
        return self._upload_frame(self.runtime.artifacts.eightbit)

    def _upload_frame(self,frame: Any,*,report_error: bool=True) -> bool:
        try:
            self.host_services.upload(frame)
            self._last_upload_error = None
            self._notify("on_upload_state_changed",True,None)
            return True
        except Exception as error:
            self._last_upload_error = error
            self._notify("on_upload_state_changed",False,error)
            self._notify("on_upload_failed",error)
            if report_error:
                self._notify_error("SLM frame upload failed",error)
            return False

    def _handle_committed_transition(
        self,section_key: str,transition: SectionStateTransition,
    ) -> bool:
        if transition is None:
            return False
        self._notify(
            "on_transition_committed",section_key,transition,
            error_title="SLM section presentation synchronization failed",
        )
        if bool(transition.frame_changed):
            return self.publish_current_frame()
        return True

    def _feedback_callbacks(self) -> SLMFeedbackCallbacks:
        return SLMFeedbackCallbacks(
            on_section_changed=lambda key:self._notify(
                "on_section_refresh_requested",key,
            ),
            on_transition_committed=lambda key,transition:
                self._handle_committed_transition(key,transition),
            on_measurement_busy_changed=lambda key,busy,message:self._notify(
                "on_feedback_measurement_busy_changed",key,busy,message,
            ),
            on_measurement_error=lambda key,error:self._notify(
                "on_feedback_measurement_error",key,error,
            ),
            on_localization_error=lambda key,error:self._notify(
                "on_feedback_localization_error",key,error,
            ),
            on_automatic_state_changed=self._on_automatic_feedback_state_changed,
            on_automatic_finished=lambda key,message:self._notify(
                "on_automatic_feedback_finished",key,message,
            ),
            on_warning=lambda title,message:self._notify(
                "on_warning",title,message,
            ),
            on_error=lambda title,error:self._notify_error(title,error),
        )

    def _on_automatic_feedback_state_changed(self,state) -> None:
        if getattr(state,"active",False):
            self.calibration.prepare_runtime_change()
        else:
            self.calibration.session_state_changed()
        self._notify("on_automatic_feedback_changed",state)

    def _calibration_callbacks(self) -> SLMCalibrationCallbacks:
        return SLMCalibrationCallbacks(
            on_planes_changed=lambda:self._notify("on_calibration_planes_changed"),
            on_state_changed=lambda key:self._notify(
                "on_calibration_state_changed",key,
            ),
            on_measurement_busy_changed=lambda key,busy,message:self._notify(
                "on_calibration_measurement_busy_changed",key,busy,message,
            ),
            on_measurement_error=lambda key,error:self._notify(
                "on_calibration_measurement_error",key,error,
            ),
            on_warning=lambda title,message:self._notify(
                "on_warning",title,message,
            ),
            on_error=lambda title,error:self._notify_error(title,error),
        )

    def set_callbacks(self,callbacks: SLMSessionCallbacks | None) -> None:
        self._require_active()
        self._callbacks = callbacks or SLMSessionCallbacks()
        self.feedback.set_callbacks(self._feedback_callbacks())
        self.calibration.set_callbacks(self._calibration_callbacks())

    def _notify(
        self,name: str,*args,error_title: str="SLM application callback failed",
    ) -> bool:
        callback = getattr(self._callbacks,name,None)
        if not callable(callback):
            return True
        try:
            callback(*args)
            return True
        except Exception as error:
            if name != "on_error":
                self._notify_error(error_title,error)
            return False

    def _notify_error(self,title: str,error: Exception) -> None:
        callback = getattr(self._callbacks,"on_error",None)
        if not callable(callback):
            return
        try:
            callback(str(title),error)
        except Exception:
            # Application state must not depend on an observer being healthy.
            pass

    def _require_configuration(self) -> SLMConfigurationService:
        service = self._configuration
        if service is None:
            raise RuntimeError("Configuration storage is not configured")
        return service

    def _require_section_layout(self) -> SLMSectionLayoutService:
        service = self._section_layout
        if service is None:
            raise RuntimeError("Section layout editing is not configured")
        return service

    def _require_editor_mode(self) -> None:
        if not self.editor_writes_allowed:
            raise RuntimeError("Operation unavailable in Fast Config mode")

    def _require_section(self,section_key: str) -> None:
        if section_key not in self.runtime.section_keys:
            raise KeyError(f"Unknown SLM section {section_key!r}")

    def _require_active(self) -> None:
        if self._disposed:
            raise RuntimeError("SLMSession has been disposed")

    def dispose(self) -> None:
        if self._disposed:
            return
        self.feedback.dispose()
        self.calibration.dispose()
        self.cancel_all_cgh()
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


def _same_optional_path(first: Any,second: Any) -> bool:
    if not first or not second:
        return first is None and second is None
    from pathlib import Path
    return Path(str(first)).expanduser().resolve() == Path(str(second)).expanduser().resolve()


__all__ = ["SLMSession","SLMSessionCallbacks"]
