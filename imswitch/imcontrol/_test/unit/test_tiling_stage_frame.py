"""Regression tests for tiling stage handling and tile-frame freshness.

Covers three defects seen on a Marzhauser XY stage + Hamamatsu rig:

* the stage flew across its travel range at the end of every tiling run,
* tiles could store a frame exposed while the stage was still moving,
* rebuilding the whole mosaic per tile starved the GUI thread.
"""

from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.controller.controllers.FocusLockController import PI
from imswitch.imcontrol.model.workflows.spiral import SPIRAL
from imswitch.imcontrol.controller.controllers.TilingController import (
    TilingController,
)
from imswitch.imcontrol.model.managers.positioners.MHXYStageManager import (
    MHXYStageManager,
)
from imswitch.imcontrol.model.workflows.stitched_image import StitchedImage


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class _LeaseManager:
    def __init__(self):
        self.acquired = []
        self.released = []

    def acquire(self, detectorNames, purpose):
        handle = f'lease-{len(self.acquired) + 1}'
        self.acquired.append((tuple(detectorNames), purpose, handle))
        return handle

    def release(self, handle):
        self.released.append(handle)


class _OffsetFrameStage:
    """Stage whose tracked frame is offset from its hardware frame.

    This is what a Marzhauser looks like to ImSwitch: the manager's tracked
    position starts at zero while the stage physically sits wherever it was
    left, so ``move`` (relative) and ``setPosition`` (absolute) disagree by a
    fixed offset for the whole session.
    """

    def __init__(self, hardware_offset):
        self.position = {'X': 0.0, 'Y': 0.0}       # tracked frame
        self.hardware = dict(hardware_offset)      # true controller frame
        self.absoluteMoves = []

    def move(self, value, axis):
        self.position[axis] += value
        self.hardware[axis] += value

    def setPosition(self, value, axis):
        self.absoluteMoves.append((value, axis))
        self.position[axis] = value
        self.hardware[axis] = value  # 'moa' is absolute in the hardware frame


def _runScanWithStage(monkeypatch, positioner, detector, grid=(3, 2)):
    class _Detectors(_LeaseManager):
        def __getitem__(self, name):
            return detector

    detectors = _Detectors()
    ctrl = TilingController.__new__(TilingController)
    ctrl._master = SimpleNamespace(
        positionersManager={'STAGE': positioner},
        detectorsManager=detectors,
    )
    ctrl._setupInfo = SimpleNamespace(
        positioners={'STAGE': SimpleNamespace(axes=['X', 'Y'])},
    )
    ctrl._stopRequested = False
    ctrl._stitcher = None
    ctrl._originXY = None
    ctrl._gridPositions = []
    ctrl._scanAcqHandle = None
    ctrl._scanning = True
    ctrl._scanThread = object()
    ctrl._closed = True  # suppress GUI signal emission from this bare shell
    ctrl._logger = _Logger()

    monkeypatch.setattr(
        'imswitch.imcontrol.controller.controllers.TilingController.time.sleep',
        lambda _seconds: None,
    )

    TilingController._runScan(
        ctrl,
        SimpleNamespace(xyPositioner='STAGE', camera='CAM'),
        n_tiles_x=grid[0],
        n_tiles_y=grid[1],
        pattern=SPIRAL,
        step_um=100.0,
        blend_overlaps=False,
        intensity_correction=False,
    )
    return ctrl, detectors


class _SimpleDetector:
    pixelSizeUm = [1.0, 1.0]

    def getLatestFrameShared(self):
        return np.ones((4, 4), dtype=np.uint16)


def test_tiling_returns_stage_to_its_true_start_when_frames_are_offset(monkeypatch):
    """The stage must physically end where it began, not at tracked-zero.

    Previously the scan recorded the origin from the tracked position and
    returned with an absolute move to it, so the stage landed at the hardware
    coordinate that happened to equal the tracked number — the full startup
    offset away from the sample.
    """
    positioner = _OffsetFrameStage({'X': 25000.0, 'Y': 41000.0})
    startHardware = dict(positioner.hardware)

    _runScanWithStage(monkeypatch, positioner, _SimpleDetector())

    assert positioner.hardware == pytest.approx(startHardware)
    assert positioner.absoluteMoves == []


