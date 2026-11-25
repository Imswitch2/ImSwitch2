"""Per-input tooling on a card with several detector inputs: the Signals
panel has a row per declared input, Find and Keep work on a second detector
input (kept in its entry of the ``inputs`` map), the level checks know a
detector input by its kind, the pre-flight lists every detector input by
name, and the Lifetime widget offers FLIM detectors only."""

from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.controller.controllers.LifetimeController import LifetimeController
from imswitch.imcontrol.model.SetupInfo import TimeTaggerInfo, TimeTaggerInputInfo
from imswitch.imcontrol.model.interfaces.timetagger_mock import MockTimeTaggerApi, SignalModel
from imswitch.imcontrol.model.managers.TimeTaggerManager import TimeTaggerManager
from imswitch.imcontrol.model.workflows.time_tagger_facade import (
    TimeTaggerFacade,
    level_warnings,
    signal_kind,
)
from imswitch.imcontrol.view.widgets.LifetimeWidget import LifetimeWidget
from imswitch.imcontrol.view.widgets.basewidgets import WidgetFactory
from imswitch.imcontrol._test.unit.test_lifetime_controller import _Comm, _Detectors, _FakeDetector

pytestmark = pytest.mark.nohardware

_REAL_FIND = TimeTaggerFacade.find_trigger_level


def _fast_find(self, input, duration_s=0.1, **kwargs):
    return _REAL_FIND(self, input, 0.002, **kwargs)


def _info():
    return TimeTaggerInfo(simulation=True, inputs={
        "apd_green": TimeTaggerInputInfo(channel=-1, triggerV=-0.25),
        "apd_red": TimeTaggerInputInfo(channel=-4, triggerV=-0.25),
        "sync": TimeTaggerInputInfo(channel=2, kind="laser_sync"),
        "line_clock": TimeTaggerInputInfo(channel=3, kind="line_clock"),
    })


def _card(info=None):
    info = info or _info()
    return TimeTaggerManager(info, api=MockTimeTaggerApi(model=SignalModel.from_info(info)))


@pytest.fixture
def card():
    made = _card()
    yield made
    made.finalize()


def test_the_facade_lists_every_input_and_judges_each_by_its_kind(card):
    tt = TimeTaggerFacade(card)
    assert tt.inputs() == ["apd_green", "apd_red", "sync", "line_clock"]
    assert tt.roles() == ["laser_sync", "line_clock"]
    assert tt.detector_inputs() == ["apd_green", "apd_red"]
    channels = tt.channels()
    assert channels["apd_red"].kind == "detector" and channels["apd_red"].plays is None
    assert channels["sync"].plays == "laser_sync"
    assert signal_kind("apd_red", card)["kind"] == "detector pulses"
    assert signal_kind("line_clock", card)["scan_only"] is True
    rates = tt.count_rates(duration_s=0.01).rates_hz
    assert set(rates) == set(tt.inputs()) and rates["apd_red"] > 0
    assert level_warnings(card, "apd_red", 0.3)[0].startswith("the level is positive")
    green, red = tt.mock_truth(8, 8, "apd_green"), tt.mock_truth(8, 8, "apd_red")
    assert not np.allclose(green[0], red[0])


def test_find_on_a_second_detector_input_points_into_the_inputs_map(card):
    tt = TimeTaggerFacade(card)
    result = _fast_find(tt, "apd_red")
    assert result.verdict == "good" and result.role == "apd_red"
    assert result.in_inputs_map
    assert result.field_name == "inputs.apd_red.triggerV"
    assert result.block_line() == f'"triggerV": {result.level_v:.3f}'
    assert card.channelInfo("apd_red").trigger_v == -0.25, "restored"


def test_preflight_lists_every_detector_input(card):
    report = TimeTaggerFacade(card).preflight(duration_s=0.01)
    names = [item.name for item in report.items]
    assert "photons arriving on apd_green" in names
    assert "photons arriving on apd_red" in names


def test_legacy_preflight_keeps_its_single_photons_line():
    info = TimeTaggerInfo(simulation=True, photonsChannel=-1, photonsTriggerV=-0.25)
    card = _card(info)
    try:
        names = [i.name for i in TimeTaggerFacade(card).preflight(duration_s=0.01).items]
        assert "photons arriving" in names
        assert not any(n.startswith("photons arriving on") for n in names)
    finally:
        card.finalize()


class _CountingDetector(_FakeDetector):
    def timeResolvedCapabilities(self):
        return {"supports_lifetime_fit": False, "time_axis": None}


@pytest.fixture
def rig(qapp, card):
    made = []

    def build():
        master = SimpleNamespace(
            detectorsManager=_Detectors({"FLIM_green": _FakeDetector(), "FLIM_red": _FakeDetector(),
                                         "COUNT": _CountingDetector()}),
            timeTaggerManager=card)
        setup = SimpleNamespace(timeTagger=card.info)
        widget = WidgetFactory(None).createWidget(LifetimeWidget)
        controller = LifetimeController(setupInfo=setup, commChannel=_Comm(), master=master,
                                        widget=widget, factory=None, moduleCommChannel=None)
        card.stopHealthSampling()
        made.append(controller)
        return SimpleNamespace(controller=controller, widget=widget, setup=setup)

    yield build
    for controller in made:
        controller.closeEvent()


def test_lifetime_widget_offers_flim_detectors_and_a_row_per_input(rig):
    r = rig()
    assert set(r.controller._detectors) == {"FLIM_green", "FLIM_red"}
    assert set(r.widget._roleRows) == {"apd_green", "apd_red", "sync", "line_clock"}
    assert r.widget.ratesTable.item(r.widget._roleRows["apd_red"], 0).text() == "apd_red (-4)"


def test_keep_writes_a_second_inputs_level_into_its_entry(rig, qtbot, monkeypatch):
    monkeypatch.setattr(TimeTaggerFacade, "find_trigger_level", _fast_find)
    saved = []
    from imswitch.imcontrol.model import configfiletools
    monkeypatch.setattr(configfiletools, "loadOptions",
                        lambda: (SimpleNamespace(setupFileName="x.json"), False))
    monkeypatch.setattr(configfiletools, "saveSetupInfo",
                        lambda options, setup: saved.append(setup.timeTagger))
    r = rig()
    r.controller._levelResult = None
    r.widget.ratesTable.cellWidget(r.widget._roleRows["apd_red"], 3).click()
    qtbot.waitUntil(lambda: r.controller._levelResult is not None, timeout=15000)
    qtbot.waitUntil(lambda: not r.controller._workerBusy(), timeout=5000)
    result = r.controller._levelResult
    assert r.widget.keepLevelButton.isEnabled()
    r.widget.keepLevelButton.click()
    qtbot.waitUntil(lambda: bool(saved), timeout=5000)
    kept = saved[-1]
    assert kept.inputs["apd_red"].triggerV == round(result.level_v, 3)
    assert kept.inputs["apd_green"].triggerV == -0.25
    assert kept.photonsTriggerV == TimeTaggerInfo().photonsTriggerV, "no legacy field written"
    kept.resolvedInputs()   # still a valid block
    deadline = time.monotonic() + 5
    while "kept in the setup file" not in r.widget.footerStatus.text() and time.monotonic() < deadline:
        qtbot.wait(20)
    assert "inputs.apd_red.triggerV" in r.widget.footerStatus.text()
