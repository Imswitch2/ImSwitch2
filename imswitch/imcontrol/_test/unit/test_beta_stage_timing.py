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


def test_undeclared_stage_timing_is_warned_about_once(caplog):
    """2 ms is the MoNaLISA stage's timing, not every stage's."""
    designer = BetaScanDesigner()
    with caplog.at_level(logging.WARNING):
        designer.make_signal(_parameters(0.02), setupInfoBasic)
        designer.make_signal(_parameters(0.02), setupInfoBasic)
    undeclared = [r for r in caplog.records if 'does not declare move_time/settle_time' in r.getMessage()]
    assert len(undeclared) == 1
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        BetaScanDesigner().make_signal(
            _parameters(0.02, move_time=0.002, settle_time=0.002), setupInfoBasic
        )
    assert not any('does not declare' in r.getMessage() for r in caplog.records)


def test_shipped_beta_setups_declare_their_stage_timing():
    import json
    from pathlib import Path
    import imswitch
    setups = Path(imswitch.__file__).parent / '_data' / 'user_defaults' / 'imcontrol_setups'
    for path in setups.glob('*.json'):
        scan = json.loads(path.read_text(encoding='utf-8')).get('scan') or {}
        if scan.get('scanDesigner') != 'BetaScanDesigner':
            continue
        params = scan.get('scanDesignerParams') or {}
        assert 'move_time' in params and 'settle_time' in params, path.name


def test_timing_diagnostics_distinguish_return_from_move_and_settle():
    from imswitch.imcontrol.model.beta_scan_timing import (
        missing_beta_timing_keys, beta_scan_timing_ui_warning,
    )
    missing = missing_beta_timing_keys({'return_time': 0.03})
    assert missing == ('move_time', 'settle_time')
    message, tooltip = beta_scan_timing_ui_warning({'return_time': 0.03}, 0.005)
    assert '1 ms of 5 ms dwell' in message and 'defaults' in message
    assert 'return_time' not in tooltip
    message, _ = beta_scan_timing_ui_warning(
        {'return_time': 0.03, 'move_time': .0002, 'settle_time': .0002}, .005
    )
    assert not message


def test_missing_beta_timing_logs_at_scan_manager_startup(caplog):
    from dataclasses import replace
    from imswitch.imcontrol.model.managers.ScanManagerBase import ScanManagerBase
    setup = replace(
        setupInfoBasic,
        scan=replace(setupInfoBasic.scan, scanDesigner='BetaScanDesigner',
                     scanDesignerParams={'return_time': 0.03}),
    )
    with caplog.at_level(logging.WARNING):
        ScanManagerBase(setup)
    assert any('does not declare move_time/settle_time' in r.message for r in caplog.records)


def test_beta_scan_ui_warning_prioritizes_short_dwell_and_clears_when_valid():
    from imswitch.imcontrol.model.beta_scan_timing import beta_scan_timing_ui_warning

    old = {'return_time': .03}
    message, tooltip = beta_scan_timing_ui_warning(old, .005)
    assert 'Only 1 ms of 5 ms dwell is stationary' in message
    assert 'defaults' in message and 'does not declare' in tooltip

    message, _ = beta_scan_timing_ui_warning(old, .003)
    assert 'too short' in message

    explicit = {'return_time': .03, 'move_time': .0003, 'settle_time': .0002}
    message, details = beta_scan_timing_ui_warning(explicit, .003)
    assert message == details == ''
