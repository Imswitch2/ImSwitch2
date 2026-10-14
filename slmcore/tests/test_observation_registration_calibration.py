import numpy as np
import pytest

from slmcore import (
    DEFAULT_REGISTRIES,ImageMeasurement,ObservationPlaneDefinition,
    ObservationPlaneStore,ObservationRegistrationStore,PositionReferenceStore,SLMGeometry,SLMIdentity,
    SLMRuntime,SLMSession,split_slm_geometry,
)
from slmcore.core.cgh import CGHResult
from slmcore.core.cgh.localization import (
    LocalizationResult,SpotOrdering,spot_ordering_permutation,
)


def _runtime(*,nx=3,ny=3,serial="SER123",key="slm"):

    geometry=SLMGeometry(width=128,height=64,pixel_size_um=1.0)
    runtime=SLMRuntime(
        identity=SLMIdentity(key,serial),geometry=geometry,
        section_geometries=split_slm_geometry(geometry,2),
        registries=DEFAULT_REGISTRIES,
    )
    for section in runtime.section_keys:
        runtime.apply_section_patch(section,{
            ("cgh","active"):True,
            ("cgh","selected_target"):"multi_foci_vector",
            ("cgh","multi_foci_vector","params","n_foci_x"):nx,
            ("cgh","multi_foci_vector","params","n_foci_y"):ny,
        })
        job=runtime.prepare_section_base_cgh(section)
        runtime.commit_section_cgh(section,CGHResult(
            generation=job.generation,spec=job.spec,target_name=job.target_name,
            pattern=np.ones(job.spec.context.shape,dtype=np.complex128),
        ))
    return runtime


def _session(
    tmp_path,*,planes=("Intermediate",),nx=3,ny=3,serial="SER123",key="slm",
    plane_store=None,registration_store=None,reference_store=None,
):
    if plane_store is None:
        plane_store=ObservationPlaneStore(tmp_path/"planes")
        for name in planes:
            plane_store.add(ObservationPlaneDefinition(name=name))
    if registration_store is None:
        registration_store=ObservationRegistrationStore(tmp_path/"registrations")
    if reference_store is None:
        reference_store=PositionReferenceStore(tmp_path/"references")
    session=SLMSession(
        runtime=_runtime(nx=nx,ny=ny,serial=serial,key=key),
        observation_plane_store=plane_store,
        observation_registration_store=registration_store,
        position_reference_store=reference_store,
    )
    return session,registration_store


def _grid(count_x=3,count_y=3):
    x,y=np.meshgrid(np.linspace(30.0,150.0,count_x),np.linspace(25.0,145.0,count_y))
    return np.vstack([x.ravel(),y.ravel()])


def _attach_localization(
    session,section,full_points,*,plane="Intermediate",crop=(7,57,11,61),
    detector="cam",image_shape=(180,220),matched_mask=None,inferred_mask=None,
):
    runtime=session.runtime
    if session.feedback.active_observation_plane(section)!=plane:
        session.feedback.select_observation_plane(section,plane)
    internal=runtime._get_section(section)
    resolution=internal._cgh_session.create_target_resolution(
        internal.state.cgh,internal._build_context(internal.state),
    )
    count=int(resolution.lattice_indices.shape[1])
    full=np.asarray(full_points,dtype=np.float64)[:,:count]
    y1,y2,x1,x2=(int(v) for v in crop)
    local=full-np.asarray([[float(x1)],[float(y1)]])
    measurement=ImageMeasurement(
        image=np.zeros(image_shape,dtype=np.float64),source=detector,detector=detector,
        pixel_size_um=0.1,
        metadata={"observation_plane":plane},
    )
    runtime.set_section_feedback_measurement(section,measurement)
    parameters=dict(runtime.get_section_feedback_status(section).localization_params)
    if matched_mask is None:
        matched_mask=np.ones(count,dtype=bool)
    else:
        matched_mask=np.asarray(matched_mask,dtype=bool)[:count]
    if inferred_mask is None:
        inferred_mask=np.zeros(count,dtype=bool)
    else:
        inferred_mask=np.asarray(inferred_mask,dtype=bool)[:count]
    localization=LocalizationResult(
        target_type="multi_foci_vector",
        target_params=dict(resolution.canonical_params),
        parameters=parameters,
        lattice_indices=resolution.lattice_indices,
        crop_coord=(y1,y2,x1,x2),
        cropped_image=np.zeros((y2-y1,x2-x1),dtype=np.float64),
        expected_positions_px=local,
        measured_positions_px=local,
        period_x_px=30.0,period_y_px=30.0,
        offset_x_px=float(local[0,0]),offset_y_px=float(local[1,0]),
        diagnostics={
            "measurement_id":measurement.measurement_id,
            "matched_mask":tuple(bool(v) for v in matched_mask),
            "inferred_mask":tuple(bool(v) for v in inferred_mask),
            "matched_count":int(np.count_nonzero(matched_mask)),
            "inferred_count":int(np.count_nonzero(inferred_mask)),
            "missing_count":int(count-np.count_nonzero(matched_mask|inferred_mask)),
        },
    )
    runtime.commit_section_feedback_localization(section,localization,parameters)
    return localization,measurement


