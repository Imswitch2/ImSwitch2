"""Free-running tiling uses the *calibrated* Z actuator, not focus lock's Z."""

import importlib
import json
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.controller.controllers.TilingController import TilingController
from imswitch.imcontrol.model.workflows.spiral import SPIRAL
from imswitch.imcontrol.model.workflows.stage_tilt_compensation import TiltPlane


class _Logger:
    def __init__(self):
        self.errors = []

    def error(self, *args, **kwargs):
        self.errors.append(args)

    def warning(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass


class _XY:
    def __init__(self):
        self.position = {'X': 0., 'Y': 0.}
        self.moves = []

    def move(self, value, axis):
        self.moves.append((value, axis))
        self.position[axis] += value


class _Z:
    axes = ['Z']
    isAvailable = True

    def __init__(self):
        self.position = 0.
        self.moves = []

    def get_abs(self, axis):
        assert axis == 'Z'
        return self.position

    def move(self, value, axis):
        assert axis == 'Z'
        self.position += value
        self.moves.append(value)

    def setPosition(self, value, axis):
        assert axis == 'Z'
        self.moves.append(value - self.position)
        self.position = value
        return True


class _Detector:
    pixelSizeUm = (1., 1.)


class _Detectors:
    def __init__(self):
        self.detector = _Detector()

    def __getitem__(self, name):
        return self.detector

    def acquire(self, names, purpose):
        return object()

    def release(self, handle):
        pass


class _Source:
    def __init__(self):
        self.count = 0

    def prepare(self):
        pass

    def acquire(self):
        self.count += 1
        return np.ones((8, 8), dtype=np.uint16), True

    def finish(self):
        pass


class _Stitcher:
    step_y_px = 3
    step_x_px = 3

    def __init__(self, **kwargs):
        pass

    def add_tile(self, *args):
        pass

    def get_overview(self):
        return np.ones((8, 8), dtype=np.uint16)

    def finish(self):
        return False


@pytest.fixture
def rig(monkeypatch, tmp_path):
    module = importlib.import_module(
        'imswitch.imcontrol.controller.controllers.TilingController'
    )
    monkeypatch.setattr(module, 'StitchedImage', _Stitcher)
    monkeypatch.setattr(module, 'tiling_folder', lambda base: tmp_path / 'tiling')
    monkeypatch.setattr(module, 'new_tilt_calibration_folder',
                        lambda name='': tmp_path / 'calibrations' / 'session')
    monkeypatch.setattr(module.time, 'sleep', lambda sec: None)
    controller = TilingController.__new__(TilingController)
    xy, z, fine = _XY(), _Z(), _Z()
    source = _Source()
    controller._master = SimpleNamespace(
        positionersManager={'Stage': xy, 'Z': z, 'Fine': fine},
        detectorsManager=_Detectors(),
    )
    controller._setupInfo = SimpleNamespace(
        positioners={'Stage': SimpleNamespace(axes=['X', 'Y'])}
    )
    focus = SimpleNamespace(
        positioner='Fine', focusLockState=lambda: 'locked',
        focusCameraAcquisitionActive=lambda: True,
    )
    controller._commChannel = SimpleNamespace(
        _focusLock=focus, _tilingActive=True,
        getTiltCalibrationSnapshot=lambda: dict(
            lock_state='locked', focus_signal_px=20.,
            measurement_age_s=.01, max_measurement_age_s=1.,
            positioner_available=True, focus_z_um=0., focus_setpoint_px=20.,
            focus_error_px=0.,
        ),
    )
    controller._stopRequested = False
    controller._stitcher = None
    controller._originXY = None
    controller._gridPositions = []
    controller._scanAcqHandle = None
    controller._scanning = True
    controller._scanThread = object()
    controller._closed = True
    controller._logger = _Logger()
    controller._getActivityLock = lambda: threading.Lock()
    controller._makeTileSource = lambda *args: source
    controller._resolveSaveSet = lambda *args: ['CAM']
    controller._saveRoot = lambda *args: tmp_path
    controller._syncPositionerFromHardware = lambda pos: None
    controller._tileAttrs = lambda *args: {}
    controller._displayPlane = lambda frame: frame
    controller._detectorPixelSizeUm = lambda detector: 1.
    controller._gridToImage = lambda gx, gy, orientation: (gx, gy)
    return controller, xy, z, fine, source, tmp_path


def _run(rig, *, precheck=False, bounds=300., record=False):
    ctrl, xy, z, fine, source, folder = rig
    plane = TiltPlane(
        path=folder / 'tilt_fit.json', sha256='a' * 64,
        xy_positioner='Stage', focus_positioner='Z',
        slope_x=-.01, slope_y=-.003,
        x_bounds=(-bounds, bounds), y_bounds=(-bounds, bounds),
        rms_residual_um=.05,
    )
    ctrl._runScan(
        SimpleNamespace(xyPositioner='Stage', camera='CAM'),
        n_tiles_x=3, n_tiles_y=3, pattern=SPIRAL, step_um=100.,
        blend_overlaps=False, intensity_correction=False, settle_s=0.,
        tilt_plane=None if record else plane,
        record_tilt=record, precheck_tilt=precheck,
    )
    return ctrl, xy, z, fine, source, folder


def test_separate_z_compensation_and_fine_focus_can_run_together(rig):
    ctrl, xy, z, fine, source, folder = _run(rig, precheck=True)
    assert not ctrl._logger.errors
    assert source.count == 9
    assert z.moves  # Compensating Z moved
    assert not fine.moves  # Focus controller's Z was untouched by tiling
    assert z.position == pytest.approx(0.)
    assert xy.position == pytest.approx({'X': 0., 'Y': 0.})
    data = json.loads((folder / 'tiling' / 'tilt_validation.json').read_text())
    assert data['completed'] is True
    assert data['focus_positioner'] == 'Z'
    assert data['n_recorded_tiles'] == 9


def test_optional_precheck_is_truly_optional(rig):
    ctrl, _, _, _, source, _ = _run(rig, bounds=50., precheck=False)
    assert not ctrl._logger.errors
    assert source.count == 9


def test_optional_precheck_blocks_outside_calibrated_area(rig):
    ctrl, _, z, _, source, _ = _run(rig, bounds=50., precheck=True)
    assert ctrl._logger.errors
    assert source.count == 0
    assert not z.moves


def test_calibration_recording_names_current_focus_actuator(rig):
    ctrl, _, _, _, source, folder = _run(rig, record=True)
    assert not ctrl._logger.errors
    assert source.count == 9
    calibration = json.loads((folder / 'calibrations' / 'session' / 'tilt_fit.json').read_text())
    assert calibration['focus_positioner'] == 'Fine'
    assert calibration['n_recorded_tiles'] == 9
    assert not (folder / 'tiling').exists()  # No acquisition data saved


class _LaggingZ(_Z):
    """Leica-style positioner: commands are accepted before readback catches up."""

    def __init__(self):
        super().__init__()
        self.targets = []
        self.reads = 0

    def get_abs(self, axis):
        self.reads += 1
        return self.position  # Deliberately never follows setPosition() here.

    def setPosition(self, value, axis):
        assert axis == 'Z'
        self.targets.append(value)
        self.moves.append(value)
        return True


def test_lagging_leica_readback_does_not_block_tiling(rig):
    ctrl, xy, _, fine, source, folder = rig
    objective = _LaggingZ()
    objective.position = 5140.589245
    ctrl._master.positionersManager['Z'] = objective
    _run((ctrl, xy, objective, fine, source, folder), precheck=False)
    assert not ctrl._logger.errors
    assert source.count == 9
    assert objective.reads == 1  # Initial Z only, never after each command.
    assert objective.targets[-1] == pytest.approx(5140.589245)  # Restore request.
    assert objective.targets[0] == pytest.approx(5140.589245 - 1.0)
    assert xy.position == pytest.approx({'X': 0., 'Y': 0.})
    data = json.loads((folder / 'tiling' / 'tilt_validation.json').read_text())
    assert data['completed'] is True


def test_rejected_z_command_stops_tiling(rig):
    ctrl, xy, z, fine, source, folder = rig

    def reject(value, axis):
        z.moves.append(value)
        return False

    z.setPosition = reject
    _run((ctrl, xy, z, fine, source, folder))
    assert ctrl._logger.errors
    assert source.count == 1  # Only origin tile was acquired.
    assert xy.position == pytest.approx({'X': 0., 'Y': 0.})


def test_cancelled_tiling_restores_original_z_by_absolute_command(rig):
    ctrl, xy, z, fine, source, folder = rig
    original_acquire = source.acquire

    def acquire_and_cancel():
        result = original_acquire()
        if source.count == 3:
            ctrl._stopRequested = True
        return result

    source.acquire = acquire_and_cancel
    _run((ctrl, xy, z, fine, source, folder))
    assert source.count == 3
    assert z.position == pytest.approx(0.)
    assert z.moves
    assert xy.position == pytest.approx({'X': 0., 'Y': 0.})


def test_calibration_only_sparse_bidirectional_never_acquires_camera(rig):
    ctrl, xy, z, fine, source, folder = rig
    ctrl._runTiltCalibrationOnly(
        SimpleNamespace(xyPositioner='Stage'),
        21, 21, 'serpentine', 20.,
        sample_nx=5, sample_ny=5, bidirectional=True,
        settle_s=0., tilt_record_name='Wide sample',
    )
    assert not ctrl._logger.errors
    assert source.count == 0  # no detector preparation/acquisition at all
    assert xy.position == pytest.approx({'X': 0., 'Y': 0.})
    data = json.loads((folder / 'calibrations' / 'session' / 'tilt_fit.json').read_text())
    assert data['completed'] is True
    assert data['calibration_only'] is True
    assert data['area_span_um'] == [400., 400.]
    assert data['n_requested_tiles'] == 25
    assert data['n_recorded_tiles'] == 50
    assert data['fit']['n_fit_points'] == 25
    assert data['bidirectional_diagnostics']['n_valid_pairs'] == 25
    assert data['origin_drift']['available'] is True
    import csv
    with (folder / 'calibrations' / 'session' / 'tilt_measurements.csv').open() as handle:
        rows = list(csv.DictReader(handle))
    assert [r['pass_index'] for r in rows] == ['1'] * 25 + ['2'] * 25
    assert [(float(r['stage_x_um']), float(r['stage_y_um'])) for r in rows[24:26]] == [
        (400., 400.), (400., 400.)
    ]  # reverse begins exactly at forward endpoint
    assert {float(r['stage_x_um']) for r in rows} == {0., 100., 200., 300., 400.}
    assert {float(r['stage_y_um']) for r in rows} == {0., 100., 200., 300., 400.}


def test_imaging_plus_bidirectional_uses_camera_only_on_forward_pass(rig):
    ctrl, xy, z, fine, source, folder = rig
    ctrl._runScan(
        SimpleNamespace(xyPositioner='Stage', camera='CAM'),
        3, 3, SPIRAL, 100., False, False, settle_s=0.,
        record_tilt=True, tilt_bidirectional=True,
    )
    assert not ctrl._logger.errors
    assert source.count == 9  # not 18 images
    assert xy.position == pytest.approx({'X': 0., 'Y': 0.})
    data = json.loads((folder / 'calibrations' / 'session' / 'tilt_fit.json').read_text())
    assert data['n_recorded_tiles'] == 18
    assert data['bidirectional'] is True
    assert data['fit']['n_fit_points'] == 9
    assert data['origin_drift']['available'] is True


def test_calibration_only_cancellation_restores_xy_and_saves_partial_data(rig):
    ctrl, xy, _, _, source, folder = rig
    original_snapshot = ctrl._commChannel.getTiltCalibrationSnapshot
    count = [0]

    def sample_and_stop():
        count[0] += 1
        if count[0] == 7:
            ctrl._stopRequested = True
        return original_snapshot()

    ctrl._commChannel.getTiltCalibrationSnapshot = sample_and_stop
    ctrl._runTiltCalibrationOnly(
        SimpleNamespace(xyPositioner='Stage'),
        21, 21, 'serpentine', 20.,
        sample_nx=5, sample_ny=5, bidirectional=True,
        settle_s=0., tilt_record_name='',
    )
    assert not ctrl._logger.errors
    assert source.count == 0
    assert xy.position == pytest.approx({'X': 0., 'Y': 0.})
    data = json.loads((folder / 'calibrations' / 'session' / 'tilt_fit.json').read_text())
    assert data['completed'] is False
    assert 0 < data['n_recorded_tiles'] < 50
    assert data['origin_drift']['available'] is True
