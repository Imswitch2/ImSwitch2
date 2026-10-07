import numpy as np
import pytest

from slmcore import (
    DEFAULT_REGISTRIES,CalibrationMismatchPolicy,GeometryOrientationCalibration,
    FOVMappingCorrection,ImageMeasurement,MappingStore,ObservationPlaneDefinition,ObservationPlaneStore,
    SLMCalibrationStore,SLMGeometry,SLMGeometryOrientationCalibrationStore,SLMIdentity,
    SLMRuntime,SLMSectionCalibration,SLMSession,SLMSessionCallbacks,
    SLMStartupPreferences,SLMWorkspace,
)
from slmcore.application.startup_preferences import StartupPreferencesState
from slmcore.core.calibration import base_mapping_signature,section_geometry_to_dict
from slmcore.core.cgh import CGHResult
from slmcore.core.cgh.execution.status import CGHResultState
from slmcore.core.cgh.localization import LocalizationResult
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


def test_switching_mapping_keeps_slm_unit_cgh_current(tmp_path):
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
    assert runtime.get_section_cgh_status("sec_0").result_state is CGHResultState.CURRENT

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
    assert runtime.get_section_cgh_status("sec_0").result_state is CGHResultState.CURRENT


def test_metric_square_mapping_change_stales_when_resolved_target_changes(tmp_path):
    runtime = _runtime()
    mapping_store = MappingStore(tmp_path)
    store = SLMCalibrationStore(tmp_path/"calibrations")
    first = mapping_store.add({"name":"100x"}).name
    second = mapping_store.add({"name":"60x"}).name
    store.save_calibration(
        runtime.identity,"sec_0",first,
        SLMSectionCalibration(
            kx_per_um=0.01,ky_per_um=0.02,
            section_geometry=section_geometry_to_dict(runtime.get_section_geometry("sec_0")),
        ),
    )
    store.save_calibration(
        runtime.identity,"sec_0",second,
        SLMSectionCalibration(
            kx_per_um=0.03,ky_per_um=0.04,
            section_geometry=section_geometry_to_dict(runtime.get_section_geometry("sec_0")),
        ),
    )
    session = SLMSession(
        runtime=runtime,calibration_store=store,mapping_store=mapping_store,
    )
    session.calibration.select_mapping(
        session.calibration.prepare_mapping_selection("sec_0",first)
    )
    runtime.apply_section_patch(
        "sec_0",
        {
            ("cgh","active"):True,
            ("cgh","selected_target"):"multi_foci_vector",
            ("cgh","multi_foci_vector","params","square"):True,
            ("cgh","multi_foci_vector","params","square_unit"):"metric",
        },
    )
    job = runtime.prepare_section_base_cgh("sec_0")
    runtime.commit_section_cgh(
        "sec_0",
        CGHResult(
            generation=job.generation,spec=job.spec,target_name=job.target_name,
            pattern=np.ones(job.spec.context.shape,dtype=np.complex128),
        ),
    )
    before = runtime.get_section_parameter(
        "sec_0",("cgh","multi_foci_vector","params","period_y_px"),
    )

    session.calibration.select_mapping(
        session.calibration.prepare_mapping_selection("sec_0",second)
    )

    after = runtime.get_section_parameter(
        "sec_0",("cgh","multi_foci_vector","params","period_y_px"),
    )
    assert after != before
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