def _add_reference(
    session,section,points,*,name="canonical-ref",plane="Intermediate",
    lifetime="session",matched_mask=None,inferred_mask=None,detector="cam488",
):
    _attach_localization(
        session,section,points,plane=plane,detector=detector,
        matched_mask=matched_mask,inferred_mask=inferred_mask,
    )
    return session.feedback.add_position_reference(
        section,name,lifetime=lifetime,
    )


def test_calibration_workflow_consumes_native_localization_and_raw_reference(tmp_path):
    session,_store=_session(tmp_path)
    native=_grid()
    matrix=np.array([[1.03,0.02,6.0],[-0.01,0.98,-4.0]])
    canonical=matrix[:,:2]@native+matrix[:,2,None]
    matched=np.ones(native.shape[1],dtype=bool); matched[4]=False
    inferred=np.zeros(native.shape[1],dtype=bool); inferred[4]=True

    _attach_localization(
        session,"sec_0",native,crop=(9,59,13,63),detector="cam405",
        matched_mask=matched,inferred_mask=inferred,
    )
    _add_reference(
        session,"sec_1",canonical,name="488-ref",detector="cam488",
        matched_mask=matched,inferred_mask=inferred,
    )
    service=session.observation_registration_calibration

    native_snapshot=service.use_latest_localization(
        "sec_0","Intermediate","488-ref",
    )
    context=service.context("sec_0","Intermediate","488-ref")
    canonical_snapshot=context.canonical_snapshot
    assert canonical_snapshot is not None
    np.testing.assert_allclose(native_snapshot.geometry.positions_px,native)
    np.testing.assert_allclose(canonical_snapshot.geometry.positions_px,canonical)
    assert native_snapshot.matched_count == 8
    assert canonical_snapshot.valid_count == 8
    assert not native_snapshot.geometry.valid_mask[4]
    assert not canonical_snapshot.geometry.valid_mask[4]
    assert canonical_snapshot.sample_policy=="genuinely_measured_sites_from_reference"
    assert context.common_site_count == 8
    assert context.fit_enabled

    candidate=service.fit(
        "sec_0","Intermediate","488-ref",model="affine",
    )
    expected_native=np.delete(native,4,axis=1)
    expected_canonical=np.delete(canonical,4,axis=1)
    np.testing.assert_allclose(candidate.native_points_px,expected_native)
    np.testing.assert_allclose(candidate.canonical_points_px,expected_canonical)
    np.testing.assert_allclose(
        candidate.native_to_canonical(expected_native),expected_canonical,atol=1e-9,
    )
    assert candidate.provenance["sample_policy"] == "native_measured_plus_raw_named_reference"
    assert candidate.provenance["canonical_reference_name"] == "488-ref"
    assert len(candidate.provenance["site_ids"]) == 8

    diagnostics=service.context("sec_0","Intermediate","488-ref").diagnostics
    assert diagnostics is not None
    assert diagnostics.point_count == 8
    np.testing.assert_allclose(
        diagnostics.transformed_native_points_px,expected_canonical,atol=1e-9,
    )
    np.testing.assert_allclose(diagnostics.forward_residual_vectors_px,0.0,atol=1e-9)



