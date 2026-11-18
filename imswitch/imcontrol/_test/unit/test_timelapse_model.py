import json
import math
from pathlib import Path

import pytest

from imswitch.imcontrol.model.SetupInfo import SetupInfo, TimelapseInfo
from imswitch.imcontrol.model.workflows.timelapse import (
    CadenceClock, IntervalMode, TimelapseKind, TimelapsePlan, point_attributes,
    timelapse_folder, timepoint_folder)

K, M = TimelapseKind, IntervalMode


def plan(**kw):
    base = dict(kind=K.CAMERA, timepoints=5, interval_s=10.0,
                interval_mode=M.START_TO_START, detectors=('cam',))
    base.update(kw)
    return TimelapsePlan(**base)


def test_valid_plans():
    assert plan().problems() == []
    plan().validate()
    assert plan(kind=K.TILING, detectors=()).problems() == []
    assert plan(kind=K.SCAN, lasers=()).problems() == []
    assert plan(lasers=(('a', None), ('b', 0.0))).problems() == []
    assert plan(lasers=(('a', 5.0),)).lasers_dict() == {'a': 5.0}


@pytest.mark.parametrize('kw', [
    dict(timepoints=0), dict(timepoints=True), dict(timepoints=3.0), dict(timepoints=2.5),
    dict(interval_s=-1.0), dict(interval_s=math.inf), dict(interval_s=math.nan),
    dict(frames_per_point=0), dict(frames_per_point=True),
    dict(kind=K.SCAN, frames_per_point=2), dict(kind=K.TILING, frames_per_point=2, detectors=()),
    dict(detectors=()), dict(kind=K.SCAN, detectors=()),
    dict(kind=K.SCAN, lasers=(('a', None),)),
    dict(lasers=(('a', 1.0), ('a', 2.0))), dict(lasers=(('', 1.0),)),
    dict(lasers=(('a', -1.0),)), dict(lasers=(('a', math.nan),)),
    dict(illumination_settle_ms=-1.0), dict(illumination_settle_ms=math.inf),
])
def test_invalid_plans(kw):
    p = plan(**kw)
    assert p.problems()
    with pytest.raises(ValueError):
        p.validate()


def test_scan_laser_message_and_frames_ok_for_camera():
    msg = '; '.join(plan(kind=K.SCAN, lasers=(('a', None),)).problems())
    assert 'scan program decides' in msg
    assert plan(frames_per_point=10, illumination_settle_ms=0.0).problems() == []


def test_clock_index_zero():
    c = CadenceClock(10, M.START_TO_START)
    s = c.next_start(0, run_start_s=100, now_s=102.5)
    assert (s.at_s, s.slip_s) == (100, 2.5)
    assert c.next_start(0, run_start_s=100, now_s=99).slip_s == 0


def test_clock_start_to_start_and_overrun():
    c = CadenceClock(10, M.START_TO_START)
    s = c.next_start(1, run_start_s=0, previous_start_s=0, previous_end_s=4, now_s=4)
    assert (s.at_s, s.slip_s) == (10, 0)
    s = c.next_start(1, run_start_s=0, previous_start_s=0, previous_end_s=14, now_s=14)
    assert (s.at_s, s.slip_s) == (14, 4)


def test_clock_end_to_start():
    c = CadenceClock(10, M.END_TO_START)
    s = c.next_start(1, run_start_s=0, previous_start_s=0, previous_end_s=4, now_s=4)
    assert (s.at_s, s.slip_s) == (14, 0)
    s = c.next_start(1, run_start_s=0, previous_start_s=0, previous_end_s=4, now_s=20)
    assert (s.at_s, s.slip_s) == (20, 6)


def test_clock_missing_previous():
    with pytest.raises(ValueError):
        CadenceClock(10, M.START_TO_START).next_start(1, run_start_s=0, previous_end_s=1, now_s=2)
    with pytest.raises(ValueError):
        CadenceClock(10, M.END_TO_START).next_start(1, run_start_s=0, previous_start_s=0, now_s=2)


def test_no_burst_across_three_overruns():
    c = CadenceClock(10, M.START_TO_START)
    start, now = 0.0, 0.0
    starts = [0.0]
    for i in range(1, 5):
        now = start + 25.0  # each point takes 25 s against a 10 s interval
        s = c.next_start(i, run_start_s=0, previous_start_s=start, now_s=now)
        assert s.at_s == now and s.slip_s == 15.0
        start = s.at_s
        starts.append(start)
    # each start is a full point duration after the last: no zero-gap catch-up
    assert [b - a for a, b in zip(starts, starts[1:])] == [25.0] * 4


def test_point_attributes_types_and_json():
    p = plan(lasers=(('a', 5.0),))
    a = point_attributes(p, 2, scheduled_s_since_start=20, slip_s=0.5,
                         lasers_applied={'a': 5.0, 'b': None}, settle_ms_applied=None)
    assert all(type(v) in (str, int, float, bool) for v in a.values())
    assert all(k.startswith('timelapse:') for k in a)
    assert json.loads(a['timelapse:lasers']) == {'a': 5.0, 'b': None}
    assert a['timelapse:illumination_settle_ms'] == 'none declared'
    assert a['timelapse:kind'] == 'camera' and a['timelapse:interval_mode'] == 'start_to_start'
    assert a['timelapse:index'] == 2 and a['timelapse:total'] == 5
    b = point_attributes(p, 0, scheduled_s_since_start=0, slip_s=0,
                         lasers_applied={}, settle_ms_applied=30)
    assert b['timelapse:illumination_settle_ms'] == 30.0


def test_folders(tmp_path):
    f = timelapse_folder(tmp_path)
    assert f.parent == tmp_path and f.name.startswith('timelapse_') and len(f.name) == len('timelapse_20260101_000000')
    assert timepoint_folder(f, 7, 120) == f / 't007'
    assert timepoint_folder(f, 3, 9).name == 't3'
    assert timepoint_folder(Path('r'), 0, 10).name == 't00'


def test_setupinfo_with_and_without_section():
    assert SetupInfo.from_dict({}).timelapse is None
    s = SetupInfo.from_dict({'timelapse': {'defaultTimepoints': 4, 'illuminationSettleMs': 20.0}})
    assert isinstance(s.timelapse, TimelapseInfo)
    assert s.timelapse.defaultTimepoints == 4 and s.timelapse.illuminationSettleMs == 20.0
    assert s.timelapse.defaultKind == 'camera' and TimelapseInfo().illuminationSettleMs is None
