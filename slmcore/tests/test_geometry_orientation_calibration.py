from __future__ import annotations

import json

import numpy as np
import pytest

from slmcore.workspace import ObservationPlaneDefinition,ObservationPlaneStore

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



def _set_feedback_observation_planes(session,*names):
    import tempfile
    store=ObservationPlaneStore(tempfile.mkdtemp(prefix="slmcore-observation-test-"))
    for name in names:
        store.add(ObservationPlaneDefinition(name=name))
    session.observation_planes.store=store
    if names:
        session.feedback.select_observation_plane("sec_0",names[0])
    return store

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
        slm_serial="SER123",section_key="sec_0",observation_plane_name="sample",
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



def test_geometry_orientation_store_migrates_v1_on_load(tmp_path):
    store=SLMGeometryOrientationCalibrationStore(tmp_path)
    identity=SLMIdentity("slm","SER123")
    path=store._path(identity,"sec_0","sample")
    path.parent.mkdir(parents=True,exist_ok=True)
    calibration=GeometryOrientationCalibration(
        slm_serial="SER123",section_key="sec_0",observation_plane_name="sample",
        orientation="rotate_90_cw",detector_name="cam",grid_x=5,grid_y=4,
        period_x_px=12.5,period_y_px=14.0,score=0.98,
        confidence_margin=0.31,matched_count=20,
    )
    legacy=calibration.to_dict()
    legacy["plane_name"]=legacy.pop("observation_plane_name")
    payload={"version":1,"calibration":legacy}
    path.write_text(json.dumps(payload),encoding="utf-8")

    loaded=store.load(identity,"sec_0","sample")

    assert loaded.observation_plane_name == "sample"
    rewritten=json.loads(path.read_text(encoding="utf-8"))
    assert rewritten["version"] == 2
    assert rewritten["calibration"]["observation_plane_name"] == "sample"
    assert "plane_name" not in rewritten["calibration"]


def test_geometry_orientation_store_rejects_unknown_versions(tmp_path):
    store=SLMGeometryOrientationCalibrationStore(tmp_path)
    identity=SLMIdentity("slm","SER123")
    path=store._path(identity,"sec_0","sample")
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({"version":3,"calibration":{}}),encoding="utf-8")

    with pytest.raises(ValueError,match="Unsupported geometry orientation calibration version"):
        store.load(identity,"sec_0","sample")

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
    _set_feedback_observation_planes(session,"sample","fourier")
    session.feedback.select_observation_plane("sec_0",active_plane["value"])
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
    session.geometry_calibration.compute_target(
        "sec_0","sample",grid_x=5,grid_y=4,period_x_px=12.0,period_y_px=15.0,
        source="cam",on_complete=lambda success,error:completed.append((success,error)),
    )

    assert completed == [(True,None)]
    assert len(uploads) == 1
    np.testing.assert_array_equal(uploads[-1],calibration_frame)
    assert dispatcher.pending == []
    context=session.geometry_calibration.context("sec_0","sample")
    assert context["frame_active"] is True
    assert context["measurement"] is None
    assert context["localization"] is None

    session.geometry_calibration.acquire("sec_0","sample","cam")
    assert len(dispatcher.pending) == 1
    assert len(uploads) == 1
    measurement=create_image_measurement(
        np.zeros((64,64),dtype=np.float64),source="detector",detector="cam",
    )
    dispatcher.complete(measurement)

    context=session.geometry_calibration.context("sec_0","sample")
    assert context["measurement"] is measurement
    assert context["localization"] is None
    assert context["frame_active"] is True
    assert len(uploads) == 1

    active_plane["value"]="fourier"
    session.feedback.select_observation_plane("sec_0","fourier")
    switched=session.geometry_calibration.context("sec_0","fourier")
    assert switched["frame_active"] is False
    assert switched["any_frame_active"] is True
    assert switched["active_frame_observation_plane"] == "sample"

    session.geometry_calibration.finish("sec_0")
    active_plane["value"]="sample"
    session.feedback.select_observation_plane("sec_0","sample")
    context=session.geometry_calibration.context("sec_0","sample")
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
    _set_feedback_observation_planes(session,"sample")
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
    session.geometry_calibration.compute_target(
        "sec_0","sample",grid_x=5,grid_y=4,period_x_px=12.0,period_y_px=15.0,
        source="cam",
    )
    before=session.geometry_calibration.context("sec_0","sample")
    session.geometry_calibration.compute_target(
        "sec_0","sample",grid_x=7,grid_y=6,period_x_px=20.0,period_y_px=21.0,
        source="cam",
    )
    after=session.geometry_calibration.context("sec_0","sample")

    assert before["frame_active"] is True
    assert after["frame_active"] is True
    assert (after["grid_x"],after["grid_y"]) == (5,4)
    assert (after["period_x_px"],after["period_y_px"]) == (12.0,15.0)
    assert len(uploads) == 1