def test_calibration_resolves_flip_x_before_fitting_continuous_registration(tmp_path):
    """Discrete SLM index orientation must not become a reflected registration."""
    session,_store=_session(tmp_path,nx=5,ny=4)
    native=_grid(5,4)
    native_localization,_measurement=_attach_localization(
        session,"sec_0",native,detector="cam405",
    )
    flip=spot_ordering_permutation(
        native_localization.lattice_indices,SpotOrdering.FLIP_X,
    )
    # Same physical point cloud, but the canonical reference columns carry the
    # opposite X lattice identity convention. Same-ID fitting would create a
    # reflection even though the true continuous registration is identity.
    canonical_with_wrong_ids=native[:,flip]
    _add_reference(
        session,"sec_1",canonical_with_wrong_ids,name="488-flipped",detector="cam488",
    )

    service=session.observation_registration_calibration
    service.use_latest_localization("sec_0","Intermediate","488-flipped")
    candidate=service.fit(
        "sec_0","Intermediate","488-flipped",model="affine",
    )

    assert candidate.provenance["correspondence"] == "exact_lattice"
    assert candidate.provenance["correspondence_requested"] == "auto"
    assert candidate.provenance["correspondence_resolved"] == "flip_x"
    assert candidate.provenance["resolved_correspondence"] == "flip_x"
    assert np.linalg.det(candidate.forward_transform.matrix[:,:2]) > 0.0
    np.testing.assert_allclose(
        candidate.native_to_canonical(native),native,atol=1e-9,
    )


def test_calibration_context_exposes_position_feedback_correspondence_contract(tmp_path):
    session,_store=_session(tmp_path,nx=5,ny=4)
    native=_grid(5,4)
    native_localization,_measurement=_attach_localization(
        session,"sec_0",native,detector="cam405",
    )
    flip=spot_ordering_permutation(
        native_localization.lattice_indices,SpotOrdering.FLIP_X,
    )
    _add_reference(
        session,"sec_1",native[:,flip],name="488-flipped",detector="cam488",
    )
    service=session.observation_registration_calibration
    service.use_latest_localization("sec_0","Intermediate","488-flipped")

    context=service.context("sec_0","Intermediate","488-flipped")
    assert context.correspondence_requested=="auto"
    assert context.correspondence_resolved=="flip_x"
    assert "auto" in context.correspondence_options
    assert "canonical" in context.correspondence_options
    assert "flip_x" in context.correspondence_options
    assert context.correspondence_diagnostics["requested"]=="auto"
    assert context.correspondence_diagnostics["resolved"]=="flip_x"
    assert context.correspondence_diagnostics["fit_count"]==native.shape[1]
    assert context.correspondence_diagnostics["reflection"] is False


