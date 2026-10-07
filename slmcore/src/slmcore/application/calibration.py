from __future__ import annotations

from dataclasses import dataclass,field,replace
import math
from typing import Any,Callable,Mapping

from ..core.calibration.geometry import (
    CalibrationGeometryMismatch,attach_calibration_geometry,
    calibration_geometry_mismatches,
)
from ..core.calibration.slm_section_calibration import SLMSectionCalibration
from ..workspace.calibration_store import SLMCalibrationStore
from .mapping import MappingService
from .observation import ObservationPlaneService
from .observation_geometry import ObservationGeometryService
from ..core.calibration.target_localization_calibration import (
    TargetLocalizationCalibrationCandidate,fit_target_localization_calibration,
)
from ..core.cgh.execution.status import CGHResultState
from ..core.cgh.localization.parameters import (
    LOCALIZATION_PARAMS,normalize_localization_parameter_input,
)
from ..core.cgh.localization.policy import suggest_localization_sources
from ..core.cgh.localization.reference import TargetLocalizationReference
from ..core.cgh.localization.workflow import localize_measurement
from ..core.measurement import ImageMeasurement
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
    values = normalize_localization_parameter_input(values)
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

    section_key: str=""
    mapping_name: str | None=None
    observation_plane_name: str | None=None
    source: str | None=None
    pixel_size_um_override: float | None=None
    measurement: ImageMeasurement | None=None
    localization_parameters: dict[str,Any]=field(default_factory=dict)
    reference: TargetLocalizationReference | None=None
    target_signature: str | None=None
    localization: Any=None
    calibration_candidate: TargetLocalizationCalibrationCandidate | None=None


@dataclass(frozen=True)
class PreparedMappingSelection:
    """Side-effect-free description of one requested active-mapping change."""

    section_key: str
    mapping_name: str | None
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

    on_mappings_changed: Callable[[],None]=_noop
    on_state_changed: Callable[[str],None]=_noop
    on_measurement_busy_changed: Callable[[str,bool,str],None]=_noop
    on_measurement_error: Callable[[str,Exception],None]=_noop
    on_warning: Callable[[str,Any],None]=_noop
    on_error: Callable[[str,Exception],None]=_noop


