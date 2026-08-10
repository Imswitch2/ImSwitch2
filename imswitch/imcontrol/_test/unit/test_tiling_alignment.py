"""Tile alignment: many neighbours per tile, and a whole-run solve.

Aligning each tile to one region of the canvas and applying whatever comes back
lets a single false match place that tile *and* every tile placed relative to
it afterwards. These cover what replaced that: correlating against every
overlapping neighbour, and re-fitting the whole layout once the run is over.
"""

import numpy as np
import pytest
from scipy.ndimage import gaussian_filter

from imswitch.imcommon.algorithms.tile_mosaic import TileLink, solve_links
from imswitch.imcontrol.model.workflows.stitched_image import StitchedImage


def _scene(size=256, seed=0):
    rng = np.random.default_rng(seed)
    return gaussian_filter(rng.random((size, size)).astype(np.float32), 2.0)


def _stitcher(tile_shape=(64, 64), step_um=48.0, pixel_size_um=1.0):
    return StitchedImage(
        tile_size_px=None,
        tile_step_um=step_um,
        px_per_um=None,
        tile_shape_px=tile_shape,
        pixel_size_um=pixel_size_um,
    )


# ----------------------------------------------------------------------
# Neighbours
# ----------------------------------------------------------------------


def test_a_new_tile_sees_every_placed_neighbour_it_overlaps():
    """A tile in the crook of an L touches both of the tiles forming it."""
    stitcher = _stitcher()
    tile = np.zeros((64, 64), np.float32)
    for grid in ((0, 0), (1, 0), (0, 1)):
        stitcher.add_tile(tile, *grid)

    found = stitcher.placed_neighbours(1, 1)

    assert {key for key, _tile, _offset in found} == {(0, 0), (1, 0), (0, 1)}


def test_the_offset_reported_is_in_the_neighbours_own_frame():
    stitcher = _stitcher()
    tile = np.zeros((64, 64), np.float32)
    stitcher.add_tile(tile, 0, 0)

    (_key, _tile, offset), = stitcher.placed_neighbours(1, 0)

    # One step along the image-x axis, and nothing along y.
    assert offset == pytest.approx((0.0, stitcher.step_x_px))


def test_tiles_that_do_not_reach_each_other_are_not_neighbours():
    stitcher = _stitcher()
    tile = np.zeros((64, 64), np.float32)
    stitcher.add_tile(tile, 0, 0)

    assert stitcher.placed_neighbours(9, 9) == []


def test_a_tile_is_never_its_own_neighbour():
    stitcher = _stitcher()
    stitcher.add_tile(np.zeros((64, 64), np.float32), 0, 0)

    assert stitcher.placed_neighbours(0, 0) == []


# ----------------------------------------------------------------------
# Re-placement
# ----------------------------------------------------------------------


def test_the_layout_can_be_re_placed_wholesale():
    stitcher = _stitcher()
    scene = _scene()
    stitcher.add_tile(scene[0:64, 0:64], 0, 0)
    stitcher.add_tile(scene[0:64, 48:112], 1, 0)

    stitcher.set_placements({(1, 0): (7, 55)})

    assert stitcher.placement(1, 0) == (7, 55)
    # The canvas is redrawn, not just the bookkeeping.
    assert stitcher.get_overview().shape[0] >= 64 + 7


def test_re_placing_ignores_a_tile_that_was_never_placed():
    stitcher = _stitcher()
    stitcher.add_tile(np.zeros((64, 64), np.float32), 0, 0)

    stitcher.set_placements({(5, 5): (10, 10)})

    assert stitcher.placement(5, 5) is None


# ----------------------------------------------------------------------
# The whole-run solve
# ----------------------------------------------------------------------


def test_the_run_solve_outvotes_one_false_measurement():
    """Three tiles agree, one link disagrees; the consensus must win.

    This is the case the live pass cannot handle: it has only the bad link at
    the moment it places that tile, so it believes it.
    """
    nominal = [(0.0, 0.0), (0.0, 48.0), (48.0, 0.0), (48.0, 48.0)]
    links = [
        TileLink(0, 1, (0.0, 48.0), 0.9, (0.0, 0.0)),
        TileLink(0, 2, (48.0, 0.0), 0.9, (0.0, 0.0)),
        TileLink(1, 3, (48.0, 0.0), 0.9, (0.0, 0.0)),
        TileLink(2, 3, (0.0, 48.0), 0.9, (0.0, 0.0)),
        TileLink(0, 3, (90.0, 90.0), 0.9, (42.0, 42.0)),   # false match
    ]

    positions, accepted = solve_links(nominal, links)

    assert len(accepted) == 4
    assert links[4].accepted is False
    assert positions[3] == pytest.approx((48.0, 48.0), abs=0.5)


def test_the_run_solve_leaves_a_consistent_layout_alone():
    nominal = [(0.0, 0.0), (0.0, 48.0), (48.0, 0.0)]
    links = [
        TileLink(0, 1, (0.0, 48.0), 0.9, (0.0, 0.0)),
        TileLink(0, 2, (48.0, 0.0), 0.9, (0.0, 0.0)),
    ]

    positions, accepted = solve_links(nominal, links)

    assert len(accepted) == 2
    assert positions == pytest.approx(nominal)


def test_a_tile_no_measurement_reached_keeps_its_commanded_place():
    nominal = [(0.0, 0.0), (0.0, 48.0), (500.0, 500.0)]
    links = [TileLink(0, 1, (0.0, 48.0), 0.9, (0.0, 0.0))]

    positions, _accepted = solve_links(nominal, links)

    assert positions[2] == pytest.approx((500.0, 500.0))
