import json

import numpy as np
import pytest

from slmcore import (
    ObservationFrame,ObservationRegistrationCalibration,
    ObservationRegistrationService,ObservationRegistrationStore,
    SLMGeometry,SLMIdentity,SLMRuntime,SLMSession,SLMWorkspace,
    fit_observation_registration,split_slm_geometry,DEFAULT_REGISTRIES,
)


def _frame(
    section,
    *,
    serial="SER123",
    plane="Intermediate",
    detector="cam",
    shape=(200,300),
    pixel=0.1,
):
    return ObservationFrame(
        slm_serial=serial,
        section_key=section,
        observation_plane_name=plane,
        detector_name=detector,
        image_shape=shape,
        pixel_size_um=pixel,
    )


def _grid(nx=4,ny=4):
    x,y=np.meshgrid(np.linspace(20.0,140.0,nx),np.linspace(30.0,150.0,ny))
    return np.vstack([x.ravel(),y.ravel()])


def _affine_calibration(
    native=None,canonical=None,*,matrix=None,provenance=None,
):
    native=native or _frame("sec_405",detector="cam405",shape=(200,300),pixel=0.1)
    canonical=canonical or _frame("sec_488",detector="cam488",shape=(220,320),pixel=0.08)
    points=_grid()
    if matrix is None:
        matrix=np.array([[1.02,0.03,4.0],[-0.01,0.98,-3.0]])
    target=matrix[:,:2]@points+matrix[:,2,None]
    return fit_observation_registration(
        native,canonical,points,target,model="affine",provenance=provenance,
    )


def test_observation_frame_detects_scope_and_detector_geometry_mismatches():
    frame=_frame("sec_405")
    assert frame.is_compatible_with(_frame("sec_405"))
    assert frame.compatibility_issues(_frame("sec_488")) == ("section",)
    assert "detector" in frame.compatibility_issues(_frame("sec_405",detector="other"))
    assert "detector image shape" in frame.compatibility_issues(
        _frame("sec_405",shape=(100,100))
    )
    assert "pixel size" in frame.compatibility_issues(_frame("sec_405",pixel=0.2))


def test_affine_registration_fits_both_directions_and_roundtrips():
    calibration=_affine_calibration()
    native=calibration.native_points_px
    canonical=calibration.canonical_points_px

    np.testing.assert_allclose(calibration.native_to_canonical(native),canonical,atol=1e-10)
    np.testing.assert_allclose(calibration.canonical_to_native(canonical),native,atol=1e-10)
    assert calibration.transform_model == "affine"
    assert calibration.polynomial_degree is None
    assert calibration.forward_rms_residual < 1e-10
    assert calibration.reverse_rms_residual < 1e-10


def test_polynomial_registration_fits_independent_exact_reverse_when_representable():
    native_frame=_frame("sec_405")
    canonical_frame=_frame("sec_488")
    native=_grid(5,5)
    canonical=np.array(native,copy=True)
    canonical[1] += 0.002*native[0]**2

    calibration=fit_observation_registration(
        native_frame,canonical_frame,native,canonical,
        model="polynomial",polynomial_degree=2,
    )

    assert calibration.transform_model == "polynomial"
    assert calibration.polynomial_degree == 2
    np.testing.assert_allclose(calibration.native_to_canonical(native),canonical,atol=1e-9)
    np.testing.assert_allclose(calibration.canonical_to_native(canonical),native,atol=1e-9)
    assert calibration.forward_transform is not calibration.reverse_transform


def test_registration_serialization_preserves_points_transforms_and_provenance():
    calibration=_affine_calibration(provenance={"focal_state":"Fintermediate"})
    restored=ObservationRegistrationCalibration.from_dict(calibration.to_dict())

    assert restored.native_frame == calibration.native_frame
    assert restored.canonical_frame == calibration.canonical_frame
    assert restored.transform_model == calibration.transform_model
    assert restored.provenance["focal_state"] == "Fintermediate"
    np.testing.assert_allclose(restored.native_points_px,calibration.native_points_px)
    np.testing.assert_allclose(
        restored.native_to_canonical(calibration.native_points_px),
        calibration.canonical_points_px,
    )


def test_registration_store_scopes_by_native_slm_section_and_plane(tmp_path):
    store=ObservationRegistrationStore(tmp_path)
    calibration=_affine_calibration()
    path=store.save(calibration)

    assert path.is_file()
    assert store.exists("SER123","sec_405","Intermediate")
    assert not store.exists("OTHER","sec_405","Intermediate")
    assert not store.exists("SER123","sec_488","Intermediate")
    assert not store.exists("SER123","sec_405","Sample")
    restored=store.load("SER123","sec_405","Intermediate")
    assert restored.native_frame == calibration.native_frame
    assert restored.canonical_frame == calibration.canonical_frame

    payload=json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["calibration"]["native_frame"]["section_key"] == "sec_405"
    assert payload["calibration"]["canonical_frame"]["section_key"] == "sec_488"


