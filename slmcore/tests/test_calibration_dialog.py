import numpy as np
import pytest
from types import SimpleNamespace


def _localization_defaults():
    try:
        from slmcore.core.cgh.localization import LOCALIZATION_PARAMS
    except Exception as error:
        pytest.skip(f"Localization parameter dependencies are unavailable: {error}")
    return {
        key:spec.default
        for key,spec in LOCALIZATION_PARAMS.items()
    }


def _qapp_and_dialog_class():
    pytest.importorskip("qtpy")
    pytest.importorskip("pyqtgraph")
    try:
        from qtpy import QtWidgets
        from slmcore.qt.calibration.dialog import CalibrationDialog
    except Exception as error:
        pytest.skip(f"CalibrationDialog dependencies are unavailable: {error}")

    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    return app,QtWidgets,CalibrationDialog


def _button_with_text(widget,QtWidgets,text):
    for button in widget.findChildren(QtWidgets.QPushButton):
        if button.text() == text:
            return button
    return None


def _target_method_combo(dialog,QtWidgets):
    for combo in dialog.findChildren(QtWidgets.QComboBox):
        if combo.findText("Target localization") >= 0:
            return combo
    return None


def test_calibration_dialog_linear_phase_emits_current_values():
    _app,QtWidgets,CalibrationDialog = _qapp_and_dialog_class()
    dialog = CalibrationDialog(
        plane_name="Plane A",
        localization_parameters=_localization_defaults(),
    )
    captured = []
    dialog.sigCalibrationRequested.connect(
        lambda method,values:captured.append((method,dict(values)))
    )

    try:
        save = _button_with_text(dialog,QtWidgets,"Save")
        assert save is not None
        save.click()

        assert captured
        method,values = captured[0]
        assert method == CalibrationDialog.LINEAR_PHASE
        assert values == {
            "plane_name":"Plane A",
            "period_x_px":100.0,
            "measured_dx_um":1.0,
            "period_y_px":100.0,
            "measured_dy_um":1.0,
        }
    finally:
        dialog.deleteLater()


def test_calibration_dialog_target_mode_uses_reusable_measurement_view():
    _app,QtWidgets,CalibrationDialog = _qapp_and_dialog_class()
    dialog = CalibrationDialog(
        plane_name="Plane A",
        localization_parameters=_localization_defaults(),
        detectors=("Camera",),
        current_detector="Camera",
    )
    acquired = []
    loaded = []
    run = []
    dialog.sigAcquireRequested.connect(acquired.append)
    dialog.sigLoadRequested.connect(lambda:loaded.append(True))
    dialog.sigLocalizationRunRequested.connect(lambda values:run.append(dict(values)))

    try:
        method_combo = _target_method_combo(dialog,QtWidgets)
        assert method_combo is not None
        method_combo.setCurrentIndex(
            method_combo.findText("Target localization")
        )

        set_calibration = _button_with_text(dialog,QtWidgets,"Set calibration")
        assert set_calibration is not None
        assert not set_calibration.isEnabled()

        dialog.set_target_reference(
            context={},
            parameters=_localization_defaults(),
            status="Ready",
        )
        from slmcore.core.measurement import ImageMeasurement
        dialog.set_target_measurement(
            ImageMeasurement(np.zeros((8,8)),source="test"),
            parameters=_localization_defaults(),
        )

        acquire = _button_with_text(dialog,QtWidgets,"Acquire")
        load = _button_with_text(dialog,QtWidgets,"Load...")
        localize = _button_with_text(dialog,QtWidgets,"Run Localization")
        assert acquire is not None
        assert load is not None
        assert localize is not None

        acquire.click()
        load.click()
        localize.click()

        assert acquired == ["Camera"]
        assert loaded == [True]
        assert run
    finally:
        dialog.deleteLater()


def test_calibration_dialog_target_candidate_enables_set_calibration():
    _app,QtWidgets,CalibrationDialog = _qapp_and_dialog_class()
    dialog = CalibrationDialog(
        plane_name="Plane A",
        localization_parameters=_localization_defaults(),
    )
    from slmcore.core.calibration import SLMSectionCalibration

    candidate = SimpleNamespace(
        calibration=SLMSectionCalibration(kx_per_um=0.01,ky_per_um=0.02),
        target_period_x_reference_px=6.0,
        target_period_y_reference_px=8.0,
        target_kx=6.0 / 512.0,
        target_ky=8.0 / 512.0,
        fitted_period_x_px=12.0,
        fitted_period_y_px=16.0,
        detector_pixel_size_um=0.5,
        measured_period_x_um=6.0,
        measured_period_y_um=8.0,
        matched_count=9,
        expected_count=9,
        rms_residual_px=0.2,
        warnings=(),
    )
    captured = []
    dialog.sigCalibrationRequested.connect(
        lambda method,values:captured.append((method,dict(values)))
    )

    try:
        method_combo = _target_method_combo(dialog,QtWidgets)
        assert method_combo is not None
        method_combo.setCurrentIndex(
            method_combo.findText("Target localization")
        )
        set_calibration = _button_with_text(dialog,QtWidgets,"Set calibration")
        assert set_calibration is not None
        assert not set_calibration.isEnabled()

        dialog.set_target_reference(
            context={},
            parameters=_localization_defaults(),
            status="Ready",
        )
        dialog.set_target_calibration_candidate(candidate)
        assert set_calibration.isEnabled()

        set_calibration.click()

        assert captured
        method,values = captured[0]
        assert method == CalibrationDialog.TARGET_LOCALIZATION
        assert values["plane_name"] == "Plane A"
        assert values["candidate"] is candidate
    finally:
        dialog.deleteLater()


def test_calibration_dialog_binds_live_acquisition_to_plane_detector_and_reason():
    _app,QtWidgets,CalibrationDialog = _qapp_and_dialog_class()
    dialog = CalibrationDialog(
        plane_name="Sample plane",
        localization_parameters=_localization_defaults(),
        detectors=("Camera",),
        current_detector="Camera",
    )
    try:
        dialog.set_bound_detector("Camera")
        dialog.set_target_reference(
            context={},parameters=_localization_defaults(),status="Ready",
        )
        dialog.set_live_acquisition_available(
            False,"Recompute the current CGH before acquiring.",
        )

        acquire = _button_with_text(dialog,QtWidgets,"Acquire")
        assert acquire is not None
        assert not acquire.isEnabled()
        assert "Recompute" in acquire.toolTip()
        assert dialog.target_view.current_detector == "Camera"
        assert not dialog.target_view._workbench.measurement_controls.detector_combo.isEnabled()
        assert "Sample plane" in dialog._plane_label.text()
        assert "Camera" in dialog._detector_label.text()
    finally:
        dialog.deleteLater()
