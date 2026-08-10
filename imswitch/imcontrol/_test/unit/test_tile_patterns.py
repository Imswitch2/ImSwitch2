"""Rectangular tile grids, and the two orders they can be visited in.

Both orders must cover the same ground and cost the same number of stops --
one settle and one focus reacquisition per tile. What differs is the order,
and therefore what a stopped run leaves behind and how far the stage travels.
"""

import pytest

from imswitch.imcontrol.model.workflows.spiral import (
    PATTERNS,
    SERPENTINE,
    SPIRAL,
    serpentine_moves,
    spiral_moves,
    spiral_rect_moves,
    tile_moves,
)

GRIDS = [(1, 1), (2, 2), (3, 1), (5, 5), (7, 3), (4, 6), (12, 8), (10, 2)]


def _positions(moves):
    x = y = 0
    out = []
    for dx, dy in moves:
        x, y = x + dx, y + dy
        out.append((x, y))
    return out


@pytest.mark.parametrize('pattern', PATTERNS)
@pytest.mark.parametrize('nx,ny', GRIDS)
def test_every_tile_is_visited_exactly_once(pattern, nx, ny):
    positions = _positions(tile_moves(nx, ny, pattern))

    assert len(positions) == nx * ny
    assert len(set(positions)) == nx * ny


@pytest.mark.parametrize('pattern', PATTERNS)
@pytest.mark.parametrize('nx,ny', GRIDS)
def test_the_grid_has_the_requested_extent(pattern, nx, ny):
    positions = _positions(tile_moves(nx, ny, pattern))
    xs = [x for x, _y in positions]
    ys = [y for _x, y in positions]

    assert max(xs) - min(xs) + 1 == nx
    assert max(ys) - min(ys) + 1 == ny


@pytest.mark.parametrize('nx,ny', GRIDS)
def test_the_spiral_centres_its_grid_on_the_start_position(nx, ny):
    """A spiral has a centre, so the start position is the middle."""
    positions = _positions(spiral_rect_moves(nx, ny))
    xs = [x for x, _y in positions]
    ys = [y for _x, y in positions]

    assert min(xs) <= 0 <= max(xs)
    assert min(ys) <= 0 <= max(ys)


@pytest.mark.parametrize('nx,ny', GRIDS)
def test_the_serpentine_corners_its_grid_on_the_start_position(nx, ny):
    """A raster has a corner, so the stage is framed at one and goes +X/+Y.

    Nothing is acquired behind the start position, which is what makes the
    control intuitive: put the stage where the area begins.
    """
    positions = _positions(serpentine_moves(nx, ny))

    assert positions[0] == (0, 0)
    assert min(x for x, _y in positions) == 0
    assert min(y for _x, y in positions) == 0
    assert max(x for x, _y in positions) == nx - 1
    assert max(y for _x, y in positions) == ny - 1


@pytest.mark.parametrize('pattern', PATTERNS)
@pytest.mark.parametrize('nx,ny', GRIDS)
def test_stops_equal_tiles_whatever_the_order(pattern, nx, ny):
    """The cost that matters: one settle and one refocus per tile, no more.

    Skipped lattice points are folded into the following delta, so the stage
    makes a longer move rather than an extra stop.
    """
    assert len(list(tile_moves(nx, ny, pattern))) == nx * ny


@pytest.mark.parametrize('nx,ny', GRIDS)
def test_serpentine_never_makes_a_move_longer_than_one_step(nx, ny):
    """Why serpentine exists: every settle follows a single tile step.

    There is no jump anywhere, not even at the start -- row turnarounds
    reverse direction rather than returning across the row.
    """
    moves = list(serpentine_moves(nx, ny))

    assert moves[0] == (0, 0)
    assert all(abs(dx) + abs(dy) == 1 for dx, dy in moves[1:])


@pytest.mark.parametrize('nx,ny', GRIDS)
def test_serpentine_travel_is_optimal(nx, ny):
    """No order can visit N positions in fewer than N-1 unit steps."""
    distance = sum(
        abs(dx) + abs(dy) for dx, dy in serpentine_moves(nx, ny)
    )

    assert distance == nx * ny - 1


@pytest.mark.parametrize('nx,ny', GRIDS)
def test_spiral_fills_outward_so_a_stopped_run_is_still_centred(nx, ny):
    """The property serpentine gives up, stated as the invariant it is.

    Truncating a spiral at any point leaves a set whose extent has grown
    outward from the start, never a band hanging off one side.
    """
    positions = _positions(spiral_rect_moves(nx, ny))

    for count in range(1, len(positions) + 1):
        seen = positions[:count]
        reach = max(max(abs(x), abs(y)) for x, y in seen)
        # Every point within the reached ring that the rectangle contains has
        # already been visited: the fill has no holes behind its frontier.
        inside = {
            (x, y)
            for x, y in positions
            if max(abs(x), abs(y)) < reach
        }
        assert inside <= set(seen)


def test_a_square_grid_visits_in_the_same_order_as_the_plain_spiral():
    """The rectangle filter is the old walk, not a replacement for it."""
    assert _positions(spiral_rect_moves(5, 5)) == _positions(spiral_moves(25))


def test_the_original_generator_is_unchanged():
    assert list(spiral_moves(1)) == [(0, 0)]
    assert list(spiral_moves(4)) == [(0, 0), (1, 0), (0, 1), (-1, 0)]


@pytest.mark.parametrize('pattern', PATTERNS)
def test_a_single_tile_is_a_no_op_move(pattern):
    assert list(tile_moves(1, 1, pattern)) == [(0, 0)]


def test_an_unknown_pattern_is_refused_not_defaulted():
    with pytest.raises(ValueError, match='Unknown tiling pattern'):
        list(tile_moves(3, 3, 'boustrophedon'))


@pytest.mark.parametrize('nx,ny', [(0, 3), (3, 0), (-1, 2)])
def test_an_empty_grid_is_refused(nx, ny):
    with pytest.raises(ValueError, match='at least 1x1'):
        list(tile_moves(nx, ny, SPIRAL))
    with pytest.raises(ValueError, match='at least 1x1'):
        list(tile_moves(nx, ny, SERPENTINE))