def test_geometry_orientation_store_scope_includes_slm_section_and_plane(tmp_path):
    store=SLMGeometryOrientationCalibrationStore(tmp_path)
    calibration=GeometryOrientationCalibration(
        slm_serial="SER123",section_key="sec_0",observation_plane_name="sample",
        orientation="identity",detector_name="cam",grid_x=5,grid_y=4,
        period_x_px=12.0,period_y_px=12.0,score=1.0,
        confidence_margin=0.5,matched_count=20,
    )
    store.save(calibration)

    assert store.exists(SLMIdentity("slm","SER123"),"sec_0","sample")
    assert not store.exists(SLMIdentity("slm","OTHER"),"sec_0","sample")
    assert not store.exists(SLMIdentity("slm","SER123"),"sec_1","sample")
    assert not store.exists(SLMIdentity("slm","SER123"),"sec_0","fourier")


def _saved_geometry(
    *, section="sec_0", plane="sample", orientation="identity", serial="SER123",
):
    return GeometryOrientationCalibration(
        slm_serial=serial,section_key=section,observation_plane_name=plane,
        orientation=orientation,detector_name="cam",grid_x=5,grid_y=4,
        period_x_px=12.0,period_y_px=14.0,score=0.98,
        confidence_margin=0.31,matched_count=20,
    )


def test_observation_geometry_service_scopes_section_and_observation_plane(tmp_path):
    from slmcore import ObservationGeometryService

    service=ObservationGeometryService(
        SLMIdentity("slm","SER123"),SLMGeometryOrientationCalibrationStore(tmp_path),
    )
    service.save(_saved_geometry(plane="sample",orientation="identity"))
    service.save(_saved_geometry(plane="fourier",orientation="rotate_90_cw"))
    service.save(_saved_geometry(
        section="sec_1",plane="sample",orientation="flip_horizontal",
    ))

    assert service.is_calibrated("sec_0","sample")
    assert service.calibrated_orientation("sec_0","sample") is FeedbackOrientation.IDENTITY
    assert service.calibrated_orientation("sec_0","fourier") is FeedbackOrientation.ROTATE_90_CW
    assert service.calibrated_orientation("sec_1","sample") is FeedbackOrientation.FLIP_HORIZONTAL
    assert service.calibration("sec_1","fourier") is None


def test_observation_geometry_service_is_usable_without_feedback(tmp_path):
    from slmcore import ObservationGeometryService

    service=ObservationGeometryService(
        SLMIdentity("slm","SER123"),SLMGeometryOrientationCalibrationStore(tmp_path),
    )
    calibration=_saved_geometry(orientation="rotate_180")
    assert not service.exists("sec_0","sample")
    assert service.save(calibration) is calibration
    assert service.load("sec_0","sample").orientation is FeedbackOrientation.ROTATE_180
    service.delete("sec_0","sample")
    assert not service.is_calibrated("sec_0","sample")


def test_feedback_reads_geometry_saved_through_shared_observation_service(tmp_path):
    from slmcore import SLMSession

    geometry=SLMGeometry(width=64,height=64,pixel_size_um=1.0)
    runtime=SLMRuntime(
        identity=SLMIdentity("slm","SER123"),geometry=geometry,
        section_geometries=split_slm_geometry(geometry,1),
        registries=DEFAULT_REGISTRIES,
    )
    session=SLMSession(
        runtime=runtime,
        geometry_orientation_calibration_store=SLMGeometryOrientationCalibrationStore(tmp_path),
    )
    _set_feedback_observation_planes(session,"sample")
    saved=_saved_geometry(orientation="rotate_90_ccw")
    session.observation_geometry.save(saved)

    assert session.feedback.geometry_orientation_calibration("sec_0") == saved
    context=session.geometry_calibration.context("sec_0","sample")
    assert context["calibrated"] is True
    assert session.feedback.feedback_orientation("sec_0") is FeedbackOrientation.ROTATE_90_CCW


