"""Scan correctness on the mock card (Lifetime 2.0, P3).

The frame role, the pixel-pattern offset, the line-delay rule, linestep
refusal, and the worker's count-based final-frame detection in both orders
against scan-done -- what the first external review asked to be tested.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.model.SetupInfo import TimeTaggerInfo
from imswitch.imcontrol.model.interfaces.timetagger_mock import MockTimeTaggerApi, SignalModel
from imswitch.imcontrol.model.managers._scan_execution import PARTICIPANTS_KEY
from imswitch.imcontrol.model.managers.TimeTaggerManager import TimeTaggerManager
from imswitch.imcontrol.model.managers.detectors.SwabianTimeTaggerManager import (
    SwabianTimeTaggerManager,
    _TTFlimWorker,
)
from imswitch.imcontrol.model.timeresolved import LiveProducts

pytestmark = pytest.mark.nohardware

SAMPLE_RATE = 100_000
DWELL_SAMPLES = 10
FLYBACK_SAMPLES = 50
NX, NY = 8, 4


class _Signal:
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
        self.sigScanBuilt = _Signal()
        self.sigScanStarted = _Signal()
        self.sigScanDone = _Signal()
        self.scanBuildFailures = []

    def reportScanBuildFailure(self, stage, error):
        self.scanBuildFailures.append((stage, error))


class _DetectorInfo:
    forAcquisition = True
    forFocusLock = False

    def __init__(self, props):
        self.managerProperties = props


def _ttl(ny=NY, nx=NX):
    line = np.zeros(nx * DWELL_SAMPLES + FLYBACK_SAMPLES, dtype=bool)
    line[:10] = True
    frame = np.zeros(line.size * ny, dtype=bool)
    frame[:10] = True
    return {"line_clock": np.tile(line, ny), "frame_start_clock": frame}


def _scan_info(name, ny=NY, nx=NX, linesteps=1):
    return {
        PARTICIPANTS_KEY: [name],
        "img_dims": [nx, ny],
        "img_axes_phys": ["x", "y"],
        "pixel_sizes": [1.0, 1.0],
        "dwell_time": DWELL_SAMPLES / SAMPLE_RATE,
        "n_linesteps": linesteps,
        "tot_scan_time_s": 1.0,
    }


def _rig(frame=True, line_delay_ps=0, detector_props=None, **info_kwargs):
    nidaq = _Nidaq()
    info = TimeTaggerInfo(
        simulation=True, photonsChannel=-1, photonsTriggerV=-0.25,
        frameClockChannel=4 if frame else None, lineClockDelayPs=line_delay_ps,
        mockSample="uniform", **info_kwargs,
    )
    model = SignalModel.from_info(info)
    model.irf_fwhm_ps = 20.0
    card = TimeTaggerManager(info, SimpleNamespace(scan=SimpleNamespace(sampleRate=SAMPLE_RATE)),
                             nidaq, api=MockTimeTaggerApi(model=model))
    props = {"laser_rep_rate_mhz": 80.0, "min_counts_per_pixel": 10}
    props.update(detector_props or {})
    det = SwabianTimeTaggerManager(_DetectorInfo(props), "FLIM", nidaq, timeTaggerManager=card)
    return nidaq, card, det


def _build(nidaq, det, **scan_kwargs):
    nidaq.sigScanBuilt.emit(_scan_info(det.name, **scan_kwargs), {"TTLCycleSignalsDict": _ttl()}, [])


def _run_worker(det, *, done_before_run=True, complete_after_polls=0):
    det._flim.complete_after_polls = complete_after_polls
    generation = det._preparedScanGeneration
    det._activeScanGeneration = generation
    worker = _TTFlimWorker(det, generation)
    frames = []
    worker.sigFrameReady.connect(lambda *args: frames.append(args))
    worker.sigFrameReady.connect(det._on_frame_ready)
    if done_before_run:
        worker.signal_done()
    worker.run()
    return worker, frames


# --------------------------------------------------------------------------- #
# Frame role, pattern offset, delays                                           #
# --------------------------------------------------------------------------- #


def test_frame_role_is_auto_and_reaches_flim():
    nidaq, card, det = _rig(frame=True)
    assert det.parameters["frame_role"].value == "auto"
    assert det.parameters["frame_channel"].value == 4
    _build(nidaq, det)
    assert det._flim.frame_begin_channel == 4

    nidaq2, card2, det2 = _rig(frame=False)
    assert det2.parameters["frame_channel"].value == 0
    _build(nidaq2, det2)
    assert det2._flim.frame_begin_channel < 0, "CHANNEL_UNUSED without a frame role"

    det.setParameter("frame_role", "none")
    assert det._frame_ch is None
    with pytest.raises(ValueError, match="frame_role 'sted_pulse'"):
        det.setParameter("frame_role", "sted_pulse")


def test_pixel_pattern_starts_at_the_offset_plus_a_positive_line_delay():
    nidaq, card, det = _rig(line_delay_ps=3000, pixelPatternOffsetPs=10_000)
    _build(nidaq, det)
    period = DWELL_SAMPLES * int(1e12 / SAMPLE_RATE)
    np.testing.assert_array_equal(det._ev_pix_begin.pattern, 13_000 + np.arange(NX) * period)
    assert det._scan["pattern_offset_ps"] == 13_000
    # The line input carries no card delay; the frame input the full one.
    assert card.tagger.getInputDelay(3) == 0
    assert card.tagger.getInputDelay(4) == 3000


def test_a_negative_line_delay_goes_on_the_card_for_both_clocks():
    nidaq, card, det = _rig(line_delay_ps=-700)
    _build(nidaq, det)
    assert card.tagger.getInputDelay(3) == -700
    assert card.tagger.getInputDelay(4) == -700
    assert det._scan["pattern_offset_ps"] == 10_000
    det.stopAcquisition()  # releases the scan hold; conditioning allowed again
    card.setDelay("line_clock", 500)
    assert card.tagger.getInputDelay(3) == 0 and card.tagger.getInputDelay(4) == 500
    assert card.patternOffsetPs("line_clock") == 10_500


def test_linesteps_are_refused_until_the_clock_count_is_fixed():
    nidaq, card, det = _rig()
    _build(nidaq, det, linesteps=2)
    assert det._preparedScanGeneration is None
    stage, error = nidaq.scanBuildFailures[0]
    assert "2 linesteps" in str(error) and "M9" in str(error)
    assert not card.scanHeld


# --------------------------------------------------------------------------- #
# The final frame, by count                                                    #
# --------------------------------------------------------------------------- #


def _final(frames):
    intensity, lifetime, is_final, *_rest, live = frames[-1]
    assert is_final
    return intensity, live


def test_a_frame_closed_before_scan_done_is_taken_at_once():
    nidaq, card, det = _rig()
    _build(nidaq, det)
    worker, frames = _run_worker(det, done_before_run=True, complete_after_polls=1)
    intensity, live = _final(frames)
    assert live.metadata["frame_closed_by_card"] is True
    assert intensity.sum() > 0
    assert not card.scanHeld


def test_a_frame_closed_after_scan_done_is_still_detected_by_count(caplog):
    nidaq, card, det = _rig()
    _build(nidaq, det)
    # The card closes the frame only after a few polls past scan-done.
    _TTFlimWorker.POLL_S = 0.02
    try:
        worker, frames = _run_worker(det, done_before_run=True, complete_after_polls=3)
    finally:
        _TTFlimWorker.POLL_S = 0.25
    _, live = _final(frames)
    assert live.metadata["frame_closed_by_card"] is True
    assert "did not close" not in caplog.text


def test_a_frame_the_card_never_closes_falls_back_after_the_grace_period(caplog):
    nidaq, card, det = _rig()
    _build(nidaq, det)
    _TTFlimWorker.POLL_S = 0.02
    _TTFlimWorker.FINAL_FRAME_GRACE_S = 0.1
    try:
        worker, frames = _run_worker(det, done_before_run=True, complete_after_polls=10**6)
    finally:
        _TTFlimWorker.POLL_S = 0.25
        _TTFlimWorker.FINAL_FRAME_GRACE_S = 2.0
    _, live = _final(frames)
    assert live.metadata["frame_closed_by_card"] is False
    assert "did not close the last frame" in caplog.text


def test_intensity_preview_carries_no_lifetime():
    nidaq, card, det = _rig(detector_props={"live_fit_period_s": 0.0})
    _build(nidaq, det)
    live = []
    det.sigTimeResolvedProducts.connect(live.append)
    worker = _TTFlimWorker(det, det._preparedScanGeneration)
    worker._direction = "forward"
    worker._background_per_bin = 0.0
    worker._dwell_s = DWELL_SAMPLES / SAMPLE_RATE
    t_axis = (np.arange(391) + 0.5) * 32e-12
    worker._emit_intensity_preview(NX, NY, t_axis.astype(np.float32))
    assert len(live) == 1 and isinstance(live[0], LiveProducts)
    assert live[0].lifetime_ns is None
    assert live[0].intensity.shape == (NY, NX) and live[0].intensity.sum() > 0
    assert live[0].metadata["preview"] == "intensity"
    assert det.parameters["live_fit_period_s"].value == 0.0


# --------------------------------------------------------------------------- #
# Stale workers and the photon delay (review round 2)                          #
# --------------------------------------------------------------------------- #


def test_a_stale_workers_completion_does_not_release_a_newer_scans_hold():
    nidaq, card, det = _rig()
    _build(nidaq, det)
    old = det._preparedScanGeneration
    det._activeScanGeneration = old
    # A new scan is prepared while the old worker's final frame is in flight.
    _build(nidaq, det)
    new = det._preparedScanGeneration
    assert new != old and card.scanHeld
    det._fireFinalFrameAck(old)
    assert card.scanHeld, "the old worker must not release the new scan's hold"
    det._onScanWorkerFinished(old)
    assert card.scanHeld
    det._fireFinalFrameAck(new)
    assert not card.scanHeld


def test_t0_goes_on_top_of_the_configured_photon_delay_and_comes_off_again():
    nidaq, card, det = _rig(detector_props={"t0_ps": 1300})
    _build(nidaq, det)
    assert card.tagger.getInputDelay(-1) == -1300, "forward: the peak moves to bin 0"
    det.stopAcquisition()
    assert card.tagger.getInputDelay(-1) == 0, "restored for the diagnostics"

    nidaq, card, det = _rig(detector_props={"t0_ps": 1300}, filterSyncByPhotons=True)
    _build(nidaq, det)
    assert card.tagger.getInputDelay(-1) == 0, "reverse: t0 is a software roll"


def test_the_enabled_parameter_keeps_the_detector_out_of_a_scan():
    nidaq, card, det = _rig()
    assert det.parameters["enabled"].value == "True"
    det.setParameter("enabled", "False")
    _build(nidaq, det)
    assert det._preparedScanGeneration is None
    assert not card.scanHeld, "the card is free for a calibration during the scan"
    assert det.pixel_marker_channels() == {}
    det.setParameter("enabled", "True")
    _build(nidaq, det)
    assert det._preparedScanGeneration is not None and card.scanHeld
    assert set(det.pixel_marker_channels()) == {"pixel_begin", "pixel_end"}
