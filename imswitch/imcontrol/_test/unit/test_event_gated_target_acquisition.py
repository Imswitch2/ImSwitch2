"""Tests for event-gated target acquisition workflow primitives."""

import ast
import inspect

import numpy as np
import pytest

import imswitch.imcontrol.model.workflows.event_probe as event_probe_module
from imswitch.imcontrol.model.workflows import (
    EventGatedAcquisitionCallback,
    EventGatedAcquisitionResult,
    EventProbe,
    EventProbeParams,
    TargetList,
    TargetTimelapseParams,
    TargetTimelapseWorkflow,
    build_mock_facade,
)


def _target():
    targets = TargetList()
    return targets.add(stage_xy=(100.0, 200.0), source="manual")


def _frame_source(frame_count=5, *, error_at=None):
    state = {"idx": 0}

    def frame_source(target, timepoint, params):
        idx = state["idx"]
        if error_at is not None and idx == error_at:
            raise RuntimeError("Camera error")
        if idx >= frame_count:
            raise StopIteration
        state["idx"] += 1
        return np.zeros((64, 64))

    return frame_source


def _detector(detect_frames=(), *, coords=None, error_at=None):
    detect_frames = set(detect_frames)

    def detector(frame, idx, params):
        if error_at is not None and idx == error_at:
            raise RuntimeError("Simulated detector failure")
        if idx in detect_frames:
            detected = coords(idx) if callable(coords) else coords
            if detected is None:
                detected = np.array([10.0, 20.0])
            return {
                "coords_detected": detected,
                "analysis_metadata": {"frame_idx": idx},
            }
        return {"coords_detected": None, "analysis_metadata": {"frame_idx": idx}}

    return detector


def _probe(
    detect_frames=(),
    *,
    params=None,
    frame_count=5,
    frame_error_at=None,
    detector_error_at=None,
    coords=None,
):
    params = params or EventProbeParams(max_frames=frame_count)
    return EventProbe(
        _frame_source(frame_count=frame_count, error_at=frame_error_at),
        _detector(detect_frames, coords=coords, error_at=detector_error_at),
        params,
    )


def _three_targets():
    targets = TargetList()
    targets.add(stage_xy=(100.0, 200.0), source="manual")
    targets.add(stage_xy=(150.0, 250.0), source="manual")
    targets.add(stage_xy=(200.0, 300.0), source="manual")
    return targets


@pytest.mark.nohardware
def test_event_probe_params_validation():
    with pytest.raises(ValueError, match="max_frames must be >= 1"):
        EventProbeParams(max_frames=0)

    with pytest.raises(ValueError, match="min_warmup_frames must be >= 0"):
        EventProbeParams(max_frames=10, min_warmup_frames=-1)

    with pytest.raises(ValueError, match="min_warmup_frames .* must be <"):
        EventProbeParams(max_frames=10, min_warmup_frames=10)

    params = EventProbeParams(max_frames=5, min_warmup_frames=2)
    assert params.max_frames == 5
    assert params.min_warmup_frames == 2


@pytest.mark.nohardware
def test_event_probe_detects_event_after_warmup():
    params = EventProbeParams(max_frames=10, min_warmup_frames=2)
    probe = _probe(detect_frames={3}, params=params, frame_count=10)

    result = probe.probe(_target(), 0)

    assert result.detected is True
    assert result.frames_seen == 10
    assert result.first_detected_frame == 3
    assert result.first_coord == (10.0, 20.0)
    assert result.all_coords.shape == (1, 2)
    assert result.error is None


@pytest.mark.nohardware
def test_event_probe_ignores_event_during_warmup():
    params = EventProbeParams(max_frames=10, min_warmup_frames=5)
    probe = _probe(detect_frames={2}, params=params, frame_count=10)

    result = probe.probe(_target(), 0)

    assert result.detected is False
    assert result.frames_seen == 10
    assert result.first_detected_frame is None
    assert result.first_coord is None
    assert result.all_coords.shape == (0, 2)


@pytest.mark.nohardware
def test_event_probe_no_event_detected():
    params = EventProbeParams(max_frames=5, min_warmup_frames=1)
    probe = _probe(params=params, frame_count=5)

    result = probe.probe(_target(), 0)

    assert result.detected is False
    assert result.frames_seen == 5
    assert result.first_detected_frame is None
    assert result.first_coord is None
    assert result.all_coords.shape == (0, 2)
    assert result.error is None


@pytest.mark.nohardware
def test_event_probe_records_multiple_events():
    params = EventProbeParams(max_frames=10, min_warmup_frames=2)
    probe = _probe(
        detect_frames={3, 5, 7},
        params=params,
        frame_count=10,
        coords=lambda idx: np.array([[float(idx), float(idx * 2)]]),
    )

    result = probe.probe(_target(), 0)

    assert result.detected is True
    assert result.frames_seen == 10
    assert result.first_detected_frame == 3
    assert result.first_coord == (3.0, 6.0)
    np.testing.assert_array_equal(
        result.all_coords,
        [[3.0, 6.0], [5.0, 10.0], [7.0, 14.0]],
    )


