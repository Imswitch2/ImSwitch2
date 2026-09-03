"""Toolkit-independent feedback workflow orchestration.

The feedback service owns measurement/localization/adaptation state changes and
automatic intensity-feedback sequencing. Presentation adapters may flush pending
editor drafts before invoking it, collect destructive-operation confirmations,
and render the callbacks emitted after authoritative application changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any,Callable,Mapping,Protocol,Sequence,TYPE_CHECKING

import numpy as np

from ..core.cgh.execution.status import CGHResultState
from ..core.cgh.feedback import (
    EditablePositionReferenceGeometry,
    FeedbackCapability,
    FeedbackOrientation,
    FOVPositionCalibration,
    PositionReference,
    PositionReferenceMode,
    fit_center_reference_geometry,
    editable_position_reference_geometry,
    editable_reference_center_px,
    editable_reference_positions,
    localization_positions_full_px,
    orient_localization,
    reference_positions_for_localization,
)
from ..core.cgh.localization import infer_missing_localization
from ..core.cgh.localization.policy import suggest_localization_sources
from ..core.cgh.propagation import simulate_propagation_fft
from ..core.measurement import ImageMeasurement

if TYPE_CHECKING:
    from .session import SLMSession


def _noop(*_args,**_kwargs) -> None:
    return None


_EDITABLE_POSITION_REFERENCE_LOCK_FIELDS = frozenset({
    "period_x_px","period_y_px","rotation_deg","lattice_angle_deg",
})


def _normalize_editable_locks(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value,Mapping):
        names = {str(name) for name,locked in value.items() if bool(locked)}
    else:
        names = {str(name) for name in value}
    unknown = names-_EDITABLE_POSITION_REFERENCE_LOCK_FIELDS
    if unknown:
        raise ValueError(
            "Unknown editable reference lock(s): %s"
            % ", ".join(sorted(unknown))
        )
    return frozenset(names)


class MeasurementRequest(Protocol):
    """Minimal cancellable measurement request consumed by feedback workflows."""

    @property
    def active(self) -> bool: ...

    def cancel(self) -> None: ...


class MeasurementDispatcher(Protocol):
    """Host-neutral asynchronous measurement dispatch contract.

    Implementations decide the execution context of completion callbacks. A Qt
    adapter can therefore marshal callbacks onto the GUI thread without making
    the application service depend on Qt.
    """

    @property
    def available(self) -> bool: ...

    def available_sources(self,section_key: str) -> Sequence[str]: ...

    def preferred_source(
        self,section_key: str,available: Sequence[str],
    ) -> str | None: ...

    def acquire(
        self,
        section_key: str,
        source: str,
        *,
        metadata: Mapping[str,Any] | None,
        on_result: Callable[[ImageMeasurement],None],
        on_error: Callable[[Exception],None],
    ) -> MeasurementRequest: ...


@dataclass(frozen=True)
class AutomaticFeedbackState:
    """Immutable observable state of the automatic intensity-feedback loop."""

    active: bool=False
    section_key: str | None=None
    requested_rounds: int=0
    completed_rounds: int=0
    stop_requested: bool=False
    stage: str="idle"
    progress: str=""


@dataclass(frozen=True)
class FeedbackParameterUpdateResult:
    changed: bool
    candidate_analysis: Any=None


@dataclass(frozen=True)
class FeedbackOrientationContext:
    """Current transient orientation and its persistent plane/default context."""

    orientation: FeedbackOrientation
    saved_orientation: FeedbackOrientation
    plane_name: str | None=None
    plane_override: bool=False
    change_allowed: bool=True
    change_unavailable_reason: str=""

    @property
    def save_needed(self) -> bool:
        return self.orientation is not self.saved_orientation


@dataclass(frozen=True)
class PositionReferenceSelection:
    """Transient per-section/plane position-reference choice."""

    mode: PositionReferenceMode=PositionReferenceMode.GLOBAL_FIT
    saved_name: str | None=None
    editable_geometry: EditablePositionReferenceGeometry | None=None
    editable_locks: frozenset[str]=frozenset()

    def __post_init__(self) -> None:
        mode = PositionReferenceMode.normalize(self.mode)
        saved_name = (
            None
            if self.saved_name is None or not str(self.saved_name).strip()
            else str(self.saved_name).strip()
        )
        geometry = self.editable_geometry
        if geometry is not None and not isinstance(geometry,EditablePositionReferenceGeometry):
            geometry = EditablePositionReferenceGeometry.from_mapping(geometry)
        locks = _normalize_editable_locks(self.editable_locks)
        if mode is PositionReferenceMode.SAVED and saved_name is None:
            raise ValueError("Saved position-reference mode requires a name")
        object.__setattr__(self,"mode",mode)
        object.__setattr__(self,"saved_name",saved_name)
        object.__setattr__(self,"editable_geometry",geometry)
        object.__setattr__(self,"editable_locks",locks)


@dataclass(frozen=True)
class PositionReferenceContext:
    """UI-facing position-reference state for one feedback section."""

    selection: PositionReferenceSelection
    plane_name: str | None
    saved_names: tuple[str,...]=()
    compatible: bool=True
    compatibility_error: str=""
    change_allowed: bool=True
    change_unavailable_reason: str=""
    save_allowed: bool=False
    save_unavailable_reason: str=""
    fit_geometry: EditablePositionReferenceGeometry | None=None


@dataclass(frozen=True)
class SLMFeedbackCallbacks:
    """Presentation-neutral events emitted by :class:`SLMFeedbackService`."""

    on_section_changed: Callable[[str],None]=_noop
    on_transition_committed: Callable[[str,Any],None]=_noop
    on_measurement_busy_changed: Callable[[str,bool,str],None]=_noop
    on_measurement_error: Callable[[str,Exception],None]=_noop
    on_localization_error: Callable[[str,Exception],None]=_noop
    on_automatic_state_changed: Callable[[AutomaticFeedbackState],None]=_noop
    on_automatic_finished: Callable[[str,str],None]=_noop
    on_warning: Callable[[str,Any],None]=_noop
    on_error: Callable[[str,Exception],None]=_noop


@dataclass
class _AutomaticRun:
    run_id: int
    section_key: str
    requested_rounds: int
    source: str
    reuse_previous_localization: bool
    completed_rounds: int=0
    stop_requested: bool=False
    stage: str="starting"


class AutomaticFeedbackRunner:
    """Sequence automatic intensity feedback using application services only."""

    def __init__(self,service: "SLMFeedbackService") -> None:
        self._service = service
        self._counter = 0
        self._run: _AutomaticRun | None = None

    @property
    def active(self) -> bool:
        return self._run is not None

    @property
    def section_key(self) -> str | None:
        return None if self._run is None else self._run.section_key

    @property
    def state(self) -> AutomaticFeedbackState:
        run = self._run
        if run is None:
            return AutomaticFeedbackState()
        return AutomaticFeedbackState(
            active=True,
            section_key=run.section_key,
            requested_rounds=run.requested_rounds,
            completed_rounds=run.completed_rounds,
            stop_requested=run.stop_requested,
            stage=run.stage,
            progress=self._progress(run),
        )

    def start(
        self,
        section_key: str,
        *,
        rounds: int,
        source: str,
        reuse_previous_localization: bool,
    ) -> bool:
        if self._run is not None:
            return False
        if not self._service.can_run_automatic_feedback:
            self._service._error(
                "Automatic feedback unavailable",
                RuntimeError(self._service.automatic_feedback_unavailable_reason),
            )
            return False
        rounds = int(rounds)
        if rounds <= 0:
            self._service._error(
                "Automatic feedback failed",ValueError("Rounds must be > 0"),
            )
            return False
        source = str(source or "").strip()
        if not source:
            self._service._error(
                "Automatic feedback failed",
                ValueError("Select a detector before starting automatic feedback."),
            )
            return False
        if self._service.session.is_cgh_computing(section_key):
            self._service._error(
                "Automatic feedback failed",
                RuntimeError("Wait for the current CGH computation to finish."),
            )
            return False
        cgh_status = self._service.session.runtime.get_section_cgh_status(section_key)
        if cgh_status.result_state is not CGHResultState.CURRENT:
            self._service._error(
                "Automatic feedback failed",
                RuntimeError("Automatic feedback requires a current computed CGH."),
            )
            return False

        self._counter += 1
        self._run = _AutomaticRun(
            run_id=self._counter,
            section_key=section_key,
            requested_rounds=rounds,
            source=source,
            reuse_previous_localization=bool(reuse_previous_localization),
        )
        self._emit_state()
        self._start_next_round(self._run.run_id)
        return True

    def stop(self) -> None:
        run = self._run
        if run is None:
            return
        run.stop_requested = True
        if run.stage == "acquiring":
            self._service.cancel_measurement(run.section_key)
            self._finish(cancelled=True)
            return
        self._emit_state()

    def cancel_for_runtime_change(self) -> None:
        run = self._run
        if run is None:
            return
        self._service.cancel_measurement(run.section_key)
        self._run = None
        self._emit_state()

    def _start_next_round(self,run_id: int) -> None:
        run = self._current(run_id)
        if run is None:
            return
        if run.stop_requested:
            self._finish(cancelled=True)
            return
        if run.completed_rounds >= run.requested_rounds:
            self._finish(cancelled=False)
            return

        run.stage = "acquiring"
        self._emit_state()
        self._service.request_measurement(
            run.section_key,
            run.source,
            metadata=self._service.feedback_measurement_metadata(run.section_key),
            on_result=lambda measurement,rid=run_id:self._on_measurement(rid,measurement),
            on_error=lambda error,rid=run_id:self._fail(rid,error),
        )

    def _on_measurement(self,run_id: int,measurement: ImageMeasurement) -> None:
        run = self._current(run_id)
        if run is None:
            return
        if run.stop_requested:
            self._finish(cancelled=True)
            return
        try:
            previous_available = self._service.commit_measurement(
                run.section_key,measurement,reuse_previous_localization=False,
            )
            run.stage = "localizing"
            self._emit_state()
            localized = False
            if run.reuse_previous_localization and previous_available:
                try:
                    self._service.reuse_localization(
                        run.section_key,raise_errors=True,
                    )
                    localized = True
                except Exception as error:
                    self._service._warning(
                        "Automatic feedback",
                        "Previous localization could not be reused; running "
                        "localization instead.\n%s" % error,
                    )
            if not localized:
                self._service.localize_and_commit(run.section_key)

            if run.stop_requested:
                self._finish(cancelled=True)
                return

            run.stage = "adapting"
            self._emit_state()
            ok,_transition = self._service.apply_intensity_feedback(
                run.section_key,raise_errors=True,
            )
            if not ok:
                raise RuntimeError("Intensity adaptation was not applied")

            run.stage = "computing"
            self._emit_state()
            started = self._service.session.compute_adapted_cgh(
                run.section_key,
                on_finished=lambda success,error,rid=run_id:
                    self._on_cgh_finished(rid,success,error),
            )
            if not started and self._current(run_id) is not None:
                raise RuntimeError("Adapted CGH computation did not start")
        except Exception as error:
            self._fail(run_id,error)

    def _on_cgh_finished(
        self,run_id: int,success: bool,error: Exception | None,
    ) -> None:
        run = self._current(run_id)
        if run is None:
            return
        if not success:
            if error is None or error is self._service.session.last_upload_error:
                self._finish(cancelled=False,failed=True)
            else:
                self._fail(run_id,error)
            return
        run.completed_rounds += 1
        if run.stop_requested:
            self._finish(cancelled=True)
            return
        self._start_next_round(run_id)

    def _fail(self,run_id: int,error: Exception) -> None:
        if self._current(run_id) is None:
            return
        self._service._error("Automatic feedback failed",error)
        self._finish(cancelled=False,failed=True)

    def _finish(self,*,cancelled: bool,failed: bool=False) -> None:
        run = self._run
        if run is None:
            return
        self._service.cancel_measurement(run.section_key)
        self._run = None
        self._emit_state()
        if failed:
            text = "Automatic feedback stopped after an error."
        elif cancelled:
            text = "Automatic feedback stopped."
        else:
            text = "Automatic feedback completed (%d round(s))." % run.completed_rounds
        self._service._callbacks.on_automatic_finished(run.section_key,text)

    def _current(self,run_id: int) -> _AutomaticRun | None:
        run = self._run
        if run is None or run.run_id != int(run_id):
            return None
        return run

    def _emit_state(self) -> None:
        self._service._callbacks.on_automatic_state_changed(self.state)

    @staticmethod
    def _progress(run: _AutomaticRun) -> str:
        current = min(run.completed_rounds + 1,run.requested_rounds)
        if run.stop_requested and run.stage != "acquiring":
            return "Stopping after current operation..."
        labels = {
            "starting":"Automatic feedback starting...",
            "acquiring":"Round %d/%d · acquiring measurement..." % (
                current,run.requested_rounds,
            ),
            "localizing":"Round %d/%d · localizing..." % (
                current,run.requested_rounds,
            ),
            "adapting":"Round %d/%d · applying feedback..." % (
                current,run.requested_rounds,
            ),
            "computing":"Round %d/%d · computing adapted CGH..." % (
                current,run.requested_rounds,
            ),
        }
        return labels.get(run.stage,"Automatic feedback running...")


class SLMFeedbackService:
    """Application service for one session's measurement/feedback workflow."""

    def __init__(
        self,
        session: "SLMSession",
        *,
        measurements: MeasurementDispatcher | None=None,
        callbacks: SLMFeedbackCallbacks | None=None,
        position_reference_store=None,
        fov_position_calibration_store=None,
    ) -> None:
        self.session = session
        self.measurements = measurements
        self._callbacks = callbacks or SLMFeedbackCallbacks()
        self._measurement_requests: dict[str,MeasurementRequest] = {}
        # Runtime/test orientation is deliberately separate from startup
        # preferences. It is scoped to the currently active plane and only
        # becomes persistent through save_feedback_orientation().
        self._feedback_orientations: dict[
            str,tuple[str | None,FeedbackOrientation]
        ] = {}
        self._position_reference_store = position_reference_store
        self._fov_position_calibration_store = fov_position_calibration_store
        self._fov_position_calibration_selections: dict[
            tuple[str,str | None],dict[str,Any]
        ] = {}
        self._fov_position_calibration_candidates: dict[
            tuple[str,str | None],FOVPositionCalibration
        ] = {}
        self._position_reference_selections: dict[
            tuple[str,str | None],PositionReferenceSelection
        ] = {}
        if self._position_reference_store is not None:
            self._position_reference_store.add_listener(
                self._on_position_reference_store_changed
            )
        if self._fov_position_calibration_store is not None:
            self._fov_position_calibration_store.add_listener(
                self._on_fov_position_calibration_store_changed
            )
        self._automatic = AutomaticFeedbackRunner(self)

    def set_callbacks(self,callbacks: SLMFeedbackCallbacks | None) -> None:
        self._callbacks = callbacks or SLMFeedbackCallbacks()

    def set_measurement_dispatcher(
        self,measurements: MeasurementDispatcher | None,
    ) -> None:
        if measurements is self.measurements:
            return
        self.prepare_runtime_change()
        self.measurements = measurements

    @property
    def automatic_operation_active(self) -> bool:
        return self._automatic.active

    @property
    def automatic_state(self) -> AutomaticFeedbackState:
        return self._automatic.state

    @property
    def can_run_automatic_feedback(self) -> bool:
        measurements = self.measurements
        return bool(
            self.session.editor_writes_allowed
            and self.session.auto_upload_frame
            and self.session.host_services.can_upload_frame
            and measurements is not None
            and measurements.available
            and not self.session.upload_deferred
        )

    @property
    def automatic_feedback_unavailable_reason(self) -> str:
        if not self.session.editor_writes_allowed:
            return "Automatic feedback is unavailable in Fast Config mode."
        if not self.session.auto_upload_frame:
            return "Automatic feedback is disabled when auto_upload_frame=False."
        if not self.session.host_services.can_upload_frame:
            return "Automatic feedback requires a frame-upload capability."
        if self.measurements is None or not self.measurements.available:
            return "Automatic feedback requires a measurement provider."
        if self.session.upload_deferred:
            return "Automatic feedback is unavailable while frame upload is deferred."
        return ""

    def available_sources(self,section_key: str) -> Sequence[str]:
        measurements = self.measurements
        if measurements is None:
            return ()
        return tuple(measurements.available_sources(section_key))

    def preferred_source(
        self,section_key: str,available: Sequence[str],
    ) -> str | None:
        measurements = self.measurements
        if measurements is None:
            return None
        return measurements.preferred_source(section_key,available)

    def _active_feedback_plane(self,section_key: str) -> str | None:
        calibration = getattr(self.session,"calibration",None)
        if calibration is None:
            return None
        plane = calibration.active_plane(section_key)
        return str(plane or "").strip() or None

    def _saved_feedback_orientation(
        self,section_key: str,plane_name: str | None,
    ) -> FeedbackOrientation:
        preferences = self.session.startup_preferences
        value = (
            "identity" if preferences is None
            else preferences.feedback_orientation(section_key,plane_name)
        )
        return FeedbackOrientation.normalize(value)

    def _feedback_orientation_change_reason(self,section_key: str) -> str:
        status = self.session.runtime.get_section_cgh_status(section_key)
        current = status.current_round_index
        feedback_cgh_computed = bool(
            current is not None
            and (int(current) > 0 or bool(status.position_active))
        )
        if feedback_cgh_computed:
            return (
                "Feedback orientation is locked after a feedback-corrected "
                "hologram has been computed. Reset the intensity feedback to "
                "the base round or clear the position correction before "
                "changing orientation."
            )
        return ""

    def feedback_orientation(self,section_key: str) -> FeedbackOrientation:
        section = str(section_key)
        plane = self._active_feedback_plane(section)
        current = self._feedback_orientations.get(section)
        if current is None or current[0] != plane:
            orientation = self._saved_feedback_orientation(section,plane)
            self._feedback_orientations[section] = (plane,orientation)
            return orientation
        return current[1]

    def feedback_orientation_context(
        self,section_key: str,
    ) -> FeedbackOrientationContext:
        section = str(section_key)
        plane = self._active_feedback_plane(section)
        orientation = self.feedback_orientation(section)
        saved = self._saved_feedback_orientation(section,plane)
        preferences = self.session.startup_preferences
        plane_override = bool(
            plane
            and preferences is not None
            and preferences.feedback_orientation_for_plane(section,plane) is not None
        )
        reason = self._feedback_orientation_change_reason(section)
        return FeedbackOrientationContext(
            orientation=orientation,
            saved_orientation=saved,
            plane_name=plane,
            plane_override=plane_override,
            change_allowed=not bool(reason),
            change_unavailable_reason=reason,
        )

    def _position_reference_key(
        self,section_key: str,
    ) -> tuple[str,str | None]:
        section = str(section_key)
        return section,self._active_feedback_plane(section)

    def _position_reference_change_reason(self,section_key: str) -> str:
        status = self.session.runtime.get_section_feedback_status(section_key)
        if bool(status.position_available):
            return (
                "Clear the current position correction before changing its "
                "reference."
            )
        return ""

    def position_reference_selection(
        self,section_key: str,
    ) -> PositionReferenceSelection:
        key = self._position_reference_key(section_key)
        selection = self._position_reference_selections.get(key)
        if selection is None:
            selection = PositionReferenceSelection()
            self._position_reference_selections[key] = selection
        if (
            selection.mode is PositionReferenceMode.SAVED
            and (
                key[1] is None
                or self._position_reference_store is None
                or not self._position_reference_store.exists(
                    key[1],selection.saved_name
                )
            )
        ):
            selection = PositionReferenceSelection(
                editable_geometry=selection.editable_geometry,
                editable_locks=selection.editable_locks,
            )
            self._position_reference_selections[key] = selection
        return selection

    def position_reference_context(
        self,section_key: str,
    ) -> PositionReferenceContext:
        section = str(section_key)
        plane = self._active_feedback_plane(section)
        selection = self.position_reference_selection(section)
        saved_names = (
            ()
            if plane is None or self._position_reference_store is None
            else self._position_reference_store.list(plane)
        )
        compatible = True
        compatibility_error = ""
        inspection = self.session.runtime.get_section_feedback_inspection(section)
        has_localization = bool(
            inspection.measurement is not None
            and inspection.measurement.localization is not None
        )
        try:
            if has_localization:
                self._resolve_position_reference(section)
        except Exception as error:
            compatible = False
            compatibility_error = str(error)

        fit_geometry = None
        if has_localization:
            try:
                localization = orient_localization(
                    inspection.measurement.localization,
                    self.feedback_orientation(section),
                )
                fit_geometry = editable_position_reference_geometry(localization)
            except Exception:
                fit_geometry = None

        change_reason = self._position_reference_change_reason(section)
        save_reason = self._position_reference_save_reason(section)
        return PositionReferenceContext(
            selection=selection,
            plane_name=plane,
            saved_names=tuple(saved_names),
            compatible=compatible,
            compatibility_error=compatibility_error,
            change_allowed=not bool(change_reason),
            change_unavailable_reason=change_reason,
            save_allowed=not bool(save_reason),
            save_unavailable_reason=save_reason,
            fit_geometry=fit_geometry,
        )

    def set_position_reference(
        self,
        section_key: str,
        *,
        mode: PositionReferenceMode | str,
        saved_name: str | None=None,
        editable_geometry: EditablePositionReferenceGeometry | Mapping[str,Any] | None=None,
        editable_locks: Mapping[str,Any] | Sequence[str] | None=None,
    ) -> PositionReferenceSelection:
        self._require_editor_mode()
        section = str(section_key)
        reason = self._position_reference_change_reason(section)
        if reason:
            raise RuntimeError(reason)
        normalized_mode = PositionReferenceMode.normalize(mode)
        previous = self.position_reference_selection(section)
        if editable_geometry is None and previous.editable_geometry is not None:
            editable_geometry = previous.editable_geometry
        if editable_locks is None:
            editable_locks = previous.editable_locks
        if normalized_mode is PositionReferenceMode.EDITABLE and editable_geometry is None:
            inspection = self.session.runtime.get_section_feedback_inspection(section)
            measurement = inspection.measurement
            if measurement is None or measurement.localization is None:
                raise RuntimeError(
                    "Accept a localization before editing its reference lattice"
                )
            localization = orient_localization(
                measurement.localization,self.feedback_orientation(section),
            )
            editable_geometry = editable_position_reference_geometry(localization)
        selection = PositionReferenceSelection(
            mode=normalized_mode,
            saved_name=saved_name,
            editable_geometry=editable_geometry,
            editable_locks=_normalize_editable_locks(editable_locks),
        )
        plane = self._active_feedback_plane(section)
        if selection.mode is PositionReferenceMode.SAVED:
            if plane is None:
                raise RuntimeError(
                    "Select a measurement plane before using a saved position reference."
                )
            if self._position_reference_store is None:
                raise RuntimeError("Position-reference storage is unavailable")
            self._position_reference_store.load(plane,selection.saved_name)
        self._position_reference_selections[(section,plane)] = selection
        self._section_changed(section)
        return selection

    def fit_position_reference_center(
        self,section_key: str,
    ) -> PositionReferenceSelection:
        """Replace the editable reference with a strong center-prioritized fit."""
        self._require_editor_mode()
        section = str(section_key)
        reason = self._position_reference_change_reason(section)
        if reason:
            raise RuntimeError(reason)
        inspection = self.session.runtime.get_section_feedback_inspection(section)
        measurement = inspection.measurement
        if measurement is None or measurement.localization is None:
            raise RuntimeError(
                "Accept a localization before fitting its reference lattice"
            )
        localization = orient_localization(
            measurement.localization,self.feedback_orientation(section),
        )
        previous = self.position_reference_selection(section)
        geometry = previous.editable_geometry
        if geometry is None:
            geometry = editable_position_reference_geometry(localization)
        locks = previous.editable_locks
        selection = PositionReferenceSelection(
            mode=PositionReferenceMode.EDITABLE,
            editable_geometry=fit_center_reference_geometry(
                localization,geometry=geometry,
                locks={name:(name in locks) for name in _EDITABLE_POSITION_REFERENCE_LOCK_FIELDS},
            ),
            editable_locks=locks,
        )
        plane = self._active_feedback_plane(section)
        self._position_reference_selections[(section,plane)] = selection
        self._section_changed(section)
        return selection

    def save_position_reference(
        self,section_key: str,name: str,*,overwrite: bool=False,
    ) -> PositionReference:
        self._require_editor_mode()
        section = str(section_key)
        reason = self._position_reference_save_reason(section)
        if reason:
            raise RuntimeError(reason)
        store = self._position_reference_store
        if store is None:
            raise RuntimeError("Position-reference storage is unavailable")
        plane = self._active_feedback_plane(section)
        if plane is None:  # guarded above; retained for static clarity.
            raise RuntimeError("Select a measurement plane first")
        measurement = self.session.runtime.get_section_feedback_inspection(
            section
        ).measurement
        if measurement is None or measurement.localization is None:
            raise RuntimeError("Accept a localization before saving a reference")
        localization = orient_localization(
            measurement.localization,self.feedback_orientation(section),
        )
        config_path = self.session.current_config_path
        reference = PositionReference(
            name=name,
            plane_name=plane,
            lattice_indices=localization.lattice_indices,
            positions_px=localization_positions_full_px(localization),
            image_shape=measurement.acquisition.image.shape,
            detector_name=measurement.acquisition.detector,
            metadata={
                "source_slm_key":self.session.runtime.identity.key,
                "source_slm_serial":self.session.runtime.identity.serial_number,
                "source_section":section,
                "source_target_type":localization.target_type,
                "source_config_name":(
                    None if not config_path else Path(config_path).name
                ),
                "feedback_orientation":self.feedback_orientation(section).value,
            },
        )
        store.save(reference,overwrite=bool(overwrite))
        previous = self.position_reference_selection(section)
        self._position_reference_selections[(section,plane)] = (
            PositionReferenceSelection(
                mode=PositionReferenceMode.SAVED,
                saved_name=reference.name,
                editable_geometry=previous.editable_geometry,
                editable_locks=previous.editable_locks,
            )
        )
        self._section_changed(section)
        return reference

    def delete_position_reference(self,section_key: str,name: str) -> None:
        self._require_editor_mode()
        plane = self._active_feedback_plane(section_key)
        if plane is None:
            raise RuntimeError("Select a measurement plane first")
        if self._position_reference_store is None:
            raise RuntimeError("Position-reference storage is unavailable")
        self._position_reference_store.delete(plane,str(name))

    def position_reference_preview(self,section_key: str):
        """Return detector-space reference preview plus legacy kxy diagnostics."""
        try:
            status = self.session.runtime.get_section_feedback_status(section_key)
            if not status.localization_available:
                return None
            inspection = self.session.runtime.get_section_feedback_inspection(section_key)
            measurement = inspection.measurement
            if measurement is None or measurement.localization is None:
                return None
            localization = orient_localization(
                measurement.localization,self.feedback_orientation(section_key),
            )
            reference_positions,metadata = self._resolve_position_reference(section_key)
            reference = (
                np.asarray(localization.expected_positions_px,dtype=np.float64)
                if reference_positions is None
                else np.asarray(reference_positions,dtype=np.float64)
            )
            analysis = self.session.runtime.compute_section_feedback_position_analysis(
                section_key,
                orientation=self.feedback_orientation(section_key),
                reference_positions_px=reference_positions,
            )
            selection = self.position_reference_selection(section_key)
            fit_geometry = editable_position_reference_geometry(localization)
            editable_geometry = selection.editable_geometry
            center = (
                editable_reference_center_px(localization,editable_geometry)
                if selection.mode is PositionReferenceMode.EDITABLE
                and editable_geometry is not None
                else np.mean(reference,axis=1)
            )
            return {
                "cropped_image":np.asarray(localization.cropped_image,dtype=np.float64),
                "measured_positions_px":np.asarray(localization.measured_positions_px,dtype=np.float64),
                "global_positions_px":np.asarray(localization.expected_positions_px,dtype=np.float64),
                "reference_positions_px":reference,
                "reference_center_px":np.asarray(center,dtype=np.float64),
                "fit_center_px":editable_reference_center_px(
                    localization,fit_geometry,
                ),
                "fit_geometry":fit_geometry.to_dict(),
                "editable_geometry":(
                    None if editable_geometry is None else editable_geometry.to_dict()
                ),
                "ideal_positions_kxy":np.asarray(
                    self.session.runtime.get_section_fov_position_context(
                        section_key
                    )["ideal_positions_kxy"]
                ),
                "baseline_positions_kxy":(
                    np.asarray(analysis.corrected_positions_kxy)
                    - np.asarray(analysis.correction_kxy)
                ),
                "displacement_kxy":np.asarray(analysis.correction_kxy),
                "reference":metadata,
            }
        except Exception:
            return None

    def _resolve_position_reference(
        self,section_key: str,
    ) -> tuple[np.ndarray | None,dict[str,Any]]:
        section = str(section_key)
        inspection = self.session.runtime.get_section_feedback_inspection(section)
        measurement = inspection.measurement
        if measurement is None or measurement.localization is None:
            raise RuntimeError("Accept a localization before using position feedback")
        localization = orient_localization(
            measurement.localization,self.feedback_orientation(section),
        )
        selection = self.position_reference_selection(section)
        if selection.mode is PositionReferenceMode.GLOBAL_FIT:
            return None,{"mode":selection.mode.value,"label":"Global fit"}
        if selection.mode is PositionReferenceMode.EDITABLE:
            geometry = selection.editable_geometry
            if geometry is None:
                geometry = editable_position_reference_geometry(localization)
            return editable_reference_positions(localization,geometry),{
                "mode":selection.mode.value,
                "label":"Editable lattice",
                "geometry":geometry.to_dict(),
            }

        plane = self._active_feedback_plane(section)
        if plane is None:
            raise RuntimeError(
                "Saved position references require an active measurement plane"
            )
        if self._position_reference_store is None:
            raise RuntimeError("Position-reference storage is unavailable")
        reference = self._position_reference_store.load(
            plane,selection.saved_name,
        )
        if tuple(reference.image_shape) != tuple(measurement.acquisition.image.shape):
            raise RuntimeError(
                "Saved reference image shape %s does not match current measurement %s"
                % (reference.image_shape,measurement.acquisition.image.shape)
            )
        current_detector = str(measurement.acquisition.detector or "").strip() or None
        if (
            reference.detector_name is not None
            and current_detector is not None
            and reference.detector_name != current_detector
        ):
            raise RuntimeError(
                'Saved reference detector "%s" does not match current detector "%s"'
                % (reference.detector_name,current_detector)
            )
        positions = reference_positions_for_localization(reference,localization)
        return positions,{
            "mode":selection.mode.value,
            "label":"Saved: %s" % reference.name,
            "name":reference.name,
            "plane_name":reference.plane_name,
            "created_at":reference.created_at,
        }

    def _position_reference_save_reason(self,section_key: str) -> str:
        status = self.session.runtime.get_section_feedback_status(section_key)
        if FeedbackCapability.POSITION_CORRECTION not in set(status.capabilities):
            return "Position references are unavailable for this target."
        change_reason = self._position_reference_change_reason(section_key)
        if change_reason:
            return change_reason
        if self._position_reference_store is None:
            return "Position-reference storage is unavailable."
        if self._active_feedback_plane(section_key) is None:
            return "Select a measurement plane before saving a position reference."
        inspection = self.session.runtime.get_section_feedback_inspection(section_key)
        measurement = inspection.measurement
        if measurement is None or measurement.localization is None:
            return "Accept a localization before saving a position reference."
        if not _localization_is_complete(measurement.localization):
            return "Only a complete, genuinely localized target can be saved as a reference."
        return ""

    def _on_position_reference_store_changed(self) -> None:
        runtime = self.session.runtime
        for section in tuple(runtime.section_keys):
            key = self._position_reference_key(section)
            selection = self._position_reference_selections.get(key)
            if selection is not None and selection.mode is PositionReferenceMode.SAVED:
                plane = key[1]
                if (
                    plane is None
                    or self._position_reference_store is None
                    or not self._position_reference_store.exists(
                        plane,selection.saved_name
                    )
                ):
                    self._position_reference_selections[key] = PositionReferenceSelection()
            self._section_changed(section)

    def _fov_position_key(self,section_key: str) -> tuple[str,str | None]:
        section = str(section_key)
        return section,self._active_feedback_plane(section)

    def _saved_fov_position_calibration_name(
        self,section_key: str,plane_name: str | None,
    ) -> str | None:
        preferences = self.session.startup_preferences
        if preferences is None:
            return None
        return preferences.default_fov_position_calibration(
            section_key,plane_name,
        )

    def _load_fov_position_calibration(
        self,section_key: str,plane_name: str,name: str,
    ) -> FOVPositionCalibration:
        store = self._fov_position_calibration_store
        if store is None:
            raise RuntimeError("FOV position-calibration storage is unavailable")
        return store.load(
            self.session.runtime.identity,section_key,plane_name,name,
        )

    def fov_position_calibration_context(self,section_key: str) -> dict[str,Any]:
        section = str(section_key)
        plane = self._active_feedback_plane(section)
        key = (section,plane)
        store = self._fov_position_calibration_store
        names = (
            () if plane is None or store is None else
            store.list(self.session.runtime.identity,section,plane)
        )
        selection = self._fov_position_calibration_selections.get(key)
        if selection is None:
            default_name = self._saved_fov_position_calibration_name(section,plane)
            selected = default_name if default_name in names else None
            selection = {"selected_name":selected,"applied":bool(selected)}
            self._fov_position_calibration_selections[key] = selection
        selected = str(selection.get("selected_name") or "").strip() or None
        if selected not in names:
            selected = None
            selection["selected_name"] = None
            selection["applied"] = False
        applied = bool(selection.get("applied",False) and selected)
        runtime_context = self.session.runtime.get_section_fov_position_context(section)
        outside = np.asarray(runtime_context.get("outside_mask",()),dtype=bool)
        extrapolated_count = int(np.count_nonzero(outside)) if applied else 0
        calibration = (
            None if selected is None or plane is None or store is None
            else self._load_fov_position_calibration(section,plane,selected)
        )
        candidate = self._fov_position_calibration_candidates.get(key)
        return {
            "plane_name":plane,
            "available_names":tuple(names),
            "selected_name":selected,
            "applied":applied,
            "default_name":self._saved_fov_position_calibration_name(section,plane),
            "supported":bool(runtime_context.get("supported",False)),
            "calibration":calibration,
            "candidate":candidate,
            "extrapolated_count":extrapolated_count,
            "target_count":int(np.asarray(runtime_context["ideal_positions_kxy"]).shape[1]),
            "max_extrapolation_kxy":float(runtime_context.get("max_extrapolation_kxy",0.0)),
            "coverage_tolerance_kxy":float(runtime_context.get("coverage_tolerance_kxy",0.0)),
        }

    def fov_position_calibration_preview(self,section_key: str) -> dict[str,Any] | None:
        context = self.fov_position_calibration_context(section_key)
        key = self._fov_position_key(section_key)
        candidate = self._fov_position_calibration_candidates.get(key)
        calibration = candidate if candidate is not None else context.get("calibration")
        if calibration is None:
            return None
        current = self.session.runtime.get_section_fov_position_context(section_key)
        ideal = np.asarray(current["ideal_positions_kxy"],dtype=np.float64)
        # Inspect the field at the current target when it is meaningful.  With
        # no target (or an incompatible target), fall back to the calibration's
        # own sample positions so the read-only FOV view remains usable.
        if bool(current.get("supported",False)) and ideal.shape[1] > 0:
            preview_positions = ideal
        else:
            preview_positions = np.asarray(
                calibration.sample_positions_kxy,dtype=np.float64,
            )
        predicted = calibration.evaluate(preview_positions)
        return {
            "source":"candidate" if candidate is not None else "applied",
            "name":None if candidate is not None else calibration.name,
            "model":calibration.model,
            "degree":calibration.degree,
            "sample_count":calibration.sample_count,
            "rms_residual_kxy":calibration.rms_residual_kxy,
            "max_residual_kxy":calibration.max_residual_kxy,
            "sample_positions_kxy":np.asarray(calibration.sample_positions_kxy),
            "sample_displacements_kxy":np.asarray(calibration.sample_displacements_kxy),
            "current_positions_kxy":preview_positions,
            "current_displacements_kxy":predicted,
        }

    def select_fov_position_calibration(
        self,section_key: str,name: str | None,
    ) -> None:
        self._require_editor_mode()
        section = str(section_key)
        plane = self._active_feedback_plane(section)
        if plane is None:
            raise RuntimeError("Select a measurement plane before choosing an FOV calibration")
        key = (section,plane)
        selected = str(name or "").strip() or None
        selection = self._fov_position_calibration_selections.setdefault(
            key,{"selected_name":None,"applied":False},
        )
        calibration = None
        if selected is not None:
            calibration = self._load_fov_position_calibration(section,plane,selected)
        selection["selected_name"] = selected
        if bool(selection.get("applied",False)):
            if calibration is None:
                selection["applied"] = False
            self._set_runtime_fov_position_calibration(section,calibration)
            self._warn_fov_extrapolation(section)
        self._section_changed(section)

    def set_fov_position_calibration_applied(
        self,section_key: str,applied: bool,
    ) -> None:
        self._require_editor_mode()
        section = str(section_key)
        plane = self._active_feedback_plane(section)
        if plane is None:
            raise RuntimeError("Select a measurement plane before applying an FOV calibration")
        key = (section,plane)
        selection = self._fov_position_calibration_selections.setdefault(
            key,{"selected_name":None,"applied":False},
        )
        name = str(selection.get("selected_name") or "").strip() or None
        applied = bool(applied)
        if applied and name is None:
            raise RuntimeError("Choose an FOV calibration before enabling Apply")
        calibration = (
            self._load_fov_position_calibration(section,plane,name)
            if applied else None
        )
        selection["applied"] = applied
        self._set_runtime_fov_position_calibration(section,calibration)
        self._section_changed(section)
        self._warn_fov_extrapolation(section)

    def set_default_fov_position_calibration(self,section_key: str) -> None:
        self._require_editor_mode()
        section = str(section_key)
        plane = self._active_feedback_plane(section)
        if plane is None:
            raise RuntimeError("Select a measurement plane before setting an FOV default")
        context = self.fov_position_calibration_context(section)
        name = context.get("selected_name")
        if not name:
            raise RuntimeError("Choose an FOV calibration before setting the default")
        preferences = self.session.startup_preferences
        if preferences is None:
            raise RuntimeError("Startup preferences are unavailable")
        preferences.set_default_fov_position_calibration(section,plane,name)
        self.set_fov_position_calibration_applied(section,True)

    def delete_fov_position_calibration(self,section_key: str) -> str:
        self._require_editor_mode()
        section = str(section_key)
        context = self.fov_position_calibration_context(section)
        plane = context.get("plane_name")
        name = context.get("selected_name")
        if not plane or not name:
            raise RuntimeError("Choose an FOV calibration before deleting it")
        store = self._fov_position_calibration_store
        if store is None:
            raise RuntimeError("FOV position-calibration storage is unavailable")

        # Delete first so a filesystem failure leaves both selection and
        # startup default untouched.  The synchronous store listener clears
        # the selected/applied runtime calibration.
        store.delete(self.session.runtime.identity,section,plane,name)
        preferences = self.session.startup_preferences
        if (
            preferences is not None
            and preferences.default_fov_position_calibration(section,plane) == name
        ):
            preferences.set_default_fov_position_calibration(section,plane,None)
        self._fov_position_calibration_candidates.pop((section,plane),None)
        self._section_changed(section)
        return str(name)

    def fit_fov_position_calibration(
        self,section_key: str,*,model: str="polynomial",degree: int=2,
    ) -> FOVPositionCalibration:
        self._require_editor_mode()
        section = str(section_key)
        plane = self._active_feedback_plane(section)
        if plane is None:
            raise RuntimeError("Select a measurement plane before fitting an FOV calibration")
        status = self.session.runtime.get_section_feedback_status(section)
        if FeedbackCapability.POSITION_CORRECTION not in set(status.capabilities):
            raise RuntimeError("The current target does not support position correction")

        # Prefer the accepted transient correction.  It remains authoritative
        # after computing an adapted hologram, which lets users verify the
        # correction experimentally and only then promote it to a persistent
        # FOV calibration.  Fall back to a fresh analysis only before a
        # correction has been accepted.
        session_inspection = self.session.runtime.get_section_cgh_session_inspection(
            section
        )
        correction = session_inspection.position_correction
        if correction is not None:
            ideal = np.asarray(
                correction.ideal_positions_kxy,dtype=np.float64,
            )
            total_displacement = (
                np.asarray(correction.corrected_positions_kxy,dtype=np.float64)
                - ideal
            )
            reference_metadata = dict(correction.reference or {})
            detector = correction.measurement.acquisition.detector
            source = "accepted_position_correction"
        else:
            reference_positions,reference_metadata = self._resolve_position_reference(
                section
            )
            analysis = self.session.runtime.compute_section_feedback_position_analysis(
                section,
                orientation=self.feedback_orientation(section),
                reference_positions_px=reference_positions,
            )
            current = self.session.runtime.get_section_fov_position_context(section)
            ideal = np.asarray(current["ideal_positions_kxy"],dtype=np.float64)
            total_displacement = (
                np.asarray(analysis.corrected_positions_kxy,dtype=np.float64)-ideal
            )
            inspection = self.session.runtime.get_section_feedback_inspection(section)
            detector = None
            if inspection.measurement is not None:
                detector = inspection.measurement.acquisition.detector
            source = "current_position_analysis"

        calibration = FOVPositionCalibration.fit(
            name="Current candidate",
            slm_serial=self.session.runtime.identity.serial_number,
            section_key=section,plane_name=plane,
            ideal_positions_kxy=ideal,
            total_displacements_kxy=total_displacement,
            model=model,degree=degree,detector_name=detector,
            provenance={
                "reference":dict(reference_metadata or {}),
                "source":source,
                "source_applied_calibration":self.fov_position_calibration_context(
                    section
                ).get("selected_name"),
            },
        )
        self._fov_position_calibration_candidates[(section,plane)] = calibration
        self._section_changed(section)
        return calibration

    def save_fov_position_calibration(
        self,section_key: str,name: str,*,overwrite: bool=False,
    ) -> FOVPositionCalibration:
        self._require_editor_mode()
        section = str(section_key)
        plane = self._active_feedback_plane(section)
        if plane is None:
            raise RuntimeError("Select a measurement plane before saving an FOV calibration")
        candidate = self._fov_position_calibration_candidates.get((section,plane))
        if candidate is None:
            raise RuntimeError("Build an FOV calibration fit before saving")
        saved = candidate.with_name(str(name))
        store = self._fov_position_calibration_store
        if store is None:
            raise RuntimeError("FOV position-calibration storage is unavailable")
        store.save(saved,overwrite=overwrite)
        self._section_changed(section)
        return saved

    def fov_position_calibration_replacement_warning(
        self,section_key: str,
    ) -> str:
        section = str(section_key)
        context = self.fov_position_calibration_context(section)
        plane = context.get("plane_name")
        name = context.get("selected_name")
        if not plane or not name or not context.get("applied"):
            return ""
        candidate = self._fov_position_calibration_candidates.get((section,plane))
        if candidate is None:
            return ""
        old = self._load_fov_position_calibration(section,plane,name)
        old_area = float(old.coverage_area)
        new_area = float(candidate.coverage_area)
        if old_area <= 0 or new_area <= 0:
            return (
                "Candidate and applied FOV calibration coverage could not be "
                "compared reliably because one convex hull is degenerate."
            ) if old_area != new_area else ""

        old_outside_new,_,_ = candidate.coverage_extrapolation(
            old.sample_positions_kxy
        )
        new_outside_old,_,_ = old.coverage_extrapolation(
            candidate.sample_positions_kxy
        )
        lost = int(np.count_nonzero(old_outside_new))
        added = int(np.count_nonzero(new_outside_old))
        area_ratio = new_area/old_area
        area_changed = area_ratio < 0.98 or area_ratio > 1.02
        if lost or added or area_changed:
            details = [
                "candidate area %.1f%% of the previous convex-hull area"
                % (100.0*area_ratio)
            ]
            if lost:
                details.append(
                    "%d/%d previous samples lie outside the candidate coverage"
                    % (lost,old.sample_count)
                )
            if added:
                details.append(
                    "%d/%d candidate samples lie outside the previous coverage"
                    % (added,candidate.sample_count)
                )
            return "Candidate measured coverage differs from the applied calibration (" + "; ".join(details) + ")."
        return ""

    def replace_applied_fov_position_calibration(self,section_key: str) -> str:
        self._require_editor_mode()
        section = str(section_key)
        context = self.fov_position_calibration_context(section)
        plane = context.get("plane_name")
        name = context.get("selected_name")
        if not plane or not name or not context.get("applied"):
            raise RuntimeError("An applied FOV calibration is required for replacement")
        candidate = self._fov_position_calibration_candidates.get((section,plane))
        if candidate is None:
            raise RuntimeError("Build a candidate FOV fit before replacing the applied calibration")
        saved = candidate.with_name(name)
        self._fov_position_calibration_store.save(saved,overwrite=True)
        self._fov_position_calibration_candidates.pop((section,plane),None)
        self._set_runtime_fov_position_calibration(section,saved)
        self._section_changed(section)
        self._warn_fov_extrapolation(section)
        return name

    def _set_runtime_fov_position_calibration(
        self,section_key: str,calibration: FOVPositionCalibration | None,
        *,notify: bool=True,
    ) -> None:
        # Context refreshes happen for all sections on plane/catalog changes.
        # Do not cancel a CGH request or create redundant callbacks when this
        # section already holds the exact same persistent field.
        current = None
        current_known = False
        try:
            current = self.session.runtime.get_section_fov_position_context(
                section_key
            ).get("calibration")
            current_known = True
        except Exception:
            # If the current target is temporarily invalid, fall back to the
            # conservative path below; the runtime setter remains authoritative.
            pass
        same = (
            current is calibration
            or (
                current_known
                and current is not None
                and calibration is not None
                and current.to_dict() == calibration.to_dict()
            )
            or (current_known and current is None and calibration is None)
        )
        if same:
            if notify:
                self._section_changed(section_key)
            return

        self.session.cancel_cgh(section_key)
        transition = self.session.runtime.set_section_fov_position_calibration(
            section_key,calibration,
        )
        if notify:
            if transition is not None:
                self._callbacks.on_transition_committed(section_key,transition)
            else:
                self._section_changed(section_key)

    def _warn_fov_extrapolation(self,section_key: str) -> None:
        context = self.fov_position_calibration_context(section_key)
        count = int(context.get("extrapolated_count",0) or 0)
        if count:
            self._warning(
                "FOV calibration extrapolation",
                "%d of %d current target positions lie outside the measured FOV "
                "coverage (beyond numerical tolerance). The polynomial calibration "
                "will be extrapolated at those positions."
                % (count,int(context.get("target_count",0) or 0)),
            )

    def apply_startup_fov_position_calibration_defaults(self) -> None:
        self.refresh_fov_position_calibration_contexts()

    def refresh_fov_position_calibration_contexts(self) -> None:
        runtime = self.session.runtime
        active_keys = set()
        for section_key in tuple(runtime.section_keys):
            section = str(section_key)
            plane = self._active_feedback_plane(section)
            key = (section,plane)
            active_keys.add(key)
            selection = self._fov_position_calibration_selections.get(key)
            if selection is None:
                name = self._saved_fov_position_calibration_name(section,plane)
                valid_default = bool(
                    name and plane and self._fov_position_calibration_store is not None
                    and self._fov_position_calibration_store.exists(
                        runtime.identity,section,plane,name
                    )
                )
                selection = {
                    "selected_name":name if valid_default else None,
                    "applied":valid_default,
                }
                self._fov_position_calibration_selections[key] = selection
            name = str(selection.get("selected_name") or "").strip() or None
            applied = bool(selection.get("applied",False) and name and plane)
            calibration = (
                self._load_fov_position_calibration(section,plane,name)
                if applied else None
            )
            self._set_runtime_fov_position_calibration(
                section,calibration,notify=True,
            )
        for key in tuple(self._fov_position_calibration_candidates):
            if key not in active_keys and key[0] not in runtime.section_keys:
                self._fov_position_calibration_candidates.pop(key,None)

    def _on_fov_position_calibration_store_changed(self) -> None:
        runtime = self.session.runtime
        for section_key in tuple(runtime.section_keys):
            section = str(section_key)
            plane = self._active_feedback_plane(section)
            if plane is None:
                continue
            key = (section,plane)
            selection = self._fov_position_calibration_selections.get(key)
            if selection is None:
                continue
            name = str(selection.get("selected_name") or "").strip() or None
            exists = bool(
                name and self._fov_position_calibration_store.exists(
                    runtime.identity,section,plane,name
                )
            )
            if not exists:
                selection["selected_name"] = None
                selection["applied"] = False
                self._set_runtime_fov_position_calibration(section,None,notify=True)
            elif selection.get("applied"):
                calibration = self._load_fov_position_calibration(section,plane,name)
                self._set_runtime_fov_position_calibration(section,calibration,notify=True)
            else:
                self._section_changed(section)

    def set_feedback_orientation(
        self,section_key: str,orientation: FeedbackOrientation | str,
    ) -> FeedbackOrientation:
        self._require_editor_mode()
        section = str(section_key)
        reason = self._feedback_orientation_change_reason(section)
        if reason:
            raise RuntimeError(reason)
        normalized = FeedbackOrientation.normalize(orientation)
        plane = self._active_feedback_plane(section)
        self._feedback_orientations[section] = (plane,normalized)
        if self.session.runtime.get_section_feedback_status(
            section
        ).localization_available:
            self._update_committed_analysis(section)
        self._section_changed(section)
        return normalized

    def save_feedback_orientation(self,section_key: str) -> FeedbackOrientation:
        """Persist the current transient orientation for the active plane/default."""
        self._require_editor_mode()
        section = str(section_key)
        orientation = self.feedback_orientation(section)
        plane = self._active_feedback_plane(section)
        preferences = self.session.startup_preferences
        if preferences is None:
            raise RuntimeError("Startup preferences are unavailable")
        if plane is None:
            preferences.set_feedback_orientation_default(section,orientation.value)
        else:
            preferences.set_feedback_orientation_for_plane(
                section,plane,orientation.value,
            )
        self._section_changed(section)
        return orientation

    def refresh_feedback_orientation_contexts(self) -> None:
        """Discard unsaved orientation only for sections whose active plane changed."""
        for section_key in tuple(self.session.runtime.section_keys):
            section = str(section_key)
            current = self._feedback_orientations.get(section)
            if current is None:
                continue
            plane = self._active_feedback_plane(section)
            if current[0] == plane:
                continue
            orientation = self._saved_feedback_orientation(section,plane)
            self._feedback_orientations[section] = (plane,orientation)
            if self.session.runtime.get_section_feedback_status(
                section
            ).localization_available:
                self._update_committed_analysis(section)
            self._section_changed(section)

    def request_measurement(
        self,
        section_key: str,
        source: str,
        *,
        metadata: Mapping[str,Any] | None,
        on_result: Callable[[ImageMeasurement],None],
        on_error: Callable[[Exception],None],
    ) -> None:
        self._require_editor_mode()
        self.cancel_measurement(section_key)
        self._callbacks.on_measurement_busy_changed(
            section_key,True,"Waiting for %s..." % source,
        )
        measurements = self.measurements
        if measurements is None:
            error = RuntimeError("No host measurement provider is configured.")
            self._callbacks.on_measurement_busy_changed(section_key,False,"")
            self._callbacks.on_measurement_error(section_key,error)
            on_error(error)
            return

        def result_callback(measurement):
            self._measurement_requests.pop(section_key,None)
            self._callbacks.on_measurement_busy_changed(section_key,False,"")
            if not self.session.editor_writes_allowed:
                return
            on_result(measurement)

        def error_callback(error):
            self._measurement_requests.pop(section_key,None)
            self._callbacks.on_measurement_busy_changed(section_key,False,"")
            if not isinstance(error,Exception):
                error = RuntimeError(str(error))
            self._callbacks.on_measurement_error(section_key,error)
            on_error(error)

        try:
            request = measurements.acquire(
                section_key,
                source,
                metadata=metadata,
                on_result=result_callback,
                on_error=error_callback,
            )
        except Exception as error:
            self._callbacks.on_measurement_busy_changed(section_key,False,"")
            self._callbacks.on_measurement_error(section_key,error)
            on_error(error)
            return
        if request.active:
            self._measurement_requests[section_key] = request

    def cancel_measurement(self,section_key: str) -> None:
        request = self._measurement_requests.pop(section_key,None)
        if request is not None:
            request.cancel()
        self._callbacks.on_measurement_busy_changed(section_key,False,"")

    def acquire(
        self,
        section_key: str,
        source: str,
        *,
        reuse_previous_localization: bool=False,
    ) -> None:
        self._require_editor_mode()

        def commit_result(measurement):
            try:
                self.commit_measurement(
                    section_key,measurement,
                    reuse_previous_localization=reuse_previous_localization,
                )
            except Exception as error:
                self._callbacks.on_measurement_error(section_key,error)

        self.request_measurement(
            section_key,
            source,
            metadata=self.feedback_measurement_metadata(section_key),
            on_result=commit_result,
            on_error=lambda _error:None,
        )

    def commit_measurement(
        self,
        section_key: str,
        measurement: ImageMeasurement,
        *,
        reuse_previous_localization: bool=False,
    ) -> bool:
        self._require_editor_mode()
        runtime = self.session.runtime
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
        self._section_changed(section_key)
        return previous_available

    def localization_candidate(
        self,section_key: str,parameters: Mapping[str,Any],
    ):
        self.validate_target_hint_use(section_key,parameters)
        runtime = self.session.runtime
        candidate = runtime.compute_section_feedback_localization_candidate(
            section_key,parameters,
        )
        metrics = None
        try:
            metrics = runtime.compute_section_feedback_intensity_analysis(
                section_key,candidate,orientation=self.feedback_orientation(section_key),
            )
        except Exception as error:
            self._warning(
                "Measurement metrics",
                "Measurement metrics are unavailable: %s" % error,
            )
        return candidate,metrics

    def infer_missing_localization_candidate(
        self,section_key: str,localization: Any,
    ):
        """Return a candidate with unresolved lattice sites explicitly inferred."""
        self._require_editor_mode()
        candidate = infer_missing_localization(localization)
        metrics = None
        try:
            metrics = self.session.runtime.compute_section_feedback_intensity_analysis(
                section_key,candidate,orientation=self.feedback_orientation(section_key),
            )
        except Exception as error:
            self._warning(
                "Measurement metrics",
                "Measurement metrics are unavailable: %s" % error,
            )
        return candidate,metrics

    def accept_localization(
        self,
        section_key: str,
        localization: Any,
        parameters: Mapping[str,Any],
        *,
        raise_errors: bool=False,
    ) -> bool:
        try:
            self._require_editor_mode()
            self.require_current_cgh_for_localization_commit(section_key)
            self.validate_target_hint_use(section_key,parameters)
            runtime = self.session.runtime
            runtime.commit_section_feedback_localization(
                section_key,localization,parameters,
            )
            key = self._position_reference_key(section_key)
            selection = self._position_reference_selections.get(key)
            if selection is not None and selection.mode is PositionReferenceMode.EDITABLE:
                oriented = orient_localization(
                    localization,self.feedback_orientation(section_key),
                )
                self._position_reference_selections[key] = PositionReferenceSelection(
                    mode=PositionReferenceMode.EDITABLE,
                    editable_geometry=editable_position_reference_geometry(oriented),
                )
            self._update_committed_analysis(section_key,localization)
            self._section_changed(section_key)
            return True
        except Exception as error:
            if raise_errors:
                raise
            self._callbacks.on_localization_error(section_key,error)
            return False

    def reuse_localization(
        self,section_key: str,*,raise_errors: bool=False,
    ):
        try:
            self._require_editor_mode()
            self.require_current_cgh_for_localization_commit(section_key)
            localization = self.session.runtime.reuse_section_feedback_localization(
                section_key
            )
            self._update_committed_analysis(section_key,localization)
            self._section_changed(section_key)
            return localization
        except Exception as error:
            if raise_errors:
                raise
            self._callbacks.on_localization_error(section_key,error)
            return None

    def localize_and_commit(self,section_key: str) -> None:
        status = self.session.runtime.get_section_feedback_status(section_key)
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
    ) -> FeedbackParameterUpdateResult:
        self._require_editor_mode()
        runtime = self.session.runtime
        changed = runtime.update_section_feedback_parameters(
            section_key,group,dict(changes or {}),
        )
        candidate_analysis = None
        if changed and str(group) in ("intensity","intensity_analysis"):
            if localization is not None:
                try:
                    candidate_analysis = runtime.compute_section_feedback_intensity_analysis(
                        section_key,localization,
                        orientation=self.feedback_orientation(section_key),
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
        self._section_changed(section_key)
        return FeedbackParameterUpdateResult(
            changed=bool(changed),candidate_analysis=candidate_analysis,
        )

    def apply_intensity_feedback(
        self,section_key: str,*,raise_errors: bool=False,
    ):
        return self._apply_resolution_operation(
            section_key,
            lambda runtime:runtime.apply_section_intensity_feedback(section_key),
            "Applying intensity feedback failed",
            raise_errors=raise_errors,
        )

    def reset_intensity_feedback(self,section_key: str):
        return self._apply_resolution_operation(
            section_key,
            lambda runtime:runtime.reset_section_intensity_feedback(section_key),
            "Resetting intensity feedback failed",
        )

    def apply_position_correction(
        self,section_key: str,*,reset_intensity: bool=False,
    ):
        reference_positions,reference_metadata = self._resolve_position_reference(
            section_key
        )
        return self._apply_resolution_operation(
            section_key,
            lambda runtime:runtime.apply_section_position_correction(
                section_key,reset_intensity=bool(reset_intensity),
                orientation=self.feedback_orientation(section_key),
                reference_positions_px=reference_positions,
                reference_metadata=reference_metadata,
            ),
            "Applying position correction failed",
        )

    def set_position_active(
        self,section_key: str,active: bool,*,reset_intensity: bool=False,
    ):
        return self._apply_resolution_operation(
            section_key,
            lambda runtime:runtime.set_section_position_correction_active(
                section_key,bool(active),reset_intensity=bool(reset_intensity),
            ),
            "Changing position correction failed",
        )

    def clear_position_correction(
        self,section_key: str,*,reset_intensity: bool=False,
    ):
        return self._apply_resolution_operation(
            section_key,
            lambda runtime:runtime.clear_section_position_correction(
                section_key,reset_intensity=bool(reset_intensity),
            ),
            "Clearing position correction failed",
        )

    def reset_to_round(self,section_key: str,round_index: int):
        self._require_editor_mode()
        runtime = self.session.runtime
        inspection = runtime.get_section_cgh_session_inspection(section_key)
        rounds = {item.index:item for item in inspection.rounds}
        if int(round_index) not in rounds:
            raise ValueError("Only computed rounds can be restored.")
        self.session.cancel_cgh(section_key)
        transition = runtime.reset_section_cgh_to_round(section_key,int(round_index))
        if transition is not None:
            self._callbacks.on_transition_committed(section_key,transition)
        else:
            self._section_changed(section_key)
        return transition

    def propagate_round(
        self,
        section_key: str,
        round_index: int,
        *,
        position_context: str="corrected",
        pad_size: Any=1024,
    ):
        pad_size = int(pad_size)
        if pad_size <= 0:
            raise ValueError("CGH propagation pad size must be > 0")
        inspection = self.session.runtime.get_section_cgh_session_inspection(section_key)
        if str(position_context) == "not_corrected":
            selected = inspection.position_reference_round
        elif str(position_context) == "corrected":
            selected = next(
                (item for item in inspection.rounds if item.index == int(round_index)),
                None,
            )
        else:
            raise ValueError(
                "Unknown position history context: %r" % position_context
            )
        if selected is None or selected.result is None:
            raise RuntimeError("The selected round has no computed CGH result")
        return simulate_propagation_fft(
            selected.result.pattern,padding=True,pad_size=pad_size,
        )

    def feedback_measurement_metadata(self,section_key: str) -> dict[str,Any]:
        status = self.session.runtime.get_section_cgh_status(section_key)
        return {
            "slm_key":self.session.runtime.identity.key,
            "section_key":section_key,
            "cgh_state":status.result_state.value,
            "cgh_generation":status.result_generation,
            "target_type":status.target_type,
        }

    def localization_context(self,section_key: str) -> Mapping[str,Any]:
        runtime = self.session.runtime
        status = runtime.get_section_cgh_status(section_key)
        if status.result_state is not CGHResultState.CURRENT:
            return {}
        return runtime.get_section_feedback_localization_context(section_key)

    def require_current_cgh_for_localization_commit(self,section_key: str) -> None:
        status = self.session.runtime.get_section_cgh_status(section_key)
        if status.result_state is CGHResultState.MISSING:
            raise RuntimeError(
                "No CGH has been computed yet. Compute the CGH before accepting "
                "feedback localization."
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
        status = self.session.runtime.get_section_cgh_status(section_key)
        if status.result_state is not CGHResultState.CURRENT:
            raise RuntimeError(
                "Target localization guidance requires a current CGH result. "
                "Use automatic/manual localization or recompute the CGH first."
            )

    def start_automatic_feedback(
        self,
        section_key: str,
        *,
        rounds: int,
        source: str,
        reuse_previous_localization: bool=False,
    ) -> bool:
        self._require_editor_mode()
        return self._automatic.start(
            section_key,
            rounds=rounds,
            source=source,
            reuse_previous_localization=reuse_previous_localization,
        )

    def stop_automatic_feedback(self) -> None:
        self._automatic.stop()

    def prepare_runtime_change(self) -> None:
        self._automatic.cancel_for_runtime_change()
        for section_key in tuple(self._measurement_requests):
            self.cancel_measurement(section_key)
        self._feedback_orientations.clear()

    def dispose(self) -> None:
        self.prepare_runtime_change()
        if self._position_reference_store is not None:
            self._position_reference_store.remove_listener(
                self._on_position_reference_store_changed
            )
        if self._fov_position_calibration_store is not None:
            self._fov_position_calibration_store.remove_listener(
                self._on_fov_position_calibration_store_changed
            )
        self._position_reference_selections.clear()
        self._fov_position_calibration_selections.clear()
        self._fov_position_calibration_candidates.clear()

    def _update_committed_analysis(
        self,section_key: str,localization: Any=None,
    ) -> None:
        try:
            runtime = self.session.runtime
            analysis = runtime.compute_section_feedback_intensity_analysis(
                section_key,localization,
                orientation=self.feedback_orientation(section_key),
            )
            runtime.set_section_feedback_intensity_analysis(section_key,analysis)
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
        if not self.session.editor_writes_allowed:
            if raise_errors:
                raise RuntimeError(
                    "Feedback changes are unavailable in Fast Config mode"
                )
            return False,None
        self.session.cancel_cgh(section_key)
        try:
            transition = operation(self.session.runtime)
            if transition is not None:
                self._callbacks.on_transition_committed(section_key,transition)
            else:
                self._section_changed(section_key)
            return True,transition
        except Exception as error:
            self._section_changed(section_key)
            if raise_errors:
                raise
            self._error(error_title,error)
            return False,None

    def _section_changed(self,section_key: str) -> None:
        self._callbacks.on_section_changed(section_key)

    def _warning(self,title: str,message: Any) -> None:
        self._callbacks.on_warning(str(title),message)

    def _error(self,title: str,error: Exception) -> None:
        if not isinstance(error,Exception):
            error = RuntimeError(str(error))
        self._callbacks.on_error(str(title),error)

    def _require_editor_mode(self) -> None:
        if not self.session.editor_writes_allowed:
            raise RuntimeError("Operation unavailable in Fast Config mode")


def _localization_is_complete(localization: Any) -> bool:
    count = int(localization.lattice_indices.shape[1])
    matched_value = dict(getattr(localization,"diagnostics",{}) or {}).get(
        "matched_mask"
    )
    if matched_value is None:
        return True
    matched = np.asarray(matched_value,dtype=bool)
    return matched.shape == (count,) and bool(np.all(matched))