def test_registration_service_projects_native_to_unregistered_explicit_canonical(tmp_path):
    store=ObservationRegistrationStore(tmp_path)
    calibration=_affine_calibration()
    store.save(calibration)
    service=ObservationRegistrationService(SLMIdentity("slm","SER123"),store)

    projected=service.project_points(
        calibration.native_frame,calibration.canonical_frame,
        calibration.native_points_px,
    )
    np.testing.assert_allclose(projected,calibration.canonical_points_px,atol=1e-10)

    restored=service.project_points(
        calibration.canonical_frame,calibration.native_frame,
        calibration.canonical_points_px,
    )
    np.testing.assert_allclose(restored,calibration.native_points_px,atol=1e-10)


def test_registration_service_projects_between_two_registered_native_frames(tmp_path):
    store=ObservationRegistrationStore(tmp_path)
    canonical=_frame("sec_488",detector="cam488",shape=(220,320),pixel=0.08)
    first=_affine_calibration(
        native=_frame("sec_405",detector="cam405"),canonical=canonical,
    )
    second=_affine_calibration(
        native=_frame("sec_561",detector="cam561"),canonical=canonical,
        matrix=np.array([[0.97,-0.02,8.0],[0.01,1.01,5.0]]),
    )
    store.save(first); store.save(second)
    service=ObservationRegistrationService(SLMIdentity("slm","SER123"),store)

    expected=second.canonical_to_native(
        first.native_to_canonical(first.native_points_px)
    )
    actual=service.project_points(
        first.native_frame,second.native_frame,first.native_points_px,
    )
    np.testing.assert_allclose(actual,expected,atol=1e-10)


def test_registration_service_can_read_source_registration_from_another_slm(tmp_path):
    store=ObservationRegistrationStore(tmp_path)
    canonical=_frame("sec_488",serial="CANON",detector="cam488")
    source=_affine_calibration(
        native=_frame("sec_405",serial="SOURCE",detector="cam405"),
        canonical=canonical,
    )
    target=_affine_calibration(
        native=_frame("sec_405",serial="CURRENT",detector="cam405b"),
        canonical=canonical,
        matrix=np.array([[0.99,0.01,2.0],[-0.02,1.03,-1.0]]),
    )
    store.save(source); store.save(target)
    service=ObservationRegistrationService(SLMIdentity("current","CURRENT"),store)

    expected=target.canonical_to_native(source.native_to_canonical(source.native_points_px))
    actual=service.project_points(source.native_frame,target.native_frame,source.native_points_px)
    np.testing.assert_allclose(actual,expected,atol=1e-10)


def test_registration_service_rejects_stale_native_detector_geometry(tmp_path):
    store=ObservationRegistrationStore(tmp_path)
    calibration=_affine_calibration()
    store.save(calibration)
    service=ObservationRegistrationService(SLMIdentity("slm","SER123"),store)
    stale=_frame(
        "sec_405",detector="cam405",shape=(100,100),pixel=0.1,
    )

    with pytest.raises(ValueError,match="detector image shape"):
        service.native_to_canonical(stale,calibration.native_points_px)


def test_registration_service_requires_explicit_bridge_for_unrelated_frames(tmp_path):
    service=ObservationRegistrationService(
        SLMIdentity("slm","SER123"),ObservationRegistrationStore(tmp_path),
    )
    source=_frame("sec_a")
    target=_frame("sec_b")
    with pytest.raises(ValueError,match="No observation registration"):
        service.project_points(source,target,_grid())


def test_workspace_and_session_expose_observation_registration_service(tmp_path):
    workspace=SLMWorkspace(tmp_path)
    assert workspace.observation_registration_calibrations_root == (
        tmp_path/"observation_registration_calibrations"
    )
    assert workspace.observation_registration_store.directory == (
        tmp_path/"observation_registration_calibrations"
    )

    geometry=SLMGeometry(width=64,height=64,pixel_size_um=1.0)
    runtime=SLMRuntime(
        identity=SLMIdentity("slm","SER123"),geometry=geometry,
        section_geometries=split_slm_geometry(geometry,1),
        registries=DEFAULT_REGISTRIES,
    )
    session=SLMSession(
        runtime=runtime,
        observation_registration_store=workspace.observation_registration_store,
    )
    calibration=_affine_calibration()
    assert session.observation_registration.save(calibration) is calibration
    assert session.observation_registration.exists("sec_405","Intermediate")


def test_identity_registration_and_same_frame_projection_are_exact(tmp_path):
    frame=_frame("sec_488")
    points=_grid()
    calibration=fit_observation_registration(
        frame,frame,points,points,model="affine",
    )
    np.testing.assert_allclose(calibration.native_to_canonical(points),points,atol=1e-12)
    np.testing.assert_allclose(calibration.canonical_to_native(points),points,atol=1e-12)

    service=ObservationRegistrationService(
        SLMIdentity("slm","SER123"),ObservationRegistrationStore(tmp_path),
    )
    projected=service.project_points(frame,frame,points)
    np.testing.assert_array_equal(projected,points)