def test_manual_registration_correspondence_override_invalidates_fit_and_changes_pairing(tmp_path):
    session,_store=_session(tmp_path,nx=5,ny=4)
    native=_grid(5,4)
    native_localization,_measurement=_attach_localization(
        session,"sec_0",native,detector="cam405",
    )
    flip=spot_ordering_permutation(
        native_localization.lattice_indices,SpotOrdering.FLIP_X,
    )
    _add_reference(
        session,"sec_1",native[:,flip],name="488-flipped",detector="cam488",
    )
    service=session.observation_registration_calibration
    service.use_latest_localization("sec_0","Intermediate","488-flipped")
    auto_candidate=service.fit(
        "sec_0","Intermediate","488-flipped",model="affine",
    )
    assert np.linalg.det(auto_candidate.forward_transform.matrix[:,:2])>0.0

    details=service.set_correspondence(
        "sec_0","Intermediate","488-flipped","canonical",
    )
    assert details["requested"]=="canonical"
    assert details["resolved"]=="canonical"
    context=service.context("sec_0","Intermediate","488-flipped")
    assert context.correspondence_requested=="canonical"
    assert context.correspondence_resolved=="canonical"
    assert context.candidate is None
    assert context.diagnostics is None

    manual_candidate=service.fit(
        "sec_0","Intermediate","488-flipped",model="affine",
    )
    assert manual_candidate.provenance["correspondence_requested"]=="canonical"
    assert manual_candidate.provenance["correspondence_resolved"]=="canonical"
    assert np.linalg.det(manual_candidate.forward_transform.matrix[:,:2])<0.0


def test_manual_registration_correspondence_rejects_shape_incompatible_choice(tmp_path):
    session,_store=_session(tmp_path,nx=5,ny=4)
    native=_grid(5,4)
    _attach_localization(session,"sec_0",native,detector="cam405")
    _add_reference(session,"sec_1",native,name="488-ref",detector="cam488")
    service=session.observation_registration_calibration
    service.use_latest_localization("sec_0","Intermediate","488-ref")
    context=service.context("sec_0","Intermediate","488-ref")
    assert "rotate_90_cw" not in context.correspondence_options
    with pytest.raises(ValueError,match="not valid for this finite lattice"):
        service.set_correspondence(
            "sec_0","Intermediate","488-ref","rotate_90_cw",
        )


def test_saved_manual_correspondence_is_read_only_until_new_calibration(tmp_path):
    session,_store=_session(tmp_path,nx=5,ny=4)
    native=_grid(5,4)
    native_localization,_measurement=_attach_localization(
        session,"sec_0",native,detector="cam405",
    )
    flip=spot_ordering_permutation(
        native_localization.lattice_indices,SpotOrdering.FLIP_X,
    )
    _add_reference(
        session,"sec_1",native[:,flip],name="488-flipped",detector="cam488",
    )
    service=session.observation_registration_calibration
    service.use_latest_localization("sec_0","Intermediate","488-flipped")
    service.set_correspondence(
        "sec_0","Intermediate","488-flipped","flip_x",
    )
    service.fit("sec_0","Intermediate","488-flipped",model="affine")
    service.save("sec_0","Intermediate","488-flipped")

    service.prepare_runtime_change()
    context=service.context("sec_0","Intermediate","488-flipped")
    assert context.saved_edit_mode
    assert context.correspondence_requested=="flip_x"
    assert context.correspondence_resolved=="flip_x"
    assert context.correspondence_options==("flip_x",)
    with pytest.raises(RuntimeError,match="Correspondence is fixed"):
        service.set_correspondence(
            "sec_0","Intermediate","488-flipped","canonical",
        )

    fresh=service.start_new_calibration("sec_0","Intermediate","488-flipped")
    assert not fresh.saved_edit_mode
    assert fresh.correspondence_requested=="auto"


def test_cross_slm_session_reference_can_define_canonical_frame(tmp_path):
    plane_store=ObservationPlaneStore(tmp_path/"planes")
    plane_store.add(ObservationPlaneDefinition(name="Intermediate"))
    registration_store=ObservationRegistrationStore(tmp_path/"registrations")
    reference_store=PositionReferenceStore(tmp_path/"references")
    session488,_=_session(
        tmp_path,serial="SER488",key="slm488",plane_store=plane_store,
        registration_store=registration_store,reference_store=reference_store,
    )
    session405,_=_session(
        tmp_path,serial="SER405",key="slm405",plane_store=plane_store,
        registration_store=registration_store,reference_store=reference_store,
    )
    native=_grid()
    canonical=native+np.array([[5.0],[-7.0]])
    _add_reference(session488,"sec_1",canonical,name="488-session",lifetime="session")
    _attach_localization(session405,"sec_0",native,detector="cam405")

    service=session405.observation_registration_calibration
    records=service.reference_records("sec_0","Intermediate")
    assert [record.name for record in records]==["488-session"]
    assert records[0].lifetime.value=="session"
    service.use_latest_localization("sec_0","Intermediate","488-session")
    candidate=service.fit("sec_0","Intermediate","488-session",model="affine")
    assert candidate.native_frame.slm_serial=="SER405"
    assert candidate.canonical_frame.slm_serial=="SER488"
    assert candidate.canonical_frame.section_key=="sec_1"
    np.testing.assert_allclose(candidate.native_to_canonical(native),canonical,atol=1e-9)
    service.save("sec_0","Intermediate","488-session")
    restored=registration_store.load("SER405","sec_0","Intermediate")
    assert restored.canonical_frame.slm_serial=="SER488"


