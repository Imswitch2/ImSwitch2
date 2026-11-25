"""Finding a trigger level, per input, on the mock card.

``find_trigger_level`` sweeps an input's levels without the user choosing a
range and returns a verdict; these tests drive every verdict through the
mock's comparator model (plateau between the noise floor and the pulse
amplitude, nothing for the wrong polarity, optional noise counts near 0 V),
the restore/apply contract, the warnings shown where levels are set, and a
card that only counts photons -- no sync, no clocks, no FLIM.
"""

from __future__ import annotations

import numpy as np
import pytest

from imswitch.imcontrol.model.SetupInfo import TimeTaggerInfo
from imswitch.imcontrol.model.interfaces.timetagger_mock import MockTimeTaggerApi, SignalModel
from imswitch.imcontrol.model.managers.TimeTaggerManager import (
    TimeTaggerBusyError,
    TimeTaggerManager,
)
from imswitch.imcontrol.model.timeresolved.preflight import rep_rate_mismatch, run_device_preflight
from imswitch.imcontrol.model.workflows.time_tagger_facade import (
    LEVEL_VERDICTS,
    TimeTaggerFacade,
    level_warnings,
)

pytestmark = pytest.mark.nohardware

DURATION = 0.005   # per level: the mock's rates are exact, a short count is enough


def _tt(faults=None, info=None, **model) -> TimeTaggerFacade:
    info = info or TimeTaggerInfo(simulation=True, photonsChannel=-1, photonsTriggerV=-0.25,
                                  stedPulseChannel=5, mockFaults=faults or {})
    signal = SignalModel.from_info(info)
    for key, value in model.items():
        setattr(signal, key, value)
    return TimeTaggerFacade(TimeTaggerManager(info, api=MockTimeTaggerApi(model=signal)))


# --------------------------------------------------------------------------- #
# Every verdict                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role, amplitude", [("photons", -0.5), ("laser_sync", 0.8)])
def test_good_finds_the_middle_of_the_plateau(role, amplitude):
    tt = _tt()
    result = tt.find_trigger_level(role, DURATION)
    assert result.verdict == "good", result.summary()
    low, high = result.plateau_v
    # The plateau runs from the comparator's noise floor to the pulse top.
    assert 0.05 <= low <= 0.12 and abs(amplitude) - 0.1 <= high <= abs(amplitude)
    assert result.level_v == pytest.approx(np.sign(amplitude) * (low + high) / 2, abs=1e-3)
    assert result.width_v >= 0.1 and result.margin_v == pytest.approx(result.width_v / 2)
    assert result.rate_hz > 0 and not result.applied
    assert len(result.levels_v) == len(result.rates_hz) >= 16
    assert "apply it" in result.action


def test_wrong_polarity_is_found_by_probing_the_other_sign():
    tt = _tt({"wrong_sync_polarity": True})
    result = tt.find_trigger_level("laser_sync", DURATION)
    assert result.verdict == "wrong_polarity", result.summary()
    assert result.level_v < 0 and "use channel -2" in result.reason
    assert any(v < 0 for v in result.levels_v) and any(v > 0 for v in result.levels_v)
    assert result.block_line() == '"laserSyncTriggerV": %.3f' % result.level_v


def test_no_signal_when_nothing_counts_at_any_level():
    result = _tt({"dead_photons": True}).find_trigger_level("photons", DURATION)
    assert result.verdict == "no_signal" and result.level_v is None
    assert result.plateau_v is None and LEVEL_VERDICTS["no_signal"] in result.summary()


def test_noise_only_when_counts_only_fall_away_from_zero():
    result = _tt({"dead_photons": True}, noise_rate_hz=2e6).find_trigger_level(
        "photons", DURATION)
    assert result.verdict == "noise_only", result.summary()
    assert result.level_v is None


def test_too_small_when_the_pulses_barely_clear_the_noise():
    tt = _tt()
    tt.manager.tagger.signal_model.pulse_amplitude_v[5] = 0.12   # a photodiode into 50 ohm
    result = tt.find_trigger_level("sted_pulse", DURATION)
    assert result.verdict == "too_small", result.summary()
    assert result.width_v < 0.1 and 0.05 < result.level_v < 0.12
    # Smaller still: a bump without a plateau, still a signal, not noise.
    tt.manager.tagger.signal_model.pulse_amplitude_v[5] = 0.07
    result = tt.find_trigger_level("sted_pulse", DURATION)
    assert result.verdict == "too_small" and result.plateau_v is None


def test_a_scan_clock_between_scans_needs_a_scan_and_keeps_the_default():
    result = _tt().find_trigger_level("line_clock", DURATION)
    assert result.verdict == "needs_scan"
    assert result.level_v == pytest.approx(0.5)
    assert "only while a scan runs" in result.action and "1-1.5 V" in result.reason


def test_a_scan_clock_during_a_scan_gets_its_plateau():
    tt = _tt()
    tagger = tt.manager.tagger
    edges = np.arange(20_000, dtype=np.int64) * 1_000_000_000   # 1 kHz for 20 s
    tagger.load_scan_edges({3: edges}, 20.0)
    tagger.start_scan()
    result = tt.find_trigger_level("line_clock", 0.02)
    assert result.verdict == "good", result.summary()
    assert 0.5 < result.level_v < 0.7, "the middle of 0.1 to 1.2 V"
    assert result.rate_hz == pytest.approx(1000, rel=0.1)


# --------------------------------------------------------------------------- #
# Restore and apply                                                            #
# --------------------------------------------------------------------------- #