@pytest.mark.nohardware
def test_event_probe_detector_error_is_recorded():
    probe = _probe(params=EventProbeParams(max_frames=5), detector_error_at=2)

    result = probe.probe(_target(), 0)

    assert result.detected is False
    assert result.frames_seen == 3
    assert result.first_detected_frame is None
    assert result.error == "Simulated detector failure"


@pytest.mark.nohardware
def test_event_probe_frame_source_error_is_recorded():
    probe = _probe(params=EventProbeParams(max_frames=5), frame_error_at=2)

    result = probe.probe(_target(), 0)

    assert result.detected is False
    assert result.frames_seen == 2
    assert result.first_detected_frame is None
    assert result.error == "Camera error"


@pytest.mark.nohardware
def test_event_probe_stop_iteration_stops_early():
    probe = _probe(params=EventProbeParams(max_frames=10), frame_count=3)

    result = probe.probe(_target(), 0)

    assert result.detected is False
    assert result.frames_seen == 3
    assert result.error is None


@pytest.mark.nohardware
def test_event_gated_callback_skips_acquisition_when_no_event():
    probe = _probe(params=EventProbeParams(max_frames=5))
    positive_called = []

    def positive_acquisition(target, timepoint, probe_result):
        positive_called.append((target.id, timepoint))
        return "positive_result"

    callback = EventGatedAcquisitionCallback(probe, positive_acquisition)
    result = callback(_target(), 0)

    assert isinstance(result, EventGatedAcquisitionResult)
    assert result.probe_result.detected is False
    assert result.acquisition_triggered is False
    assert result.acquisition_result is None
    assert result.acquisition_error is None
    assert positive_called == []


@pytest.mark.nohardware
def test_event_gated_callback_triggers_acquisition_once_when_event_detected():
    probe = _probe(detect_frames={2}, params=EventProbeParams(max_frames=5))
    positive_called = []

    def positive_acquisition(target, timepoint, probe_result):
        positive_called.append((target.id, timepoint, probe_result.first_detected_frame))
        return f"acquired_t{target.id}_tp{timepoint}"

    callback = EventGatedAcquisitionCallback(probe, positive_acquisition)
    result = callback(_target(), 0)

    assert result.probe_result.detected is True
    assert result.probe_result.first_detected_frame == 2
    assert result.acquisition_triggered is True
    assert result.acquisition_result == "acquired_t1_tp0"
    assert result.acquisition_error is None
    assert positive_called == [(1, 0, 2)]


@pytest.mark.nohardware
def test_event_gated_callback_positive_acquisition_error_propagates():
    probe = _probe(detect_frames={2}, params=EventProbeParams(max_frames=5))

    def positive_acquisition(target, timepoint, probe_result):
        raise RuntimeError("Scan trigger failed")

    callback = EventGatedAcquisitionCallback(probe, positive_acquisition)

    with pytest.raises(RuntimeError, match="Scan trigger failed"):
        callback(_target(), 0)


@pytest.mark.nohardware
def test_event_gated_callback_probe_error_raises_by_default():
    probe = _probe(params=EventProbeParams(max_frames=5), frame_error_at=2)
    positive_called = []

    def positive_acquisition(target, timepoint, probe_result):
        positive_called.append((target.id, timepoint))
        return "positive_result"

    callback = EventGatedAcquisitionCallback(probe, positive_acquisition)

    with pytest.raises(RuntimeError, match="Event probe failed: Camera error"):
        callback(_target(), 0)
    assert positive_called == []


@pytest.mark.nohardware
def test_event_gated_callback_probe_error_can_skip_when_configured():
    probe = _probe(params=EventProbeParams(max_frames=5), frame_error_at=2)
    positive_called = []

    def positive_acquisition(target, timepoint, probe_result):
        positive_called.append((target.id, timepoint))
        return "positive_result"

    callback = EventGatedAcquisitionCallback(
        probe, positive_acquisition, fail_on_probe_error=False
    )

    result = callback(_target(), 0)

    assert result.probe_result.error == "Camera error"
    assert result.acquisition_triggered is False
    assert positive_called == []


