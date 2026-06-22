"""Tests for TargetTimelapseWorkflow phase T2 target timelapse without modes.

Focused unit tests covering:
- Target visit order (A -> B -> C -> A over multiple timepoints)
- Disabled targets are skipped
- Continue-on-failure policy
- Abort-on-failure policy
- Interval scheduling without slow sleeps (deterministic timing via injected functions)
"""

import pytest

from imswitch.imcontrol.model.workflows import (
    TargetList,
    TargetTimelapseParams,
    TargetTimelapseWorkflow,
    build_mock_facade,
)


@pytest.mark.nohardware
def test_target_order_abc_over_multiple_timepoints():
    """Workflow visits enabled targets in order A -> B -> C -> A across timepoints."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual")  # id=1
    targets.add(stage_xy=(150.0, 250.0), source="manual")  # id=2
    targets.add(stage_xy=(200.0, 300.0), source="manual")  # id=3

    params = TargetTimelapseParams(n_timepoints=2, interval_s=0, settle_s=0)

    visited = []

    def callback(target, timepoint_idx):
        visited.append((target.id, timepoint_idx))
        return f"result_{target.id}_tp{timepoint_idx}"

    workflow = TargetTimelapseWorkflow(facade, targets, params, callback)
    result = workflow.run()

    assert visited == [
        (1, 0), (2, 0), (3, 0),
        (1, 1), (2, 1), (3, 1),
    ]
    assert len(result.acquisitions) == 6
    assert result.total_timepoints_completed == 2
    assert not result.aborted
    assert all(acq.success for acq in result.acquisitions)


@pytest.mark.nohardware
def test_disabled_targets_are_skipped():
    """Only enabled targets are visited; disabled targets are ignored."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual", enabled=True)   # id=1
    targets.add(stage_xy=(150.0, 250.0), source="manual", enabled=False)  # id=2 (disabled)
    targets.add(stage_xy=(200.0, 300.0), source="manual", enabled=True)   # id=3

    params = TargetTimelapseParams(n_timepoints=1)

    visited = []
    workflow = TargetTimelapseWorkflow(
        facade, targets, params, lambda t, tp: visited.append(t.id)
    )
    result = workflow.run()

    assert visited == [1, 3]
    assert len(result.acquisitions) == 2
    assert result.acquisitions[0].target_id == 1
    assert result.acquisitions[1].target_id == 3


@pytest.mark.nohardware
def test_continue_on_failure_policy():
    """With failure_policy='continue', acquisition errors are logged but workflow continues."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual")  # id=1
    targets.add(stage_xy=(150.0, 250.0), source="manual")  # id=2
    targets.add(stage_xy=(200.0, 300.0), source="manual")  # id=3

    params = TargetTimelapseParams(n_timepoints=2, failure_policy="continue")

    def callback(target, timepoint_idx):
        if target.id == 2 and timepoint_idx == 0:
            raise RuntimeError("Simulated failure at target 2, timepoint 0")
        return f"ok_{target.id}"

    workflow = TargetTimelapseWorkflow(facade, targets, params, callback)
    result = workflow.run()

    assert len(result.acquisitions) == 6
    assert result.total_timepoints_completed == 2
    assert not result.aborted

    acq_t2_tp0 = [a for a in result.acquisitions if a.target_id == 2 and a.timepoint == 0][0]
    assert not acq_t2_tp0.success
    assert "Simulated failure" in acq_t2_tp0.error
    assert acq_t2_tp0.callback_result is None

    successful = [a for a in result.acquisitions if a.success]
    assert len(successful) == 5


@pytest.mark.nohardware
def test_abort_on_failure_policy():
    """With failure_policy='abort', first acquisition error stops the workflow immediately."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual")  # id=1
    targets.add(stage_xy=(150.0, 250.0), source="manual")  # id=2
    targets.add(stage_xy=(200.0, 300.0), source="manual")  # id=3

    params = TargetTimelapseParams(n_timepoints=2, failure_policy="abort")

    def callback(target, timepoint_idx):
        if target.id == 2:
            raise RuntimeError("Failure at target 2")
        return f"ok_{target.id}"

    workflow = TargetTimelapseWorkflow(facade, targets, params, callback)
    result = workflow.run()

    assert result.aborted
    assert len(result.acquisitions) == 2
    assert result.acquisitions[0].target_id == 1
    assert result.acquisitions[0].success
    assert result.acquisitions[1].target_id == 2
    assert not result.acquisitions[1].success
    assert result.total_timepoints_completed == 0


