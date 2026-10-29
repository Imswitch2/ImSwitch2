"""Focused GUI-contract tests for the compact unified MoNaLISA widget."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtCore, QtWidgets  # noqa: E402

from imswitch.improcess.reconstructors.monalisa.params import (  # noqa: E402
    PRESET_CUSTOM,
    PRESET_FAST,
    PRESET_FULL,
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


def test_fast_mode_hides_optional_processing_sections(qapp):
    widget = MonalisaParamsWidget()
    assert widget.get_values()["preset"] == PRESET_FAST
    assert widget.localizationBox.isHidden() is False
    assert widget.scanningBox.isHidden() is False
    assert widget.spotBox.isHidden() is False
    assert widget.ismBox.isHidden() is True
    assert widget.correctionsBox.isHidden() is True
    assert widget.outputBox.isHidden() is True
    assert widget.spotModeCombo.isHidden() is True


def test_robust_is_the_ui_name_for_the_existing_full_contract_value(qapp):
    widget = MonalisaParamsWidget()
    index = widget.modeCombo.findText("Robust")
    widget.modeCombo.setCurrentIndex(index)
    values = widget.get_values()
    assert values["preset"] == PRESET_FULL
    assert values["spot_width_mode"] == "Auto"
    assert values["reassignment"] == "auto"
    assert widget.ismBox.isHidden() is False


def test_fast_fwhm_is_a_normal_edit_not_a_customization(qapp):
    widget = MonalisaParamsWidget()
    widget.fwhmSpin.setValue(widget.fwhmSpin.value() + 5.0)
    assert widget.get_values()["preset"] == PRESET_FAST


def test_changing_a_mode_controlled_option_enters_custom(qapp):
    widget = MonalisaParamsWidget()
    widget.modeCombo.setCurrentIndex(widget.modeCombo.findText("Robust"))
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

    assert widget.orientationResultLabel.text() == "→ -x-y"
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
    assert widget.orientationResultLabel.text() == "→ -x-y"
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
