"""Focused GUI-contract tests for the compact unified MoNaLISA widget."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtCore, QtWidgets  # noqa: E402

from imswitch.improcess.reconstructors.monalisa.params import (  # noqa: E402
    PRESET_FAST,
    PRESET_FULL,
    default_params,
)
from imswitch.improcess.reconstructors.monalisa.params_widget import MonalisaParamsWidget  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_compact_widget_defaults_match_the_backend_contract(qapp):
    from imswitch.improcess.model.plugin_contract import widget_problems
    from imswitch.improcess.reconstructors.monalisa.reconstructor import MonalisaReconstructor

    widget = MonalisaParamsWidget()
    assert widget_problems(MonalisaReconstructor(), widget) == []

def test_setup_fwhm_is_available_before_first_dataset_and_contract_replayable(qapp, tmp_path, monkeypatch):
    import json
    from imswitch.improcess.reconstructors.monalisa import params_widget as module
    from imswitch.improcess.reconstructors.monalisa import setup_defaults
    from imswitch.improcess.reconstructors.monalisa.reconstructor import MonalisaReconstructor
    from imswitch.improcess.model.plugin_contract import widget_problems

    path = tmp_path / "hardware.json"
    path.write_text(json.dumps({
        "detectors": {"CAM": {"managerProperties": {"monalisaSpotFwhmNm": 238.0}}}
    }))
    monkeypatch.setattr(setup_defaults, "current_setup_path", lambda: path)
    monkeypatch.setattr(module, "current_setup_path", lambda: path)
    widget = MonalisaParamsWidget()
    assert widget._current_data_obj is None
    assert widget._detector_id == "CAM"
    assert widget.fwhmSpin.value() == pytest.approx(238.0)
    assert widget.get_values()["spot_width_source"] == "Setup default"
    assert widget.useSetupFwhmButton.isEnabled()
    assert widget.saveFwhmDefaultButton.isEnabled()
    assert widget_problems(MonalisaReconstructor(), widget) == []

    widget.fwhmSpin.setValue(245.0)
    widget.useSetupFwhmButton.trigger()
    assert widget.fwhmSpin.value() == pytest.approx(238.0)
    assert widget.get_values()["spot_width_source"] == "Setup default"

    # Saving is permitted before the first dataset only because CAM is
    # unambiguously identified by the active setup.
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *a, **kw: QtWidgets.QMessageBox.Yes)
    widget.fwhmSpin.setValue(251.0)
    widget.saveFwhmDefaultButton.trigger()
    assert setup_defaults.read_detector_defaults("CAM", path=path).fwhm_nm == pytest.approx(251.0)
    widget.load_from_attrs({"recording:detector_name": "CAM", "monalisa:spot_fwhm_nm": 280.0})
    assert widget.fwhmSpin.value() == pytest.approx(280.0)
    assert widget.get_values()["spot_width_source"] == "Recording"
    widget.useSetupFwhmButton.trigger()
    assert widget.fwhmSpin.value() == pytest.approx(251.0)


def test_startup_ambiguous_detectors_do_not_enable_fwhm_setup_actions(qapp, tmp_path, monkeypatch):
    import json
    from imswitch.improcess.reconstructors.monalisa import params_widget as module
    from imswitch.improcess.reconstructors.monalisa import setup_defaults

    path = tmp_path / "hardware.json"
    path.write_text(json.dumps({"detectors": {
        "A": {"managerProperties": {"monalisaSpotFwhmNm": 230.0}},
        "B": {"managerProperties": {"monalisaSpotFwhmNm": 250.0}},
    }}))
    monkeypatch.setattr(setup_defaults, "current_setup_path", lambda: path)
    monkeypatch.setattr(module, "current_setup_path", lambda: path)
    widget = MonalisaParamsWidget()
    assert widget._detector_id == "unknown"
    assert widget.fwhmSpin.value() == pytest.approx(default_params()["spot_fwhm"])
    assert not widget.useSetupFwhmButton.isEnabled()
    assert not widget.saveFwhmDefaultButton.isEnabled()


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


def test_full_defaults_to_auto_ism_without_rewriting_fwhm_or_fitting(qapp):
    widget = MonalisaParamsWidget()
    index = widget.modeCombo.findText("Full")
    widget.modeCombo.setCurrentIndex(index)
    values = widget.get_values()
    assert values["preset"] == PRESET_FULL
    assert values["spot_width_mode"] == "Manual/default"
    assert values["estimator"] == "local"
    assert values["reassignment"] == "auto"
    assert widget.ismBox.isHidden() is False



def test_three_profiles_appear_in_the_existing_mode_combo(qapp):
    widget = MonalisaParamsWidget()
    assert [widget.modeCombo.itemText(i) for i in range(widget.modeCombo.count())] == [
        "Normal", "Full", "DLL-equivalent",
    ]
    assert widget.legacyBox.isHidden()
    assert widget.fittingBox.isHidden() is False


def test_fast_fwhm_is_a_normal_edit_not_a_customization(qapp):
    widget = MonalisaParamsWidget()
    widget.fwhmSpin.setValue(widget.fwhmSpin.value() + 5.0)
    assert widget.get_values()["preset"] == PRESET_FAST


def test_full_processing_controls_do_not_change_mode(qapp):
    widget = MonalisaParamsWidget()
    widget.modeCombo.setCurrentIndex(widget.modeCombo.findText("Full"))
    widget.reassignmentCombo.setCurrentIndex(widget.reassignmentCombo.findText("Manual"))
    widget.shiftFactorSpin.setValue(.42)
    widget.sharpenCheck.setChecked(True)
    widget.pinholeCheck.setChecked(True)
    result = widget.get_values()
    assert result["preset"] == PRESET_FULL
    assert result["reassignment"] == "fixed"
    assert result["shift_factor"] == .42
    assert result["sharpen"] is True
    assert result["pinhole_stack"] is True


def test_full_settings_survive_fast_and_full_roundtrip(qapp):
    widget = MonalisaParamsWidget()
    widget._set_combo_data(widget.estimatorCombo, "joint")
    widget.fwhmSpin.setValue(207)
    widget.intensityCheck.setChecked(True)
    widget.modeCombo.setCurrentIndex(widget.modeCombo.findText("Full"))
    widget._set_combo_data(widget.reassignmentCombo, "fixed")
    widget.shiftFactorSpin.setValue(.47)
    widget.sharpenCheck.setChecked(True)
    full = widget.get_values()
    widget.modeCombo.setCurrentIndex(widget.modeCombo.findText("Normal"))
    fast = widget.get_values()
    assert fast["reassignment"] == "off"
    assert fast["sharpen"] is False
    assert fast["full_options"]["reassignment"] == "fixed"
    for key in ("estimator", "spot_fwhm", "intensity_correction"):
        assert fast[key] == full[key]
    widget.modeCombo.setCurrentIndex(widget.modeCombo.findText("Full"))
    back = widget.get_values()
    assert back["reassignment"] == "fixed"
    assert back["shift_factor"] == .47
    assert back["sharpen"] is True
    assert back["estimator"] == "joint"
    assert back["spot_fwhm"] == 207


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
    assert widget.get_values()["preset"] == PRESET_FAST

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

def test_editing_joint_support_keeps_full_mode(qapp):
    widget = MonalisaParamsWidget()
    widget.modeCombo.setCurrentIndex(widget.modeCombo.findText("Full"))
    assert widget.get_values()["preset"] == PRESET_FULL
    widget._set_combo_data(widget.footprintShapeCombo, "lattice")
    values = widget.get_values()
    assert values["preset"] == PRESET_FULL
    assert values["footprint_shape"] == "lattice"

def test_spot_width_and_fitting_are_independent_top_level_sections(qapp):
    widget = MonalisaParamsWidget()
    layout = widget.spotBox.grid
    assert widget.spotBox.details.title().startswith("⌄ Spot width")
    assert layout.itemAtPosition(0, 0).widget() is widget.fwhmLabel
    assert layout.itemAtPosition(0, 4).widget() is widget.estimateFwhmButton
    assert layout.itemAtPosition(0, 5).widget() is widget.fwhmMenuButton
    assert layout.itemAtPosition(1, 0) is None  # no nested fitting header
    assert widget.fittingBox.parent() is widget._scrollContent
    assert widget.fittingDetailsWidget is widget.fittingBox.details
    assert widget.fittingMenuButton.menu() is widget.fittingMenu
    assert widget.fwhmMenuButton.menu() is widget.fwhmMenu
    # The FWHM is detector-specific and can differ from the headless default.
    from imswitch.improcess.model.plugin_contract import widget_problems
    from imswitch.improcess.reconstructors.monalisa.reconstructor import MonalisaReconstructor
    assert widget_problems(MonalisaReconstructor(), widget) == []


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

def test_full_preserves_gaussian_fitting_and_spot_width(qapp):
    widget = MonalisaParamsWidget()
    widget.modeCombo.setCurrentIndex(widget.modeCombo.findText("Full"))
    assert widget.fwhmSpin.isHidden() is False
    assert widget.fittingBox.isHidden() is False
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
    spot = widget.spotBox.grid
    fitting = widget.fittingDetailsWidget.layout()
    assert widget.spotModeCombo.findText("Manual") >= 0  # real mode selector stays
    assert widget.fwhmSourceLabel.isHidden()
    assert widget.footprintSourceLabel.isHidden()
    assert spot.itemAtPosition(1, 0) is None
    assert widget.fittingBox.header is widget.fittingHeader
    assert widget.fittingBox.details is widget.fittingDetailsWidget
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
    assert widget.fittingBox.header is header
    assert toggle.autoRaise() is True
    assert toggle.toolButtonStyle() == QtCore.Qt.ToolButtonTextOnly
    assert toggle.sizePolicy().verticalPolicy() == QtWidgets.QSizePolicy.Fixed
    assert header.layout().contentsMargins().top() == 0
    assert header.layout().contentsMargins().bottom() == 0
    assert header.height() == toggle.fontMetrics().height() + 2
    assert toggle.text().startswith("› Fitting")
    assert "Local · Square · From lattice" in header._summary_label.text()
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
    # is deliberately collapsed, and FWHM and fitting use separate sections.
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
        assert header.layout().count() == 3
        # No header action buttons; Localization/Scanning have separate muted summaries.
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


def test_compact_sections_use_native_groupbox_frames_only_when_expanded(qapp):
    """Localization/Scanning must paint exactly like Spot model and Corrections."""
    widget = MonalisaParamsWidget()
    assert isinstance(widget.localizationDetailsWidget, QtWidgets.QGroupBox)
    assert isinstance(widget.scanningDetailsWidget, QtWidgets.QGroupBox)
    for header, details in (
        (widget.localizationHeader, widget.localizationDetailsWidget),
        (widget.scanningHeader, widget.scanningDetailsWidget),
    ):
        assert header._show_rule is False
        assert header._summary_label is not None
        assert header.toggle.font().bold() is True
        assert header._summary_label.font().bold() is False
        assert details.styleSheet() == widget.spotBox.details.styleSheet() == ""
        assert details.layout().contentsMargins() == widget.spotBox.details.layout().contentsMargins()
        assert "border" not in details.styleSheet()
        assert details.title().startswith("⌄ ")
        assert details._summary == header._summary
        assert details.toolTip().startswith("Click title to collapse.")

    assert widget.localizationHeader.isHidden() is True
    assert widget.localizationDetailsWidget.isHidden() is False
    assert widget.scanningHeader.isHidden() is False
    assert widget.scanningDetailsWidget.isHidden() is True
    # Both views are alternatives, never two stacked headers and borders.
    widget.scanningToggle.click()
    assert widget.scanningHeader.isHidden() is True
    assert widget.scanningDetailsWidget.isHidden() is False
    widget.scanningDetailsWidget.collapseRequested.emit()
    assert widget.scanningHeader.isHidden() is False
    assert widget.scanningDetailsWidget.isHidden() is True


def test_native_section_titles_follow_current_summary_and_collapse(qapp):
    widget = MonalisaParamsWidget()
    parameters = widget.get_values()
    widget._set_combo_data(widget.localizationModeCombo, "Manual")
    assert "Manual" in widget.localizationDetailsWidget.title()
    widget.localizationDetailsWidget.collapseRequested.emit()
    assert widget.localizationToggle.isChecked() is False
    assert widget.localizationDetailsWidget.isHidden() is True
    assert widget.localizationHeader.isHidden() is False
    widget.localizationToggle.click()
    assert widget.localizationDetailsWidget.isHidden() is False
    assert widget.localizationHeader.isHidden() is True
    # Expanding/collapsing does not itself alter scientific parameters.
    assert widget.get_values()["estimator"] == parameters["estimator"]
    assert widget.get_values()["footprint_mode"] == parameters["footprint_mode"]


def test_clicking_native_groupbox_title_collapses_section(qapp):
    """The expanded frame itself must provide a discoverable collapse action."""
    from qtpy import QtTest

    widget = MonalisaParamsWidget()
    widget.show()
    qapp.processEvents()
    details = widget.localizationDetailsWidget
    option = QtWidgets.QStyleOptionGroupBox()
    details.initStyleOption(option)
    title_rect = details.style().subControlRect(
        QtWidgets.QStyle.CC_GroupBox,
        option,
        QtWidgets.QStyle.SC_GroupBoxLabel,
        details,
    )
    assert title_rect.isValid() and not title_rect.isEmpty()
    QtTest.QTest.mouseClick(details, QtCore.Qt.LeftButton, pos=title_rect.center())
    qapp.processEvents()
    assert widget.localizationToggle.isChecked() is False
    assert details.isHidden() is True
    widget.close()


def test_all_monalisa_sections_share_native_expanded_frame_and_thin_collapsed_header(qapp):
    from imswitch.improcess.reconstructors.monalisa.collapsible_section import CompactSection

    widget = MonalisaParamsWidget()
    sections = (
        widget.localizationBox, widget.scanningBox, widget.spotBox,
        widget.fittingBox, widget.legacyBox, widget.ismBox,
        widget.correctionsBox, widget.outputBox,
    )
    assert all(isinstance(section, CompactSection) for section in sections)
    for section in sections:
        assert section.header._show_rule is False
        assert section.header.toggle.font().bold() is True
        assert section.header.toggle.autoRaise() is True
        assert section.details.styleSheet() == ""
        assert section.details.layout().contentsMargins() == widget.spotBox.details.layout().contentsMargins()
        assert section.header.isHidden() == section.isExpanded()
        assert section.details.isHidden() != section.isExpanded()
        assert "background: transparent" in section.toggle.styleSheet()

    assert widget.spotBox.isExpanded() is True  # Estimate is available immediately
    assert widget.fittingBox.isExpanded() is False
    assert widget.scanningBox.isExpanded() is False
    assert widget.correctionsBox.isExpanded() is True  # keep familiar checkbox access


def test_compact_sections_retain_scientific_parameters_and_live_summaries(qapp):
    widget = MonalisaParamsWidget()
    original = widget.get_values()
    for section in (widget.spotBox, widget.fittingBox, widget.correctionsBox, widget.outputBox):
        section.setExpanded(not section.isExpanded())
        section.setExpanded(not section.isExpanded())
    assert widget.get_values() == original

    widget.fwhmSpin.setValue(230.0)
    assert "230 nm" in widget.spotBox.header._summary
    widget.intensityCheck.setChecked(True)
    assert "Intensity" in widget.correctionsBox.header._summary
    widget.sharpenCheck.setChecked(True)
    assert "Sharpening On" in widget.outputBox.header._summary
    widget._set_combo_data(widget.reassignmentCombo, "fixed")
    widget.shiftFactorSpin.setValue(0.4)
    assert "Manual · 0.4" in widget.ismBox.header._summary
    widget._set_combo_data(widget.legacyBackgroundCombo, "Gaussian")
    assert "Gaussian background" in widget.legacyBox.header._summary


def test_mode_visibility_and_expansion_are_independent(qapp):
    widget = MonalisaParamsWidget()
    widget.fittingBox.setExpanded(True)
    widget.correctionsBox.setExpanded(False)
    widget.modeCombo.setCurrentIndex(widget.modeCombo.findText("Full"))
    assert widget.fittingBox.isHidden() is False
    assert widget.ismBox.isHidden() is False
    widget.modeCombo.setCurrentIndex(widget.modeCombo.findText("Normal"))
    assert widget.fittingBox.isHidden() is False
    assert widget.fittingBox.isExpanded() is True
    assert widget.correctionsBox.isExpanded() is False
    assert widget.ismBox.isHidden() is True
    assert widget.legacyBox.isHidden() is True


def test_native_title_click_collapses_new_fitting_section(qapp):
    from qtpy import QtTest
    widget = MonalisaParamsWidget()
    widget.fittingBox.setExpanded(True)
    widget.show()
    qapp.processEvents()
    group = widget.fittingBox.details
    option = QtWidgets.QStyleOptionGroupBox()
    group.initStyleOption(option)
    rect = group.style().subControlRect(QtWidgets.QStyle.CC_GroupBox, option,
                                        QtWidgets.QStyle.SC_GroupBoxLabel, group)
    assert rect.isValid() and not rect.isEmpty()
    QtTest.QTest.mouseClick(group, QtCore.Qt.LeftButton, pos=rect.center())
    qapp.processEvents()
    assert widget.fittingBox.isExpanded() is False
    assert widget.fittingBox.header.isHidden() is False
    widget.close()


def test_ism_estimate_is_in_ism_section_and_requires_selected_data(qapp):
    widget = MonalisaParamsWidget()
    assert widget.estimateISMButton.text() == "Estimate"
    assert not widget.estimateISMButton.isEnabled()
    widget._current_data_obj = object()
    widget._update_ui()
    assert widget.estimateISMButton.isEnabled()


def test_ism_estimate_acceptance_sets_fixed_without_reconstruction(qapp):
    from imswitch.improcess.reconstructors.monalisa.ism_calibration import ISMShiftEstimate
    from imswitch.improcess.reconstructors.monalisa.reassignment import ShiftFactor
    import numpy as np

    widget = MonalisaParamsWidget()
    widget.modeCombo.setCurrentIndex(widget.modeCombo.findText("Full"))
    data_obj = object()
    widget._current_data_obj = data_obj
    widget._ism_estimate_serial = 1
    widget._ism_estimate_revision = widget._fwhm_data_revision
    widget._ism_estimate_signature = widget._ism_calibration_signature(widget._ism_estimate_params())
    factor = ShiftFactor(
        alpha=.4, alpha_x=.35, alpha_y=.45,
        per_offset=np.array([[1., 0., .4, .9]]), spread=.02,
    )
    measurement = ISMShiftEstimate(factor, "+x+y", ("x", "y"), 0, 1)
    widget._finish_ism_estimate((1, data_obj, measurement, "Raw-frame ISM", None, ""))
    assert widget.reassignmentCombo.currentData() == "fixed"
    assert widget.shiftFactorSpin.value() == .4
    assert widget.shiftXSpin.value() == .35
    assert widget.shiftYSpin.value() == .45
    assert "ISM shift" in widget.previewStatusLabel.text()


def test_ism_estimate_rejects_old_dataset_result(qapp):
    from imswitch.improcess.reconstructors.monalisa.ism_calibration import ISMShiftEstimate
    from imswitch.improcess.reconstructors.monalisa.reassignment import ShiftFactor
    import numpy as np

    widget = MonalisaParamsWidget()
    previous_data = object()
    widget._current_data_obj = object()
    widget._ism_estimate_serial = 2
    widget._ism_estimate_revision = widget._fwhm_data_revision
    widget._ism_estimate_signature = widget._ism_calibration_signature(widget._ism_estimate_params())
    factor = ShiftFactor(.3, .3, .3, np.array([[1., 0., .3, .9]]), .02)
    measurement = ISMShiftEstimate(factor, "+x+y", ("x", "y"), 0, 1)
    before = widget.shiftFactorSpin.value()
    widget._finish_ism_estimate((2, previous_data, measurement, "", None, ""))
    assert widget.shiftFactorSpin.value() == before
    assert "Discarded ISM estimate" in widget.previewStatusLabel.text()


def test_dll_mode_shares_calibrations_and_restores_gaussian_settings(qapp):
    widget = MonalisaParamsWidget()
    widget.modeCombo.setCurrentIndex(widget.modeCombo.findText("Full"))
    widget._set_combo_data(widget.reassignmentCombo, "fixed")
    widget.shiftFactorSpin.setValue(.39)
    widget.sharpenCheck.setChecked(True)
    widget._advanced_values["estimator"] = "joint"
    widget.fwhmSpin.setValue(240.)
    widget.intensityCheck.setChecked(True)
    widget.modeCombo.setCurrentIndex(widget.modeCombo.findText("DLL-equivalent"))
    values = widget.get_values()
    assert values["gaussian_mode"] == PRESET_FULL
    assert values["gaussian_estimator"] == "joint"
    assert values["estimator"] == "dll_python"
    assert values["spot_fwhm"] == 240.
    assert values["intensity_correction"] is True
    assert values["reassignment"] == "off"
    assert values["sharpen"] is False
    assert values["variance_weighting"] is False
    assert widget.ismBox.isHidden() and widget.outputBox.isHidden()
    assert widget.fittingBox.isHidden()
    widget.modeCombo.setCurrentIndex(widget.modeCombo.findText("Full"))
    restored = widget.get_values()
    assert restored["estimator"] == "joint"
    assert restored["reassignment"] == "fixed"
    assert restored["shift_factor"] == pytest.approx(.39)
    assert restored["sharpen"] is True
    assert restored["spot_fwhm"] == 240.


def test_dll_mode_expert_disables_only_irrelevant_numerics(qapp):
    from imswitch.improcess.reconstructors.monalisa.advanced_settings import AdvancedReconstructionDialog

    widget = MonalisaParamsWidget()
    dialog = AdvancedReconstructionDialog(widget._advanced_values, dll_equivalent=True)
    assert not dialog.background.isEnabled()
    assert not dialog.sampling.isEnabled()
    assert not dialog.variance.isEnabled()
    assert not dialog.sharpenSigma.isEnabled()
    assert dialog.pixelNm.isEnabled()
    assert dialog.placement.isEnabled()
    dialog.close()

    normal_dialog = AdvancedReconstructionDialog(widget._advanced_values)
    assert normal_dialog.background.isEnabled()
    assert normal_dialog.variance.isEnabled()
    normal_dialog.close()
