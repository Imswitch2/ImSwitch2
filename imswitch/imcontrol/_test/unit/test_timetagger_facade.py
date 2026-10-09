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


# --------------------------------------------------------------------------- #
# Review round 2: ownership of the test signal, bounded waits, restores        #
# --------------------------------------------------------------------------- #


def test_test_signal_on_owns_the_card_and_is_refused_during_a_scan():
    tt = _facade()
    card = tt.manager.tagger
    with tt.test_signal_on(["line_clock"]):
        assert tt.manager.calibrationOwner == "test_signal"
        assert tt.count_rates(["line_clock"], 0.01).rates_hz["line_clock"] == TEST_SIGNAL_RATE_HZ
        with pytest.raises(TimeTaggerBusyError, match="held by a calibration"):
            tt.manager.beginScanHold("FLIM")
    assert not card.getTestSignal(3) and tt.manager.calibrationOwner is None

    tt.manager.beginScanHold("FLIM")
    with pytest.raises(TimeTaggerBusyError):
        with tt.test_signal_on(["line_clock"]):
            pass
    with pytest.raises(TimeTaggerBusyError, match="test signal"):
        tt.test_signal(["line_clock"], True)
    assert not card.getTestSignal(3), "no fake pixel markers into the scan"
    tt.manager.endScanHold("FLIM")


class _StalledMeasurement:
    """A measurement whose capture never finishes (the vendor's default
    ``waitUntilFinished`` would block forever)."""

    def __init__(self):
        self.stopped = False
        self.waits = 0

    def startFor(self, duration_ps):
        pass

    def waitUntilFinished(self, timeout=-1):
        self.waits += 1
        assert timeout > 0, "an unbounded wait would block Stop"
        return False

    def getData(self):
        raise AssertionError("never finished")

    def stop(self):
        self.stopped = True


def test_a_stalled_measurement_times_out_instead_of_blocking():
    tt = _facade()
    tt.FINISH_MARGIN_S = 0.05
    stalled = _StalledMeasurement()
    with pytest.raises(TimeTaggerError, match="did not finish"):
        tt._run(stalled, 0.001)
    assert stalled.stopped and stalled.waits >= 1


def test_stop_reaches_a_script_waiting_on_a_measurement():
    from imswitch.imcommon.model.cancellation import (
        CancelToken,
        OperationCancelled,
        clearCurrentCancelToken,
        setCurrentCancelToken,
    )

    tt = _facade()
    tt.FINISH_MARGIN_S = 10.0
    token = CancelToken()
    setCurrentCancelToken(token)
    try:
        stalled = _StalledMeasurement()
        token.requestStop()
        with pytest.raises(OperationCancelled):
            tt._run(stalled, 0.001)
    finally:
        clearCurrentCancelToken()
    assert stalled.stopped


def test_rep_rate_restores_the_divider_and_filter_it_found():
    tt = _facade(filter_on=True)
    card = tt.manager.tagger
    card.setEventDivider(2, 4)
    rep = tt.rep_rate(duration_s=0.01, divider=16)
    assert rep.rate_hz == pytest.approx(80e6, rel=0.01)
    assert card.getEventDivider(2) == 4, "theirs, put back"
    assert card.filterOn

    card.setConditionalFilter([], [])  # someone took the filter off
    rep = tt.rep_rate(duration_s=0.01)
    assert not rep.filter_was_on
    assert not card.filterOn, "and it stays off"


def test_histogram_measures_at_the_configured_delay_and_puts_the_cards_back():
    tt = _facade()
    card = tt.manager.tagger
    card.setInputDelay(-1, -1300)  # what a forward scan with t0_ps=1300 leaves
    hist = tt.histogram(duration_s=0.3)
    assert hist.photon_delay_ps == 0
    assert hist.peak_ns == pytest.approx(1.3, abs=0.3), "absolute, not shifted by the leftover"
    assert card.getInputDelay(-1) == -1300, "restored"
    assert "photon_delay_ps" in hist.to_dict()
    # Running it twice gives the same t0: nothing accumulates.
    again = tt.histogram(duration_s=0.3)
    assert again.peak_ns == pytest.approx(hist.peak_ns, abs=0.3)

    tt.manager.beginScanHold("FLIM")
    with pytest.raises(TimeTaggerBusyError):
        tt.histogram(duration_s=0.01)
    tt.manager.endScanHold("FLIM")


