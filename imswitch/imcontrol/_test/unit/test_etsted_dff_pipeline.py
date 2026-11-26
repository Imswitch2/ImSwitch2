"""The shipped dF/F analysis pipeline, its coordinate convention, and the
affine -> coefficient-file helper.
"""
import importlib.util
import time
from collections import deque
from pathlib import Path

import numpy as np
import pytest

from imswitch.imcontrol.model.EtSTEDPipelineRunner import EtSTEDPipelineRunner
from imswitch.imcontrol.model.EtSTEDTransformService import EtSTEDTransformService
from imswitch.imcontrol.model.EtSTEDTriggeredScanRunner import EtSTEDTriggeredScanRunner

DEFAULTS = Path(__file__).resolve().parents[3] / '_data' / 'user_defaults' / 'imcontrol_etsted'
PIPELINE = DEFAULTS / 'analysis_pipelines' / 'dff_event.py'


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dff_event = _load(PIPELINE, 'dff_event').dff_event
SHAPE = (256, 320)          # rows, columns: not square, so row/column mix-ups show


class Stream:
    """Feeds frames through the pipeline the way the controller does."""

    def __init__(self, mask=None, testmode=False, **params):
        self.exinfo = None
        self.mask = mask
        self.testmode = testmode
        self.params = params
        self.prev = deque(maxlen=10)
        self.rng = np.random.default_rng(1)
        self.last = None

    def frame(self, gain=1.0, spikes=(), base=200.0):
        image = self.rng.poisson(base * gain, SHAPE).astype(np.float32)
        yy, xx = np.mgrid[:SHAPE[0], :SHAPE[1]]
        for row, col, amplitude in spikes:
            image += amplitude * np.exp(-((yy - row) ** 2 + (xx - col) ** 2) / (2 * 4.0 ** 2))
        return image

    def push(self, image):
        result = dff_event(image, self.prev, self.mask, self.testmode, self.exinfo,
                           **self.params)
        self.exinfo = result[1]
        self.prev.append(image)
        self.last = result
        return np.asarray(result[0])

    def run(self, frames, **frame_args):
        return [self.push(self.frame(**frame_args(i) if callable(frame_args) else frame_args))
                for i in range(frames)]


def test_a_quiet_noisy_sample_never_triggers():
    stream = Stream()
    assert all(c.size == 0 for c in stream.run(150))


def test_slow_bleaching_and_drift_do_not_trigger():
    stream = Stream()
    bleach = [stream.push(stream.frame(gain=0.995 ** i)) for i in range(200)]
    drift = [stream.push(stream.frame(gain=0.995 ** 200 * (1 + 0.004 * i))) for i in range(150)]
    assert all(c.size == 0 for c in bleach + drift)


def test_a_local_rise_triggers_at_its_position_as_column_then_row():
    stream = Stream()
    stream.run(30)
    coords = stream.push(stream.frame(spikes=[(60, 200, 400.0)]))
    assert coords.shape[1] == 2 and len(coords) >= 1
    column, row = coords[0]
    # (c1, c2) = (column, row) of the full-resolution frame, within a bin
    assert column == pytest.approx(200, abs=4) and row == pytest.approx(60, abs=4)


def test_candidates_come_strongest_first_and_at_most_five():
    stream = Stream()
    stream.run(30)
    spikes = [(40, 50, 300.0), (150, 250, 900.0), (200, 120, 500.0)]
    coords = stream.push(stream.frame(spikes=spikes))
    assert len(coords) == 3
    order = [(round(c), round(r)) for c, r in coords]
    assert abs(order[0][0] - 250) <= 4 and abs(order[0][1] - 150) <= 4
    assert abs(order[1][0] - 120) <= 4 and abs(order[2][0] - 50) <= 4
    many = [(20 + 40 * i, 20 + 50 * i, 400.0 + 10 * i) for i in range(6)]
    assert len(stream.push(stream.frame(spikes=many))) <= 5


def test_the_binary_mask_is_respected():
    mask = np.ones(SHAPE, bool)
    mask[100:200, 200:300] = False                     # the strong spike is masked out
    stream = Stream(mask=mask)
    stream.run(30)
    coords = stream.push(stream.frame(spikes=[(150, 250, 900.0), (40, 50, 400.0)]))
    assert len(coords) == 1
    assert coords[0][0] == pytest.approx(50, abs=4) and coords[0][1] == pytest.approx(40, abs=4)


def test_a_mask_that_does_not_fit_is_an_error_not_ignored():
    stream = Stream(mask=np.ones((10, 10), bool))
    stream.push(stream.frame())
    with pytest.raises(ValueError, match='binary mask'):
        stream.push(stream.frame())


def test_warmup_frames_only_build_the_baseline():
    stream = Stream(warmup_frames=20)
    coords = [stream.push(stream.frame(spikes=[(60, 200, 600.0)])) for _ in range(15)]
    assert all(c.size == 0 for c in coords)


def test_a_standing_event_keeps_triggering_because_its_baseline_is_not_updated():
    stream = Stream()
    stream.run(30)
    hits = [len(stream.push(stream.frame(spikes=[(60, 200, 400.0)]))) for _ in range(40)]
    assert all(hit >= 1 for hit in hits)


