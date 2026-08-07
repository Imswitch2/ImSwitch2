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
