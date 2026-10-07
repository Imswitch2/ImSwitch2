from __future__ import annotations

import numpy as np
import pytest

from slmcore import (
    DEFAULT_REGISTRIES,GeometryOrientationCalibration,ImageMeasurement,
    ObservationPlaneDefinition,SLMGeometry,SLMIdentity,SLMRuntime,
    SLMSectionCalibration,SLMSession,SLMStartupPreferences,SLMWorkspace,
)
from slmcore.application.startup_preferences import StartupPreferencesState
from slmcore.core.calibration import section_geometry_to_dict
from slmcore.core.cgh import CGHResult
from slmcore.core.cgh.localization import LocalizationResult
from slmcore.core.engine.section import split_slm_geometry


def _runtime():
    geometry=SLMGeometry(width=96,height=96,pixel_size_um=1.0)
    runtime=SLMRuntime(
        identity=SLMIdentity("slm","SER123"),geometry=geometry,
        section_geometries=split_slm_geometry(geometry,1),registries=DEFAULT_REGISTRIES,
    )
    runtime.apply_section_patch("sec_0",{
        ("cgh","active"):True,
        ("cgh","selected_target"):"multi_foci_vector",
        ("cgh","multi_foci_vector","params","n_foci_x"):3,
        ("cgh","multi_foci_vector","params","n_foci_y"):3,
    })
    job=runtime.prepare_section_base_cgh("sec_0")
    runtime.commit_section_cgh("sec_0",CGHResult(
        generation=job.generation,spec=job.spec,target_name=job.target_name,
        pattern=np.ones(job.spec.context.shape,dtype=np.complex128),
    ))
    return runtime


def _session(tmp_path):
    runtime=_runtime(); workspace=SLMWorkspace(tmp_path)
    mapping=workspace.mapping_store.add({"name":"100x"}).name
    workspace.calibration_store.save_calibration(
        runtime.identity,"sec_0",mapping,
        SLMSectionCalibration(
            kx_per_um=0.01,ky_per_um=0.012,mapping_name=mapping,
            section_geometry=section_geometry_to_dict(runtime.get_section_geometry("sec_0")),
        ),
    )
    workspace.observation_plane_store.add(ObservationPlaneDefinition(
        name="Main",preferred_detector="cam",default_pixel_size_um=0.2,
    ))
    workspace.geometry_orientation_calibration_store.save(
        GeometryOrientationCalibration(
            slm_serial=runtime.identity.serial_number,section_key="sec_0",
            observation_plane_name="Main",orientation="identity",detector_name="cam",
            grid_x=3,grid_y=3,period_x_px=20.0,period_y_px=20.0,
            score=0.99,confidence_margin=0.3,matched_count=9,
        )
    )
    saved=[]
    preferences=StartupPreferencesState(SLMStartupPreferences(),saved.append)
    session=SLMSession(
        runtime=runtime,calibration_store=workspace.calibration_store,
        mapping_store=workspace.mapping_store,
        fov_mapping_correction_store=workspace.fov_mapping_correction_store,
        observation_plane_store=workspace.observation_plane_store,
        geometry_orientation_calibration_store=workspace.geometry_orientation_calibration_store,
        startup_preferences=preferences,
    )
    session.calibration.select_mapping(
        session.calibration.prepare_mapping_selection("sec_0",mapping)
    )
    return session,mapping,workspace


def _localization():
    xx,yy=np.meshgrid(np.arange(3),np.arange(3),indexing="xy")
    lattice=np.vstack([xx.ravel(),yy.ravel()])
    expected=np.vstack([20.0+20.0*lattice[0],20.0+20.0*lattice[1]])
    # Smooth detector-space distortion around the globally fitted lattice.
    measured=expected+np.vstack([
        0.3+0.02*(expected[0]-40.0),
        -0.2+0.015*(expected[1]-40.0),
    ])
    return LocalizationResult(
        target_type="multi_foci_vector",target_params={},parameters={},
        lattice_indices=lattice,crop_coord=(0,96,0,96),cropped_image=np.zeros((96,96)),
        expected_positions_px=expected,measured_positions_px=measured,
        period_x_px=20.0,period_y_px=20.0,offset_x_px=20.0,offset_y_px=20.0,
        diagnostics={
            "matched_mask":tuple(True for _ in range(9)),
            "affine_linear":((20.0,0.0),(0.0,20.0)),
            "affine_translation":(20.0,20.0),
            "rms_residual_px":0.0,
        },
    )


