"""``facade.time_tagger`` on the mock card, and the pre-flight checklist.

What the Time Tagger tutorials call, exercised the way they call it: count
rates, the rep-rate procedure with its temporary divider and filter, the
histogram in both directions, the trigger sweep inside a calibration
transaction, the test signal, and the preflight report against a FLIM
detector's settings.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.model.SetupInfo import TimeTaggerInfo
from imswitch.imcontrol.model.interfaces.timetagger_mock import TEST_SIGNAL_RATE_HZ
from imswitch.imcontrol.model.managers.TimeTaggerManager import (
    TimeTaggerBusyError,
    TimeTaggerError,
    TimeTaggerManager,
)
from imswitch.imcontrol.model.managers.detectors.SwabianTimeTaggerManager import (
    SwabianTimeTaggerManager,
)
from imswitch.imcontrol.model.timeresolved.preflight import (
    PreflightReport,
    run_device_preflight,
)
from imswitch.imcontrol.model.workflows import (
    MicroscopeFacade,
    TimeTaggerFacade,
    build_facade_from_master,
)

pytestmark = pytest.mark.nohardware


class _Signal:
    def connect(self, slot):
        pass


class _Nidaq:
    isSimulated = True
    sigScanBuilt = _Signal()
    sigScanStarted = _Signal()
    sigScanDone = _Signal()


class _DetectorInfo:
    forAcquisition = True
    forFocusLock = False

    def __init__(self, props):
        self.managerProperties = props


def _facade(filter_on=False, **info_kwargs) -> TimeTaggerFacade:
    info = TimeTaggerInfo(simulation=True, photonsChannel=-1, photonsTriggerV=-0.25,
                          filterSyncByPhotons=filter_on, mockSample="uniform",
                          **info_kwargs)
    return TimeTaggerFacade(TimeTaggerManager(info, nidaqManager=_Nidaq()))


# --------------------------------------------------------------------------- #
# Construction                                                                 #
# --------------------------------------------------------------------------- #


def test_build_facade_from_master_attaches_the_card_when_the_setup_has_one():
    info = TimeTaggerInfo(simulation=True)
    master = SimpleNamespace(timeTaggerManager=TimeTaggerManager(info),
                             detectorsManager={}, lasersManager={}, positionersManager={})
    facade = build_facade_from_master(master)
    assert isinstance(facade, MicroscopeFacade)
    assert isinstance(facade.time_tagger, TimeTaggerFacade)
    assert facade.time_tagger.is_mock
    assert facade.time_tagger.roles() == ["photons", "laser_sync", "line_clock"]

    none = build_facade_from_master(SimpleNamespace(timeTaggerManager=None))
    assert none.time_tagger is None


def test_channels_report_the_block():
    tt = _facade()
    report = tt.channels()["photons"]
    assert (report.channel, report.edge, report.trigger_v) == (-1, "falling", -0.25)
    assert tt.metadata()["roles"]["photons"]["channel"] == -1
    assert tt.tcspc_direction == "forward"


# --------------------------------------------------------------------------- #
# Measurements                                                                 #
# --------------------------------------------------------------------------- #


def test_count_rates_and_test_signal():
    tt = _facade()
    rates = tt.count_rates(duration_s=0.01)
    assert rates.rates_hz["photons"] == pytest.approx(500e3 * 1.02 + 2000, rel=0.05)
    assert rates.rates_hz["laser_sync"] == pytest.approx(80e6, rel=0.01)
    assert "MHz" in rates.summary()
    tt.test_signal(["line_clock"], True)
    assert tt.count_rates(["line_clock"], 0.01).rates_hz["line_clock"] == TEST_SIGNAL_RATE_HZ
    tt.test_signal(["line_clock"], False)
    assert tt.count_rates(["line_clock"], 0.01).rates_hz["line_clock"] == 0.0


def test_dark_rates_see_only_the_dark_counts_with_the_mock_laser_off():
    tt = _facade()
    tt.set_mock_laser(False)
    dark = tt.dark_rates(duration_s=0.01)
    assert dark.rates_hz["photons"] == pytest.approx(2000.0, rel=0.05)
    tt.set_mock_laser(True)
    assert tt.count_rates(["photons"], 0.01).rates_hz["photons"] > 100e3


def test_rep_rate_uses_the_divider_and_restores_the_card():
    tt = _facade(filter_on=True)
    card = tt.manager.tagger
    assert card.filterOn
    rep = tt.rep_rate(duration_s=0.01, divider=16)
    assert rep.rate_hz == pytest.approx(80e6, rel=0.01)
    assert rep.period_ps == pytest.approx(12500.0, rel=0.01)
    assert rep.divider == 16 and rep.filter_was_on
    assert rep.jitter_ps > 0
    assert "laser_rep_rate_mhz = 80.0" in rep.summary()
    # Restored: the divider is gone and the filter is back on.
    assert card.getEventDivider(2) == 1
    assert card.filterOn
    assert tt.manager.calibrationOwner is None


def test_rep_rate_is_refused_while_a_scan_holds_the_card():
    tt = _facade()
    tt.manager.beginScanHold("FLIM")
    with pytest.raises(TimeTaggerBusyError, match="while a scan holds"):
        tt.rep_rate(duration_s=0.01)
    tt.manager.endScanHold("FLIM")


def test_rep_rate_with_no_sync_names_the_cable():
    tt = _facade(laserSyncTriggerV=-0.5)  # wrong polarity for a +0.8 V sync
    with pytest.raises(TimeTaggerError, match="No edges on laser_sync"):
        tt.rep_rate(duration_s=0.01)


def test_histogram_reads_forward_time_in_both_directions():
    # Enough photons that argmax does not wander along the slow decay's top.
    forward = _facade().histogram(duration_s=0.3)
    reverse = _facade(filter_on=True).histogram(duration_s=0.3)
    assert forward.direction == "forward" and reverse.direction == "reverse"
    assert forward.t_axis_ns.size == 391 == reverse.t_axis_ns.size
    assert forward.peak_ns == pytest.approx(1.3, abs=0.3)
    assert reverse.peak_ns == pytest.approx(1.3, abs=0.3)
    # The FWHM of a 2.5 ns decay through a 350 ps IRF: tau ln 2 plus a bit.
    assert 1.5 < forward.fwhm_ns < 3.0
    assert forward.total > 1000
    assert "peak at" in forward.summary()
    assert reverse.click_role == "photons" and reverse.start_role == "laser_sync"


def test_trigger_sweep_finds_the_plateau_and_restores_the_level():
    tt = _facade()
    levels = np.linspace(-0.7, 0.0, 15)
    sweep = tt.trigger_sweep("photons", levels, duration_s=0.001)
    assert sweep.plateau_v is not None
    assert -0.45 < sweep.plateau_v < -0.1, "between the noise floor and the -0.5 V pulse"
    assert tt.channels()["photons"].trigger_v == -0.25
    assert tt.manager.calibrationOwner is None
    positive = tt.trigger_sweep("photons", [0.1, 0.3], duration_s=0.001)
    assert positive.plateau_v is None, "the wrong polarity counts nothing"
    assert "no plateau" in positive.summary()


# --------------------------------------------------------------------------- #
# Preflight                                                                    #
# --------------------------------------------------------------------------- #


def _detector(card, **props):
    base = {"laser_rep_rate_mhz": 80.0}
    base.update(props)
    return SwabianTimeTaggerManager(_DetectorInfo(base), "FLIM", _Nidaq(),
                                    timeTaggerManager=card)


def test_preflight_is_green_on_a_well_configured_mock():
    tt = _facade()
    det = _detector(tt.manager, background_rate_hz=12_000.0)
    report = run_device_preflight(tt, det, duration_s=0.01)
    assert isinstance(report, PreflightReport)
    assert report.ok, report.summary()
    names = [item.name for item in report.items]
    assert "laser sync rate matches laser_rep_rate_mhz" in names
    assert "TCSPC window spans the laser period" in names
    assert report.summary().startswith("FLIM pre-flight: green")
    skipped = [item for item in report.items if item.status == "skipped"]
    assert len(skipped) == 3, "the scan-aware checks are reported, not forgotten"


def test_preflight_flags_a_wrong_rep_rate_and_a_short_window():
    tt = _facade()
    det = _detector(tt.manager, laser_rep_rate_mhz=40.0, n_bins=64)
    report = run_device_preflight(tt, det, duration_s=0.01)
    by_name = {item.name: item for item in report.items}
    assert by_name["laser sync rate matches laser_rep_rate_mhz"].status == "fail"
    assert "set laser_rep_rate_mhz = 80.0" in by_name["laser sync rate matches laser_rep_rate_mhz"].hint
    assert by_name["TCSPC window spans the laser period"].status == "warn"
    assert not report.ok


def test_preflight_fails_a_short_window_in_reverse_mode_and_counts_overflows():
    tt = _facade(filter_on=True, mockFaults={"model": "Time Tagger 20"})
    det = _detector(tt.manager, n_bins=64)
    report = run_device_preflight(tt, det, duration_s=0.01)
    by_name = {item.name: item for item in report.items}
    assert by_name["TCSPC window spans the laser period"].status == "fail"
    assert by_name["no USB overflows"].status == "ok", "the filter keeps a TT20 in budget"

    unfiltered = _facade(mockFaults={"model": "Time Tagger 20"})
    report = run_device_preflight(unfiltered, _detector(unfiltered.manager), duration_s=0.01)
    assert {i.name: i for i in report.items}["no USB overflows"].status == "fail"


def test_preflight_reports_a_missing_card():
    class _NoLibrary:
        @staticmethod
        def createTimeTagger(*_a):
            raise RuntimeError("no card")

    tt = TimeTaggerFacade(TimeTaggerManager(TimeTaggerInfo(), api=_NoLibrary))
    report = run_device_preflight(tt, None, duration_s=0.01)
    assert not report.ok and report.items[0].status == "fail"