def test_tiling_unwinds_only_the_moves_it_made_when_stopped_early(monkeypatch):
    """A cancelled scan returns from wherever it actually got to."""

    class _StopAfterThirdTile(_SimpleDetector):
        def __init__(self, ctrlHolder):
            self.calls = 0
            self.ctrlHolder = ctrlHolder

        def getLatestFrameShared(self):
            self.calls += 1
            if self.calls >= 3:
                self.ctrlHolder['ctrl']._stopRequested = True
            return np.ones((4, 4), dtype=np.uint16)

    holder = {}
    positioner = _OffsetFrameStage({'X': 25000.0, 'Y': 41000.0})
    startHardware = dict(positioner.hardware)
    detector = _StopAfterThirdTile(holder)

    class _Detectors(_LeaseManager):
        def __getitem__(self, name):
            return detector

    detectors = _Detectors()
    ctrl = TilingController.__new__(TilingController)
    holder['ctrl'] = ctrl
    ctrl._master = SimpleNamespace(
        positionersManager={'STAGE': positioner},
        detectorsManager=detectors,
    )
    ctrl._setupInfo = SimpleNamespace(
        positioners={'STAGE': SimpleNamespace(axes=['X', 'Y'])},
    )
    ctrl._stopRequested = False
    ctrl._stitcher = None
    ctrl._originXY = None
    ctrl._gridPositions = []
    ctrl._scanAcqHandle = None
    ctrl._scanning = True
    ctrl._scanThread = object()
    ctrl._closed = True
    ctrl._logger = _Logger()

    monkeypatch.setattr(
        'imswitch.imcontrol.controller.controllers.TilingController.time.sleep',
        lambda _seconds: None,
    )
    TilingController._runScan(
        ctrl,
        SimpleNamespace(xyPositioner='STAGE', camera='CAM'),
        n_tiles_x=3, n_tiles_y=3, pattern=SPIRAL, step_um=100.0,
        blend_overlaps=False, intensity_correction=False,
    )

    assert positioner.hardware == pytest.approx(startHardware)
    assert positioner.absoluteMoves == []


def test_tiling_syncs_stage_position_before_anchoring_the_overview(monkeypatch):
    """The overview anchor comes from hardware when the driver can report it."""

    class _SyncingStage(_OffsetFrameStage):
        def __init__(self):
            super().__init__({'X': 25000.0, 'Y': 41000.0})
            self.syncCalls = 0

        def syncPositionFromHardware(self):
            self.syncCalls += 1
            self.position = dict(self.hardware)
            return True

    positioner = _SyncingStage()
    ctrl, _ = _runScanWithStage(monkeypatch, positioner, _SimpleDetector())

    assert positioner.syncCalls == 1
    assert ctrl._originXY == (25000.0, 41000.0)


# ----------------------------------------------------------------------
# Fresh-frame handshake
# ----------------------------------------------------------------------


class _ChunkDetector:
    """Detector that serves frames through the chunk-consumer broker."""

    pixelSizeUm = [1.0, 1.0]

    def __init__(self, framesPerRead=1):
        self.framesPerRead = framesPerRead
        self.boundaries = 0
        self.released = []
        self._counter = 0

    def startChunkConsumer(self, key):
        self.boundaries += 1

    def readChunk(self, key):
        frames = []
        for _ in range(self.framesPerRead):
            self._counter += 1
            frames.append(np.full((4, 4), self._counter, dtype=np.uint16))
        return frames

    def releaseChunkConsumer(self, key):
        self.released.append(key)

    def getLatestFrameShared(self):
        return np.full((4, 4), 999, dtype=np.uint16)


