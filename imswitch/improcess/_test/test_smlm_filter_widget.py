"""The SMLM filter panel: a property, its histogram, a band on it."""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtWidgets  # noqa: E402

from imswitch.improcess.model import LocalizationResult, localizations_from_columns  # noqa: E402
from imswitch.improcess.model.localization_schema import LOCALIZATION_COLUMNS  # noqa: E402
from imswitch.improcess.processors.smlm_filter import SmlmFilterProcessor  # noqa: E402
from imswitch.improcess.view.ResultProcessorWidget import ResultProcessorWidget  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _result(n=6):
    locs = localizations_from_columns({
        "frame": np.arange(n),
        "x_nm": np.linspace(0.0, 500.0, n),
        "y_nm": np.linspace(0.0, 300.0, n),
        "photons": 100.0 * np.arange(1, n + 1),
        "sigma_x_nm": np.full(n, 120.0),
        "sigma_y_nm": np.full(n, 130.0),
    })
    return LocalizationResult("locs", locs, pixel_size_nm=100.0)


def _columns(widget):
    return [widget.propertyCombo.itemData(i) for i in range(widget.propertyCombo.count())]


def test_without_a_table_every_column_is_offered_and_the_band_is_open(qapp):
    widget = SmlmFilterProcessor().make_param_widget(None)
    assert _columns(widget) == list(LOCALIZATION_COLUMNS)
    assert not widget.lowerSpin.isEnabled() and not widget.upperSpin.isEnabled()
    assert widget.get_values() == SmlmFilterProcessor.default_params()


def test_a_table_narrows_the_columns_and_sets_the_band_to_its_range(qapp):
    widget = SmlmFilterProcessor().make_param_widget(None)
    widget.setResult(_result())

    # z, sigma_z and the precisions are all zero here: not offered.
    assert _columns(widget) == ["frame", "x_nm", "y_nm", "sigma_x_nm", "sigma_y_nm", "photons"]
    assert widget.property() == "photons"
    assert widget.get_values() == {
        "property": "photons", "mode": "bandpass", "lower": 100.0, "upper": 600.0,
    }
    assert widget.lowerSpin.isEnabled()
    assert widget.region.isVisible()
    assert widget.countLabel.text() == "Keeps 6 of 6 localizations."


def test_the_spin_boxes_and_the_region_are_two_handles_on_one_band(qapp):
    widget = SmlmFilterProcessor().make_param_widget(None)
    widget.setResult(_result())

    widget.lowerSpin.setValue(250.0)
    assert widget.region.getRegion() == (250.0, 600.0)
    assert widget.countLabel.text() == "Keeps 4 of 6 localizations."

    widget.region.setRegion((150.0, 450.0))
    assert widget.get_values()["lower"] == 150.0
    assert widget.get_values()["upper"] == 450.0
    assert widget.countLabel.text() == "Keeps 3 of 6 localizations."


def test_bandstop_counts_what_the_band_drops(qapp):
    widget = SmlmFilterProcessor().make_param_widget(None)
    widget.setResult(_result())
    widget.region.setRegion((150.0, 450.0))

    widget.modeCombo.setCurrentIndex(1)

    assert widget.mode() == "bandstop"
    assert widget.countLabel.text() == "Keeps 3 of 6 localizations."
    filtered = SmlmFilterProcessor().apply(_result(), widget.get_values())
    assert list(filtered.locs.photons) == [100.0, 500.0, 600.0]


def test_changing_the_property_redraws_and_resets_the_band(qapp):
    widget = SmlmFilterProcessor().make_param_widget(None)
    widget.setResult(_result())
    widget.lowerSpin.setValue(250.0)

    widget.propertyCombo.setCurrentIndex(_columns(widget).index("frame"))

    values = widget.get_values()
    assert values["property"] == "frame"
    assert (values["lower"], values["upper"]) == (0.0, 5.0)
    assert widget.lowerSpin.decimals() == 0


def test_the_chosen_property_survives_a_table_switch_when_it_still_exists(qapp):
    widget = SmlmFilterProcessor().make_param_widget(None)
    widget.setResult(_result())
    widget.propertyCombo.setCurrentIndex(_columns(widget).index("sigma_x_nm"))

    widget.setResult(_result(n=3))

    assert widget.property() == "sigma_x_nm"
    assert widget.get_values()["lower"] == 120.0


def test_set_values_takes_what_it_is_given(qapp):
    widget = SmlmFilterProcessor().make_param_widget(None)
    widget.setResult(_result())

    widget.set_values({"property": "frame", "mode": "bandstop", "lower": 2, "upper": None})

    assert widget.get_values() == {
        "property": "frame", "mode": "bandstop", "lower": 2.0, "upper": None,
    }
    assert widget.countLabel.text() == "Keeps 2 of 6 localizations."

    widget.set_values({"lower": None, "upper": None})
    assert widget.get_values()["lower"] is None
    assert not widget.lowerSpin.isEnabled()


def test_a_result_that_is_not_a_table_clears_the_panel(qapp):
    from imswitch.improcess.model import ArrayProcessingResult

    widget = SmlmFilterProcessor().make_param_widget(None)
    widget.setResult(_result())
    widget.setResult(ArrayProcessingResult("img", np.zeros((4, 4), np.float32), ["Y", "X"]))

    assert not widget.region.isVisible()
    assert widget.get_values()["lower"] is None
    assert _columns(widget) == list(LOCALIZATION_COLUMNS)


def test_the_panel_button_says_apply_filter_and_follows_the_current_result(qapp):
    panel = ResultProcessorWidget(SmlmFilterProcessor())
    assert panel.runButton.text() == "Apply filter"

    panel.setCurrentResult(_result())

    assert panel.runButton.isEnabled()
    assert panel.paramWidget.property() == "photons"
    assert panel.parameterValues()["upper"] == 600.0


def _big_result(n=50_000, seed=0):
    rng = np.random.default_rng(seed)
    photons = rng.gamma(4.0, 600.0, n)
    locs = localizations_from_columns({
        "frame": rng.integers(0, 40, n),
        "x_nm": rng.uniform(0, 5000, n), "y_nm": rng.uniform(0, 5000, n),
        "photons": photons,
    })
    return LocalizationResult("big", locs, pixel_size_nm=100.0)


def test_the_histogram_has_as_many_bins_as_the_data_and_the_plot_allow(qapp):
    widget = SmlmFilterProcessor().make_param_widget(None)
    widget.resize(900, 400)
    widget.plot.setFixedWidth(600)
    widget.setResult(_big_result())
    xs, counts = widget._bars.getData()
    # Photons are continuous: bins follow the Freedman-Diaconis width, not a
    # fixed count, and there are never more than the plot has pixels.
    bins = len(counts)
    assert 20 < bins <= 600
    assert bins != 100

    # A whole-number column of 40 distinct values gets one bin per value.
    widget.propertyCombo.setCurrentIndex(_columns(widget).index("frame"))
    assert len(widget._bars.getData()[1]) == 40

    # A narrower plot has room for fewer bars.
    widget.propertyCombo.setCurrentIndex(_columns(widget).index("photons"))
    widget.plot.setFixedWidth(60)
    widget._redrawForWidth()
    assert len(widget._bars.getData()[1]) <= 60


def test_an_open_band_stop_says_why_nothing_runs(qapp):
    widget = SmlmFilterProcessor().make_param_widget(None)
    widget.setResult(_result())
    widget.set_values({"mode": "bandstop", "lower": None, "upper": None})
    with pytest.raises(ValueError, match="band-stop needs at least one threshold"):
        SmlmFilterProcessor().apply(_result(), widget.get_values())