@pytest.mark.parametrize("correction_model",["per_spot","translation","rigid","affine","polynomial"])
def test_registered_reference_still_supports_all_residual_alignment_models(correction_model):
    from slmcore.core.measurement import (
        PositionReferenceTarget,ReferenceGeometry,
        align_resolved_position_reference,resolve_exact_lattice,
    )

    x,y=np.meshgrid(np.arange(4.0)*12.0,np.arange(4.0)*11.0,indexing="xy")
    canonical=np.vstack([x.ravel(),y.ravel()])
    source_native=canonical-np.array([[5.0],[-4.0]])
    source_frame=_frame("sec_405",serial="SER-405",detector="cam405",shape=(160,180),pixel=0.12)
    canonical_frame=_frame("sec_488",serial="SER-488",detector="cam488",shape=(140,170),pixel=0.10)
    registration=fit_observation_registration(
        source_frame,canonical_frame,source_native,canonical,model="affine",
    )
    projected=registration.native_to_canonical(source_native)
    site_ids=tuple((int(i%4),int(i//4)) for i in range(projected.shape[1]))
    reference=ReferenceGeometry(positions_px=projected,site_ids=site_ids)
    measured=canonical+np.array([[2.0],[-3.0]])
    target=PositionReferenceTarget(
        registered_positions_px=canonical,
        measured_positions_px=measured,
        site_ids=site_ids,
    )
    resolved=resolve_exact_lattice(reference,target)
    result=align_resolved_position_reference(
        target,resolved,model=correction_model,polynomial_degree=2,
    )
    np.testing.assert_allclose(result.predicted_positions_px,canonical,atol=1e-8)
    np.testing.assert_allclose(result.correction_px,np.tile(np.array([[-2.0],[3.0]]),(1,canonical.shape[1])),atol=1e-8)


def test_registration_service_rejects_persisted_self_registration(tmp_path):
    frame=_frame("sec_488")
    points=_grid()
    calibration=fit_observation_registration(
        frame,frame,points,points,model="affine",
    )
    service=ObservationRegistrationService(
        SLMIdentity("slm","SER123"),ObservationRegistrationStore(tmp_path),
    )

    with pytest.raises(ValueError,match="cannot be registered to itself"):
        service.save(calibration)


def test_registration_service_enforces_one_canonical_frame_per_observation_plane(tmp_path):
    store=ObservationRegistrationStore(tmp_path)
    service=ObservationRegistrationService(SLMIdentity("slm","SER123"),store)
    canonical=_frame("sec_488",detector="cam488")
    first=_affine_calibration(
        native=_frame("sec_405",detector="cam405"),canonical=canonical,
    )
    consistent=_affine_calibration(
        native=_frame("sec_561",detector="cam561"),canonical=canonical,
    )
    conflicting=_affine_calibration(
        native=_frame("sec_640",detector="cam640"),
        canonical=_frame("sec_561",detector="cam561"),
    )

    service.save(first)
    service.save(consistent)
    assert service.canonical_frame("Intermediate") == canonical
    with pytest.raises(ValueError,match="already uses a different canonical frame"):
        service.save(conflicting)


def test_replacing_only_registration_may_redefine_canonical_frame(tmp_path):
    store=ObservationRegistrationStore(tmp_path)
    service=ObservationRegistrationService(SLMIdentity("slm","SER123"),store)
    native=_frame("sec_405",detector="cam405")
    first=_affine_calibration(
        native=native,canonical=_frame("sec_488",detector="cam488"),
    )
    replacement=_affine_calibration(
        native=native,canonical=_frame("sec_561",detector="cam561"),
    )

    service.save(first)
    service.save(replacement)
    assert service.canonical_frame("Intermediate") == replacement.canonical_frame


def test_registration_store_can_enumerate_plane_registrations_workspace_wide(tmp_path):
    store=ObservationRegistrationStore(tmp_path)
    canonical=_frame("sec_ref",serial="CANON",detector="camref")
    first=_affine_calibration(
        native=_frame("sec_405",serial="SLM-A",detector="cam405"),canonical=canonical,
    )
    second=_affine_calibration(
        native=_frame("sec_488",serial="SLM-B",detector="cam488"),canonical=canonical,
    )
    sample=_affine_calibration(
        native=_frame("sec_405",serial="SLM-A",plane="Sample",detector="cam405"),
        canonical=_frame("sec_ref",serial="CANON",plane="Sample",detector="camref"),
    )
    store.save(first); store.save(second); store.save(sample)

    intermediate=store.registrations("Intermediate")
    assert {item.native_frame.slm_serial for item in intermediate} == {"SLM-A","SLM-B"}
    assert all(item.native_frame.observation_plane_name=="Intermediate" for item in intermediate)
