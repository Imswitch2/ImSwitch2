import numpy as np
import pytest
from types import SimpleNamespace


def _app():
    pytest.importorskip("qtpy")
    try:
        from qtpy import QtWidgets
    except Exception as error:
        pytest.skip("Qt bindings are unavailable: %s" % error)
    app=QtWidgets.QApplication.instance()
    if app is None:app=QtWidgets.QApplication([])
    return app


def test_management_workspace_keeps_orientation_and_registration_separate():
    _app()
    from slmcore.qt.calibration.management_dialogs import CalibrationManagementDialog
    dialog=CalibrationManagementDialog(
        sections=("sec_405","sec_488"),mappings=(),
        section_titles={"sec_405":"405","sec_488":"488"},
        current_section="sec_405",
    )
    try:
        assert dialog.geometry_list.parent().parent().title()=="Orientation by section"
        assert dialog.registration_list.parent().parent().title()=="Registration by section"
        dialog.set_registration_rows((
            {"section":"sec_405","section_title":"405","status":"Registered","canonical_title":"488","model":"polynomial 2","point_count":20},
            {"section":"sec_488","section_title":"488","status":"Canonical"},
        ))
        assert "Registered → 488" in dialog.registration_list.item(0).text()
        assert "Canonical" in dialog.registration_list.item(1).text()
        dialog.registration_list.setCurrentRow(1)
        assert not dialog.registration_calibrate.isEnabled()
        dialog.registration_list.setCurrentRow(0)
        assert dialog.registration_calibrate.isEnabled()
        assert dialog.registration_calibrate.text()=="Inspect / Refit"
    finally:
        dialog.deleteLater()


def test_registration_widget_presents_fit_diagnostics_and_reference_availability():
    _app()
    from slmcore.qt.calibration.observation_registration_dialog import ObservationRegistrationCalibrationWidget
    reference=SimpleNamespace(
        name="488-ref",lifetime=SimpleNamespace(value="session"),
        reference=SimpleNamespace(metadata={"source_slm_serial":"SER488","source_section":"sec_488"}),
    )
    widget=ObservationRegistrationCalibrationWidget(
        native_section_key="sec_405",native_section_title="405",
        observation_plane_name="Intermediate",
        references=(reference,),canonical_reference_name="488-ref",
    )
    diagnostics=SimpleNamespace(
        point_count=4,
        native_points_px=np.array([[0.,1.,0.,1.],[0.,0.,1.,1.]]),
        canonical_points_px=np.array([[2.,3.,2.,3.],[4.,4.,5.,5.]]),
        transformed_native_points_px=np.array([[2.,3.,2.,3.],[4.,4.,5.,5.]]),
        forward_residual_vectors_px=np.zeros((2,4)),
        transformed_canonical_points_px=np.array([[0.,1.,0.,1.],[0.,0.,1.,1.]]),
        reverse_residual_vectors_px=np.zeros((2,4)),
        forward_rms_residual_px=0.01,forward_max_residual_px=0.02,
        reverse_rms_residual_px=0.015,reverse_max_residual_px=0.03,
    )
    native=SimpleNamespace(measurement_id="m1",matched_count=4)
    canonical=SimpleNamespace(
        lifetime=SimpleNamespace(value="session"),valid_count=4,total_count=4,
        frame=SimpleNamespace(slm_serial="SER488",section_key="sec_488"),
        sample_policy="genuinely_measured_sites_from_reference",
    )
    context=SimpleNamespace(
        native_snapshot=native,canonical_snapshot=canonical,
        available_references=(reference,),canonical_reference_name="488-ref",
        latest_native_available=True,fit_enabled=True,save_enabled=True,
        common_site_count=4,diagnostics=diagnostics,
        correspondence_requested="auto",correspondence_resolved="flip_x",
        correspondence_options=("auto","canonical","flip_x","flip_y","rotate_180"),
        correspondence_diagnostics={
            "requested":"auto","resolved":"flip_x","fit_count":4,
            "relative_rotation_deg":0.1,"rms_px":0.2,"reflection":False,
            "median_displacement_px":3.0,
        },
        candidate=SimpleNamespace(transform_model="polynomial",polynomial_degree=2),
    )
    try:
        widget.set_context(context)
        assert widget.fit_button.isEnabled()
        assert widget.save_button.isEnabled()
        assert "Forward RMS" in widget.diagnostics_label.text()
        assert widget.native_use.isEnabled()
        assert widget.canonical_reference_name=="488-ref"
        assert "SER488" in widget.reference_status.text()
        assert widget.correspondence_combo.currentData()=="auto"
        assert "Auto" in widget.correspondence_combo.currentText()
        assert "Flip X" in widget.correspondence_info.text()
        assert "4 matched sites" in widget.correspondence_info.text()
        assert widget.preview.direction_combo.count()==2
        assert "Native → Canonical" in widget.preview.summary_label.text()
        widget.preview.direction_combo.setCurrentIndex(1)
        assert "Canonical → Native" in widget.preview.summary_label.text()
    finally:
        widget.deleteLater()



