"""The Lifetime widget on its own: modes, settings, displays, the alias."""

from __future__ import annotations

import numpy as np
import pytest

from imswitch.imcontrol.view import widgets
from imswitch.imcontrol.view.widgets.LifetimeWidget import (
    FLIMHistWidget,
    LifetimeWidget,
    MODES,
)
from imswitch.imcontrol.view.widgets.basewidgets import WidgetFactory

pytestmark = pytest.mark.nohardware


@pytest.fixture
def widget(qapp):
    return WidgetFactory(None).createWidget(LifetimeWidget)


def test_flimhist_resolves_to_the_lifetime_widget_on_its_flim_view(qapp):
    assert widgets.FLIMHistWidget is FLIMHistWidget
    assert issubclass(FLIMHistWidget, LifetimeWidget)
    alias = WidgetFactory(None).createWidget(widgets.FLIMHistWidget)
    assert alias.getMode() == 'FLIM'


def test_modes_switch_the_panel_and_hide_the_settings_in_signals(widget):
    seen = []
    widget.sigModeChanged.connect(seen.append)
    for mode in MODES:
        widget.modeButtons[mode].setChecked(True)
        assert widget.getMode() == mode
        assert widget._modeStack.currentIndex() == MODES.index(mode)
    assert seen == list(MODES)[1:] + [] or seen[-1] == MODES[-1]
    assert widget._settingsGroup.isHidden() or not widget._settingsGroup.isVisibleTo(widget)
    widget.setMode('FLIM')
    assert not widget._settingsGroup.isHidden()


def test_settings_emit_once_per_edit_and_round_trip(widget):
    seen = []
    widget.sigSettingChanged.connect(lambda k, v: seen.append((k, v)))
    widget.settingFields['t0_ps'].setValue(1300)
    widget.settingFields['fit_method'].setCurrentText('phasor')
    assert seen == [('t0_ps', 1300), ('fit_method', 'phasor')]
    widget.setSetting('rep_rate_mhz', 79.9876)
    assert widget.getSetting('rep_rate_mhz') == pytest.approx(79.9876)
    assert len(seen) == 2, "setSetting is silent"
    widget.setSetting('tau_min_ns', 3.0)
    widget.setSetting('tau_max_ns', 1.0)
    assert widget.getColourRange() == (0.0, 1.0), "an inverted range falls back"


def test_decay_histogram_scatter_and_rates_render(widget):
    t = (np.arange(40) + 0.5) * 0.32
    counts = 500 * np.exp(-t / 2.5)
    widget.updateDecay(t, counts, peak_ns=0.16, background_per_bin=0.5, direction='reverse',
                       tau_ns=2.5, binwidth_ps=320)
    assert 'reverse' in widget._decayInfo.text() and '40×320 ps' in widget._decayInfo.text()
    widget.logCheck.setChecked(True)
    widget.updateDecay(t, counts, peak_ns=0.16)
    assert widget._decayPlot.getPlotItem().ctrl.logYCheck.isChecked()
    widget.updateLifetimeHistogram(np.array([1.0, 2.0, 0.0, np.nan, 3.0]))
    assert 'Valid pixels: 3' in widget.histStatLabel.text()
    widget.updateLifetimeHistogram(np.zeros(5))
    assert 'Valid pixels: 0' in widget.histStatLabel.text()
    widget.updateScatter(np.arange(10), np.linspace(1, 3, 10))
    assert 'median 2.00 ns' in widget.tauStatLabel.text()
    widget.updatePhasor(0.5, 0.4)
    assert '(0.500, 0.400)' in widget.phasorLabel.text()

    widget.setRoles(['photons', 'laser_sync'])
    levels = []
    widget.sigTriggerLevelEdited.connect(lambda r, v: levels.append((r, v)))
    widget.setTriggerLevel('photons', -0.25)
    assert levels == [], "setTriggerLevel is silent"
    widget.ratesTable.cellWidget(0, 2).setValue(-0.3)
    assert levels == [('photons', -0.3)]
    widget.updateRates({'photons': 1.2e6, 'laser_sync': 80e6}, direction='forward',
                       filter_on=False, overflows=2)
    assert widget.ratesTable.item(0, 1).text() == '1.200 MHz'
    assert 'overflows: 2' in widget.signalsInfo.text()
    widget.setPreflight([('ok', 'green'), ('fail', 'red line')])
    assert 'red line' in widget.signalsText.toPlainText()


def test_running_state_locks_the_footer_and_unchecks_live(widget):
    widget.liveButton.setChecked(True)
    widget.setRunning(True, live=True)
    assert not widget.runButton.isEnabled() and widget.stopButton.isEnabled()
    assert not widget.saveButton.isEnabled()
    widget.setRunning(False)
    assert widget.runButton.isEnabled() and not widget.liveButton.isChecked()


def test_gate_table_regions_and_presets(widget):
    seen = []
    widget.sigGatesChanged.connect(lambda: seen.append(True))
    widget.setGates([dict(name='early', start_ns=0.5, stop_ns=2.5, reference='peak'),
                     dict(name='late', start_ns=2.5, stop_ns=8.0, reference='absolute')])
    assert seen == [], 'setGates is silent'
    assert [g['name'] for g in widget.getGates()] == ['early', 'late']
    assert len(widget._regions) == 2
    # Peak-relative regions follow the decay's peak.
    t = (np.arange(40) + 0.5) * 0.32
    widget.updateDecay(t, np.ones(40), peak_ns=1.0)
    assert widget._regions[0].getRegion() == pytest.approx((1.5, 3.5))
    assert widget._regions[1].getRegion() == pytest.approx((2.5, 8.0))
    # Dragging a region edits the table (relative to the peak); setRegion
    # ends with the same finished signal a drag does.
    widget._regions[0].setRegion((2.0, 4.0))
    assert widget.getGates()[0]['start_ns'] == pytest.approx(1.0)
    assert widget.getGates()[0]['stop_ns'] == pytest.approx(3.0)
    assert seen == [True]
    # Editing the table moves the region.
    widget.gateTable.item(1, 2).setText('9')
    assert widget._regions[1].getRegion() == pytest.approx((2.5, 9.0))
    widget.addGateButton.click()
    assert len(widget.getGates()) == 3 and widget.getGates()[2]['start_ns'] == 9.0
    widget.removeGateButton.click()
    assert len(widget.getGates()) == 2
    widget.setPresets(['sted_early_late'])
    chosen = []
    widget.sigPresetSelected.connect(chosen.append)
    widget.loadPresetButton.click()
    assert chosen == ['sted_early_late']
    widget.setStedMarker(0.3)
    assert widget._stedLine.isVisible()
