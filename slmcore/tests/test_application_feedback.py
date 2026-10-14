import numpy as np

from slmcore import (
    DEFAULT_REGISTRIES,ImageMeasurement,SLMGeometry,SLMIdentity,SLMRuntime,
    SLMSectionCalibration,SLMSession,SLMSessionCallbacks,
)
from slmcore.core.cgh import CGHResult
from slmcore.core.engine.section import split_slm_geometry
from slmcore.host import SLMDeviceProvider,SLMHostServices
from slmcore.workspace import ObservationPlaneDefinition,ObservationPlaneStore


def _set_feedback_observation_planes(session,*names):
    import tempfile
    store=ObservationPlaneStore(tempfile.mkdtemp(prefix="slmcore-observation-test-"))
    for name in names:
        store.add(ObservationPlaneDefinition(name=name))
    session.observation_planes.store=store
    if names:
        session.feedback.select_observation_plane("sec_0",names[0])
    return store


def _runtime(*,serial="SER123"):
    geometry = SLMGeometry(width=64,height=64,pixel_size_um=1.0)
    runtime = SLMRuntime(
        identity=SLMIdentity("slm",serial),
        geometry=geometry,
        section_geometries=split_slm_geometry(geometry,1),
        registries=DEFAULT_REGISTRIES,
    )
    runtime.set_section_calibration(
        "sec_0",SLMSectionCalibration(
            kx_per_um=0.01,ky_per_um=0.012,mapping_name="sample",
        ),
    )
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
    pattern = np.ones(job.spec.context.shape,dtype=np.complex128)
    runtime.commit_section_cgh(
        "sec_0",
        CGHResult(
            generation=job.generation,
            spec=job.spec,
            target_name=job.target_name,
            pattern=pattern,
        ),
    )
    return runtime


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
        return ("cam",)

    def preferred_source(self,section_key,available):
        return available[0] if available else None

    def acquire(self,section_key,source,*,metadata,on_result,on_error):
        request = _Request()
        self.pending.append((request,on_result,on_error,metadata))
        return request

    def complete(self,measurement,index=-1):
        request,on_result,_on_error,_metadata = self.pending[index]
        request._active = False
        on_result(measurement)


def test_feedback_service_commits_measurement_without_qt():
    runtime = _runtime()
    section_refreshes = []
    feedback_refreshes = []
    session = SLMSession(
        runtime=runtime,
        callbacks=SLMSessionCallbacks(
            on_section_refresh_requested=section_refreshes.append,
            on_feedback_state_changed=feedback_refreshes.append,
        ),
    )
    measurement = ImageMeasurement(
        image=np.zeros((64,64),dtype=np.float64),source="test",
    )

    previous = session.feedback.commit_measurement("sec_0",measurement)

    assert previous is False
    inspection = runtime.get_section_feedback_inspection("sec_0")
    assert inspection.measurement is not None
    assert inspection.measurement.acquisition.measurement_id == measurement.measurement_id
    assert section_refreshes == []
    assert feedback_refreshes == ["sec_0"]


def test_measurement_context_changes_use_feedback_state_refresh_and_noop_params_do_not_refresh(tmp_path):
    runtime=_runtime()
    store=ObservationPlaneStore(tmp_path / "observations")
    store.add(ObservationPlaneDefinition(name="Main",preferred_detector="cam"))
    store.add(ObservationPlaneDefinition(name="Intermediate"))
    section_refreshes=[]
    feedback_refreshes=[]
    session=SLMSession(
        runtime=runtime,
        observation_plane_store=store,
        callbacks=SLMSessionCallbacks(
            on_section_refresh_requested=section_refreshes.append,
            on_feedback_state_changed=feedback_refreshes.append,
        ),
    )

    assert session.feedback.select_observation_plane("sec_0","Main")
    assert section_refreshes == []
    assert feedback_refreshes == ["sec_0"]

    feedback_refreshes.clear()
    assert session.feedback.set_source_override("sec_0","other_cam")
    assert section_refreshes == []
    assert feedback_refreshes == ["sec_0"]

    feedback_refreshes.clear()
    result=session.feedback.update_parameters("sec_0","localization",{})
    assert result.changed is False
    assert section_refreshes == []
    assert feedback_refreshes == []
    session.dispose()


def test_feedback_measurement_dispatch_is_host_neutral_and_cancellable():
    runtime = _runtime()
    dispatcher = _Dispatcher()
    busy = []
    session = SLMSession(
        runtime=runtime,
        measurement_dispatcher=dispatcher,
        callbacks=SLMSessionCallbacks(
            on_feedback_measurement_busy_changed=(
                lambda key,value,message:busy.append((key,value,message))
            ),
        ),
    )
    measurement = ImageMeasurement(
        image=np.zeros((64,64),dtype=np.float64),source="cam",
    )

    session.feedback.acquire("sec_0","cam")
    request = dispatcher.pending[-1][0]
    assert request.active
    assert busy[-1][1] is True

    dispatcher.complete(measurement)
    assert busy[-1][1] is False
    assert runtime.get_section_feedback_inspection("sec_0").measurement is not None

    session.feedback.acquire("sec_0","cam")
    request = dispatcher.pending[-1][0]
    session.feedback.prepare_runtime_change()
    assert request.cancelled


def test_automatic_feedback_capability_belongs_to_application_service():
    runtime = _runtime()
    dispatcher = _Dispatcher()
    session = SLMSession(
        runtime=runtime,
        measurement_dispatcher=dispatcher,
        host_services=SLMHostServices(
            device=SLMDeviceProvider(upload_frame=lambda _frame:None),
        ),
    )

    assert session.can_run_automatic_feedback
    session.set_auto_upload_frame(False)
    assert not session.can_run_automatic_feedback
    assert "auto_upload_frame=False" in (
        session.feedback.automatic_feedback_unavailable_reason
    )


def test_feedback_orientation_comes_only_from_saved_geometry(tmp_path):
    from slmcore.application.startup_preferences import StartupPreferencesState
    from slmcore.setup import SLMStartupPreferences
    from slmcore.workspace import SLMGeometryOrientationCalibrationStore

    runtime = _runtime()
    preferences = StartupPreferencesState(
        SLMStartupPreferences(feedback_orientations={
            "sec_0":{
                "default":"flip_vertical",
                "planes":{"sample":"flip_horizontal"},
            },
        }),
        lambda _value:None,
    )
    session = SLMSession(
        runtime=runtime,
        startup_preferences=preferences,
        geometry_orientation_calibration_store=(
            SLMGeometryOrientationCalibrationStore(tmp_path/"geometry")
        ),
    )
    _set_feedback_observation_planes(session,"sample","fourier")

    session.feedback.select_observation_plane("sec_0","sample")
    # Legacy startup orientation preferences no longer act as a feedback override.
    assert session.feedback.feedback_orientation("sec_0").value == "identity"

    session.geometry_calibration.save_manual(
        "sec_0","sample","rotate_90_cw",
    )
    session.geometry_calibration.save_manual(
        "sec_0","fourier","flip_horizontal",
    )
    assert session.feedback.feedback_orientation("sec_0").value == "rotate_90_cw"

    session.feedback.select_observation_plane("sec_0","fourier")
    assert session.feedback.feedback_orientation("sec_0").value == "flip_horizontal"


def test_feedback_exposes_geometry_as_read_only_context():
    runtime = _runtime()
    session = SLMSession(runtime=runtime)

    assert hasattr(session.feedback,"geometry_orientation_calibration")
    assert hasattr(session.feedback,"feedback_orientation")
    assert not hasattr(session.feedback,"set_feedback_orientation")
    assert not hasattr(session.feedback,"save_feedback_orientation")
    assert not hasattr(session.feedback,"feedback_orientation_context")


def _attach_reference_localization(
    runtime,section_key="sec_0",*,image_shape=(96,112),detector="cam",
    pixel_size_um=None,crop_coord=(8,72,16,80),measured_shift=(1.0,2.0),
):
    from slmcore.core.cgh.localization import LocalizationResult

    section = runtime._get_section(section_key)
    resolution = section._cgh_session.create_target_resolution(
        section.state.cgh,section._build_context(section.state),
    )
    count = int(resolution.lattice_indices.shape[1])
    image = np.zeros(tuple(image_shape),dtype=np.float64)
    measurement = ImageMeasurement(
        image=image,source="cam",detector=detector,pixel_size_um=pixel_size_um,
    )
    runtime.set_section_feedback_measurement(section_key,measurement)
    parameters = dict(
        runtime.get_section_feedback_status(section_key).localization_params
    )
    expected = np.array([
        [12.0,44.0,12.0,44.0],
        [12.0,12.0,44.0,44.0],
    ],dtype=np.float64)[:,:count]
    measured = expected + np.asarray(measured_shift,dtype=np.float64).reshape(2,1)
    y1,y2,x1,x2=tuple(int(value) for value in crop_coord)
    localization = LocalizationResult(
        target_type="multi_foci_vector",
        target_params=dict(resolution.canonical_params),
        parameters=parameters,
        lattice_indices=resolution.lattice_indices,
        crop_coord=(y1,y2,x1,x2),
        cropped_image=np.zeros((y2-y1,x2-x1),dtype=np.float64),
        expected_positions_px=expected,
        measured_positions_px=measured,
        period_x_px=32.0,
        period_y_px=32.0,
        offset_x_px=12.0,
        offset_y_px=12.0,
        diagnostics={
            "measurement_id":measurement.measurement_id,
            "matched_mask":tuple(True for _ in range(count)),
            "matched_count":count,
            "missing_count":0,
        },
    )
    runtime.commit_section_feedback_localization(
        section_key,localization,parameters,
    )
    return localization


