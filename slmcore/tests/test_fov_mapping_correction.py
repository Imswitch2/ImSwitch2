from __future__ import annotations

import json
import numpy as np

from slmcore import (
    DEFAULT_REGISTRIES,FOVMappingCorrection,ImageMeasurement,SLMGeometry,
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
    return FOVMappingCorrection.fit(
        name=name,slm_serial="SER123",section_key="sec_0",mapping_name="100x",base_mapping_signature="",
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


def test_fov_store_is_scoped_by_slm_section_and_mapping(tmp_path):
    workspace = SLMWorkspace(tmp_path)
    store = workspace.fov_mapping_correction_store
    calibration = _calibration("sample map")

    path = store.save(calibration)
    assert "SER123".lower() in str(path).lower()
    assert store.list("SER123","sec_0","100x") == ("sample map",)
    assert store.list("OTHER","sec_0","100x") == ()
    assert store.list("SER123","sec_1","100x") == ()
    assert store.list("SER123","sec_0","60x") == ()
    restored = store.load("SER123","sec_0","100x","sample map")
    np.testing.assert_allclose(restored.evaluate(_grid(3)),calibration.evaluate(_grid(3)))




def test_mapping_service_owns_fov_correction_persistence(tmp_path):
    from slmcore import MappingService,SLMIdentity

    workspace = SLMWorkspace(tmp_path)
    identity = SLMIdentity("slm","SER123")
    service = MappingService(
        workspace.mapping_store,workspace.fov_mapping_correction_store,lambda:identity,
    )
    correction = _calibration("field A")

    service.save_fov_correction(correction)
    assert service.fov_correction_names("sec_0","100x") == ("field A",)
    assert service.fov_correction_names("sec_0","60x") == ()
    loaded = service.load_fov_correction("sec_0","100x","field A")
    np.testing.assert_allclose(loaded.evaluate(_grid(3)),correction.evaluate(_grid(3)))
    service.delete_fov_correction("sec_0","100x","field A")
    assert service.fov_correction_names("sec_0","100x") == ()
    assert "fov_mapping_corrections" in str(workspace.fov_mapping_corrections_root)

def test_fov_mapping_correction_store_roundtrips_measurement_provenance(tmp_path):
    workspace = SLMWorkspace(tmp_path)
    points = _grid()
    correction = FOVMappingCorrection.fit(
        name="field A",slm_serial="SER123",section_key="sec_0",
        mapping_name="100x",base_mapping_signature="",
        ideal_positions_kxy=points,total_displacements_kxy=_synthetic_displacement(points),
        degree=2,detector_name="cam",provenance={
            "observation_plane":"Main",
            "measurement_source":"cam",
            "measurement_pixel_size_um":0.108,
            "source":"current_position_analysis",
        },
    )
    workspace.fov_mapping_correction_store.save(correction)
    restored = workspace.fov_mapping_correction_store.load(
        "SER123","sec_0","100x","field A"
    )
    assert restored.provenance["observation_plane"] == "Main"
    assert restored.provenance["measurement_source"] == "cam"
    assert restored.provenance["measurement_pixel_size_um"] == 0.108
    assert restored.provenance["source"] == "current_position_analysis"


def test_startup_preferences_persist_fov_default_by_section_and_mapping():
    saved = []
    state = StartupPreferencesState(SLMStartupPreferences(),saved.append)
    state.set_default_fov_correction("sec_0","sample","map A")

    assert state.default_fov_correction("sec_0","sample") == "map A"
    payload = saved[-1].to_dict()
    assert payload["fov_mapping_corrections"]["sec_0"]["mappings"]["sample"] == "map A"
    restored = SLMStartupPreferences.from_dict(payload)
    assert restored.fov_mapping_corrections["sec_0"].mappings["sample"] == "map A"


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
    before = runtime.get_section_fov_mapping_context("sec_0")
    ideal = before["ideal_positions_kxy"]
    calibration = FOVMappingCorrection.fit(
        name="runtime map",slm_serial="SER123",section_key="sec_0",
        mapping_name="100x",base_mapping_signature="",ideal_positions_kxy=_grid(),
        total_displacements_kxy=_synthetic_displacement(_grid()),degree=2,
    )

    transition = runtime.set_section_fov_mapping_correction("sec_0",calibration)
    assert transition is not None
    after = runtime.get_section_fov_mapping_context("sec_0")
    np.testing.assert_allclose(
        after["baseline_positions_kxy"],
        ideal+calibration.evaluate(ideal),
    )
    assert runtime.get_section_cgh_status("sec_0").result_state.value == "stale"

    # Persistent FOV selection is deliberately external to CGH config snapshots.
    config = runtime.create_config()
    restored = SLMRuntime.from_config(config,registries=DEFAULT_REGISTRIES)
    restored_context = restored.get_section_fov_mapping_context("sec_0")
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
    context = runtime.get_section_fov_mapping_context("sec_0")
    assert context["supported"] is False
    assert context["ideal_positions_kxy"].shape == (2,0)

    calibration = _calibration("ready for later")
    transition = runtime.set_section_fov_mapping_correction("sec_0",calibration)
    assert transition is None
    held = runtime.get_section_fov_mapping_context("sec_0")
    assert held["correction"].name == "ready for later"
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

    transition = runtime.set_section_fov_mapping_correction(
        "sec_0",_calibration("held map"),
    )
    assert transition is None
    after = runtime.get_section_feedback_inspection("sec_0")
    assert after.measurement is not None
    assert (
        after.measurement.acquisition.measurement_id
        == before.measurement.acquisition.measurement_id
    )
    assert runtime.get_section_fov_mapping_context("sec_0")["supported"] is False


def test_multifoci_fov_numerical_path_is_characterized_point_by_point():
    """Pin the Phase-1 numerical contract before FOV ownership moves."""
    runtime = _runtime()
    ideal_context = runtime.get_section_fov_mapping_context("sec_0")
    ideal = ideal_context["ideal_positions_kxy"]

    # The target resolver owns the ideal lattice. Keep its exact current output
    # explicit so later ownership refactors cannot silently move the correction
    # earlier into target resolution.
    expected_ideal = np.array([
        [-0.005859375, 0.005859375, -0.005859375, 0.005859375],
        [-0.005859375, -0.005859375, 0.005859375, 0.005859375],
    ])
    np.testing.assert_allclose(ideal, expected_ideal, atol=1e-12)
    assert ideal.shape[1] == 4

    # Fit a deterministic affine displacement field from exact samples. This is
    # deliberately non-uniform so the assertion proves correction is evaluated
    # independently at every resolved target position.
    samples = _grid()
    displacement = np.vstack([
        0.01 + 0.04 * samples[0] - 0.02 * samples[1],
        -0.015 + 0.01 * samples[0] + 0.03 * samples[1],
    ])
    calibration = FOVMappingCorrection.fit(
        name="characterization map", slm_serial="SER123", section_key="sec_0",
        mapping_name="100x",base_mapping_signature="", ideal_positions_kxy=samples,
        total_displacements_kxy=displacement, degree=1,
    )

    runtime.set_section_fov_mapping_correction("sec_0", calibration)
    corrected = runtime.get_section_fov_mapping_context("sec_0")
    expected_displacement = np.vstack([
        0.01 + 0.04 * expected_ideal[0] - 0.02 * expected_ideal[1],
        -0.015 + 0.01 * expected_ideal[0] + 0.03 * expected_ideal[1],
    ])

    np.testing.assert_allclose(
        corrected["ideal_positions_kxy"], expected_ideal, atol=1e-12,
    )
    np.testing.assert_allclose(
        corrected["displacement_kxy"], expected_displacement, atol=1e-12,
    )
    np.testing.assert_allclose(
        corrected["baseline_positions_kxy"],
        expected_ideal + expected_displacement,
        atol=1e-12,
    )
    assert corrected["baseline_positions_kxy"].shape[1] == 4


def test_base_mapping_signature_uses_numerics_and_geometry_not_provenance():
    from slmcore import SLMSectionCalibration
    from slmcore.core.calibration import base_mapping_signature

    geometry = {"key":"sec_0","x":0,"y":0,"width":64,"height":64}
    first = SLMSectionCalibration(
        matrix_2x2=((0.01,0.002),(-0.001,0.012)),
        mapping_name="100x",created_at="2026-01-01",source="first",
        section_geometry=geometry,metadata={"detector":"cam-a"},
    )
    provenance_only = first.copy()
    provenance_only.mapping_name = "renamed"
    provenance_only.created_at = "2027-04-05"
    provenance_only.source = "other"
    provenance_only.metadata = {"detector":"cam-b","note":"changed"}
    numerical_change = first.copy()
    numerical_change.matrix_2x2 = ((0.0101,0.002),(-0.001,0.012))

    assert base_mapping_signature(first) == base_mapping_signature(provenance_only)
    assert base_mapping_signature(first) != base_mapping_signature(numerical_change)


def test_base_mapping_signature_changes_with_section_geometry():
    from slmcore import SLMSectionCalibration
    from slmcore.core.calibration import base_mapping_signature

    first = SLMSectionCalibration(
        kx_per_um=0.01,ky_per_um=0.012,
        section_geometry={"key":"sec_0","x":0,"y":0,"width":64,"height":64},
    )
    changed = first.copy()
    changed.section_geometry = {
        "key":"sec_0","x":1,"y":0,"width":64,"height":64,
    }
    assert base_mapping_signature(first) != base_mapping_signature(changed)


def test_mapping_service_refits_saved_fov_samples_without_acquisition(tmp_path):
    from slmcore import MappingService,SLMIdentity

    workspace = SLMWorkspace(tmp_path)
    identity = SLMIdentity("slm","SER123")
    service = MappingService(
        workspace.mapping_store,workspace.fov_mapping_correction_store,lambda:identity,
    )
    original = _calibration("field A")
    service.save_fov_correction(original)

    refit = service.refit_fov_correction("sec_0","100x","field A",degree=1)

    assert refit.name == "field A"
    assert refit.degree == 1
    np.testing.assert_allclose(refit.sample_positions_kxy,original.sample_positions_kxy)
    np.testing.assert_allclose(refit.sample_displacements_kxy,original.sample_displacements_kxy)
    assert refit.provenance["refit_from_saved_samples"]["previous_degree"] == 2
    # Preview/refit is non-persistent until explicitly saved.
    assert service.load_fov_correction("sec_0","100x","field A").degree == 2


def test_mapping_service_renames_fov_correction_preserving_fit(tmp_path):
    from slmcore import MappingService,SLMIdentity

    workspace = SLMWorkspace(tmp_path)
    identity = SLMIdentity("slm","SER123")
    service = MappingService(
        workspace.mapping_store,workspace.fov_mapping_correction_store,lambda:identity,
    )
    original = _calibration("field A")
    service.save_fov_correction(original)

    renamed = service.rename_fov_correction("sec_0","100x","field A","field B")

    assert renamed.name == "field B"
    assert service.fov_correction_names("sec_0","100x") == ("field B",)
    restored = service.load_fov_correction("sec_0","100x","field B")
    np.testing.assert_allclose(restored.coefficients_kxy,original.coefficients_kxy)
    np.testing.assert_allclose(restored.sample_positions_kxy,original.sample_positions_kxy)


def test_fov_store_schema_v2_wraps_shared_transform(tmp_path):
    workspace=SLMWorkspace(tmp_path)
    correction=_calibration("schema v2")
    path=workspace.fov_mapping_correction_store.save(correction)
    payload=json.loads(path.read_text(encoding="utf-8"))

    assert payload["version"]==2
    assert payload["correction"]["transform"]["model"]=="polynomial"
    assert payload["correction"]["transform"]["polynomial_degree"]==2
    assert "coefficients_kxy" not in payload["correction"]


def test_fov_store_loads_v1_polynomial_and_rewrites_only_on_explicit_save(tmp_path):
    workspace=SLMWorkspace(tmp_path)
    store=workspace.fov_mapping_correction_store
    original=_calibration("legacy field")
    path=store.save(original)
    legacy={
        "name":original.name,
        "slm_serial":original.slm_serial,
        "section_key":original.section_key,
        "mapping_name":original.mapping_name,
        "base_mapping_signature":original.base_mapping_signature,
        "sample_positions_kxy":original.sample_positions_kxy.tolist(),
        "sample_displacements_kxy":original.sample_displacements_kxy.tolist(),
        "coefficients_kxy":original.coefficients_kxy.tolist(),
        "normalization_center_kxy":original.normalization_center_kxy.tolist(),
        "normalization_scale_kxy":original.normalization_scale_kxy.tolist(),
        "model":"polynomial",
        "degree":original.degree,
        "detector_name":original.detector_name,
        "rms_residual_kxy":original.rms_residual_kxy,
        "max_residual_kxy":original.max_residual_kxy,
        "provenance":dict(original.provenance),
        "created_at":original.created_at,
    }
    path.write_text(json.dumps({"version":1,"correction":legacy}),encoding="utf-8")

    restored=store.load("SER123","sec_0","100x","legacy field")
    assert restored.model=="polynomial"
    assert restored.degree==2
    np.testing.assert_allclose(restored.evaluate(_grid(4)),original.evaluate(_grid(4)))
    # Loading migrates in memory and does not mutate a user's saved file.
    assert json.loads(path.read_text(encoding="utf-8"))["version"]==1

    store.save(restored,overwrite=True)
    assert json.loads(path.read_text(encoding="utf-8"))["version"]==2


def test_saved_fov_samples_can_be_refit_across_shared_transform_models(tmp_path):
    from slmcore import MappingService,SLMIdentity

    workspace=SLMWorkspace(tmp_path)
    service=MappingService(
        workspace.mapping_store,workspace.fov_mapping_correction_store,
        lambda:SLMIdentity("slm","SER123"),
    )
    original=_calibration("field A")
    service.save_fov_correction(original)

    for model in ("translation","rigid","affine"):
        refit=service.refit_fov_correction("sec_0","100x","field A",model=model)
        assert refit.model==model
        assert refit.degree is None
        np.testing.assert_allclose(refit.sample_positions_kxy,original.sample_positions_kxy)
        np.testing.assert_allclose(refit.sample_displacements_kxy,original.sample_displacements_kxy)

    polynomial=service.refit_fov_correction(
        "sec_0","100x","field A",model="polynomial",degree=3,
    )
    assert polynomial.model=="polynomial"
    assert polynomial.degree==3