def test_the_level_is_restored_unless_a_good_one_is_applied():
    tt = _tt()
    card = tt.manager
    tt.find_trigger_level("photons", DURATION)
    assert card.channelInfo("photons").trigger_v == -0.25
    assert card.tagger.getTriggerLevel(1) == -0.25

    result = tt.find_trigger_level("photons", DURATION, apply=True)
    assert result.applied and card.channelInfo("photons").trigger_v == result.level_v
    assert card.tagger.getTriggerLevel(1) == result.level_v
    assert card.calibrationOwner is None, "the transaction is released"

    bad = _tt({"wrong_sync_polarity": True})
    result = bad.find_trigger_level("laser_sync", DURATION, apply=True)
    assert not result.applied, "only a good level is ever applied"
    assert bad.manager.channelInfo("laser_sync").trigger_v == 0.5


def test_the_level_is_restored_when_the_sweep_fails_midway(monkeypatch):
    tt = _tt()
    card = tt.manager
    calls = []
    real = card.api.Countrate

    def failing(tagger, channels):
        calls.append(1)
        if len(calls) == 4:
            raise RuntimeError("USB gone")
        return real(tagger, channels)
    monkeypatch.setattr(card.api, "Countrate", failing)
    with pytest.raises(RuntimeError, match="USB gone"):
        tt.find_trigger_level("photons", DURATION)
    assert card.tagger.getTriggerLevel(1) == -0.25
    assert card.channelInfo("photons").trigger_v == -0.25
    assert card.calibrationOwner is None


def test_finding_a_level_is_refused_while_a_scan_holds_the_card():
    tt = _tt()
    tt.manager.beginScanHold("FLIM")
    with pytest.raises(TimeTaggerBusyError):
        tt.find_trigger_level("photons", DURATION)
    assert tt.manager.tagger.getTriggerLevel(1) == -0.25


# --------------------------------------------------------------------------- #
# Any input, and a card that only counts                                       #
# --------------------------------------------------------------------------- #


def test_any_input_without_a_role_can_be_swept_by_its_channel():
    """A second detector on input 6 (not declarable as a role today): the
    level finding works per input, so it already serves several detectors."""
    tt = _tt()
    model = tt.manager.tagger.signal_model
    model.input_rates_hz[6] = 3e5
    model.pulse_amplitude_v[6] = -0.6
    tt.manager.tagger.setTriggerLevel(6, -0.2)
    result = tt.find_trigger_level(-6, DURATION, apply=True)
    assert result.verdict == "good" and result.role is None and result.input == "input -6"
    assert result.block_line() is None, "no block field for an input without a role"
    assert tt.manager.tagger.getTriggerLevel(6) == result.level_v
    assert tt.count_rates([-6], DURATION).rates_hz[-6] == pytest.approx(3e5, rel=0.05)


def _counting_only() -> TimeTaggerFacade:
    info = TimeTaggerInfo(simulation=True, photonsChannel=-1, photonsTriggerV=-0.25,
                          laserSyncChannel=None, lineClockChannel=None)
    return _tt(info=info)


def test_a_counting_only_card_has_only_its_photon_input():
    tt = _counting_only()
    assert tt.roles() == ["photons"]
    assert set(tt.channels()) == {"photons"}
    result = tt.find_trigger_level("photons", DURATION)
    assert result.verdict == "good", result.summary()
    rates = tt.count_rates(duration_s=DURATION).rates_hz
    assert list(rates) == ["photons"] and rates["photons"] > 0
    assert tt.manager.tagger.getOverflowsAndClear() == 0, "no phantom sync in the tag budget"


def test_preflight_on_a_counting_only_card_checks_what_is_there():
    report = run_device_preflight(_counting_only(), None, duration_s=DURATION)
    names = [item.name for item in report.items]
    assert report.summary().startswith("Time Tagger pre-flight: green"), report.summary()
    assert "photons arriving" in names and "no USB overflows" in names
    for absent in ("laser sync", "TCSPC", "pile-up", "background", "line clock", "frame clock"):
        assert not any(absent in name for name in names), (absent, names)


# --------------------------------------------------------------------------- #
# Warnings where levels are set                                                #
# --------------------------------------------------------------------------- #


def test_level_warnings_name_what_is_wrong():
    card = _tt().manager
    assert level_warnings(card, "photons", -0.25, rate_hz=5e5) == []
    sign = level_warnings(card, "photons", 0.25)
    assert len(sign) == 1 and "falling edge" in sign[0]
    assert any("noise" in w for w in level_warnings(card, "photons", -0.03))
    assert any("DAQ line" in w for w in level_warnings(card, "line_clock", 1.3))
    assert level_warnings(card, "line_clock", 0.5, rate_hz=0.0) == [], "silent between scans"
    assert any("0 Hz during a scan" in w
               for w in level_warnings(card, "line_clock", 0.5, rate_hz=0.0, scan_running=True))
    assert any("counts nothing" in w for w in level_warnings(card, "photons", -0.25, rate_hz=0.0))
    assert any("outside the input range" in w for w in level_warnings(card, -7, 3.0))


def test_rep_rate_mismatch_reuses_the_preflight_tolerance():
    assert rep_rate_mismatch(80.0e6, 80.0) is None
    assert rep_rate_mismatch(80.2e6, 80.0) is None
    assert "40" in rep_rate_mismatch(80.0e6, 40.0)
    assert rep_rate_mismatch(0.0, 80.0) is None