def test_position_reference_accepts_inferred_but_not_unresolved_sites(tmp_path):
    from dataclasses import replace

    import pytest

    from slmcore import PositionReferenceStore
    from slmcore.workspace import SLMGeometryOrientationCalibrationStore
    from slmcore.core.cgh.localization import infer_missing_localization

    store = PositionReferenceStore(tmp_path / "position_references")
    runtime = _runtime()
    session = SLMSession(runtime=runtime,position_reference_store=store)
    _set_feedback_observation_planes(session,"sample")
    full = _attach_reference_localization(runtime)
    diagnostics = dict(full.diagnostics)
    diagnostics.update({
        "matched_mask":(True,False,True,True),
        "matched_count":3,
        "inferred_count":0,
        "missing_count":1,
    })
    partial = replace(full,diagnostics=diagnostics)
    params = dict(runtime.get_section_feedback_status("sec_0").localization_params)
    runtime.commit_section_feedback_localization("sec_0",partial,params)

    context = session.feedback.position_reference_context("sec_0")
    assert not context.create_allowed
    assert "Infer missing" in context.create_unavailable_reason
    with pytest.raises(RuntimeError,match="Infer missing"):
        session.feedback.add_position_reference("sec_0","partial")

    inferred = infer_missing_localization(partial)
    runtime.commit_section_feedback_localization("sec_0",inferred,params)
    context = session.feedback.position_reference_context("sec_0")
    assert context.create_allowed
    reference = session.feedback.add_position_reference("sec_0","resolved")
    assert reference.metadata["matched_count"] == 3
    assert reference.metadata["inferred_count"] == 1
    np.testing.assert_allclose(
        reference.positions_px[:,1],
        inferred.expected_positions_px[:,1] + np.asarray(inferred.crop_coord)[[2,0]],
    )
    session.dispose()


def test_named_session_reference_is_shared_but_selection_is_transient(tmp_path):
    from slmcore import (
        PositionReferenceLifetime,PositionReferenceMode,PositionReferenceStore,
    )

    store = PositionReferenceStore(tmp_path / "position_references")
    runtime_a = _runtime(serial="SER-A")
    session_a = SLMSession(
        runtime=runtime_a,
        position_reference_store=store,
    )
    _set_feedback_observation_planes(session_a,"sample")
    localization = _attach_reference_localization(runtime_a)

    reference = session_a.feedback.add_position_reference(
        "sec_0","aligned OFF",
    )
    assert reference.metadata["source_slm_serial"] == "SER-A"
    np.testing.assert_allclose(
        reference.positions_px,
        localization.measured_positions_px + np.array([[16.0],[8.0]]),
    )
    assert store.get_record(
        "sample","aligned OFF"
    ).lifetime is PositionReferenceLifetime.SESSION
    assert session_a.feedback.position_reference_selection(
        "sec_0"
    ).mode is PositionReferenceMode.REFERENCE

    # A different SLM session sharing the same live store sees the reference,
    # but it does not inherit the other session's active selection.
    runtime_b = _runtime(serial="SER-B")
    session_b = SLMSession(
        runtime=runtime_b,
        position_reference_store=store,
    )
    _set_feedback_observation_planes(session_b,"sample")
    context = session_b.feedback.position_reference_context("sec_0")
    assert tuple(record.name for record in context.references) == ("aligned OFF",)
    assert context.selection.mode is PositionReferenceMode.GLOBAL_FIT

    selection = session_b.feedback.set_position_reference(
        "sec_0",mode="reference",reference_name="aligned OFF",
    )
    assert selection.mode is PositionReferenceMode.REFERENCE
    # Selecting a named reference before acquiring/localizing is valid; actual
    # lattice/image compatibility is checked as soon as localization exists.
    assert session_b.feedback.position_reference_context("sec_0").compatible

    session_b.feedback.persist_position_reference("sec_0","aligned OFF")
    assert store.get_record(
        "sample","aligned OFF"
    ).lifetime is PositionReferenceLifetime.PERSISTENT
    assert len(store.list("sample")) == 1

    session_a.dispose()
    session_b.dispose()

def test_position_reference_workflow_remains_available_with_stored_correction(tmp_path):
    from dataclasses import replace

    from slmcore import PositionReferenceMode,PositionReferenceStore

    store = PositionReferenceStore(tmp_path / "position_references")
    runtime = _runtime()
    session = SLMSession(runtime=runtime,position_reference_store=store)
    _set_feedback_observation_planes(session,"sample")
    _attach_reference_localization(runtime)
    session.feedback.add_position_reference("sec_0","first reference")

    original_status = runtime.get_section_feedback_status
    runtime.get_section_feedback_status = lambda section:replace(
        original_status(section),position_available=True,position_active=True,
    )
    try:
        context = session.feedback.position_reference_context("sec_0")
        assert context.change_allowed
        assert context.create_allowed

        selection = session.feedback.set_position_reference(
            "sec_0",mode=PositionReferenceMode.GLOBAL_FIT,
        )
        assert selection.mode is PositionReferenceMode.GLOBAL_FIT
        selection = session.feedback.set_position_reference(
            "sec_0",mode=PositionReferenceMode.REFERENCE,
            reference_name="first reference",
        )
        assert selection.reference_name == "first reference"

        saved = session.feedback.add_position_reference(
            "sec_0","corrected localization",
        )
        assert saved.name == "corrected localization"
        assert session.feedback.position_reference_selection(
            "sec_0"
        ).reference_name == "corrected localization"
    finally:
        runtime.get_section_feedback_status = original_status
        session.dispose()


def test_reference_source_updates_preserve_correction_model_state(tmp_path):
    from slmcore import PositionReferenceMode,PositionReferenceStore

    store=PositionReferenceStore(tmp_path / "position_references")
    runtime=_runtime()
    session=SLMSession(runtime=runtime,position_reference_store=store)
    _set_feedback_observation_planes(session,"sample")
    from dataclasses import replace
    localization=_attach_reference_localization(runtime)
    diagnostics=dict(localization.diagnostics)
    diagnostics.update({
        "affine_linear":((32.0,0.0),(0.0,32.0)),
        "affine_translation":(12.0,12.0),
    })
    localization=replace(localization,diagnostics=diagnostics)
    parameters=dict(runtime.get_section_feedback_status("sec_0").localization_params)
    runtime.commit_section_feedback_localization("sec_0",localization,parameters)

    selection=session.feedback.set_position_reference(
        "sec_0",mode=PositionReferenceMode.EDITABLE,
        correction_mode="polynomial",polynomial_degree=3,
    )
    assert selection.correction_mode=="polynomial"
    assert selection.polynomial_degree==3

    selection=session.feedback.fit_position_reference_center("sec_0")
    assert selection.mode is PositionReferenceMode.EDITABLE
    assert selection.correction_mode=="polynomial"
    assert selection.polynomial_degree==3

    session.feedback.add_position_reference("sec_0","new reference")
    selection=session.feedback.position_reference_selection("sec_0")
    assert selection.mode is PositionReferenceMode.REFERENCE
    assert selection.correction_mode=="polynomial"
    assert selection.polynomial_degree==3

    session.feedback.delete_position_reference("sec_0","new reference")
    selection=session.feedback.position_reference_selection("sec_0")
    assert selection.mode is PositionReferenceMode.GLOBAL_FIT
    assert selection.correction_mode=="polynomial"
    assert selection.polynomial_degree==3
    assert selection.correction.manual_transform is None

    session.dispose()


def test_named_reference_reindexes_source_orientation_for_destination_section(tmp_path):
    from slmcore import PositionReferenceStore
    from slmcore.core.cgh.feedback import orient_localization
    from slmcore.workspace import SLMGeometryOrientationCalibrationStore

    store = PositionReferenceStore(tmp_path / "position_references")

    runtime_a = _runtime(serial="SER-A")
    session_a = SLMSession(
        runtime=runtime_a,position_reference_store=store,
        geometry_orientation_calibration_store=(
            SLMGeometryOrientationCalibrationStore(tmp_path / "geometry_a")
        ),
    )
    _set_feedback_observation_planes(session_a,"sample")
    localization_a = _attach_reference_localization(runtime_a)
    session_a.geometry_calibration.save_manual(
        "sec_0","sample","rotate_180",
    )
    reference = session_a.feedback.add_position_reference("sec_0","488 reference")
    assert reference.metadata["feedback_orientation"] == "rotate_180"

    runtime_b = _runtime(serial="SER-B")
    session_b = SLMSession(
        runtime=runtime_b,position_reference_store=store,
        geometry_orientation_calibration_store=(
            SLMGeometryOrientationCalibrationStore(tmp_path / "geometry_b")
        ),
    )
    _set_feedback_observation_planes(session_b,"sample")
    localization_b = _attach_reference_localization(runtime_b)
    session_b.geometry_calibration.save_manual(
        "sec_0","sample","flip_vertical",
    )
    session_b.feedback.set_position_reference(
        "sec_0",mode="reference",reference_name="488 reference",
    )

    positions,metadata = session_b.feedback._resolve_position_reference("sec_0")
    assert metadata["name"] == "488 reference"
    expected = orient_localization(localization_b,"flip_vertical")
    np.testing.assert_allclose(positions,expected.measured_positions_px)

    session_a.dispose()
    session_b.dispose()


