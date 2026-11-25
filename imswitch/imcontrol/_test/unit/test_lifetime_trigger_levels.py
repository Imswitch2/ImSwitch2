"""The Lifetime widget's Signals panel finding, applying and keeping trigger
levels on the mock card, its warnings, a counting-only card, and the pooled
lifetime histogram staying bounded."""

from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np
import pytest

import importlib
from imswitch.imcontrol.controller.controllers.LifetimeController import LifetimeController
from imswitch.imcontrol.model.SetupInfo import TimeTaggerInfo
from imswitch.imcontrol.model.interfaces.timetagger_mock import MockTimeTaggerApi, SignalModel
from imswitch.imcontrol.model.managers.TimeTaggerManager import TimeTaggerManager
from imswitch.imcontrol.view.widgets.LifetimeWidget import LifetimeWidget
from imswitch.imcontrol.view.widgets.basewidgets import WidgetFactory
from imswitch.imcontrol._test.unit.test_lifetime_controller import _Comm, _Detectors, _FakeDetector

pytestmark = pytest.mark.nohardware

lc_module = importlib.import_module(
    'imswitch.imcontrol.controller.controllers.LifetimeController')


def _card(info=None, slow_levels_s=0.0):
    info = info or TimeTaggerInfo(simulation=True, photonsChannel=-1, photonsTriggerV=-0.25)
    api = MockTimeTaggerApi(model=SignalModel.from_info(info))
    card = TimeTaggerManager(info, api=api)
    if slow_levels_s:
        tagger = card.tagger
        original = tagger.setTriggerLevel

        def slow(channel, voltage):
            time.sleep(slow_levels_s)
            original(channel, voltage)
        tagger.setTriggerLevel = slow
    return card


@pytest.fixture
def make(qapp, monkeypatch):
    made = []

    def build(card, detectors=True):
        comm = _Comm()
        detector = _FakeDetector()
        master = SimpleNamespace(
            detectorsManager=_Detectors({'FLIM': detector} if detectors else {}),
            timeTaggerManager=card)
        setup = SimpleNamespace(timeTagger=card.info)
        widget = WidgetFactory(None).createWidget(LifetimeWidget)
        controller = LifetimeController(setupInfo=setup, commChannel=comm, master=master,
                                        widget=widget, factory=None, moduleCommChannel=None)
        card.stopHealthSampling()   # the tests feed health samples themselves
        made.append((controller, card))
        return SimpleNamespace(controller=controller, widget=widget, card=card, setup=setup)

    yield build
    for controller, card in made:
        controller.closeEvent()
        card.finalize()


def _find(rig, qtbot, role):
    rig.controller._levelResult = None
    rig.widget.ratesTable.cellWidget(rig.widget._roleRows[role], 3).click()
    qtbot.waitUntil(lambda: rig.controller._levelResult is not None, timeout=15000)
    qtbot.waitUntil(lambda: not rig.controller._workerBusy(), timeout=5000)
    return rig.controller._levelResult


def test_find_shows_the_level_and_applies_it_only_on_request(make, qtbot, monkeypatch):
    monkeypatch.setattr('imswitch.imcontrol.model.workflows.time_tagger_facade.'
                        'TimeTaggerFacade.find_trigger_level',
                        _fast_find)
    rig = make(_card())
    widget, card = rig.widget, rig.card
    assert widget.ratesTable.item(0, 0).text() == 'photons (-1)'
    result = _find(rig, qtbot, 'photons')
    assert result.verdict == 'good'
    assert card.channelInfo('photons').trigger_v == -0.25, 'never applied silently'
    assert 'good' in widget.levelResultLabel.text() and '"photonsTriggerV"' in widget.levelResultLabel.text()
    assert widget.applyLevelButton.isEnabled() and widget.keepLevelButton.isEnabled()
    assert 'level (V)' in widget.signalsText.toPlainText(), 'the sweep is shown'

    widget.applyLevelButton.click()
    qtbot.waitUntil(lambda: card.channelInfo('photons').trigger_v == result.level_v, timeout=5000)
    qtbot.waitUntil(lambda: 'until restart' in widget.footerStatus.text(), timeout=5000)
    assert widget.triggerLevel('photons') == pytest.approx(result.level_v)

    widget.copyLevelButton.click()
    from qtpy import QtWidgets
    assert QtWidgets.QApplication.clipboard().text() == result.block_line()


def _fast_find(self, input, duration_s=0.1, **kwargs):
    """The real search with a short count per level (the mock is exact)."""
    return _REAL_FIND(self, input, 0.002, **kwargs)


from imswitch.imcontrol.model.workflows.time_tagger_facade import TimeTaggerFacade as _F  # noqa: E402
_REAL_FIND = _F.find_trigger_level


def test_apply_and_keep_writes_the_setup_file_through_the_existing_mechanism(make, qtbot, monkeypatch):
    monkeypatch.setattr(_F, 'find_trigger_level', _fast_find)
    saved = []
    from imswitch.imcontrol.model import configfiletools
    monkeypatch.setattr(configfiletools, 'loadOptions', lambda: (SimpleNamespace(setupFileName='x.json'), False))
    monkeypatch.setattr(configfiletools, 'saveSetupInfo',
                        lambda options, setup: saved.append(setup.timeTagger.laserSyncTriggerV))
    rig = make(_card())
    result = _find(rig, qtbot, 'laser_sync')
    rig.widget.keepLevelButton.click()
    qtbot.waitUntil(lambda: bool(saved), timeout=5000)
    assert saved == [round(result.level_v, 3)]
    assert rig.setup.timeTagger.laserSyncTriggerV == round(result.level_v, 3)
    assert rig.card.channelInfo('laser_sync').trigger_v == result.level_v
    assert 'kept in the setup file' in rig.widget.footerStatus.text()