def test_delete_mapping_cascades_owned_fov_defaults_and_runtime_state(tmp_path):
    from dataclasses import replace

    runtime = _runtime()
    workspace = SLMWorkspace(tmp_path)
    mapping = workspace.mapping_store.add({"name":"100x"}).name
    other = workspace.mapping_store.add({"name":"60x"}).name
    _save_calibration(workspace.calibration_store,runtime,mapping)
    _save_calibration(workspace.calibration_store,runtime,other)

    saved_preferences = []
    preferences = StartupPreferencesState(
        SLMStartupPreferences(fov_mapping_corrections={
            "sec_0":{"mappings":{mapping:"field A",other:"field B"}},
        }),
        saved_preferences.append,
    )
    session = SLMSession(
        runtime=runtime,
        calibration_store=workspace.calibration_store,
        mapping_store=workspace.mapping_store,
        fov_mapping_correction_store=workspace.fov_mapping_correction_store,
        startup_preferences=preferences,
    )
    session.calibration.select_mapping(
        session.calibration.prepare_mapping_selection("sec_0",mapping)
    )

    signature = base_mapping_signature(runtime.get_section_calibration_copy("sec_0"))
    positions = np.asarray(((-0.1,0.1,-0.1,0.1),(-0.1,-0.1,0.1,0.1)))
    displacement = np.asarray(((0.01,0.01,0.01,0.01),(-0.02,-0.02,-0.02,-0.02)))
    first = FOVMappingCorrection.fit(
        name="field A",slm_serial=runtime.identity.serial_number,
        section_key="sec_0",mapping_name=mapping,base_mapping_signature=signature,
        ideal_positions_kxy=positions,total_displacements_kxy=displacement,degree=1,
    )
    second = replace(first,name="field B",mapping_name=other)
    other_slm = replace(first,name="other SLM",slm_serial="SER999")
    workspace.fov_mapping_correction_store.save(first)
    workspace.fov_mapping_correction_store.save(second)
    workspace.fov_mapping_correction_store.save(other_slm)

    session.fov_mapping_runtime.select_fov_mapping_correction("sec_0","field A")
    session.fov_mapping_runtime.set_fov_mapping_correction_applied("sec_0",True)
    assert runtime.get_section_fov_mapping_context("sec_0")["correction"] is not None

    deleted = session.calibration.delete_mapping(mapping)

    assert deleted
    assert not workspace.mapping_store.has(mapping)
    assert workspace.mapping_store.has(other)
    assert not workspace.calibration_store.load_calibration(
        runtime.identity,"sec_0",mapping,
    ).is_valid()
    assert workspace.calibration_store.load_calibration(
        runtime.identity,"sec_0",other,
    ).is_valid()
    assert workspace.fov_mapping_correction_store.list(
        runtime.identity,"sec_0",mapping,
    ) == ()
    assert workspace.fov_mapping_correction_store.list(
        "SER999","sec_0",mapping,
    ) == ()
    assert workspace.fov_mapping_correction_store.list(
        runtime.identity,"sec_0",other,
    ) == ("field B",)
    assert preferences.default_mapping("sec_0") is None
    assert preferences.default_fov_correction("sec_0",mapping) is None
    assert preferences.default_fov_correction("sec_0",other) == "field B"
    assert session.calibration.active_mapping("sec_0") is None
    assert runtime.get_section_calibration_copy("sec_0") is None
    assert runtime.get_section_fov_mapping_context("sec_0")["correction"] is None
    assert saved_preferences
    session.dispose()


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
    observation_store = ObservationPlaneStore(tmp_path/"observations")
    observation_store.add(ObservationPlaneDefinition(
        name="Intermediate",preferred_detector="cam2",default_pixel_size_um=0.2,
    ))
    device = MockSLMDeviceProvider()
    busy = []
    session = SLMSession(
        runtime=runtime,
        calibration_store=store,
        mapping_store=mapping_store,
        observation_plane_store=observation_store,
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
    assert availability.detector == "cam2"

    session.calibration.acquire_target_measurement("sec_0","cam2")
    request = dispatcher.pending[-1][0]
    assert request.active
    assert dispatcher.pending[-1][4] == "cam2"
    assert dispatcher.pending[-1][3]["mapping_name"] == mapping
    assert dispatcher.pending[-1][3]["observation_plane"] == "Intermediate"
    assert busy[-1][1] is True

    measurement = ImageMeasurement(
        image=np.zeros((64,64),dtype=np.float64),
        source="cam",
        detector="cam",
    )
    dispatcher.complete(measurement)
    assert busy[-1][1] is False
    committed = session.calibration.target_state("sec_0").measurement
    assert committed.pixel_size_um == pytest.approx(0.2)


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


def _observation_store(tmp_path):
    store = ObservationPlaneStore(tmp_path/"observations")
    store.add(ObservationPlaneDefinition(
        name="Main",preferred_detector="MainCam",default_pixel_size_um=0.4,
    ))
    store.add(ObservationPlaneDefinition(
        name="Intermediate",preferred_detector="IntermediateCam",
        default_pixel_size_um=0.2,
    ))
    return store


def _save_observation_geometry(store,runtime,plane,orientation="identity"):
    store.save(GeometryOrientationCalibration(
        slm_serial=runtime.identity.serial_number,
        section_key="sec_0",
        observation_plane_name=plane,
        orientation=orientation,
        detector_name=plane + "Cam",
        grid_x=5,grid_y=4,
        period_x_px=12.0,period_y_px=14.0,
        score=0.99,confidence_margin=0.3,matched_count=20,
    ))


def _fake_target_localization():
    lattice = np.asarray([[0,1,0,1],[0,0,1,1]],dtype=np.int64)
    positions = np.asarray(
        [[10.0,20.0,10.0,20.0],[10.0,10.0,30.0,30.0]],
        dtype=np.float64,
    )
    return LocalizationResult(
        target_type="multi_foci_vector",
        target_params={},
        parameters={},
        lattice_indices=lattice,
        crop_coord=(0,64,0,64),
        cropped_image=np.zeros((64,64),dtype=np.float64),
        expected_positions_px=positions,
        measured_positions_px=positions,
        period_x_px=10.0,
        period_y_px=20.0,
        offset_x_px=0.0,
        offset_y_px=0.0,
        diagnostics={
            "matched_mask":(True,True,True,True),
            "rms_residual_px":0.0,
        },
    )


def test_mapping_calibration_observation_selection_is_independent_from_feedback_and_cgh(tmp_path):
    runtime = _runtime(with_cgh=True)
    mapping_store = MappingStore(tmp_path/"mappings")
    mapping = _mapping(mapping_store)
    calibration_store = SLMCalibrationStore(tmp_path/"calibrations")
    _save_calibration(calibration_store,runtime,mapping)
    observations = _observation_store(tmp_path)
    session = SLMSession(
        runtime=runtime,
        calibration_store=calibration_store,
        mapping_store=mapping_store,
        observation_plane_store=observations,
    )
    session.calibration.select_mapping(
        session.calibration.prepare_mapping_selection("sec_0",mapping)
    )
    session.feedback.select_observation_plane("sec_0","Main")
    job = runtime.prepare_section_base_cgh("sec_0")
    runtime.commit_section_cgh(
        "sec_0",
        CGHResult(
            generation=job.generation,spec=job.spec,target_name=job.target_name,
            pattern=np.ones(job.spec.context.shape,dtype=np.complex128),
        ),
    )
    assert runtime.get_section_cgh_status("sec_0").result_state is CGHResultState.CURRENT

    session.calibration.select_target_observation_plane("sec_0","Intermediate")

    assert session.calibration.target_observation_plane("sec_0") == "Intermediate"
    assert session.feedback.active_observation_plane("sec_0") == "Main"
    assert session.calibration.active_mapping("sec_0") == mapping
    assert runtime.get_section_cgh_status("sec_0").result_state is CGHResultState.CURRENT


def test_target_mapping_calibration_uses_observation_scale_without_geometry_dependency(
    tmp_path,monkeypatch,
):
    runtime = _runtime(with_cgh=True)
    mapping_store = MappingStore(tmp_path/"mappings")
    mapping = _mapping(mapping_store)
    calibration_store = SLMCalibrationStore(tmp_path/"calibrations")
    observations = _observation_store(tmp_path)
    geometry_store = SLMGeometryOrientationCalibrationStore(tmp_path/"geometry")
    session = SLMSession(
        runtime=runtime,
        calibration_store=calibration_store,
        mapping_store=mapping_store,
        observation_plane_store=observations,
        geometry_orientation_calibration_store=geometry_store,
    )
    session.calibration.select_mapping(
        session.calibration.prepare_mapping_selection("sec_0",mapping)
    )
    monkeypatch.setattr(
        "slmcore.application.calibration.localize_measurement",
        lambda *_args,**_kwargs:_fake_target_localization(),
    )

    session.calibration.select_target_observation_plane("sec_0","Intermediate")
    session.calibration.ensure_target_reference("sec_0")
    measurement = ImageMeasurement(
        np.zeros((64,64),dtype=np.float64),source="camera",detector="IntermediateCam",
    )
    state = session.calibration.commit_target_measurement(
        "sec_0",measurement,source="IntermediateCam",
    )
    assert state.measurement.pixel_size_um == pytest.approx(0.2)
    candidate = session.calibration.run_target_localization(
        "sec_0",state.localization_parameters,
    )
    assert candidate.calibration.mapping_name == mapping
    assert candidate.calibration.metadata["observation_plane"] == "Intermediate"
    assert candidate.calibration.metadata["observation_source"] == "IntermediateCam"
    assert candidate.calibration.metadata["pixel_size_um"] == pytest.approx(0.2)
    assert "geometry_calibration" not in candidate.calibration.metadata
    saved = session.calibration.apply_target_candidate("sec_0",mapping)
    assert saved.mapping_name == mapping
    assert mapping_store.names == (mapping,)

    session.calibration.discard_target_state("sec_0")
    session.calibration.select_target_observation_plane("sec_0","Main")
    session.calibration.ensure_target_reference("sec_0")
    actual_scale = ImageMeasurement(
        np.zeros((64,64),dtype=np.float64),source="external",
        detector="OtherCam",pixel_size_um=0.3,
    )
    state = session.calibration.commit_target_measurement(
        "sec_0",actual_scale,source="OtherCam",
    )
    assert state.measurement.pixel_size_um == pytest.approx(0.3)
    candidate = session.calibration.run_target_localization(
        "sec_0",state.localization_parameters,
    )
    assert candidate.calibration.mapping_name == mapping
    assert candidate.calibration.metadata["observation_plane"] == "Main"
    assert candidate.calibration.metadata["observation_source"] == "OtherCam"
    assert candidate.calibration.metadata["pixel_size_um"] == pytest.approx(0.3)
    assert "geometry_calibration" not in candidate.calibration.metadata
    # Base target calibration is period-based and does not require or reinterpret
    # observation geometry.
    assert candidate.fitted_period_x_px == pytest.approx(10.0)
    assert candidate.fitted_period_y_px == pytest.approx(20.0)
    saved = session.calibration.apply_target_candidate("sec_0",mapping)
    assert saved.mapping_name == mapping
    assert mapping_store.names == (mapping,)
    loaded = calibration_store.load_calibration(runtime.identity,"sec_0",mapping)
    assert loaded.metadata["observation_plane"] == "Main"


def test_target_mapping_calibration_does_not_require_geometry_for_selected_observation(tmp_path,monkeypatch):
    runtime = _runtime(with_cgh=True)
    mapping_store = MappingStore(tmp_path/"mappings")
    mapping = _mapping(mapping_store)
    observations = ObservationPlaneStore(tmp_path/"observations")
    observations.add(ObservationPlaneDefinition(
        name="Intermediate",default_pixel_size_um=0.2,
    ))
    session = SLMSession(
        runtime=runtime,
        calibration_store=SLMCalibrationStore(tmp_path/"calibrations"),
        mapping_store=mapping_store,
        observation_plane_store=observations,
        geometry_orientation_calibration_store=(
            SLMGeometryOrientationCalibrationStore(tmp_path/"geometry")
        ),
    )
    session.calibration.select_mapping(
        session.calibration.prepare_mapping_selection("sec_0",mapping)
    )
    state = session.calibration.ensure_target_reference("sec_0")
    session.calibration.commit_target_measurement(
        "sec_0",
        ImageMeasurement(np.zeros((64,64)),source="file"),
        source="file",
    )
    monkeypatch.setattr(
        "slmcore.application.calibration.localize_measurement",
        lambda *_args,**_kwargs:_fake_target_localization(),
    )
    candidate = session.calibration.run_target_localization(
        "sec_0",state.localization_parameters,
    )
    assert candidate.calibration.mapping_name == mapping
    assert candidate.calibration.metadata["observation_plane"] == "Intermediate"
    assert "geometry_calibration" not in candidate.calibration.metadata


def test_target_measurement_pixel_size_uses_workflow_override_after_plane_fallback(tmp_path):
    runtime = _runtime(with_cgh=True)
    mapping_store = MappingStore(tmp_path/"mappings_override")
    mapping = _mapping(mapping_store)
    observations = ObservationPlaneStore(tmp_path/"observations_override")
    observations.add(ObservationPlaneDefinition(name="External"))
    session = SLMSession(
        runtime=runtime,
        calibration_store=SLMCalibrationStore(tmp_path/"calibrations_override"),
        mapping_store=mapping_store,
        observation_plane_store=observations,
    )
    session.calibration.select_mapping(
        session.calibration.prepare_mapping_selection("sec_0",mapping)
    )
    session.calibration.ensure_target_reference("sec_0")
    state = session.calibration.commit_target_measurement(
        "sec_0",
        ImageMeasurement(np.zeros((64,64)),source="file"),
        source="file",
        pixel_size_um=0.75,
    )
    assert state.observation_plane_name == "External"
    assert state.source == "file"
    assert state.measurement.pixel_size_um == pytest.approx(0.75)


def test_uncalibrated_mapping_can_keep_dormant_metric_target_and_bootstrap_reference(tmp_path):
    runtime = _runtime()
    mapping_store = MappingStore(tmp_path)
    store = SLMCalibrationStore(tmp_path/"calibrations")
    calibrated = mapping_store.add({"name":"calibrated"}).name
    uncalibrated = mapping_store.add({"name":"new"}).name
    _save_calibration(store,runtime,calibrated)
    session = SLMSession(
        runtime=runtime,calibration_store=store,mapping_store=mapping_store,
    )
    session.calibration.select_mapping(
        session.calibration.prepare_mapping_selection("sec_0",calibrated)
    )
    runtime.apply_section_patch(
        "sec_0",
        {
            ("cgh","active"):True,
            ("cgh","selected_target"):"multi_foci_vector",
            ("cgh","multi_foci_vector","params","square"):True,
            ("cgh","multi_foci_vector","params","square_unit"):"metric",
        },
    )
    before_x = runtime.get_section_parameter(
        "sec_0",("cgh","multi_foci_vector","params","period_x_px"),
    )
    before_y = runtime.get_section_parameter(
        "sec_0",("cgh","multi_foci_vector","params","period_y_px"),
    )

    assert session.calibration.select_mapping(
        session.calibration.prepare_mapping_selection("sec_0",uncalibrated)
    )
    assert runtime.get_section_calibration_copy("sec_0") is None
    assert runtime.get_section_parameter(
        "sec_0",("cgh","multi_foci_vector","params","period_x_px"),
    ) == before_x
    assert runtime.get_section_parameter(
        "sec_0",("cgh","multi_foci_vector","params","period_y_px"),
    ) == before_y

    state = session.calibration.ensure_target_reference("sec_0")
    assert state.reference is not None
    assert state.reference.target_type == "multi_foci_vector"


def test_runtime_replacement_drops_embedded_calibration_for_missing_mapping(tmp_path):
    runtime = _runtime()
    mapping_store = MappingStore(tmp_path)
    store = SLMCalibrationStore(tmp_path/"calibrations")
    warnings = []
    session = SLMSession(
        runtime=runtime,
        calibration_store=store,
        mapping_store=mapping_store,
        callbacks=SLMSessionCallbacks(
            on_warning=lambda title,message:warnings.append((title,str(message))),
        ),
    )

    replacement = _runtime()
    replacement.set_section_calibration(
        "sec_0",
        SLMSectionCalibration(
            mapping_name="deleted_mapping",
            kx_per_um=0.01,
            ky_per_um=0.02,
            section_geometry=section_geometry_to_dict(
                replacement.get_section_geometry("sec_0")
            ),
        ),
    )

    session.replace_runtime(replacement)

    assert replacement.get_section_calibration_copy("sec_0") is None
    assert session.calibration.active_mapping("sec_0") is None
    assert any("deleted_mapping" in message for _title,message in warnings)


def test_broken_mapping_calibration_does_not_block_mapping_deletion(tmp_path):
    runtime = _runtime()
    mapping_store = MappingStore(tmp_path)
    store = SLMCalibrationStore(tmp_path/"calibrations")
    mapping = mapping_store.add({"name":"broken"}).name
    path = store.directory/runtime.identity.serial_number/"sec_0"/"broken.json"
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text('{"version": 1, "calibration": {}}\n',encoding="utf-8")
    session = SLMSession(
        runtime=runtime,calibration_store=store,mapping_store=mapping_store,
    )

    with pytest.raises(ValueError,match="Unsupported mapping-calibration store version"):
        session.calibration.saved_calibration("sec_0",mapping)

    deleted = session.calibration.delete_mapping(mapping)

    assert str(path) in deleted
    assert not path.exists()
    assert not mapping_store.has(mapping)
