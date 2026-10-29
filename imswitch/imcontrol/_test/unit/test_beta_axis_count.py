"""The Beta stage designer scans setups with fewer than three scanning stages.

target_device carries one entry per forScanning positioner, and the designer
refused anything but three -- so a one- or two-stage rig could not scan at
all, although the scans it designs never need a third axis.
"""

import numpy as np
import pytest

from imswitch.imcontrol._test import setupInfoBasic
from imswitch.imcontrol.model.signaldesigners.BetaScanDesigner import BetaScanDesigner


def _parameters(n, **extra):
    params = {'target_device': ['X', 'Y', 'Z'][:n],
              'axis_length': [5, 4, 0][:n],
              'axis_step_size': [1, 1, 1][:n],
              'axis_centerpos': [0, 0, 0][:n],
              'axis_startpos': [[0], [0], [0]][:n],
              'sequence_time': 0.02,
              'return_time': 0.001,
              'phase_delay': 0}
    params.update(extra)
    return params


def test_a_two_stage_rig_scans_like_a_three_stage_rig_with_a_flat_slow_axis():
    reference, refPositions, refInfo = BetaScanDesigner().make_signal(_parameters(3), setupInfoBasic)
    signals, positions, info = BetaScanDesigner().make_signal(_parameters(2), setupInfoBasic)
    assert list(signals) == ['X', 'Y']
    for name in signals:
        assert np.array_equal(signals[name], reference[name])
    assert positions == refPositions == [5, 4]
    assert info['img_dims'] == refInfo['img_dims']
    assert info['minmaxes'] == refInfo['minmaxes']


def test_a_one_stage_rig_drives_only_its_stage():
    middleFlat = _parameters(3, axis_length=[5, 0, 0])
    reference, refPositions, refInfo = BetaScanDesigner().make_signal(middleFlat, setupInfoBasic)
    signals, positions, info = BetaScanDesigner().make_signal(
        _parameters(1, axis_length=[5]), setupInfoBasic
    )
    assert list(signals) == ['X']
    assert np.array_equal(signals['X'], reference['X'])
    assert positions == refPositions == [5, 1]
    assert info['minmaxes'] == refInfo['minmaxes'][:1]
    assert BetaScanDesigner().checkSignalComp(_parameters(1), setupInfoBasic, info)


@pytest.mark.parametrize('n', [1, 2])
def test_a_position_snapshot_needs_one_entry_per_real_stage(n):
    snapshot = [[10], [20]][:n]
    signals, _, _ = BetaScanDesigner().make_signal(
        _parameters(n, axis_position_before_scan=snapshot), setupInfoBasic
    )
    assert list(signals) == ['X', 'Y'][:n]
    with pytest.raises(ValueError, match='one entry per target device'):
        BetaScanDesigner().make_signal(
            _parameters(n, axis_position_before_scan=[[10], [20], [30]]), setupInfoBasic
        )


def test_more_than_three_stages_is_refused_with_the_reason():
    params = _parameters(3)
    for key, extra in (('target_device', 'W'), ('axis_length', 0), ('axis_step_size', 1),
                       ('axis_centerpos', 0), ('axis_startpos', [0])):
        params[key] = params[key] + [extra]
    with pytest.raises(ValueError, match='one to three axes .* got 4 target devices'):
        BetaScanDesigner().make_signal(params, setupInfoBasic)
