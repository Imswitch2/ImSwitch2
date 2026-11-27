"""Several detectors on one Time Tagger card, on the mock.

Two and four counting detectors in one scan, FLIM on two inputs against one
sync, a FLIM and a counting detector together: each detector images its own
input (the mock gives every detector input a sample of its own, so a
cross-wired image is caught), every one acknowledges its own final frame,
the pixel markers exist once per card per scan, and the card stays held
until the last detector of the scan has drained. Abort, stop and a slow
detector run on real threads; the rest drive the worker synchronously.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np
import pytest
from qtpy import QtCore

from imswitch.imcontrol.model.SetupInfo import TimeTaggerInfo, TimeTaggerInputInfo
from imswitch.imcontrol.model.interfaces import timetagger_mock as tm
from imswitch.imcontrol.model.managers._scan_execution import PARTICIPANTS_KEY
from imswitch.imcontrol.model.managers.TimeTaggerManager import TimeTaggerError, TimeTaggerManager
from imswitch.imcontrol.model.managers.detectors import SwabianTimeTaggerManager as stm

pytestmark = pytest.mark.nohardware

SAMPLE_RATE, DWELL, FLYBACK, NX, NY = 100_000, 10, 50, 16, 12
GUI_BOUND_S = 0.15

#: Detector inputs of the rigs below: falling-edge NIM, as a SPAD.
APDS = {"apd_a": -1, "apd_b": -4, "apd_c": -5, "apd_d": -6}


class _Sig:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class _Nidaq:
    isSimulated = True

    def __init__(self):
        self.sigScanBuilt, self.sigScanStarted, self.sigScanDone = _Sig(), _Sig(), _Sig()
        self.sigScanStopped = _Sig()
        self.failures = []

    def reportScanBuildFailure(self, stage, error):
        self.failures.append((stage, error))


class _Info:
    forAcquisition = True
    forFocusLock = False

    def __init__(self, props):
        self.managerProperties = dict(props)


def _ttl(ny=NY, nx=NX):
    line = np.zeros(nx * DWELL + FLYBACK, dtype=bool)
    line[:10] = True
    frame = np.zeros(line.size * ny, dtype=bool)
    frame[:10] = True
    return {"line_clock": np.tile(line, ny), "frame_start_clock": frame}


def _scan_info(names):
    return {PARTICIPANTS_KEY: list(names), "img_dims": [NX, NY], "img_axes_phys": ["x", "y"],
            "pixel_sizes": [1.0, 1.0], "dwell_time": DWELL / SAMPLE_RATE,
            "tot_scan_time_s": 1.0}


def _card(apds, *, sync=False, frame=False, sample="uniform", api_class=tm.MockTimeTaggerApi,
          **info_kwargs):
    inputs = {name: TimeTaggerInputInfo(channel=APDS[name], triggerV=-0.25) for name in apds}
    if sync:
        inputs["laser_sync"] = TimeTaggerInputInfo(channel=2, kind="laser_sync")
    inputs["line_clock"] = TimeTaggerInputInfo(channel=3, kind="line_clock")
    if frame:
        inputs["frame_clock"] = TimeTaggerInputInfo(channel=7, kind="frame_clock")
    info = TimeTaggerInfo(simulation=True, inputs=inputs, mockSample=sample, **info_kwargs)
    model = tm.SignalModel.from_info(info)
    model.irf_fwhm_ps = 20.0
    nidaq = _Nidaq()
    card = TimeTaggerManager(info, SimpleNamespace(scan=SimpleNamespace(sampleRate=SAMPLE_RATE)),
                             nidaq, api=api_class(model=model))
    return nidaq, card


def _detector(nidaq, card, name, photon_input, measurement="counting", **props):
    p = {"photon_input": photon_input, "measurement": measurement,
         "laser_rep_rate_mhz": 80.0, "min_counts_per_pixel": 5, "live_fit_period_s": 0.0}
    p.update(props)
    return stm.SwabianTimeTaggerManager(_Info(p), name, nidaq, timeTaggerManager=card)


def _rig(apds, measurement="counting", **card_kwargs):
    nidaq, card = _card(apds, **card_kwargs)
    dets = [_detector(nidaq, card, f"DET_{name}", name, measurement) for name in apds]
    return nidaq, card, dets


def _prepare(nidaq, dets):
    nidaq.sigScanBuilt.emit(_scan_info([d.name for d in dets]),
                            {"TTLCycleSignalsDict": _ttl()}, [])
    assert nidaq.failures == []


def _run_sync(det):
    """The detector's scan worker, run to its final frame on this thread."""
    generation = det._preparedScanGeneration
    assert generation is not None
    det._activeScanGeneration = generation
    worker = (stm._TTCountWorker if det._scan.get("measurement") == "counting"
              else stm._TTFlimWorker)(det, generation)
    frames = []
    worker.sigFrameReady.connect(lambda *args: frames.append(args))
    worker.sigFrameReady.connect(det._on_frame_ready)
    worker.sigTerminated.connect(det._onScanWorkerFinished)
    det._scanWorker = worker
    acks = []
    worker.signal_done()
    det.finishScan("graceful", lambda: acks.append(det.name))
    worker.run()
    det._scanWorker = None
    return frames, acks


