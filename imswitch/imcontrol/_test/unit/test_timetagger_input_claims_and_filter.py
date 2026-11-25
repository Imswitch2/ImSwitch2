"""Which detectors may share a Time Tagger input, and the conditional
filter's trigger list derived from use.

Rejected: two FLIM detectors on one input (each sets its own t0 as that
input's delay and they would fit the same photons twice) -- at startup,
naming both detectors and the input, and on a run-time switch of
``click_role``. Allowed: a FLIM and a counting detector on one input (two
independent readers of one tag stream), two counting detectors on one input.

The filter (``filterSyncByPhotons``) triggers only on the inputs FLIM
detectors image: between scans every FLIM detector's input, during a scan
the participating FLIM detectors' inputs (set when the clocks start, put
back when the session ends, both on the card's I/O thread). Counting inputs
never trigger it; a card with no FLIM detector gets no filter write; a
legacy single-FLIM setup gets exactly its one write at connect (the golden
test pins it call for call).
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.SetupInfo import TimeTaggerInfo, TimeTaggerInputInfo
from imswitch.imcontrol.model.interfaces import timetagger_mock as tm
from imswitch.imcontrol.model.managers.TimeTaggerManager import TimeTaggerManager
from imswitch.imcontrol.model.workflows.time_tagger_facade import TimeTaggerFacade
from imswitch.imcontrol._test.unit.test_timetagger_multi_detector import (
    APDS,
    _Info,
    _Nidaq,
    _prepare,
    _residual,
    _run_sync,
    _truth,
)
from imswitch.imcontrol.model.managers.detectors import SwabianTimeTaggerManager as stm

pytestmark = pytest.mark.nohardware

SAMPLE_RATE = 100_000


class _LoggingApi(tm.MockTimeTaggerApi):
    """A mock card that records its conditional-filter writes."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.filter_writes = []

    def createTimeTagger(self, serial=None):
        tagger = super().createTimeTagger(serial)
        original = tagger.setConditionalFilter

        def logged(trigger, filtered):
            self.filter_writes.append((list(trigger), list(filtered)))
            return original(trigger, filtered)

        tagger.setConditionalFilter = logged
        return tagger


def _card(apds=("apd_a", "apd_b"), *, filtered=True, sync=True):
    inputs = {n: TimeTaggerInputInfo(channel=APDS[n], triggerV=-0.25) for n in apds}
    if sync:
        inputs["laser_sync"] = TimeTaggerInputInfo(channel=2, kind="laser_sync")
    inputs["line_clock"] = TimeTaggerInputInfo(channel=3, kind="line_clock")
    info = TimeTaggerInfo(simulation=True, inputs=inputs, mockSample="uniform",
                          filterSyncByPhotons=filtered)
    api = _LoggingApi(model=tm.SignalModel.from_info(info))
    nidaq = _Nidaq()
    card = TimeTaggerManager(info, SimpleNamespace(scan=SimpleNamespace(sampleRate=SAMPLE_RATE)),
                             nidaq, api=api)
    return nidaq, card, api


def _det(nidaq, card, name, photon_input, measurement, **props):
    p = {"photon_input": photon_input, "measurement": measurement,
         "laser_rep_rate_mhz": 80.0, "min_counts_per_pixel": 5, "live_fit_period_s": 0.0}
    p.update(props)
    return stm.SwabianTimeTaggerManager(_Info(p), name, nidaq, timeTaggerManager=card)


def _settle(card):
    assert card.waitForFilter()
    card.submit(lambda: None).result(timeout=5)


# --------------------------------------------------------------------------- #
# Who may share an input                                                       #
# --------------------------------------------------------------------------- #


def test_two_flim_detectors_on_one_input_are_rejected_at_startup():
    nidaq, card, _api = _card()
    _det(nidaq, card, "FLIM_1", "apd_a", "flim")
    with pytest.raises(ValueError) as error:
        _det(nidaq, card, "FLIM_2", "apd_a", "flim")
    text = str(error.value)
    assert "'FLIM_1' and 'FLIM_2' both image Time Tagger input 'apd_a'" in text
    assert "its own detector input" in text and 'measurement "counting"' in text
    assert card.detectorInputs() == {"FLIM_1": ("apd_a", "flim")}


def test_a_role_and_its_input_name_are_one_input():
    nidaq, card, _api = _card(apds=("apd_a",))      # photons resolves to apd_a
    _det(nidaq, card, "FLIM_1", "apd_a", "flim")
    with pytest.raises(ValueError, match="both image Time Tagger input 'apd_a'"):
        _det(nidaq, card, "FLIM_2", "photons", "flim")