def test_registration_widget_emits_manual_correspondence_override():
    _app()
    from slmcore.qt.calibration.observation_registration_dialog import ObservationRegistrationCalibrationWidget
    reference=SimpleNamespace(
        name="488-ref",lifetime=SimpleNamespace(value="session"),
        reference=SimpleNamespace(metadata={"source_slm_serial":"SER488","source_section":"sec_488"}),
    )
    widget=ObservationRegistrationCalibrationWidget(
        native_section_key="sec_405",native_section_title="405",
        observation_plane_name="Intermediate",references=(reference,),
        canonical_reference_name="488-ref",
    )
    native=SimpleNamespace(measurement_id="m1",matched_count=4)
    canonical=SimpleNamespace(
        lifetime=SimpleNamespace(value="session"),valid_count=4,total_count=4,
        frame=SimpleNamespace(slm_serial="SER488",section_key="sec_488"),
        sample_policy="genuinely_measured_sites_from_reference",
    )
    context=SimpleNamespace(
        native_snapshot=native,canonical_snapshot=canonical,
        available_references=(reference,),canonical_reference_name="488-ref",
        latest_native_available=True,fit_enabled=True,save_enabled=False,
        common_site_count=4,diagnostics=None,candidate=None,
        correspondence_requested="auto",correspondence_resolved="flip_x",
        correspondence_options=("auto","canonical","flip_x"),
        correspondence_diagnostics={"resolved":"flip_x","fit_count":4,"rms_px":0.2},
    )
    emitted=[]
    widget.sigCorrespondenceChanged.connect(emitted.append)
    try:
        widget.set_context(context)
        index=widget.correspondence_combo.findData("flip_x")
        assert index>=0
        widget.correspondence_combo.setCurrentIndex(index)
        assert emitted==["flip_x"]
    finally:
        widget.deleteLater()