def test_fresh_frame_handshake_skips_the_frame_that_spans_the_move():
    """Two frames past the boundary are required; the newest is returned.

    The first frame to arrive after the boundary may have started exposing
    before it — i.e. while the stage was still moving. Only the second is
    provably clean.
    """
    ctrl = TilingController.__new__(TilingController)
    ctrl._logger = _Logger()
    ctrl._stopRequested = False
    ctrl._closed = False

    detector = _ChunkDetector(framesPerRead=1)
    frame, wasFresh = TilingController._grabSettledFrame(ctrl, detector)

    assert wasFresh is True
    assert detector.boundaries == 1
    # Frames 1 and 2 were read; frame 1 is discarded as possibly in-motion.
    assert int(frame[0, 0]) == 2


def test_fresh_frame_handshake_returns_newest_of_a_burst():
    ctrl = TilingController.__new__(TilingController)
    ctrl._logger = _Logger()
    ctrl._stopRequested = False
    ctrl._closed = False

    detector = _ChunkDetector(framesPerRead=3)
    frame, wasFresh = TilingController._grabSettledFrame(ctrl, detector)

    assert wasFresh is True
    assert int(frame[0, 0]) == 3


def test_fresh_frame_handshake_falls_back_when_no_frames_arrive(monkeypatch):
    """A silent camera degrades to the latest buffered frame, flagged stale."""

    class _SilentDetector(_ChunkDetector):
        def readChunk(self, key):
            return []

    ctrl = TilingController.__new__(TilingController)
    ctrl._logger = _Logger()
    ctrl._stopRequested = False
    ctrl._closed = False

    monkeypatch.setattr(
        'imswitch.imcontrol.controller.controllers.TilingController'
        '._FRESH_FRAME_TIMEOUT_S', 0.0, raising=False,
    )
    monkeypatch.setattr(
        'imswitch.imcontrol.controller.controllers.TilingController.time.sleep',
        lambda _seconds: None,
    )

    frame, wasFresh = TilingController._grabSettledFrame(ctrl, _SilentDetector())

    assert wasFresh is False
    assert int(frame[0, 0]) == 999


def test_tiling_releases_its_chunk_consumer(monkeypatch):
    positioner = _OffsetFrameStage({'X': 0.0, 'Y': 0.0})
    detector = _ChunkDetector(framesPerRead=2)

    _runScanWithStage(monkeypatch, positioner, detector, grid=(2, 2))

    assert detector.released == ['tiling']


# ----------------------------------------------------------------------
# Marzhauser coordinate frame
# ----------------------------------------------------------------------


class _FakeRS232:
    def __init__(self, posReply):
        self.posReply = posReply
        self.commands = []

    def query(self, cmd):
        self.commands.append(cmd)
        if cmd == '?pos':
            return self.posReply
        return 'ok'


def _makeStage(posReply):
    info = SimpleNamespace(
        axes=['X', 'Y'],
        managerProperties={'rs232device': 'rs232'},
        forPositioning=True,
        forScanning=False,
        resetOnClose=False,
        joystick=False,
        liveUpdate=False,
        shortcutModifier=None,
    )
    driver = _FakeRS232(posReply)
    stage = MHXYStageManager(info, 'STAGE', rs232sManager={'rs232': driver})
    return stage, driver


def test_marzhauser_seeds_tracked_position_from_hardware():
    """Absolute ('moa') moves are only correct if the frames share an origin."""
    stage, _ = _makeStage('25000.5 41000.25')

    assert stage.positionSynced is True
    assert stage.position == {'X': 25000.5, 'Y': 41000.25}


def test_marzhauser_falls_back_to_zero_when_position_is_unreadable():
    stage, _ = _makeStage(None)

    assert stage.positionSynced is False
    assert stage.position == {'X': 0.0, 'Y': 0.0}


@pytest.mark.parametrize('reply, expected', [
    ('1.0 2.0', {'X': 1.0, 'Y': 2.0}),
    ('1.0,2.0', {'X': 1.0, 'Y': 2.0}),
    ('  3.5   4.5  ', {'X': 3.5, 'Y': 4.5}),
    ('1.0 2.0 3.0 4.0', {'X': 1.0, 'Y': 2.0}),
])
def test_marzhauser_parses_position_replies(reply, expected):
    assert MHXYStageManager._parsePositionReply(reply, ['X', 'Y']) == expected


