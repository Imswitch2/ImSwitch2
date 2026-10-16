import numpy as np
import pytest

from slmcore import (
    ImageMeasurement,ObservationPlaneDefinition,
    PositionReference,PositionReferenceLifetime,PositionReferenceMode,
    SLMGeometry,SLMIdentity,SLMSectionAddress,SLMSectionCalibration,
    SLMSectionsDefinition,SLMDefinition,SLMWorkspace,
)
from slmcore.core.engine.section import SectionSplitLayout
from slmcore.host import MockSLMDeviceProvider,SLMHostServices


def _app():
    pytest.importorskip("qtpy")
    pytest.importorskip("pyqtgraph")
    try:
        from qtpy import QtWidgets
    except Exception as error:
        pytest.skip("Qt bindings are unavailable: %s" % error)
    app=QtWidgets.QApplication.instance()
    if app is None:
        app=QtWidgets.QApplication([])
    return app


def _definition(key,serial,label,n_sections=2):
    return SLMDefinition(
        identity=SLMIdentity(key,serial,label),
        geometry=SLMGeometry(width=24,height=12,pixel_size_um=1.0),
        sections=SLMSectionsDefinition(
            layout=SectionSplitLayout(n_sections=n_sections,axis="x"),
            customizable=True,
        ),
    )


def _session(factory,key,serial,label,n_sections=2):
    session,panel=factory.create(
        definition=_definition(key,serial,label,n_sections=n_sections),
        host_services=SLMHostServices(device=MockSLMDeviceProvider()),
        on_startup_preferences_changed=lambda _preferences:None,
    )
    return session,panel


def _two_session_workspace(tmp_path):
    _app()
    from slmcore.qt import SLMQtSessionFactory,SLMQtSessionGroup,SLMWorkspaceCoordinator

    workspace=SLMWorkspace(tmp_path)
    factory=SLMQtSessionFactory(workspace=workspace)
    first,first_panel=_session(factory,"slm_a","SER-A","405 SLM")
    second,second_panel=_session(factory,"slm_b","SER-B","488 SLM",n_sections=3)
    group=SLMQtSessionGroup()
    group.add_session(first,key="slm_a")
    group.add_session(second,key="slm_b")
    coordinator=SLMWorkspaceCoordinator(group,workspace)
    return workspace,group,coordinator,first,first_panel,second,second_panel


def _dispose(group,coordinator,*session_panels):
    coordinator.dispose()
    for session,panel in session_panels:
        group.remove_session(session.runtime.identity.key)
        session.dispose()
        panel.deleteLater()


def test_shared_workspace_reuses_one_dialog_and_switches_physical_manager(tmp_path):
    (
        _workspace,group,coordinator,first,first_panel,second,second_panel,
    )=_two_session_workspace(tmp_path)
    try:
        first_section=first.runtime.section_keys[0]
        second_section=second.runtime.section_keys[-1]
        assert coordinator.open(SLMSectionAddress("slm_a",first_section))
        dialog=coordinator.dialog
        assert dialog is not None
        assert dialog.windowTitle()=="SLM Workspace"
        assert dialog.slm_combo.count()==2
        assert coordinator.active_address==SLMSectionAddress("slm_a",first_section)
        assert first.calibration_manager._management_dialog is dialog
        assert second.calibration_manager._management_dialog is None

        assert coordinator.open(SLMSectionAddress("slm_b",second_section))
        assert coordinator.dialog is dialog
        assert coordinator.active_address==SLMSectionAddress("slm_b",second_section)
        assert first.calibration_manager._management_dialog is None
        assert second.calibration_manager._management_dialog is dialog
    finally:
        _dispose(
            group,coordinator,(first,first_panel),(second,second_panel),
        )


