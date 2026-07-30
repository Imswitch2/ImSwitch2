"""Tests for mosaic axis orientation: assembly, inversion, and the probe.

A camera can be mounted eight ways relative to the stage axes (four directions
for a positive X step, times two remaining perpendicular choices for Y). These
tests pin down all eight.
"""

import itertools
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.controller.controllers.TilingController import (
    TilingController,
)
from imswitch.imcontrol.model.workflows.tile_registration import (
    describe_orientation,
    infer_orientation,
    measure_pair_shift,
    orientation_advice,
)

ALL_ORIENTATIONS = list(itertools.product([False, True], repeat=3))


def _texture(shape, seed=0):
    from scipy.ndimage import gaussian_filter
    rng = np.random.default_rng(seed)
    return gaussian_filter(rng.random(shape).astype(np.float32), 2.0)


# ----------------------------------------------------------------------
# The transform and its inverse
# ----------------------------------------------------------------------


def test_there_are_exactly_eight_distinct_mountings():
    """Four directions for +X, times two perpendicular choices for +Y."""
    mappings = set()
    for orientation in ALL_ORIENTATIONS:
        x_step = TilingController._gridToImage(1, 0, orientation)
        y_step = TilingController._gridToImage(0, 1, orientation)
        mappings.add((x_step, y_step))

    assert len(mappings) == 8
    # A +X step lands on one of four unit directions...
    assert len({m[0] for m in mappings}) == 4
    # ...and +Y is always perpendicular to it, never parallel.
    for x_step, y_step in mappings:
        assert x_step[0] * y_step[0] + x_step[1] * y_step[1] == 0


@pytest.mark.parametrize('orientation', ALL_ORIENTATIONS)
def test_grid_transform_is_invertible(orientation):
    """Stage -> image -> stage must round-trip for every mounting."""
    for gx, gy in [(1, 0), (0, 1), (3, -2), (-4, 5)]:
        ix, iy = TilingController._gridToImage(gx, gy, orientation)
        # The image offset is (dx, dy) = (ix, iy) in grid units.
        back_x, back_y = TilingController._imageOffsetToStage(ix, iy, orientation)
        assert (back_x, back_y) == (gx, gy)


@pytest.mark.parametrize('orientation', ALL_ORIENTATIONS)
def test_grid_transform_preserves_distance(orientation):
    """Every mounting is a rigid motion — it may rotate/mirror, never scale."""
    ix, iy = TilingController._gridToImage(3, 4, orientation)
    assert np.hypot(ix, iy) == pytest.approx(5.0)


def test_default_orientation_is_the_identity():
    assert TilingController._gridToImage(1, 0, (False, False, False)) == (1, 0)
    assert TilingController._gridToImage(0, 1, (False, False, False)) == (0, 1)


def test_flip_x_mirrors_only_the_x_axis():
    assert TilingController._gridToImage(1, 0, (True, False, False)) == (-1, 0)
    assert TilingController._gridToImage(0, 1, (True, False, False)) == (0, 1)


def test_swap_sends_a_stage_x_step_onto_image_rows():
    ix, iy = TilingController._gridToImage(1, 0, (False, False, True))
    assert (ix, iy) == (0, 1)


# ----------------------------------------------------------------------
# Inferring the orientation from measurements
# ----------------------------------------------------------------------


@pytest.mark.parametrize('orientation', ALL_ORIENTATIONS)
def test_infer_orientation_recovers_every_mounting(orientation):
    """Simulate the two probe steps and check the mounting is identified."""
    step = 40.0
    # Image displacement (dy, dx) for a +X and a +Y stage step.
    ix, iy = TilingController._gridToImage(1, 0, orientation)
    x_shift = (iy * step, ix * step)
    ix, iy = TilingController._gridToImage(0, 1, orientation)
    y_shift = (iy * step, ix * step)

    assert infer_orientation(x_shift, y_shift) == orientation


def test_infer_orientation_without_a_y_step_assumes_y_unflipped():
    assert infer_orientation((0.0, 40.0)) == (False, False, False)
    assert infer_orientation((0.0, -40.0)) == (True, False, False)


def test_advice_is_silent_when_the_orientation_already_matches():
    advice = orientation_advice((True, False, False), (True, False, False))
    assert 'checks out' in advice
    assert 'wrong' not in advice


def test_advice_names_the_setting_to_change():
    advice = orientation_advice((False, False, False), (True, False, False))
    assert 'looks wrong' in advice
    assert 'Flip X' in advice
    assert 'flipTileAxisX' in advice


