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
    changed = []
    session = SLMSession(
        runtime=runtime,
        callbacks=SLMSessionCallbacks(
            on_section_refresh_requested=changed.append,
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
    assert changed[-1] == "sec_0"


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


def test_feedback_orientation_is_transient_until_explicitly_saved_and_scoped_by_plane():
    from slmcore.application.startup_preferences import StartupPreferencesState
    from slmcore.setup import SLMStartupPreferences

    runtime = _runtime()
    saved = []
    preferences = StartupPreferencesState(
        SLMStartupPreferences(feedback_orientations={
            "sec_0":{
                "default":"identity",
                "planes":{"sample":"flip_horizontal"},
            },
        }),
        saved.append,
    )
    session = SLMSession(runtime=runtime,startup_preferences=preferences)
    active_plane = {"value":None}
    _set_feedback_observation_planes(session,"sample","fourier")
    session.feedback.select_observation_plane("sec_0",active_plane["value"])

    assert session.feedback.feedback_orientation("sec_0").value == "identity"
    session.feedback.set_feedback_orientation("sec_0","flip_vertical")
    assert session.feedback.feedback_orientation("sec_0").value == "flip_vertical"
    assert saved == []

    context = session.feedback.feedback_orientation_context("sec_0")
    assert context.observation_plane_name is None
    assert context.save_needed
    session.feedback.save_feedback_orientation("sec_0")
    assert saved[-1].feedback_orientations["sec_0"].default == "flip_vertical"

    active_plane["value"] = "sample"
    session.feedback.select_observation_plane("sec_0","sample")
    session.feedback.refresh_feedback_orientation_contexts()
    assert session.feedback.feedback_orientation("sec_0").value == "flip_horizontal"
    context = session.feedback.feedback_orientation_context("sec_0")
    assert context.observation_plane_name == "sample"
    assert context.observation_plane_override
    assert not context.save_needed

    session.feedback.set_feedback_orientation("sec_0","rotate_180")
    assert session.feedback.feedback_orientation("sec_0").value == "rotate_180"
    assert saved[-1].feedback_orientations["sec_0"].planes["sample"] == "flip_horizontal"
    session.feedback.save_feedback_orientation("sec_0")
    assert saved[-1].feedback_orientations["sec_0"].planes["sample"] == "rotate_180"



def test_manual_plane_orientation_save_persists_authoritative_geometry_calibration(tmp_path):
    from slmcore.workspace import SLMGeometryOrientationCalibrationStore

    runtime = _runtime()
    geometry_store = SLMGeometryOrientationCalibrationStore(tmp_path/"geometry")
    session = SLMSession(
        runtime=runtime,
        geometry_orientation_calibration_store=geometry_store,
    )
    _set_feedback_observation_planes(session,"sample")
    session.feedback.select_observation_plane("sec_0","sample")

    assert not session.observation_geometry.is_calibrated("sec_0","sample")
    session.feedback.set_feedback_orientation("sec_0","rotate_90_cw")
    session.feedback.save_feedback_orientation("sec_0")

    assert session.observation_geometry.is_calibrated("sec_0","sample")
    saved = session.observation_geometry.load("sec_0","sample")
    assert saved.orientation.value == "rotate_90_cw"
    assert saved.protocol == "manual_orientation_v1"
    assert saved.provenance["method"] == "manual"
    context = session.feedback.feedback_orientation_context("sec_0")
    assert context.calibrated
    assert context.saved_orientation.value == "rotate_90_cw"

def test_feedback_orientation_unsaved_value_is_discarded_only_when_plane_changes():
    from slmcore.application.startup_preferences import StartupPreferencesState
    from slmcore.setup import SLMStartupPreferences

    runtime = _runtime()
    preferences = StartupPreferencesState(
        SLMStartupPreferences(feedback_orientations={
            "sec_0":{
                "default":"identity",
                "planes":{
                    "sample":"flip_horizontal",
                    "fourier":"rotate_180",
                },
            },
        }),
        lambda _value:None,
    )
    session = SLMSession(runtime=runtime,startup_preferences=preferences)
    active_plane = {"value":"sample"}
    _set_feedback_observation_planes(session,"sample","fourier")
    session.feedback.select_observation_plane("sec_0",active_plane["value"])

    assert session.feedback.feedback_orientation("sec_0").value == "flip_horizontal"
    session.feedback.set_feedback_orientation("sec_0","flip_vertical")
    session.feedback.refresh_feedback_orientation_contexts()
    assert session.feedback.feedback_orientation("sec_0").value == "flip_vertical"

    active_plane["value"] = "fourier"
    session.feedback.select_observation_plane("sec_0","fourier")
    session.feedback.refresh_feedback_orientation_contexts()
    assert session.feedback.feedback_orientation("sec_0").value == "rotate_180"

    active_plane["value"] = "sample"
    session.feedback.select_observation_plane("sec_0","sample")
    session.feedback.refresh_feedback_orientation_contexts()
    assert session.feedback.feedback_orientation("sec_0").value == "flip_horizontal"


def test_feedback_orientation_change_locks_only_after_feedback_cgh_is_computed():
    from types import SimpleNamespace
    import pytest

    runtime = _runtime()
    session = SLMSession(runtime=runtime)

    # Base CGH round 0 is still editable. This also covers an intensity
    # "Adapt target" working round, because the last computed round remains 0.
    session.feedback.set_feedback_orientation("sec_0","flip_horizontal")

    original_status = runtime.get_section_cgh_status
    runtime.get_section_cgh_status = lambda _section:SimpleNamespace(
        current_round_index=1,
        position_active=False,
    )
    try:
        context = session.feedback.feedback_orientation_context("sec_0")
        assert not context.change_allowed
        with pytest.raises(RuntimeError,match="locked"):
            session.feedback.set_feedback_orientation("sec_0","identity")
    finally:
        runtime.get_section_cgh_status = original_status

    runtime.get_section_cgh_status = lambda _section:SimpleNamespace(
        current_round_index=0,
        position_active=True,
    )
    try:
        context = session.feedback.feedback_orientation_context("sec_0")
        assert not context.change_allowed
        with pytest.raises(RuntimeError,match="locked"):
            session.feedback.set_feedback_orientation("sec_0","identity")
    finally:
        runtime.get_section_cgh_status = original_status



def _attach_reference_localization(runtime,section_key="sec_0"):
    from slmcore.core.cgh.localization import LocalizationResult

    section = runtime._get_section(section_key)
    resolution = section._cgh_session.create_target_resolution(
        section.state.cgh,section._build_context(section.state),
    )
    count = int(resolution.lattice_indices.shape[1])
    image = np.zeros((96,112),dtype=np.float64)
    measurement = ImageMeasurement(image=image,source="cam",detector="cam")
    runtime.set_section_feedback_measurement(section_key,measurement)
    parameters = dict(
        runtime.get_section_feedback_status(section_key).localization_params
    )
    expected = np.array([
        [12.0,44.0,12.0,44.0],
        [12.0,12.0,44.0,44.0],
    ],dtype=np.float64)[:,:count]
    measured = expected + np.array([[1.0],[2.0]])
    localization = LocalizationResult(
        target_type="multi_foci_vector",
        target_params=dict(resolution.canonical_params),
        parameters=parameters,
        lattice_indices=resolution.lattice_indices,
        crop_coord=(8,72,16,80),
        cropped_image=np.zeros((64,64),dtype=np.float64),
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


def test_named_reference_reindexes_source_orientation_for_destination_section(tmp_path):
    from slmcore import PositionReferenceStore
    from slmcore.core.cgh.feedback import orient_localization

    store = PositionReferenceStore(tmp_path / "position_references")

    runtime_a = _runtime(serial="SER-A")
    session_a = SLMSession(runtime=runtime_a,position_reference_store=store)
    _set_feedback_observation_planes(session_a,"sample")
    localization_a = _attach_reference_localization(runtime_a)
    session_a.feedback.set_feedback_orientation("sec_0","rotate_180")
    reference = session_a.feedback.add_position_reference("sec_0","488 reference")
    assert reference.metadata["feedback_orientation"] == "rotate_180"

    runtime_b = _runtime(serial="SER-B")
    session_b = SLMSession(runtime=runtime_b,position_reference_store=store)
    _set_feedback_observation_planes(session_b,"sample")
    localization_b = _attach_reference_localization(runtime_b)
    session_b.feedback.set_feedback_orientation("sec_0","flip_vertical")
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


def test_fov_preview_falls_back_to_saved_samples_without_cgh_target(tmp_path):
    from slmcore import FOVMappingCorrection,SLMWorkspace

    geometry = SLMGeometry(width=64,height=64,pixel_size_um=1.0)
    runtime = SLMRuntime(
        identity=SLMIdentity("slm","SER123"),geometry=geometry,
        section_geometries=split_slm_geometry(geometry,1),
        registries=DEFAULT_REGISTRIES,
    )
    points = np.array([
        [-0.2,0.0,0.2,-0.2,0.0,0.2],
        [-0.2,-0.2,-0.2,0.2,0.2,0.2],
    ])
    displacement = np.repeat(np.array([[0.01],[-0.02]]),points.shape[1],axis=1)
    calibration = FOVMappingCorrection.fit(
        name="field A",slm_serial="SER123",section_key="sec_0",
        mapping_name="sample",base_mapping_signature="",ideal_positions_kxy=points,
        total_displacements_kxy=displacement,degree=1,
    )
    workspace = SLMWorkspace(tmp_path)
    workspace.fov_mapping_correction_store.save(calibration)
    session = SLMSession(
        runtime=runtime,
        fov_mapping_correction_store=workspace.fov_mapping_correction_store,
    )
    session.calibration.active_mapping = lambda _section:"sample"
    session.fov_mapping_runtime.refresh_fov_mapping_contexts()
    session.fov_mapping_runtime.select_fov_mapping_correction("sec_0","field A")

    preview = session.fov_mapping_runtime.fov_mapping_preview("sec_0")
    assert preview is not None
    np.testing.assert_allclose(preview["current_positions_kxy"],points)
    assert preview["current_positions_kxy"].shape[1] > 0
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

    current_session.feedback.set_position_reference(
        "sec_0",mode="reference",reference_name="488 reference",
        correction_mode="affine",affine_transform=None,
    )
    affine,metadata = current_session.feedback._resolve_position_reference("sec_0")
    assert metadata["correction_model"]["mode"] == "affine"

    registered = np.asarray(modified.expected_positions_px,dtype=np.float64)
    affine_error = np.asarray(affine)-measured
    design = np.column_stack([
        registered.T,np.ones(registered.shape[1],dtype=np.float64),
    ])
    coefficients,_,_,_ = np.linalg.lstsq(design,affine_error.T,rcond=None)
    fitted_error = (design@coefficients).T
    np.testing.assert_allclose(affine_error,fitted_error,atol=1e-10)

    # The bad site's correction is no longer its raw reference jump.
    per_spot_error = np.asarray(per_spot)-measured
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


def test_quadratic_reference_correction_reproduces_smooth_nonlinear_field():
    from slmcore.application.feedback.references import (
        _apply_quadratic_displacement,_fit_quadratic_displacement,
    )

    xs,ys = np.meshgrid(
        np.linspace(10.0,90.0,5),np.linspace(20.0,80.0,4),indexing="xy",
    )
    registered = np.vstack([xs.ravel(),ys.ravel()])
    x = (registered[0]-50.0)/40.0
    y = (registered[1]-50.0)/30.0
    expected_error = np.vstack([
        1.2 + 0.4*x - 0.25*y + 1.8*x*x - 0.7*x*y + 0.6*y*y,
        -0.8 + 0.2*x + 0.5*y - 0.4*x*x + 0.9*x*y + 1.1*y*y,
    ])

    polynomial = _fit_quadratic_displacement(registered,expected_error)
    fitted_error = _apply_quadratic_displacement(polynomial,registered)
    np.testing.assert_allclose(fitted_error,expected_error,atol=1e-10)

    # An affine field cannot reproduce the deliberately nonlinear curvature.
    design = np.column_stack([
        registered.T,np.ones(registered.shape[1],dtype=np.float64),
    ])
    affine_coefficients,_,_,_ = np.linalg.lstsq(
        design,expected_error.T,rcond=None,
    )
    affine_error = (design@affine_coefficients).T
    assert np.max(np.abs(affine_error-expected_error)) > 0.5