def test_section_local_workspace_request_routes_to_exact_slm_and_mapping_tab(tmp_path):
    (
        _workspace,group,coordinator,first,first_panel,second,second_panel,
    )=_two_session_workspace(tmp_path)
    try:
        assert coordinator.open(SLMSectionAddress("slm_a",first.runtime.section_keys[0]))
        dialog=coordinator.dialog
        dialog.tabs.setCurrentIndex(2)

        section=second.runtime.section_keys[-1]
        second.section_collection.sigCalibrationRequested.emit(section)

        assert coordinator.active_address==SLMSectionAddress("slm_b",section)
        assert coordinator.dialog is dialog
        assert dialog.tabs.currentIndex()==0
        view=second.section_collection.section_view(section)
        assert view.mapping_manage_button is not None
        assert view.mapping_manage_button.isEnabled()
    finally:
        _dispose(
            group,coordinator,(first,first_panel),(second,second_panel),
        )


def test_active_embedded_workflow_locks_context_and_rejects_retarget(tmp_path):
    (
        _workspace,group,coordinator,first,first_panel,second,second_panel,
    )=_two_session_workspace(tmp_path)
    try:
        first_address=SLMSectionAddress("slm_a",first.runtime.section_keys[0])
        second_address=SLMSectionAddress("slm_b",second.runtime.section_keys[0])
        assert coordinator.open(first_address)
        dialog=coordinator.dialog
        manager=first.calibration_manager
        manager._set_embedded_workflow(("base",first_address.section_key))
        from slmcore.qt import SLMControlMode
        assert not group.can_change_control_mode
        assert not group.set_control_mode(SLMControlMode.FAST_CONFIG)
        assert not dialog.context_switching_enabled
        assert not dialog.slm_combo.isEnabled()
        assert not dialog.section_combo.isEnabled()
        assert not coordinator.open(second_address)
        assert coordinator.active_address==first_address

        manager._set_embedded_workflow(None)
        assert dialog.context_switching_enabled
        assert dialog.slm_combo.isEnabled()
        assert dialog.section_combo.isEnabled()
        assert coordinator.open(second_address)
    finally:
        _dispose(
            group,coordinator,(first,first_panel),(second,second_panel),
        )


def test_workspace_remains_inspectable_but_read_only_in_fast_mode(tmp_path):
    (
        _workspace,group,coordinator,first,first_panel,second,second_panel,
    )=_two_session_workspace(tmp_path)
    from slmcore.qt import SLMControlMode
    try:
        address=SLMSectionAddress("slm_a",first.runtime.section_keys[0])
        assert coordinator.open(address)
        dialog=coordinator.dialog
        assert dialog.mutations_enabled

        assert group.set_control_mode(SLMControlMode.FAST_CONFIG)
        assert coordinator.dialog is dialog
        assert not dialog.mutations_enabled
        assert dialog.tabs.isEnabled()
        assert not dialog.mapping_add.isEnabled()
        assert not dialog.base_calibrate.isEnabled()

        assert group.set_control_mode(SLMControlMode.EDITOR)
        assert dialog.mutations_enabled
    finally:
        _dispose(
            group,coordinator,(first,first_panel),(second,second_panel),
        )


def test_workspace_slm_catalog_tracks_group_add_remove(tmp_path):
    _app()
    from slmcore.qt import SLMQtSessionFactory,SLMQtSessionGroup,SLMWorkspaceCoordinator

    workspace=SLMWorkspace(tmp_path)
    factory=SLMQtSessionFactory(workspace=workspace)
    first,first_panel=_session(factory,"slm_a","SER-A","405 SLM")
    second,second_panel=_session(factory,"slm_b","SER-B","488 SLM")
    group=SLMQtSessionGroup()
    group.add_session(first,key="slm_a")
    coordinator=SLMWorkspaceCoordinator(group,workspace)
    try:
        assert coordinator.open(SLMSectionAddress("slm_a",first.runtime.section_keys[0]))
        dialog=coordinator.dialog
        assert dialog.slm_combo.count()==1
        group.add_session(second,key="slm_b")
        assert dialog.slm_combo.count()==2
        group.remove_session("slm_b")
        assert dialog.slm_combo.count()==1
    finally:
        coordinator.dispose()
        group.remove_session("slm_a")
        group.remove_session("slm_b")
        first.dispose(); second.dispose()
        first_panel.deleteLater(); second_panel.deleteLater()