def _fov_calibration_for_runtime(runtime,name,displacement=(0.01,-0.02)):
    from slmcore import FOVMappingCorrection
    from slmcore.core.calibration import base_mapping_signature

    section = runtime._get_section("sec_0")
    resolution = section._cgh_session.create_target_resolution(
        section.state.cgh,section._build_context(section.state),
    )
    ideal = np.asarray(resolution.ideal_spot_positions_kxy,dtype=np.float64)
    delta = np.repeat(
        np.asarray(displacement,dtype=np.float64).reshape(2,1),
        ideal.shape[1],axis=1,
    )
    return FOVMappingCorrection.fit(
        name=name,
        slm_serial=runtime.identity.serial_number,
        section_key="sec_0",
        mapping_name="sample",base_mapping_signature=base_mapping_signature(
            runtime.get_section_calibration_copy("sec_0")
        ),
        ideal_positions_kxy=ideal,
        total_displacements_kxy=delta,
        degree=1,
    )


def test_fov_default_is_mapping_scoped_and_apply_off_is_temporary(tmp_path):
    from slmcore import SLMStartupPreferences,SLMWorkspace
    from slmcore.application.startup_preferences import StartupPreferencesState

    workspace = SLMWorkspace(tmp_path)
    runtime = _runtime()
    calibration = _fov_calibration_for_runtime(runtime,"field A")
    workspace.fov_mapping_correction_store.save(calibration)
    saved = []
    preferences = StartupPreferencesState(
        SLMStartupPreferences(fov_mapping_corrections={
            "sec_0":{"mappings":{"sample":"field A"}},
        }),
        saved.append,
    )
    session = SLMSession(
        runtime=runtime,
        fov_mapping_correction_store=workspace.fov_mapping_correction_store,
        startup_preferences=preferences,
    )
    session.calibration.active_mapping = lambda _section:"sample"

    session.fov_mapping_runtime.apply_startup_fov_mapping_defaults()
    context = session.fov_mapping_runtime.fov_mapping_context("sec_0")
    assert context["selected_name"] == "field A"
    assert context["applied"] is True
    assert runtime.get_section_fov_mapping_context("sec_0")["correction"].name == "field A"

    session.fov_mapping_runtime.set_fov_mapping_correction_applied("sec_0",False)
    context = session.fov_mapping_runtime.fov_mapping_context("sec_0")
    assert context["applied"] is False
    assert context["default_name"] == "field A"
    assert runtime.get_section_fov_mapping_context("sec_0")["correction"] is None
    assert saved == []

    session.dispose()


def test_fov_management_selection_does_not_change_applied_default(tmp_path):
    from slmcore import SLMStartupPreferences,SLMWorkspace
    from slmcore.application.startup_preferences import StartupPreferencesState

    workspace = SLMWorkspace(tmp_path)
    runtime = _runtime()
    first = _fov_calibration_for_runtime(runtime,"field A",(0.01,-0.02))
    second = _fov_calibration_for_runtime(runtime,"field B",(-0.03,0.04))
    store = workspace.fov_mapping_correction_store
    store.save(first); store.save(second)
    saved = []
    preferences = StartupPreferencesState(SLMStartupPreferences(),saved.append)
    session = SLMSession(
        runtime=runtime,fov_mapping_correction_store=store,
        startup_preferences=preferences,
    )
    session.calibration.active_mapping = lambda _section:"sample"
    session.fov_mapping_runtime.refresh_fov_mapping_contexts()

    session.fov_mapping_runtime.select_fov_mapping_correction("sec_0","field A")
    session.fov_mapping_runtime.set_default_fov_mapping_correction("sec_0")
    assert saved[-1].fov_mapping_corrections["sec_0"].mappings["sample"] == "field A"

    session.fov_mapping_runtime.select_fov_mapping_correction("sec_0","field B")
    context = session.fov_mapping_runtime.fov_mapping_context("sec_0")
    assert context["applied"] is True
    assert context["selected_name"] == "field B"
    assert context["default_name"] == "field A"
    assert runtime.get_section_fov_mapping_context("sec_0")["correction"].name == "field A"

    session.dispose()


def test_switching_default_fov_calibration_marks_position_feedback_stale(tmp_path):
    from slmcore import SLMStartupPreferences,SLMWorkspace
    from slmcore.application.startup_preferences import StartupPreferencesState

    workspace = SLMWorkspace(tmp_path)
    runtime = _runtime()
    store = workspace.fov_mapping_correction_store
    first = _fov_calibration_for_runtime(runtime,"field A")
    second = _fov_calibration_for_runtime(runtime,"field B",(0.02,0.01))
    store.save(first); store.save(second)
    preferences = StartupPreferencesState(SLMStartupPreferences(),lambda _value:None)
    session = SLMSession(runtime=runtime,fov_mapping_correction_store=store,startup_preferences=preferences)
    session.calibration.active_mapping = lambda _section:"sample"
    session.fov_mapping_runtime.refresh_fov_mapping_contexts()
    session.fov_mapping_runtime.select_fov_mapping_correction("sec_0","field A")
    session.fov_mapping_runtime.set_default_fov_mapping_correction("sec_0")

    _attach_reference_localization(runtime)
    session.feedback.apply_position_correction("sec_0")
    assert runtime.get_section_feedback_status("sec_0").position_active is True

    session.fov_mapping_runtime.select_fov_mapping_correction("sec_0","field B")
    session.fov_mapping_runtime.set_default_fov_mapping_correction("sec_0")
    status = runtime.get_section_feedback_status("sec_0")
    assert status.position_active is False
    assert status.position_available is True
    assert status.position_stale is True
    assert "FOV mapping correction changed" in status.position_stale_reason
    assert runtime.get_section_fov_mapping_context("sec_0")["correction"].name == "field B"

    session.dispose()


def test_mapping_switch_clears_previous_runtime_fov_when_new_mapping_has_no_default(tmp_path):
    from slmcore import FOVMappingCorrection,SLMStartupPreferences,SLMWorkspace
    from slmcore.application.startup_preferences import StartupPreferencesState

    workspace = SLMWorkspace(tmp_path)
    runtime = _runtime()
    calibration = _fov_calibration_for_runtime(runtime,"field A")
    workspace.fov_mapping_correction_store.save(calibration)
    preferences = StartupPreferencesState(SLMStartupPreferences(),lambda _value:None)
    session = SLMSession(
        runtime=runtime,
        fov_mapping_correction_store=workspace.fov_mapping_correction_store,
        startup_preferences=preferences,
    )
    active = {"plane":"sample"}
    session.calibration.active_mapping = lambda _section:active["plane"]
    session.fov_mapping_runtime.refresh_fov_mapping_contexts()
    session.fov_mapping_runtime.select_fov_mapping_correction("sec_0","field A")
    session.fov_mapping_runtime.set_default_fov_mapping_correction("sec_0")
    assert runtime.get_section_fov_mapping_context("sec_0")["correction"].name == "field A"

    active["plane"] = "other"
    session.fov_mapping_runtime.refresh_fov_mapping_contexts()
    assert runtime.get_section_fov_mapping_context("sec_0")["correction"] is None
    assert session.fov_mapping_runtime.fov_mapping_context("sec_0")["applied"] is False
    session.dispose()


def test_reading_fov_context_does_not_mutate_runtime_before_explicit_startup_apply(tmp_path):
    from slmcore import SLMStartupPreferences,SLMWorkspace
    from slmcore.application.startup_preferences import StartupPreferencesState

    workspace = SLMWorkspace(tmp_path)
    runtime = _runtime()
    calibration = _fov_calibration_for_runtime(runtime,"field A")
    workspace.fov_mapping_correction_store.save(calibration)
    preferences = StartupPreferencesState(
        SLMStartupPreferences(fov_mapping_corrections={
            "sec_0":{"mappings":{"sample":"field A"}},
        }),
        lambda _preferences:None,
    )
    transitions = []
    session = SLMSession(
        runtime=runtime,
        fov_mapping_correction_store=workspace.fov_mapping_correction_store,
        startup_preferences=preferences,
        callbacks=SLMSessionCallbacks(
            on_transition_committed=lambda key,transition:transitions.append((key,transition)),
        ),
    )
    session.calibration.active_mapping = lambda _section:"sample"

    context = session.fov_mapping_runtime.fov_mapping_context("sec_0")
    assert context["selected_name"] == "field A"
    assert context["applied"] is True
    assert runtime.get_section_fov_mapping_context("sec_0")["correction"] is None
    assert transitions == []

    session.fov_mapping_runtime.apply_startup_fov_mapping_defaults()
    assert runtime.get_section_fov_mapping_context("sec_0")["correction"].name == "field A"
    assert len(transitions) == 1
    session.dispose()


def test_delete_fov_calibration_clears_selection_runtime_and_default(tmp_path):
    from slmcore import SLMStartupPreferences,SLMWorkspace
    from slmcore.application.startup_preferences import StartupPreferencesState

    workspace = SLMWorkspace(tmp_path)
    runtime = _runtime()
    calibration = _fov_calibration_for_runtime(runtime,"field A")
    store = workspace.fov_mapping_correction_store
    store.save(calibration)
    saved = []
    preferences = StartupPreferencesState(SLMStartupPreferences(),saved.append)
    session = SLMSession(
        runtime=runtime,
        fov_mapping_correction_store=store,
        startup_preferences=preferences,
    )
    session.calibration.active_mapping = lambda _section:"sample"
    session.fov_mapping_runtime.refresh_fov_mapping_contexts()
    session.fov_mapping_runtime.select_fov_mapping_correction("sec_0","field A")
    session.fov_mapping_runtime.set_default_fov_mapping_correction("sec_0")
    assert preferences.default_fov_correction("sec_0","sample") == "field A"

    deleted = session.fov_mapping_runtime.delete_fov_mapping_correction("sec_0")

    assert deleted == "field A"
    assert not store.exists(runtime.identity,"sec_0","sample","field A")
    context = session.fov_mapping_runtime.fov_mapping_context("sec_0")
    assert context["selected_name"] is None
    assert context["applied"] is False
    assert context["default_name"] is None
    assert runtime.get_section_fov_mapping_context("sec_0")["correction"] is None
    assert saved[-1].fov_mapping_corrections == {}
    session.dispose()