@pytest.mark.parametrize('reply', [None, '', 'ERR', '1.0'])
def test_marzhauser_rejects_unusable_position_replies(reply):
    assert MHXYStageManager._parsePositionReply(reply, ['X', 'Y']) is None


def test_marzhauser_sync_updates_tracked_position():
    stage, driver = _makeStage('10.0 20.0')
    driver.posReply = '30.0 40.0'

    assert stage.syncPositionFromHardware() is True
    assert stage.position == {'X': 30.0, 'Y': 40.0}


# ----------------------------------------------------------------------
# Incremental stitching
# ----------------------------------------------------------------------


def _referenceOverview(tiles, step_px, tile_shape):
    """Full-rebuild reference, mirroring the original get_overview()."""
    grid_xs = [pos[0] for pos in tiles]
    grid_ys = [pos[1] for pos in tiles]
    min_gx, max_gx = min(grid_xs), max(grid_xs)
    min_gy, max_gy = min(grid_ys), max(grid_ys)
    width = step_px * (max_gx - min_gx) + tile_shape[1]
    height = step_px * (max_gy - min_gy) + tile_shape[0]

    canvas_sum = np.zeros((height, width), dtype=np.float32)
    canvas_weight = np.zeros((height, width), dtype=np.float32)
    for (gx, gy), tile in tiles.items():
        row = (gy - min_gy) * step_px
        col = (gx - min_gx) * step_px
        canvas_sum[row:row + tile.shape[0], col:col + tile.shape[1]] += tile
        canvas_weight[row:row + tile.shape[0], col:col + tile.shape[1]] += 1.0
    out = np.zeros_like(canvas_sum)
    np.divide(canvas_sum, canvas_weight, out=out, where=canvas_weight > 0)
    return out


def test_incremental_stitching_matches_full_rebuild():
    """The incremental accumulator must be pixel-identical to a full rebuild."""
    rng = np.random.default_rng(0)
    tile_shape = (8, 8)
    step_px = 4  # overlapping tiles, so blending is exercised

    stitcher = StitchedImage(
        tile_size_px=None,
        tile_step_um=4.0,
        px_per_um=None,
        tile_shape_px=tile_shape,
        pixel_size_um=1.0,
        blend_overlaps=True,
        intensity_correction=False,
    )

    normalized = {}
    # Spiral-ish order that grows the canvas in every direction.
    for gx, gy in [(0, 0), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1)]:
        tile = rng.integers(0, 65535, size=tile_shape, dtype=np.uint16)
        stitcher.add_tile(tile, gx, gy)
        normalized[(gx, gy)] = tile.astype(np.float32) / np.iinfo(np.uint16).max

    expected = _referenceOverview(normalized, step_px, tile_shape)
    np.testing.assert_allclose(stitcher.get_overview(), expected, rtol=1e-6)


def test_overview_is_not_a_shared_mutable_buffer():
    stitcher = StitchedImage(
        tile_size_px=None, tile_step_um=4.0, px_per_um=None,
        tile_shape_px=(4, 4), pixel_size_um=1.0,
    )
    stitcher.add_tile(np.ones((4, 4), dtype=np.uint16), 0, 0)

    first = stitcher.get_overview()
    first[:] = 0.0

    assert not np.all(stitcher.get_overview() == 0.0)


def test_replacing_a_tile_rebuilds_rather_than_double_counting():
    stitcher = StitchedImage(
        tile_size_px=None, tile_step_um=4.0, px_per_um=None,
        tile_shape_px=(4, 4), pixel_size_um=1.0, blend_overlaps=True,
    )
    full = np.full((4, 4), np.iinfo(np.uint16).max, dtype=np.uint16)
    stitcher.add_tile(full, 0, 0)
    stitcher.add_tile(np.zeros((4, 4), dtype=np.uint16), 0, 0)

    np.testing.assert_allclose(stitcher.get_overview(), np.zeros((4, 4)))