def test_slm_combo_switches_manager_and_replaces_section_catalog(tmp_path):
    (
        _workspace,group,coordinator,first,first_panel,second,second_panel,
    )=_two_session_workspace(tmp_path)
    try:
        assert coordinator.open(SLMSectionAddress("slm_a",first.runtime.section_keys[0]))
        dialog=coordinator.dialog
        assert dialog.section_combo.count()==len(first.runtime.section_keys)==2

        index=dialog.slm_combo.findData("slm_b")
        assert index>=0
        dialog.slm_combo.setCurrentIndex(index)

        assert coordinator.active_slm_key=="slm_b"
        assert second.calibration_manager._management_dialog is dialog
        assert first.calibration_manager._management_dialog is None
        assert dialog.section_combo.count()==len(second.runtime.section_keys)==3
        assert {dialog.section_combo.itemData(i) for i in range(dialog.section_combo.count())} == set(second.runtime.section_keys)
    finally:
        _dispose(group,coordinator,(first,first_panel),(second,second_panel))


def test_workspace_action_signal_is_owned_only_by_selected_manager(tmp_path):
    (
        _workspace,group,coordinator,first,first_panel,second,second_panel,
    )=_two_session_workspace(tmp_path)
    calls=[]
    first.calibration_manager._management_add_mapping=lambda section:calls.append(("a",section))
    second.calibration_manager._management_add_mapping=lambda section:calls.append(("b",section))
    try:
        assert coordinator.open(SLMSectionAddress("slm_a",first.runtime.section_keys[0]))
        dialog=coordinator.dialog
        dialog.sigMappingAddRequested.emit(first.runtime.section_keys[0])
        assert calls==[("a",first.runtime.section_keys[0])]

        assert coordinator.open(SLMSectionAddress("slm_b",second.runtime.section_keys[0]))
        dialog.sigMappingAddRequested.emit(second.runtime.section_keys[0])
        assert calls[-1]==("b",second.runtime.section_keys[0])
        assert calls.count(("a",first.runtime.section_keys[0]))==1
    finally:
        _dispose(group,coordinator,(first,first_panel),(second,second_panel))


def test_active_workflow_refuses_workspace_close(tmp_path,monkeypatch):
    (
        _workspace,group,coordinator,first,first_panel,second,second_panel,
    )=_two_session_workspace(tmp_path)
    from qtpy import QtWidgets
    monkeypatch.setattr(QtWidgets.QMessageBox,"information",lambda *args,**kwargs:QtWidgets.QMessageBox.Ok)
    try:
        address=SLMSectionAddress("slm_a",first.runtime.section_keys[0])
        assert coordinator.open(address)
        dialog=coordinator.dialog
        manager=first.calibration_manager
        manager._set_embedded_workflow(("base",address.section_key))
        assert dialog.isVisible()

        coordinator.close()

        assert dialog.isVisible()
        assert coordinator.active_address==address
        manager._set_embedded_workflow(None)
        coordinator.close()
        assert not dialog.isVisible()
    finally:
        _dispose(group,coordinator,(first,first_panel),(second,second_panel))


def test_standalone_session_keeps_local_workspace_fallback(tmp_path):
    _app()
    from slmcore.qt import SLMQtSessionFactory
    workspace=SLMWorkspace(tmp_path)
    factory=SLMQtSessionFactory(workspace=workspace)
    session,panel=_session(factory,"solo","SER-S","Solo SLM")
    try:
        section=session.runtime.section_keys[-1]
        session.section_collection.sigCalibrationRequested.emit(section)
        dialog=session.calibration_manager._management_dialog
        assert dialog is not None
        assert session.calibration_manager._management_dialog_owned
        assert dialog.section_key==section
        assert dialog.slm_combo.count()==1
    finally:
        dialog=session.calibration_manager._management_dialog
        if dialog is not None:
            dialog.close(); dialog.deleteLater()
        session.dispose(); panel.deleteLater()