def test_persistent_reference_is_available_for_registration(tmp_path):
    session,_store=_session(tmp_path)
    native=_grid(); canonical=native+2.0
    _attach_localization(session,"sec_0",native)
    _add_reference(session,"sec_1",canonical,name="persistent-ref",lifetime="persistent")
    records=session.observation_registration_calibration.reference_records("sec_0","Intermediate")
    assert len(records)==1
    assert records[0].lifetime.value=="persistent"
    context=session.observation_registration_calibration.context(
        "sec_0","Intermediate","persistent-ref",
    )
    assert context.canonical_snapshot is not None
    assert context.canonical_snapshot.lifetime.value=="persistent"


def test_calibration_workflow_fits_polynomial_and_persists_candidate(tmp_path):
    session,store=_session(tmp_path)
    native=_grid()
    canonical=np.array(native,copy=True)
    canonical[0]+=0.001*native[0]*native[1]
    canonical[1]+=0.0015*native[0]**2-0.0005*native[1]**2
    _attach_localization(session,"sec_0",native,detector="cam405")
    _add_reference(session,"sec_1",canonical,name="488-ref",detector="cam488")
    service=session.observation_registration_calibration
    service.use_latest_localization("sec_0","Intermediate","488-ref")

    candidate=service.fit(
        "sec_0","Intermediate","488-ref",model="polynomial",polynomial_degree=2,
        provenance={"focal_state":"Fintermediate"},
    )
    assert candidate.transform_model == "polynomial"
    assert candidate.polynomial_degree == 2
    assert candidate.provenance["focal_state"] == "Fintermediate"
    np.testing.assert_allclose(candidate.native_to_canonical(native),canonical,atol=1e-8)

    saved=service.save("sec_0","Intermediate","488-ref")
    restored=store.load("SER123","sec_0","Intermediate")
    assert saved.native_frame == restored.native_frame
    assert saved.canonical_frame == restored.canonical_frame
    assert service.context("sec_0","Intermediate","488-ref").saved_registration is not None

    service.delete("sec_0","Intermediate")
    assert not store.exists("SER123","sec_0","Intermediate")


def test_using_new_latest_localization_invalidates_previous_fit(tmp_path):
    session,_store=_session(tmp_path)
    native=_grid(); canonical=native+np.array([[3.0],[-2.0]])
    _attach_localization(session,"sec_0",native)
    _add_reference(session,"sec_1",canonical,name="488-ref")
    service=session.observation_registration_calibration
    service.use_latest_localization("sec_0","Intermediate","488-ref")
    service.fit("sec_0","Intermediate","488-ref",model="affine")
    assert service.context("sec_0","Intermediate","488-ref").candidate is not None

    _attach_localization(session,"sec_0",native+np.array([[1.0],[0.0]]))
    service.use_latest_localization("sec_0","Intermediate","488-ref")
    context=service.context("sec_0","Intermediate","488-ref")
    assert context.candidate is None
    assert context.diagnostics is None


