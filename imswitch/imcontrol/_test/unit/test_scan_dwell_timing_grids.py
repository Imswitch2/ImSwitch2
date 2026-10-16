"""The scan timing grids (docs/design/plans/scan-timing-configurable-dwell.md,
findings F1-F3, stage S0): the pixel grid, the galvo line, the TTL lines and
the point detectors' bins agree at every dwell, because the dwell is
realized once (a whole number of samples, rounded up) and every count
follows from it. The cases that used to fail are the reproductions of the
plan's findings; the numbers they pin are what S0 changed.

Nothing here touches hardware: the designers and the detectors' sample
arithmetic are pure functions of the contract.
"""
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.model.managers.NidaqManager import (
    SCAN_CLOCK_RATE_HZ, TIMER_COUNTER_RATE_HZ,
)
from imswitch.imcontrol.model.signaldesigners.AdvancedScanTTLCycleDesigner import (
    AdvancedScanTTLCycleDesigner,
)
from imswitch.imcontrol.model.signaldesigners.GalvoScanDesigner import GalvoScanDesigner

from .test_galvo_signal_goldens import _params, _setup_sted_like

PIXELS = 20


def _setup():
    setup = _setup_sted_like()
    setup.lasers = {'L488': SimpleNamespace(digitalLine='Dev1/port0/line0')}
    setup.detectors = {}
    return setup


def _galvo(dwell_us, step_um=1.0):
    params = _params(['GalvoX', 'GalvoY'], [PIXELS * step_um, 5.0], [step_um, 1.0])
    params['sequence_time'] = dwell_us * 1e-6
    return params, GalvoScanDesigner().make_signal(params, _setup())


def _ttl(params, info):
    digital = {'target_device': ['L488'], 'n_linesteps': 1, 'linestep_enable': {'L488': [True]},
               'pulse_starts_s': {'L488': [[]]}, 'pulse_ends_s': {'L488': [[]]},
               'advanced_mode': False, 'sequence_time': params['sequence_time']}
    signals, _ = AdvancedScanTTLCycleDesigner().make_signal(digital, _setup(), info)
    return np.asarray(signals['L488'], dtype=bool)


def _on_runs(line):
    on = np.flatnonzero(line)
    return np.split(on, np.flatnonzero(np.diff(on) > 1) + 1) if on.size else []


# --- the galvo line, the TTL line and the pixel grid ------------------------------------

@pytest.mark.parametrize('dwell_us', [10, 20, 30])
def test_on_the_grid_the_ttl_line_covers_the_galvo_line(dwell_us):
    params, (_sig, _pos, info) = _galvo(dwell_us)
    runs = _on_runs(_ttl(params, info))
    assert len(runs[0]) == info['scan_samples'][1] == PIXELS * info['samples_per_pixel']


@pytest.mark.parametrize('dwell_us', [15, 25, 12])
def test_off_the_grid_the_ttl_line_still_covers_the_galvo_line(dwell_us):
    # F1, fixed: the galvo used to sweep the requested dwell per pixel (the
    # line rounded as a whole) while the TTL designer gated the rounded
    # pixel -- at 15 µs the gate covered 20 of 30 samples of every line and
    # drifted a third of a line per line. Now the dwell is realized once.
    params, (_sig, _pos, info) = _galvo(dwell_us)
    runs = _on_runs(_ttl(params, info))
    assert len(runs[0]) == info['scan_samples'][1] == PIXELS * info['samples_per_pixel']
    assert runs[1][0] - runs[0][0] == info['scan_samples_d2_period']   # the TTL period


def test_the_off_grid_numbers_after_s0():
    """What 15 µs is now: rounded up to 20 µs (the advanced-compatible
    policy's rule), the line 40 samples, the gate 40 samples, the request
    kept beside the realized dwell."""
    params, (_sig, _pos, info) = _galvo(15)
    runs = _on_runs(_ttl(params, info))
    assert info['samples_per_pixel'] == 2
    assert info['scan_samples'][1] == 40 == len(runs[0])
    assert info['dwell_time'] == pytest.approx(20e-6)
    assert info['dwell_requested_s'] == pytest.approx(15e-6)
    assert runs[1][0] - runs[0][0] == info['scan_samples_d2_period']


