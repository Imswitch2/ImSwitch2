import numpy as np
import pytest

from slmcore import (
    DEFAULT_REGISTRIES,CalibrationMismatchPolicy,ImageMeasurement,
    MappingStore,SLMCalibrationStore,SLMGeometry,SLMIdentity,
    SLMRuntime,SLMSectionCalibration,SLMSession,SLMSessionCallbacks,
    SLMStartupPreferences,
)
from slmcore.application.startup_preferences import StartupPreferencesState
from slmcore.core.calibration import section_geometry_to_dict
from slmcore.core.cgh import CGHResult
from slmcore.core.cgh.execution.status import CGHResultState
from slmcore.core.engine.section import split_slm_geometry
from slmcore.host import MockSLMDeviceProvider,SLMHostServices


def _runtime(*,with_cgh=False):
    geometry = SLMGeometry(width=64,height=64,pixel_size_um=1.0)
    runtime = SLMRuntime(
        identity=SLMIdentity("slm","SER123"),
        geometry=geometry,
        section_geometries=split_slm_geometry(geometry,1),
        registries=DEFAULT_REGISTRIES,
    )
    if with_cgh:
        runtime.apply_section_patch(
            "sec_0",
            {
                ("cgh","active"):True,
                ("cgh","selected_target"):"multi_foci_vector",
                ("cgh","multi_foci_vector","params","n_foci_x"):2,
                ("cgh","multi_foci_vector","params","n_foci_y"):2,
            },
        )
        job = runtime.prepare_section_base_cgh("sec_0")
        runtime.commit_section_cgh(
            "sec_0",
            CGHResult(
                generation=job.generation,
                spec=job.spec,
                target_name=job.target_name,
                pattern=np.ones(job.spec.context.shape,dtype=np.complex128),
            ),
        )
    return runtime


def _mapping(store):
    return store.add({
        "name":"100x",
        "description":"test",
    }).name


def _save_calibration(store,runtime,mapping,*,geometry=None):
    geometry = runtime.get_section_geometry("sec_0") if geometry is None else geometry
    return store.save_calibration(
        runtime.identity,"sec_0",mapping,
        SLMSectionCalibration(
            kx_per_um=0.01,
            ky_per_um=0.02,
            section_geometry=section_geometry_to_dict(geometry),
        ),
    )


class _Request:
    def __init__(self):
        self._active = True
        self.cancelled = False

    @property
    def active(self):
        return self._active

    def cancel(self):
        self.cancelled = True
        self._active = False


class _Dispatcher:
    available = True

    def __init__(self):
        self.pending = []

    def available_sources(self,section_key):
        return ("cam","cam2")

    def preferred_source(self,section_key,available):
        return available[0] if available else None

    def acquire(self,section_key,source,*,metadata,on_result,on_error):
        request = _Request()
        self.pending.append((request,on_result,on_error,metadata,source))
        return request

    def complete(self,measurement,index=-1):
        request,on_result,_on_error,_metadata,_source = self.pending[index]
        request._active = False
        on_result(measurement)


def test_mapping_selection_is_prepared_then_committed_headlessly(tmp_path):
    runtime = _runtime()
    mapping_store = MappingStore(tmp_path)
    store = SLMCalibrationStore(tmp_path/"calibrations")
    mapping = _mapping(mapping_store)
    _save_calibration(store,runtime,mapping)
    session = SLMSession(runtime=runtime,calibration_store=store,mapping_store=mapping_store)

    prepared = session.calibration.prepare_mapping_selection("sec_0",mapping)
    assert prepared.mapping_name == mapping
    assert prepared.calibration_mismatches == ()
    assert session.calibration.active_mapping("sec_0") is None

    assert session.calibration.select_mapping(prepared)
    assert session.calibration.active_mapping("sec_0") == mapping
    assert runtime.get_section_calibration_copy("sec_0").mapping_name == mapping


