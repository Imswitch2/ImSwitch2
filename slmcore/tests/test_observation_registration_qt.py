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
    finally:
        widget.deleteLater()