def test_registration_workspace_prioritizes_viewer_and_preserves_zoom_on_refresh():
    _app()
    from qtpy import QtCore
    from slmcore.qt.calibration.observation_registration_dialog import ObservationRegistrationCalibrationWidget
    reference=SimpleNamespace(
        name="488-ref",lifetime=SimpleNamespace(value="session"),
        reference=SimpleNamespace(metadata={"source_slm_serial":"SER488","source_section":"sec_488"}),
    )
    widget=ObservationRegistrationCalibrationWidget(
        native_section_key="sec_405",native_section_title="405",
        observation_plane_name="Intermediate",references=(reference,),
        canonical_reference_name="488-ref",
    )
    diagnostics=SimpleNamespace(
        point_count=4,
        native_points_px=np.array([[0.,10.,0.,10.],[0.,0.,10.,10.]]),
        canonical_points_px=np.array([[2.,12.,2.,12.],[4.,4.,14.,14.]]),
        transformed_native_points_px=np.array([[2.,12.,2.,12.],[4.,4.,14.,14.]]),
        forward_residual_vectors_px=np.zeros((2,4)),
        transformed_canonical_points_px=np.array([[0.,10.,0.,10.],[0.,0.,10.,10.]]),
        reverse_residual_vectors_px=np.zeros((2,4)),
        forward_rms_residual_px=0.0,forward_max_residual_px=0.0,
        reverse_rms_residual_px=0.0,reverse_max_residual_px=0.0,
    )
    try:
        assert widget.workflow_splitter.orientation()==QtCore.Qt.Horizontal
        assert widget.viewer_tabs.count()==2
        assert widget.viewer_tabs.tabText(0)=="Measured vs canonical"
        assert widget.viewer_tabs.tabText(1)=="Registration field"

        widget.preview.set_diagnostics(diagnostics)
        widget.preview.view_box.setRange(xRange=(3.,6.),yRange=(4.,7.),padding=0.0)
        before=widget.preview.view_box.viewRange()
        # The periodic UI refresh may resend the same diagnostics object/state.
        # This must update overlays without forcing autoRange.
        widget.preview.set_diagnostics(diagnostics)
        after=widget.preview.view_box.viewRange()
        assert np.allclose(before,after)
        assert widget.preview.reset_button.isEnabled()
    finally:
        widget.deleteLater()


def test_saved_registration_widget_is_read_only_except_for_refit_controls():
    _app()
    from slmcore.qt.calibration.observation_registration_dialog import ObservationRegistrationCalibrationWidget
    widget=ObservationRegistrationCalibrationWidget(
        native_section_key="sec_405",native_section_title="405",
        observation_plane_name="Intermediate",references=(),
        canonical_reference_name="session-ref",
    )
    diagnostics=SimpleNamespace(
        point_count=4,
        native_points_px=np.array([[0.,1.,0.,1.],[0.,0.,1.,1.]]),
        canonical_points_px=np.array([[2.,3.,2.,3.],[4.,4.,5.,5.]]),
        transformed_native_points_px=np.array([[2.,3.,2.,3.],[4.,4.,5.,5.]]),
        forward_residual_vectors_px=np.zeros((2,4)),
        transformed_canonical_points_px=np.array([[0.,1.,0.,1.],[0.,0.,1.,1.]]),
        reverse_residual_vectors_px=np.zeros((2,4)),
        forward_rms_residual_px=0.01,forward_max_residual_px=0.02,
        reverse_rms_residual_px=0.015,reverse_max_residual_px=0.03,
    )
    candidate=SimpleNamespace(
        transform_model="polynomial",polynomial_degree=3,point_count=4,
        created_at="saved-1",
        canonical_frame=SimpleNamespace(slm_serial="SER488",section_key="sec_488"),
        provenance={"canonical_reference_name":"session-ref"},
    )
    context=SimpleNamespace(
        saved_edit_mode=True,native_snapshot=None,canonical_snapshot=None,
        available_references=(),canonical_reference_name="session-ref",
        latest_native_available=False,fit_enabled=True,save_enabled=True,
        common_site_count=4,diagnostics=diagnostics,candidate=candidate,
        correspondence_requested="auto",correspondence_resolved="flip_x",
        correspondence_options=("auto",),
        correspondence_diagnostics={"requested":"auto","resolved":"flip_x","fit_count":4},
    )
    emitted=[]
    widget.sigNewCalibrationRequested.connect(lambda:emitted.append("new"))
    try:
        widget.set_context(context)
        assert not widget.reference_combo.isEnabled()
        assert not widget.correspondence_combo.isEnabled()
        assert not widget.native_group.isVisible()
        assert widget.new_calibration_button.isVisible()
        assert widget.fit_button.text()=="Refit"
        assert widget.save_button.text()=="Save update"
        assert widget.cancel_button.text()=="Close"
        assert widget.model_combo.currentData()=="polynomial"
        assert widget.degree_combo.currentData()==3
        assert "Fixed after save" in widget.correspondence_info.text()
        assert "session-ref" in widget.reference_status.text()
        widget.new_calibration_button.click()
        assert emitted==["new"]
    finally:
        widget.deleteLater()