def _residual(image, truth):
    """How badly ``truth`` (scaled) explains ``image``: 0 = perfectly."""
    image = np.asarray(image, dtype=np.float64).ravel()
    truth = np.asarray(truth, dtype=np.float64).ravel()
    scale = float(image @ truth) / max(1e-12, float(truth @ truth))
    return float(np.sum((image - scale * truth) ** 2) / max(1e-12, float(image @ image)))


def _truth(card, name):
    rate, tau = card.tagger.sample_truth(NY, NX, channel=card.channel(name))
    return rate, tau


# --------------------------------------------------------------------------- #
# Counting detectors                                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("n", [2, 4])
def test_counting_detectors_image_their_own_inputs(n):
    apds = list(APDS)[:n]
    nidaq, card, dets = _rig(apds)
    _prepare(nidaq, dets)
    images = {}
    for det in dets:
        frames, acks = _run_sync(det)
        finals = [f for f in frames if f[2]]
        assert len(finals) == 1 and acks == [det.name]
        raw = det.drainChunk().raw
        assert raw.shape == (1, NY, NX)
        assert det.drainChunk().raw.size == 0, "the final frame is recorded once"
        images[det.photonInput] = raw[0]
        assert raw[0].sum() > 0

    # Cross-wiring: every image is explained best by its own input's truth.
    for name, image in images.items():
        residuals = {other: _residual(image, _truth(card, other)[0]) for other in apds}
        assert min(residuals, key=residuals.get) == name, residuals
        assert residuals[name] < 0.05, residuals
    # And they differ from each other: four inputs, four images.
    flat = [img.ravel() / img.sum() for img in images.values()]
    for i in range(len(flat)):
        for j in range(i + 1, len(flat)):
            assert np.abs(flat[i] - flat[j]).sum() > 0.1


def test_one_scan_builds_one_marker_pair_for_every_detector():
    nidaq, card, dets = _rig(list(APDS))
    before = set(card.tagger._generators)
    _prepare(nidaq, dets)
    made = set(card.tagger._generators) - before
    assert len(made) == 2, "the pixel markers exist once per card per scan"
    channels = {det.pixel_marker_channels()["pixel_begin"] for det in dets}
    assert len(channels) == 1
    session = card.scanSession
    assert set(session.participants) == {d.name for d in dets}
    for det in dets:
        counter = det._flim.counter
        assert (counter.begin_channel, counter.end_channel) == session.markers.channels
        assert counter.click_channel == card.channel(det.photonInput)


def test_the_card_is_held_until_the_last_detector_has_drained():
    nidaq, card, dets = _rig(["apd_a", "apd_b"])
    _prepare(nidaq, dets)
    assert card.scanHeld
    _run_sync(dets[0])
    assert card.scanHeld, "released while another detector of the scan still acquires"
    with pytest.raises(TimeTaggerError):
        card.setTriggerLevel("apd_a", -0.3)
    _run_sync(dets[1])
    assert not card.scanHeld
    assert card.scanSession.closed and card.scanSession.leftClean()
    card.setTriggerLevel("apd_a", -0.3)