def test_feedback_observation_plane_switch_does_not_change_mapping_fov_or_cgh(tmp_path):
    from slmcore import SLMStartupPreferences,SLMWorkspace
    from slmcore.application.startup_preferences import StartupPreferencesState

    runtime = _runtime()
    workspace = SLMWorkspace(tmp_path / "workspace")
    fov = _fov_calibration_for_runtime(runtime,"field A")
    workspace.fov_mapping_correction_store.save(fov)
    store = ObservationPlaneStore(tmp_path / "observations")
    store.add(ObservationPlaneDefinition(
        name="Main",preferred_detector="cam",default_pixel_size_um=0.1,
    ))
    store.add(ObservationPlaneDefinition(
        name="Intermediate",default_pixel_size_um=0.2,
    ))
    preferences = StartupPreferencesState(SLMStartupPreferences(),lambda _value:None)
    session = SLMSession(
        runtime=runtime,observation_plane_store=store,
        fov_mapping_correction_store=workspace.fov_mapping_correction_store,
        startup_preferences=preferences,
    )
    session.calibration.active_mapping = lambda _section:"sample"
    assert session.feedback.active_observation_plane("sec_0") is None
    assert session.feedback.select_observation_plane("sec_0","Main")

    session.fov_mapping_runtime.refresh_fov_mapping_contexts()
    session.fov_mapping_runtime.select_fov_mapping_correction("sec_0","field A")
    session.fov_mapping_runtime.set_default_fov_mapping_correction("sec_0")
    # Applying FOV intentionally stales the generated hologram. Recompute once so
    # the checkpoint can prove that observation switching itself does not stale it.
    job = runtime.prepare_section_base_cgh("sec_0")
    runtime.commit_section_cgh(
        "sec_0",CGHResult(
            generation=job.generation,spec=job.spec,target_name=job.target_name,
            pattern=np.ones(job.spec.context.shape,dtype=np.complex128),
        ),
    )
    session.feedback.commit_measurement(
        "sec_0",ImageMeasurement(
            image=np.zeros((64,64)),source="cam",detector="cam",pixel_size_um=0.1,
        ),
    )

    before_status = runtime.get_section_cgh_status("sec_0")
    before_result = runtime.get_section_cgh_result_copy("sec_0")
    before_calibration = runtime.get_section_calibration_copy("sec_0")
    before_fov = runtime.get_section_fov_mapping_context("sec_0")
    assert before_fov["correction"].name == "field A"
    assert runtime.get_section_feedback_inspection("sec_0").measurement is not None

    assert session.feedback.select_observation_plane("sec_0","Intermediate")

    assert session.feedback.active_observation_plane("sec_0") == "Intermediate"
    assert runtime.get_section_calibration_copy("sec_0") == before_calibration
    assert runtime.get_section_cgh_status("sec_0") == before_status
    np.testing.assert_array_equal(
        runtime.get_section_cgh_result_copy("sec_0").pattern,before_result.pattern,
    )
    after_fov = runtime.get_section_fov_mapping_context("sec_0")
    assert after_fov["correction"].name == "field A"
    np.testing.assert_allclose(
        after_fov["baseline_positions_kxy"],before_fov["baseline_positions_kxy"],
    )
    assert session.fov_mapping_runtime.fov_mapping_context("sec_0")["applied"] is True
    # Measurement/localization state belongs to the old observation context and is
    # intentionally invalidated without changing generated pattern state.
    assert runtime.get_section_feedback_inspection("sec_0").measurement is None


def test_multiple_observation_planes_require_explicit_feedback_selection(tmp_path):
    runtime = _runtime()
    store = ObservationPlaneStore(tmp_path)
    store.add(ObservationPlaneDefinition(name="Main"))
    store.add(ObservationPlaneDefinition(name="Intermediate"))
    session = SLMSession(runtime=runtime,observation_plane_store=store)

    assert session.feedback.active_observation_plane("sec_0") is None
    assert session.feedback.select_observation_plane("sec_0","Main")
    assert session.feedback.active_observation_plane("sec_0") == "Main"


def test_multiple_observation_planes_block_measurement_until_selected(tmp_path):
    import pytest

    runtime = _runtime()
    dispatcher = _Dispatcher()
    store = ObservationPlaneStore(tmp_path)
    store.add(ObservationPlaneDefinition(name="Main"))
    store.add(ObservationPlaneDefinition(name="Intermediate"))
    session = SLMSession(
        runtime=runtime,measurement_dispatcher=dispatcher,observation_plane_store=store,
    )

    with pytest.raises(RuntimeError,match="Select an observation plane"):
        session.feedback.acquire("sec_0","cam")
    assert dispatcher.pending == []

    session.feedback.select_observation_plane("sec_0","Main")
    session.feedback.acquire("sec_0","cam")
    assert len(dispatcher.pending) == 1


def test_feedback_plane_suggests_detector_but_override_does_not_switch_plane(tmp_path):
    runtime = _runtime()
    store = ObservationPlaneStore(tmp_path)
    store.add(ObservationPlaneDefinition(name="Main",preferred_detector="main_cam"))
    session = SLMSession(runtime=runtime,observation_plane_store=store)
    available = ("main_cam","other_cam")

    assert session.feedback.preferred_source("sec_0",available) == "main_cam"
    assert session.feedback.set_source_override("sec_0","other_cam")
    assert session.feedback.preferred_source("sec_0",available) == "other_cam"
    assert session.feedback.active_observation_plane("sec_0") == "Main"


def test_detector_measurement_uses_observation_plane_pixel_size_fallback(tmp_path):
    runtime = _runtime()
    dispatcher = _Dispatcher()
    store = ObservationPlaneStore(tmp_path)
    store.add(ObservationPlaneDefinition(name="Main",preferred_detector="cam",default_pixel_size_um=0.123))
    session = SLMSession(runtime=runtime,measurement_dispatcher=dispatcher,observation_plane_store=store)

    session.feedback.acquire("sec_0","cam")
    dispatcher.complete(ImageMeasurement(image=np.zeros((64,64)),source="cam",detector="cam"))
    committed = runtime.get_section_feedback_inspection("sec_0").measurement.acquisition
    assert committed.pixel_size_um == 0.123
    assert dispatcher.pending[-1][3]["observation_plane"] == "Main"


def test_external_measurement_pixel_size_overrides_observation_plane_fallback(tmp_path):
    runtime = _runtime()
    dispatcher = _Dispatcher()
    store = ObservationPlaneStore(tmp_path)
    store.add(ObservationPlaneDefinition(name="Main",default_pixel_size_um=0.123))
    session = SLMSession(runtime=runtime,measurement_dispatcher=dispatcher,observation_plane_store=store)

    session.feedback.acquire("sec_0","cam")
    dispatcher.complete(ImageMeasurement(image=np.zeros((64,64)),source="file",pixel_size_um=0.456))
    committed = runtime.get_section_feedback_inspection("sec_0").measurement.acquisition
    assert committed.pixel_size_um == 0.456


def test_recalibrating_base_mapping_keeps_fov_record_but_disables_apply(tmp_path):
    from slmcore import SLMSectionCalibration,SLMStartupPreferences,SLMWorkspace
    from slmcore.application.startup_preferences import StartupPreferencesState

    workspace = SLMWorkspace(tmp_path)
    runtime = _runtime()
    correction = _fov_calibration_for_runtime(runtime,"field A")
    workspace.fov_mapping_correction_store.save(correction)
    preferences = StartupPreferencesState(SLMStartupPreferences(),lambda _value:None)
    session = SLMSession(
        runtime=runtime,
        fov_mapping_correction_store=workspace.fov_mapping_correction_store,
        startup_preferences=preferences,
    )
    session.calibration.active_mapping = lambda _section:"sample"
    session.fov_mapping_runtime.select_fov_mapping_correction("sec_0","field A")
    session.fov_mapping_runtime.set_default_fov_mapping_correction("sec_0")
    assert session.fov_mapping_runtime.fov_mapping_context("sec_0")["applied"] is True

    runtime.set_section_calibration(
        "sec_0",SLMSectionCalibration(
            kx_per_um=0.02,ky_per_um=0.012,mapping_name="sample",
        ),
    )
    session.fov_mapping_runtime.refresh_fov_mapping_contexts()

    context = session.fov_mapping_runtime.fov_mapping_context("sec_0")
    assert context["default_name"] == "field A"
    assert context["default_compatible"] is False
    assert context["applied"] is False
    assert workspace.fov_mapping_correction_store.exists(
        runtime.identity,"sec_0","sample","field A"
    )
    assert runtime.get_section_fov_mapping_context("sec_0")["correction"] is None
    session.dispose()


