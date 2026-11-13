"""Performance overrides alter execution decisions, not fitted image signals.

The extraction-level tests deliberately require neither a GUI nor a GPU.
"""

import numpy as np
import pytest

from imswitch.improcess.reconstructors.monalisa.extraction import ExtractionOperator
from imswitch.improcess.reconstructors.monalisa.fast_execution import _extract_fast_recording
from imswitch.improcess.reconstructors.monalisa.local_extraction import LocalExtractionOperator
from imswitch.improcess.reconstructors.monalisa.params import default_params


def _frames():
    yy, xx = np.mgrid[:31, :31]
    frames = []
    for amplitude in (1.0, 1.2, 2.0, 1.4, 2.4, 2.1):
        frames.append((12 + amplitude * 80 * np.exp(-((xx - 9.3)**2 + (yy - 10.7)**2)/6)
                       + amplitude * 35 * np.exp(-((xx - 22.0)**2 + (yy - 20.0)**2)/6)))
    return np.asarray(frames)


def _run(frames, operator, *, amplitude_only, groups=None):
    logs = []
    result = _extract_fast_recording(
        frames, operator, "CPU", lambda *args: None, lambda: None,
        amplitude_only=amplitude_only, frame_groups=groups,
    )
    return result


@pytest.mark.parametrize("estimator", ["local", "joint"])
def test_amplitude_only_and_batching_parity(estimator):
    frames = _frames()
    x, y = [9.3, 22.0], [10.7, 20.0]
    if estimator == "local":
        operator = LocalExtractionOperator.build(
            x, y, 1.75, frames.shape[1:], background="constant",
            footprint_shape="rectangular", footprint_size=9,
            sampling="camera_pixels",
        )
    else:
        operator = ExtractionOperator.build(
            x, y, 1.75, frames.shape[1:], background="constant",
            reach_sigma=2.5,
        )
    grouped = [np.array([0, 2, 4]), np.array([1, 3, 5])]
    baseline = _run(frames, operator, amplitude_only=True)
    no_batch = _run(frames, operator, amplitude_only=True, groups=grouped)
    no_amplitude_only = _run(frames, operator, amplitude_only=False, groups=grouped)
    np.testing.assert_allclose(no_batch.amplitude, baseline.amplitude, rtol=1e-8, atol=1e-9)
    np.testing.assert_allclose(no_amplitude_only.amplitude, baseline.amplitude,
                               rtol=1e-8, atol=1e-9)
    np.testing.assert_allclose(no_batch.background, baseline.background,
                               rtol=1e-8, atol=1e-9)
    np.testing.assert_allclose(no_amplitude_only.background, baseline.background,
                               rtol=1e-8, atol=1e-9)


def test_disable_batching_changes_number_and_order_of_solver_calls():
    class Operator:
        def __init__(self):
            self.calls = []
        def apply_fast_device(self, batch, device):
            self.calls.append(("fast", batch[:, 0, 0].tolist()))
            from imswitch.improcess.reconstructors.monalisa.extraction import ExtractionResult
            n = len(batch)
            return ExtractionResult(batch[:, 0, 0].reshape(n, 1).copy(), None, None,
                                    np.full(n, np.nan), np.empty((n, 0)))
        def apply_device(self, batch, device):
            self.calls.append(("full", batch[:, 0, 0].tolist()))
            return self.apply_fast_device(batch, device)

    frames = np.arange(6, dtype=float).reshape(6, 1, 1)
    op = Operator()
    baseline = _run(frames, op, amplitude_only=True)
    assert len(op.calls) == 1
    op.calls.clear()
    grouped = _run(frames, op, amplitude_only=True,
                   groups=[np.array([0, 2, 4]), np.array([1, 3, 5])])
    assert len(op.calls) == 2
    np.testing.assert_array_equal(baseline.amplitude, grouped.amplitude)
    assert op.calls[0][1] == [0, 2, 4]
    assert op.calls[1][1] == [1, 3, 5]


def test_invalid_groups_fail_closed():
    frames = np.ones((3, 1, 1))
    class Operator:
        def apply_fast_device(self, batch, device):
            from imswitch.improcess.reconstructors.monalisa.extraction import ExtractionResult
            n = len(batch)
            return ExtractionResult(np.ones((n, 1)), None, None,
                                    np.full(n, np.nan), np.empty((n, 0)))
    with pytest.raises(ValueError, match="cover"):
        _run(frames, Operator(), amplitude_only=True, groups=[[0, 1]])
    with pytest.raises(ValueError, match="duplicate"):
        _run(frames, Operator(), amplitude_only=True, groups=[[0, 1], [1, 2]])


def test_defaults_leave_optimization_enabled_and_timing_off():
    params = default_params()
    assert params["performance_amplitude_only"] == "auto"
    assert params["performance_batch_extraction"] == "auto"
    assert params["performance_placement_reuse"] == "auto"
    assert params["show_reconstruction_timings"] is False