def test_dedicated_fov_workflow_saves_mapping_owned_correction_without_feedback(tmp_path,monkeypatch):
    session,mapping,_workspace=_session(tmp_path)
    service=session.fov_calibration
    feedback_selection=session.feedback.position_reference_context("sec_0").selection
    state=service.ensure_reference("sec_0",mapping)
    assert state.observation_plane_name=="Main"
    measurement=ImageMeasurement(
        np.zeros((96,96)),source="file",pixel_size_um=0.2,
    )
    service.commit_measurement("sec_0",mapping,measurement)
    monkeypatch.setattr(
        "slmcore.application.fov_calibration.localize_measurement",
        lambda *_args,**_kwargs:_localization(),
    )
    state=service.run_localization("sec_0",mapping,state.localization_parameters)
    assert state.editable_geometry is not None
    service.fit_center("sec_0",mapping)
    correction=service.fit_candidate("sec_0",mapping,degree=1)
    assert correction.mapping_name==mapping
    assert correction.provenance["workflow"]=="editable_lattice_fov_calibration"
    assert correction.provenance["observation_plane"]=="Main"
    assert correction.provenance["desired_positions_source"]=="editable_lattice"

    saved=service.save_candidate(
        "sec_0",mapping,"dedicated",set_default=True,
    )
    assert session.mappings.fov_correction_names("sec_0",mapping)==("dedicated",)
    assert session.startup_preferences.default_fov_correction("sec_0",mapping)=="dedicated"
    np.testing.assert_allclose(
        session.mappings.load_fov_correction("sec_0",mapping,"dedicated").sample_displacements_kxy,
        saved.sample_displacements_kxy,
    )
    # The workflow is application-owned and does not consume Position Feedback state.
    assert session.feedback.position_reference_context("sec_0").selection == feedback_selection



def test_dedicated_fov_fit_excludes_inferred_samples(tmp_path,monkeypatch):
    from dataclasses import replace

    from slmcore.core.cgh.localization import infer_missing_localization

    session,mapping,_workspace=_session(tmp_path)
    service=session.fov_calibration
    state=service.ensure_reference("sec_0",mapping)
    service.commit_measurement(
        "sec_0",mapping,ImageMeasurement(np.zeros((96,96)),source="file",pixel_size_um=0.2),
    )
    full=_localization()
    diagnostics=dict(full.diagnostics)
    matched=list(diagnostics["matched_mask"]); matched[4]=False
    diagnostics.update({
        "matched_mask":tuple(matched),
        "matched_count":8,
        "inferred_count":0,
        "missing_count":1,
    })
    inferred=infer_missing_localization(replace(full,diagnostics=diagnostics))
    monkeypatch.setattr(
        "slmcore.application.fov_calibration.localize_measurement",
        lambda *_args,**_kwargs:inferred,
    )
    service.run_localization("sec_0",mapping,state.localization_parameters)
    correction=service.fit_candidate("sec_0",mapping,degree=1)

    assert correction.sample_positions_kxy.shape[1] == 8
    assert correction.provenance["matched_sample_count"] == 8
    assert correction.provenance["excluded_inferred_count"] == 1
    assert correction.provenance["excluded_unresolved_count"] == 0
    session.dispose()


def test_dedicated_fov_workflow_reports_missing_observation_geometry(tmp_path,monkeypatch):
    session,mapping,workspace=_session(tmp_path)
    workspace.geometry_orientation_calibration_store.delete("SER123","sec_0","Main")
    service=session.fov_calibration
    state=service.ensure_reference("sec_0",mapping)
    service.commit_measurement(
        "sec_0",mapping,ImageMeasurement(np.zeros((96,96)),source="file",pixel_size_um=0.2),
    )
    monkeypatch.setattr(
        "slmcore.application.fov_calibration.localize_measurement",
        lambda *_args,**_kwargs:_localization(),
    )
    with pytest.raises(RuntimeError,match=r"Geometry not calibrated for Section sec_0 × Plane Main"):
        service.run_localization("sec_0",mapping,state.localization_parameters)