def test_a_legacy_block_rejects_two_flim_detectors_on_photons():
    nidaq = _Nidaq()
    info = TimeTaggerInfo(simulation=True, photonsChannel=-1, photonsTriggerV=-0.25)
    card = TimeTaggerManager(info, nidaqManager=nidaq, api=tm.MockTimeTaggerApi(
        model=tm.SignalModel.from_info(info)))
    stm.SwabianTimeTaggerManager(_Info({}), "FLIM", nidaq, timeTaggerManager=card)
    with pytest.raises(ValueError, match="'FLIM' and 'FLIM2' both image Time Tagger input 'photons'"):
        stm.SwabianTimeTaggerManager(_Info({}), "FLIM2", nidaq, timeTaggerManager=card)


@pytest.mark.parametrize("first, second", [("flim", "counting"), ("counting", "flim"),
                                           ("counting", "counting")])
def test_flim_and_counting_or_two_counting_may_share_an_input(first, second):
    nidaq, card, _api = _card(filtered=False)
    a = _det(nidaq, card, "A", "apd_a", first, t0_ps=700)
    b = _det(nidaq, card, "B", "apd_a", second, t0_ps=700)
    _prepare(nidaq, [a, b])
    if "flim" in (first, second):
        # The FLIM detector's t0 is on the shared input during the scan.
        assert card.tagger.getInputDelay(-1) == -700
    for det in (a, b):
        frames, acks = _run_sync(det)
        assert acks == [det.name]
    assert card.tagger.getInputDelay(-1) == 0, "t0 put back after the scan"
    assert not card.scanHeld
    # Both image the same input: the same sample (and the counts are the
    # FLIM intensity, to the Poisson noise of the mock's two draws).
    for det in (a, b):
        image = det._image_intensity[0] if det.measurement == "flim" else det.drainChunk().raw[0]
        assert _residual(image, _truth(card, "apd_a")[0]) < 0.05


def test_a_run_time_switch_onto_another_flim_detectors_input_is_refused():
    nidaq, card, _api = _card()
    _det(nidaq, card, "FLIM_a", "apd_a", "flim")
    b = _det(nidaq, card, "FLIM_b", "apd_b", "flim")
    b.setParameter("click_role", "apd_a")
    assert b.parameters["click_role"].value == "apd_b"
    assert b.photonInput == "apd_b" and b._click_ch == -4
    assert card.detectorInputs()["FLIM_b"] == ("apd_b", "flim")


def test_a_finalized_detector_frees_its_input():
    nidaq, card, _api = _card()
    first = _det(nidaq, card, "FLIM_1", "apd_a", "flim")
    first.finalize()
    _det(nidaq, card, "FLIM_2", "apd_a", "flim")
    assert card.detectorInputs() == {"FLIM_2": ("apd_a", "flim")}


# --------------------------------------------------------------------------- #
# The conditional filter, derived from use                                     #
# --------------------------------------------------------------------------- #


def test_two_flim_detectors_trigger_on_their_inputs_and_a_scan_writes_nothing():
    nidaq, card, api = _card()
    dets = [_det(nidaq, card, f"FLIM_{n}", n, "flim") for n in ("apd_a", "apd_b")]
    _settle(card)
    assert card.tagger.getConditionalFilterTrigger() == [-1, -4]
    writes = len(api.filter_writes)
    _prepare(nidaq, dets)
    nidaq.sigScanStarted.emit()
    for det in dets:
        det._teardownScanThread(timeoutMs=2000)
        _run_sync(det)
    _settle(card)
    assert len(api.filter_writes) == writes, "both take part: the list is already right"
    assert card.metadata()["filter_channels"] == [[-1, -4], [2]]


def test_flim_and_counting_only_the_flim_input_triggers():
    nidaq, card, api = _card()
    flim = _det(nidaq, card, "FLIM", "apd_a", "flim")
    count = _det(nidaq, card, "COUNT", "apd_b", "counting")
    _settle(card)
    assert card.filterChannels() == ([-1], [2])
    assert card.tagger.getConditionalFilterTrigger() == [-1]
    _prepare(nidaq, [flim, count])
    nidaq.sigScanStarted.emit()
    for det in (flim, count):
        det._teardownScanThread(timeoutMs=2000)
        _run_sync(det)
    _settle(card)
    assert all(-4 not in trigger for trigger, _f in api.filter_writes), \
        "a counting input never triggers the filter"


