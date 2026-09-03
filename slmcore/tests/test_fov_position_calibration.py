from __future__ import annotations

import numpy as np

from slmcore import (
    DEFAULT_REGISTRIES,FOVPositionCalibration,ImageMeasurement,SLMGeometry,
    SLMIdentity,SLMRuntime,SLMStartupPreferences,SLMWorkspace,
)
from slmcore.application.startup_preferences import StartupPreferencesState
from slmcore.core.cgh import CGHResult
from slmcore.core.cgh.feedback.analysis import analyze_position
from slmcore.core.cgh.feedback.model import FeedbackMeasurement
from slmcore.core.cgh.localization import LocalizationResult
from slmcore.core.engine.section import split_slm_geometry


def _grid(n=5,span=0.25):
    values = np.linspace(-span,span,n)
    xx,yy = np.meshgrid(values,values,indexing="xy")
    return np.vstack([xx.ravel(),yy.ravel()])


def _synthetic_displacement(points):
    x,y = points
    return np.vstack([
        0.001 + 0.02*x - 0.01*y + 0.03*x*x + 0.01*x*y,
        -0.002 + 0.015*y + 0.005*x + 0.02*y*y - 0.008*x*y,
    ])


def _calibration(name="map",points=None):
    points = _grid() if points is None else np.asarray(points,dtype=np.float64)
    return FOVPositionCalibration.fit(
        name=name,slm_serial="SER123",section_key="sec_0",plane_name="sample",
        ideal_positions_kxy=points,
        total_displacements_kxy=_synthetic_displacement(points),
        degree=2,
    )


def test_quadratic_fov_fit_recovers_smooth_field_and_retains_samples():
    calibration = _calibration()
    probes = np.array([
        [-0.21,-0.03,0.17,0.23],
        [0.18,-0.11,0.02,-0.19],
    ])

    np.testing.assert_allclose(
        calibration.evaluate(probes),_synthetic_displacement(probes),atol=1e-12,
    )
    assert calibration.sample_count == 25
    assert calibration.rms_residual_kxy < 1e-12
    assert calibration.max_residual_kxy < 1e-12


def test_fov_coverage_allows_small_boundary_tolerance_but_flags_real_extrapolation():
    calibration = _calibration()
    diameter = np.linalg.norm(np.ptp(calibration.sample_positions_kxy,axis=1))
    # Default tolerance is 0.5% of coverage diameter.
    tiny = 0.001*diameter
    points = np.array([
        [0.25+tiny,0.35],
        [0.0,0.0],
    ])

    outside,maximum,tolerance = calibration.coverage_extrapolation(points)
    assert not outside[0]
    assert outside[1]
    assert maximum > tolerance > 0


def test_fov_store_is_scoped_by_slm_section_and_plane(tmp_path):
    workspace = SLMWorkspace(tmp_path)
    store = workspace.fov_position_calibration_store
    calibration = _calibration("sample map")

    path = store.save(calibration)
    assert "SER123".lower() in str(path).lower()
    assert store.list("SER123","sec_0","sample") == ("sample map",)
    assert store.list("OTHER","sec_0","sample") == ()
    assert store.list("SER123","sec_1","sample") == ()
    assert store.list("SER123","sec_0","pupil") == ()
    restored = store.load("SER123","sec_0","sample","sample map")
    np.testing.assert_allclose(restored.evaluate(_grid(3)),calibration.evaluate(_grid(3)))


def test_startup_preferences_persist_fov_default_by_section_and_plane():
    saved = []
    state = StartupPreferencesState(SLMStartupPreferences(),saved.append)
    state.set_default_fov_position_calibration("sec_0","sample","map A")

    assert state.default_fov_position_calibration("sec_0","sample") == "map A"
    payload = saved[-1].to_dict()
    assert payload["fov_position_calibrations"]["sec_0"]["planes"]["sample"] == "map A"
    restored = SLMStartupPreferences.from_dict(payload)
    assert restored.fov_position_calibrations["sec_0"].planes["sample"] == "map A"


def test_position_analysis_residual_is_applied_from_fov_baseline_not_ideal():
    ideal = np.array([
        [-0.2,0.2,-0.2,0.2],
        [-0.2,-0.2,0.2,0.2],
    ])
    baseline = ideal + np.array([[0.01],[0.02]])
    expected_px = np.array([[10.,30.,10.,30.],[10.,10.,30.,30.]])
    measured_px = expected_px.copy()  # zero residual feedback
    localization = LocalizationResult(
        target_type="multi_foci_vector",target_params={},parameters={},
        lattice_indices=np.array([[0,1,0,1],[0,0,1,1]]),
        crop_coord=(0,40,0,40),cropped_image=np.zeros((40,40)),
        expected_positions_px=expected_px,measured_positions_px=measured_px,
        period_x_px=20.,period_y_px=20.,offset_x_px=10.,offset_y_px=10.,
        diagnostics={"matched_mask":(True,True,True,True)},
    )
    measurement = FeedbackMeasurement(
        acquisition=ImageMeasurement(image=np.zeros((40,40)),source="test"),
        localization=localization,
    )

    analysis = analyze_position(
        measurement,ideal_positions_kxy=ideal,
        baseline_positions_kxy=baseline,parameters={},
    )
    np.testing.assert_allclose(analysis.correction_kxy,0.0,atol=1e-12)
    np.testing.assert_allclose(analysis.corrected_positions_kxy,baseline,atol=1e-12)