def test_a_scan_clock_during_a_held_scan_says_what_to_do_instead(make, qtbot):
    rig = make(_card())
    rig.card.beginScanHold('FLIM')
    rig.widget.ratesTable.cellWidget(rig.widget._roleRows['line_clock'], 3).click()
    assert not rig.controller._workerBusy(), 'no sweep that could only fail'
    text = rig.widget.levelResultLabel.text()
    assert 'fires only during a scan' in text and '"enabled" set to False' in text
    assert not rig.widget.applyLevelButton.isEnabled()
    rig.card.endScanHold('FLIM')


def test_a_level_edit_returns_at_once_and_lands_in_order(make, qtbot):
    rig = make(_card(slow_levels_s=0.3))
    widget, card = rig.widget, rig.card
    spin = widget.ratesTable.cellWidget(widget._roleRows['photons'], 2)
    started = time.monotonic()
    for value in (-0.2, -0.3, -0.35):
        spin.setValue(value)
    assert time.monotonic() - started < 0.15, 'the GUI waited for the card'
    qtbot.waitUntil(lambda: card.channelInfo('photons').trigger_v == -0.35, timeout=5000)
    assert card.tagger.getTriggerLevel(1) == -0.35


def test_a_refused_level_edit_puts_the_cards_level_back(make, qtbot):
    rig = make(_card())
    rig.card.beginScanHold('FLIM')
    spin = rig.widget.ratesTable.cellWidget(rig.widget._roleRows['photons'], 2)
    spin.setValue(-0.4)
    qtbot.waitUntil(lambda: spin.value() == pytest.approx(-0.25), timeout=5000)
    assert 'scan holds the card' in rig.widget.footerStatus.text()
    rig.card.endScanHold('FLIM')


def test_warnings_appear_in_the_check_column(make, qtbot):
    rig = make(_card())
    widget = rig.widget
    assert widget.levelWarnings('photons') == []
    widget.setTriggerLevel('photons', 0.3)          # the wrong sign for a falling edge
    rig.controller._refreshLevelWarnings()
    assert any('falling edge' in w for w in widget.levelWarnings('photons'))
    # A health sample: the sync at a rate the detector's setting disagrees with.
    widget.setSetting('rep_rate_mhz', 40.0)
    rig.controller._showHealth(SimpleNamespace(
        rates_hz={'photons': 0.0, 'laser_sync': 80e6, 'line_clock': 0.0}, overflows=0,
        tcspc_direction='forward', filter_on=False, is_mock=True))
    assert any('counts nothing' in w for w in widget.levelWarnings('photons'))
    assert any('laser_rep_rate_mhz' in w for w in widget.levelWarnings('laser_sync'))
    assert widget.levelWarnings('line_clock') == [], 'a scan clock is silent between scans'


def test_a_counting_only_card_gets_one_row_and_the_whole_level_finding(make, qtbot, monkeypatch):
    monkeypatch.setattr(_F, 'find_trigger_level', _fast_find)
    info = TimeTaggerInfo(simulation=True, photonsChannel=-1, photonsTriggerV=-0.25,
                          laserSyncChannel=None, lineClockChannel=None)
    rig = make(_card(info), detectors=False)
    widget = rig.widget
    assert widget.ratesTable.rowCount() == 1 and widget.sweepRoleCombo.count() == 1
    assert widget.levelWarnings('photons') == []
    result = _find(rig, qtbot, 'photons')
    assert result.verdict == 'good' and widget.applyLevelButton.isEnabled()
    rig.controller._showHealth(SimpleNamespace(
        rates_hz={'photons': 5e5}, overflows=0, tcspc_direction='forward',
        filter_on=False, is_mock=True))
    assert widget.levelWarnings('photons') == []
    # Pre-flight: only what is there.
    widget.preflightButton.click()
    qtbot.waitUntil(lambda: 'pre-flight' in widget.signalsText.toPlainText(), timeout=10000)
    text = widget.signalsText.toPlainText()
    assert text.startswith('Time Tagger pre-flight: green'), text
    assert 'sync' not in text and 'TCSPC' not in text


def test_the_pooled_histogram_stays_bounded(make, monkeypatch):
    monkeypatch.setattr(lc_module, 'POOL_MAX_VALUES', 1000)
    rig = make(_card())
    controller = rig.controller
    rng = np.random.default_rng(1)
    for k in range(30):
        tau = 1.0 if k < 15 else 3.0
        controller._poolLifetimes(rng.normal(tau, 0.05, size=(16, 16)).astype(np.float32))
    pool = controller._pooledLifetimes[0]
    assert len(controller._pooledLifetimes) == 1 and pool.size == 1000
    assert controller._pooledSeen == 30 * 256
    # Half the values seen were near 1 ns, half near 3 ns: so is the sample.
    assert 0.35 < np.mean(pool < 2.0) < 0.65