def test_describe_orientation_reads_naturally():
    assert describe_orientation((False, False, False)) == 'no flips'
    assert describe_orientation((True, False, False)) == 'Flip X'
    assert describe_orientation((True, True, True)) == 'Swap X/Y + Flip X + Flip Y'


# ----------------------------------------------------------------------
# The probe against real image data
# ----------------------------------------------------------------------


def test_measure_pair_shift_recovers_a_real_displacement():
    scene = _texture((256, 256), seed=11)
    first = scene[64:192, 64:192]
    second = scene[74:202, 84:212]  # 10 down, 20 right

    shift = measure_pair_shift(first, second)

    assert shift is not None
    assert shift[0] == pytest.approx(10.0, abs=1.0)
    assert shift[1] == pytest.approx(20.0, abs=1.0)


def test_measure_pair_shift_declines_mismatched_or_flat_tiles():
    assert measure_pair_shift(np.zeros((8, 8)), np.zeros((16, 16))) is None
    assert measure_pair_shift(np.ones((32, 32)), np.ones((32, 32))) is None
    assert measure_pair_shift(None, np.ones((4, 4))) is None


@pytest.mark.parametrize('orientation', ALL_ORIENTATIONS)
def test_probe_identifies_the_mounting_from_acquired_tiles(orientation):
    """End-to-end: acquire three tiles through a mounting, recover it."""
    scene = _texture((512, 512), seed=13)
    tile = 128
    step_px = 40
    base_row, base_col = 160, 160

    # The stage visits (0,0) -> (1,0) -> (1,1); a +X then a +Y step.
    frames = []
    for gx, gy in [(0, 0), (1, 0), (1, 1)]:
        ix, iy = TilingController._gridToImage(gx, gy, orientation)
        row = base_row + iy * step_px
        col = base_col + ix * step_px
        frames.append(scene[row:row + tile, col:col + tile].copy())

    ctrl = TilingController.__new__(TilingController)
    # The scan ran with no flips configured; the probe should say what is real.
    advice = TilingController._probeOrientation(ctrl, frames, (False, False, False))

    if orientation == (False, False, False):
        assert 'checks out' in advice
    else:
        assert 'looks wrong' in advice
        assert describe_orientation(orientation) in advice


def test_probe_stays_quiet_without_enough_tiles():
    ctrl = TilingController.__new__(TilingController)
    assert TilingController._probeOrientation(ctrl, [], (False,) * 3) == ''
    assert TilingController._probeOrientation(
        ctrl, [np.ones((8, 8))], (False,) * 3
    ) == ''


# ----------------------------------------------------------------------
# Click-to-navigate must invert whatever the assembly did
# ----------------------------------------------------------------------


@pytest.mark.parametrize('orientation', ALL_ORIENTATIONS)
def test_click_to_navigate_inverts_the_assembly_transform(orientation):
    """Clicking a tile's centre must drive the stage to that tile."""
    from imswitch.imcontrol.model.workflows.stitched_image import StitchedImage

    tile_shape = (16, 16)
    step_um = 8.0
    origin = (1000.0, 2000.0)

    stitcher = StitchedImage(
        tile_size_px=None, tile_step_um=step_um, px_per_um=None,
        tile_shape_px=tile_shape, pixel_size_um=1.0,
    )

    ctrl = TilingController.__new__(TilingController)
    ctrl._stitcher = stitcher
    ctrl._originXY = origin
    ctrl._orientation = orientation
    ctrl._gridPositions = []

    grid = [(0, 0), (1, 0), (0, 1), (-1, 0), (0, -1), (1, 1)]
    for gx, gy in grid:
        ix, iy = TilingController._gridToImage(gx, gy, orientation)
        stitcher.add_tile(np.ones(tile_shape, dtype=np.uint16), ix, iy)
        ctrl._gridPositions.append((gx, gy))

    for gx, gy in grid:
        ix, iy = TilingController._gridToImage(gx, gy, orientation)
        placement = stitcher.placement(ix, iy)
        canvas_row0, canvas_col0 = stitcher.canvas_origin_px
        # Canvas pixel at this tile's centre.
        row = placement[0] - canvas_row0 + tile_shape[0] // 2
        col = placement[1] - canvas_col0 + tile_shape[1] // 2

        stage = TilingController._canvasPixelToStage(ctrl, row, col)

        assert stage[0] == pytest.approx(origin[0] + gx * step_um)
        assert stage[1] == pytest.approx(origin[1] + gy * step_um)