def _reference(
    name,plane="sample",source_slm="slm_a",source_section="section_0",
    source_serial=None,
):
    if source_serial is None:
        source_serial={"slm_a":"SER-A","slm_b":"SER-B"}.get(source_slm,"SER-X")
    return PositionReference(
        name=name,
        observation_plane_name=plane,
        lattice_indices=np.array([[0,1],[0,0]],dtype=np.int64),
        positions_px=np.array([[10.0,20.0],[12.0,12.0]],dtype=np.float64),
        image_shape=(64,80),
        detector_name="cam",
        metadata={
            "source_slm_key":source_slm,
            "source_slm_serial":source_serial,
            "source_section":source_section,
            "source_pixel_size_um":0.1,
            "source_target_type":"lattice",
            "feedback_orientation":"identity",
            "lattice_representation":{
                "lattice_indices":[[0,1],[0,0]],
                "cell_indices":[[0,1],[0,0]],
                "basis_indices":[0,1],
                "cell_vectors":[[1.0,0.0],[0.0,2.0]],
                "basis_offsets":[[0.0,0.5],[0.0,1.0]],
                "diagnostics":{
                    "family":"alternating_rows",
                    "stagger":0.5,
                },
            },
            "matched_count":1,
            "inferred_count":1,
        },
        created_at="2026-10-15T12:00:00",
    )


def test_references_tab_is_workspace_scoped_and_live(tmp_path):
    (
        workspace,group,coordinator,first,first_panel,second,second_panel,
    )=_two_session_workspace(tmp_path)
    store=workspace.position_reference_store
    store.add(_reference("sample ref"))
    store.add(_reference("intermediate ref",plane="intermediate",source_slm="unloaded",source_section="mf"))
    try:
        assert coordinator.open(SLMSectionAddress("slm_a",first.runtime.section_keys[0]))
        dialog=coordinator.dialog
        dialog.tabs.setCurrentIndex(2)
        assert dialog.reference_view.table.rowCount()==2
        assert not dialog.slm_combo.isEnabled()
        assert not dialog.section_combo.isEnabled()
        rows={
            dialog.reference_view.table.item(row,1).text():row
            for row in range(dialog.reference_view.table.rowCount())
        }
        unloaded_row=rows["intermediate"]
        assert "unloaded / mf" in dialog.reference_view.table.item(unloaded_row,3).text()
        dialog.reference_view.table.selectRow(unloaded_row)
        assert "Source SLM key: unloaded" in dialog.reference_view.details.text()
        assert "Source section: mf" in dialog.reference_view.details.text()
        assert (
            "Lattice: alternating_rows · 2 sites · 2-point basis · stagger=0.5"
            in dialog.reference_view.details.text()
        )

        # Changing the physical SLM elsewhere does not filter the workspace catalog.
        assert coordinator.open(SLMSectionAddress("slm_b",second.runtime.section_keys[0]))
        dialog.tabs.setCurrentIndex(2)
        assert dialog.reference_view.table.rowCount()==2

        store.add(_reference("new live ref",plane="sample"))
        assert dialog.reference_view.table.rowCount()==3
    finally:
        _dispose(group,coordinator,(first,first_panel),(second,second_panel))


def test_reference_persist_and_delete_actions_update_catalog(tmp_path,monkeypatch):
    (
        workspace,group,coordinator,first,first_panel,second,second_panel,
    )=_two_session_workspace(tmp_path)
    from qtpy import QtWidgets
    store=workspace.position_reference_store
    store.add(_reference("ref"))
    try:
        assert coordinator.open(SLMSectionAddress("slm_a",first.runtime.section_keys[0]))
        dialog=coordinator.dialog
        dialog.tabs.setCurrentIndex(2)
        view=dialog.reference_view
        assert view.persist_button.isEnabled()
        view.persist_button.click()
        assert store.get_record("sample","ref").lifetime is PositionReferenceLifetime.PERSISTENT
        assert view.table.rowCount()==1

        monkeypatch.setattr(QtWidgets.QMessageBox,"question",lambda *args,**kwargs:QtWidgets.QMessageBox.Yes)
        view.delete_button.click()
        assert not store.exists("sample","ref")
        assert view.table.rowCount()==0
    finally:
        _dispose(group,coordinator,(first,first_panel),(second,second_panel))


