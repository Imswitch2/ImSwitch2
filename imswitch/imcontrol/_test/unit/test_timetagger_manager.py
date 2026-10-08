"""TimeTaggerManager: the shared card, its roles, conditioning and scan hold.

Everything runs against the in-process mock; nothing needs the vendor
library. The ``nidaq`` stand-ins only carry ``isSimulated``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.SetupInfo import SetupInfo, TimeTaggerInfo
from imswitch.imcontrol.model.interfaces.timetagger_mock import (
    TEST_SIGNAL_RATE_HZ,
    MockTimeTaggerApi,
)
from imswitch.imcontrol.model.managers.TimeTaggerManager import (
    ROLES,
    TimeTaggerBusyError,
    TimeTaggerError,
    TimeTaggerManager,
)

pytestmark = pytest.mark.nohardware


def _simulated_nidaq(simulated=True):
    return SimpleNamespace(isSimulated=simulated)


# --------------------------------------------------------------------------- #
# SetupInfo                                                                    #
# --------------------------------------------------------------------------- #


def test_setup_info_has_no_time_tagger_by_default():
    assert SetupInfo().timeTagger is None


def test_time_tagger_block_round_trips_through_json():
    setup = SetupInfo.from_json(
        '{"timeTagger": {"photonsChannel": -1, "photonsTriggerV": -0.25,'
        ' "frameClockChannel": null, "filterSyncByPhotons": true}}',
        infer_missing=True,
    )
    info = setup.timeTagger
    assert isinstance(info, TimeTaggerInfo)
    assert info.photonsChannel == -1
    assert info.photonsTriggerV == -0.25
    assert info.frameClockChannel is None
    assert info.laserSyncChannel == 2  # default
    assert info.filterSyncByPhotons is True
    assert info.useMockOnFailure is False  # deliberately not Teensy's True


# --------------------------------------------------------------------------- #
# Connection and roles                                                         #
# --------------------------------------------------------------------------- #


def test_simulation_uses_the_mock_and_resolves_roles():
    info = TimeTaggerInfo(simulation=True, photonsChannel=-1,
                          frameClockChannel=4, photonsDeadtimePs=50_000,
                          lineClockDelayPs=1200)
    tt = TimeTaggerManager(info)

    assert tt.connected and tt.isMock
    assert tt.model.endswith("(mock)")
    assert set(tt.channels()) == {"photons", "laser_sync", "line_clock", "frame_clock"}
    assert not tt.hasRole("sted_pulse")
    assert tt.channel("photons") == -1
    assert tt.channelInfo("photons").edge == "falling"
    assert tt.channelInfo("photons").input == 1
    # Conditioning was applied at connect.
    card = tt.tagger
    assert card.getTriggerLevel(1) == 0.5
    assert card.getDeadtime(-1) == 50_000
    # A positive line delay moves the detector's marker pattern, not the
    # card's input (the raw line channel stays visible to the diagnostics);
    # the frame clock, which has no pattern, takes it on the card.
    assert card.getInputDelay(3) == 0
    assert card.getInputDelay(4) == 1200
    assert tt.patternOffsetPs("line_clock") == 10_000 + 1200
    assert tt.patternOffsetPs("frame_clock") == 10_000
    assert tt.tcspcDirection == "forward"


def test_roles_are_the_documented_closed_set():
    assert ROLES == ("photons", "laser_sync", "line_clock", "frame_clock", "sted_pulse")


def test_unknown_role_is_a_clear_error():
    tt = TimeTaggerManager(TimeTaggerInfo(simulation=True))
    with pytest.raises(TimeTaggerError, match="'sted_pulse' is not configured"):
        tt.channel("sted_pulse")


def test_filter_sets_the_conditional_filter_and_reverses_direction():
    tt = TimeTaggerManager(TimeTaggerInfo(simulation=True, filterSyncByPhotons=True))
    card = tt.tagger
    assert card.getConditionalFilterTrigger() == [1]
    assert card.getConditionalFilterFiltered() == [2]
    assert tt.tcspcDirection == "reverse"
    assert tt.metadata()["tcspc_direction"] == "reverse"


def test_missing_library_is_logged_not_raised_and_ensure_connected_raises():
    class _NoLibrary:
        @staticmethod
        def createTimeTagger(*_a):
            raise RuntimeError("no card on USB")

    tt = TimeTaggerManager(TimeTaggerInfo(), api=_NoLibrary)
    assert not tt.connected
    with pytest.raises(TimeTaggerError, match="no card on USB"):
        tt.ensureConnected()
    with pytest.raises(TimeTaggerError):
        _ = tt.tagger


def test_mock_fallback_needs_both_the_flag_and_a_simulated_nidaq():
    class _NoLibrary:
        @staticmethod
        def createTimeTagger(*_a):
            raise RuntimeError("no card on USB")

    rig = TimeTaggerManager(
        TimeTaggerInfo(useMockOnFailure=True), api=_NoLibrary,
        nidaqManager=_simulated_nidaq(False),
    )
    assert not rig.connected, "a rig must never image a missing card as zeros"

    bench = TimeTaggerManager(
        TimeTaggerInfo(useMockOnFailure=True), api=_NoLibrary,
        nidaqManager=_simulated_nidaq(True),
    )
    assert bench.connected and bench.isMock

    noflag = TimeTaggerManager(
        TimeTaggerInfo(), api=_NoLibrary, nidaqManager=_simulated_nidaq(True),
    )
    assert not noflag.connected


def test_injected_api_is_used_for_real_or_mock_alike():
    api = MockTimeTaggerApi(rates_hz={1: 1.2e6, 2: 80e6})
    tt = TimeTaggerManager(TimeTaggerInfo(serial="X1"), api=api)
    assert tt.api is api
    assert tt.serial == "X1"
    assert api.created[0] is tt.tagger
    rates = api.Countrate(tt.tagger, [tt.channel("photons"), tt.channel("laser_sync")])
    assert list(rates.getData()) == [1.2e6, 80e6]


# --------------------------------------------------------------------------- #
# Conditioning writes and the scan hold                                        #
# --------------------------------------------------------------------------- #


def test_trigger_level_writes_through_and_updates_the_role():
    tt = TimeTaggerManager(TimeTaggerInfo(simulation=True))
    tt.setTriggerLevel("photons", -0.3)
    assert tt.tagger.getTriggerLevel(1) == -0.3
    assert tt.channelInfo("photons").trigger_v == -0.3
    tt.setDelay("line_clock", 800)
    assert tt.channelInfo("line_clock").delay_ps == 800
    tt.setDeadtime("photons", 40_000)
    assert tt.tagger.getDeadtime(1) == 40_000


def test_scan_hold_refuses_conditioning_until_released():
    tt = TimeTaggerManager(TimeTaggerInfo(simulation=True))
    tt.beginScanHold("FLIM")
    assert tt.scanHeld
    with pytest.raises(TimeTaggerBusyError, match="FLIM"):
        tt.setTriggerLevel("photons", 0.1)
    with pytest.raises(TimeTaggerBusyError):
        tt.setDelay("line_clock", 1)
    assert tt.tagger.getTriggerLevel(1) == 0.5, "the refused write changed nothing"

    tt.endScanHold("FLIM")
    tt.endScanHold("FLIM")  # releasing twice is harmless
    assert not tt.scanHeld
    tt.setTriggerLevel("photons", 0.1)
    assert tt.tagger.getTriggerLevel(1) == 0.1


def test_two_holders_both_have_to_release():
    tt = TimeTaggerManager(TimeTaggerInfo(simulation=True))
    tt.beginScanHold("FLIM")
    tt.beginScanHold("FLIM2")
    tt.endScanHold("FLIM")
    assert tt.scanHeld
    tt.endScanHold("FLIM2")
    assert not tt.scanHeld


# --------------------------------------------------------------------------- #
# Metadata, finalize, test signal                                              #
# --------------------------------------------------------------------------- #


def test_metadata_snapshot_names_every_role_and_the_card():
    tt = TimeTaggerManager(TimeTaggerInfo(simulation=True, photonsChannel=-1))
    md = tt.metadata()
    assert md["is_mock"] is True
    assert md["serial"] == "MOCK-000001"
    assert md["roles"]["photons"] == {
        "channel": -1, "edge": "falling", "trigger_v": 0.5,
        "deadtime_ps": 0, "delay_ps": 0,
    }
    assert set(md["roles"]) == {"photons", "laser_sync", "line_clock"}


def test_finalize_frees_the_card_once_and_refuses_reconnect():
    tt = TimeTaggerManager(TimeTaggerInfo(simulation=True))
    card = tt.tagger
    assert tt.finalize() is True
    assert card.freed
    assert tt.finalize() is True  # idempotent
    assert not tt.connected
    with pytest.raises(TimeTaggerError, match="finalized"):
        tt.ensureConnected()


def test_the_mock_test_signal_counts_like_the_card_would():
    tt = TimeTaggerManager(TimeTaggerInfo(simulation=True))
    card = tt.tagger
    card.setTestSignal([tt.channel("photons")], True)
    rate = tt.api.Countrate(card, [tt.channel("photons")])
    assert rate.getData()[0] == TEST_SIGNAL_RATE_HZ
    card.setTestSignal(tt.channel("photons"), False)
    assert tt.api.Countrate(card, [1]).getData()[0] == 0.0


# --------------------------------------------------------------------------- #
# Conditioning failures and the test signal (review round 2)                   #
# --------------------------------------------------------------------------- #


class _FilterRefusingApi(MockTimeTaggerApi):
    """A card whose conditional filter cannot be set (a wiring or firmware
    refusal): the rest of the conditioning goes through."""

    def createTimeTagger(self, serial=None):
        tagger = super().createTimeTagger(serial)
        original = tagger.setConditionalFilter

        def refuse(trigger, filtered):
            if trigger:
                raise RuntimeError("conditional filter unsupported on this input")
            return original(trigger, filtered)

        tagger.setConditionalFilter = refuse
        tagger._original_setConditionalFilter = original
        return tagger


def test_a_failed_conditioning_refuses_scans_and_never_claims_reverse():
    info = TimeTaggerInfo(simulation=True, filterSyncByPhotons=True)
    tt = TimeTaggerManager(info, api=_FilterRefusingApi())
    assert tt.connected, "the card is open: the application still starts"
    assert not tt.conditioned
    assert "unsupported" in str(tt.conditioningError)
    # Unfiltered data must never be read as reverse TCSPC.
    assert tt.tcspcDirection == "forward"
    assert tt.metadata()["conditioned"] is False
    assert tt.health(0.001).filter_on is False
    # The diagnostics still reach the card (to find out why) ...
    tt.ensureConnected()
    assert tt.api.Countrate(tt.tagger, [2]).getData()[0] > 0
    # ... but a scan is refused (the detector turns this into a rollback) ...
    with pytest.raises(TimeTaggerError, match="conditioning could not be applied"):
        tt.beginScanHold("FLIM")
    assert not tt.scanHeld
    # ... until the conditioning goes through, which every attempt retries.
    tt.tagger.setConditionalFilter = tt.tagger._original_setConditionalFilter
    tt.beginScanHold("FLIM")
    assert tt.scanHeld and tt.conditioned and tt.tcspcDirection == "reverse"
    tt.endScanHold("FLIM")
    assert tt.tagger.getConditionalFilterTrigger() == [1]


def test_the_test_signal_is_refused_while_a_scan_holds_the_card():
    tt = TimeTaggerManager(TimeTaggerInfo(simulation=True))
    tt.beginScanHold("FLIM")
    with pytest.raises(TimeTaggerBusyError, match="test signal"):
        tt.setTestSignal(["line_clock"], True)
    assert not tt.tagger.getTestSignal(3), "no fake pixel markers into the scan"
    tt.endScanHold("FLIM")
    tt.setTestSignal(["line_clock"], True)
    assert tt.tagger.getTestSignal(3)
    tt.setTestSignal(["line_clock"], False)