def test_repeat_scans_reuse_markers_and_counters_and_stay_attributed():
    nidaq, card, dets = _rig(["apd_a", "apd_b"])
    first_images, counters = {}, {}
    for scan in range(3):
        _prepare(nidaq, dets)
        for det in dets:
            if scan:
                assert det._scan["flim_reused"] is True
                assert det._scan["pixel_markers_reused"] is True
                assert det._flim is counters[det.name]
            counters[det.name] = det._flim
            frames, acks = _run_sync(det)
            assert acks == [det.name]
            image = det.drainChunk().raw[0]
            if scan == 0:
                first_images[det.name] = image
            else:
                # A cleared counter: a fresh frame, not the last one again.
                assert not np.array_equal(image, first_images[det.name])
            residuals = {o: _residual(image, _truth(card, o)[0]) for o in ("apd_a", "apd_b")}
            assert min(residuals, key=residuals.get) == det.photonInput
    assert card.scanSession.number == 3


def test_a_detector_not_in_the_scan_does_not_join_its_session():
    nidaq, card, dets = _rig(["apd_a", "apd_b"])
    nidaq.sigScanBuilt.emit(_scan_info([dets[0].name]), {"TTLCycleSignalsDict": _ttl()}, [])
    assert card.scanSession.participants == (dets[0].name,)
    assert dets[1]._preparedScanGeneration is None
    _run_sync(dets[0])
    assert not card.scanHeld


def test_an_aborted_participant_means_new_markers_next_scan():
    nidaq, card, dets = _rig(["apd_a", "apd_b"])
    _prepare(nidaq, dets)
    markers = card.scanSession.markers
    _run_sync(dets[0])
    dets[1].finishScan("abort", lambda: None)
    assert not card.scanHeld
    assert not card.scanSession.leftClean()
    _prepare(nidaq, dets)
    assert card.scanSession.markers is not markers
    assert all(det._scan["flim_reused"] is False for det in dets)


def test_no_detector_joins_after_the_clocks_started():
    nidaq, card, dets = _rig(["apd_a", "apd_b"])
    session = card.joinScanSession("DET_apd_a")
    session.started = True
    with pytest.raises(TimeTaggerError, match="after its clocks started"):
        card.joinScanSession("DET_apd_b")
    assert card.scanHeld   # the first participant's hold stands
    card.leaveScanSession("DET_apd_a")
    assert not card.scanHeld


def test_a_late_leave_from_an_old_session_keeps_the_new_hold():
    nidaq, card, _dets = _rig(["apd_a"])
    old = card.joinScanSession("X")
    new = card.joinScanSession("X")          # joining again: a new scan
    assert new is not old and old.closed
    card.leaveScanSession("X", session=old)
    assert card.scanHeld, "a late leave dropped the new scan's hold"
    card.leaveScanSession("X", session=new)
    assert not card.scanHeld


def test_counting_needs_no_sync_and_flim_says_what_it_needs():
    nidaq, card = _card(["apd_a"], sync=False)
    counting = _detector(nidaq, card, "COUNT", "apd_a", "counting")
    assert counting.parameters["start_channel"].value == 0
    assert counting.timeResolvedCapabilities()["supports_lifetime_fit"] is False
    with pytest.raises(RuntimeError, match="no time axis"):
        counting.configureTimeResolvedProducts(stm.TimeResolvedScanConfig())
    # As before for a card without a sync: refused when the detector is built.
    with pytest.raises(ValueError, match='FLIM needs a laser sync; set measurement to "counting"'):
        _detector(nidaq, card, "FLIM", "apd_a", "flim")
    _prepare(nidaq, [counting])
    _run_sync(counting)
    assert counting.drainChunk().raw[0].sum() > 0
    assert not card.scanHeld


def test_a_detector_must_name_its_input_when_the_card_has_several():
    nidaq, card = _card(["apd_a", "apd_b"])
    with pytest.raises(ValueError, match="photon_input: one of apd_a, apd_b"):
        stm.SwabianTimeTaggerManager(_Info({"measurement": "counting"}), "X", nidaq,
                                     timeTaggerManager=card)
    with pytest.raises(ValueError, match="is a line_clock input"):
        _detector(nidaq, card, "Y", "line_clock")
    with pytest.raises(ValueError, match="measurement 'tcspc'"):
        _detector(nidaq, card, "Z", "apd_a", measurement="tcspc")


