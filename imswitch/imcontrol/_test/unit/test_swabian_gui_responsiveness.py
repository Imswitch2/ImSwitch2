"""The Time Tagger never holds the GUI thread (the "Swabian freezes the UI"
report): scan start of a repeat scan, stop and abort during a fit, the
repeat-scan cycle and trigger-level edits, on a mock card made slow where the
vendor library is slow -- building a ``Flim``, reading a frame, writing a
trigger level -- and with a fit that takes seconds.

Every bound here is on the time a call made *on the GUI thread* takes, or on
the longest gap of a 5 ms heartbeat timer during a whole cycle. Before the
change (measured on the same rig): a stop or abort during a 1.5 s fit held
the GUI thread for the rest of the fit (up to the 2 s join, then FAULTED);
every repeat frame built a new ``Flim`` on it; a level edit waited for the
card; a final fit longer than 5 s was cut off by the scan coordinator.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
from qtpy import QtCore

from imswitch.imcontrol.model.SetupInfo import TimeTaggerInfo
from imswitch.imcontrol.model.interfaces import timetagger_mock as tm
from imswitch.imcontrol.model.managers import _scan_execution as se
from imswitch.imcontrol.model.managers.TimeTaggerManager import TimeTaggerManager
from imswitch.imcontrol.model.managers.detectors import SwabianTimeTaggerManager as stm

pytestmark = pytest.mark.nohardware

SAMPLE_RATE, DWELL, FLYBACK, NX, NY = 100_000, 10, 50, 16, 8
#: What a GUI-thread call may take: far below anything a user perceives as
#: a freeze, far above scheduling noise on a loaded CI machine.
GUI_BOUND_S = 0.15

SLOW = dict(flim_build=0.3, read=0.2, level=0.3, fit=1.0)


class _SlowFlim(tm.Flim):
    def __init__(self, *args, **kwargs):
        time.sleep(SLOW["flim_build"])
        super().__init__(*args, **kwargs)

    def getCurrentFrame(self):
        time.sleep(SLOW["read"])
        return super().getCurrentFrame()

    def getReadyFrameEx(self):
        time.sleep(SLOW["read"])
        return super().getReadyFrameEx()


class _SlowTagger(tm.MockTimeTagger):
    slow_levels = False

    def setTriggerLevel(self, channel, voltage):
        if self.slow_levels:
            time.sleep(SLOW["level"])
        super().setTriggerLevel(channel, voltage)


class _SlowApi(tm.MockTimeTaggerApi):
    Flim = _SlowFlim

    def createTimeTagger(self, serial=None):
        tagger = _SlowTagger(serial=serial, model=self._model, rates_hz=self._rates)
        self.created.append(tagger)
        return tagger


@pytest.fixture
def slow_fits(monkeypatch):
    """Every per-pixel fit sleeps SLOW['fit'] for the whole frame (pro rata
    for a block of rows); the global fit on the summed decay does not."""
    for name in ("fit_moment", "fit_phasor", "fit_exp1"):
        original = getattr(stm, name)

        def slow(*args, _original=original, **kwargs):
            pixels = args[0].shape[0] * args[0].shape[1] if np.ndim(args[0]) == 3 else 1
            if pixels > 1:
                time.sleep(SLOW["fit"] * pixels / (NX * NY))
            return _original(*args, **kwargs)
        monkeypatch.setattr(stm, name, slow)


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
        self.managerProperties = props


def _ttl():
    line = np.zeros(NX * DWELL + FLYBACK, dtype=bool)
    line[:10] = True
    return {"line_clock": np.tile(line, NY)}


def _scan_info(name):
    return {se.PARTICIPANTS_KEY: [name], "img_dims": [NX, NY], "img_axes_phys": ["x", "y"],
            "pixel_sizes": [1.0, 1.0], "dwell_time": DWELL / SAMPLE_RATE, "tot_scan_time_s": 1.0}


@pytest.fixture
def rig(qapp):
    made = []

    def build(**props):
        nidaq = _Nidaq()
        info = TimeTaggerInfo(simulation=True, photonsChannel=-1, photonsTriggerV=-0.25,
                              mockSample="uniform")
        card = TimeTaggerManager(info, SimpleNamespace(scan=SimpleNamespace(sampleRate=SAMPLE_RATE)),
                                 nidaq, api=_SlowApi(model=tm.SignalModel.from_info(info)))
        p = {"laser_rep_rate_mhz": 80.0, "min_counts_per_pixel": 5, "live_fit_period_s": 0.2}
        p.update(props)
        det = stm.SwabianTimeTaggerManager(_Info(p), "FLIM", nidaq, timeTaggerManager=card)
        made.append((card, det))
        return nidaq, card, det

    yield build
    for card, det in made:
        det.finalize()
        card.finalize()


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


def _arm(nidaq, det):
    nidaq.sigScanBuilt.emit(_scan_info(det.name), {"TTLCycleSignalsDict": _ttl()}, [])
    nidaq.sigScanStarted.emit()


def _in_a_fit(det):
    """The worker has read a frame and is fitting it (the slow part)."""
    return det._scanWorker is not None


class _Heartbeat:
    def __init__(self):
        self.timer = QtCore.QTimer()
        self.timer.setInterval(5)
        self.timer.timeout.connect(self._tick)
        self.last = time.monotonic()
        self.max_gap = 0.0

    def _tick(self):
        now = time.monotonic()
        self.max_gap = max(self.max_gap, now - self.last)
        self.last = now

    def start(self):
        self.last = time.monotonic()
        self.max_gap = 0.0
        self.timer.start()

    def stop(self):
        self.timer.stop()


# --------------------------------------------------------------------------- #
# Stop and abort while the worker is busy                                      #
# --------------------------------------------------------------------------- #


def test_stop_during_a_fit_does_not_wait_for_the_fit(qapp, rig, slow_fits):
    nidaq, card, det = rig()
    _arm(nidaq, det)
    det._flim.complete_after_polls = 10 ** 9   # the frame stays open: previews
    _pump(qapp, 0.5)                           # first read done, inside the fit
    worker = det._scanWorker
    assert worker is not None

    stall = _timed(det.stopAcquisition)

    assert stall < GUI_BOUND_S, f"stop held the GUI thread {stall:.3f} s"
    assert det._scanWorker is None and not card.scanHeld
    # The worker leaves at its next check and is reaped; nothing raised.
    assert _pump(qapp, 3.0, until=lambda: not det._retiredWorkers or
                 (det._reapRetiredWorkers() or not det._retiredWorkers))


def test_abort_during_a_fit_acknowledges_at_once_and_drops_the_frame(qapp, rig, slow_fits):
    nidaq, card, det = rig()
    _arm(nidaq, det)
    det._flim.complete_after_polls = 10 ** 9
    _pump(qapp, 0.5)
    frames = []
    det.sigNewFrame.connect(lambda: frames.append(1))
    acks = []

    stall = _timed(lambda: det.finishScan("abort", lambda: acks.append(1)))

    assert stall < GUI_BOUND_S, f"abort held the GUI thread {stall:.3f} s"
    assert acks == [1]
    _pump(qapp, 2.0)
    assert frames == [], "the aborted scan's frame was published after the abort"
    assert det.drainChunk().raw.size == 0


def test_a_worker_wedged_in_the_card_is_still_quarantined(qapp, rig, monkeypatch):
    """Retiring a busy worker is not forgetting it: one that never leaves
    the vendor library makes the next stop raise, as the bounded join did."""
    nidaq, card, det = rig()
    release = threading.Event()
    monkeypatch.setattr(_SlowFlim, "getCurrentFrame", lambda self: release.wait(30) and None)
    _arm(nidaq, det)
    det._flim.complete_after_polls = 10 ** 9
    _pump(qapp, 0.5)
    assert _timed(det.stopAcquisition) < GUI_BOUND_S
    assert len(det._retiredWorkers) == 1
    monkeypatch.setattr(stm, "_RETIRED_WORKER_STUCK_S", 0.0)
    with pytest.raises(TimeoutError, match="has not left the Time Tagger library"):
        det.stopAcquisition()
    release.set()
    assert _pump(qapp, 3.0, until=lambda: (det._reapRetiredWorkers() or not det._retiredWorkers))
    det.stopAcquisition()   # recovered


# --------------------------------------------------------------------------- #
# The repeat-scan cycle                                                         #
# --------------------------------------------------------------------------- #


def test_repeat_scan_cycles_reuse_the_flim_and_keep_the_gui_responsive(qapp, rig):
    """The "live view" freeze: every repeat frame ran stop and start back to
    back on the GUI thread, building a new cube-sized ``Flim`` each time."""
    nidaq, card, det = rig(live_fit_period_s=0.0)
    flims, finals, cycle_stalls = [], [], []
    det.sigTimeResolvedProducts.connect(lambda live: finals.append(live) if live.is_final else None)

    def on_ack():
        started = time.monotonic()
        det.stopAcquisition()      # the scan lease released
        _arm(nidaq, det)           # the next repeat frame
        cycle_stalls.append(time.monotonic() - started)
        flims.append(det._flim)
        if len(cycle_stalls) < 3:
            QtCore.QTimer.singleShot(30, finish)

    def finish():
        nidaq.sigScanDone.emit()
        det.finishScan("graceful", on_ack)

    _arm(nidaq, det)               # the first start builds the Flim
    first = det._flim
    beat = _Heartbeat()
    beat.start()
    QtCore.QTimer.singleShot(30, finish)
    assert _pump(qapp, 20.0, until=lambda: len(cycle_stalls) >= 3)
    beat.stop()

    assert len(finals) == 3 and all(f.metadata["frame_closed_by_card"] for f in finals)
    assert all(flim is first for flim in flims), "a repeat frame rebuilt the Flim"
    assert max(cycle_stalls) < GUI_BOUND_S, cycle_stalls
    assert beat.max_gap < GUI_BOUND_S + 0.05, f"GUI blocked {beat.max_gap:.3f} s in a cycle"
    assert det._scan["flim_reused"] is True


def test_a_frame_the_card_did_not_close_is_not_reused(qapp, rig):
    """An aborted or unclosed frame leaves the card's pixel index mid-frame:
    the next scan must start on a new Flim."""
    nidaq, card, det = rig(live_fit_period_s=0.0)
    _arm(nidaq, det)
    first = det._flim
    det.finishScan("abort", lambda: None)
    det.stopAcquisition()
    _arm(nidaq, det)
    assert det._flim is not first and det._scan["flim_reused"] is False


def test_a_new_geometry_builds_a_new_flim_and_frees_the_old_off_the_gui_thread(qapp, rig):
    nidaq, card, det = rig(live_fit_period_s=0.0)
    _arm(nidaq, det)
    released = []
    real = card.releaseLater
    card.releaseLater = lambda box: (released.append(list(box)), real(box))
    # As if its final frame closed cleanly and drained: the flag, then the
    # release that follows it (which also leaves the card's scan session, so
    # the shared pixel markers are kept for the next scan).
    det._flimCleanGeneration = det._preparedScanGeneration
    det._releaseCard(det._preparedScanGeneration)
    det.setParameter("n_bins", 200)
    _arm(nidaq, det)
    assert det._scan["flim_reused"] is False
    assert det._scan["pixel_markers_reused"] is True
    assert len(released) == 1 and released[0][0] is not det._flim


# --------------------------------------------------------------------------- #
# A slow final fit against the end-of-scan barrier                             #
# --------------------------------------------------------------------------- #


class _Detectors:
    """What the scan coordinator needs of a DetectorsManager: execOn, and a
    lease release that stops the detector with the bounded 2 s wait."""

    def __init__(self, det):
        self.det = det
        self.faulted = False

    def execOn(self, name, function):
        return function(self.det)

    def release(self, handle):
        done = threading.Event()
        error = []

        def stop():
            try:
                self.det.stopAcquisition()
            except Exception as exc:
                error.append(exc)
            finally:
                done.set()
        threading.Thread(target=stop, daemon=True).start()
        if not done.wait(2.0) or error:
            self.faulted = True


def test_a_slow_final_fit_neither_faults_the_detector_nor_loses_the_frame(qapp, rig, slow_fits):
    nidaq, card, det = rig(live_fit_period_s=0.2)
    detectors = _Detectors(det)
    coordinator = se.ScanExecutionCoordinator(
        detectors, nidaq,
        scheduleTimeout=lambda s, callback: QtCore.QTimer.singleShot(int(s * 1000), callback))
    finals = []
    det.sigTimeResolvedProducts.connect(lambda live: finals.append(live) if live.is_final else None)
    _arm(nidaq, det)
    det._flim.complete_after_polls = 10 ** 9
    # One preview: the worker now knows what a frame costs on this card.
    assert _pump(qapp, 5.0, until=lambda: det._frameCostPerValueS is not None)
    # The final read + fit (1.2 s here) is longer than the deadline the
    # coordinator is given (0.5 s); the detector's own deadline covers it.
    assert det.finishTimeoutS() > SLOW["read"] + SLOW["fit"]
    token = se.ScanIterationToken(["FLIM"], generation=1)
    token.leaseHandle = object()
    done = []
    nidaq.sigScanDone.emit()
    coordinator.resolve(token, se.FINISH_GRACEFUL, onComplete=lambda: done.append(1),
                        timeoutS=0.5)
    assert _pump(qapp, 15.0, until=lambda: bool(done))

    assert not token.timedOut, "the barrier gave up on the final frame"
    assert len(finals) == 1 and finals[0].is_final
    assert det.drainChunk().raw.shape == (1, NY, NX), "the recording lost its frame"
    assert not detectors.faulted


def test_the_coordinator_takes_the_longest_participant_deadline():
    class _WithHint:
        def __init__(self, value):
            self.value = value

        def finishTimeoutS(self):
            if isinstance(self.value, Exception):
                raise self.value
            return self.value

    class _Without:
        pass

    detectors = {"a": _WithHint(12.5), "b": _Without(), "c": _WithHint("junk"),
                 "d": _WithHint(RuntimeError("x")), "e": _WithHint(10_000.0)}
    manager = SimpleNamespace(execOn=lambda name, f: f(detectors[name]))
    coordinator = se.ScanExecutionCoordinator(manager, None)
    assert coordinator._participantFinishTimeout(["a", "b", "c", "d"]) == 12.5
    assert coordinator._participantFinishTimeout(["b"]) == 0.0
    assert coordinator._participantFinishTimeout(["e"]) == se.MAX_FINISH_TIMEOUT_S


# --------------------------------------------------------------------------- #
# Trigger levels and re-entry                                                   #
# --------------------------------------------------------------------------- #


def test_a_level_edit_does_not_wait_for_the_card(qapp, rig):
    nidaq, card, det = rig()
    card.tagger.slow_levels = True

    stall = _timed(lambda: det.setParameter("click_trigger", -0.3))

    assert stall < GUI_BOUND_S, f"the level edit held the GUI thread {stall:.3f} s"
    card.submit(lambda: None).result(timeout=5)
    assert card.tagger.getTriggerLevel(1) == -0.3
    assert det.parameters["click_trigger"].value == -0.3


def test_level_edits_in_quick_succession_land_in_order(qapp, rig):
    nidaq, card, det = rig()
    card.tagger.slow_levels = True
    for level in (-0.1, -0.2, -0.35):
        det.setParameter("click_trigger", level)
    card.submit(lambda: None).result(timeout=5)
    assert card.tagger.getTriggerLevel(1) == -0.35
    assert det.parameters["click_trigger"].value == -0.35


def test_starts_stops_and_aborts_arriving_twice_are_harmless(qapp, rig, slow_fits):
    """A click during a slow moment can deliver a second start, stop or
    abort while the first is in flight: none may raise out of a slot or
    leave a second worker running."""
    nidaq, card, det = rig()
    _arm(nidaq, det)
    worker = det._scanWorker
    nidaq.sigScanStarted.emit()                       # a second start
    assert det._scanWorker is worker, "a second start replaced the running worker"
    acks = []
    det.finishScan("abort", lambda: acks.append(1))
    det.finishScan("abort", lambda: acks.append(2))   # a second abort
    det.stopAcquisition()
    det.stopAcquisition()                             # a second stop
    assert acks == [1, 2]
    assert det._scanWorker is None and not card.scanHeld
    # A stale frame from the stopped worker is ignored, never raised.
    det._on_frame_ready(np.zeros((NY, NX), np.float32), np.zeros((NY, NX), np.float32),
                        True, None, None, 0.0, 12345, None)
    # A frame that cannot be committed is logged, not raised out of the slot.
    det._activeScanGeneration = 7
    det._on_frame_ready(np.zeros((3, 3), np.float32), np.zeros((3, 3), np.float32),
                        False, None, None, 0.0, 7, None)
    assert _pump(qapp, 3.0, until=lambda: (det._reapRetiredWorkers() or not det._retiredWorkers))