def test_the_mock_card_does_not_retain_finished_measurements():
    import gc

    tt = _facade()
    card = tt.manager.tagger
    for _ in range(5):
        tt.histogram(duration_s=0.001)
    gc.collect()
    assert len(card.measurements) == 0


# --------------------------------------------------------------------------- #
# Scan clocks: the measurements tutorials 07 to 09 make                        #
# --------------------------------------------------------------------------- #

SAMPLE_RATE = 100_000
NX, NY, DWELL, FLYBACK = 8, 4, 10, 50


class _ScanSignal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class _ScanNidaq:
    isSimulated = True

    def __init__(self):
        self.sigScanBuilt = _ScanSignal()
        self.sigScanStarted = _ScanSignal()
        self.sigScanDone = _ScanSignal()


def _scan_facade(**info_kwargs):
    nidaq = _ScanNidaq()
    info = TimeTaggerInfo(simulation=True, photonsChannel=-1, photonsTriggerV=-0.25,
                          frameClockChannel=4, mockSample="uniform", **info_kwargs)
    manager = TimeTaggerManager(info, SimpleNamespace(scan=SimpleNamespace(sampleRate=SAMPLE_RATE)),
                                nidaq)
    line = np.zeros(NX * DWELL + FLYBACK, dtype=bool)
    line[:10] = True
    frame = np.zeros(line.size * NY, dtype=bool)
    frame[:10] = True
    ttl = {"line_clock": np.tile(line, NY), "frame_start_clock": frame}
    nidaq.sigScanBuilt.emit({}, {"TTLCycleSignalsDict": ttl}, [])
    return TimeTaggerFacade(manager), nidaq


def test_count_edges_spans_the_scan_when_started_from_inside_the_counter():
    tt, nidaq = _scan_facade()
    scan_s = (NX * DWELL + FLYBACK) * NY / SAMPLE_RATE
    # Started before the scan (start=): every line, line 0 included.
    n = tt.count_edges("line_clock", duration_s=scan_s + 0.05,
                       start=lambda: nidaq.sigScanStarted.emit())
    assert n == NY
    # A counter started after the scan began misses the one frame edge at
    # t=0 -- the reason for start=.
    assert tt.count_edges("frame_clock", duration_s=0.02) == 0
    assert tt.count_edges("frame_clock", duration_s=0.02,
                          start=lambda: nidaq.sigScanStarted.emit()) == 1
    # After scan-done the clocks are silent, whatever the edges loaded.
    nidaq.sigScanDone.emit()
    assert tt.count_rates(["line_clock"], 0.01).rates_hz["line_clock"] == 0.0


def test_period_of_the_line_clock_is_dwell_times_nx_plus_the_flyback():
    tt, nidaq = _scan_facade()
    nidaq.sigScanStarted.emit()
    line_ps = (NX * DWELL + FLYBACK) / SAMPLE_RATE * 1e12
    period = tt.period("line_clock", duration_s=0.01, expected_period_ps=line_ps)
    assert period.n_periods == NY - 1
    assert period.period_ps == pytest.approx(line_ps, rel=1e-3)
    assert period.jitter_ps < 100
    assert "periods" in period.summary()
    # Without a design number the rate is counted first.
    nidaq2 = _scan_facade()[1]
    assert nidaq2 is not nidaq
    silent = tt.period("sted_pulse" if False else "laser_sync", duration_s=0.001,
                       expected_period_ps=12_500)
    assert silent.n_periods > 0


