"""The Beta stage designer's move and settle are declared, and bound the dwell."""

import logging

import numpy as np
import pytest

from imswitch.imcontrol._test import setupInfoBasic
from imswitch.imcontrol.model.signaldesigners.BetaScanDesigner import (
    BetaScanDesigner, DEFAULT_MOVE_TIME_S, DEFAULT_SETTLE_TIME_S,
)


def _parameters(dwell, **extra):
    params = {'target_device': ['X', 'Y', 'Z'],
              'axis_length': [5, 5, 0],
              'axis_step_size': [1, 1, 1],
              'axis_centerpos': [0, 0, 25],
              'axis_startpos': [[0], [0], [25]],
              'sequence_time': dwell,
              'return_time': 0.001,
              'phase_delay': 0}
    params.update(extra)
    return params


def test_a_dwell_the_stage_cannot_move_and_settle_inside_is_refused():
    """The ramp and settle occupied the last 4 ms of every dwell whatever its
    length, folding back over the previous pixels below 4 ms -- and 1 ms was
    the panel's default."""
    with pytest.raises(ValueError, match='1 ms dwell is not longer than the 2 ms move plus 2 ms settle'):
        BetaScanDesigner().make_signal(_parameters(0.001), setupInfoBasic)
    with pytest.raises(ValueError):
        BetaScanDesigner().make_signal(_parameters(DEFAULT_MOVE_TIME_S + DEFAULT_SETTLE_TIME_S), setupInfoBasic)


def test_a_fast_stage_declares_shorter_times():
    signals, _, info = BetaScanDesigner().make_signal(
        _parameters(0.001, move_time=0.0002, settle_time=0.0002), setupInfoBasic
    )
    assert info['img_dims'][0] == 5


def test_a_dwell_that_is_mostly_motion_is_warned_about(caplog):
    with caplog.at_level(logging.WARNING):
        BetaScanDesigner().make_signal(_parameters(0.005), setupInfoBasic)
    assert any('1 ms of each 5 ms dwell is stationary' in r.getMessage() for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        BetaScanDesigner().make_signal(_parameters(0.02), setupInfoBasic)
    assert not any('stationary' in r.getMessage() for r in caplog.records)


def test_the_fast_axis_rests_at_each_pixel_for_the_stationary_part():
    dwell = 0.02
    signals, _, info = BetaScanDesigner().make_signal(_parameters(dwell), setupInfoBasic)
    fast = np.asarray(signals['X'])
    rate = setupInfoBasic.scan.sampleRate
    per_pixel = int(dwell * rate)
    stationary = per_pixel - int((DEFAULT_MOVE_TIME_S + DEFAULT_SETTLE_TIME_S) * rate)
    first = fast[:stationary]
    assert np.allclose(first, first[0])
    second = fast[per_pixel:per_pixel + stationary]
    assert np.allclose(second, second[0]) and second[0] != first[0]


def test_the_scan_time_cap_is_honoured_by_the_stage_designer():
    designer = BetaScanDesigner()
    quick = _parameters(0.02)
    assert designer.estimateScanSeconds(quick, setupInfoBasic) == pytest.approx(5 * 5 * 0.02 + 5 * 0.001)
    assert designer.checkSignalLength(quick, setupInfoBasic)

    long = _parameters(2.0, axis_length=[50, 50, 0])
    capped = type(setupInfoBasic.scan)(**{**setupInfoBasic.scan.__dict__, 'maxScanTimeMin': 1})
    setup = type(setupInfoBasic)(**{**setupInfoBasic.__dict__, 'scan': capped})
    assert not designer.checkSignalLength(long, setup)