def test_observation_geometry_persistence_does_not_change_cgh(tmp_path):
    from slmcore import SLMSession

    geometry=SLMGeometry(width=64,height=64,pixel_size_um=1.0)
    runtime=SLMRuntime(
        identity=SLMIdentity("slm","SER123"),geometry=geometry,
        section_geometries=split_slm_geometry(geometry,1),
        registries=DEFAULT_REGISTRIES,
    )
    session=SLMSession(
        runtime=runtime,
        geometry_orientation_calibration_store=SLMGeometryOrientationCalibrationStore(tmp_path),
    )
    state_before=runtime.get_section_state_copy("sec_0").to_dict()
    frame_before=np.array(runtime.artifacts.eightbit,copy=True)
    status_before=runtime.get_section_cgh_status("sec_0")

    session.observation_geometry.save(_saved_geometry())
    session.observation_geometry.delete("sec_0","sample")

    assert runtime.get_section_state_copy("sec_0").to_dict() == state_before
    np.testing.assert_array_equal(runtime.artifacts.eightbit,frame_before)
    status_after=runtime.get_section_cgh_status("sec_0")
    assert status_after == status_before


def test_shared_geometry_calibration_service_has_explicit_observation_context(tmp_path):
    from slmcore import SLMSession

    geometry=SLMGeometry(width=64,height=64,pixel_size_um=1.0)
    runtime=SLMRuntime(
        identity=SLMIdentity("slm","SER123"),geometry=geometry,
        section_geometries=split_slm_geometry(geometry,1),
        registries=DEFAULT_REGISTRIES,
    )
    session=SLMSession(
        runtime=runtime,
        geometry_orientation_calibration_store=SLMGeometryOrientationCalibrationStore(tmp_path),
    )
    _set_feedback_observation_planes(session,"sample","fourier")
    session.feedback.select_observation_plane("sec_0","sample")

    context=session.geometry_calibration.context("sec_0","fourier")

    assert context["observation_plane_name"] == "fourier"
    assert session.feedback.active_observation_plane("sec_0") == "sample"
    assert session.geometry_calibration.observation_geometry is session.observation_geometry


def test_feedback_consumes_geometry_but_does_not_expose_calibration_workflow():
    from slmcore import SLMSession

    geometry=SLMGeometry(width=64,height=64,pixel_size_um=1.0)
    runtime=SLMRuntime(
        identity=SLMIdentity("slm","SER123"),geometry=geometry,
        section_geometries=split_slm_geometry(geometry,1),
        registries=DEFAULT_REGISTRIES,
    )
    session=SLMSession(runtime=runtime)

    assert hasattr(session,"geometry_calibration")
    assert not hasattr(session.feedback,"compute_geometry_orientation_calibration_target")
    assert not hasattr(session.feedback,"save_geometry_orientation_calibration")


def test_shared_geometry_calibration_service_saves_manual_geometry(tmp_path):
    from slmcore import SLMSession

    geometry=SLMGeometry(width=64,height=64,pixel_size_um=1.0)
    runtime=SLMRuntime(
        identity=SLMIdentity("slm","SER123"),geometry=geometry,
        section_geometries=split_slm_geometry(geometry,1),
        registries=DEFAULT_REGISTRIES,
    )
    session=SLMSession(
        runtime=runtime,
        geometry_orientation_calibration_store=SLMGeometryOrientationCalibrationStore(tmp_path),
    )
    _set_feedback_observation_planes(session,"sample")

    saved=session.geometry_calibration.save_manual(
        "sec_0","sample",FeedbackOrientation.ROTATE_90_CW,
    )

    assert saved.protocol == "manual_orientation_v1"
    assert saved.orientation is FeedbackOrientation.ROTATE_90_CW
    assert saved.provenance["method"] == "manual"
    assert session.observation_geometry.is_calibrated("sec_0","sample")
    loaded=session.observation_geometry.calibration("sec_0","sample")
    assert loaded is not None
    assert loaded.orientation is FeedbackOrientation.ROTATE_90_CW
