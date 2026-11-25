"""GUI-thread time at scan start with N detectors on one card (N = 1, 2, 4).

The rig of ``test_swabian_gui_responsiveness``: a mock card made as slow as
the vendor library can be (building a ``Flim`` 0.3 s, reading a frame
0.2 s), with the same slowness given to building and reading a
``CountBetweenMarkers`` (its real cost is unknown). Measured: how long
preparing and starting a scan (``sigScanBuilt`` + ``sigScanStarted``, both
on the GUI thread) takes the first time, and on a repeat scan after every
detector drained a clean frame.

Every measurement must exist before the NI-DAQ starts the clocks, so the
first start costs one build per detector -- that scales with N and is
reported (``MEASURED``, printed with ``-s``), bounded per detector. A
repeat start reuses every measurement and the shared pixel markers, so it
stays below the GUI bound whatever N.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.model.SetupInfo import TimeTaggerInfo, TimeTaggerInputInfo
from imswitch.imcontrol.model.interfaces import timetagger_mock as tm
from imswitch.imcontrol.model.managers.TimeTaggerManager import TimeTaggerManager
from imswitch.imcontrol.model.managers.detectors import SwabianTimeTaggerManager as stm
from imswitch.imcontrol._test.unit import test_swabian_gui_responsiveness as base

pytestmark = pytest.mark.nohardware

SLOW = base.SLOW
GUI_BOUND_S = base.GUI_BOUND_S
CHANNELS = (-1, -4, -5, -6)

#: (measurement, N) -> {"first": s, "repeat": s}, for the report.
MEASURED = {}


class _SlowCounter(tm.CountBetweenMarkers):
    def __init__(self, *args, **kwargs):
        time.sleep(SLOW["flim_build"])
        super().__init__(*args, **kwargs)

    def getData(self):
        time.sleep(SLOW["read"])
        return super().getData()


class _SlowApi(base._SlowApi):
    CountBetweenMarkers = _SlowCounter


def _rig(n, measurement):
    inputs = {f"apd{i}": TimeTaggerInputInfo(channel=CHANNELS[i], triggerV=-0.25) for i in range(n)}
    inputs["sync"] = TimeTaggerInputInfo(channel=2, kind="laser_sync")
    inputs["line_clock"] = TimeTaggerInputInfo(channel=3, kind="line_clock")
    info = TimeTaggerInfo(simulation=True, inputs=inputs, mockSample="uniform")
    nidaq = base._Nidaq()
    card = TimeTaggerManager(info, SimpleNamespace(scan=SimpleNamespace(sampleRate=base.SAMPLE_RATE)),
                             nidaq, api=_SlowApi(model=tm.SignalModel.from_info(info)))
    dets = []
    for i in range(n):
        props = {"photon_input": f"apd{i}", "measurement": measurement,
                 "laser_rep_rate_mhz": 80.0, "min_counts_per_pixel": 5, "live_fit_period_s": 0.0}
        dets.append(stm.SwabianTimeTaggerManager(base._Info(props), f"DET{i}", nidaq,
                                                 timeTaggerManager=card))
    return nidaq, card, dets


def _arm(nidaq, dets):
    info = base._scan_info(dets[0].name)
    info[base.se.PARTICIPANTS_KEY] = [d.name for d in dets]
    nidaq.sigScanBuilt.emit(info, {"TTLCycleSignalsDict": base._ttl()}, [])
    nidaq.sigScanStarted.emit()
    assert nidaq.failures == []


@pytest.mark.parametrize("measurement", ["flim", "counting"])
@pytest.mark.parametrize("n", [1, 2, 4])
def test_gui_thread_time_at_first_and_repeat_start(qapp, n, measurement):
    nidaq, card, dets = _rig(n, measurement)
    try:
        first = base._timed(lambda: _arm(nidaq, dets))
        # Drain a clean frame on every detector, release the leases.
        nidaq.sigScanDone.emit()
        acks = []
        for det in dets:
            det.finishScan("graceful", lambda d=det: acks.append(d.name))
        assert base._pump(qapp, 20.0, until=lambda: len(acks) == n)
        assert base._pump(qapp, 2.0, until=lambda: not card.scanHeld)
        for det in dets:
            det.stopAcquisition()
        assert card.scanSession.leftClean()

        repeat = base._timed(lambda: _arm(nidaq, dets))
        assert all(det._scan["flim_reused"] for det in dets)
        assert all(det._scan["pixel_markers_reused"] for det in dets)
        MEASURED[(measurement, n)] = {"first": round(first, 3), "repeat": round(repeat, 3)}
        print(f"\nGUI thread, {n} x {measurement}: first start {first:.3f} s, "
              f"repeat start {repeat:.4f} s")

        # One build per detector at the first start, nothing else that
        # scales; nothing at all on a repeat.
        assert first < n * SLOW["flim_build"] + GUI_BOUND_S, first
        assert repeat < GUI_BOUND_S, repeat
        for det in dets:
            det.stopAcquisition()
    finally:
        for det in dets:
            det.finalize()
        card.finalize()
    assert np.isfinite(first)
