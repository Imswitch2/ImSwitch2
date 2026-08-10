"""Tile-visit orders for a rectangular grid.

Every generator here yields ``(dx, dy)`` *deltas* in whole tile steps, and the
caller performs one stage move and acquires one tile per delta. Skipping a
lattice point therefore costs nothing: the delta across it is summed, and the
stage makes one longer move rather than an extra stop. Stops always equal the
number of tiles.

Two orders, because they answer different questions:

``spiral``
    Grows outward from the start position, so a run stopped at any point still
    leaves a filled, centred mosaic. This is what you want when the extent is
    open-ended -- keep going until something interesting appears.

``serpentine``
    Rasters the rectangle, one tile step at a time, with no jump anywhere in
    the run. Cheapest possible travel, and every settle follows a single step,
    so a settle tuned for one step is right for the whole run. What you want
    when you know the area you are surveying and intend to finish it.

They also anchor the rectangle differently, and deliberately so. A spiral has
a centre, so the start position is the middle of the mosaic. A raster has a
corner, so the start position is that corner and the grid extends in +X and
+Y from it -- which is how you frame a raster in practice: put the stage at
one corner of what you want and say how far to go.
"""
from __future__ import annotations

from typing import Generator, Iterator, Tuple

SPIRAL = 'spiral'
SERPENTINE = 'serpentine'
PATTERNS = (SPIRAL, SERPENTINE)

Delta = Tuple[int, int]


def spiral_moves(n_steps: int) -> Generator[Delta, None, None]:
    """Yield (dx, dy) deltas that trace a square spiral.

    The first yield is always (0, 0) (the centre position).
    Subsequent yields step right, then rotate counter-clockwise,
    expanding the spiral after every two legs.

    Args:
        n_steps: Total number of positions (including the centre).
    """
    yield (0, 0)
    dx, dy = 1, 0
    step_limit = 1
    step_count = 0
    leg = 0
    for _ in range(n_steps - 1):
        yield (dx, dy)
        step_count += 1
        if step_count == step_limit:
            step_count = 0
            leg += 1
            dx, dy = -dy, dx
            if leg % 2 == 0:
                step_limit += 1


def _bounds(nx: int, ny: int) -> Tuple[int, int, int, int]:
    """Rectangle limits about the origin, biased so the start is inside it."""
    if nx < 1 or ny < 1:
        raise ValueError(f'A tiling grid needs at least 1x1 tiles, got {nx}x{ny}')
    half_x, half_y = (nx - 1) // 2, (ny - 1) // 2
    return -half_x, nx - 1 - half_x, -half_y, ny - 1 - half_y


def spiral_rect_moves(nx: int, ny: int) -> Generator[Delta, None, None]:
    """Spiral order restricted to an ``nx`` by ``ny`` rectangle.

    The square spiral already visits every lattice point outward from the
    centre, so a rectangle is a *filter* over it rather than a different walk:
    positions outside are dropped and their deltas folded into the next one
    that is kept. The stage never stops on a skipped position -- it makes one
    longer move -- so the number of settles and focus reacquisitions is exactly
    ``nx * ny``, as it would be for a square run of the same size.

    What the skips do cost is distance, and that grows with the aspect ratio
    because the walk keeps leaving the rectangle and coming back. At 12x8 it is
    about a quarter more travel than a raster; for a long strip it is several
    times more, and :func:`serpentine_moves` is the better answer there.
    """
    lo_x, hi_x, lo_y, hi_y = _bounds(nx, ny)
    total = nx * ny
    x = y = 0
    pending_x = pending_y = 0
    emitted = 0
    # The bounding square of the rectangle; every point inside it is reached.
    for dx, dy in spiral_moves(max(nx, ny) ** 2):
        x, y = x + dx, y + dy
        pending_x, pending_y = pending_x + dx, pending_y + dy
        if lo_x <= x <= hi_x and lo_y <= y <= hi_y:
            yield pending_x, pending_y
            pending_x = pending_y = 0
            emitted += 1
            if emitted == total:
                return


def serpentine_moves(nx: int, ny: int) -> Generator[Delta, None, None]:
    """Raster an ``nx`` by ``ny`` rectangle from the start position outward.

    The start position is the grid's **corner**, not its centre: the first
    delta is ``(0, 0)`` and the grid grows in +X and +Y from where the stage
    already is. So there is no jump anywhere in the run -- every delta after
    the first is one tile step, row turnarounds included, because the rows
    reverse direction rather than returning across the row.

    That is the whole point of this order. A settle tuned for one tile step is
    the right settle for every move in the run, and the total travel is
    ``nx * ny - 1`` steps, which is the least any order could manage.
    """
    if nx < 1 or ny < 1:
        raise ValueError(f'A tiling grid needs at least 1x1 tiles, got {nx}x{ny}')
    x = y = 0
    for row in range(ny):
        columns = range(nx) if row % 2 == 0 else range(nx - 1, -1, -1)
        for column in columns:
            yield column - x, row - y
            x, y = column, row


def tile_moves(nx: int, ny: int, pattern: str = SPIRAL) -> Iterator[Delta]:
    """Deltas visiting every tile of an ``nx`` by ``ny`` grid.

    ``pattern`` selects the order; see the module docstring for which to want.
    An unknown pattern is refused rather than defaulted, because silently
    choosing the other order changes both where a run starts and what a
    stopped run leaves behind.
    """
    if pattern == SPIRAL:
        return spiral_rect_moves(nx, ny)
    if pattern == SERPENTINE:
        return serpentine_moves(nx, ny)
    raise ValueError(
        f'Unknown tiling pattern {pattern!r}; expected one of '
        f'{", ".join(PATTERNS)}'
    )