def test_dark_pixels_do_not_trigger_through_min_baseline():
    stream = Stream(min_baseline=50.0)
    stream.run(30, base=2.0)
    coords = stream.push(stream.frame(base=8.0))      # x4 on a dark background
    assert coords.size == 0


def test_testmode_returns_the_dff_map_and_experiment_mode_does_not():
    stream = Stream(testmode=True)
    stream.run(5)
    assert len(stream.last) == 3 and stream.last[2].ndim == 2
    stream = Stream(testmode=False)
    stream.run(5)
    assert len(stream.last) == 2


def test_the_pipeline_loads_in_the_runner_with_its_parameters():
    import sys
    sys.path.insert(0, str(PIPELINE.parent))
    try:
        runner = EtSTEDPipelineRunner()
        runner.load('dff_event')
        assert runner.get_user_parameter_names() == [
            'dff_threshold', 'bin_px', 'baseline_frames', 'smooth_px',
            'min_baseline', 'warmup_frames']
    finally:
        sys.path.remove(str(PIPELINE.parent))
        sys.modules.pop('dff_event', None)


@pytest.mark.parametrize('side', [512, 1024, 2048])
def test_per_frame_cost_guard(side):
    rng = np.random.default_rng(0)
    frames = [rng.poisson(200, (side, side)).astype(np.float32) for _ in range(3)]
    exinfo, prev = None, deque(maxlen=10)
    times = []
    for index in range(8):
        started = time.perf_counter()
        _, exinfo = dff_event(frames[index % 3], prev, None, False, exinfo)
        times.append(time.perf_counter() - started)
    # measured about 1.4 / 5 / 23 ms on a slow CI-like box; the guard is a
    # generous multiple so a load-sensitive run does not flake, yet a
    # full-resolution implementation (hundreds of ms) fails it.
    assert float(np.median(times[1:])) < {512: 0.05, 1024: 0.12, 2048: 0.4}[side]


# ---- the transform convention -------------------------------------------------

def _poly3_service(coefficients):
    service = EtSTEDTransformService()
    service.function = lambda coords, coefficients: EtSTEDTransformService.poly_thirdorder_transform(
        coefficients, coords)
    service.set_coefficients(coefficients)
    return service


MATRIX = np.array([[0.0, 0.15, -20.0],      # scan_1 (X) follows the camera's COLUMN
                   [0.15, 0.0, -12.0]])     # scan_2 (Y) follows the camera's ROW


def test_affine_in_row_col_convention_round_trips_through_the_transform_service(tmp_path):
    coefficients = EtSTEDTransformService.coefficients_from_affine(MATRIX, 'row_col')
    path = tmp_path / 'rig.csv'
    EtSTEDTransformService.save_coefficients(path, coefficients)
    service = _poly3_service(EtSTEDTransformService.load_coefficients(path))

    stream = Stream()
    stream.run(30)
    c1, c2 = stream.push(stream.frame(spikes=[(60, 200, 400.0)]))[0]      # (column, row)
    row, col = c2, c1
    expected = MATRIX @ np.array([row, col, 1.0])
    np.testing.assert_allclose(service.apply([c1, c2]), expected, atol=1e-9)


def test_col_row_order_is_the_other_way_round():
    by_col_row = EtSTEDTransformService.coefficients_from_affine(MATRIX[:, [1, 0, 2]], 'col_row')
    by_row_col = EtSTEDTransformService.coefficients_from_affine(MATRIX, 'row_col')
    np.testing.assert_array_equal(by_col_row, by_row_col)


def test_a_3x3_matrix_and_bad_input_are_handled():
    full = np.vstack([MATRIX, [0, 0, 1]])
    np.testing.assert_array_equal(
        EtSTEDTransformService.coefficients_from_affine(full),
        EtSTEDTransformService.coefficients_from_affine(MATRIX))
    with pytest.raises(ValueError):
        EtSTEDTransformService.coefficients_from_affine(np.eye(4))
    with pytest.raises(ValueError):
        EtSTEDTransformService.coefficients_from_affine(MATRIX, 'xy')


def test_transform_outputs_drive_the_first_and_second_active_scan_axes():
    analog = {'target_device': ['X', 'None', 'Y', 'Z'], 'axis_centerpos': [0.0, 0.0, 0.0, 0.0]}
    EtSTEDTriggeredScanRunner().set_center_scan_parameter(
        analog, ['X', 'Y'], np.array([1.5, -2.5]))
    assert analog['axis_centerpos'] == [1.5, 0.0, -2.5, 0.0]


def test_the_helper_script_writes_the_same_file(tmp_path):
    import subprocess, sys
    out = tmp_path / 'script.csv'
    root = Path(__file__).resolve().parents[4]
    subprocess.run([sys.executable, str(root / 'tools' / 'etsted_affine_to_csv.py'),
                    '--matrix', *map(str, MATRIX.ravel()), '--out', str(out)], check=True)
    np.testing.assert_allclose(
        EtSTEDTransformService.load_coefficients(out),
        EtSTEDTransformService.coefficients_from_affine(MATRIX))
