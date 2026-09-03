import numpy as np

from slmcore import (
    DEFAULT_REGISTRIES,ImageMeasurement,SLMGeometry,SLMIdentity,SLMRuntime,
    SLMSession,SLMSessionCallbacks,
)
from slmcore.core.cgh import CGHResult
from slmcore.core.engine.section import split_slm_geometry
from slmcore.host import SLMDeviceProvider,SLMHostServices


def _runtime(*,serial="SER123"):
    geometry = SLMGeometry(width=64,height=64,pixel_size_um=1.0)
    runtime = SLMRuntime(
        identity=SLMIdentity("slm",serial),
        geometry=geometry,
        section_geometries=split_slm_geometry(geometry,1),
        registries=DEFAULT_REGISTRIES,
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
    session.calibration.active_plane = lambda _section:active_plane["value"]

    assert session.feedback.feedback_orientation("sec_0").value == "identity"
    session.feedback.set_feedback_orientation("sec_0","flip_vertical")
    assert session.feedback.feedback_orientation("sec_0").value == "flip_vertical"
    assert saved == []

    context = session.feedback.feedback_orientation_context("sec_0")
    assert context.plane_name is None
    assert context.save_needed
    session.feedback.save_feedback_orientation("sec_0")
    assert saved[-1].feedback_orientations["sec_0"].default == "flip_vertical"

    active_plane["value"] = "sample"
    session.feedback.refresh_feedback_orientation_contexts()
    assert session.feedback.feedback_orientation("sec_0").value == "flip_horizontal"
    context = session.feedback.feedback_orientation_context("sec_0")
    assert context.plane_name == "sample"
    assert context.plane_override
    assert not context.save_needed

    session.feedback.set_feedback_orientation("sec_0","rotate_180")
    assert session.feedback.feedback_orientation("sec_0").value == "rotate_180"
    assert saved[-1].feedback_orientations["sec_0"].planes["sample"] == "flip_horizontal"
    session.feedback.save_feedback_orientation("sec_0")
    assert saved[-1].feedback_orientations["sec_0"].planes["sample"] == "rotate_180"


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
    session.calibration.active_plane = lambda _section:active_plane["value"]

    assert session.feedback.feedback_orientation("sec_0").value == "flip_horizontal"
    session.feedback.set_feedback_orientation("sec_0","flip_vertical")
    session.feedback.refresh_feedback_orientation_contexts()
    assert session.feedback.feedback_orientation("sec_0").value == "flip_vertical"

    active_plane["value"] = "fourier"
    session.feedback.refresh_feedback_orientation_contexts()
    assert session.feedback.feedback_orientation("sec_0").value == "rotate_180"

    active_plane["value"] = "sample"
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


def test_saved_position_reference_is_shared_but_selection_is_transient(tmp_path):
    from slmcore import PositionReferenceMode,SLMWorkspace

    workspace = SLMWorkspace(tmp_path)
    runtime_a = _runtime(serial="SER-A")
    session_a = SLMSession(
        runtime=runtime_a,
        position_reference_store=workspace.position_reference_store,
    )
    session_a.calibration.active_plane = lambda _section:"sample"
    localization = _attach_reference_localization(runtime_a)

    reference = session_a.feedback.save_position_reference(
        "sec_0","aligned OFF",
    )
    assert reference.metadata["source_slm_serial"] == "SER-A"
    np.testing.assert_allclose(
        reference.positions_px,
        localization.measured_positions_px + np.array([[16.0],[8.0]]),
    )
    assert session_a.feedback.position_reference_selection(
        "sec_0"
    ).mode is PositionReferenceMode.SAVED

    # A different SLM session on the same workspace sees the resource, but it
    # does not inherit the other session's active selection.
    runtime_b = _runtime(serial="SER-B")
    session_b = SLMSession(
        runtime=runtime_b,
        position_reference_store=workspace.position_reference_store,
    )
    session_b.calibration.active_plane = lambda _section:"sample"
    context = session_b.feedback.position_reference_context("sec_0")
    assert context.saved_names == ("aligned OFF",)
    assert context.selection.mode is PositionReferenceMode.GLOBAL_FIT

    selection = session_b.feedback.set_position_reference(
        "sec_0",mode="saved",saved_name="aligned OFF",
    )
    assert selection.mode is PositionReferenceMode.SAVED
    # Selecting a saved reference before acquiring/localizing is valid; actual
    # lattice/image compatibility is checked as soon as localization exists.
    assert session_b.feedback.position_reference_context("sec_0").compatible

    session_a.dispose()
    session_b.dispose()


def _fov_calibration_for_runtime(runtime,name,displacement=(0.01,-0.02)):
    from slmcore import FOVPositionCalibration

    section = runtime._get_section("sec_0")
    resolution = section._cgh_session.create_target_resolution(
        section.state.cgh,section._build_context(section.state),
    )
    ideal = np.asarray(resolution.ideal_spot_positions_kxy,dtype=np.float64)
    delta = np.repeat(
        np.asarray(displacement,dtype=np.float64).reshape(2,1),
        ideal.shape[1],axis=1,
    )
    return FOVPositionCalibration.fit(
        name=name,
        slm_serial=runtime.identity.serial_number,
        section_key="sec_0",
        plane_name="sample",
        ideal_positions_kxy=ideal,
        total_displacements_kxy=delta,
        degree=1,
    )


def test_fov_default_is_plane_scoped_and_apply_off_is_temporary(tmp_path):
    from slmcore import SLMStartupPreferences,SLMWorkspace
    from slmcore.application.startup_preferences import StartupPreferencesState

    workspace = SLMWorkspace(tmp_path)
    runtime = _runtime()
    calibration = _fov_calibration_for_runtime(runtime,"field A")
    workspace.fov_position_calibration_store.save(calibration)
    saved = []
    preferences = StartupPreferencesState(
        SLMStartupPreferences(fov_position_calibrations={
            "sec_0":{"planes":{"sample":"field A"}},
        }),
        saved.append,
    )
    session = SLMSession(
        runtime=runtime,
        fov_position_calibration_store=workspace.fov_position_calibration_store,
        startup_preferences=preferences,
    )
    session.calibration.active_plane = lambda _section:"sample"

    session.feedback.apply_startup_fov_position_calibration_defaults()
    context = session.feedback.fov_position_calibration_context("sec_0")
    assert context["selected_name"] == "field A"
    assert context["applied"] is True
    assert runtime.get_section_fov_position_context("sec_0")["calibration"].name == "field A"

    session.feedback.set_fov_position_calibration_applied("sec_0",False)
    context = session.feedback.fov_position_calibration_context("sec_0")
    assert context["applied"] is False
    assert context["default_name"] == "field A"
    assert runtime.get_section_fov_position_context("sec_0")["calibration"] is None
    assert saved == []

    session.dispose()


def test_fov_selection_swap_while_applied_updates_runtime_and_keeps_default(tmp_path):
    from slmcore import SLMStartupPreferences,SLMWorkspace
    from slmcore.application.startup_preferences import StartupPreferencesState

    workspace = SLMWorkspace(tmp_path)
    runtime = _runtime()
    first = _fov_calibration_for_runtime(runtime,"field A",(0.01,-0.02))
    second = _fov_calibration_for_runtime(runtime,"field B",(-0.03,0.04))
    store = workspace.fov_position_calibration_store
    store.save(first); store.save(second)
    saved = []
    preferences = StartupPreferencesState(SLMStartupPreferences(),saved.append)
    session = SLMSession(
        runtime=runtime,fov_position_calibration_store=store,
        startup_preferences=preferences,
    )
    session.calibration.active_plane = lambda _section:"sample"
    session.feedback.refresh_fov_position_calibration_contexts()

    session.feedback.select_fov_position_calibration("sec_0","field A")
    session.feedback.set_fov_position_calibration_applied("sec_0",True)
    session.feedback.set_default_fov_position_calibration("sec_0")
    assert saved[-1].fov_position_calibrations["sec_0"].planes["sample"] == "field A"

    session.feedback.select_fov_position_calibration("sec_0","field B")
    context = session.feedback.fov_position_calibration_context("sec_0")
    assert context["applied"] is True
    assert context["selected_name"] == "field B"
    assert context["default_name"] == "field A"
    assert runtime.get_section_fov_position_context("sec_0")["calibration"].name == "field B"

    session.dispose()


def test_switching_fov_calibration_clears_transient_position_feedback(tmp_path):
    from slmcore import SLMWorkspace

    workspace = SLMWorkspace(tmp_path)
    runtime = _runtime()
    store = workspace.fov_position_calibration_store
    first = _fov_calibration_for_runtime(runtime,"field A")
    second = _fov_calibration_for_runtime(runtime,"field B",(0.02,0.01))
    store.save(first); store.save(second)
    session = SLMSession(runtime=runtime,fov_position_calibration_store=store)
    session.calibration.active_plane = lambda _section:"sample"
    session.feedback.refresh_fov_position_calibration_contexts()
    session.feedback.select_fov_position_calibration("sec_0","field A")
    session.feedback.set_fov_position_calibration_applied("sec_0",True)

    _attach_reference_localization(runtime)
    session.feedback.apply_position_correction("sec_0")
    assert runtime.get_section_feedback_status("sec_0").position_active is True

    session.feedback.select_fov_position_calibration("sec_0","field B")
    status = runtime.get_section_feedback_status("sec_0")
    assert status.position_active is False
    assert runtime.get_section_fov_position_context("sec_0")["calibration"].name == "field B"

    session.dispose()


def test_fov_preview_falls_back_to_saved_samples_without_cgh_target(tmp_path):
    from slmcore import FOVPositionCalibration,SLMWorkspace

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
    calibration = FOVPositionCalibration.fit(
        name="field A",slm_serial="SER123",section_key="sec_0",
        plane_name="sample",ideal_positions_kxy=points,
        total_displacements_kxy=displacement,degree=1,
    )
    workspace = SLMWorkspace(tmp_path)
    workspace.fov_position_calibration_store.save(calibration)
    session = SLMSession(
        runtime=runtime,
        fov_position_calibration_store=workspace.fov_position_calibration_store,
    )
    session.calibration.active_plane = lambda _section:"sample"
    session.feedback.refresh_fov_position_calibration_contexts()
    session.feedback.select_fov_position_calibration("sec_0","field A")
    session.feedback.set_fov_position_calibration_applied("sec_0",True)

    preview = session.feedback.fov_position_calibration_preview("sec_0")
    assert preview is not None
    np.testing.assert_allclose(preview["current_positions_kxy"],points)
    assert preview["current_positions_kxy"].shape[1] > 0
    session.dispose()


def test_plane_switch_clears_previous_runtime_fov_when_new_plane_has_no_selection(tmp_path):
    from slmcore import FOVPositionCalibration,SLMWorkspace

    workspace = SLMWorkspace(tmp_path)
    runtime = _runtime()
    calibration = _fov_calibration_for_runtime(runtime,"field A")
    workspace.fov_position_calibration_store.save(calibration)
    session = SLMSession(
        runtime=runtime,
        fov_position_calibration_store=workspace.fov_position_calibration_store,
    )
    active = {"plane":"sample"}
    session.calibration.active_plane = lambda _section:active["plane"]
    session.feedback.refresh_fov_position_calibration_contexts()
    session.feedback.select_fov_position_calibration("sec_0","field A")
    session.feedback.set_fov_position_calibration_applied("sec_0",True)
    assert runtime.get_section_fov_position_context("sec_0")["calibration"].name == "field A"

    active["plane"] = "other"
    session.feedback.refresh_fov_position_calibration_contexts()
    assert runtime.get_section_fov_position_context("sec_0")["calibration"] is None
    assert session.feedback.fov_position_calibration_context("sec_0")["applied"] is False
    session.dispose()


def test_reading_fov_context_does_not_mutate_runtime_before_explicit_startup_apply(tmp_path):
    from slmcore import SLMStartupPreferences,SLMWorkspace
    from slmcore.application.startup_preferences import StartupPreferencesState

    workspace = SLMWorkspace(tmp_path)
    runtime = _runtime()
    calibration = _fov_calibration_for_runtime(runtime,"field A")
    workspace.fov_position_calibration_store.save(calibration)
    preferences = StartupPreferencesState(
        SLMStartupPreferences(fov_position_calibrations={
            "sec_0":{"planes":{"sample":"field A"}},
        }),
        lambda _preferences:None,
    )
    transitions = []
    session = SLMSession(
        runtime=runtime,
        fov_position_calibration_store=workspace.fov_position_calibration_store,
        startup_preferences=preferences,
        callbacks=SLMSessionCallbacks(
            on_transition_committed=lambda key,transition:transitions.append((key,transition)),
        ),
    )
    session.calibration.active_plane = lambda _section:"sample"

    context = session.feedback.fov_position_calibration_context("sec_0")
    assert context["selected_name"] == "field A"
    assert context["applied"] is True
    assert runtime.get_section_fov_position_context("sec_0")["calibration"] is None
    assert transitions == []

    session.feedback.apply_startup_fov_position_calibration_defaults()
    assert runtime.get_section_fov_position_context("sec_0")["calibration"].name == "field A"
    assert len(transitions) == 1
    session.dispose()


def test_fov_replacement_warning_detects_shifted_equal_area_coverage(tmp_path):
    from slmcore import FOVPositionCalibration,SLMWorkspace

    workspace = SLMWorkspace(tmp_path)
    runtime = _runtime()
    old = _fov_calibration_for_runtime(runtime,"field A")
    workspace.fov_position_calibration_store.save(old)
    session = SLMSession(
        runtime=runtime,
        fov_position_calibration_store=workspace.fov_position_calibration_store,
    )
    session.calibration.active_plane = lambda _section:"sample"
    session.feedback.refresh_fov_position_calibration_contexts()
    session.feedback.select_fov_position_calibration("sec_0","field A")
    session.feedback.set_fov_position_calibration_applied("sec_0",True)

    shifted = np.array(old.sample_positions_kxy,copy=True)
    shifted[0] += 0.05
    candidate = FOVPositionCalibration.fit(
        name="Current candidate",slm_serial=runtime.identity.serial_number,
        section_key="sec_0",plane_name="sample",
        ideal_positions_kxy=shifted,
        total_displacements_kxy=np.array(old.sample_displacements_kxy,copy=True),
        degree=1,
    )
    session.feedback._fov_position_calibration_candidates[("sec_0","sample")] = candidate
    warning = session.feedback.fov_position_calibration_replacement_warning("sec_0")
    assert "coverage differs" in warning
    assert "outside" in warning
    session.dispose()


def test_fov_fit_uses_accepted_position_correction_after_apply(tmp_path):
    from slmcore import SLMWorkspace

    workspace = SLMWorkspace(tmp_path)
    runtime = _runtime()
    session = SLMSession(
        runtime=runtime,
        fov_position_calibration_store=workspace.fov_position_calibration_store,
    )
    session.calibration.active_plane = lambda _section:"sample"
    session.feedback.refresh_fov_position_calibration_contexts()
    _attach_reference_localization(runtime)
    session.feedback.apply_position_correction("sec_0")

    inspection = runtime.get_section_cgh_session_inspection("sec_0")
    correction = inspection.position_correction
    assert correction is not None
    expected = (
        np.asarray(correction.corrected_positions_kxy)
        - np.asarray(correction.ideal_positions_kxy)
    )

    # Once accepted, fitting must no longer depend on re-running the current
    # localization analysis. This is what keeps the fit available after an
    # adapted hologram has been computed and experimentally checked.
    original = runtime.compute_section_feedback_position_analysis
    runtime.compute_section_feedback_position_analysis = lambda *args,**kwargs: (
        (_ for _ in ()).throw(AssertionError("fresh analysis should not run"))
    )
    try:
        candidate = session.feedback.fit_fov_position_calibration(
            "sec_0",degree=1,
        )
    finally:
        runtime.compute_section_feedback_position_analysis = original

    np.testing.assert_allclose(candidate.sample_displacements_kxy,expected)
    assert candidate.provenance["source"] == "accepted_position_correction"
    session.dispose()


def test_delete_fov_calibration_clears_selection_runtime_and_default(tmp_path):
    from slmcore import SLMStartupPreferences,SLMWorkspace
    from slmcore.application.startup_preferences import StartupPreferencesState

    workspace = SLMWorkspace(tmp_path)
    runtime = _runtime()
    calibration = _fov_calibration_for_runtime(runtime,"field A")
    store = workspace.fov_position_calibration_store
    store.save(calibration)
    saved = []
    preferences = StartupPreferencesState(SLMStartupPreferences(),saved.append)
    session = SLMSession(
        runtime=runtime,
        fov_position_calibration_store=store,
        startup_preferences=preferences,
    )
    session.calibration.active_plane = lambda _section:"sample"
    session.feedback.refresh_fov_position_calibration_contexts()
    session.feedback.select_fov_position_calibration("sec_0","field A")
    session.feedback.set_fov_position_calibration_applied("sec_0",True)
    session.feedback.set_default_fov_position_calibration("sec_0")
    assert preferences.default_fov_position_calibration("sec_0","sample") == "field A"

    deleted = session.feedback.delete_fov_position_calibration("sec_0")

    assert deleted == "field A"
    assert not store.exists(runtime.identity,"sec_0","sample","field A")
    context = session.feedback.fov_position_calibration_context("sec_0")
    assert context["selected_name"] is None
    assert context["applied"] is False
    assert context["default_name"] is None
    assert runtime.get_section_fov_position_context("sec_0")["calibration"] is None
    assert saved[-1].fov_position_calibrations == {}
    session.dispose()