def test_skew_is_signed_and_the_delay_is_restored():
    tt, nidaq = _scan_facade()
    card = tt.manager.tagger
    line_ps = int((NX * DWELL + FLYBACK) / SAMPLE_RATE * 1e12)
    lines = np.arange(NY) * line_ps
    # The frame edge lands 5 ns AFTER the first line edge: a negative skew a
    # plain histogram could never see.
    card.load_scan_edges({3: lines, 4: np.array([5_000])}, 1.0)
    before = card.getInputDelay(3)
    skew = tt.skew("frame_clock", "line_clock", duration_s=0.01, added_delay_ps=50_000)
    assert skew.skew_ps == pytest.approx(-5_000, abs=200)
    assert skew.n_pairs == 1 and skew.added_delay_ps == 50_000
    assert skew.pattern_offset_ps == 10_000
    assert skew.frame_leads_pixel_0, "-5 ns + the 10 ns offset: the frame still leads"
    assert card.getInputDelay(3) == before
    assert tt.manager.calibrationOwner is None
    assert "-5000 ps" in skew.summary() or "-5,000" in skew.summary() or "-4" in skew.summary()

    card.load_scan_edges({3: lines, 4: np.array([20_000])}, 1.0)
    late = tt.skew("frame_clock", "line_clock", duration_s=0.01)
    assert late.skew_ps == pytest.approx(-20_000, abs=200)
    assert not late.frame_leads_pixel_0

    tt.manager.beginScanHold("FLIM")
    with pytest.raises(TimeTaggerBusyError):
        tt.skew(duration_s=0.001)
    tt.manager.endScanHold("FLIM")


def test_scope_lists_the_edges_after_the_trigger():
    tt, nidaq = _scan_facade()
    line_ps = (NX * DWELL + FLYBACK) / SAMPLE_RATE * 1e12
    trace = tt.scope(["line_clock", "laser_sync"], trigger_role="frame_clock",
                     window_ps=int(line_ps * 1.5), duration_s=0.01)
    rising = [t for t, state in trace["line_clock"] if state == "rising"]
    assert rising == [0, int(line_ps)]
    falling = [t for t, state in trace["line_clock"] if state == "falling"]
    assert falling[0] == 1_000_000, "a 1 us clock pulse"
    sync = [t for t, state in trace["laser_sync"] if state == "rising"]
    assert len(sync) == 1000, "capped at n_max_events, like the card"
    short = tt.scope(["laser_sync"], trigger_role="frame_clock", window_ps=125_000,
                     duration_s=0.001)
    assert len([t for t, s in short["laser_sync"] if s == "rising"]) == 10


def test_mock_truth_and_faults_exist_on_the_mock_only():
    tt, _ = _scan_facade()
    rates, lifetimes = tt.mock_truth(4, 8)
    assert rates.shape == (4, 8) == lifetimes.shape
    tt.set_mock_fault("line_delay_ps", 1234)
    assert tt.manager.tagger._model.faults["line_delay_ps"] == 1234
    tt.set_mock_fault("line_delay_ps", None)
    assert "line_delay_ps" not in tt.manager.tagger._model.faults
    assert tt.pattern_offset_ps("line_clock") == 10_000


def test_sted_pulse_delay_needs_the_role_and_peaks_after_the_excitation():
    with pytest.raises(TimeTaggerError, match="sted_pulse"):
        _facade().sted_pulse_delay(duration_s=0.01)
    tt = _facade(stedPulseChannel=5)
    assert "sted_pulse" in tt.roles()
    sted = tt.sted_pulse_delay(duration_s=0.05)
    model = tt.manager.tagger._model
    # The mock's STED pulse sits sted_delay_ps after the excitation (t0).
    assert sted.peak_ns == pytest.approx((model.t0_ps + model.sted_delay_ps) / 1000.0, abs=0.05)
    assert sted.total > 1000
    assert sted.click_role == "sted_pulse" and sted.direction == "forward"


def test_scope_prepares_the_scan_before_the_capture_and_starts_it_after():
    tt, nidaq = _scan_facade()
    order = []
    real_scope = tt.manager.api.Scope

    class _RecordingScope(real_scope):
        def __init__(self, *args, **kwargs):
            order.append('scope built')
            super().__init__(*args, **kwargs)

    tt.manager.api.Scope = _RecordingScope
    try:
        tt.scope(['line_clock'], trigger_role='frame_clock', window_ps=10_000_000, duration_s=0.001,
                 prepare=lambda: order.append('prepared'),
                 start=lambda: (order.append('started'), nidaq.sigScanStarted.emit()))
    finally:
        tt.manager.api.Scope = real_scope
    assert order == ['prepared', 'scope built', 'started']
