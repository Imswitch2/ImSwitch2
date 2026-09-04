from __future__ import annotations

import numpy as np

from slmcore import DEFAULT_REGISTRIES,SLMGeometry,SLMIdentity,SLMRuntime
from slmcore.core.cgh import CGHResult
from slmcore.core.cgh.feedback import (
    FeedbackOrientation,GeometryOrientationCalibration,
    analyze_geometry_orientation,geometry_orientation_code,
    orientation_permutation,
)
from slmcore.core.cgh.localization import LocalizationResult
from slmcore.core.cgh.localization.lattice import rectangular_lattice_indices
from slmcore.core.engine.section import split_slm_geometry
from slmcore.core.measurement import create_image_measurement
from slmcore.workspace import SLMGeometryOrientationCalibrationStore


def _localization_for_orientation(orientation: FeedbackOrientation):
    indices=rectangular_lattice_indices(5,4)
    target=geometry_orientation_code(indices)
    count=indices.shape[1]
    positions=np.empty((2,count),dtype=np.float64)
    positions[0]=20+(indices[0]-indices[0].min())*12
    positions[1]=20+(indices[1]-indices[1].min())*12
    permutation=orientation_permutation(indices,orientation)
    physical_powers=np.zeros(count,dtype=np.float64)
    physical_powers[permutation]=target
    image=np.zeros((100,100),dtype=np.float64)
    for index,(x,y) in enumerate(positions.T):
        image[int(y),int(x)]=1000.0*physical_powers[index]
    localization=LocalizationResult(
        target_type="multi_foci_vector",target_params={},parameters={},
        lattice_indices=indices,crop_coord=(0,100,0,100),cropped_image=image,
        expected_positions_px=positions,measured_positions_px=positions,
        period_x_px=12.0,period_y_px=12.0,offset_x_px=20.0,offset_y_px=20.0,
        diagnostics={"matched_mask":np.ones(count,dtype=bool)},
    )
    return create_image_measurement(image,source="test"),localization,target


def test_geometry_orientation_code_discriminates_all_eight_mappings():
    for orientation in FeedbackOrientation:
        measurement,localization,target=_localization_for_orientation(orientation)
        analysis=analyze_geometry_orientation(
            measurement,localization,target,integration_size_px=3,
        )
        assert analysis.accepted
        assert analysis.orientation is orientation
        assert analysis.score > 0.99
        assert analysis.confidence_margin > 0.12


def test_geometry_orientation_store_is_authoritative_per_scope(tmp_path):
    store=SLMGeometryOrientationCalibrationStore(tmp_path)
    identity=SLMIdentity("slm","SER123")
    calibration=GeometryOrientationCalibration(
        slm_serial="SER123",section_key="sec_0",plane_name="sample",
        orientation="rotate_90_cw",detector_name="cam",grid_x=5,grid_y=4,
        period_x_px=12.5,period_y_px=14.0,score=0.98,
        confidence_margin=0.31,matched_count=20,
    )
    store.save(calibration)
    assert store.exists(identity,"sec_0","sample")
    loaded=store.load(identity,"sec_0","sample")
    assert loaded.orientation is FeedbackOrientation.ROTATE_90_CW
    assert loaded.detector_name == "cam"
    assert not store.exists(identity,"sec_0","fourier")
    store.delete(identity,"sec_0","sample")
    assert not store.exists(identity,"sec_0","sample")


