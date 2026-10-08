"""The card's two exclusive holds and its single overflow owner.

From the first external review of the Lifetime 2.0 plan: a calibration's
temporary settings must exclude a scan from starting (and a scan a
calibration), and the read-and-clear overflow counter must be read in one
place so a health look can never hide an overflow from a frame.
"""

from __future__ import annotations

import pytest

from imswitch.imcontrol.model.SetupInfo import TimeTaggerInfo
from imswitch.imcontrol.model.managers.TimeTaggerManager import (
    TimeTaggerBusyError,
    TimeTaggerManager,
)

pytestmark = pytest.mark.nohardware


def _card():
    return TimeTaggerManager(TimeTaggerInfo(simulation=True))


def test_calibration_excludes_scans_and_scans_exclude_calibration():
    tt = _card()
    with tt.calibrationTransaction("rep_rate"):
        assert tt.calibrationOwner == "rep_rate"
        with pytest.raises(TimeTaggerBusyError, match="held by a calibration"):
            tt.beginScanHold("FLIM")
        assert not tt.scanHeld
    assert tt.calibrationOwner is None

    tt.beginScanHold("FLIM")
    with pytest.raises(TimeTaggerBusyError, match="while a scan holds"):
        with tt.calibrationTransaction("rep_rate"):
            pass
    tt.endScanHold("FLIM")


def test_only_the_calibration_owner_may_condition_during_its_transaction():
    tt = _card()
    with tt.calibrationTransaction("rep_rate"):
        with pytest.raises(TimeTaggerBusyError, match="owns the card"):
            tt.setTriggerLevel("photons", -0.2)
        tt.setTriggerLevel("photons", -0.2, owner="rep_rate")
        assert tt.channelInfo("photons").trigger_v == -0.2
        with pytest.raises(TimeTaggerBusyError, match="Another calibration"):
            with tt.calibrationTransaction("other"):
                pass
    tt.setTriggerLevel("photons", 0.3)  # free again


def test_transaction_is_released_on_any_exception():
    tt = _card()

    class _Cancelled(BaseException):
        pass

    with pytest.raises(_Cancelled):
        with tt.calibrationTransaction("sweep"):
            raise _Cancelled()
    assert tt.calibrationOwner is None
    tt.beginScanHold("FLIM")
    assert tt.scanHeld


def test_overflow_total_is_monotonic_and_shared():
    tt = _card()
    card = tt.tagger
    baseline = tt.overflows()
    card.addOverflows(3)
    assert tt.overflows() == baseline + 3
    assert tt.overflows() == baseline + 3, "a second read does not clear it"
    card.addOverflows(2)
    assert tt.overflows() == baseline + 5


def test_a_health_look_cannot_hide_an_overflow_from_a_frame():
    """The frame's own baseline sees the overflow a health read consumed."""
    tt = _card()
    frame_baseline = tt.overflows()           # the detector arms its scan
    tt.tagger.addOverflows(1)                 # the link drops a block
    look = tt.health(integration_s=0.001)     # the strip looks meanwhile
    assert look.overflows == 1
    assert tt.overflows() - frame_baseline == 1, "the frame is still invalid"
    second = tt.health(integration_s=0.001)
    assert second.overflows == 0, "the strip reports each overflow once"


def test_health_reports_every_role_and_the_filter_label():
    tt = TimeTaggerManager(TimeTaggerInfo(simulation=True, filterSyncByPhotons=True))
    tt.tagger.setChannelRate(1, 1.0e6)
    look = tt.health(integration_s=0.001)
    assert set(look.rates_hz) == {"photons", "laser_sync", "line_clock"}
    assert look.rates_hz["photons"] == 1.0e6
    assert look.filter_on and look.sync_rate_label == "sync (filtered)"
    assert look.tcspc_direction == "reverse"
    assert look.to_dict()["is_mock"] is True


def test_health_sampler_emits_and_finalize_joins_it():
    tt = _card()
    seen = []
    tt.startHealthSampling(period_s=0.05, callback=seen.append)
    import time
    deadline = time.monotonic() + 2.0
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen, "the sampler never emitted"
    card = tt.tagger
    assert tt.finalize() is True
    assert tt._samplerThread is None
    assert card.freed