def test_reference_manager_is_read_only_in_fast_mode(tmp_path):
    (
        workspace,group,coordinator,first,first_panel,second,second_panel,
    )=_two_session_workspace(tmp_path)
    from slmcore.qt import SLMControlMode
    workspace.position_reference_store.add(_reference("ref"))
    try:
        assert coordinator.open(SLMSectionAddress("slm_a",first.runtime.section_keys[0]))
        dialog=coordinator.dialog
        dialog.tabs.setCurrentIndex(2)
        assert dialog.reference_view.persist_button.isEnabled()
        assert dialog.reference_view.delete_button.isEnabled()

        assert group.set_control_mode(SLMControlMode.FAST_CONFIG)
        assert not dialog.reference_view.persist_button.isEnabled()
        assert not dialog.reference_view.delete_button.isEnabled()
    finally:
        _dispose(group,coordinator,(first,first_panel),(second,second_panel))



def _attach_feedback_reference_localization(session,workspace,section_key):
    """Install one complete localization suitable for real reference creation."""
    from slmcore.core.cgh.localization import LocalizationResult

    try:
        workspace.observation_plane_store.add(ObservationPlaneDefinition(name="sample"))
    except ValueError:
        pass
    session.feedback.select_observation_plane(section_key,"sample")

    runtime=session.runtime
    runtime.set_section_calibration(
        section_key,
        SLMSectionCalibration(
            kx_per_um=0.01,ky_per_um=0.012,mapping_name="sample",
        ),
    )
    runtime.apply_section_patch(
        section_key,
        {
            ("cgh","active"):True,
            ("cgh","selected_target"):"multi_foci_vector",
            ("cgh","multi_foci_vector","params","n_foci_x"):2,
            ("cgh","multi_foci_vector","params","n_foci_y"):2,
        },
    )
    section=runtime._get_section(section_key)
    resolution=section._cgh_session.create_target_resolution(
        section.state.cgh,section._build_context(section.state),
    )
    count=int(resolution.lattice_indices.shape[1])
    measurement=ImageMeasurement(
        image=np.zeros((64,80),dtype=np.float64),
        source="cam",detector="cam",pixel_size_um=0.1,
    )
    runtime.set_section_feedback_measurement(section_key,measurement)
    parameters=dict(runtime.get_section_feedback_status(section_key).localization_params)
    expected=np.array(
        [[12.0,44.0,12.0,44.0],[12.0,12.0,44.0,44.0]],
        dtype=np.float64,
    )[:,:count]
    localization=LocalizationResult(
        target_type="multi_foci_vector",
        target_params=dict(resolution.canonical_params),
        parameters=parameters,
        lattice_indices=resolution.lattice_indices,
        crop_coord=(0,64,0,80),
        cropped_image=np.zeros((64,80),dtype=np.float64),
        expected_positions_px=expected,
        measured_positions_px=expected+np.array([[1.0],[2.0]]),
        period_x_px=32.0,
        period_y_px=32.0,
        offset_x_px=12.0,
        offset_y_px=12.0,
        diagnostics={
            "measurement_id":measurement.measurement_id,
            "matched_mask":tuple(True for _ in range(count)),
            "matched_count":count,
            "inferred_count":0,
            "missing_count":0,
        },
    )
    runtime.commit_section_feedback_localization(
        section_key,localization,parameters,
    )


def test_reference_created_through_feedback_updates_workspace_catalog(tmp_path):
    (
        workspace,group,coordinator,first,first_panel,second,second_panel,
    )=_two_session_workspace(tmp_path)
    try:
        section=first.runtime.section_keys[0]
        assert coordinator.open(SLMSectionAddress("slm_a",section))
        dialog=coordinator.dialog
        dialog.tabs.setCurrentIndex(2)
        assert dialog.reference_view.table.rowCount()==0

        _attach_feedback_reference_localization(first,workspace,section)
        first.feedback.add_position_reference(section,"from feedback")

        assert dialog.reference_view.table.rowCount()==1
        assert dialog.reference_view.table.item(0,0).text()=="from feedback"
        assert dialog.reference_view.table.item(0,1).text()=="sample"
    finally:
        _dispose(group,coordinator,(first,first_panel),(second,second_panel))


