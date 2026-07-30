"""Tests for tile registration, settle time, and binning-aware tile geometry."""

from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.controller.controllers.TilingController import (
    TilingController,
)
from imswitch.imcontrol.model.workflows.stitched_image import StitchedImage
from imswitch.imcontrol.model.workflows.tile_registration import (
    RegistrationReport,
    TileShift,
    estimate_shift,
    max_shift_for_step,
)


class _Logger:
    def __init__(self):
        self.messages = []

    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        self.messages.append(('info', args[0] if args else ''))

    def warning(self, *args, **kwargs):
        self.messages.append(('warning', args[0] if args else ''))

    def error(self, *args, **kwargs):
        self.messages.append(('error', args[0] if args else ''))


def _texture(shape, seed=0):
    """Random but smooth texture — correlates well, unlike white noise."""
    rng = np.random.default_rng(seed)
    from scipy.ndimage import gaussian_filter
    return gaussian_filter(rng.random(shape).astype(np.float32), 2.0)


# ----------------------------------------------------------------------
# Shift estimation
# ----------------------------------------------------------------------


def test_estimate_shift_recovers_a_known_displacement():
    """The returned shift is the correction to ADD to the nominal placement."""
    scene = _texture((256, 256), seed=1)
    reference = scene[0:128, 0:128]
    # The tile the stage *thinks* sits at (0, 64) really sits at (3, 69).
    moving = scene[3:131, 69:197]

    shift, confidence, reason = estimate_shift(
        reference, moving, nominal_offset=(0, 64), max_shift_px=32,
    )

    assert reason == ''
    assert shift[0] == pytest.approx(3.0, abs=0.5)
    assert shift[1] == pytest.approx(5.0, abs=0.5)
    assert confidence > 0.0


def test_estimate_shift_rejects_an_implausible_correction():
    scene = _texture((256, 256), seed=2)
    reference = scene[0:128, 0:128]
    # True origin (10, 74) -> a (10, 10) correction, well over the 4 px limit.
    moving = scene[10:138, 74:202]

    shift, _confidence, reason = estimate_shift(
        reference, moving, nominal_offset=(0, 64), max_shift_px=4,
    )

    assert shift == (0.0, 0.0)
    assert 'exceeds' in reason


def test_estimate_shift_declines_a_featureless_overlap():
    flat = np.ones((128, 128), dtype=np.float32)

    shift, _confidence, reason = estimate_shift(
        flat, flat, nominal_offset=(0, 64), max_shift_px=32,
    )

    assert shift == (0.0, 0.0)
    assert 'featureless' in reason


def test_estimate_shift_declines_when_tiles_do_not_overlap():
    scene = _texture((128, 128), seed=3)

    shift, _confidence, reason = estimate_shift(
        scene, scene, nominal_offset=(0, 512), max_shift_px=32,
    )

    assert shift == (0.0, 0.0)
    assert 'too small' in reason


def test_max_shift_scales_with_the_tile_step():
    assert max_shift_for_step(200, 200, 0.5) == pytest.approx(100.0)
    # Never below the minimum correlatable overlap.
    assert max_shift_for_step(4, 4, 0.5) >= 16


# ----------------------------------------------------------------------
# Diagnostics
# ----------------------------------------------------------------------


def _report(shifts):
    report = RegistrationReport()
    for shift in shifts:
        report.add(shift)
    return report


def test_report_flags_a_scale_error_as_geometry_not_stage_jitter():
    """Corrections proportional to the expected step mean wrong pixel size."""
    # Nominal step 100 px, but each tile really lands 200 px away: the classic
    # signature of binning not being applied to the sample-plane pixel size.
    report = _report([
        TileShift(grid=(i, 0), applied=(0.0, 100.0), measured=(0.0, 100.0),
                  expected=(0.0, 100.0), confidence=0.9, accepted=True)
        for i in range(1, 5)
    ])

    assert report.scale_estimate() == pytest.approx(2.0)
    summary = report.summary()
    assert 'scale error' in summary
    assert 'pixel size' in summary


def test_report_stays_quiet_about_scale_for_small_random_corrections():
    rng = np.random.default_rng(0)
    report = _report([
        TileShift(grid=(i, 0),
                  applied=(float(rng.normal(0, 1)), float(rng.normal(0, 1))),
                  measured=(0.0, 0.0), expected=(0.0, 200.0),
                  confidence=0.9, accepted=True)
        for i in range(1, 12)
    ])

    assert report.scale_estimate() == pytest.approx(1.0, abs=0.02)
    assert 'scale error' not in report.summary()