def test_mapping_switch_uses_each_mappings_default_fov_state(tmp_path):
    from dataclasses import replace
    from slmcore import SLMStartupPreferences,SLMWorkspace
    from slmcore.application.startup_preferences import StartupPreferencesState

    workspace = SLMWorkspace(tmp_path)
    runtime = _runtime()
    first = _fov_calibration_for_runtime(runtime,"field A")
    second = replace(first,name="field B",mapping_name="other")
    workspace.fov_mapping_correction_store.save(first)
    workspace.fov_mapping_correction_store.save(second)
    preferences = StartupPreferencesState(
        SLMStartupPreferences(fov_mapping_corrections={
            "sec_0":{"mappings":{"sample":"field A","other":"field B"}},
        }),
        lambda _value:None,
    )
    active = {"mapping":"sample"}
    session = SLMSession(
        runtime=runtime,
        fov_mapping_correction_store=workspace.fov_mapping_correction_store,
        startup_preferences=preferences,
    )
    session.calibration.active_mapping = lambda _section:active["mapping"]
    session.fov_mapping_runtime.apply_startup_fov_mapping_defaults()
    assert runtime.get_section_fov_mapping_context("sec_0")["correction"].name == "field A"

    active["mapping"] = "other"
    session.fov_mapping_runtime.refresh_fov_mapping_contexts()
    context = session.fov_mapping_runtime.fov_mapping_context("sec_0")
    assert context["default_name"] == "field B"
    assert context["applied"] is True
    assert runtime.get_section_fov_mapping_context("sec_0")["correction"].name == "field B"
    session.dispose()


def test_config_restores_exact_named_fov_and_does_not_fallback_when_missing(tmp_path):
    from slmcore import (
        SLMConfigurationService,SLMDefinition,SLMRuntimeFactory,
        SLMSectionsDefinition,SLMStartupPreferences,SLMWorkspace,
    )
    from slmcore.application.startup_preferences import StartupPreferencesState
    from slmcore.core.engine.section import SectionSplitLayout

    runtime = _runtime()
    section_geometry = runtime.get_section_geometry("sec_0")
    runtime.set_section_calibration(
        "sec_0",
        SLMSectionCalibration(
            kx_per_um=0.01,
            ky_per_um=0.012,
            mapping_name="sample",
            section_geometry=vars(section_geometry),
        ),
    )
    # Recompute after adding the persisted calibration geometry so the saved
    # round belongs to the exact context that the config will restore.
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

    first = _fov_calibration_for_runtime(runtime,"field A",(0.01,-0.02))
    second = _fov_calibration_for_runtime(runtime,"field B",(-0.03,0.04))
    workspace = SLMWorkspace(tmp_path)
    workspace.fov_mapping_correction_store.save(first)
    workspace.fov_mapping_correction_store.save(second)

    runtime.set_section_fov_mapping_correction("sec_0",first)
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
    assert runtime.get_section_cgh_status("sec_0").result_state.value == "current"

    definition = SLMDefinition(
        identity=runtime.identity,
        geometry=runtime.geometry,
        sections=SLMSectionsDefinition(
            layout=SectionSplitLayout(n_sections=1,axis="x"),
            customizable=True,
        ),
    )
    factory = SLMRuntimeFactory(
        definition=definition,registries=DEFAULT_REGISTRIES,
    )
    config_store = workspace.config_store(runtime.identity,DEFAULT_REGISTRIES)
    configuration = SLMConfigurationService(
        store=config_store,runtime_factory=factory,
    )
    preferences = StartupPreferencesState(
        SLMStartupPreferences(fov_mapping_corrections={
            "sec_0":{"mappings":{"sample":"field B"}},
        }),
        lambda _value:None,
    )
    session = SLMSession(
        runtime=runtime,
        runtime_factory=factory,
        configuration_service=configuration,
        fov_mapping_correction_store=workspace.fov_mapping_correction_store,
        startup_preferences=preferences,
    )
    # The mapping catalog is not relevant to this focused config test; the
    # embedded valid base calibration identifies the active mapping.
    session.calibration.active_mapping = lambda _section:"sample"

    saved = session.save_config("with_field_a")
    saved_config,_ = config_store.load(saved.path)
    state = saved_config.sections["sec_0"].fov_correction
    assert state.name == "field A"
    assert state.applied is True

    # Change the live/default context to B, then load the config.  The saved A
    # must win without changing the persistent default B.
    session.fov_mapping_runtime.select_fov_mapping_correction("sec_0","field B")
    session.fov_mapping_runtime.set_fov_mapping_correction_applied("sec_0",True)
    assert runtime.get_section_fov_mapping_context("sec_0")["correction"].name == "field B"

    outcome = session.load_config(str(saved.path))
    assert outcome.warnings == ()
    restored = session.runtime.get_section_fov_mapping_context("sec_0")["correction"]
    assert restored.name == "field A"
    context = session.fov_mapping_runtime.fov_mapping_context("sec_0")
    assert context["applied_name"] == "field A"
    assert context["default_name"] == "field B"
    assert session.runtime.get_section_cgh_status("sec_0").result_state.value == "current"

    # Turning FOV off keeps the selected name in a newly saved config.
    session.fov_mapping_runtime.set_fov_mapping_correction_applied("sec_0",False)
    off = session.save_config("field_a_off")
    off_config,_ = config_store.load(off.path)
    off_state = off_config.sections["sec_0"].fov_correction
    assert off_state.name == "field A"
    assert off_state.applied is False

    # If A is later removed, loading the original config must not silently use
    # current/default B.  It leaves FOV unset and reports an inline warning.
    workspace.fov_mapping_correction_store.delete(
        runtime.identity,"sec_0","sample","field A",
    )
    outcome = session.load_config(str(saved.path))
    assert any(
        "field A" in warning.message and "No FOV correction was applied" in warning.message
        for warning in outcome.warnings
    )
    assert session.runtime.get_section_fov_mapping_context("sec_0")["correction"] is None
    assert session.runtime.get_section_cgh_status("sec_0").result_state.value == "stale"
    session.dispose()


def test_named_reference_affine_correction_uses_one_continuous_field(tmp_path):
    from dataclasses import replace

    from slmcore import PositionReferenceStore

    store = PositionReferenceStore(tmp_path / "position_references")

    reference_runtime = _runtime(serial="SER-488")
    reference_session = SLMSession(
        runtime=reference_runtime,position_reference_store=store,
    )
    _set_feedback_observation_planes(reference_session,"sample")
    reference_localization = _attach_reference_localization(reference_runtime)
    reference_session.feedback.add_position_reference("sec_0","488 reference")

    current_runtime = _runtime(serial="SER-405")
    current_session = SLMSession(
        runtime=current_runtime,position_reference_store=store,
    )
    _set_feedback_observation_planes(current_session,"sample")
    current_localization = _attach_reference_localization(current_runtime)

    # One deliberately bad current spot makes the desired per-spot correction
    # non-affine. Affine mode must not preserve that one independent jump.
    measured = np.array(current_localization.measured_positions_px,copy=True)
    measured[0,-1] += 20.0
    diagnostics = dict(current_localization.diagnostics)
    diagnostics.update({
        "affine_linear":((32.0,0.0),(0.0,32.0)),
        "affine_translation":(12.0,12.0),
    })
    modified = replace(
        current_localization,
        measured_positions_px=measured,
        diagnostics=diagnostics,
    )
    parameters = dict(
        current_runtime.get_section_feedback_status("sec_0").localization_params
    )
    current_runtime.commit_section_feedback_localization(
        "sec_0",modified,parameters,
    )

    current_session.feedback.set_position_reference(
        "sec_0",mode="reference",reference_name="488 reference",
        correction_mode="per_spot",
    )
    per_spot,metadata = current_session.feedback._resolve_position_reference("sec_0")
    assert metadata["correction_model"]["mode"] == "per_spot"
    per_spot_error = np.asarray(per_spot)-measured

    current_session.feedback.set_position_reference(
        "sec_0",mode="reference",reference_name="488 reference",
        correction_mode="translation",
    )
    translated,metadata = current_session.feedback._resolve_position_reference("sec_0")
    assert metadata["correction_model"]["mode"] == "translation"
    translation_error = np.asarray(translated)-measured
    np.testing.assert_allclose(
        translation_error,
        np.repeat(np.mean(per_spot_error,axis=1)[:,None],translation_error.shape[1],axis=1),
        atol=1e-10,
    )

    current_session.feedback.set_position_reference(
        "sec_0",mode="reference",reference_name="488 reference",
        correction_mode="rigid",
    )
    rigid,metadata = current_session.feedback._resolve_position_reference("sec_0")
    assert metadata["correction_model"]["mode"] == "rigid"
    registered = np.asarray(modified.expected_positions_px,dtype=np.float64)
    rigid_error = np.asarray(rigid)-measured
    rigid_registered = registered+rigid_error
    # A rigid fit may translate/rotate the lattice, but must preserve distances.
    np.testing.assert_allclose(
        np.linalg.norm(rigid_registered[:,1:]-rigid_registered[:,:-1],axis=0),
        np.linalg.norm(registered[:,1:]-registered[:,:-1],axis=0),
        atol=1e-10,
    )

    current_session.feedback.set_position_reference(
        "sec_0",mode="reference",reference_name="488 reference",
        correction_mode="affine",affine_transform=None,
    )
    affine,metadata = current_session.feedback._resolve_position_reference("sec_0")
    assert metadata["correction_model"]["mode"] == "affine"

    affine_error = np.asarray(affine)-measured
    design = np.column_stack([
        registered.T,np.ones(registered.shape[1],dtype=np.float64),
    ])
    coefficients,_,_,_ = np.linalg.lstsq(design,affine_error.T,rcond=None)
    fitted_error = (design@coefficients).T
    np.testing.assert_allclose(affine_error,fitted_error,atol=1e-10)

    # The bad site's correction is no longer its raw reference jump.
    assert not np.allclose(affine_error[:,-1],per_spot_error[:,-1])

    preview = current_session.feedback.position_reference_preview("sec_0")
    assert preview is not None
    assert preview["correction_mode"] == "affine"
    assert preview["affine_transform"].shape == (2,3)
    np.testing.assert_allclose(
        preview["raw_reference_positions_px"],per_spot,atol=1e-12,
    )

    # Manual affine editing is stored as a transient transform. Translation of
    # that mapping translates every correction vector by the same amount.
    edited_transform = np.asarray(preview["affine_transform"],dtype=np.float64).copy()
    edited_transform[:,2] += np.array([1.5,-2.0])
    current_session.feedback.set_position_reference(
        "sec_0",mode="reference",reference_name="488 reference",
        correction_mode="affine",affine_transform=edited_transform,
    )
    edited,_metadata = current_session.feedback._resolve_position_reference("sec_0")
    edited_error = np.asarray(edited)-measured
    np.testing.assert_allclose(
        edited_error-affine_error,
        np.repeat(np.array([[1.5],[-2.0]]),affine_error.shape[1],axis=1),
        atol=1e-10,
    )

    current_session.feedback.set_position_reference(
        "sec_0",mode="reference",reference_name="488 reference",
        correction_mode="affine",affine_transform=None,
    )
    assert current_session.feedback.position_reference_selection(
        "sec_0"
    ).affine_transform is None

    reference_session.dispose()
    current_session.dispose()


