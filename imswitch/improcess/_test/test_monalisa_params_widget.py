"""Focused GUI-contract tests for the compact unified MoNaLISA widget."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtWidgets  # noqa: E402

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
