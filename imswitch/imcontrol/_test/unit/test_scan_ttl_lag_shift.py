"""The digital lines follow the beam (scan timing plan, S5): the galvo trails
its command by the phase delay, the detectors' readout is shifted by it, and
now the laser and camera lines are played that much later too -- the dark
edge pixel of fast scans was the last pixel of every line integrating past a
gate that closed on command time.
"""
import json
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.model import SetupInfo
from imswitch.imcontrol.model.managers.NidaqManager import (
    NidaqManager, NidaqManagerError, SCAN_CLOCK_RATE_HZ, delayed_lines, lag_samples,
)
from imswitch.imcontrol.model.scan_parameters import lag_report
from imswitch.imcontrol.model.signaldesigners.AdvancedScanTTLCycleDesigner import (
    AdvancedScanTTLCycleDesigner,
)
from imswitch.imcontrol.model.signaldesigners.GalvoScanDesigner import GalvoScanDesigner

from .test_galvo_signal_goldens import _params, _setup_sted_like


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
    with pytest.raises(NidaqManagerError, match='L488: a 6-sample phase delay is as long'):
        delayed_lines([high], 6, names=['L488'])              # a lag the stream cannot hold
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


def test_a_line_still_active_at_the_end_is_refused_not_cut():
    """The samples a delay pushes past the end are dropped: a gate still open
    there would lose its falling edge and stay open after the scan. Refused,
    naming the line, instead of played (review 2026-10-16)."""
    gate = np.array([False, False, True, True, True], dtype=bool)
    with pytest.raises(NidaqManagerError, match='L488 is active in the last 2 samples'):
        delayed_lines([gate], 2, idle=[False], names=['L488'])
    (fits,) = delayed_lines([gate], 2, idle=[True], names=['L488'])   # active-low: that is idle
    np.testing.assert_array_equal(fits, [True, True, False, False, True])


def test_the_lag_report_states_what_each_side_plays_and_the_residual():
    """The gate is delayed in whole scan-clock samples, the detectors in whole
    timer samples; the residual is what is left between them, at most half a
    scan-clock sample."""
    slow = lag_report(3, 100_000, 1_000_000)                  # 3 µs on a 10 µs clock
    assert slow == pytest.approx({'requested_s': 3e-6, 'gate_delay_s': 0.0,
                                  'detector_delay_s': 3e-6, 'residual_s': 3e-6})
    fast = lag_report(3, 1_000_000, 1_000_000)                # the 1 µs clock plays it exactly
    assert fast['residual_s'] == 0.0 and fast['gate_delay_s'] == pytest.approx(3e-6)
    half = lag_report(35, 100_000, 1_000_000)                 # 3.5 samples: to even, 4
    assert half['gate_delay_s'] == pytest.approx(40e-6)
    assert half['residual_s'] == pytest.approx(-5e-6)
    assert lag_report(0, 100_000, 1_000_000)['residual_s'] == 0.0


def _galvo_line_scan(rate_hz, phase_delay_us):
    """A 1 µs-pixel galvo raster at ``rate_hz`` with its laser gate, as the
    Advanced scan designs it."""
    setup = _setup_sted_like()
    setup.scan.sampleRate = rate_hz
    setup.lasers = {'L488': SimpleNamespace(digitalLine='Dev1/port0/line0')}
    setup.detectors = {}
    params = _params(['GalvoX', 'GalvoY'], [0.4, 0.2], [0.02, 0.02])
    params['sequence_time'] = 1e-6 if rate_hz >= 1_000_000 else 1e-5
    params['phase_delay'] = phase_delay_us
    _sig, _pos, info = GalvoScanDesigner().make_signal(params, setup)
    digital = {'target_device': ['L488'], 'n_linesteps': 1, 'linestep_enable': {'L488': [True]},
               'pulse_starts_s': {'L488': [[]]}, 'pulse_ends_s': {'L488': [[]]},
               'advanced_mode': False, 'sequence_time': params['sequence_time']}
    ttl, _ = AdvancedScanTTLCycleDesigner().make_signal(digital, setup, info)
    return info, np.asarray(ttl['L488'], dtype=bool)


@pytest.mark.parametrize('phase_delay_us', [30, 250, 1000])
def test_a_galvo_scan_ends_with_room_for_its_lag_and_its_last_pixel_is_gated(phase_delay_us):
    """At 1 MHz: the designer ends the stream with at least the lag of idle
    samples, so the delayed gate keeps its final falling edge, and the last
    pixel the detector reads -- lag later than commanded -- is under the
    gate. 1000 µs is longer than this scan's last turnaround and the 100 µs
    safety padding together (596 samples): only the end padding sized by the
    lag makes room for it."""
    rate = 1_000_000
    info, gate = _galvo_line_scan(rate, phase_delay_us)
    shift = lag_samples({'phase_delay': phase_delay_us}, rate)
    (played,) = delayed_lines([gate], shift, idle=[False], names=['L488'])
    assert not played[-1]                                         # closed after the scan
    assert np.count_nonzero(np.diff(played.astype(np.int8)) == -1) == \
        np.count_nonzero(np.diff(gate.astype(np.int8)) == -1)     # every falling edge kept
    last_on = np.flatnonzero(gate)[-1]                            # the last commanded pixel sample
    spp = info['samples_per_pixel']
    window = slice(last_on - spp + 1 + shift, last_on + 1 + shift)  # where the detector reads it
    assert played[window].all()


def test_a_lag_longer_than_the_idle_end_is_refused():
    info, gate = _galvo_line_scan(1_000_000, 30)
    idle_end = len(gate) - 1 - np.flatnonzero(gate)[-1]
    delayed_lines([gate], idle_end, idle=[False], names=['L488'])   # exactly fits
    with pytest.raises(NidaqManagerError, match='L488 is active in the last'):
        delayed_lines([gate], idle_end + 1, idle=[False], names=['L488'])


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
        assert info['lag'] == pytest.approx({'requested_s': 30e-6, 'gate_delay_s': 30e-6,
                                             'detector_delay_s': 30e-6, 'residual_s': 0.0})
        np.testing.assert_array_equal(signals[0], [False] * 3 + [True] * 4 + [False] * 3)
        assert signalDic['TTLCycleSignalsDict']['L488'] is ttl    # the design is untouched
    finally:
        try:
            manager.finalize()
        except Exception:
            pass