def test_current_reference_supports_free_affine_editing():
    runtime=_runtime()
    session=SLMSession(runtime=runtime)
    localization=_attach_reference_localization(runtime)
    from dataclasses import replace
    diagnostics=dict(localization.diagnostics)
    diagnostics.update({
        "affine_linear":((32.0,0.0),(0.0,32.0)),
        "affine_translation":(12.0,12.0),
    })
    localization=replace(localization,diagnostics=diagnostics)
    parameters=dict(runtime.get_section_feedback_status("sec_0").localization_params)
    runtime.commit_section_feedback_localization("sec_0",localization,parameters)

    selection=session.feedback.set_position_reference(
        "sec_0",mode="current",correction_mode="affine",
    )
    assert selection.mode.value=="current"
    preview=session.feedback.position_reference_preview("sec_0")
    assert preview is not None
    np.testing.assert_allclose(
        preview["raw_reference_positions_px"],localization.measured_positions_px,
    )
    np.testing.assert_allclose(
        preview["reference_positions_px"],localization.measured_positions_px,
        atol=1e-12,
    )
    assert preview["fitted_transform"]["model"]=="affine"

    transform=dict(preview["fitted_transform"])
    matrix=np.asarray(transform["matrix"],dtype=np.float64)
    shift=np.array([2.5,-1.25])
    matrix[:,2]+=shift
    transform["matrix"]=matrix.tolist()
    session.feedback.set_position_reference(
        "sec_0",mode="current",correction_mode="affine",
        manual_transform=transform,
    )
    edited=session.feedback.position_reference_preview("sec_0")
    assert edited is not None
    np.testing.assert_allclose(
        edited["reference_positions_px"]-edited["measured_positions_px"],
        np.repeat(shift[:,None],edited["measured_positions_px"].shape[1],axis=1),
        atol=1e-10,
    )
    assert edited["manual_transform"] is not None

    session.feedback.set_position_reference(
        "sec_0",mode="current",correction_mode="affine",manual_transform=None,
    )
    reset=session.feedback.position_reference_preview("sec_0")
    assert reset is not None
    np.testing.assert_allclose(
        reset["reference_positions_px"],reset["measured_positions_px"],atol=1e-12,
    )
    session.dispose()


def test_global_fit_can_use_continuous_correction_model():
    runtime=_runtime()
    session=SLMSession(runtime=runtime)
    localization=_attach_reference_localization(runtime)
    from dataclasses import replace
    diagnostics=dict(localization.diagnostics)
    diagnostics.update({
        "affine_linear":((32.0,0.0),(0.0,32.0)),
        "affine_translation":(12.0,12.0),
    })
    localization=replace(localization,diagnostics=diagnostics)
    parameters=dict(runtime.get_section_feedback_status("sec_0").localization_params)
    runtime.commit_section_feedback_localization("sec_0",localization,parameters)

    session.feedback.set_position_reference(
        "sec_0",mode="global_fit",correction_mode="affine",
    )
    preview=session.feedback.position_reference_preview("sec_0")
    assert preview is not None
    assert preview["correction_mode"]=="affine"
    assert preview["fitted_transform"]["model"]=="affine"
    # The helper localization is a pure translation from registered to measured,
    # so an affine fit reproduces the registered/global-fit reference exactly.
    np.testing.assert_allclose(
        preview["reference_positions_px"],localization.expected_positions_px,
        atol=1e-10,
    )
    session.dispose()


def test_manual_position_transform_lifecycle_is_source_and_localization_bound():
    runtime=_runtime()
    session=SLMSession(runtime=runtime)
    localization=_attach_reference_localization(runtime)
    from dataclasses import replace
    diagnostics=dict(localization.diagnostics)
    diagnostics.update({
        "affine_linear":((32.0,0.0),(0.0,32.0)),
        "affine_translation":(12.0,12.0),
    })
    localization=replace(localization,diagnostics=diagnostics)
    parameters=dict(runtime.get_section_feedback_status("sec_0").localization_params)
    runtime.commit_section_feedback_localization("sec_0",localization,parameters)

    session.feedback.set_position_reference(
        "sec_0",mode="current",correction_mode="affine",
    )
    preview=session.feedback.position_reference_preview("sec_0")
    transform=dict(preview["fitted_transform"])
    matrix=np.asarray(transform["matrix"],dtype=np.float64)
    matrix[:,2]+=np.asarray([1.0,-0.5])
    transform["matrix"]=matrix.tolist()
    selection=session.feedback.set_position_reference(
        "sec_0",mode="current",correction_mode="affine",
        manual_transform=transform,
    )
    assert selection.manual_transform is not None

    # Lightweight/same-selection refreshes preserve the manual edit.
    selection=session.feedback.set_position_reference(
        "sec_0",mode="current",correction_mode="affine",
    )
    assert selection.manual_transform is not None

    # A different reference source invalidates the source-bound edit.
    selection=session.feedback.set_position_reference(
        "sec_0",mode="global_fit",correction_mode="affine",
    )
    assert selection.manual_transform is None

    # A different correction model likewise returns to its automatic fit.
    preview=session.feedback.position_reference_preview("sec_0")
    transform=dict(preview["fitted_transform"])
    matrix=np.asarray(transform["matrix"],dtype=np.float64)
    matrix[:,2]+=np.asarray([0.25,0.75])
    transform["matrix"]=matrix.tolist()
    session.feedback.set_position_reference(
        "sec_0",mode="global_fit",correction_mode="affine",
        manual_transform=transform,
    )
    selection=session.feedback.set_position_reference(
        "sec_0",mode="global_fit",correction_mode="rigid",
    )
    assert selection.manual_transform is None

    # Polynomial manual edits are also tied to the polynomial degree.
    from slmcore.core.transform2d import FittedTransform2D
    polynomial=FittedTransform2D(
        model="polynomial",polynomial_degree=2,
        coefficients=np.zeros((2,6),dtype=np.float64),
        normalization_center=np.asarray([20.0,20.0]),
        normalization_scale=np.asarray([10.0,10.0]),
    )
    selection=session.feedback.set_position_reference(
        "sec_0",mode="current",correction_mode="polynomial",
        polynomial_degree=2,manual_transform=polynomial,
    )
    assert selection.manual_transform is not None
    selection=session.feedback.set_position_reference(
        "sec_0",mode="current",correction_mode="polynomial",
        polynomial_degree=3,
    )
    assert selection.manual_transform is None

    # Interactive edits are fitted against one accepted localization and must
    # not survive accepting a new localization, even if geometry is identical.
    session.feedback.set_position_reference(
        "sec_0",mode="current",correction_mode="affine",
    )
    preview=session.feedback.position_reference_preview("sec_0")
    transform=dict(preview["fitted_transform"])
    matrix=np.asarray(transform["matrix"],dtype=np.float64)
    matrix[:,2]+=np.asarray([0.5,0.5])
    transform["matrix"]=matrix.tolist()
    session.feedback.set_position_reference(
        "sec_0",mode="current",correction_mode="affine",
        manual_transform=transform,
    )
    assert session.feedback.accept_localization(
        "sec_0",localization,parameters,raise_errors=True,
    )
    assert session.feedback.position_reference_selection(
        "sec_0"
    ).manual_transform is None
    session.dispose()


def _observation_frame_for_reference(reference):
    from slmcore import ObservationFrame

    payload=reference.metadata["source_observation_frame"]
    return ObservationFrame.from_dict(payload)


def _current_observation_frame(session,*,section="sec_0"):
    from slmcore import ObservationFrame

    inspection=session.runtime.get_section_feedback_inspection(section)
    acquisition=inspection.measurement.acquisition
    plane=session.feedback.active_observation_plane(section)
    return ObservationFrame(
        slm_serial=session.runtime.identity.serial_number,
        section_key=section,
        observation_plane_name=plane,
        detector_name=acquisition.detector,
        image_shape=acquisition.image.shape,
        pixel_size_um=acquisition.pixel_size_um,
    )


