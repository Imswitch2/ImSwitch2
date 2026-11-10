"""Focused GUI-contract tests for the compact unified MoNaLISA widget."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtCore, QtWidgets  # noqa: E402

from imswitch.improcess.reconstructors.monalisa.params import (  # noqa: E402
    PRESET_CUSTOM,
    PRESET_FAST,
    PRESET_FULL,
    PRESET_LEGACY,
    default_params,
)
from imswitch.improcess.reconstructors.monalisa.params_widget import MonalisaParamsWidget  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_compact_widget_defaults_match_the_backend_contract(qapp):
    widget = MonalisaParamsWidget()
    assert widget.get_values() == default_params()


def test_preview_starts_visible_and_workflow_is_scrollable(qapp):
    widget = MonalisaParamsWidget()
    assert widget.previewCheckbox.isChecked() is True
    assert widget.scrollArea.widgetResizable() is True
    assert widget.scrollArea.verticalScrollBarPolicy() == QtCore.Qt.ScrollBarAsNeeded
    assert widget.scrollArea.horizontalScrollBarPolicy() == QtCore.Qt.ScrollBarAlwaysOff
    assert widget.previewStatusLabel.minimumHeight() > 0
    assert widget.scanStatusLabel.minimumHeight() > 0


def test_fast_mode_shows_cheap_corrections_but_hides_heavier_sections(qapp):
    widget = MonalisaParamsWidget()
    assert widget.get_values()["preset"] == PRESET_FAST
    assert widget.localizationBox.isHidden() is False
    assert widget.scanningBox.isHidden() is False
    assert widget.spotBox.isHidden() is False
    assert widget.ismBox.isHidden() is True
    assert widget.correctionsBox.isHidden() is False
    assert widget.outputBox.isHidden() is True
    assert widget.legacyBox.isHidden() is True
    assert widget.spotModeCombo.isHidden() is False
    assert widget.advancedButton.isHidden() is False


def test_fast_corrections_do_not_turn_the_mode_into_custom(qapp):
    widget = MonalisaParamsWidget()
    widget.intensityCheck.setChecked(True)
    widget.cellOffsetsCheck.setChecked(True)
    values = widget.get_values()
    assert values["preset"] == PRESET_FAST
    assert values["intensity_correction"] is True
    assert values["cell_offsets"] is True


def test_full_auto_is_the_ui_name_for_the_existing_full_contract_value(qapp):
    widget = MonalisaParamsWidget()
    index = widget.modeCombo.findText("Full (Auto + ISM)")
    widget.modeCombo.setCurrentIndex(index)
    values = widget.get_values()
    assert values["preset"] == PRESET_FULL
    assert values["spot_width_mode"] == "Auto"
    assert values["reassignment"] == "auto"
    assert widget.ismBox.isHidden() is False



def test_legacy_dll_is_a_unified_mode_with_classic_controls(qapp):
    widget = MonalisaParamsWidget()
    widget.modeCombo.setCurrentIndex(widget.modeCombo.findText("Legacy DLL"))
    values = widget.get_values()
    assert values["preset"] == PRESET_LEGACY
    assert widget.legacyBox.isHidden() is False
    assert widget.correctionsBox.isHidden() is True
    assert widget.ismBox.isHidden() is True
    assert widget.outputBox.isHidden() is True
    assert widget.orientationModeCombo.isEnabled() is False
    assert widget.scanningAdvancedButton.isEnabled() is False
    assert values["device"] == "CPU"


def test_fast_fwhm_is_a_normal_edit_not_a_customization(qapp):
    widget = MonalisaParamsWidget()
    widget.fwhmSpin.setValue(widget.fwhmSpin.value() + 5.0)
    assert widget.get_values()["preset"] == PRESET_FAST


def test_changing_a_mode_controlled_option_enters_custom(qapp):
    widget = MonalisaParamsWidget()
    widget.modeCombo.setCurrentIndex(widget.modeCombo.findText("Full (Auto + ISM)"))
    widget.reassignmentCombo.setCurrentIndex(widget.reassignmentCombo.findText("Manual"))
    assert widget.get_values()["preset"] == PRESET_CUSTOM


def _combo_item_enabled(combo, text):
    index = combo.findText(text)
    assert index >= 0
    item = combo.model().item(index)
    return item.isEnabled()


def test_locked_sources_are_disabled_until_a_lock_exists(qapp, tmp_path):
    from imswitch.improcess.reconstructors.monalisa.session_registry import MonalisaSessionRegistry

    registry = MonalisaSessionRegistry(tmp_path)
    widget = MonalisaParamsWidget(session_registry=registry)

    assert _combo_item_enabled(widget.localizationModeCombo, "Locked") is False
    assert _combo_item_enabled(widget.orientationModeCombo, "Locked") is False
    assert "No locked localization" in widget.localizationModeCombo.toolTip()
    assert "No locked orientation" in widget.orientationModeCombo.toolTip()


def test_lock_current_localization_selects_it_and_enters_locked_mode(qapp, tmp_path, monkeypatch):
    from imswitch.improcess.reconstructors.monalisa.calibration import PatternLocalization
    from imswitch.improcess.reconstructors.monalisa.params import LOCALIZATION_LOCKED
    from imswitch.improcess.reconstructors.monalisa.session_registry import MonalisaSessionRegistry

    registry = MonalisaSessionRegistry(tmp_path)
    widget = MonalisaParamsWidget(session_registry=registry)
    localization = PatternLocalization.manual_grid(
        period_x=10.0,
        period_y=12.0,
        offset_x=1.0,
        offset_y=2.0,
        source="auto",
    )
    widget.set_auto_localization(localization, "ready")
    monkeypatch.setattr(
        QtWidgets.QInputDialog,
        "getText",
        lambda *args, **kwargs: ("Good localization", True),
    )

    widget._lock_current_localization()

    assert widget._combo_data(widget.localizationModeCombo) == LOCALIZATION_LOCKED
    assert widget._selected_locked_localization_id
    assert widget._combo_data(widget.lockedLocalizationCombo) == widget._selected_locked_localization_id
    assert _combo_item_enabled(widget.localizationModeCombo, "Locked") is True


def test_compact_scan_summary_shows_only_meaningful_effective_step_change(qapp):
    widget = MonalisaParamsWidget()
    widget._recorded_scan_axes = {
        "y": {"count": 21, "step_nm": 35.0},
    }
    widget._recorded_scan_summary = "Recorded: Y 21, step 35 nm"
    widget._scan_result_summary = {
        "orientation": "-x-y",
        "output_pixel_nm": (36.0, 36.0),
        "coverage_holes": 0,
        "coverage_overlaps": 0,
        "coverage_cell_pixels": 21,
    }

    widget._refresh_orientation_display()

    assert widget.orientationResultLabel.text() == "-x-y"
    assert "Y scan" in widget.scanSummaryLabel.text()
    assert "21 pos." in widget.scanSummaryLabel.text()
    assert "35.0 → 36.0 nm" in widget.scanSummaryLabel.text()
    assert widget.scanStatusLabel.text() == "✓ 21/21 • 0 holes • adjusted +2.9%"


def test_lock_current_orientation_enters_locked_mode(qapp, tmp_path):
    from imswitch.improcess.reconstructors.monalisa.params import ORIENTATION_LOCKED
    from imswitch.improcess.reconstructors.monalisa.session_registry import MonalisaSessionRegistry

    registry = MonalisaSessionRegistry(tmp_path)
    widget = MonalisaParamsWidget(session_registry=registry)
    registry.set_current_orientation("unknown", "-x-y", source="auto")
    widget._refresh_registry_controls()

    widget._lock_current_orientation()

    assert widget._combo_data(widget.orientationModeCombo) == ORIENTATION_LOCKED
    assert widget.orientationResultLabel.text() == "-x-y"
    assert _combo_item_enabled(widget.orientationModeCombo, "Locked") is True


def test_background_localization_busy_state_disables_duplicate_auto_actions(qapp):
    from imswitch.improcess.reconstructors.monalisa.calibration import PatternLocalization

    widget = MonalisaParamsWidget()
    localization = PatternLocalization.manual_grid(
        period_x=10.0,
        period_y=10.0,
        offset_x=0.0,
        offset_y=0.0,
        source="auto",
    )
    widget.set_auto_localization(localization, "ready")
    assert widget.localizeButton.isEnabled() is True
    assert widget.lockLocalizationButton.isEnabled() is True

    widget.set_localization_job_state(True, "Auto: localizing…")

    assert widget.localizeButton.isEnabled() is False
    assert widget.lockLocalizationButton.isEnabled() is False
    assert widget.localizationSettingsButton.isEnabled() is True
    assert widget.previewStatusLabel.text() == "Auto: localizing…"

    widget.set_localization_job_state(False)
    assert widget.localizeButton.isEnabled() is True
    assert widget.lockLocalizationButton.isEnabled() is True


def test_embedded_localization_counts_as_current_auto_result(qapp):
    from types import SimpleNamespace

    from imswitch.improcess.reconstructors.monalisa.calibration import PatternLocalization

    widget = MonalisaParamsWidget()
    localization = PatternLocalization.manual_grid(
        period_x=10.0,
        period_y=12.0,
        offset_x=1.0,
        offset_y=2.0,
        source="auto",
    )
    data_obj = SimpleNamespace(
        name="reconstructed-data",
        dataPath="/tmp/reconstructed-data.hdf5",
        acquisition_layout=None,
    )
    widget.load_from_attrs(
        {
            "monalisa_calibration": {
                "resolved_localization": localization.to_dict(),
            }
        },
        data_obj,
    )

    assert widget.has_auto_localization_for_current_data() is True
    assert "Embedded localization" in widget.previewStatusLabel.text()


def test_fitting_settings_are_editable_in_main_panel(qapp):
    widget = MonalisaParamsWidget()
    assert widget._combo_data(widget.estimatorCombo) == "local"
    assert widget._combo_data(widget.footprintShapeCombo) == "square"
    assert widget._combo_data(widget.footprintModeCombo) == "from_lattice"
    assert widget.fitCalculatedSizeLabel.isVisible() is False or widget.fitCalculatedSizeLabel.text() == "Auto size"
    widget._set_combo_data(widget.footprintModeCombo, "fixed")
    assert widget.footprintSizeSpin.isHidden() is False
    widget.footprintSizeSpin.setValue(11)
    assert widget.get_values()["footprint_size_px"] == 11
    widget._set_combo_data(widget.footprintShapeCombo, "circle")
    widget.footprintSizeSpin.setValue(10.5)
    assert widget.get_values()["footprint_size_px"] == 10.5
    assert widget.get_values()["preset"] == PRESET_CUSTOM

def test_calibrated_fwhm_can_be_fixed_then_saved_to_setup(qapp, tmp_path, monkeypatch):
    import json
    from imswitch.improcess.reconstructors.monalisa import params_widget as module
    from imswitch.improcess.reconstructors.monalisa.setup_defaults import read_detector_defaults

    path = tmp_path / "hardware.json"
    path.write_text(json.dumps({"detectors": {"CAM": {"managerProperties": {}}}}))
    monkeypatch.setattr(module, "current_setup_path", lambda: path)
    monkeypatch.setattr(module, "read_detector_defaults", lambda detector, path=path: read_detector_defaults(detector, path=path))
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *args, **kwargs: QtWidgets.QMessageBox.Yes)
    widget = MonalisaParamsWidget()
    widget.load_from_attrs({"recording:detector_name": "CAM"})
    widget._set_combo_data(widget.spotModeCombo, "Auto")
    widget.set_auto_spot_sigma_px(1.5)
    assert widget.useEstimatedFwhmButton.isEnabled()
    widget._use_estimated_fwhm()
    assert widget.get_values()["spot_width_mode"] == "Manual/default"
    widget._save_fwhm_default()
    assert read_detector_defaults("CAM", path=path).fwhm_nm == pytest.approx(widget.fwhmSpin.value())
    assert widget.get_values()["spot_width_source"] == "Setup default"


def test_orientation_setup_default_is_explicitly_saved_and_recorded_value_wins(qapp, tmp_path, monkeypatch):
    import json
    from imswitch.improcess.reconstructors.monalisa import params_widget as module
    from imswitch.improcess.reconstructors.monalisa.setup_defaults import read_detector_defaults

    path = tmp_path / "hardware.json"
    path.write_text(json.dumps({"detectors": {"CAM": {"managerProperties": {}}}}))
    monkeypatch.setattr(module, "current_setup_path", lambda: path)
    monkeypatch.setattr(module, "read_detector_defaults", lambda detector, path=path: read_detector_defaults(detector, path=path))
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *args, **kwargs: QtWidgets.QMessageBox.Yes)
    widget = MonalisaParamsWidget()
    widget.load_from_attrs({"recording:detector_name": "CAM"})
    widget._set_combo_data(widget.orientationModeCombo, "Manual")
    widget._set_combo_data(widget.orientationCombo, "-x+y")
    widget._save_orientation_default()
    assert read_detector_defaults("CAM", path=path).orientation == "-x+y"
    assert widget.get_values()["orientation_mode"] == "Setup default"
    assert widget.get_values()["orientation"] == "-x+y"

    # An older recording carries its own orientation; never substitute the
    # newly configured setup orientation silently.
    widget2 = MonalisaParamsWidget()
    widget2.load_from_attrs({"recording:detector_name": "CAM", "monalisa:scan_orientation": "+y-x"})
    assert widget2.get_values()["orientation_mode"] == "Recorded"
    assert widget2.get_values()["orientation"] == "+y-x"
    widget2._set_combo_data(widget2.orientationModeCombo, "Setup default")
    assert widget2.get_values()["orientation"] == "-x+y"


def test_pre_reconstruction_estimate_keeps_manual_editing_and_rejects_stale_data(qapp):
    from imswitch.improcess.reconstructors.monalisa.params import SPOT_AUTO, SPOT_MANUAL

    widget = MonalisaParamsWidget()
    current = object()
    widget._current_data_obj = current
    widget._fwhm_estimate_serial = 1
    widget._fwhm_estimate_busy = True
    widget._fwhm_estimate_revision = widget._fwhm_data_revision
    widget._fwhm_estimate_signature = widget._fwhm_calibration_signature(widget.get_values())
    widget._set_combo_data(widget.spotModeCombo, SPOT_AUTO)
    # The Estimate action is independent of Auto during reconstruction.
    widget._finish_fwhm_estimate((1, current, 1.8, "measured", None, ""))
    assert widget.get_values()["spot_width_mode"] == SPOT_MANUAL
    assert widget.fwhmSpin.isEnabled()
    assert widget.fwhmSpin.value() == pytest.approx(1.8 * 2.35482 * 77.0, rel=0.001)
    widget.fwhmSpin.setValue(200.0)
    assert widget.get_values()["spot_fwhm"] == 200.0
    # A result for the previous dataset cannot overwrite the new one's width.
    widget._fwhm_estimate_serial = 2
    widget._fwhm_estimate_busy = True
    widget._fwhm_estimate_revision = widget._fwhm_data_revision
    widget._fwhm_estimate_signature = widget._fwhm_calibration_signature(widget.get_values())
    widget._current_data_obj = object()
    widget._finish_fwhm_estimate((2, current, 4.0, "old scan", None, ""))
    assert widget.fwhmSpin.value() == 200.0


def test_saved_fitting_defaults_include_estimator(qapp, tmp_path, monkeypatch):
    import json
    from imswitch.improcess.reconstructors.monalisa import params_widget as module
    from imswitch.improcess.reconstructors.monalisa.setup_defaults import read_detector_defaults

    path = tmp_path / "hardware.json"
    path.write_text(json.dumps({"detectors": {"CAM": {"managerProperties": {}}}}))
    monkeypatch.setattr(module, "current_setup_path", lambda: path)
    monkeypatch.setattr(
        module, "read_detector_defaults",
        lambda detector, path=path: read_detector_defaults(detector, path=path),
    )
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question", lambda *args, **kwargs: QtWidgets.QMessageBox.Yes,
    )
    widget = MonalisaParamsWidget()
    widget.load_from_attrs({"recording:detector_name": "CAM"})
    widget._set_combo_data(widget.estimatorCombo, "joint")
    widget._set_combo_data(widget.footprintShapeCombo, "circle")
    widget._save_footprint_default()
    saved = read_detector_defaults("CAM", path=path)
    assert (saved.estimator, saved.footprint_mode, saved.footprint_shape) == (
        "joint", "from_lattice", "circle",
    )
    new_widget = MonalisaParamsWidget()
    new_widget.load_from_attrs({"recording:detector_name": "CAM"})
    assert new_widget.get_values()["estimator"] == "joint"
    assert new_widget.get_values()["footprint_shape"] == "circle"
    new_widget._set_combo_data(new_widget.estimatorCombo, "local")
    assert "Modified" in new_widget.footprintSourceLabel.text()
    new_widget.useSetupFittingAction.trigger()
    assert new_widget.get_values()["estimator"] == "joint"
    assert new_widget.footprintSourceLabel.text() == "Setup default"

def test_joint_support_is_editable_in_main_spot_model(qapp):
    widget = MonalisaParamsWidget()
    widget._set_combo_data(widget.estimatorCombo, "joint")
    widget._set_combo_data(widget.footprintModeCombo, "fixed")
    widget._set_combo_data(widget.footprintShapeCombo, "circle")
    widget.footprintSizeSpin.setValue(10.5)
    assert widget.get_values()["footprint_size_px"] == 10.5
    widget._set_combo_data(widget.estimatorCombo, "local")
    assert widget.get_values()["footprint_size_px"] == 10.5
    widget._set_combo_data(widget.footprintShapeCombo, "lattice")
    assert widget.footprintModeCombo.isEnabled() is False
    assert widget.footprintSizeSpin.isHidden() is True
    assert widget.footprintSigmaSpin.isHidden() is True

def test_editing_joint_support_marks_full_as_custom(qapp):
    widget = MonalisaParamsWidget()
    widget.modeCombo.setCurrentIndex(widget.modeCombo.findText("Full (Auto + ISM)"))
    assert widget.get_values()["preset"] == PRESET_FULL
    widget._set_combo_data(widget.footprintShapeCombo, "lattice")
    values = widget.get_values()
    assert values["preset"] == PRESET_CUSTOM
    assert values["footprint_shape"] == "lattice"

def test_spot_model_keeps_fwhm_editable_and_inline_fitting_header(qapp):
    widget = MonalisaParamsWidget()
    layout = widget.spotBox.layout()
    assert layout.itemAtPosition(0, 0).widget() is widget.fwhmLabel
    assert layout.itemAtPosition(0, 4).widget() is widget.estimateFwhmButton
    assert layout.itemAtPosition(0, 5).widget() is widget.fwhmMenuButton
    assert layout.itemAtPosition(1, 0).widget() is widget.fittingHeader
    assert layout.itemAtPosition(2, 0).widget() is widget.fittingDetailsWidget
    assert widget.fittingMenuButton.menu() is widget.fittingMenu
    assert widget.fwhmMenuButton.menu() is widget.fwhmMenu
    assert widget.get_values() == default_params()


def test_expert_background_roundtrips_without_changing_preset(qapp, monkeypatch):
    from imswitch.improcess.reconstructors.monalisa.advanced_settings import AdvancedReconstructionDialog

    widget = MonalisaParamsWidget()
    assert widget.get_values()["background"] == "constant"

    def accept_background(dialog):
        dialog.background.setCurrentText("none")
        return QtWidgets.QDialog.Accepted

    monkeypatch.setattr(AdvancedReconstructionDialog, "exec_", accept_background)
    widget._open_advanced_reconstruction()
    assert widget.get_values()["background"] == "none"
    # Background did not control the preset before Patch A either.
    assert widget.get_values()["preset"] == PRESET_FAST


def test_fitting_status_compares_saved_estimator_and_footprint(qapp):
    from imswitch.improcess.reconstructors.monalisa.setup_defaults import DetectorSetupDefaults

    widget = MonalisaParamsWidget()
    widget._setup_defaults = DetectorSetupDefaults(
        detector="CAM", footprint_mode="from_lattice", footprint_shape="square",
        footprint_size=9.0, estimator="joint",
    )
    widget._advanced_values["estimator"] = "joint"
    widget._update_ui()
    assert widget.footprintSourceLabel.text() == "Setup default"
    widget._advanced_values["footprint_size_px"] = 15.0
    widget._update_ui()
    assert widget.footprintSourceLabel.text() == "Setup default"  # unused fixed size
    widget._advanced_values["estimator"] = "local"
    widget._update_ui()
    assert widget.footprintSourceLabel.text() == "Modified from setup"

def test_legacy_hides_gaussian_fitting_but_keeps_spot_width(qapp):
    widget = MonalisaParamsWidget()
    widget.modeCombo.setCurrentIndex(widget.modeCombo.findText("Legacy DLL"))
    assert widget.fwhmSpin.isHidden() is False
    assert widget.fittingHeader.isHidden() is True
    assert widget.estimatorCombo.isHidden() is True
    assert widget.footprintSourceLabel.isHidden() is True
    assert widget.footprintSizeSpin.isHidden() is True
    assert widget.get_values()["background"] == "constant"


def test_footprint_value_field_only_shows_for_relevant_sizing(qapp):
    widget = MonalisaParamsWidget()
    assert widget.footprintValueStack.currentIndex() == 2  # From lattice
    widget._set_combo_data(widget.footprintModeCombo, "fixed")
    assert widget.footprintValueStack.currentIndex() == 0
    widget._set_combo_data(widget.footprintModeCombo, "fwhm")
    assert widget.footprintValueStack.currentIndex() == 1
    widget._set_combo_data(widget.footprintShapeCombo, "lattice")
    assert widget.footprintValueStack.currentIndex() == 3  # no size for cells


def test_expert_only_contains_specialist_numerical_settings(qapp):
    from imswitch.improcess.reconstructors.monalisa.advanced_settings import AdvancedReconstructionDialog
    widget = MonalisaParamsWidget()
    dialog = AdvancedReconstructionDialog(widget._advanced_values)
    assert hasattr(dialog, "background")
    assert hasattr(dialog, "sampling")
    assert not hasattr(dialog, "footprintShape")
    assert not hasattr(dialog, "estimator")
    assert "estimator" not in dialog.values()


def test_spot_model_source_badges_do_not_consume_space(qapp):
    widget = MonalisaParamsWidget()
    spot = widget.spotBox.layout()
    fitting = widget.fittingDetailsWidget.layout()
    assert widget.spotModeCombo.findText("Manual") >= 0  # real mode selector stays
    assert widget.fwhmSourceLabel.isHidden()
    assert widget.footprintSourceLabel.isHidden()
    assert spot.itemAtPosition(1, 0).widget() is widget.fittingHeader
    assert spot.itemAtPosition(2, 0).widget() is widget.fittingDetailsWidget
    assert fitting.itemAtPosition(1, 0) is None
    assert "Application default" not in widget.fittingToggle.text()
    assert "FWHM source:" in widget.fwhmMenuButton.toolTip()


def test_spot_model_keeps_fwhm_visible_and_collapses_only_fitting(qapp):
    widget = MonalisaParamsWidget()
    assert widget.fittingToggle.isChecked() is False
    assert widget.fittingDetailsWidget.isHidden() is True
    assert widget.fwhmSpin.isHidden() is False
    assert widget.estimateFwhmButton.isHidden() is False
    assert widget.fittingHeader._summary.startswith("Local · Square · From lattice")
    assert widget.fittingHeader._status == ""  # no redundant startup badge

    widget.fittingToggle.click()
    assert widget.fittingToggle.isChecked() is True
    assert widget.fittingDetailsWidget.isHidden() is False
    assert widget.estimatorCombo.isHidden() is False
    widget.fwhmSpin.setValue(230.0)
    assert widget.get_values()["spot_fwhm"] == pytest.approx(230.0)

    widget.fittingToggle.click()
    assert widget.fittingDetailsWidget.isHidden() is True
    assert widget.fwhmSpin.isHidden() is False
    assert "From lattice" in widget.fittingHeader._summary


def test_original_joint_auto_2p5_sigma_is_editable_relative_support(qapp):
    widget = MonalisaParamsWidget()
    widget.fittingToggle.click()
    widget._set_combo_data(widget.estimatorCombo, "joint")
    widget._set_combo_data(widget.footprintShapeCombo, "circle")
    widget._set_combo_data(widget.footprintModeCombo, "fwhm")
    assert widget.footprintModeCombo.count() == 3
    assert widget.footprintModeCombo.findText("Auto (2.5σ)") == -1
    result = widget.get_values()
    assert result["estimator"] == "joint"
    assert result["footprint_shape"] == "circle"
    assert result["footprint_mode"] == "fwhm"
    assert result["footprint_sigma_factor"] == pytest.approx(2.5)
    assert widget.footprintValueStack.currentIndex() == 1
    assert widget.footprintSigmaSpin.isHidden() is False
    assert "Circle · 2.5σ radius" in widget.fittingHeader._summary

    # Same controls for any factor and for Local. The mode never changes when
    # the factor crosses 2.5, including exactly 2.5 again.
    widget.footprintSigmaSpin.setValue(3.0)
    assert widget.get_values()["footprint_sigma_factor"] == pytest.approx(3.0)
    assert widget._combo_data(widget.footprintModeCombo) == "fwhm"
    widget._set_combo_data(widget.estimatorCombo, "local")
    widget.footprintSigmaSpin.setValue(2.5)
    assert widget._combo_data(widget.footprintModeCombo) == "fwhm"
    assert widget.footprintValueStack.currentIndex() == 1
    assert widget.footprintSigmaSpin.isHidden() is False
    widget._set_combo_data(widget.footprintShapeCombo, "square")
    assert widget.get_values()["footprint_shape"] == "square"
    assert widget.get_values()["footprint_mode"] == "fwhm"


def test_fitting_collapse_matches_results_splitter_header(qapp):
    widget = MonalisaParamsWidget()
    header = widget.fittingHeader
    toggle = widget.fittingToggle
    assert toggle is header.toggle
    assert widget.spotBox.layout().itemAtPosition(1, 0).widget() is header
    assert toggle.autoRaise() is True
    assert toggle.toolButtonStyle() == QtCore.Qt.ToolButtonTextOnly
    assert toggle.sizePolicy().verticalPolicy() == QtWidgets.QSizePolicy.Fixed
    assert header.layout().contentsMargins().top() == 0
    assert header.layout().contentsMargins().bottom() == 0
    assert header.height() == toggle.fontMetrics().height() + 2
    assert toggle.text().startswith("› Fitting · Local · Square · From lattice")
    # The actual Results splitter uses all-transparent button states, not the
    # native arrow-button and alternate-base hover used before this hotfix.
    css = toggle.styleSheet().replace(" ", "")
    assert "QToolButton:hover{border:none;background:transparent;}" in css
    assert "QToolButton:pressed{border:none;background:transparent;}" in css
    assert "QToolButton:checked{border:none;background:transparent;}" in css
    assert widget.fittingDetailsWidget.isHidden()
    toggle.click()
    assert not widget.fittingDetailsWidget.isHidden()
    assert toggle.text().startswith("⌄ Fitting")
    toggle.click()
    assert widget.fittingDetailsWidget.isHidden()
    assert toggle.text().startswith("› Fitting")


def test_collapsed_fitting_summary_tracks_saved_and_modified_defaults(qapp, tmp_path, monkeypatch):
    import json
    from imswitch.improcess.reconstructors.monalisa import params_widget as module
    from imswitch.improcess.reconstructors.monalisa.setup_defaults import read_detector_defaults

    path = tmp_path / "hardware.json"
    path.write_text(json.dumps({"detectors": {"CAM": {"managerProperties": {}}}}))
    monkeypatch.setattr(module, "current_setup_path", lambda: path)
    monkeypatch.setattr(module, "read_detector_defaults",
                        lambda detector, path=path: read_detector_defaults(detector, path=path))
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *args, **kwargs: QtWidgets.QMessageBox.Yes)
    widget = MonalisaParamsWidget()
    widget.load_from_attrs({"recording:detector_name": "CAM"})
    widget._set_combo_data(widget.estimatorCombo, "joint")
    widget._set_combo_data(widget.footprintShapeCombo, "circle")
    widget._set_combo_data(widget.footprintModeCombo, "fwhm")
    widget._save_footprint_default()
    assert widget.fittingHeader._status == "Setup"
    assert "Setup" not in widget.fittingToggle.text()
    saved = read_detector_defaults("CAM", path=path)
    assert (saved.estimator, saved.footprint_shape, saved.footprint_mode) == ("joint", "circle", "fwhm")
    assert saved.footprint_sigma_factor == pytest.approx(2.5)
    widget._set_combo_data(widget.footprintModeCombo, "fixed")
    assert widget.fittingHeader._status == "Modified"
    assert "Modified" not in widget.fittingToggle.text()
    widget.useSetupFittingAction.trigger()
    assert widget.fittingHeader._status == "Setup"
    assert "Circle · 2.5σ radius" in widget.fittingHeader._summary


def test_saved_circle_relative_2p5_uses_editable_field(qapp):
    from imswitch.improcess.reconstructors.monalisa.setup_defaults import DetectorSetupDefaults

    widget = MonalisaParamsWidget()
    widget._setup_defaults = DetectorSetupDefaults(
        detector="CAM", footprint_mode="fwhm", footprint_shape="circle",
        footprint_sigma_factor=2.5, estimator="joint",
    )
    widget._restore_fitting_default()
    assert widget.get_values()["footprint_mode"] == "fwhm"
    assert widget.get_values()["footprint_sigma_factor"] == pytest.approx(2.5)
    assert widget._combo_data(widget.footprintModeCombo) == "fwhm"
    assert widget.footprintValueStack.currentIndex() == 1
    assert "2.5σ radius" in widget.fittingHeader._summary


def test_localization_and_scanning_use_thin_disclosures_without_header_actions(qapp):
    widget = MonalisaParamsWidget()

    # Auto localization remains immediately usable on first opening. Scanning
    # is deliberately collapsed, while FWHM and fitting retain Patch F layout.
    assert widget.localizationToggle.isChecked() is True
    assert widget.localizationDetailsWidget.isHidden() is False
    assert widget.scanningToggle.isChecked() is False
    assert widget.scanningDetailsWidget.isHidden() is True
    assert "Auto · Pending" in widget.localizationHeader._summary
    assert "Auto" in widget.scanningHeader._summary
    assert widget.fittingToggle.isChecked() is False
    assert widget.estimateFwhmButton.isHidden() is False

    for header in (widget.localizationHeader, widget.scanningHeader, widget.fittingHeader):
        assert header.toggle.autoRaise() is True
        assert header.toggle.toolButtonStyle() == QtCore.Qt.ToolButtonTextOnly
        style = header.toggle.styleSheet()
        assert "QToolButton:hover" in style and "background: transparent" in style
        assert "palette(alternate-base)" not in style
        assert header.layout().count() == 2  # disclosure and hairline stretch; no actions
        assert header.height() == header.toggle.height()


def test_compact_sections_are_pure_presentation_and_keep_editable_controls(qapp):
    widget = MonalisaParamsWidget()
    original = widget.get_values()
    widget.localizationToggle.click()
    widget.scanningToggle.click()
    assert widget.localizationDetailsWidget.isHidden() is True
    assert widget.scanningDetailsWidget.isHidden() is False
    assert widget.get_values() == original

    widget.localizationToggle.click()
    widget._set_combo_data(widget.localizationModeCombo, "Manual")
    widget.periodXSpin.setValue(12.0)
    assert "Manual · 12 ×" in widget.localizationHeader._summary
    assert widget.get_values()["manual_period_x_px"] == 12.0

    widget._set_combo_data(widget.orientationModeCombo, "Manual")
    widget._set_combo_data(widget.orientationCombo, "-x+y")
    assert "-x+y · Manual" in widget.scanningHeader._summary
    widget.scanningToggle.click()
    assert widget.scanningDetailsWidget.isHidden() is True
    assert "-x+y · Manual" in widget.scanningHeader._summary
    assert widget.get_values()["orientation"] == "-x+y"


def test_collapsed_scanning_summary_tracks_recording_and_setup_source(qapp):
    widget = MonalisaParamsWidget()
    widget._recorded_orientation = "+y-x"
    widget._recorded_scan_axes = {
        "y": {"count": 30, "step_nm": 62.0},
        "z": {"count": 17, "step_nm": 150.0},
    }
    widget._set_combo_data(widget.orientationModeCombo, "Recorded")
    assert "+y-x · Recorded · YZ" in widget.scanningHeader._summary
    assert widget.scanningDetailsWidget.isHidden() is True
    widget._set_combo_data(widget.orientationModeCombo, "Auto")
    assert "Recorded" not in widget.scanningHeader._summary


def test_lock_localization_updates_header_and_collapses_details(qapp, tmp_path, monkeypatch):
    from imswitch.improcess.reconstructors.monalisa.calibration import PatternLocalization
    from imswitch.improcess.reconstructors.monalisa.session_registry import MonalisaSessionRegistry

    registry = MonalisaSessionRegistry(tmp_path)
    widget = MonalisaParamsWidget(session_registry=registry)
    widget.set_auto_localization(
        PatternLocalization.manual_grid(
            period_x=10.0, period_y=12.0, offset_x=1.0, offset_y=2.0,
            source="auto",
        ),
        "ready",
    )
    monkeypatch.setattr(
        QtWidgets.QInputDialog, "getText", lambda *args, **kwargs: ("Hex 750", True)
    )
    widget._lock_current_localization()
    assert "Locked · Hex 750" in widget.localizationHeader._summary
    assert widget.localizationDetailsWidget.isHidden() is True
    assert widget._combo_data(widget.localizationModeCombo) == "Locked"
    # Expansion is still a user-controlled presentation choice after locking.
    widget.localizationToggle.click()
    assert widget.localizationDetailsWidget.isHidden() is False
    assert widget.lockedLocalizationCombo.currentText() == "Hex 750"
