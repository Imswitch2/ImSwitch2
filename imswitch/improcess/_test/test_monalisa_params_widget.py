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


def test_expert_dialog_can_suggest_footprint_from_manual_lattice(qapp):
    from imswitch.improcess.reconstructors.monalisa.advanced_settings import AdvancedReconstructionDialog
    from imswitch.improcess.reconstructors.monalisa.calibration import PatternLocalization

    widget = MonalisaParamsWidget()
    localization = PatternLocalization.manual_grid(11.05, 11.05, 2, 3)
    dialog = AdvancedReconstructionDialog(widget._advanced_values, localization=localization)
    assert dialog.estimator.currentText() == "local"
    dialog.footprintSize.setValue(5)
    dialog.suggestButton.click()
    assert dialog.values()["footprint_size_px"] == 11
    dialog.footprintShape.setCurrentIndex(dialog.footprintShape.findData("circle"))
    assert dialog.values()["footprint_shape"] == "circle"


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


def test_expert_footprint_setup_default_is_reusable(qapp, tmp_path, monkeypatch):
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
    widget._advanced_values.update({
        "footprint_mode": "from_lattice",
        "footprint_shape": "circle",
        "footprint_size_px": 7.0,
    })
    widget._save_footprint_default()
    saved = read_detector_defaults("CAM", path=path)
    assert (saved.footprint_mode, saved.footprint_shape, saved.footprint_size) == (
        "from_lattice", "circle", 7,
    )
    new_widget = MonalisaParamsWidget()
    new_widget.load_from_attrs({"recording:detector_name": "CAM"})
    values = new_widget.get_values()
    assert values["footprint_mode"] == "from_lattice"
    assert values["footprint_shape"] == "circle"
    assert "Circle · From lattice" in new_widget.footprintSummaryLabel.text()


def test_expert_joint_support_is_editable_without_new_widget(qapp):
    from imswitch.improcess.reconstructors.monalisa.advanced_settings import AdvancedReconstructionDialog

    widget = MonalisaParamsWidget()
    dialog = AdvancedReconstructionDialog(widget._advanced_values)
    assert dialog.footprintMode.currentData() == "from_lattice"
    assert dialog.footprintShape.currentData() == "square"
    dialog.estimator.setCurrentText("joint")
    assert dialog.footprintShape.isEnabled() is True
    dialog.footprintMode.setCurrentIndex(dialog.footprintMode.findData("fixed"))
    assert dialog.footprintSize.isEnabled() is True
    dialog.footprintShape.setCurrentIndex(dialog.footprintShape.findData("circle"))
    dialog.footprintSize.setValue(10.5)
    assert dialog.values()["footprint_mode"] == "fixed"
    assert dialog.values()["footprint_size_px"] == 10.5
    dialog.estimator.setCurrentText("local")
    assert dialog.values()["footprint_size_px"] == 10.5  # one shared choice
    dialog.footprintShape.setCurrentIndex(dialog.footprintShape.findData("lattice"))
    assert dialog.footprintMode.isEnabled() is False
    assert dialog.sampling.isEnabled() is False


def test_editing_joint_support_marks_full_as_custom(qapp, monkeypatch):
    from imswitch.improcess.reconstructors.monalisa.advanced_settings import AdvancedReconstructionDialog

    widget = MonalisaParamsWidget()
    widget.modeCombo.setCurrentIndex(widget.modeCombo.findText("Full (Auto + ISM)"))
    assert widget.get_values()["preset"] == PRESET_FULL

    def choose_lattice(dialog):
        dialog.estimator.setCurrentText("joint")
        dialog.footprintShape.setCurrentIndex(dialog.footprintShape.findData("lattice"))
        return QtWidgets.QDialog.Accepted

    monkeypatch.setattr(AdvancedReconstructionDialog, "exec_", choose_lattice)
    widget._open_advanced_reconstruction()
    values = widget.get_values()
    assert values["preset"] == PRESET_CUSTOM
    assert values["footprint_shape"] == "lattice"
