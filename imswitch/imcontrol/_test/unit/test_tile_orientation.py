"""Tests for mosaic axis orientation: assembly and inversion.

A camera can be mounted eight ways relative to the stage axes (four directions
for a positive X step, times two remaining perpendicular choices for Y). These
tests pin down all eight.
"""

import itertools

import numpy as np
import pytest

from imswitch.imcontrol.controller.controllers.TilingController import (
    TilingController,
)

ALL_ORIENTATIONS = list(itertools.product([False, True], repeat=3))


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