@pytest.mark.nohardware
def test_interval_scheduling_deterministic():
    """Interval is measured between cycle starts; test uses fake time for determinism."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual")
    targets.add(stage_xy=(150.0, 250.0), source="manual")

    params = TargetTimelapseParams(n_timepoints=3, interval_s=10.0, settle_s=0.0)

    fake_time = [0.0]
    sleep_calls = []

    def fake_sleep(duration_s):
        sleep_calls.append(duration_s)
        fake_time[0] += duration_s

    def fake_time_fn():
        return fake_time[0]

    def callback(target, timepoint_idx):
        fake_time[0] += 2.0
        return "ok"

    workflow = TargetTimelapseWorkflow(
        facade, targets, params, callback, sleep_fn=fake_sleep, time_fn=fake_time_fn
    )
    result = workflow.run()

    assert len(result.acquisitions) == 6
    assert result.total_timepoints_completed == 3
    assert not result.aborted

    assert len(sleep_calls) == 2

    assert sleep_calls[0] == pytest.approx(6.0, abs=0.01)
    assert sleep_calls[1] == pytest.approx(6.0, abs=0.01)


@pytest.mark.nohardware
def test_settle_time_is_applied():
    """Settle time is applied after stage move and before acquisition callback."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual")

    params = TargetTimelapseParams(n_timepoints=1, settle_s=0.5)

    sleep_calls = []

    def fake_sleep(duration_s):
        sleep_calls.append(duration_s)

    workflow = TargetTimelapseWorkflow(
        facade, targets, params, lambda t, tp: None, sleep_fn=fake_sleep
    )
    workflow.run()

    assert 0.5 in sleep_calls


@pytest.mark.nohardware
def test_stage_movement_recorded():
    """Stage position is recorded at acquisition time (after move and settle)."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(123.5, 456.7), source="manual")

    params = TargetTimelapseParams(n_timepoints=1)

    workflow = TargetTimelapseWorkflow(facade, targets, params, lambda t, tp: "done")
    result = workflow.run()

    assert len(result.acquisitions) == 1
    assert result.acquisitions[0].stage_xy == pytest.approx((123.5, 456.7), abs=0.01)

    move_calls = [c for c in facade.calls if c[0] == "stage_con.move_to"]
    assert len(move_calls) == 1
    assert move_calls[0][1] == pytest.approx((123.5, 456.7), abs=0.01)


@pytest.mark.nohardware
def test_callback_result_is_recorded():
    """Callback return value is stored in acquisition metadata."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual")  # id=1

    params = TargetTimelapseParams(n_timepoints=2)

    def callback(target, timepoint_idx):
        return {"target_id": target.id, "timepoint": timepoint_idx, "data": [1, 2, 3]}

    workflow = TargetTimelapseWorkflow(facade, targets, params, callback)
    result = workflow.run()

    assert len(result.acquisitions) == 2
    assert result.acquisitions[0].callback_result == {
        "target_id": 1, "timepoint": 0, "data": [1, 2, 3]
    }
    assert result.acquisitions[1].callback_result == {
        "target_id": 1, "timepoint": 1, "data": [1, 2, 3]
    }


@pytest.mark.nohardware
def test_elapsed_time_is_calculated():
    """TargetAcquisitionResult.elapsed_s is difference between completed and started."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual")

    params = TargetTimelapseParams(n_timepoints=1, settle_s=0)

    fake_time = [0.0]

    def fake_time_fn():
        return fake_time[0]

    def callback(target, timepoint_idx):
        fake_time[0] += 3.5
        return "ok"

    workflow = TargetTimelapseWorkflow(
        facade, targets, params, callback, time_fn=fake_time_fn
    )
    result = workflow.run()

    assert len(result.acquisitions) == 1
    assert result.acquisitions[0].elapsed_s == pytest.approx(3.5, abs=0.01)


@pytest.mark.nohardware
def test_empty_target_list_returns_immediately():
    """Workflow with no enabled targets completes immediately without error."""
    facade = build_mock_facade()
    targets = TargetList()

    params = TargetTimelapseParams(n_timepoints=5)

    callback_called = []
    workflow = TargetTimelapseWorkflow(
        facade, targets, params, lambda t, tp: callback_called.append(True)
    )
    result = workflow.run()

    assert len(result.acquisitions) == 0
    assert result.total_timepoints_completed == 0
    assert not result.aborted
    assert len(callback_called) == 0


@pytest.mark.nohardware
def test_all_targets_disabled_returns_immediately():
    """Workflow with only disabled targets completes immediately."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual", enabled=False)
    targets.add(stage_xy=(150.0, 250.0), source="manual", enabled=False)

    params = TargetTimelapseParams(n_timepoints=3)

    workflow = TargetTimelapseWorkflow(facade, targets, params, lambda t, tp: "ok")
    result = workflow.run()

    assert len(result.acquisitions) == 0
    assert result.total_timepoints_completed == 0