def test_workspace_delete_reconciles_active_feedback_reference(tmp_path):
    (
        workspace,group,coordinator,first,first_panel,second,second_panel,
    )=_two_session_workspace(tmp_path)
    try:
        source_section=first.runtime.section_keys[0]
        target_section=second.runtime.section_keys[0]
        _attach_feedback_reference_localization(first,workspace,source_section)
        first.feedback.add_position_reference(source_section,"shared")
        second.feedback.select_observation_plane(target_section,"sample")
        second.feedback.set_position_reference(
            target_section,
            mode=PositionReferenceMode.REFERENCE,
            reference_name="shared",
        )
        assert second.feedback.position_reference_selection(
            target_section,
        ).mode is PositionReferenceMode.REFERENCE

        assert coordinator.open(SLMSectionAddress("slm_b",target_section))
        dialog=coordinator.dialog
        dialog.tabs.setCurrentIndex(2)
        view=dialog.reference_view
        assert view.table.rowCount()==1
        view.table.selectRow(0)
        view.delete_button.click()

        assert not workspace.position_reference_store.exists("sample","shared")
        assert second.feedback.position_reference_selection(
            target_section,
        ).mode is PositionReferenceMode.GLOBAL_FIT
    finally:
        _dispose(group,coordinator,(first,first_panel),(second,second_panel))


def test_removing_active_session_switches_to_remaining_physical_slm(tmp_path):
    (
        _workspace,group,coordinator,first,first_panel,second,second_panel,
    )=_two_session_workspace(tmp_path)
    try:
        assert coordinator.open(
            SLMSectionAddress("slm_a",first.runtime.section_keys[-1])
        )
        dialog=coordinator.dialog
        group.remove_session("slm_a")

        assert coordinator.active_slm_key=="slm_b"
        assert coordinator.active_address is not None
        assert coordinator.active_address.slm_key=="slm_b"
        assert dialog.slm_combo.count()==1
        assert dialog.slm_combo.currentData()=="slm_b"
        assert dialog.section_combo.count()==len(second.runtime.section_keys)
    finally:
        coordinator.dispose()
        group.remove_session("slm_a"); group.remove_session("slm_b")
        first.dispose(); second.dispose()
        first_panel.deleteLater(); second_panel.deleteLater()


def test_last_session_removal_clears_context_and_next_add_reactivates(tmp_path):
    _app()
    from slmcore.qt import SLMQtSessionFactory,SLMQtSessionGroup,SLMWorkspaceCoordinator

    workspace=SLMWorkspace(tmp_path)
    factory=SLMQtSessionFactory(workspace=workspace)
    first,first_panel=_session(factory,"slm_a","SER-A","405 SLM")
    second,second_panel=_session(factory,"slm_b","SER-B","488 SLM",n_sections=3)
    group=SLMQtSessionGroup(); group.add_session(first,key="slm_a")
    coordinator=SLMWorkspaceCoordinator(group,workspace)
    try:
        assert coordinator.open(SLMSectionAddress("slm_a",first.runtime.section_keys[0]))
        dialog=coordinator.dialog
        group.remove_session("slm_a")

        assert coordinator.active_slm_key is None
        assert coordinator.active_address is None
        assert dialog.slm_combo.count()==0
        assert dialog.section_combo.count()==0
        assert dialog.mapping_list.count()==0
        assert dialog.observation_list.count()==0
        assert dialog.geometry_list.count()==0
        assert dialog.registration_list.count()==0
        assert "No SLM session" in dialog.inspection_text.text()
        # Workspace-wide references remain a separate resource tab.
        assert dialog.tabs.count()==3

        group.add_session(second,key="slm_b")
        assert coordinator.active_slm_key=="slm_b"
        assert coordinator.active_address is not None
        assert coordinator.active_address.slm_key=="slm_b"
        assert dialog.slm_combo.count()==1
        assert dialog.section_combo.count()==len(second.runtime.section_keys)
    finally:
        coordinator.dispose()
        group.remove_session("slm_a"); group.remove_session("slm_b")
        first.dispose(); second.dispose()
        first_panel.deleteLater(); second_panel.deleteLater()


