"""The scan timing grids (docs/design/plans/scan-timing-configurable-dwell.md,
findings F1-F3): where the pixel grid, the galvo line, the TTL lines and the
point detectors' bins agree, and where they do not. The failing cases are
the reproductions of the plan's findings, marked xfail(strict) so they flip
when the plan is built; the passing cases are the controls at dwells that are
whole multiples of the 10 µs sample step.

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

F1 = 'plan scan-timing F1: the pixel and the line are rounded separately'
F2 = 'plan scan-timing F2: an off-grid dwell is quantized silently'

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


@pytest.mark.xfail(strict=True, reason=F1)
@pytest.mark.parametrize('dwell_us', [15, 25, 12])
def test_off_the_grid_the_ttl_line_is_shorter_than_the_galvo_line(dwell_us):
    # The galvo sweeps the requested dwell per pixel (the line is rounded as
    # a whole), the TTL designer builds the line from the rounded per-pixel
    # count: at 15 µs the laser gate covers 20 of 30 samples of every line
    # and the TTL period is 10 samples short of the galvo period, so the
    # gate drifts a third of a line per line.
    params, (_sig, _pos, info) = _galvo(dwell_us)
    runs = _on_runs(_ttl(params, info))
    assert len(runs[0]) == info['scan_samples'][1]


def test_the_off_grid_mismatch_in_numbers():
    """The reproduction's arithmetic, pinned: what the plan's F1 fixes."""
    params, (_sig, _pos, info) = _galvo(15)
    ttl = _ttl(params, info)
    runs = _on_runs(ttl)
    assert info['samples_per_pixel'] == 1                 # 10 µs on the pixel grid
    assert info['scan_samples'][1] == 30                  # 20 px x 1.5 samples: 15 µs swept
    assert len(runs[0]) == 20                             # the gate: 20 px x 1 sample
    period = runs[1][0] - runs[0][0]                      # TTL period: line + flyback pad
    assert period == info['scan_samples_d2_period'] - 10  # 10 samples short of the galvo's


@pytest.mark.xfail(strict=True, reason=F2)
@pytest.mark.parametrize('dwell_us', [15, 25])
def test_an_off_grid_dwell_is_refused_or_realized_as_recorded(dwell_us):
    # Neither refused nor recorded as realized: the contract says dwell_time
    # 15 µs and samples_per_pixel 1 (10 µs) in the same breath.
    _params_, (_sig, _pos, info) = _galvo(dwell_us)
    realized = info['samples_per_pixel'] * info['scan_time_step']
    assert realized == pytest.approx(info['dwell_time'])


# --- the point detectors' bins -------------------------------------------------------------

@pytest.mark.parametrize('dwell_us', [10, 15, 20, 25])
def test_the_point_detector_bins_follow_the_galvo_line_not_the_pixel_grid(dwell_us):
    """The APD's arithmetic (APDManager.py:1150-1230): it bins the requested
    dwell at the timer rate and reads the line as the galvo plays it, so its
    pixel count is right even where the TTL line is not (F1)."""
    params, (_sig, _pos, info) = _galvo(dwell_us)
    frac_det_dwell = max(1, int(round(params['sequence_time'] * TIMER_COUNTER_RATE_HZ)))
    frac_scan_det = max(1, int(round(TIMER_COUNTER_RATE_HZ * info['scan_time_step'])))
    line_det_samples = round(info['scan_samples'][1]) * frac_scan_det
    assert frac_scan_det == TIMER_COUNTER_RATE_HZ // SCAN_CLOCK_RATE_HZ
    assert line_det_samples // frac_det_dwell == PIXELS


# --- below the grid ---------------------------------------------------------------------------

@pytest.mark.parametrize('dwell_us,step_um', [(1, 0.05), (2, 0.1), (5, 0.5)])
def test_a_dwell_below_the_sample_step_is_refused_not_played(dwell_us, step_um):
    """F3: at 100 kHz a 1 µs pixel is a tenth of a sample. The designer
    refuses (the contract's positive-samples rule) rather than playing
    something else -- after the velocity check, with a message about the
    contract, not about the clock."""
    with pytest.raises(ValueError, match='scan_samples values must be positive'):
        _galvo(dwell_us, step_um)