class SLMCalibrationService:
    """Toolkit-independent calibration/mapping workflow for one SLM session."""

    def __init__(
        self,
        session,
        *,
        store: SLMCalibrationStore | None=None,
        mappings: MappingService | None=None,
        observation_planes: ObservationPlaneService | None=None,
        observation_geometry: ObservationGeometryService | None=None,
        preferences=None,
        display_name: str="",
        measurements: MeasurementDispatcher | None=None,
        callbacks: SLMCalibrationCallbacks | None=None,
    ) -> None:
        self.session = session
        self.store = store
        self.mappings = mappings or MappingService()
        self.observation_planes = observation_planes or ObservationPlaneService()
        self.observation_geometry = observation_geometry
        self.preferences = preferences
        self.display_name = str(display_name or session.runtime.identity.key)
        self.measurements = measurements
        self._callbacks = callbacks or SLMCalibrationCallbacks()
        self._states: dict[str,TargetCalibrationState] = {}
        self._requests: dict[str,MeasurementRequest] = {}
        self._active_mappings: dict[str,str] = {}
        self._generation = 0
        self._store_change_pending = False
        self._disposed = False
        if self.mappings.store is not None:
            self.mappings.add_listener(self._on_store_changed)
            if self.mappings.last_load_error is not None:
                self._warning(
                    "SLM mapping",
                    "Could not load mapping definitions: %s" % self.mappings.last_load_error,
                )
        self._synchronize_active_mappings_from_runtime()

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
    def mapping_names(self) -> tuple[str,...]:
        return self.mappings.names

    @property
    def last_mapping_store_load_error(self) -> Exception | None:
        return self.mappings.last_load_error

    def mapping_definition(self,mapping_name: str):
        return self._require_mappings().get(mapping_name)

    def active_mapping(self,section_key: str) -> str | None:
        self._require_section(section_key)
        mapping = str(self._active_mappings.get(section_key) or "").strip()
        if mapping and self.mappings.has(mapping):
            return mapping
        return None

    def _synchronize_active_mappings_from_runtime(self) -> None:
        """Reconcile independent mapping selection after construction/runtime replacement."""
        runtime = self.session.runtime
        previous = dict(self._active_mappings)
        active: dict[str,str] = {}
        for section_key in runtime.section_keys:
            calibration = runtime.get_section_calibration_copy(section_key)
            mapping = str(getattr(calibration,"mapping_name",None) or "").strip()
            if mapping and self.mappings.has(mapping):
                active[section_key] = mapping
                continue
            prior = str(previous.get(section_key) or "").strip()
            if prior and self.mappings.has(prior):
                active[section_key] = prior
                continue
            preferred = (
                None if self.preferences is None
                else self.preferences.default_mapping(section_key)
            )
            preferred = str(preferred or "").strip()
            if preferred and self.mappings.has(preferred):
                active[section_key] = preferred
        self._active_mappings = active

    def prepare_mapping_selection(
        self,section_key: str,mapping_name: str | None,
    ) -> PreparedMappingSelection:
        self._require_editor_mode()
        self._require_section(section_key)
        store = self._require_store()
        mapping = str(mapping_name or "").strip() or None
        if mapping is not None:
            self._require_mappings().get(mapping)
        calibration = (
            None if mapping is None
            else store.load_calibration(
                self.session.runtime.identity,section_key,mapping,
            )
        )
        if calibration is not None and not calibration.is_valid():
            calibration = None
        mismatches = calibration_geometry_mismatches(((
            section_key,
            self.session.runtime.get_section_geometry(section_key),
            calibration,
        ),))
        return PreparedMappingSelection(
            section_key=section_key,
            mapping_name=mapping,
            calibration=calibration,
            calibration_mismatches=tuple(mismatches),
        )

    def select_mapping(
        self,
        prepared: PreparedMappingSelection,
        *,
        calibration_mismatch_policy: CalibrationMismatchPolicy | str=(
            CalibrationMismatchPolicy.REJECT
        ),
    ) -> bool:
        self._require_editor_mode()
        if not isinstance(prepared,PreparedMappingSelection):
            raise TypeError("prepared must be a PreparedMappingSelection")
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
            else self.preferences.default_mapping(section_key)
        )
        previous_active = self.active_mapping(section_key)
        try:
            self.session.set_section_calibration(section_key,prepared.calibration)
            if prepared.mapping_name is None:
                self._active_mappings.pop(section_key,None)
            else:
                self._active_mappings[section_key] = prepared.mapping_name
            if self.preferences is not None:
                self.preferences.set_default_mapping(section_key,prepared.mapping_name)
            if prepared.mapping_name != previous_active:
                self.discard_target_state(section_key)
            self._callbacks.on_mappings_changed()
            return True
        except Exception:
            try:
                self.session.set_section_calibration(section_key,previous_calibration)
                if previous_active is None:
                    self._active_mappings.pop(section_key,None)
                else:
                    self._active_mappings[section_key] = previous_active
                if self.preferences is not None:
                    self.preferences.set_default_mapping(section_key,previous_default)
            except Exception as rollback_error:
                self._error("SLM mapping selection rollback failed",rollback_error)
            self._callbacks.on_mappings_changed()
            raise

    def add_mapping(self,definition: Mapping[str,Any]) -> str:
        self._require_editor_mode()
        return self._require_mappings().add(dict(definition or {})).name

    def delete_mapping(self,mapping_name: str) -> tuple[str,...]:
        """Delete a mapping profile and every persistent resource it owns.

        The mapping definition is removed last so catalog reconciliation can
        clear active runtime mapping/FOV state only after dependent persistent
        records and defaults have been removed.
        """
        self._require_editor_mode()
        mappings = self._require_mappings()
        name = mappings.get(mapping_name).name
        if self.preferences is not None:
            self.preferences.remove_mapping(name)
        mappings.delete_fov_corrections_for_mapping(name)
        deleted = self._require_store().delete_mapping_calibrations(name)
        mappings.delete(name)
        return deleted

    def saved_calibration(
        self,section_key: str,mapping_name: str,
    ) -> SLMSectionCalibration:
        """Load one persisted base calibration without changing active mapping state."""
        self._require_section(section_key)
        mapping = str(mapping_name or "").strip()
        if not mapping:
            raise ValueError("mapping_name must not be empty")
        self.mapping_definition(mapping)
        return self._require_store().load_calibration(
            self.session.runtime.identity,str(section_key),mapping,
        )

    def delete_mapping_calibration(
        self,section_key: str,mapping_name: str,
    ) -> str | None:
        """Delete one persisted base calibration while preserving the mapping profile."""
        self._require_editor_mode()
        self._require_section(section_key)
        mapping = str(mapping_name or "").strip()
        if not mapping:
            raise ValueError("mapping_name must not be empty")
        self.mapping_definition(mapping)
        if self.active_mapping(section_key) == mapping:
            return self.delete_calibration(section_key)
        return self._require_store().delete_calibration(
            self.session.runtime.identity,str(section_key),mapping,
        )

    def delete_calibration(self,section_key: str) -> str | None:
        """Delete only the active mapping calibration, preserving mapping selection."""
        self._require_editor_mode()
        self._require_section(section_key)
        store = self._require_store()
        mapping = self.active_mapping(section_key)
        if not mapping:
            raise RuntimeError("Select a mapping before deleting calibration.")
        runtime = self.session.runtime
        previous_calibration = runtime.get_section_calibration_copy(section_key)
        try:
            deleted = store.delete_calibration(
                runtime.identity,section_key,mapping,
            )
            self.session.set_section_calibration(section_key,None)
            self.discard_target_state(section_key)
            self._callbacks.on_mappings_changed()
            return deleted
        except Exception:
            if previous_calibration is not None and previous_calibration.is_valid():
                try:
                    restored = store.save_calibration(
                        runtime.identity,section_key,mapping,previous_calibration,
                    )
                    self.session.set_section_calibration(section_key,restored)
                except Exception as rollback_error:
                    self._error("SLM calibration deletion rollback failed",rollback_error)
            self._callbacks.on_mappings_changed()
            raise

    def apply_startup_defaults(self) -> None:
        if self.store is None or self.preferences is None or self.mappings.store is None:
            return
        runtime = self.session.runtime
        for section_key in runtime.section_keys:
            mapping = self.preferences.default_mapping(section_key)
            if not mapping:
                continue
            if not self.mappings.has(mapping):
                try:
                    self.preferences.set_default_mapping(section_key,None)
                except Exception as error:
                    self._error("SLM calibration preference failed",error)
                continue
            try:
                calibration = self.store.load_calibration(
                    runtime.identity,section_key,mapping,
                )
                if not calibration.is_valid():
                    calibration = None
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
                self._active_mappings[section_key] = mapping
            except Exception as error:
                self._warning(
                    "SLM calibration",
                    "Could not apply default calibration for %s/%s: %s"
                    % (self.display_name,section_key,error),
                )
        self._callbacks.on_mappings_changed()

    def target_state(self,section_key: str) -> TargetCalibrationState | None:
        return self._states.get(section_key)

    def observation_plane_names(self) -> tuple[str,...]:
        return tuple(item.name for item in self.observation_planes.list())

    def ensure_target_context(self,section_key: str) -> TargetCalibrationState:
        """Return/create mapping-calibration workflow state for one section.

        Observation selection is calibration-workflow state.  It is deliberately
        independent from Feedback's observation selection and from mapping
        identity itself.
        """
        self._require_editor_mode()
        self._require_section(section_key)
        state = self._states.get(section_key)
        mapping = self.active_mapping(section_key)
        if state is None:
            planes = self.observation_planes.list()
            plane_name = planes[0].name if len(planes) == 1 else None
            source = None
            if plane_name is not None:
                source = self.observation_planes.get(plane_name).preferred_detector
            state = TargetCalibrationState(
                section_key=str(section_key),
                mapping_name=mapping,
                observation_plane_name=plane_name,
                source=source,
                localization_parameters=_default_localization_parameters(),
            )
            self._states[section_key] = state
        elif state.mapping_name != mapping:
            state.mapping_name = mapping
            self._clear_target_measurement_state(state)
        return state

    def target_observation_plane(self,section_key: str) -> str | None:
        return self.ensure_target_context(section_key).observation_plane_name

    def select_target_observation_plane(
        self,section_key: str,observation_plane_name: str | None,
    ) -> bool:
        """Select observation context for mapping calibration only."""
        state = self.ensure_target_context(section_key)
        selected = str(observation_plane_name or "").strip() or None
        if selected is not None:
            plane = self.observation_planes.get(selected)
        else:
            plane = None
        if selected == state.observation_plane_name:
            return False
        self.cancel_request(section_key)
        state.observation_plane_name = selected
        state.source = None if plane is None else plane.preferred_detector
        state.pixel_size_um_override = None
        self._clear_target_measurement_state(state)
        self._callbacks.on_state_changed(section_key)
        return True

    def set_target_source(self,section_key: str,source: str | None) -> bool:
        state = self.ensure_target_context(section_key)
        selected = str(source or "").strip() or None
        if selected == state.source:
            return False
        self.cancel_request(section_key)
        state.source = selected
        self._clear_target_measurement_state(state)
        self._callbacks.on_state_changed(section_key)
        return True

    def set_target_pixel_size_override(
        self,section_key: str,pixel_size_um: float | None,
    ) -> None:
        state = self.ensure_target_context(section_key)
        if pixel_size_um is None:
            state.pixel_size_um_override = None
            return
        value = float(pixel_size_um)
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError("pixel_size_um must be a positive finite value")
        state.pixel_size_um_override = value

    @staticmethod
    def _clear_target_measurement_state(state: TargetCalibrationState) -> None:
        state.measurement = None
        state.localization = None
        state.calibration_candidate = None

    def ensure_target_reference(self,section_key: str) -> TargetCalibrationState:
        self._require_editor_mode()
        self._require_section(section_key)
        state = self.ensure_target_context(section_key)
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
        mapping = self.active_mapping(section_key)
        if not mapping:
            return CalibrationAcquisitionAvailability(
                False,reason="Select a mapping before live acquisition."
            )
        state = self.ensure_target_context(section_key)
        plane_name = state.observation_plane_name
        if not plane_name:
            return CalibrationAcquisitionAvailability(
                False,
                reason="Select an observation plane before live acquisition.",
            )
        try:
            available_sources = tuple(
                str(value).strip()
                for value in measurements.available_sources(section_key)
                if str(value).strip()
            )
        except Exception:
            available_sources = ()
        source = str(state.source or "").strip() or None
        if source not in available_sources:
            preferred = self.observation_planes.get(plane_name).preferred_detector
            if preferred in available_sources:
                source = preferred
            else:
                try:
                    source = measurements.preferred_source(section_key,available_sources)
                except Exception:
                    source = None
                source = str(source or "").strip() or None
                if source not in available_sources:
                    source = available_sources[0] if available_sources else None
        return CalibrationAcquisitionAvailability(True,detector=source)

    def acquire_target_measurement(
        self,
        section_key: str,
        detector: str | None=None,
        *,
        pixel_size_um: float | None=None,
    ) -> bool:
        self._require_editor_mode()
        state = self.ensure_target_reference(section_key)
        if detector is not None:
            requested = str(detector or "").strip() or None
            if requested != state.source:
                state.source = requested
                self._clear_target_measurement_state(state)
        if pixel_size_um is not None:
            self.set_target_pixel_size_override(section_key,pixel_size_um)
        availability = self.acquisition_availability(section_key)
        if not availability.available:
            raise RuntimeError(availability.reason)
        measurements = self.measurements
        if measurements is None:
            raise RuntimeError("No host measurement provider is configured.")
        detector = str(detector or availability.detector or "").strip()
        if not detector:
            raise RuntimeError("Select a detector before acquisition.")
        mapping = self.active_mapping(section_key)
        plane_name = state.observation_plane_name
        state.source = detector
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
                self.commit_target_measurement(
                    section_key,measurement,source=detector,
                )
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
                "mapping_name":mapping,
                "observation_plane":plane_name,
                "source":detector,
                "calibration_method":"target_localization",
            },
            on_result=on_result,
            on_error=on_error,
        )
        if request.active:
            self._requests[section_key] = request
        return True

    def commit_target_measurement(
        self,
        section_key: str,
        measurement: ImageMeasurement,
        *,
        source: str | None=None,
        pixel_size_um: float | None=None,
    ) -> TargetCalibrationState:
        self._require_editor_mode()
        state = self._states.get(section_key)
        if state is None or state.reference is None:
            raise RuntimeError("Target localization reference is not available.")
        self._require_target_observation_plane(state)
        if pixel_size_um is not None:
            self.set_target_pixel_size_override(section_key,pixel_size_um)
        measurement = self._resolve_target_measurement_scale(state,measurement)
        selected_source = str(source or "").strip() or None
        if selected_source is None:
            selected_source = (
                str(measurement.detector or "").strip()
                or str(measurement.source or "").strip()
                or None
            )
        state.source = selected_source
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
        self,
        section_key: str,
        parameters: Mapping[str,Any],
        *,
        pixel_size_um: float | None=None,
    ) -> TargetLocalizationCalibrationCandidate:
        self._require_editor_mode()
        state = self.ensure_target_reference(section_key)
        if state.measurement is None:
            raise RuntimeError("Acquire or load an image before localization.")
        if pixel_size_um is not None:
            self.set_target_pixel_size_override(section_key,pixel_size_um)
        state.measurement = self._resolve_target_measurement_scale(
            state,state.measurement,require=True,
        )
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
        candidate = self._fit_target_candidate(
            section_key,state,localization,
        )
        state.calibration_candidate = candidate
        self._callbacks.on_state_changed(section_key)
        return candidate

    def _fit_target_candidate(
        self,
        section_key: str,
        state: TargetCalibrationState,
        localization: Any,
    ) -> TargetLocalizationCalibrationCandidate:
        if state.reference is None:
            raise RuntimeError("Target localization reference is not available.")
        mapping = str(state.mapping_name or self.active_mapping(section_key) or "").strip()
        if not mapping:
            raise RuntimeError("Target calibration is not bound to a mapping.")
        self._require_mappings().get(mapping)
        state.mapping_name = mapping
        measurement = state.measurement
        pixel_size_um = None if measurement is None else measurement.pixel_size_um
        if pixel_size_um is None:
            raise RuntimeError(
                "Target-localization calibration requires measurement pixel_size_um."
            )
        return fit_target_localization_calibration(
            resolution=state.reference.resolution,
            localization=localization,
            detector_pixel_size_um=pixel_size_um,
            mapping_name=mapping,
            metadata={
                "slm_key":self.session.runtime.identity.key,
                "section_key":section_key,
                "target_type":state.reference.target_type,
                "target_signature":state.target_signature,
                "measurement_id":getattr(measurement,"measurement_id",None),
                "measurement_source":getattr(measurement,"source",None),
                "measurement_detector":getattr(measurement,"detector",None),
                "observation_plane":state.observation_plane_name,
                "observation_source":state.source,
                "pixel_size_um":pixel_size_um,
            },
        )

    def _require_target_observation_plane(
        self,state: TargetCalibrationState,
    ) -> str:
        plane_name = str(state.observation_plane_name or "").strip()
        if not plane_name:
            raise RuntimeError(
                "Select an observation plane before target-localization calibration."
            )
        self.observation_planes.get(plane_name)
        return plane_name

    def _resolve_target_measurement_scale(
        self,
        state: TargetCalibrationState,
        measurement: ImageMeasurement,
        *,
        require: bool=False,
    ) -> ImageMeasurement:
        if not isinstance(measurement,ImageMeasurement):
            raise TypeError("measurement must be an ImageMeasurement")
        if measurement.pixel_size_um is not None:
            return measurement
        plane_name = self._require_target_observation_plane(state)
        plane = self.observation_planes.get(plane_name)
        # An explicit workflow value overrides the observation-plane fallback.
        # A scale carried by the measurement itself remains authoritative above.
        pixel_size_um = state.pixel_size_um_override
        if pixel_size_um is None:
            pixel_size_um = plane.default_pixel_size_um
        if pixel_size_um is None:
            if require:
                raise RuntimeError(
                    "Target-localization calibration requires a detector pixel size. "
                    "Provide it in the measurement, define an observation-plane default, "
                    "or enter a pixel size for this calibration workflow."
                )
            return measurement
        metadata = dict(measurement.metadata)
        metadata.setdefault("pixel_size_resolution","observation_or_workflow_fallback")
        return replace(
            measurement,
            pixel_size_um=float(pixel_size_um),
            metadata=metadata,
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

    def apply_target_candidate(self,section_key: str,mapping_name: str) -> SLMSectionCalibration:
        state = self._states.get(section_key)
        if state is None or state.calibration_candidate is None:
            raise RuntimeError("Run target localization before setting calibration.")
        mapping = str(mapping_name or "").strip()
        if state.mapping_name and state.mapping_name != mapping:
            raise RuntimeError("Calibration candidate belongs to a different mapping.")
        return self.apply_and_save_calibration(
            section_key,mapping,state.calibration_candidate.calibration,
        )

    def apply_and_save_calibration(
        self,
        section_key: str,
        mapping_name: str,
        calibration: SLMSectionCalibration,
    ) -> SLMSectionCalibration:
        self._require_editor_mode()
        store = self._require_store()
        runtime = self.session.runtime
        previous_calibration = runtime.get_section_calibration_copy(section_key)
        previous_default = (
            None if self.preferences is None
            else self.preferences.default_mapping(section_key)
        )
        previous_active = self.active_mapping(section_key)
        try:
            self._require_mappings().get(mapping_name)
            value = SLMSectionCalibration.from_dict(calibration).copy()
            value.mapping_name = mapping_name
            value = attach_calibration_geometry(
                value,runtime.get_section_geometry(section_key),
            )
            self.session.set_section_calibration(section_key,value)
            value = store.save_calibration(
                runtime.identity,section_key,mapping_name,value,
            )
            self._active_mappings[section_key] = mapping_name
            if self.preferences is not None:
                self.preferences.set_default_mapping(section_key,mapping_name)
            self._callbacks.on_mappings_changed()
            return value
        except Exception:
            try:
                self.session.set_section_calibration(section_key,previous_calibration)
                if previous_active is None:
                    self._active_mappings.pop(section_key,None)
                else:
                    self._active_mappings[section_key] = previous_active
                if self.preferences is not None:
                    self.preferences.set_default_mapping(section_key,previous_default)
            except Exception as rollback_error:
                self._error("SLM calibration rollback failed",rollback_error)
            self._callbacks.on_mappings_changed()
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
        self._synchronize_active_mappings_from_runtime()
        # A loaded config may carry an embedded calibration whose mapping no
        # longer exists in the workspace catalog.  The catalog is authoritative:
        # reconcile every runtime replacement instead of leaving an orphan
        # calibration active but impossible to select/manage.
        if self.mappings.store is not None:
            self._store_change_pending = True
        if (
            self._store_change_pending
            and self.session.editor_writes_allowed
            and not self.session.automatic_operation_active
        ):
            self._reconcile_mapping_catalog()
            return
        self._callbacks.on_mappings_changed()

    def session_state_changed(self) -> None:
        if self.session.editor_writes_allowed and not self.session.automatic_operation_active:
            self.reconcile_pending_catalog_changes()

    def reconcile_pending_catalog_changes(self) -> None:
        if not self._store_change_pending:
            return
        self._reconcile_mapping_catalog()

    def _on_store_changed(self) -> None:
        if self._disposed:
            return
        if (
            not self.session.editor_writes_allowed
            or self.session.automatic_operation_active
        ):
            self._store_change_pending = True
            self._callbacks.on_mappings_changed()
            return
        self._reconcile_mapping_catalog()

    def _reconcile_mapping_catalog(self) -> None:
        self._store_change_pending = False
        if self.mappings.store is None:
            return
        runtime = self.session.runtime
        try:
            with self.session.defer_frame_upload():
                for section_key in runtime.section_keys:
                    if self.preferences is not None:
                        preferred = self.preferences.default_mapping(section_key)
                        if preferred and not self.mappings.has(preferred):
                            self.preferences.set_default_mapping(section_key,None)
                    active = str(self._active_mappings.get(section_key) or "").strip()
                    if active and not self.mappings.has(active):
                        self._active_mappings.pop(section_key,None)
                        self.discard_target_state(section_key)
                    calibration = runtime.get_section_calibration_copy(section_key)
                    calibration_mapping = str(
                        getattr(calibration,"mapping_name",None) or ""
                    ).strip()
                    if calibration_mapping and not self.mappings.has(calibration_mapping):
                        self._warning(
                            "SLM mapping",
                            (
                                'Ignoring embedded calibration for missing mapping "%s" '
                                'in section "%s".'
                                % (calibration_mapping,section_key)
                            ),
                        )
                        self.session.set_section_calibration(section_key,None)
        except Exception as error:
            self._error("SLM mapping catalog synchronization failed",error)
        self._callbacks.on_mappings_changed()

    def _require_store(self) -> SLMCalibrationStore:
        if self.store is None:
            raise RuntimeError("Calibration storage is not configured.")
        return self.store

    def _require_mappings(self) -> MappingService:
        if self.mappings.store is None:
            raise RuntimeError("Mapping storage is not configured.")
        return self.mappings

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
        if self.mappings.store is not None:
            self.mappings.remove_listener(self._on_store_changed)
        self._disposed = True


__all__ = [
    "CalibrationAcquisitionAvailability",
    "PreparedMappingSelection",
    "SLMCalibrationCallbacks",
    "SLMCalibrationService",
    "TargetCalibrationState",
]