def test_switching_mapping_changes_calibration_context_and_stales_current_cgh(tmp_path):
    runtime = _runtime(with_cgh=True)
    mapping_store = MappingStore(tmp_path)
    store = SLMCalibrationStore(tmp_path/"calibrations")
    first = mapping_store.add({"name":"100x"}).name
    second = mapping_store.add({"name":"60x"}).name
    _save_calibration(store,runtime,first)
    store.save_calibration(
        runtime.identity,"sec_0",second,
        SLMSectionCalibration(
            kx_per_um=0.03,ky_per_um=0.04,
            section_geometry=section_geometry_to_dict(
                runtime.get_section_geometry("sec_0")
            ),
        ),
    )
    session = SLMSession(
        runtime=runtime,calibration_store=store,mapping_store=mapping_store,
    )

    assert runtime.get_section_cgh_status("sec_0").result_state is CGHResultState.CURRENT
    session.calibration.select_mapping(
        session.calibration.prepare_mapping_selection("sec_0",first)
    )
    assert runtime.get_section_cgh_status("sec_0").result_state is CGHResultState.STALE

    job = runtime.prepare_section_base_cgh("sec_0")
    runtime.commit_section_cgh(
        "sec_0",
        CGHResult(
            generation=job.generation,spec=job.spec,target_name=job.target_name,
            pattern=np.ones(job.spec.context.shape,dtype=np.complex128),
        ),
    )
    assert runtime.get_section_cgh_status("sec_0").result_state is CGHResultState.CURRENT

    session.calibration.select_mapping(
        session.calibration.prepare_mapping_selection("sec_0",second)
    )

    calibration = runtime.get_section_calibration_copy("sec_0")
    assert session.calibration.active_mapping("sec_0") == second
    assert calibration.mapping_name == second
    assert calibration.kx_per_um == pytest.approx(0.03)
    assert calibration.ky_per_um == pytest.approx(0.04)
    assert runtime.get_section_cgh_status("sec_0").result_state is CGHResultState.STALE


def test_uncalibrated_mapping_selection_is_independent_from_runtime_calibration(tmp_path):
    runtime = _runtime()
    mapping_store = MappingStore(tmp_path)
    store = SLMCalibrationStore(tmp_path/"calibrations")
    mapping = _mapping(mapping_store)
    session = SLMSession(runtime=runtime,calibration_store=store,mapping_store=mapping_store)

    prepared = session.calibration.prepare_mapping_selection("sec_0",mapping)
    assert prepared.calibration is None
    assert session.calibration.select_mapping(prepared)

    assert session.calibration.active_mapping("sec_0") == mapping
    assert runtime.get_section_calibration_copy("sec_0") is None


def test_runtime_replacement_restores_active_mapping_from_calibration_provenance(tmp_path):
    runtime = _runtime()
    mapping_store = MappingStore(tmp_path)
    store = SLMCalibrationStore(tmp_path/"calibrations")
    mapping = _mapping(mapping_store)
    session = SLMSession(runtime=runtime,calibration_store=store,mapping_store=mapping_store)

    replacement = _runtime()
    calibration = _save_calibration(store,replacement,mapping)
    replacement.set_section_calibration("sec_0",calibration)
    session.replace_runtime(replacement)

    assert session.calibration.active_mapping("sec_0") == mapping
    assert replacement.get_section_calibration_copy("sec_0").mapping_name == mapping


def test_mapping_geometry_mismatch_defaults_to_reject_but_keep_is_explicit(tmp_path):
    runtime = _runtime()
    mapping_store = MappingStore(tmp_path)
    store = SLMCalibrationStore(tmp_path/"calibrations")
    mapping = _mapping(mapping_store)
    wrong_geometry = split_slm_geometry(
        SLMGeometry(width=32,height=32,pixel_size_um=1.0),1,
    )["sec_0"]
    _save_calibration(store,runtime,mapping,geometry=wrong_geometry)
    session = SLMSession(runtime=runtime,calibration_store=store,mapping_store=mapping_store)

    prepared = session.calibration.prepare_mapping_selection("sec_0",mapping)
    assert prepared.calibration_mismatches
    with pytest.raises(ValueError,match="Calibration geometry"):
        session.calibration.select_mapping(prepared)
    assert session.calibration.active_mapping("sec_0") is None

    assert session.calibration.select_mapping(
        prepared,calibration_mismatch_policy=CalibrationMismatchPolicy.KEEP,
    )
    assert session.calibration.active_mapping("sec_0") == mapping


def test_startup_default_calibration_is_application_owned_and_not_uploaded(tmp_path):
    runtime = _runtime()
    mapping_store = MappingStore(tmp_path)
    store = SLMCalibrationStore(tmp_path/"calibrations")
    mapping = _mapping(mapping_store)
    _save_calibration(store,runtime,mapping)
    saved_preferences = []
    preferences = StartupPreferencesState(
        SLMStartupPreferences(default_mappings={"sec_0":mapping}),
        saved_preferences.append,
    )
    device = MockSLMDeviceProvider()

    session = SLMSession(
        runtime=runtime,
        host_services=SLMHostServices(device=device),
        calibration_store=store,
        mapping_store=mapping_store,
        startup_preferences=preferences,
        apply_startup_calibration_defaults=True,
    )

    assert session.calibration.active_mapping("sec_0") == mapping
    assert runtime.get_section_calibration_copy("sec_0").mapping_name == mapping
    assert device.upload_count == 0
    assert saved_preferences == []


