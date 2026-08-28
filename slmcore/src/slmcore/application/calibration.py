from __future__ import annotations

from dataclasses import dataclass,field
from typing import Any,Callable,Mapping

from ..calibration.geometry import (
    CalibrationGeometryMismatch,attach_calibration_geometry,
    calibration_geometry_mismatches,
)
from ..calibration.slm_section_calibration import SLMSectionCalibration
from ..calibration.store import SLMCalibrationStore
from ..calibration.target_localization_calibration import (
    TargetLocalizationCalibrationCandidate,fit_target_localization_calibration,
)
from ..cgh.execution.status import CGHResultState
from ..cgh.localization.parameters import LOCALIZATION_PARAMS
from ..cgh.localization.policy import suggest_localization_sources
from ..cgh.localization.reference import TargetLocalizationReference
from ..cgh.localization.workflow import localize_measurement
from ..measurement import ImageMeasurement
from .configuration import CalibrationMismatchPolicy
from .feedback import MeasurementDispatcher,MeasurementRequest


def _noop(*_args,**_kwargs) -> None:
    return None


def _default_localization_parameters() -> dict[str,Any]:
    return {
        key:spec.validate(spec.default)
        for key,spec in LOCALIZATION_PARAMS.items()
    }


def _validated_localization_parameters(
    values: Mapping[str,Any],
) -> dict[str,Any]:
    values = dict(values or {})
    unknown = set(values) - set(LOCALIZATION_PARAMS)
    if unknown:
        raise KeyError(
            "Unknown localization parameter(s): " + ", ".join(sorted(unknown))
        )
    return {
        key:spec.validate(values.get(key,spec.default))
        for key,spec in LOCALIZATION_PARAMS.items()
    }


@dataclass
class TargetCalibrationState:
    """Transient target-localization calibration workflow state."""

    plane_name: str | None=None
    measurement: ImageMeasurement | None=None
    localization_parameters: dict[str,Any]=field(default_factory=dict)
    reference: TargetLocalizationReference | None=None
    target_signature: str | None=None
    localization: Any=None
    calibration_candidate: TargetLocalizationCalibrationCandidate | None=None


@dataclass(frozen=True)
class PreparedPlaneSelection:
    """Side-effect-free description of one requested active-plane change."""

    section_key: str
    plane_name: str | None
    calibration: SLMSectionCalibration | None
    calibration_mismatches: tuple[CalibrationGeometryMismatch,...]=()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,"calibration_mismatches",tuple(self.calibration_mismatches),
        )


@dataclass(frozen=True)
class CalibrationAcquisitionAvailability:
    available: bool
    detector: str | None=None
    reason: str=""


@dataclass(frozen=True)
class SLMCalibrationCallbacks:
    """Presentation-neutral events emitted by :class:`SLMCalibrationService`."""

    on_planes_changed: Callable[[],None]=_noop
    on_state_changed: Callable[[str],None]=_noop
    on_measurement_busy_changed: Callable[[str,bool,str],None]=_noop
    on_measurement_error: Callable[[str,Exception],None]=_noop
    on_warning: Callable[[str,Any],None]=_noop
    on_error: Callable[[str,Exception],None]=_noop