def test_report_handles_a_run_where_nothing_registered():
    report = _report([
        TileShift(grid=(1, 0), applied=(0.0, 0.0), measured=(0.0, 0.0),
                  expected=(0.0, 100.0), confidence=0.0, accepted=False,
                  reason='featureless'),
    ])

    assert report.residual_rms() is None
    assert '0 of 1' in report.summary()


def test_report_rms_reflects_applied_corrections():
    report = _report([
        TileShift(grid=(1, 0), applied=(3.0, 4.0), measured=(3.0, 4.0),
                  expected=(0.0, 100.0), confidence=0.9, accepted=True),
    ])

    assert report.residual_rms() == pytest.approx(5.0)


# ----------------------------------------------------------------------
# Offset-aware stitching
# ----------------------------------------------------------------------


def test_stitcher_places_a_tile_at_its_corrected_position():
    stitcher = StitchedImage(
        tile_size_px=None, tile_step_um=8.0, px_per_um=None,
        tile_shape_px=(16, 16), pixel_size_um=1.0, blend_overlaps=False,
    )
    stitcher.add_tile(np.ones((16, 16), dtype=np.uint16), 0, 0)
    stitcher.add_tile(np.ones((16, 16), dtype=np.uint16), 1, 0, offset_px=(2.0, -3.0))

    assert stitcher.placement(0, 0) == (0, 0)
    assert stitcher.placement(1, 0) == (2, 8 - 3)


def test_canvas_grows_for_a_negative_correction():
    """A tile nudged above/left of the origin must still fit on the canvas."""
    stitcher = StitchedImage(
        tile_size_px=None, tile_step_um=8.0, px_per_um=None,
        tile_shape_px=(16, 16), pixel_size_um=1.0, blend_overlaps=False,
    )
    stitcher.add_tile(np.ones((16, 16), dtype=np.uint16), 0, 0)
    stitcher.add_tile(np.full((16, 16), 2, dtype=np.uint16), 0, -1,
                      offset_px=(-5.0, -4.0))

    assert stitcher.canvas_origin_px == (-8 - 5, -4)
    overview = stitcher.get_overview()
    # Canvas spans from row -13 to row 16, col -4 to col 16.
    assert overview.shape == (16 + 13, 16 + 4)
    assert np.count_nonzero(overview) > 0


def test_zero_offset_matches_uncorrected_placement():
    a = StitchedImage(
        tile_size_px=None, tile_step_um=8.0, px_per_um=None,
        tile_shape_px=(16, 16), pixel_size_um=1.0,
    )
    b = StitchedImage(
        tile_size_px=None, tile_step_um=8.0, px_per_um=None,
        tile_shape_px=(16, 16), pixel_size_um=1.0,
    )
    rng = np.random.default_rng(5)
    for gx, gy in [(0, 0), (1, 0), (1, 1), (0, 1)]:
        tile = rng.integers(0, 65535, size=(16, 16), dtype=np.uint16)
        a.add_tile(tile, gx, gy)
        b.add_tile(tile, gx, gy, offset_px=(0.0, 0.0))

    np.testing.assert_array_equal(a.get_overview(), b.get_overview())


# ----------------------------------------------------------------------
# Binning-aware tile geometry
# ----------------------------------------------------------------------


@pytest.mark.parametrize('binning, expected', [(1, 0.5), (2, 1.0), (4, 2.0)])
def test_tile_pixel_size_accounts_for_binning(binning, expected):
    """pixelSizeUm is the unbinned value; the delivered frame is coarser.

    Without this the mosaic is laid out `binning` times too wide and never
    overlaps — indistinguishable from stage error at a glance.
    """
    ctrl = TilingController.__new__(TilingController)
    ctrl._logger = _Logger()
    detector = SimpleNamespace(
        name='CAM', pixelSizeUm=[1.0, 0.5, 0.5], binning=binning,
    )

    assert ctrl._detectorPixelSizeUm(detector) == (expected, expected)


def test_tile_pixel_size_tolerates_a_detector_without_binning():
    ctrl = TilingController.__new__(TilingController)
    ctrl._logger = _Logger()
    detector = SimpleNamespace(name='CAM', pixelSizeUm=[1.0, 0.65, 0.65])

    assert ctrl._detectorPixelSizeUm(detector) == (0.65, 0.65)