def test_named_reference_uses_target_registration_before_correspondence_and_crop(tmp_path):
    from slmcore import (
        ObservationRegistrationStore,PositionReferenceStore,
        fit_observation_registration,
    )

    references=PositionReferenceStore(tmp_path/"references")
    registrations=ObservationRegistrationStore(tmp_path/"registrations")

    source_runtime=_runtime(serial="SER-488")
    source_session=SLMSession(
        runtime=source_runtime,position_reference_store=references,
        observation_registration_store=registrations,
    )
    _set_feedback_observation_planes(source_session,"Intermediate")
    _attach_reference_localization(
        source_runtime,image_shape=(120,140),detector="cam488",pixel_size_um=0.10,
        crop_coord=(10,74,20,84),measured_shift=(2.0,-1.0),
    )
    reference=source_session.feedback.add_position_reference("sec_0","488 intermediate")
    source_frame=_observation_frame_for_reference(reference)

    current_runtime=_runtime(serial="SER-405")
    current_session=SLMSession(
        runtime=current_runtime,position_reference_store=references,
        observation_registration_store=registrations,
    )
    _set_feedback_observation_planes(current_session,"Intermediate")
    current_localization=_attach_reference_localization(
        current_runtime,image_shape=(150,170),detector="cam405",pixel_size_um=0.20,
        crop_coord=(30,94,40,104),measured_shift=(0.0,0.0),
    )
    # Preview requires the normal accepted affine-localization diagnostics.
    from dataclasses import replace
    diagnostics=dict(current_localization.diagnostics)
    diagnostics.update({
        "affine_linear":((32.0,0.0),(0.0,32.0)),
        "affine_translation":(12.0,12.0),
    })
    current_localization=replace(current_localization,diagnostics=diagnostics)
    current_runtime.commit_section_feedback_localization(
        "sec_0",current_localization,
        dict(current_runtime.get_section_feedback_status("sec_0").localization_params),
    )
    current_frame=_current_observation_frame(current_session)

    # canonical = current-native + [10, -6], therefore canonical -> current-native
    # subtracts that offset.  Only the current/405 side is registered; 488 is the
    # explicit unregistered canonical frame.
    native_points=np.array([
        [20.0,60.0,20.0,60.0],
        [30.0,30.0,70.0,70.0],
    ])
    canonical_points=native_points+np.array([[10.0],[-6.0]])
    calibration=fit_observation_registration(
        current_frame,source_frame,native_points,canonical_points,model="affine",
    )
    current_session.observation_registration.save(calibration)

    current_session.feedback.set_position_reference(
        "sec_0",mode="reference",reference_name="488 intermediate",
        correction_mode="per_spot",
    )
    cgh_before=current_runtime.get_section_cgh_result_copy("sec_0")
    mapping_before=current_runtime.get_section_fov_mapping_context("sec_0")
    _effective,raw,metadata,_transform=(
        current_session.feedback._references._resolve_position_reference_details("sec_0")
    )

    source_full=np.asarray(reference.positions_px,dtype=np.float64)
    expected_full=source_full-np.array([[10.0],[-6.0]])
    current_crop=np.array([[current_localization.crop_coord[2]],
                           [current_localization.crop_coord[0]]],dtype=np.float64)
    np.testing.assert_allclose(raw,expected_full-current_crop,atol=1e-10)
    registration=metadata["registration"]
    assert registration["applied"] is True
    assert registration["source_registration"] is None
    assert registration["target_registration"]["transform_model"] == "affine"
    assert registration["canonical_frame"] == source_frame.to_dict()
    assert registration["source_frame"] == source_frame.to_dict()
    assert registration["target_frame"] == current_frame.to_dict()

    preview=current_session.feedback.position_reference_preview("sec_0")
    assert preview is not None
    assert preview["reference"]["registration"]["applied"] is True

    # Registration is measurement/reference interpretation only: resolving the
    # reference does not mutate the FOV context or committed CGH.
    mapping_after=current_runtime.get_section_fov_mapping_context("sec_0")
    np.testing.assert_array_equal(
        mapping_after["ideal_positions_kxy"],mapping_before["ideal_positions_kxy"],
    )
    assert mapping_after["correction"] is mapping_before["correction"]
    cgh_after=current_runtime.get_section_cgh_result_copy("sec_0")
    assert cgh_before is not None and cgh_after is not None
    assert cgh_after.target_name == cgh_before.target_name
    np.testing.assert_array_equal(cgh_after.pattern,cgh_before.pattern)

    source_session.dispose(); current_session.dispose()


def test_named_reference_uses_source_registration_when_current_is_canonical(tmp_path):
    from slmcore import (
        ObservationRegistrationStore,PositionReferenceStore,
        fit_observation_registration,
    )

    references=PositionReferenceStore(tmp_path/"references")
    registrations=ObservationRegistrationStore(tmp_path/"registrations")

    source_runtime=_runtime(serial="SER-405")
    source_session=SLMSession(
        runtime=source_runtime,position_reference_store=references,
        observation_registration_store=registrations,
    )
    _set_feedback_observation_planes(source_session,"Intermediate")
    _attach_reference_localization(
        source_runtime,detector="cam405",pixel_size_um=0.12,
    )
    reference=source_session.feedback.add_position_reference("sec_0","405 intermediate")
    source_frame=_observation_frame_for_reference(reference)

    current_runtime=_runtime(serial="SER-488")
    current_session=SLMSession(
        runtime=current_runtime,position_reference_store=references,
        observation_registration_store=registrations,
    )
    _set_feedback_observation_planes(current_session,"Intermediate")
    current_localization=_attach_reference_localization(
        current_runtime,detector="cam488",pixel_size_um=0.12,
    )
    current_frame=_current_observation_frame(current_session)

    native_points=np.array([[15.,55.,15.,55.],[20.,20.,60.,60.]])
    canonical_points=native_points+np.array([[4.0],[7.0]])
    source_session.observation_registration.save(fit_observation_registration(
        source_frame,current_frame,native_points,canonical_points,model="affine",
    ))

    current_session.feedback.set_position_reference(
        "sec_0",mode="reference",reference_name="405 intermediate",
    )
    _effective,raw,metadata,_=(
        current_session.feedback._references._resolve_position_reference_details("sec_0")
    )
    source_full=np.asarray(reference.positions_px,dtype=np.float64)
    expected_full=source_full+np.array([[4.0],[7.0]])
    crop=np.array([[current_localization.crop_coord[2]],[current_localization.crop_coord[0]]])
    np.testing.assert_allclose(raw,expected_full-crop,atol=1e-10)
    assert metadata["registration"]["applied"] is True
    assert metadata["registration"]["source_registration"] is not None
    assert metadata["registration"]["target_registration"] is None

    source_session.dispose(); current_session.dispose()


def test_named_reference_projects_between_two_registered_frames_with_polynomial(tmp_path):
    from slmcore import (
        ObservationFrame,ObservationRegistrationStore,PositionReferenceStore,
        fit_observation_registration,
    )

    references=PositionReferenceStore(tmp_path/"references")
    registrations=ObservationRegistrationStore(tmp_path/"registrations")

    source_runtime=_runtime(serial="SER-A")
    source_session=SLMSession(
        runtime=source_runtime,position_reference_store=references,
        observation_registration_store=registrations,
    )
    _set_feedback_observation_planes(source_session,"Intermediate")
    _attach_reference_localization(source_runtime,detector="camA",pixel_size_um=0.1)
    reference=source_session.feedback.add_position_reference("sec_0","source")
    source_frame=_observation_frame_for_reference(reference)

    target_runtime=_runtime(serial="SER-B")
    target_session=SLMSession(
        runtime=target_runtime,position_reference_store=references,
        observation_registration_store=registrations,
    )
    _set_feedback_observation_planes(target_session,"Intermediate")
    target_localization=_attach_reference_localization(
        target_runtime,detector="camB",pixel_size_um=0.1,
    )
    target_frame=_current_observation_frame(target_session)
    canonical=ObservationFrame(
        slm_serial="SER-CANON",section_key="sec_canonical",
        observation_plane_name="Intermediate",detector_name="camCanonical",
        image_shape=(96,112),pixel_size_um=0.1,
    )

    x,y=np.meshgrid(np.linspace(5.0,85.0,5),np.linspace(5.0,75.0,5))
    source_points=np.vstack([x.ravel(),y.ravel()])
    source_canonical=np.array(source_points,copy=True)
    source_canonical[0]+=0.001*source_points[1]**2+3.0
    source_canonical[1]+=0.0015*source_points[0]**2-2.0
    source_session.observation_registration.save(fit_observation_registration(
        source_frame,canonical,source_points,source_canonical,
        model="polynomial",polynomial_degree=2,
    ))

    target_points=np.vstack([x.ravel()+2.0,y.ravel()-4.0])
    target_canonical=np.array(target_points,copy=True)
    target_canonical[0]+=0.0008*target_points[1]**2-1.0
    target_canonical[1]+=0.0012*target_points[0]**2+5.0
    target_session.observation_registration.save(fit_observation_registration(
        target_frame,canonical,target_points,target_canonical,
        model="polynomial",polynomial_degree=2,
    ))

    target_session.feedback.set_position_reference(
        "sec_0",mode="reference",reference_name="source",
    )
    load_calls=[]
    original_load=registrations.load
    def counted_load(*args,**kwargs):
        load_calls.append(tuple(args))
        return original_load(*args,**kwargs)
    registrations.load=counted_load
    _effective,raw,metadata,_=(
        target_session.feedback._references._resolve_position_reference_details("sec_0")
    )
    # Reference projection resolves each side once for provenance and reuses
    # those calibrations for the actual source -> canonical -> target mapping.
    assert len(load_calls) == 2
    registrations.load=original_load
    source_registration=registrations.load("SER-A","sec_0","Intermediate")
    target_registration=registrations.load("SER-B","sec_0","Intermediate")
    expected_full=target_registration.canonical_to_native(
        source_registration.native_to_canonical(reference.positions_px)
    )
    crop=np.array([[target_localization.crop_coord[2]],[target_localization.crop_coord[0]]])
    np.testing.assert_allclose(raw,expected_full-crop,atol=1e-8)
    assert metadata["registration"]["applied"] is True
    assert metadata["registration"]["source_registration"]["transform_model"] == "polynomial"
    assert metadata["registration"]["target_registration"]["transform_model"] == "polynomial"

    source_session.dispose(); target_session.dispose()