class SLMCalibrationService:
    """Toolkit-independent calibration/plane workflow for one SLM session."""

    def __init__(
        self,
        session,
        *,
        store: SLMCalibrationStore | None=None,
        preferences=None,
        display_name: str="",
        measurements: MeasurementDispatcher | None=None,
        callbacks: SLMCalibrationCallbacks | None=None,
    ) -> None:
        self.session = session
        self.store = store
        self.preferences = preferences
        self.display_name = str(display_name or session.runtime.identity.key)
        self.measurements = measurements
        self._callbacks = callbacks or SLMCalibrationCallbacks()
        self._states: dict[str,TargetCalibrationState] = {}
        self._requests: dict[str,MeasurementRequest] = {}
        self._generation = 0
        self._store_change_pending = False
        self._disposed = False
        if self.store is not None:
            self.store.add_listener(self._on_store_changed)
            if self.store.last_load_error is not None:
                self._warning(
                    "SLM calibration",
                    "Could not load plane definitions: %s" % self.store.last_load_error,
                )

    def set_callbacks(self,callbacks: SLMCalibrationCallbacks | None) -> None:
        self._callbacks = callbacks or SLMCalibrationCallbacks()

    def set_measurement_dispatcher(
        self,measurements: MeasurementDispatcher | None,
    ) -> None:
        if measurements is self.measurements:
            return
        self.prepare_runtime_change()
        self.measurements = measurements

    @property
    def plane_names(self) -> tuple[str,...]:
        return () if self.store is None else self.store.plane_names

    @property
    def last_store_load_error(self) -> Exception | None:
        return None if self.store is None else self.store.last_load_error

    def plane_definition(self,plane_name: str) -> dict[str,Any]:
        return self._require_store().plane_definition(plane_name)

    def active_plane(self,section_key: str) -> str | None:
        self._require_section(section_key)
        calibration = self.session.runtime.get_section_calibration_copy(section_key)
        plane = str(getattr(calibration,"plane",None) or "").strip()
        if plane and self.store is not None and self.store.has_plane(plane):
            return plane
        return None

    def prepare_plane_selection(
        self,section_key: str,plane_name: str | None,
    ) -> PreparedPlaneSelection:
        self._require_editor_mode()
        self._require_section(section_key)
        store = self._require_store()
        plane = str(plane_name or "").strip() or None
        calibration = (
            None if plane is None
            else store.load_calibration(
                self.session.runtime.identity,section_key,plane,
            )
        )
        mismatches = calibration_geometry_mismatches(((
            section_key,
            self.session.runtime.get_section_geometry(section_key),
            calibration,
        ),))
        return PreparedPlaneSelection(
            section_key=section_key,
            plane_name=plane,
            calibration=calibration,
            calibration_mismatches=tuple(mismatches),
        )

    def select_plane(
        self,
        prepared: PreparedPlaneSelection,
        *,
        calibration_mismatch_policy: CalibrationMismatchPolicy | str=(
            CalibrationMismatchPolicy.REJECT
        ),
    ) -> bool:
        self._require_editor_mode()
        if not isinstance(prepared,PreparedPlaneSelection):
            raise TypeError("prepared must be a PreparedPlaneSelection")
        self._require_section(prepared.section_key)
        policy = CalibrationMismatchPolicy.normalize(calibration_mismatch_policy)
        if prepared.calibration_mismatches and policy is not CalibrationMismatchPolicy.KEEP:
            raise ValueError(
                "Calibration geometry differs from the current section layout: "
                + "; ".join(item.summary() for item in prepared.calibration_mismatches)
            )

        section_key = prepared.section_key
        previous_calibration = self.session.runtime.get_section_calibration_copy(section_key)
        previous_default = (
            None if self.preferences is None
            else self.preferences.default_plane(section_key)
        )
        previous_active = self.active_plane(section_key)
        try:
            self.session.set_section_calibration(section_key,prepared.calibration)
            if self.preferences is not None:
                self.preferences.set_default_plane(section_key,prepared.plane_name)
            if prepared.plane_name != previous_active:
                self.discard_target_state(section_key)
            self._callbacks.on_planes_changed()
            return True
        except Exception:
            try:
                self.session.set_section_calibration(section_key,previous_calibration)
                if self.preferences is not None:
                    self.preferences.set_default_plane(section_key,previous_default)
            except Exception as rollback_error:
                self._error("SLM plane selection rollback failed",rollback_error)
            self._callbacks.on_planes_changed()
            raise

    def add_plane(self,definition: Mapping[str,Any]) -> str:
        self._require_editor_mode()
        return self._require_store().add_plane(dict(definition or {}))

    def delete_plane(self,plane_name: str) -> tuple[str,...]:
        self._require_editor_mode()
        return self._require_store().delete_plane(plane_name)

    def apply_startup_defaults(self) -> None:
        if self.store is None or self.preferences is None:
            return
        runtime = self.session.runtime
        for section_key in runtime.section_keys:
            plane = self.preferences.default_plane(section_key)
            if not plane:
                continue
            if not self.store.has_plane(plane):
                try:
                    self.preferences.set_default_plane(section_key,None)
                except Exception as error:
                    self._error("SLM calibration preference failed",error)
                continue
            try:
                calibration = self.store.load_calibration(
                    runtime.identity,section_key,plane,
                )
                mismatches = calibration_geometry_mismatches(((
                    section_key,runtime.get_section_geometry(section_key),calibration,
                ),))
                if mismatches:
                    self._warning(
                        "SLM calibration",
                        "Default calibration for %s/%s was not applied because its "
                        "recorded section geometry differs from the current layout: %s"
                        % (self.display_name,section_key,mismatches[0].summary()),
                    )
                    continue
                self.session.set_section_calibration(
                    section_key,calibration,publish_frame=False,
                )
            except Exception as error:
                self._warning(
                    "SLM calibration",
                    "Could not apply default calibration for %s/%s: %s"
                    % (self.display_name,section_key,error),
                )
        self._callbacks.on_planes_changed()

    def target_state(self,section_key: str) -> TargetCalibrationState | None:
        return self._states.get(section_key)

    def ensure_target_reference(self,section_key: str) -> TargetCalibrationState:
        self._require_editor_mode()
        self._require_section(section_key)
        state = self._states.get(section_key)
        if state is None:
            state = TargetCalibrationState(
                plane_name=self.active_plane(section_key),
                localization_parameters=_default_localization_parameters(),
            )
            self._states[section_key] = state
        try:
            reference = self.session.runtime.get_section_base_target_localization_reference(
                section_key
            )
            signature = str(reference.target_signature or "")
            if state.reference is not None and state.target_signature == signature:
                return state

            state.reference = reference
            state.target_signature = signature
            state.measurement = None
            state.localization = None
            state.calibration_candidate = None
            state.localization_parameters = self._target_reference_localization_parameters(
                reference
            )
            self._callbacks.on_state_changed(section_key)
            return state
        except Exception:
            state.reference = None
            state.target_signature = None
            state.measurement = None
            state.localization = None
            state.calibration_candidate = None
            state.localization_parameters = _default_localization_parameters()
            self._callbacks.on_state_changed(section_key)
            raise

    @staticmethod
    def _target_reference_localization_parameters(
        reference: TargetLocalizationReference,
    ) -> dict[str,Any]:
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

    def acquisition_availability(
        self,section_key: str,
    ) -> CalibrationAcquisitionAvailability:
        if not self.session.editor_writes_allowed:
            return CalibrationAcquisitionAvailability(
                False,reason="Live acquisition is unavailable in Fast Config mode."
            )
        measurements = self.measurements
        if measurements is None or not measurements.available:
            return CalibrationAcquisitionAvailability(
                False,reason="Live acquisition requires a measurement provider."
            )
        if self.session.automatic_operation_active:
            return CalibrationAcquisitionAvailability(
                False,reason="Automatic feedback is currently running."
            )
        if self.session.is_cgh_computing(section_key):
            return CalibrationAcquisitionAvailability(
                False,reason="CGH computation is in progress."
            )
        status = self.session.runtime.get_section_cgh_status(section_key)
        if status.result_state is CGHResultState.MISSING:
            return CalibrationAcquisitionAvailability(
                False,reason="Live acquisition requires a computed and applied CGH target."
            )
        if status.result_state is CGHResultState.STALE:
            return CalibrationAcquisitionAvailability(
                False,
                reason=(
                    "Live acquisition requires the current CGH target to be computed "
                    "and applied. Recompute the CGH before acquiring a calibration image."
                ),
            )
        if status.result_state is not CGHResultState.CURRENT:
            return CalibrationAcquisitionAvailability(
                False,reason="Live acquisition requires a current computed and applied CGH."
            )
        if not self.session.auto_upload_frame:
            return CalibrationAcquisitionAvailability(
                False,
                reason=(
                    "Live acquisition is disabled when auto_upload_frame=False because "
                    "slmcore cannot guarantee that the current CGH is applied to hardware."
                ),
            )
        if not self.session.host_services.can_upload_frame:
            return CalibrationAcquisitionAvailability(
                False,reason="Live acquisition requires a frame-upload capability."
            )
        if self.session.last_upload_error is not None:
            return CalibrationAcquisitionAvailability(
                False,
                reason=(
                    "The last SLM frame upload failed. Upload the current frame successfully "
                    "before acquiring a calibration image."
                ),
            )
        plane = self.active_plane(section_key)
        if not plane or self.store is None:
            return CalibrationAcquisitionAvailability(
                False,reason="Select a calibration plane before live acquisition."
            )
        detector = str(
            self.store.plane_definition(plane).get("detector_name") or ""
        ).strip()
        if not detector:
            return CalibrationAcquisitionAvailability(
                False,reason="The active calibration plane has no detector configured."
            )
        return CalibrationAcquisitionAvailability(True,detector=detector)

    def acquire_target_measurement(self,section_key: str) -> bool:
        self._require_editor_mode()
        self.ensure_target_reference(section_key)
        availability = self.acquisition_availability(section_key)
        if not availability.available:
            raise RuntimeError(availability.reason)
        measurements = self.measurements
        if measurements is None:
            raise RuntimeError("No host measurement provider is configured.")
        detector = str(availability.detector or "")
        plane = self.active_plane(section_key)
        self.cancel_request(section_key)
        generation = self._generation
        self._callbacks.on_measurement_busy_changed(
            section_key,True,"Waiting for %s..." % detector,
        )

        def on_result(measurement: ImageMeasurement) -> None:
            if generation != self._generation:
                return
            self._requests.pop(section_key,None)
            self._callbacks.on_measurement_busy_changed(section_key,False,"")
            if not self.session.editor_writes_allowed:
                return
            try:
                self.commit_target_measurement(section_key,measurement)
            except Exception as error:
                self._callbacks.on_measurement_error(section_key,error)

        def on_error(error: Exception) -> None:
            if generation != self._generation:
                return
            self._requests.pop(section_key,None)
            self._callbacks.on_measurement_busy_changed(section_key,False,"")
            if not isinstance(error,Exception):
                error = RuntimeError(str(error))
            self._callbacks.on_measurement_error(section_key,error)

        request = measurements.acquire(
            section_key,
            detector,
            metadata={
                "slm_key":self.session.runtime.identity.key,
                "section_key":section_key,
                "plane_name":plane,
                "calibration_method":"target_localization",
            },
            on_result=on_result,
            on_error=on_error,
        )
        if request.active:
            self._requests[section_key] = request
        return True

    def commit_target_measurement(
        self,section_key: str,measurement: ImageMeasurement,
    ) -> TargetCalibrationState:
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
        self._callbacks.on_state_changed(section_key)
        return state

    def run_target_localization(
        self,section_key: str,parameters: Mapping[str,Any],
    ) -> TargetLocalizationCalibrationCandidate:
        self._require_editor_mode()
        state = self.ensure_target_reference(section_key)
        if state.measurement is None:
            raise RuntimeError("Acquire or load an image before localization.")
        normalized = _validated_localization_parameters(parameters)
        reference = state.reference
        if reference is None:
            raise RuntimeError("Target localization reference is not available.")
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
        candidate = self._fit_target_candidate(section_key,state,localization)
        state.calibration_candidate = candidate
        self._callbacks.on_state_changed(section_key)
        return candidate

    def _fit_target_candidate(
        self,section_key: str,state: TargetCalibrationState,localization: Any,
    ) -> TargetLocalizationCalibrationCandidate:
        if state.reference is None:
            raise RuntimeError("Target localization reference is not available.")
        plane = str(state.plane_name or self.active_plane(section_key) or "").strip()
        if not plane:
            raise RuntimeError("Target calibration is not bound to a plane.")
        store = self._require_store()
        state.plane_name = plane
        definition = store.plane_definition(plane)
        measurement = state.measurement
        return fit_target_localization_calibration(
            resolution=state.reference.resolution,
            localization=localization,
            detector_pixel_size_um=float(definition["detector_pixel_size_um"]),
            plane=plane,
            metadata={
                "slm_key":self.session.runtime.identity.key,
                "section_key":section_key,
                "target_type":state.reference.target_type,
                "target_signature":state.target_signature,
                "measurement_id":getattr(measurement,"measurement_id",None),
                "measurement_source":getattr(measurement,"source",None),
                "measurement_detector":getattr(measurement,"detector",None),
            },
        )

    @staticmethod
    def linear_phase_calibration(
        *,
        period_x_px: Any,
        measured_dx_um: Any,
        period_y_px: Any,
        measured_dy_um: Any,
    ) -> SLMSectionCalibration:
        return SLMSectionCalibration.from_linear_phase_test(
            period_x_px,measured_dx_um,period_y_px,measured_dy_um,
        )

    def apply_target_candidate(self,section_key: str,plane_name: str) -> SLMSectionCalibration:
        state = self._states.get(section_key)
        if state is None or state.calibration_candidate is None:
            raise RuntimeError("Run target localization before setting calibration.")
        plane = str(plane_name or "").strip()
        if state.plane_name and state.plane_name != plane:
            raise RuntimeError("Calibration candidate belongs to a different plane.")
        return self.apply_and_save_calibration(
            section_key,plane,state.calibration_candidate.calibration,
        )

    def apply_and_save_calibration(
        self,
        section_key: str,
        plane_name: str,
        calibration: SLMSectionCalibration,
    ) -> SLMSectionCalibration:
        self._require_editor_mode()
        store = self._require_store()
        runtime = self.session.runtime
        previous_calibration = runtime.get_section_calibration_copy(section_key)
        previous_default = (
            None if self.preferences is None
            else self.preferences.default_plane(section_key)
        )
        try:
            definition = store.plane_definition(plane_name)
            value = SLMSectionCalibration.from_dict(calibration).copy()
            value.plane = plane_name
            value.cam_px_size_um = float(definition["detector_pixel_size_um"])
            value = attach_calibration_geometry(
                value,runtime.get_section_geometry(section_key),
            )
            self.session.set_section_calibration(section_key,value)
            value = store.save_calibration(
                runtime.identity,self.display_name,section_key,plane_name,value,
            )
            if self.preferences is not None:
                self.preferences.set_default_plane(section_key,plane_name)
            self._callbacks.on_planes_changed()
            return value
        except Exception:
            try:
                self.session.set_section_calibration(section_key,previous_calibration)
                if self.preferences is not None:
                    self.preferences.set_default_plane(section_key,previous_default)
            except Exception as rollback_error:
                self._error("SLM calibration rollback failed",rollback_error)
            self._callbacks.on_planes_changed()
            raise

    def cancel_request(self,section_key: str) -> None:
        request = self._requests.pop(section_key,None)
        if request is not None:
            request.cancel()
        self._callbacks.on_measurement_busy_changed(section_key,False,"")

    def discard_target_state(self,section_key: str) -> None:
        self.cancel_request(section_key)
        self._states.pop(section_key,None)
        self._callbacks.on_state_changed(section_key)

    def prepare_runtime_change(self) -> None:
        self._generation += 1
        for section_key in tuple(self._requests):
            self.cancel_request(section_key)
        self._states.clear()

    def runtime_replaced(self) -> None:
        if self._store_change_pending:
            self.reconcile_pending_catalog_changes()
        self._callbacks.on_planes_changed()

    def session_state_changed(self) -> None:
        if self.session.editor_writes_allowed and not self.session.automatic_operation_active:
            self.reconcile_pending_catalog_changes()

    def reconcile_pending_catalog_changes(self) -> None:
        if not self._store_change_pending:
            return
        self._reconcile_plane_catalog()

    def _on_store_changed(self) -> None:
        if self._disposed:
            return
        if (
            not self.session.editor_writes_allowed
            or self.session.automatic_operation_active
        ):
            self._store_change_pending = True
            self._callbacks.on_planes_changed()
            return
        self._reconcile_plane_catalog()

    def _reconcile_plane_catalog(self) -> None:
        self._store_change_pending = False
        if self.store is None:
            return
        runtime = self.session.runtime
        try:
            with self.session.defer_frame_upload():
                for section_key in runtime.section_keys:
                    if self.preferences is not None:
                        preferred = self.preferences.default_plane(section_key)
                        if preferred and not self.store.has_plane(preferred):
                            self.preferences.set_default_plane(section_key,None)
                    calibration = runtime.get_section_calibration_copy(section_key)
                    active = str(getattr(calibration,"plane",None) or "").strip()
                    if active and not self.store.has_plane(active):
                        self.session.set_section_calibration(section_key,None)
                        self.discard_target_state(section_key)
        except Exception as error:
            self._error("SLM plane catalog synchronization failed",error)
        self._callbacks.on_planes_changed()

    def _require_store(self) -> SLMCalibrationStore:
        if self.store is None:
            raise RuntimeError("Calibration storage is not configured.")
        return self.store

    def _require_editor_mode(self) -> None:
        if not self.session.editor_writes_allowed:
            raise RuntimeError("Operation unavailable in Fast Config mode")
        if self.session.automatic_operation_active:
            raise RuntimeError("Operation unavailable during automatic feedback")

    def _require_section(self,section_key: str) -> None:
        if section_key not in self.session.runtime.section_keys:
            raise KeyError("Unknown SLM section %r" % section_key)

    def _warning(self,title: str,message: Any) -> None:
        self._callbacks.on_warning(str(title),message)

    def _error(self,title: str,error: Exception) -> None:
        if not isinstance(error,Exception):
            error = RuntimeError(str(error))
        self._callbacks.on_error(str(title),error)

    def dispose(self) -> None:
        if self._disposed:
            return
        self.prepare_runtime_change()
        if self.store is not None:
            self.store.remove_listener(self._on_store_changed)
        self._disposed = True


__all__ = [
    "CalibrationAcquisitionAvailability",
    "PreparedPlaneSelection",
    "SLMCalibrationCallbacks",
    "SLMCalibrationService",
    "TargetCalibrationState",
]