def test_localize_deep_link_selects_requested_plane_and_reuses_feedback_ui_hook(tmp_path):
    session,_store=_session(tmp_path,planes=("Sample","Intermediate"))
    service=session.observation_registration_calibration
    calls=[]
    service.set_localization_requester(calls.append)
    session.feedback.select_observation_plane("sec_0","Sample")
    _attach_localization(session,"sec_0",_grid(),plane="Sample")
    assert service.latest_localization("sec_0","Intermediate") is None
    assert session.runtime.get_section_feedback_inspection("sec_0").measurement is not None

    service.request_localization("sec_0","Intermediate")
    assert calls == ["sec_0"]
    assert session.feedback.active_observation_plane("sec_0") == "Intermediate"
    assert session.runtime.get_section_feedback_inspection("sec_0").measurement is None


def test_calibration_context_reports_missing_native_localization_and_reference(tmp_path):
    session,_store=_session(tmp_path)
    context=session.observation_registration_calibration.context("sec_0","Intermediate")
    assert not context.latest_native_available
    assert not context.fit_enabled
    assert context.common_site_count == 0
    assert context.available_references==()
    with pytest.raises(RuntimeError,match="No accepted Intermediate localization"):
        session.observation_registration_calibration.use_latest_localization(
            "sec_0","Intermediate",
        )


def test_calibration_rejects_models_outside_affine_and_polynomial(tmp_path):
    session,_store=_session(tmp_path)
    native=_grid(); canonical=native+1.0
    _attach_localization(session,"sec_0",native)
    _add_reference(session,"sec_1",canonical,name="488-ref")
    service=session.observation_registration_calibration
    service.use_latest_localization("sec_0","Intermediate","488-ref")
    with pytest.raises(ValueError,match="affine or polynomial"):
        service.fit("sec_0","Intermediate","488-ref",model="rigid")


def test_calibration_polynomial_degree_is_limited_to_ui_supported_2_3_4(tmp_path):
    session,_store=_session(tmp_path)
    native=_grid(); canonical=native+1.0
    _attach_localization(session,"sec_0",native)
    _add_reference(session,"sec_1",canonical,name="488-ref")
    service=session.observation_registration_calibration
    service.use_latest_localization("sec_0","Intermediate","488-ref")
    with pytest.raises(ValueError,match="degree must be 2, 3, or 4"):
        service.fit(
            "sec_0","Intermediate","488-ref",model="polynomial",polynomial_degree=1,
        )


def test_saved_registration_is_available_after_session_reload(tmp_path):
    session,store=_session(tmp_path)
    native=_grid(); canonical=native+np.array([[2.0],[-3.0]])
    _attach_localization(session,"sec_0",native)
    _add_reference(session,"sec_1",canonical,name="488-ref",lifetime="persistent")
    service=session.observation_registration_calibration
    service.use_latest_localization("sec_0","Intermediate","488-ref")
    service.fit("sec_0","Intermediate","488-ref",model="affine")
    saved=service.save("sec_0","Intermediate","488-ref")
    session.dispose()

    reloaded=SLMSession(
        runtime=_runtime(),
        observation_plane_store=ObservationPlaneStore(tmp_path/"planes"),
        observation_registration_store=store,
        position_reference_store=PositionReferenceStore(tmp_path/"references"),
    )
    context=reloaded.observation_registration_calibration.context(
        "sec_0","Intermediate","488-ref",
    )
    assert context.saved_registration is not None
    assert context.saved_registration.native_frame == saved.native_frame
    assert context.saved_registration.canonical_frame == saved.canonical_frame
    assert context.saved_edit_mode
    assert context.canonical_snapshot is None
    assert context.fit_enabled
    assert context.diagnostics is not None