# --------------------------------------------------------------------------- #
# FLIM on two inputs against one sync                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("reverse", [False, True])
def test_flim_on_two_inputs_against_one_sync(reverse):
    nidaq, card = _card(["apd_a", "apd_b"], sync=True, filterSyncByPhotons=reverse)
    # exp1: its global fit lands on the truth here (the moment fit reads
    # long lifetimes short on a one-period window, the same for any input).
    dets = [_detector(nidaq, card, f"FLIM_{n}", n, "flim", fit_method="exp1")
            for n in ("apd_a", "apd_b")]
    if reverse:
        assert card.waitForFilter()
        assert card.filterChannels() == ([-1, -4], [2])
        assert card.tagger.filterOn
    _prepare(nidaq, dets)
    taus = {}
    for det in dets:
        assert det._flim.start_channel == (card.channel(det.photonInput) if reverse else 2)
        frames, acks = _run_sync(det)
        assert acks == [det.name]
        final = [f for f in frames if f[2]][-1]
        taus[det.photonInput] = final[5]
        expected = float(np.median(_truth(card, det.photonInput)[1]))
        assert final[5] == pytest.approx(expected, abs=0.25), (det.name, final[5], expected)
        assert det._image_raw is not None
    assert taus["apd_b"] - taus["apd_a"] == pytest.approx(0.75, abs=0.3)
    assert not card.scanHeld


def test_flim_and_counting_detectors_share_one_scan():
    nidaq, card = _card(["apd_a", "apd_b"], sync=True)
    flim = _detector(nidaq, card, "FLIM", "apd_a", "flim")
    count = _detector(nidaq, card, "COUNT", "apd_b", "counting")
    _prepare(nidaq, [flim, count])
    assert flim._flim.pixel_begin_channel == count._flim.counter.begin_channel
    _run_sync(flim)
    assert card.scanHeld
    _run_sync(count)
    assert not card.scanHeld
    counts = count.drainChunk().raw[0]
    assert _residual(counts, _truth(card, "apd_b")[0]) < 0.05


# --------------------------------------------------------------------------- #
# Provenance                                                                   #
# --------------------------------------------------------------------------- #


def test_each_detector_records_its_input_and_its_conditioning():
    nidaq, card = _card(["apd_a", "apd_b"], sync=True)
    card.setDeadtime("apd_b", 40_000)
    card.setDelay("apd_b", 1_500)
    flim = _detector(nidaq, card, "FLIM", "apd_a", "flim")
    count = _detector(nidaq, card, "COUNT", "apd_b", "counting")
    # The detector-parameter path recordings store per detector.
    p = count.parameters
    assert p["click_role"].value == "apd_b"
    assert p["click_channel"].value == -4
    assert p["click_trigger"].value == -0.25
    assert p["click_deadtime_ps"].value == 40_000
    assert p["click_delay_ps"].value == 1_500
    assert p["measurement"].value == "counting"
    assert flim.parameters["click_role"].value == "apd_a"
    assert flim.parameters["measurement"].value == "flim"

    flim.configureTimeResolvedProducts(stm.TimeResolvedScanConfig(), owner="t")
    _prepare(nidaq, [flim, count])
    _run_sync(flim)
    _run_sync(count)
    meta = flim.getLastTimeResolvedProducts().metadata
    assert meta["photon_input"]["input"] == "apd_a"
    assert meta["photon_input"]["channel"] == -1
    assert meta["photon_input"]["trigger_v"] == -0.25
    assert meta["time_tagger"]["inputs"]["apd_b"]["deadtime_ps"] == 40_000
    assert set(meta["photon_input"]["scan_session"]["participants"]) == {"FLIM", "COUNT"}
    provenance = count.inputProvenance()
    assert provenance["input"] == "apd_b" and provenance["delay_ps"] == 1_500


# --------------------------------------------------------------------------- #
# Threads: abort, stop and a slow detector                                     #
# --------------------------------------------------------------------------- #


class _SlowCounter(tm.CountBetweenMarkers):
    delay_s = 0.0

    def getData(self):
        time.sleep(type(self).delay_s)
        return super().getData()


class _SlowCountApi(tm.MockTimeTaggerApi):
    CountBetweenMarkers = _SlowCounter