@pytest.mark.nohardware
def test_abc_cycling_only_selected_targets_trigger_acquisition():
    facade = build_mock_facade()
    targets = _three_targets()
    probe_params = EventProbeParams(max_frames=5)
    events_detected = {
        (1, 0): True,
        (1, 1): False,
        (2, 0): False,
        (2, 1): True,
        (3, 0): True,
        (3, 1): True,
    }
    acquisitions_triggered = []

    def callback(target, timepoint):
        detect_frames = {2} if events_detected[(target.id, timepoint)] else set()
        probe = _probe(detect_frames=detect_frames, params=probe_params)

        def positive_acquisition(tgt, tp, probe_result):
            acquisitions_triggered.append((tgt.id, tp))
            return f"acquired_{tgt.id}_{tp}"

        return EventGatedAcquisitionCallback(probe, positive_acquisition)(
            target, timepoint
        )

    workflow = TargetTimelapseWorkflow(
        facade,
        targets,
        TargetTimelapseParams(n_timepoints=2, interval_s=0),
        callback,
    )
    result = workflow.run()

    assert result.total_timepoints_completed == 2
    assert not result.aborted
    assert len(result.acquisitions) == 6
    assert acquisitions_triggered == [(1, 0), (3, 0), (2, 1), (3, 1)]

    for acquisition in result.acquisitions:
        expected = events_detected[(acquisition.target_id, acquisition.timepoint)]
        assert acquisition.success
        assert acquisition.callback_result.acquisition_triggered is expected


def _event_positive_callback(probe_params, acquisitions_attempted):
    def callback(target, timepoint):
        probe = _probe(detect_frames={2}, params=probe_params)

        def positive_acquisition(tgt, tp, probe_result):
            acquisitions_attempted.append((tgt.id, tp))
            if tgt.id == 2 and tp == 0:
                raise RuntimeError("Scan trigger failed at target 2")
            return f"ok_{tgt.id}_{tp}"

        return EventGatedAcquisitionCallback(probe, positive_acquisition)(
            target, timepoint
        )

    return callback


@pytest.mark.nohardware
def test_positive_callback_error_propagates_to_workflow_abort():
    acquisitions_attempted = []
    workflow = TargetTimelapseWorkflow(
        build_mock_facade(),
        _three_targets(),
        TargetTimelapseParams(n_timepoints=2, interval_s=0, failure_policy="abort"),
        _event_positive_callback(EventProbeParams(max_frames=5), acquisitions_attempted),
    )

    result = workflow.run()

    assert result.aborted
    assert result.total_timepoints_completed == 0
    assert acquisitions_attempted == [(1, 0), (2, 0)]
    assert len(result.acquisitions) == 2
    assert result.acquisitions[0].success
    assert result.acquisitions[1].success is False
    assert "Scan trigger failed" in result.acquisitions[1].error


@pytest.mark.nohardware
def test_positive_callback_error_with_continue_policy():
    acquisitions_attempted = []
    workflow = TargetTimelapseWorkflow(
        build_mock_facade(),
        _three_targets(),
        TargetTimelapseParams(n_timepoints=2, interval_s=0, failure_policy="continue"),
        _event_positive_callback(EventProbeParams(max_frames=5), acquisitions_attempted),
    )

    result = workflow.run()

    assert not result.aborted
    assert result.total_timepoints_completed == 2
    assert acquisitions_attempted == [
        (1, 0),
        (2, 0),
        (3, 0),
        (1, 1),
        (2, 1),
        (3, 1),
    ]
    assert len(result.acquisitions) == 6
    failed_acq = [
        acq
        for acq in result.acquisitions
        if acq.target_id == 2 and acq.timepoint == 0
    ][0]
    assert not failed_acq.success
    assert "Scan trigger failed" in failed_acq.error


@pytest.mark.nohardware
def test_probe_error_obeys_workflow_continue_policy():
    acquisitions_attempted = []

    def callback(target, timepoint):
        probe = _probe(
            detect_frames={2},
            params=EventProbeParams(max_frames=5),
            frame_error_at=1 if (target.id, timepoint) == (2, 0) else None,
        )

        def positive_acquisition(tgt, tp, probe_result):
            acquisitions_attempted.append((tgt.id, tp))
            return f"ok_{tgt.id}_{tp}"

        return EventGatedAcquisitionCallback(probe, positive_acquisition)(
            target, timepoint
        )

    workflow = TargetTimelapseWorkflow(
        build_mock_facade(),
        _three_targets(),
        TargetTimelapseParams(n_timepoints=2, interval_s=0, failure_policy="continue"),
        callback,
    )

    result = workflow.run()

    assert not result.aborted
    assert result.total_timepoints_completed == 2
    assert len(result.acquisitions) == 6
    assert acquisitions_attempted == [(1, 0), (3, 0), (1, 1), (2, 1), (3, 1)]
    failed_acq = [
        acq
        for acq in result.acquisitions
        if acq.target_id == 2 and acq.timepoint == 0
    ][0]
    assert not failed_acq.success
    assert "Event probe failed: Camera error" in failed_acq.error


@pytest.mark.nohardware
def test_event_probe_module_has_no_controller_or_qt_imports():
    tree = ast.parse(inspect.getsource(event_probe_module))
    disallowed_prefixes = (
        "imswitch.imcontrol.controller",
        "imswitch.imcontrol.view",
        "qtpy",
        "PyQt5",
        "PySide",
    )

    imported_modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    assert not [
        module
        for module in imported_modules
        if module.startswith(disallowed_prefixes)
    ]