def test_store_catalog_deletion_reconciles_runtime_and_preference(tmp_path):
    runtime = _runtime()
    mapping_store = MappingStore(tmp_path)
    store = SLMCalibrationStore(tmp_path/"calibrations")
    mapping = _mapping(mapping_store)
    _save_calibration(store,runtime,mapping)
    saved_preferences = []
    preferences = StartupPreferencesState(
        SLMStartupPreferences(),saved_preferences.append,
    )
    session = SLMSession(
        runtime=runtime,
        calibration_store=store,
        mapping_store=mapping_store,
        startup_preferences=preferences,
    )
    session.calibration.select_mapping(
        session.calibration.prepare_mapping_selection("sec_0",mapping)
    )
    assert preferences.default_mapping("sec_0") == mapping

    session.calibration.delete_mapping(mapping)

    assert session.calibration.active_mapping("sec_0") is None
    assert runtime.get_section_calibration_copy("sec_0") is None
    assert preferences.default_mapping("sec_0") is None
    assert saved_preferences


def test_delete_calibration_keeps_active_mapping_and_mapping_definition(tmp_path):
    runtime = _runtime()
    mapping_store = MappingStore(tmp_path)
    store = SLMCalibrationStore(tmp_path/"calibrations")
    mapping = _mapping(mapping_store)
    _save_calibration(store,runtime,mapping)
    session = SLMSession(runtime=runtime,calibration_store=store,mapping_store=mapping_store)
    session.calibration.select_mapping(
        session.calibration.prepare_mapping_selection("sec_0",mapping)
    )

    deleted = session.calibration.delete_calibration("sec_0")

    assert deleted is not None
    assert session.calibration.active_mapping("sec_0") == mapping
    assert runtime.get_section_calibration_copy("sec_0") is None
    assert mapping_store.has(mapping)
    assert not store.load_calibration(runtime.identity,"sec_0",mapping).is_valid()


def test_calibration_live_acquisition_uses_host_neutral_dispatcher(tmp_path):
    runtime = _runtime(with_cgh=True)
    mapping_store = MappingStore(tmp_path)
    store = SLMCalibrationStore(tmp_path/"calibrations")
    mapping = _mapping(mapping_store)
    _save_calibration(store,runtime,mapping)
    dispatcher = _Dispatcher()
    device = MockSLMDeviceProvider()
    busy = []
    session = SLMSession(
        runtime=runtime,
        calibration_store=store,
        mapping_store=mapping_store,
        measurement_dispatcher=dispatcher,
        host_services=SLMHostServices(device=device),
        callbacks=SLMSessionCallbacks(
            on_calibration_measurement_busy_changed=(
                lambda key,value,message:busy.append((key,value,message))
            ),
        ),
    )
    session.calibration.select_mapping(
        session.calibration.prepare_mapping_selection("sec_0",mapping)
    )
    # Calibration changes invalidate the previously computed CGH. Recompute it
    # before exercising the live-acquisition policy.
    job = runtime.prepare_section_base_cgh("sec_0")
    runtime.commit_section_cgh(
        "sec_0",
        CGHResult(
            generation=job.generation,
            spec=job.spec,
            target_name=job.target_name,
            pattern=np.ones(job.spec.context.shape,dtype=np.complex128),
        ),
    )
    session.upload_current_frame()
    assert device.upload_count == 1

    state = session.calibration.ensure_target_reference("sec_0")
    assert state.reference is not None
    availability = session.calibration.acquisition_availability("sec_0")
    assert availability.available
    assert availability.detector is None

    session.calibration.acquire_target_measurement("sec_0","cam2")
    request = dispatcher.pending[-1][0]
    assert request.active
    assert dispatcher.pending[-1][4] == "cam2"
    assert dispatcher.pending[-1][3]["mapping_name"] == mapping
    assert busy[-1][1] is True

    measurement = ImageMeasurement(
        image=np.zeros((64,64),dtype=np.float64),
        source="cam",
        detector="cam",
    )
    dispatcher.complete(measurement)
    assert busy[-1][1] is False
    assert session.calibration.target_state("sec_0").measurement is measurement


def test_target_state_is_discarded_on_runtime_change(tmp_path):
    runtime = _runtime(with_cgh=True)
    mapping_store = MappingStore(tmp_path)
    store = SLMCalibrationStore(tmp_path/"calibrations")
    mapping = _mapping(mapping_store)
    _save_calibration(store,runtime,mapping)
    session = SLMSession(runtime=runtime,calibration_store=store,mapping_store=mapping_store)
    session.calibration.select_mapping(
        session.calibration.prepare_mapping_selection("sec_0",mapping)
    )
    assert session.calibration.ensure_target_reference("sec_0") is not None
    assert session.calibration.target_state("sec_0") is not None

    session.calibration.prepare_runtime_change()
    assert session.calibration.target_state("sec_0") is None