def test_saved_registration_refits_without_original_session_reference(tmp_path):
    session,store=_session(tmp_path)
    native=_grid(); canonical=native+np.array([[2.0],[-3.0]])
    _attach_localization(session,"sec_0",native)
    _add_reference(session,"sec_1",canonical,name="session-ref",lifetime="session")
    service=session.observation_registration_calibration
    service.use_latest_localization("sec_0","Intermediate","session-ref")
    service.fit("sec_0","Intermediate","session-ref",model="affine")
    saved=service.save("sec_0","Intermediate","session-ref")
    session.dispose()

    reloaded=SLMSession(
        runtime=_runtime(),
        observation_plane_store=ObservationPlaneStore(tmp_path/"planes"),
        observation_registration_store=store,
        position_reference_store=PositionReferenceStore(tmp_path/"references"),
    )
    service=reloaded.observation_registration_calibration
    assert service.reference_records("sec_0","Intermediate")==()
    context=service.context("sec_0","Intermediate")
    assert context.saved_edit_mode
    assert context.canonical_reference_name=="session-ref"
    np.testing.assert_allclose(context.candidate.native_points_px,saved.native_points_px)
    np.testing.assert_allclose(context.candidate.canonical_points_px,saved.canonical_points_px)

    candidate=service.fit(
        "sec_0","Intermediate","session-ref",model="polynomial",polynomial_degree=2,
    )
    assert candidate.transform_model=="polynomial"
    assert candidate.polynomial_degree==2
    np.testing.assert_allclose(candidate.native_points_px,saved.native_points_px)
    np.testing.assert_allclose(candidate.canonical_points_px,saved.canonical_points_px)
    assert candidate.provenance["correspondence_requested"]==saved.provenance["correspondence_requested"]
    assert candidate.provenance["refit_from_saved_registration"] is True

    updated=service.save("sec_0","Intermediate","session-ref")
    assert updated.transform_model=="polynomial"
    assert service.context("sec_0","Intermediate").saved_edit_mode


def test_save_switches_live_registration_workflow_to_saved_edit_mode(tmp_path):
    session,_store=_session(tmp_path)
    native=_grid(); canonical=native+1.0
    _attach_localization(session,"sec_0",native)
    _add_reference(session,"sec_1",canonical,name="488-ref")
    service=session.observation_registration_calibration
    service.use_latest_localization("sec_0","Intermediate","488-ref")
    service.fit("sec_0","Intermediate","488-ref",model="affine")
    before=service.context("sec_0","Intermediate","488-ref")
    assert not before.saved_edit_mode
    service.save("sec_0","Intermediate","488-ref")
    after=service.context("sec_0","Intermediate","488-ref")
    assert after.saved_edit_mode
    assert after.native_snapshot is None
    assert after.canonical_snapshot is None
    assert after.diagnostics is not None


def test_calibration_rejects_reference_from_native_observation_frame(tmp_path):
    session,_store=_session(tmp_path)
    native=_grid()
    _add_reference(session,"sec_0",native,name="self-ref")
    # Self references are hidden from normal choices.
    assert session.observation_registration_calibration.reference_records(
        "sec_0","Intermediate",
    )==()


def test_legacy_reference_without_matched_site_metadata_remains_usable(tmp_path):
    session,_store=_session(tmp_path)
    native=_grid(); canonical=native+3.0
    _attach_localization(session,"sec_0",native)
    reference=_add_reference(session,"sec_1",canonical,name="legacy-ref")
    # Simulate a reference written before matched_lattice_indices was persisted.
    payload=reference.to_dict(); payload["metadata"].pop("matched_lattice_indices",None)
    from slmcore import PositionReference
    legacy=PositionReference.from_dict(payload)
    store=session.feedback._references._position_reference_store
    store.add(legacy,overwrite=True)
    context=session.observation_registration_calibration.context(
        "sec_0","Intermediate","legacy-ref",
    )
    assert context.canonical_snapshot.sample_policy=="all_stored_reference_sites_legacy"
    session.observation_registration_calibration.use_latest_localization(
        "sec_0","Intermediate","legacy-ref",
    )
    candidate=session.observation_registration_calibration.fit(
        "sec_0","Intermediate","legacy-ref",model="affine",
    )
    assert candidate.provenance["canonical_sample_policy"]=="all_stored_reference_sites_legacy"