def test_counting_only_writes_no_filter():
    nidaq, card, api = _card()
    dets = [_det(nidaq, card, f"C_{n}", n, "counting") for n in ("apd_a", "apd_b")]
    _prepare(nidaq, dets)
    nidaq.sigScanStarted.emit()
    for det in dets:
        det._teardownScanThread(timeoutMs=2000)
        _run_sync(det)
    _settle(card)
    assert api.filter_writes == []
    assert card.tagger.getConditionalFilterTrigger() == []
    assert card.metadata()["filter_channels"] is None


def test_a_scan_with_some_flim_detectors_narrows_the_list_then_puts_it_back():
    nidaq, card, api = _card()
    a = _det(nidaq, card, "FLIM_a", "apd_a", "flim")
    _det(nidaq, card, "FLIM_b", "apd_b", "flim")
    _settle(card)
    before = len(api.filter_writes)
    nidaq.sigScanBuilt.emit({**_scan(["FLIM_a"])}, {"TTLCycleSignalsDict": _ttl()}, [])
    assert api.filter_writes[before:] == [], "nothing while the scan is prepared"
    # The card's own slot (what this adds on the GUI thread at scan start):
    # it queues the write and returns.
    started = time.monotonic()
    card._onScanStarted()
    stall = time.monotonic() - started
    _settle(card)
    assert api.filter_writes[before:] == [([-1], [2])], "narrowed when the clocks start"
    a._teardownScanThread(timeoutMs=2000)
    _run_sync(a)
    _settle(card)
    assert api.filter_writes[before:] == [([-1], [2]), ([-1, -4], [2])], "put back at the end"
    assert card.tagger.getConditionalFilterTrigger() == [-1, -4]
    assert stall < 0.05, stall


def test_rep_rate_after_a_narrowed_scan_restores_every_flim_input():
    nidaq, card, api = _card()
    a = _det(nidaq, card, "FLIM_a", "apd_a", "flim")
    _det(nidaq, card, "FLIM_b", "apd_b", "flim")
    _settle(card)
    # A slow I/O thread: the session's restore is still queued when the
    # calibration starts; the calibration must wait for it, not undo it.
    card.submit(time.sleep, 0.3)
    nidaq.sigScanBuilt.emit(_scan(["FLIM_a"]), {"TTLCycleSignalsDict": _ttl()}, [])
    nidaq.sigScanStarted.emit()
    a._teardownScanThread(timeoutMs=2000)
    _run_sync(a)
    rep = TimeTaggerFacade(card).rep_rate(duration_s=0.01)
    assert rep.filter_was_on
    _settle(card)
    assert card.tagger.getConditionalFilterTrigger() == [-1, -4]


def test_filter_switched_off_in_the_setup_is_never_written():
    nidaq, card, api = _card(filtered=False)
    a = _det(nidaq, card, "FLIM_a", "apd_a", "flim")
    _det(nidaq, card, "C_b", "apd_b", "counting")
    _prepare(nidaq, [a])
    nidaq.sigScanStarted.emit()
    a._teardownScanThread(timeoutMs=2000)
    _run_sync(a)
    _settle(card)
    assert api.filter_writes == []
    assert card.tcspcDirection == "forward"


def test_a_legacy_card_with_a_counting_detector_keeps_its_connect_filter():
    nidaq = _Nidaq()
    info = TimeTaggerInfo(simulation=True, photonsChannel=-1, photonsTriggerV=-0.25,
                          filterSyncByPhotons=True)
    api = _LoggingApi(model=tm.SignalModel.from_info(info))
    card = TimeTaggerManager(info, nidaqManager=nidaq, api=api)
    stm.SwabianTimeTaggerManager(_Info({"measurement": "counting"}), "COUNT", nidaq,
                                 timeTaggerManager=card)
    _settle(card)
    assert api.filter_writes == [([-1], [2])], "the connect write, and nothing else"


def test_a_refused_filter_write_is_retried_before_the_next_scan():
    nidaq, card, api = _card()
    real = card.tagger.setConditionalFilter
    calls = []

    def refuse(trigger, filtered):
        calls.append(list(trigger))
        if len(calls) == 1:
            raise RuntimeError("refused")
        return real(trigger, filtered)
    card.tagger.setConditionalFilter = refuse
    _det(nidaq, card, "FLIM_a", "apd_a", "flim")
    _settle(card)
    assert card.conditioningError is not None
    card.joinScanSession("X")      # retries the conditioning first
    assert card.conditioned
    assert card.tagger.getConditionalFilterTrigger() == [-1]
    card.leaveScanSession("X")


def _ttl():
    from imswitch.imcontrol._test.unit.test_timetagger_multi_detector import _ttl as ttl
    return ttl()


def _scan(names):
    from imswitch.imcontrol._test.unit.test_timetagger_multi_detector import _scan_info
    return _scan_info(names)