@pytest.mark.nohardware
def test_invalid_n_timepoints_raises():
    """n_timepoints < 1 raises ValueError on construction."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual")

    params = TargetTimelapseParams(n_timepoints=0)

    with pytest.raises(ValueError, match="n_timepoints must be >= 1"):
        TargetTimelapseWorkflow(facade, targets, params, lambda t, tp: None)


@pytest.mark.nohardware
def test_invalid_interval_raises():
    """interval_s < 0 raises ValueError on construction."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual")

    params = TargetTimelapseParams(n_timepoints=1, interval_s=-5.0)

    with pytest.raises(ValueError, match="interval_s must be >= 0"):
        TargetTimelapseWorkflow(facade, targets, params, lambda t, tp: None)


@pytest.mark.nohardware
def test_invalid_settle_time_raises():
    """settle_s < 0 raises ValueError on construction."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual")

    params = TargetTimelapseParams(n_timepoints=1, settle_s=-0.1)

    with pytest.raises(ValueError, match="settle_s must be >= 0"):
        TargetTimelapseWorkflow(facade, targets, params, lambda t, tp: None)


@pytest.mark.nohardware
def test_invalid_failure_policy_raises():
    """Invalid failure policy raises ValueError on construction."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual")

    params = TargetTimelapseParams(n_timepoints=1, failure_policy="retry")

    with pytest.raises(ValueError, match="failure_policy"):
        TargetTimelapseWorkflow(facade, targets, params, lambda t, tp: None)


@pytest.mark.nohardware
def test_callback_invoked_exactly_once_per_enabled_target_per_timepoint():
    """Callback is called exactly once for each enabled target at each timepoint."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual", enabled=True)   # id=1
    targets.add(stage_xy=(150.0, 250.0), source="manual", enabled=False)  # id=2 (disabled)
    targets.add(stage_xy=(200.0, 300.0), source="manual", enabled=True)   # id=3
    targets.add(stage_xy=(250.0, 350.0), source="manual", enabled=True)   # id=4

    params = TargetTimelapseParams(n_timepoints=3)

    call_count = {}

    def callback(target, timepoint_idx):
        key = (target.id, timepoint_idx)
        call_count[key] = call_count.get(key, 0) + 1
        return "ok"

    workflow = TargetTimelapseWorkflow(facade, targets, params, callback)
    result = workflow.run()

    expected_calls = {
        (1, 0): 1, (1, 1): 1, (1, 2): 1,
        (3, 0): 1, (3, 1): 1, (3, 2): 1,
        (4, 0): 1, (4, 1): 1, (4, 2): 1,
    }
    assert call_count == expected_calls
    assert len(result.acquisitions) == 9


@pytest.mark.nohardware
def test_cycle_longer_than_interval_proceeds_immediately():
    """If cycle takes longer than interval, next cycle starts immediately without waiting."""
    facade = build_mock_facade()
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual")

    params = TargetTimelapseParams(n_timepoints=2, interval_s=5.0)

    fake_time = [0.0]
    sleep_calls = []

    def fake_sleep(duration_s):
        sleep_calls.append(duration_s)
        fake_time[0] += duration_s

    def fake_time_fn():
        return fake_time[0]

    def callback(target, timepoint_idx):
        fake_time[0] += 10.0
        return "ok"

    workflow = TargetTimelapseWorkflow(
        facade, targets, params, callback, sleep_fn=fake_sleep, time_fn=fake_time_fn
    )
    workflow.run()

    assert len(sleep_calls) == 0