# ----------------------------------------------------------------------
# dt-aware focus PI
# ----------------------------------------------------------------------


def test_pi_integral_scales_with_elapsed_time():
    """A delayed tick must integrate proportionally, not by a fixed step."""
    onTime = PI(setPoint=0.0, multiplier=1, kp=0.0, ki=1.0, nominalDt=0.1)
    delayed = PI(setPoint=0.0, multiplier=1, kp=0.0, ki=1.0, nominalDt=0.1)

    onTime.update(-1.0, dt=0.1)
    onTime.update(-1.0, dt=0.1)

    delayed.update(-1.0, dt=0.1)
    delayed.update(-1.0, dt=0.3)

    assert delayed.out > onTime.out


def test_pi_without_dt_keeps_historical_behaviour():
    """Without dt the integral term is unscaled, as it always was.

    ``update`` returns the *increment* to apply, because the positioner
    integrates relative moves; the running command it accumulates to is still
    exposed as ``out``. Applying that running command as a relative move made
    the actuator integrate an already-integrated signal, which is what turned
    the loop into a divergent double integrator.
    """
    legacy = PI(setPoint=0.0, multiplier=1, kp=0.5, ki=1.0)

    first = legacy.update(-1.0)
    second = legacy.update(-1.0)

    assert first == pytest.approx(0.5)          # kp * error
    assert second == pytest.approx(1.0)         # kp * dError + ki * error
    assert legacy.out == pytest.approx(1.5)     # the running command


def test_pi_clamps_a_pathological_gap():
    pi = PI(setPoint=0.0, multiplier=1, kp=0.0, ki=1.0, nominalDt=0.1,
            maxIntegralScale=5.0)
    pi.update(-1.0, dt=0.1)   # first step is proportional only (kp == 0)
    pi.update(-1.0, dt=60.0)

    # dt/nominalDt is 600 here; the clamp caps the integral step at 5x.
    assert pi.out == pytest.approx(5.0)


def test_free_running_tilt_samples_after_existing_settle_before_acquire(
        monkeypatch, tmp_path):
    """No new dwell; all coordinates come from the tiling worker's XY frame."""
    import csv
    import json
    from imswitch.imcontrol.model.workflows.stage_tilt_calibration import StageTiltRecorder

    stage = _OffsetFrameStage({'X': 3000.0, 'Y': 5000.0})
    history = []

    class _CapturingDetector(_SimpleDetector):
        def getLatestFrameShared(self):
            history.append(('acquire', dict(stage.position)))
            return super().getLatestFrameShared()

    class _Detectors(_LeaseManager):
        def __getitem__(self, name):
            return camera

    camera = _CapturingDetector()
    ctrl = TilingController.__new__(TilingController)
    ctrl._master = SimpleNamespace(
        positionersManager={'STAGE': stage}, detectorsManager=_Detectors(),
    )
    ctrl._setupInfo = SimpleNamespace(
        positioners={'STAGE': SimpleNamespace(axes=['X', 'Y'])},
        focusLock=SimpleNamespace(positioner='FocusPiezoDAC'),
    )
    ctrl._stopRequested = False
    ctrl._stitcher = None
    ctrl._originXY = None
    ctrl._gridPositions = []
    ctrl._scanAcqHandle = None
    ctrl._scanning = True
    ctrl._scanThread = object()
    ctrl._closed = True
    ctrl._logger = _Logger()
    ctrl._saveRoot = lambda _info: tmp_path

    def snapshot():
        x, y = stage.position['X'], stage.position['Y']
        history.append(('sample', dict(stage.position)))
        return dict(
            focus_z_um=3.0 + .01*x - .02*y,
            focus_signal_px=20.1, focus_setpoint_px=20.0,
            focus_error_px=.1, measurement_age_s=.01,
            max_measurement_age_s=1., lock_state='locked',
            positioner_available=True, dac_voltage_v=1.,
            dac_voltage_clamped=False,
        )

    ctrl._commChannel = SimpleNamespace(
        getTiltCalibrationSnapshot=snapshot,
        _focusLock=SimpleNamespace(positioner='FocusPiezoDAC',
                                   focusLockState=lambda: 'locked'),
    )
    sleeps = []
    monkeypatch.setattr(
        'imswitch.imcontrol.controller.controllers.TilingController.new_tilt_calibration_folder',
        lambda name='': tmp_path / 'tiling_000',
    )
    monkeypatch.setattr(
        'imswitch.imcontrol.controller.controllers.TilingController.time.sleep',
        lambda seconds: sleeps.append(seconds),
    )
    TilingController._runScan(
        ctrl, SimpleNamespace(xyPositioner='STAGE', camera='CAM'),
        n_tiles_x=3, n_tiles_y=3, pattern=SPIRAL, step_um=100,
        blend_overlaps=False, intensity_correction=False,
        settle_s=2.25, save_tiles=False, record_tilt=True,
    )
    # Initial dwell + one per subsequent tile, and one final settle to
    # measure Z drift after the stage has returned to its origin.
    assert sleeps.count(2.25) == 10
    samples = [i for i, (kind, _) in enumerate(history) if kind == 'sample']
    # One origin sample before and one after the scan bracket the nine
    # per-tile samples; each per-tile sample precedes its acquisition.
    assert len(samples) == 11
    assert all(history[idx+1][0] == 'acquire' for idx in samples[1:-1])
    folders = list(tmp_path.glob('tiling_*/tilt_fit.json'))
    assert len(folders) == 1
    result = json.loads(folders[0].read_text())
    assert result['n_valid_tiles'] == 9
    assert result['fit']['slope_x_um_per_um'] == pytest.approx(.01)
    assert result['fit']['slope_y_um_per_um'] == pytest.approx(-.02)
    assert not list(tmp_path.rglob('tile_*_CAM*'))  # Save tiles remained off