def test_active_workspace_reconciles_real_section_layout_change(tmp_path):
    (
        _workspace,group,coordinator,first,first_panel,second,second_panel,
    )=_two_session_workspace(tmp_path)
    try:
        removed_section=first.runtime.section_keys[-1]
        assert coordinator.open(SLMSectionAddress("slm_a",removed_section))
        dialog=coordinator.dialog

        prepared=first.prepare_section_layout_change(
            SectionSplitLayout(n_sections=1,axis="x")
        )
        assert first.apply_prepared_section_layout_change(prepared)

        assert dialog.section_combo.count()==1
        assert tuple(
            dialog.section_combo.itemData(i)
            for i in range(dialog.section_combo.count())
        )==tuple(first.runtime.section_keys)
        assert coordinator.active_address==SLMSectionAddress(
            "slm_a",first.runtime.section_keys[0],
        )
    finally:
        _dispose(group,coordinator,(first,first_panel),(second,second_panel))


def test_section_topology_signal_cancels_active_calibration_before_refresh(tmp_path):
    (
        _workspace,group,coordinator,first,first_panel,second,second_panel,
    )=_two_session_workspace(tmp_path)
    warnings=[]
    first.sigWarning.connect(lambda title,message:warnings.append((title,message)))
    try:
        address=SLMSectionAddress("slm_a",first.runtime.section_keys[0])
        assert coordinator.open(address)
        manager=first.calibration_manager
        manager._set_embedded_workflow(("base",address.section_key))
        assert manager.management_workflow_active

        # The host emits this signal after a real section collection replacement.
        # Emitting it directly isolates the coordinator's safety response.
        first.section_host.sigSectionsChanged.emit()

        assert not manager.management_workflow_active
        assert coordinator.active_address is not None
        assert warnings
        assert "section topology changed" in warnings[-1][1]
    finally:
        _dispose(group,coordinator,(first,first_panel),(second,second_panel))


def test_workspace_context_items_expose_exact_physical_identity_tooltips(tmp_path):
    (
        _workspace,group,coordinator,first,first_panel,second,second_panel,
    )=_two_session_workspace(tmp_path)
    from qtpy import QtCore
    try:
        section=second.runtime.section_keys[-1]
        assert coordinator.open(SLMSectionAddress("slm_b",section))
        dialog=coordinator.dialog
        slm_index=dialog.slm_combo.findData("slm_b")
        slm_tip=str(dialog.slm_combo.itemData(slm_index,QtCore.Qt.ToolTipRole) or "")
        assert "SLM: slm_b" in slm_tip
        assert "Serial: SER-B" in slm_tip

        section_index=dialog.section_combo.findData(section)
        section_tip=str(dialog.section_combo.itemData(section_index,QtCore.Qt.ToolTipRole) or "")
        assert "SLM: slm_b" in section_tip
        assert "Section: %s" % section in section_tip
        assert "Serial: SER-B" in section_tip
    finally:
        _dispose(group,coordinator,(first,first_panel),(second,second_panel))


def test_reference_source_label_falls_back_and_recovers_with_session_catalog(tmp_path):
    (
        workspace,group,coordinator,first,first_panel,second,second_panel,
    )=_two_session_workspace(tmp_path)
    section=second.runtime.section_keys[0]
    workspace.position_reference_store.add(
        _reference("from b",source_slm="slm_b",source_section=section)
    )
    try:
        assert coordinator.open(SLMSectionAddress("slm_a",first.runtime.section_keys[0]))
        dialog=coordinator.dialog
        dialog.tabs.setCurrentIndex(2)
        friendly=dialog.reference_view.table.item(0,3).text()
        assert "488 SLM" in friendly

        group.remove_session("slm_b")
        fallback=dialog.reference_view.table.item(0,3).text()
        assert fallback.startswith("slm_b / %s" % section)

        group.add_session(second,key="slm_b")
        recovered=dialog.reference_view.table.item(0,3).text()
        assert "488 SLM" in recovered
    finally:
        coordinator.dispose()
        group.remove_session("slm_a"); group.remove_session("slm_b")
        first.dispose(); second.dispose()
        first_panel.deleteLater(); second_panel.deleteLater()



