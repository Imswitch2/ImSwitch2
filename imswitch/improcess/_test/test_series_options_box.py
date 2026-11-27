"""The Timepoints box of the reconstruction panel."""

from types import SimpleNamespace

from imswitch.improcess._test._lapse_recordings import record_lapse
from imswitch.improcess.model import series as sm
from imswitch.improcess.view.SeriesOptionsBox import SeriesOptionsBox


def _info(tmp_path, **kwargs):
    paths = record_lapse(tmp_path / "lapse", 4)
    return sm.discover_series(str(paths[1]), **kwargs)


def test_the_box_is_hidden_and_adds_nothing_for_a_source_outside_a_series(qtbot):
    box = SeriesOptionsBox()
    qtbot.addWidget(box)
    assert box.isHidden() and box.get_values() == {}
    box.set_series(None)
    assert box.isHidden() and box.get_values() == {}


def test_the_box_offers_this_all_or_a_range_with_the_reconstructors_default(qtbot, tmp_path):
    info = _info(tmp_path)
    box = SeriesOptionsBox()
    qtbot.addWidget(box)
    box.set_series(info, "this")
    assert not box.isHidden() and "4 timepoint(s)" in box.summary.text()
    assert box.get_values()["_series"] == "this" and not box.rangeWidget.isEnabled()
    box.set_series(info, "all")
    assert box.get_values()["_series"] == "all"
    box.modeCombo.setCurrentIndex(box.modeCombo.findData("range"))
    box.firstSpin.setValue(1)
    box.lastSpin.setValue(3)
    box.stepSpin.setValue(2)
    box.missingCombo.setCurrentIndex(box.missingCombo.findData("nan"))
    values = box.get_values()
    assert values == {"_series": "range", "_series_first": 1, "_series_last": 3, "_series_step": 2,
                      "_series_missing": "nan", "_series_shapes": "refuse"}
    assert sm.SeriesRequest.from_params(values).mode == "range"
    box.set_series(None)
    assert box.get_values() == {}


def test_opening_the_series_itself_selects_all_whatever_the_default(qtbot, tmp_path):
    info = _info(tmp_path)
    box = SeriesOptionsBox()
    qtbot.addWidget(box)
    box.set_series(info, "this", opened_as_series=True)
    assert box.get_values()["_series"] == "all"


def test_the_panel_adds_the_boxs_values_to_the_plugins_own(qtbot):
    from imswitch.improcess.view.ImProcessMainView import ImProcessMainView

    box = SeriesOptionsBox()
    qtbot.addWidget(box)
    view = SimpleNamespace(parTree=SimpleNamespace(get_values=lambda: {"refine": True}), seriesBox=box)
    assert ImProcessMainView.getReconstructionParams(view) == {"refine": True}
    info = SimpleNamespace(members=(1, 2), opened_as_series=False, describe=lambda: "x")
    box.set_series(info, "all")
    merged = ImProcessMainView.getReconstructionParams(view)
    assert merged["refine"] is True and merged["_series"] == "all"
