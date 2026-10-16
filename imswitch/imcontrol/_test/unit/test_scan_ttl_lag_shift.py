"""The digital lines follow the beam (scan timing plan, S5): the galvo trails
its command by the phase delay, the detectors' readout is shifted by it, and
now the laser and camera lines are played that much later too -- the dark
edge pixel of fast scans was the last pixel of every line integrating past a
gate that closed on command time.
"""
import json

import numpy as np
import pytest

from imswitch.imcontrol.model import SetupInfo
from imswitch.imcontrol.model.managers.NidaqManager import (
    NidaqManager, SCAN_CLOCK_RATE_HZ, delayed_lines, lag_samples,
)


def test_the_lag_is_whole_samples_of_the_scan_clock_nearest():
    assert lag_samples({'phase_delay': 0}) == 0
    assert lag_samples({}) == 0
    assert lag_samples({'phase_delay': 35}) == 4             # 35 µs on a 10 µs clock
    assert lag_samples({'phase_delay': 100}) == 10
    assert lag_samples({'phase_delay': 3}, rate_hz=1_000_000) == 3


def test_delayed_lines_pad_with_the_idle_level_and_keep_the_length():
    high = np.array([False, False, True, True, False, False], dtype=bool)
    low = ~high                                               # an active-low line idles high
    shifted_high, shifted_low = delayed_lines([high, low], 2, idle=[False, True])
    np.testing.assert_array_equal(shifted_high, [False, False, False, False, True, True])
    np.testing.assert_array_equal(shifted_low, [True, True, True, True, False, False])
    assert len(shifted_high) == len(high)
    (same,) = delayed_lines([high], 0)
    np.testing.assert_array_equal(same, high)
    (whole,) = delayed_lines([high], 6)                       # a lag past the stream: as is
    np.testing.assert_array_equal(whole, high)
    (on_at_start,) = delayed_lines([np.array([True, True, False])], 1)
    np.testing.assert_array_equal(on_at_start, [False, True, True])   # idle low, not its 1st


def test_the_gate_lands_on_the_detectors_pixel_after_the_shift():
    """One line of four 2-sample pixels and a 3-sample lag: the detector's
    pixel k integrates command samples [2k + lag, 2k + 2 + lag); the gate for
    pixel k, played lag later, covers exactly those samples."""
    lag, spp, pixels = 3, 2, 4
    gate = np.zeros(pixels * spp + lag + 2, dtype=bool)
    gate[:pixels * spp] = True                                # on for the command-time line
    (played,) = delayed_lines([gate], lag)
    for k in range(pixels):
        start = k * spp + lag
        assert played[start:start + spp].all(), k
    assert not played[pixels * spp + lag:].any()              # off after the last pixel
    assert not played[:lag].any()                             # idle before the beam arrives


def _manager():
    setup = SetupInfo.from_json(json.dumps({
        'detectors': {}, 'positioners': {},
        'lasers': {'L488': {'analogChannel': None, 'digitalLine': 'Dev1/port0/line1',
                            'managerName': 'NidaqLaserManager', 'managerProperties': {},
                            'wavelength': 488, 'valueRangeMin': 0, 'valueRangeMax': 1}},
        'scan': {'scanWidgetType': 'PointScan', 'scanDesigner': 'GalvoScanDesigner',
                 'scanDesignerParams': {}, 'TTLCycleDesigner': 'PointScanTTLCycleDesigner',
                 'TTLCycleDesignerParams': {}, 'sampleRate': SCAN_CLOCK_RATE_HZ},
        'nidaq': {'simulation': True, 'timerCounterChannel': 'Dev1/ctr2',
                  'startTrigger': False},
    }), infer_missing=True)
    return NidaqManager(setup)


def test_a_simulated_run_writes_the_lines_delayed_by_the_phase_delay():
    manager = _manager()
    try:
        ttl = np.array([True] * 4 + [False] * 6, dtype=bool)
        signalDic = {'scanSignalsDict': {}, 'TTLCycleSignalsDict': {'L488': ttl}}
        info = {'phase_delay': 30, 'n_linesteps': 1, 'scan_time_step': 1 / SCAN_CLOCK_RATE_HZ,
                'scan_samples_total': 10, 'img_dims': [10], 'scan_samples': [1, 10],
                'scan_samples_d2_period': 10}
        manager.runScan(signalDic, info)
        devices, signals, shift = manager._lastDigitalWrite
        assert devices == ['L488'] and shift == 3
        np.testing.assert_array_equal(signals[0], [False] * 3 + [True] * 4 + [False] * 3)
        assert signalDic['TTLCycleSignalsDict']['L488'] is ttl    # the design is untouched
    finally:
        try:
            manager.finalize()
        except Exception:
            pass