def _runtime():
    geometry = SLMGeometry(width=64,height=64,pixel_size_um=1.0)
    runtime = SLMRuntime(
        identity=SLMIdentity("slm","SER123"),geometry=geometry,
        section_geometries=split_slm_geometry(geometry,1),
        registries=DEFAULT_REGISTRIES,
    )
    runtime.apply_section_patch("sec_0",{
        ("cgh","active"):True,
        ("cgh","selected_target"):"multi_foci_vector",
        ("cgh","multi_foci_vector","params","n_foci_x"):2,
        ("cgh","multi_foci_vector","params","n_foci_y"):2,
    })
    job = runtime.prepare_section_base_cgh("sec_0")
    runtime.commit_section_cgh("sec_0",CGHResult(
        generation=job.generation,spec=job.spec,target_name=job.target_name,
        pattern=np.ones(job.spec.context.shape,dtype=np.complex128),
    ))
    return runtime


def test_runtime_fov_layer_changes_baseline_without_becoming_cgh_config_state():
    runtime = _runtime()
    before = runtime.get_section_fov_position_context("sec_0")
    ideal = before["ideal_positions_kxy"]
    calibration = FOVPositionCalibration.fit(
        name="runtime map",slm_serial="SER123",section_key="sec_0",
        plane_name="sample",ideal_positions_kxy=_grid(),
        total_displacements_kxy=_synthetic_displacement(_grid()),degree=2,
    )

    transition = runtime.set_section_fov_position_calibration("sec_0",calibration)
    assert transition is not None
    after = runtime.get_section_fov_position_context("sec_0")
    np.testing.assert_allclose(
        after["baseline_positions_kxy"],
        ideal+calibration.evaluate(ideal),
    )
    assert runtime.get_section_cgh_status("sec_0").result_state.value == "stale"

    # Persistent FOV selection is deliberately external to CGH config snapshots.
    config = runtime.create_config()
    restored = SLMRuntime.from_config(config,registries=DEFAULT_REGISTRIES)
    restored_context = restored.get_section_fov_position_context("sec_0")
    np.testing.assert_allclose(
        restored_context["baseline_positions_kxy"],
        restored_context["ideal_positions_kxy"],
    )


def test_fov_context_is_inert_without_selected_cgh_target():
    geometry = SLMGeometry(width=64,height=64,pixel_size_um=1.0)
    runtime = SLMRuntime(
        identity=SLMIdentity("slm","SER123"),geometry=geometry,
        section_geometries=split_slm_geometry(geometry,1),
        registries=DEFAULT_REGISTRIES,
    )
    context = runtime.get_section_fov_position_context("sec_0")
    assert context["supported"] is False
    assert context["ideal_positions_kxy"].shape == (2,0)

    calibration = _calibration("ready for later")
    transition = runtime.set_section_fov_position_calibration("sec_0",calibration)
    assert transition is None
    held = runtime.get_section_fov_position_context("sec_0")
    assert held["calibration"].name == "ready for later"
    assert held["baseline_positions_kxy"].shape == (2,0)


def test_fov_change_does_not_discard_feedback_on_unsupported_target():
    geometry = SLMGeometry(width=64,height=64,pixel_size_um=1.0)
    runtime = SLMRuntime(
        identity=SLMIdentity("slm","SER123"),geometry=geometry,
        section_geometries=split_slm_geometry(geometry,1),
        registries=DEFAULT_REGISTRIES,
    )
    runtime.apply_section_patch("sec_0",{
        ("cgh","active"):True,
        ("cgh","selected_target"):"multi_foci",
        ("cgh","multi_foci","params","n_foci_x"):2,
        ("cgh","multi_foci","params","n_foci_y"):2,
    })
    job = runtime.prepare_section_base_cgh("sec_0")
    runtime.commit_section_cgh("sec_0",CGHResult(
        generation=job.generation,spec=job.spec,target_name=job.target_name,
        pattern=np.ones(job.spec.context.shape,dtype=np.complex128),
    ))
    measurement = ImageMeasurement(
        image=np.zeros((64,64),dtype=np.float64),source="test",
    )
    runtime.set_section_feedback_measurement("sec_0",measurement)
    before = runtime.get_section_feedback_inspection("sec_0")
    assert before.measurement is not None

    transition = runtime.set_section_fov_position_calibration(
        "sec_0",_calibration("held map"),
    )
    assert transition is None
    after = runtime.get_section_feedback_inspection("sec_0")
    assert after.measurement is not None
    assert (
        after.measurement.acquisition.measurement_id
        == before.measurement.acquisition.measurement_id
    )
    assert runtime.get_section_fov_position_context("sec_0")["supported"] is False