@pytest.mark.parametrize('dwell_us', [10, 15, 20, 25])
def test_the_realized_dwell_is_the_recorded_dwell(dwell_us):
    # F2, fixed: dwell_time is what plays; the request is beside it only
    # when it differed.
    _params_, (_sig, _pos, info) = _galvo(dwell_us)
    assert info['samples_per_pixel'] * info['scan_time_step'] == pytest.approx(info['dwell_time'])
    on_grid = dwell_us % 10 == 0
    assert (info['dwell_requested_s'] is None) == on_grid


def test_the_designers_and_the_timing_policy_round_the_dwell_the_same_way():
    """One arithmetic (plan scan-timing, rule 3): the designers' rule is the
    advanced-compatible-v1 policy's step rule, and the converter calls the
    same function."""
    from imswitch.imcontrol.model.scan_parameters import (
        DWELL_ROUNDING, realized_dwell_s, samples_for_duration,
    )
    from imswitch.imcontrol.model.scan_program.policy import resolve_policy
    from imswitch.imcontrol.model.scan_program.schedule import realized_duration
    assert resolve_policy('advanced-compatible-v1').rounding['step'] == DWELL_ROUNDING
    for dwell in (1e-5, 1.5e-5, 2e-5, 2.5e-5, 3.3e-5, 1.2e-5):
        assert realized_duration(dwell, SCAN_CLOCK_RATE_HZ, 'advanced-compatible-v1')[0] \
            == realized_dwell_s(dwell, SCAN_CLOCK_RATE_HZ)
    assert samples_for_duration(2e-5, 1e5) == 2          # not 3: float noise snapped
    assert samples_for_duration(1.5e-5, 1e5) == 2        # ceil
    assert samples_for_duration(1.5e-5, 1e5, 'nearest-even') == 2
    with pytest.raises(ValueError, match='10 µs|1e-05 s'):
        samples_for_duration(1.5e-5, 1e5, 'exact')


def test_the_speed_check_runs_on_the_realized_dwell():
    """Rounding up never speeds the sweep, so a request the check accepts
    is accepted realized; the check names the realized dwell."""
    params = _params(['GalvoX', 'GalvoY'], [20.0, 5.0], [1.0, 1.0])
    params['sequence_time'] = 15e-6
    assert GalvoScanDesigner().scanSpeedRefusal(params, _setup()) == ''
    params['sequence_time'] = 5e-6                        # realized 1 sample: 10 µs, at the limit
    assert GalvoScanDesigner().scanSpeedRefusal(params, _setup()) == ''
    params['axis_step_size'][0] = 2.0                      # 0.2 µm/µs at the realized 10 µs
    refusal = GalvoScanDesigner().scanSpeedRefusal(params, _setup())
    assert 'at a 10 µs dwell' in refusal and 'vel_max of 0.1' in refusal


# --- the point detectors' bins -------------------------------------------------------------

@pytest.mark.parametrize('dwell_us', [10, 15, 20, 25])
def test_the_point_detector_bins_follow_the_galvo_line_not_the_pixel_grid(dwell_us):
    """The APD's arithmetic (APDManager.py:1150-1230): it bins the requested
    dwell at the timer rate and reads the line as the galvo plays it, so its
    pixel count is right even where the TTL line is not (F1)."""
    _params_, (_sig, _pos, info) = _galvo(dwell_us)
    frac_det_dwell = max(1, int(round(info['dwell_time'] * TIMER_COUNTER_RATE_HZ)))
    frac_scan_det = max(1, int(round(TIMER_COUNTER_RATE_HZ * info['scan_time_step'])))
    line_det_samples = round(info['scan_samples'][1]) * frac_scan_det
    assert frac_scan_det == TIMER_COUNTER_RATE_HZ // SCAN_CLOCK_RATE_HZ
    assert line_det_samples // frac_det_dwell == PIXELS


# --- below the grid ---------------------------------------------------------------------------

@pytest.mark.parametrize('dwell_us,step_um', [(1, 0.05), (2, 0.1), (5, 0.5)])
def test_a_dwell_below_the_sample_step_rounds_up_to_one_sample(dwell_us, step_um):
    """F3 at S0: at 100 kHz a 1 µs pixel is a tenth of a sample; it is
    realized as one sample (10 µs), said in the contract, and the speed
    check runs on that. A real 1 µs pixel needs the configurable clock (S1)."""
    params, (_sig, _pos, info) = _galvo(dwell_us, step_um)
    assert info['samples_per_pixel'] == 1
    assert info['dwell_time'] == pytest.approx(10e-6)
    assert info['dwell_requested_s'] == pytest.approx(dwell_us * 1e-6)