def test_last_session_removal_clears_workflow_lock_state(tmp_path):
    _app()
    from slmcore.qt import SLMQtSessionFactory,SLMQtSessionGroup,SLMWorkspaceCoordinator

    workspace=SLMWorkspace(tmp_path)
    factory=SLMQtSessionFactory(workspace=workspace)
    first,first_panel=_session(factory,"slm_a","SER-A","405 SLM")
    group=SLMQtSessionGroup(); group.add_session(first,key="slm_a")
    coordinator=SLMWorkspaceCoordinator(group,workspace)
    try:
        address=SLMSectionAddress("slm_a",first.runtime.section_keys[0])
        assert coordinator.open(address)
        dialog=coordinator.dialog
        first.calibration_manager._set_embedded_workflow(("base",address.section_key))
        assert not dialog.context_switching_enabled

        group.remove_session("slm_a")

        assert coordinator.active_slm_key is None
        assert not first.calibration_manager.management_workflow_active
        assert dialog.context_switching_enabled
        assert dialog.slm_combo.count()==0
        assert dialog.section_combo.count()==0
    finally:
        coordinator.dispose()
        group.remove_session("slm_a")
        first.dispose(); first_panel.deleteLater()


def test_reference_live_label_requires_matching_stored_serial(tmp_path):
    (
        workspace,group,coordinator,first,first_panel,second,second_panel,
    )=_two_session_workspace(tmp_path)
    section=second.runtime.section_keys[0]
    workspace.position_reference_store.add(
        _reference(
            "old b",source_slm="slm_b",source_section=section,
            source_serial="OLD-SER-B",
        )
    )
    try:
        assert coordinator.open(SLMSectionAddress("slm_a",first.runtime.section_keys[0]))
        dialog=coordinator.dialog
        dialog.tabs.setCurrentIndex(2)
        source=dialog.reference_view.table.item(0,3).text()
        assert source.startswith("slm_b / %s" % section)
        assert "488 SLM" not in source
        source_tip=dialog.reference_view.table.item(0,3).toolTip()
        assert "OLD-SER-B" in source_tip
        assert "currently loaded serial=SER-B" in source_tip
    finally:
        _dispose(group,coordinator,(first,first_panel),(second,second_panel))


def test_section_title_refresh_is_deferred_until_active_workflow_finishes(tmp_path):
    (
        _workspace,group,coordinator,first,first_panel,second,second_panel,
    )=_two_session_workspace(tmp_path)
    try:
        section=first.runtime.section_keys[0]
        address=SLMSectionAddress("slm_a",section)
        assert coordinator.open(address)
        dialog=coordinator.dialog
        manager=first.calibration_manager
        index=dialog.section_combo.findData(section)
        old_text=dialog.section_combo.itemText(index)

        manager._set_embedded_workflow(("base",section))
        first.section_host.set_section_title(section,"Renamed while calibrating")

        index=dialog.section_combo.findData(section)
        assert dialog.section_combo.itemText(index)==old_text
        assert "slm_a" in coordinator._section_titles_refresh_pending

        manager._set_embedded_workflow(None)

        index=dialog.section_combo.findData(section)
        assert dialog.section_combo.itemText(index)=="Renamed while calibrating"
        assert "slm_a" not in coordinator._section_titles_refresh_pending
    finally:
        _dispose(group,coordinator,(first,first_panel),(second,second_panel))

def test_workspace_coordinator_dispose_is_idempotent_and_unhooks_sessions(tmp_path):
    (
        workspace,group,coordinator,first,first_panel,second,second_panel,
    )=_two_session_workspace(tmp_path)
    try:
        assert coordinator.open(SLMSectionAddress("slm_a",first.runtime.section_keys[0]))
        coordinator.dispose()
        coordinator.dispose()
        assert not coordinator._managers
        assert not coordinator._section_host_connections
        assert first.calibration_manager._shared_workspace_opener is None
        assert second.calibration_manager._shared_workspace_opener is None

        # Formerly observed sources can still emit after disposal without
        # invoking stale coordinator callbacks.
        first.section_host.sigSectionsChanged.emit()
        workspace.position_reference_store.add(_reference("after dispose"))
    finally:
        group.remove_session("slm_a"); group.remove_session("slm_b")
        first.dispose(); second.dispose()
        first_panel.deleteLater(); second_panel.deleteLater()