def _pump(qapp, seconds, until=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents(QtCore.QEventLoop.AllEvents, 5)
        if until is not None and until():
            return True
        time.sleep(0.002)
    return bool(until()) if until is not None else None


def _timed(function):
    started = time.monotonic()
    function()
    return time.monotonic() - started


@pytest.fixture
def threaded(qapp):
    made = []

    def build(apds, slow=None, delay_s=1.0):
        nidaq, card = _card(apds, api_class=_SlowCountApi)
        dets = [_detector(nidaq, card, f"DET_{n}", n, live_fit_period_s=0.2) for n in apds]
        made.append((card, dets))
        if slow is not None:
            _SlowCounter.delay_s = 0.0
        return nidaq, card, dets

    yield build
    _SlowCounter.delay_s = 0.0
    for card, dets in made:
        for det in dets:
            det.finalize()
        card.finalize()


def _slow(det, seconds):
    """Make this detector's reads take ``seconds`` (the others stay fast)."""
    counter = det._flim.counter
    original = counter.getData

    def getData():
        time.sleep(seconds)
        return original()
    counter.getData = getData


def test_graceful_end_with_one_slow_detector_acks_each_and_holds_until_the_last(qapp, threaded):
    nidaq, card, (fast, slow) = threaded(["apd_a", "apd_b"])
    _prepare(nidaq, [fast, slow])
    _slow(slow, 0.6)
    nidaq.sigScanStarted.emit()
    acks = []
    nidaq.sigScanDone.emit()
    for det in (fast, slow):
        assert _timed(lambda d=det: d.finishScan("graceful", lambda d=det: acks.append(d.name))) \
            < GUI_BOUND_S
    assert _pump(qapp, 5.0, until=lambda: fast.name in acks)
    if slow.name not in acks:
        assert card.scanHeld, "released before the slow detector drained"
    assert _pump(qapp, 5.0, until=lambda: slow.name in acks)
    assert sorted(acks) == sorted([fast.name, slow.name])
    assert _pump(qapp, 1.0, until=lambda: not card.scanHeld)
    assert fast.drainChunk().raw.shape == (1, NY, NX)
    assert slow.drainChunk().raw.shape == (1, NY, NX)


def test_abort_with_one_slow_detector_acks_at_once_and_releases_the_card(qapp, threaded):
    nidaq, card, (fast, slow) = threaded(["apd_a", "apd_b"])
    _prepare(nidaq, [fast, slow])
    _slow(slow, 1.5)
    slow._flim.counter.complete_after_polls = 10 ** 9   # its frame stays open
    fast._flim.counter.complete_after_polls = 10 ** 9
    nidaq.sigScanStarted.emit()
    _pump(qapp, 0.4)                                    # the slow one is inside a read
    acks = []
    for det in (fast, slow):
        stall = _timed(lambda d=det: d.finishScan("abort", lambda d=det: acks.append(d.name)))
        assert stall < GUI_BOUND_S, f"abort held the GUI thread {stall:.3f} s"
    assert sorted(acks) == sorted([fast.name, slow.name])
    assert not card.scanHeld
    _pump(qapp, 2.0)
    assert fast.drainChunk().raw.size == 0 and slow.drainChunk().raw.size == 0
    assert not card.scanSession.leftClean()


def test_stop_with_one_slow_detector_is_bounded(qapp, threaded):
    nidaq, card, (fast, slow) = threaded(["apd_a", "apd_b"])
    _prepare(nidaq, [fast, slow])
    _slow(slow, 1.5)
    slow._flim.counter.complete_after_polls = 10 ** 9
    nidaq.sigScanStarted.emit()
    _pump(qapp, 0.4)
    for det in (fast, slow):
        stall = _timed(det.stopAcquisition)
        assert stall < GUI_BOUND_S, f"stop held the GUI thread {stall:.3f} s"
    assert not card.scanHeld
    assert _pump(qapp, 4.0, until=lambda: (slow._reapRetiredWorkers() or not slow._retiredWorkers))


def test_threads_finish_a_repeat_cycle_for_every_detector(qapp, threaded):
    nidaq, card, dets = threaded(list(APDS))
    finals = {d.name: 0 for d in dets}
    for det in dets:
        det.sigTimeResolvedProducts.connect(
            lambda live, n=det.name: finals.__setitem__(n, finals[n] + 1) if live.is_final else None)
    for _cycle in range(2):
        _prepare(nidaq, dets)
        nidaq.sigScanStarted.emit()
        nidaq.sigScanDone.emit()
        acks = []
        for det in dets:
            det.finishScan("graceful", lambda d=det: acks.append(d.name))
        assert _pump(qapp, 10.0, until=lambda: len(acks) == len(dets))
        assert _pump(qapp, 1.0, until=lambda: not card.scanHeld)
        for det in dets:
            det.stopAcquisition()
            assert det.drainChunk().raw.shape == (1, NY, NX)
    assert all(count == 2 for count in finals.values()), finals
    assert all(det._scan["flim_reused"] for det in dets)