def test_position_reference_presentation_resolves_reference_once(tmp_path):
    from slmcore import PositionReferenceStore

    runtime=_runtime()
    session=SLMSession(
        runtime=runtime,
        position_reference_store=PositionReferenceStore(tmp_path/"references"),
    )
    _set_feedback_observation_planes(session,"sample")
    from dataclasses import replace
    localization=_attach_reference_localization(runtime)
    diagnostics=dict(localization.diagnostics)
    diagnostics.update({
        "affine_linear":((32.0,0.0),(0.0,32.0)),
        "affine_translation":(12.0,12.0),
    })
    localization=replace(localization,diagnostics=diagnostics)
    runtime.commit_section_feedback_localization(
        "sec_0",localization,
        dict(runtime.get_section_feedback_status("sec_0").localization_params),
    )

    references=session.feedback._references
    original=references._resolve_position_reference_details
    calls=[]
    def counted(*args,**kwargs):
        calls.append(args[0])
        return original(*args,**kwargs)
    references._resolve_position_reference_details=counted
    try:
        context,preview=session.feedback.position_reference_presentation("sec_0")
    finally:
        references._resolve_position_reference_details=original

    assert context.compatible
    assert preview is not None
    measurement=runtime.get_section_feedback_inspection("sec_0").measurement
    assert measurement is not None
    assert preview["measurement_id"] == measurement.acquisition.measurement_id
    assert calls == ["sec_0"]
    session.dispose()


def test_same_native_section_reference_bypasses_registration_round_trip(tmp_path):
    from slmcore import (
        ObservationFrame,ObservationRegistrationStore,PositionReferenceStore,
        fit_observation_registration,
    )

    references=PositionReferenceStore(tmp_path/"references")
    registrations=ObservationRegistrationStore(tmp_path/"registrations")
    runtime=_runtime(serial="SER-SAME")
    session=SLMSession(
        runtime=runtime,position_reference_store=references,
        observation_registration_store=registrations,
    )
    _set_feedback_observation_planes(session,"Intermediate")
    localization=_attach_reference_localization(runtime,detector="cam",pixel_size_um=0.1)
    reference=session.feedback.add_position_reference("sec_0","same")
    frame=_current_observation_frame(session)

    points=np.array([[10.,30.,10.,30.],[15.,15.,35.,35.]])
    canonical=points+np.array([[25.0],[-13.0]])
    canonical_frame=ObservationFrame(
        slm_serial="SER-CANONICAL",section_key="sec_ref",
        observation_plane_name="Intermediate",detector_name="cam-canonical",
        image_shape=frame.image_shape,pixel_size_um=frame.pixel_size_um,
    )
    session.observation_registration.save(fit_observation_registration(
        frame,canonical_frame,points,canonical,model="affine",
    ))

    session.feedback.set_position_reference("sec_0",mode="reference",reference_name="same")
    _effective,raw,metadata,_=(
        session.feedback._references._resolve_position_reference_details("sec_0")
    )
    crop=np.array([[localization.crop_coord[2]],[localization.crop_coord[0]]])
    np.testing.assert_allclose(raw,reference.positions_px-crop,atol=1e-12)
    assert metadata["registration"]["applied"] is False
    assert metadata["registration"]["reason"] == "same native observation frame"

    session.dispose()


def test_named_reference_without_registration_keeps_legacy_detector_checks(tmp_path):
    import pytest
    from slmcore import ObservationRegistrationStore,PositionReferenceStore

    references=PositionReferenceStore(tmp_path/"references")
    registrations=ObservationRegistrationStore(tmp_path/"registrations")
    source_runtime=_runtime(serial="SER-A")
    source_session=SLMSession(
        runtime=source_runtime,position_reference_store=references,
        observation_registration_store=registrations,
    )
    _set_feedback_observation_planes(source_session,"Intermediate")
    _attach_reference_localization(source_runtime,detector="camA")
    source_session.feedback.add_position_reference("sec_0","source")

    target_runtime=_runtime(serial="SER-B")
    target_session=SLMSession(
        runtime=target_runtime,position_reference_store=references,
        observation_registration_store=registrations,
    )
    _set_feedback_observation_planes(target_session,"Intermediate")
    _attach_reference_localization(target_runtime,detector="camB")
    target_session.feedback.set_position_reference(
        "sec_0",mode="reference",reference_name="source",
    )

    with pytest.raises(RuntimeError,match='Reference detector "camA" does not match'):
        target_session.feedback._resolve_position_reference("sec_0")

    source_session.dispose(); target_session.dispose()


def test_identity_registration_preserves_named_reference_coordinates(tmp_path):
    from slmcore import ObservationRegistrationStore,PositionReferenceStore,fit_observation_registration

    references=PositionReferenceStore(tmp_path/"references")
    registrations=ObservationRegistrationStore(tmp_path/"registrations")

    source_runtime=_runtime(serial="SER-SOURCE")
    source_session=SLMSession(
        runtime=source_runtime,position_reference_store=references,
        observation_registration_store=registrations,
    )
    _set_feedback_observation_planes(source_session,"Intermediate")
    source_localization=_attach_reference_localization(source_runtime,detector="cam")
    reference=source_session.feedback.add_position_reference("sec_0","identity")
    source_frame=_observation_frame_for_reference(reference)

    target_runtime=_runtime(serial="SER-TARGET")
    target_session=SLMSession(
        runtime=target_runtime,position_reference_store=references,
        observation_registration_store=registrations,
    )
    _set_feedback_observation_planes(target_session,"Intermediate")
    target_localization=_attach_reference_localization(target_runtime,detector="cam")
    target_frame=_current_observation_frame(target_session)

    points=np.array([[5.,35.,5.,35.],[10.,10.,40.,40.]])
    source_session.observation_registration.save(fit_observation_registration(
        source_frame,target_frame,points,points,model="affine",
    ))
    target_session.feedback.set_position_reference(
        "sec_0",mode="reference",reference_name="identity",
    )
    _effective,raw,metadata,_=(
        target_session.feedback._references._resolve_position_reference_details("sec_0")
    )
    crop=np.array([[target_localization.crop_coord[2]],[target_localization.crop_coord[0]]])
    np.testing.assert_allclose(raw,reference.positions_px-crop,atol=1e-12)
    assert metadata["registration"]["applied"] is True

    source_session.dispose(); target_session.dispose()


def test_position_reference_changes_use_narrow_refresh_callback(tmp_path):
    from slmcore import PositionReferenceMode,PositionReferenceStore

    store=PositionReferenceStore(tmp_path / "position_references")
    section_refreshes=[]
    reference_refreshes=[]
    runtime=_runtime()
    session=SLMSession(
        runtime=runtime,
        position_reference_store=store,
        callbacks=SLMSessionCallbacks(
            on_section_refresh_requested=section_refreshes.append,
            on_position_reference_changed=reference_refreshes.append,
        ),
    )
    _set_feedback_observation_planes(session,"sample")
    _attach_reference_localization(runtime)
    section_refreshes.clear()
    reference_refreshes.clear()

    session.feedback.add_position_reference("sec_0","reference")
    assert section_refreshes == []
    assert reference_refreshes == ["sec_0"]
    assert session.feedback.position_reference_selection(
        "sec_0"
    ).reference_name == "reference"

    reference_refreshes.clear()
    session.feedback.persist_position_reference("sec_0","reference")
    assert section_refreshes == []
    assert reference_refreshes == ["sec_0"]

    reference_refreshes.clear()
    session.feedback.set_position_reference(
        "sec_0",mode=PositionReferenceMode.GLOBAL_FIT,
    )
    assert section_refreshes == []
    assert reference_refreshes == ["sec_0"]

    reference_refreshes.clear()
    session.feedback.delete_position_reference("sec_0","reference")
    assert section_refreshes == []
    assert reference_refreshes == ["sec_0"]
    session.dispose()


def test_shared_position_reference_store_notifies_each_session_narrowly(tmp_path):
    from slmcore import PositionReferenceStore

    store=PositionReferenceStore(tmp_path / "position_references")
    refreshes_a=[]
    refreshes_b=[]
    section_refreshes_a=[]
    section_refreshes_b=[]
    runtime_a=_runtime(serial="SER-A")
    runtime_b=_runtime(serial="SER-B")
    session_a=SLMSession(
        runtime=runtime_a,
        position_reference_store=store,
        callbacks=SLMSessionCallbacks(
            on_section_refresh_requested=section_refreshes_a.append,
            on_position_reference_changed=refreshes_a.append,
        ),
    )
    session_b=SLMSession(
        runtime=runtime_b,
        position_reference_store=store,
        callbacks=SLMSessionCallbacks(
            on_section_refresh_requested=section_refreshes_b.append,
            on_position_reference_changed=refreshes_b.append,
        ),
    )
    _set_feedback_observation_planes(session_a,"sample")
    _set_feedback_observation_planes(session_b,"sample")
    _attach_reference_localization(runtime_a)
    section_refreshes_a.clear(); section_refreshes_b.clear()
    refreshes_a.clear(); refreshes_b.clear()

    session_a.feedback.add_position_reference("sec_0","shared")

    assert section_refreshes_a == []
    assert section_refreshes_b == []
    assert refreshes_a == ["sec_0"]
    assert refreshes_b == ["sec_0"]
    assert tuple(
        record.name
        for record in session_b.feedback.position_reference_context("sec_0").references
    ) == ("shared",)
    session_a.dispose(); session_b.dispose()