@pytest.mark.parametrize('stop_after,max_voltage', [(None, 2.), (3, 2.), (None, 1.35)])
def test_open_loop_tilt_compensation_follows_xy_and_restores_origin(
        monkeypatch, tmp_path, stop_after, max_voltage):
    """A tile's relative Z move precedes the *existing* settle/acquire.

    Cancellation must unwind both stage XY and our own commanded Z.
    """
    import csv
    import json
    from pathlib import Path
    from imswitch.imcontrol.model.workflows.stage_tilt_compensation import TiltPlane

    history = []
    stage = _OffsetFrameStage({'X': 1200., 'Y': 2200.})
    stage.position.update(X=1200., Y=2200.)
    original_xy = dict(stage.position)
    original_move = stage.move

    def stage_move(distance, axis):
        history.append(('xy', axis, distance))
        original_move(distance, axis)

    stage.move = stage_move

    class _DAC:
        isAvailable = True
        _offset_voltage = 1.
        _volts_per_um = .05
        _min_voltage = 0.
        _max_voltage = 2.

        def __init__(self):
            self.z = 6.0

        def get_abs(self, axis='Z'):
            return self.z

        def move(self, distance, axis='Z'):
            history.append(('z', distance))
            self.z += distance

        def setPosition(self, target, axis='Z'):
            assert axis == 'Z'
            self.move(target - self.z, axis)
            return True

    dac = _DAC()
    dac._max_voltage = max_voltage

    class _Focus:
        positioner = 'FocusPiezoDAC'

        def focusLockState(self):
            return 'unlocked'

        def focusCameraAcquisitionActive(self):
            return True

        def getPositionerAbs(self):
            return dac.get_abs()

        def movePositioner(self, dz):
            dac.move(dz)

    focus = _Focus()

    def snapshot():
        x, y = stage.position['X'], stage.position['Y']
        expected_z = (6. - .00926 * (x - 1200.) - .00296 * (y - 2200.))
        signal = 20 + 10 * (dac.z - expected_z)
        history.append(('snapshot', signal))
        return dict(lock_state='unlocked', focus_signal_px=signal,
                    measurement_age_s=.01, max_measurement_age_s=1.,
                    positioner_available=True)

    class _Detectors(_LeaseManager):
        def __getitem__(self, name):
            return _SimpleDetector()

    ctrl = TilingController.__new__(TilingController)
    ctrl._master = SimpleNamespace(
        positionersManager={'Stage': stage, 'FocusPiezoDAC': dac},
        detectorsManager=_Detectors(),
    )
    ctrl._setupInfo = SimpleNamespace(
        positioners={'Stage': SimpleNamespace(axes=['X', 'Y'])},
        focusLock=SimpleNamespace(positioner='FocusPiezoDAC'),
    )
    ctrl._commChannel = SimpleNamespace(
        _focusLock=focus, getTiltCalibrationSnapshot=snapshot,
    )
    ctrl._logger = _Logger()
    ctrl._closed = True
    ctrl._stopRequested = False
    ctrl._scanning = True
    ctrl._scanThread = object()
    ctrl._stitcher = None
    ctrl._scanAcqHandle = None
    ctrl._originXY = None
    ctrl._gridPositions = []
    ctrl._saveRoot = lambda _info: tmp_path

    class _Source:
        pointAttributes = None

        def __init__(self):
            self.count = 0

        def prepare(self):
            pass

        def finish(self):
            pass

        def acquire(self):
            self.count += 1
            history.append(('acquire', self.count))
            if self.count == stop_after:
                ctrl._stopRequested = True
            return np.ones((4, 4), dtype=np.uint16), True

    source = _Source()
    ctrl._makeTileSource = lambda *_args: source
    ctrl._resolveSaveSet = lambda *_args: ['CAM']
    monkeypatch.setattr(
        'imswitch.imcontrol.controller.controllers.TilingController.time.sleep',
        lambda seconds: history.append(('sleep', seconds)),
    )
    plane = TiltPlane(
        path=Path('test.json'), sha256='a' * 64,
        xy_positioner='Stage', focus_positioner='FocusPiezoDAC',
        slope_x=-.00926, slope_y=-.00296,
        x_bounds=(1100., 1300.), y_bounds=(2100., 2300.),
        rms_residual_um=.0593,
    )
    TilingController._runScan(
        ctrl, SimpleNamespace(xyPositioner='Stage', camera='CAM'),
        n_tiles_x=3, n_tiles_y=3, pattern=SPIRAL, step_um=100.,
        blend_overlaps=False, intensity_correction=False,
        settle_s=2.5, tilt_plane=plane,
        precheck_tilt=(max_voltage < 2.),
    )
    if max_voltage < 2.:
        # The full Z envelope is rejected *before* any XY or Z movement.
        assert source.count == 0
        assert not stage.absoluteMoves
        assert not [e for e in history if e[0] in ('xy', 'z', 'acquire')]
        assert stage.position == pytest.approx(original_xy)
        assert dac.z == pytest.approx(6.)
        return
    n = stop_after or 9
    assert source.count == n
    assert stage.position == pytest.approx(original_xy)
    assert dac.z == pytest.approx(6.)
    assert len([event for event in history if event[0] == 'sleep']) == n
    # Each acquired tile has a fresh sample immediately beforehand.
    assert all(history[i - 1][0] == 'snapshot'
               for i, event in enumerate(history) if event[0] == 'acquire')
    # All feed-forward commands happen before the next settle, not after it.
    for i, event in enumerate(history):
        if event[0] == 'z' and i + 1 < len(history) and history[i + 1][0] == 'sleep':
            assert history[i + 2][0] == 'snapshot'
    report = next(tmp_path.glob('tiling_*/tilt_validation.json'))
    info = json.loads(report.read_text())
    assert info['completed'] == (stop_after is None)
    assert info['n_recorded_tiles'] == n
    assert info['n_valid_focus_samples'] == n
    assert info['max_abs_focus_signal_delta_px'] < 1e-8
    with (report.parent / 'tilt_compensation.csv').open(newline='') as file:
        assert len(list(csv.DictReader(file))) == n
    assert (report.parent / 'tilt_validation.png').exists()