def test_geometry_calibration_cgh_is_detached_and_preserves_section_stack():
    geometry=SLMGeometry(width=64,height=64,pixel_size_um=1.0)
    runtime=SLMRuntime(
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
    base_job=runtime.prepare_section_base_cgh("sec_0")
    runtime.commit_section_cgh("sec_0",CGHResult(
        generation=base_job.generation,spec=base_job.spec,
        target_name=base_job.target_name,
        pattern=np.ones(base_job.spec.context.shape,dtype=np.complex128),
    ))
    state_before=runtime.get_section_state_copy("sec_0")
    frame_before=np.array(runtime.artifacts.eightbit,copy=True)
    status_before=runtime.get_section_cgh_status("sec_0")

    candidate,job=runtime.prepare_section_geometry_orientation_cgh(
        "sec_0",grid_x=5,grid_y=4,period_x_px=18.0,period_y_px=21.0,
    )

    assert runtime.get_section_state_copy("sec_0").to_dict() == state_before.to_dict()
    np.testing.assert_array_equal(runtime.artifacts.eightbit,frame_before)
    assert runtime.get_section_cgh_status("sec_0").result_state is status_before.result_state
    assert candidate.state.optics.to_dict() == state_before.optics.to_dict()
    assert candidate.state.aberrations.to_dict() == state_before.aberrations.to_dict()
    assert candidate.state.corrections.to_dict() == state_before.corrections.to_dict()
    assert job.spec.target_params["n_foci_x"] == 5
    assert job.spec.target_params["n_foci_y"] == 4
    assert job.spec.target_params["period_x_px"] == 18.0
    assert job.spec.target_params["period_y_px"] == 21.0
    assert np.unique(np.round(job.resolution.spot_intensities,8)).size == 20


class _GeometryRequest:
    def __init__(self):
        self._active=True

    @property
    def active(self):
        return self._active

    def cancel(self):
        self._active=False


class _GeometryDispatcher:
    available=True

    def __init__(self):
        self.pending=[]

    def available_sources(self,section_key):
        return ("cam",)

    def preferred_source(self,section_key,available):
        return available[0] if available else None

    def acquire(self,section_key,source,*,metadata,on_result,on_error):
        request=_GeometryRequest()
        self.pending.append((request,on_result,on_error,metadata))
        return request

    def complete(self,measurement,index=-1):
        request,on_result,_on_error,_metadata=self.pending[index]
        request._active=False
        on_result(measurement)


def test_geometry_calibration_workflow_is_manual_and_restores_only_on_finish(monkeypatch):
    from slmcore import SLMSession
    from slmcore.host import SLMDeviceProvider,SLMHostServices

    geometry=SLMGeometry(width=64,height=64,pixel_size_um=1.0)
    runtime=SLMRuntime(
        identity=SLMIdentity("slm","SER123"),geometry=geometry,
        section_geometries=split_slm_geometry(geometry,1),
        registries=DEFAULT_REGISTRIES,
    )
    dispatcher=_GeometryDispatcher()
    uploads=[]
    session=SLMSession(
        runtime=runtime,
        measurement_dispatcher=dispatcher,
        host_services=SLMHostServices(
            device=SLMDeviceProvider(
                upload_frame=lambda frame:uploads.append(np.array(frame,copy=True))
            ),
        ),
    )
    active_plane={"value":"sample"}
    session.calibration.active_plane=lambda _section:active_plane["value"]
    calibration_frame=np.full(runtime.geometry.shape,37,dtype=np.uint8)

    def fake_compute(
        section_key,*,grid_x,grid_y,period_x_px,period_y_px,on_finished,
    ):
        assert section_key == "sec_0"
        on_finished(True,None,{
            "frame":calibration_frame,
            "localization_context":{},
        })
        return True

    monkeypatch.setattr(session,"compute_geometry_orientation_cgh",fake_compute)
    completed=[]
    session.feedback.compute_geometry_orientation_calibration_target(
        "sec_0",grid_x=5,grid_y=4,period_x_px=12.0,period_y_px=15.0,
        source="cam",on_complete=lambda success,error:completed.append((success,error)),
    )

    assert completed == [(True,None)]
    assert len(uploads) == 1
    np.testing.assert_array_equal(uploads[-1],calibration_frame)
    assert dispatcher.pending == []
    context=session.feedback.geometry_orientation_calibration_context("sec_0")
    assert context["frame_active"] is True
    assert context["measurement"] is None
    assert context["localization"] is None

    session.feedback.acquire_geometry_orientation_calibration("sec_0","cam")
    assert len(dispatcher.pending) == 1
    assert len(uploads) == 1
    measurement=create_image_measurement(
        np.zeros((64,64),dtype=np.float64),source="detector",detector="cam",
    )
    dispatcher.complete(measurement)

    context=session.feedback.geometry_orientation_calibration_context("sec_0")
    assert context["measurement"] is measurement
    assert context["localization"] is None
    assert context["frame_active"] is True
    assert len(uploads) == 1

    active_plane["value"]="fourier"
    switched=session.feedback.geometry_orientation_calibration_context("sec_0")
    assert switched["frame_active"] is False
    assert switched["any_frame_active"] is True
    assert switched["active_frame_plane"] == "sample"

    session.feedback.finish_geometry_orientation_calibration("sec_0")
    active_plane["value"]="sample"
    context=session.feedback.geometry_orientation_calibration_context("sec_0")
    assert context["frame_active"] is False
    assert context["measurement"] is measurement
    assert len(uploads) == 2
    np.testing.assert_array_equal(uploads[-1],runtime.artifacts.eightbit)


def test_geometry_calibration_recompute_failure_keeps_previous_active_target(monkeypatch):
    from slmcore import SLMSession
    from slmcore.host import SLMDeviceProvider,SLMHostServices

    geometry=SLMGeometry(width=64,height=64,pixel_size_um=1.0)
    runtime=SLMRuntime(
        identity=SLMIdentity("slm","SER123"),geometry=geometry,
        section_geometries=split_slm_geometry(geometry,1),
        registries=DEFAULT_REGISTRIES,
    )
    uploads=[]
    session=SLMSession(
        runtime=runtime,
        host_services=SLMHostServices(
            device=SLMDeviceProvider(
                upload_frame=lambda frame:uploads.append(np.array(frame,copy=True))
            ),
        ),
    )
    session.calibration.active_plane=lambda _section:"sample"
    calls={"count":0}

    def fake_compute(
        section_key,*,grid_x,grid_y,period_x_px,period_y_px,on_finished,
    ):
        calls["count"]+=1
        if calls["count"] == 1:
            on_finished(True,None,{
                "frame":np.full(runtime.geometry.shape,11,dtype=np.uint8),
                "localization_context":{},
            })
        else:
            on_finished(False,RuntimeError("synthetic compute failure"),None)
        return True

    monkeypatch.setattr(session,"compute_geometry_orientation_cgh",fake_compute)
    session.feedback.compute_geometry_orientation_calibration_target(
        "sec_0",grid_x=5,grid_y=4,period_x_px=12.0,period_y_px=15.0,
        source="cam",
    )
    before=session.feedback.geometry_orientation_calibration_context("sec_0")
    session.feedback.compute_geometry_orientation_calibration_target(
        "sec_0",grid_x=7,grid_y=6,period_x_px=20.0,period_y_px=21.0,
        source="cam",
    )
    after=session.feedback.geometry_orientation_calibration_context("sec_0")

    assert before["frame_active"] is True
    assert after["frame_active"] is True
    assert (after["grid_x"],after["grid_y"]) == (5,4)
    assert (after["period_x_px"],after["period_y_px"]) == (12.0,15.0)
    assert len(uploads) == 1