# ----------------------------------------------------------------------
# End-to-end: registration recovers a real stage error
# ----------------------------------------------------------------------


class _DriftingStage:
    """Stage that consistently undershoots its commanded X moves."""

    def __init__(self, undershoot_um):
        self.position = {'X': 0.0, 'Y': 0.0}
        self.truth = {'X': 0.0, 'Y': 0.0}
        self.undershoot = undershoot_um

    def move(self, value, axis):
        self.position[axis] += value
        actual = value
        if value and axis == 'X':
            actual = value - np.sign(value) * self.undershoot
        self.truth[axis] += actual


def test_registration_recovers_a_systematic_stage_undershoot(monkeypatch):
    """A stage that lands 4 µm short each step should still stitch seamlessly."""
    scene = _texture((512, 512), seed=7)
    tile_shape = (64, 64)
    step_um = 32.0        # 1 µm per pixel, so 32 px step -> 50% overlap
    undershoot_um = 4.0

    stage = _DriftingStage(undershoot_um)

    class _Detector:
        pixelSizeUm = [1.0, 1.0, 1.0]
        binning = 1

        def getLatestFrameShared(self):
            # Crop the scene at the stage's TRUE position, not the commanded one.
            row = int(round(stage.truth['Y'])) + 128
            col = int(round(stage.truth['X'])) + 128
            patch = scene[row:row + tile_shape[0], col:col + tile_shape[1]]
            return (patch * 65535).astype(np.uint16)

    class _Detectors:
        def __init__(self):
            self.released = []

        def acquire(self, names, purpose):
            return 'lease-1'

        def release(self, handle):
            self.released.append(handle)

        def __getitem__(self, name):
            return _Detector()

    ctrl = TilingController.__new__(TilingController)
    ctrl._master = SimpleNamespace(
        positionersManager={'STAGE': stage},
        detectorsManager=_Detectors(),
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
    ctrl._registrationReport = None
    ctrl._logger = _Logger()

    monkeypatch.setattr(
        'imswitch.imcontrol.controller.controllers.TilingController.time.sleep',
        lambda _seconds: None,
    )

    TilingController._runScan(
        ctrl,
        SimpleNamespace(xyPositioner='STAGE', camera='CAM',
                        registrationMaxShiftFraction=0.5),
        n_tiles=4, step_um=step_um,
        blend_overlaps=False, intensity_correction=False,
        settle_s=0.0, register_tiles=True,
    )

    report = ctrl._registrationReport
    assert report is not None
    accepted = report.accepted
    assert accepted, 'registration should have matched at least one tile'

    # The X undershoot is 4 µm = 4 px at this pixel size; the measured
    # correction must recover it rather than leaving the mosaic misaligned.
    x_corrections = [
        s.applied[1] for s in accepted if abs(s.expected[1]) > 0
    ]
    assert x_corrections
    assert np.median(np.abs(x_corrections)) == pytest.approx(
        undershoot_um, abs=1.5
    )


def test_settle_time_is_honoured(monkeypatch):
    """The configured settle time reaches the sleep between tiles."""
    sleeps = []

    class _Detector:
        pixelSizeUm = [1.0, 1.0, 1.0]
        binning = 1

        def getLatestFrameShared(self):
            return np.ones((8, 8), dtype=np.uint16)

    class _Detectors:
        def acquire(self, names, purpose):
            return 'lease-1'

        def release(self, handle):
            pass

        def __getitem__(self, name):
            return _Detector()

    class _Stage:
        def __init__(self):
            self.position = {'X': 0.0, 'Y': 0.0}

        def move(self, value, axis):
            self.position[axis] += value

    ctrl = TilingController.__new__(TilingController)
    ctrl._master = SimpleNamespace(
        positionersManager={'STAGE': _Stage()},
        detectorsManager=_Detectors(),
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
    ctrl._registrationReport = None
    ctrl._logger = _Logger()

    monkeypatch.setattr(
        'imswitch.imcontrol.controller.controllers.TilingController.time.sleep',
        lambda seconds: sleeps.append(seconds),
    )

    TilingController._runScan(
        ctrl, SimpleNamespace(xyPositioner='STAGE', camera='CAM'),
        n_tiles=3, step_um=4.0,
        blend_overlaps=False, intensity_correction=False,
        settle_s=0.75, register_tiles=False,
    )

    assert sleeps
    assert all(s == 0.75 for s in sleeps)
