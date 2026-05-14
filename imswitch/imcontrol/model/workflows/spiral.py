"""Square spiral coordinate generator."""
from __future__ import annotations
from typing import Generator

def spiral_moves(n_steps: int) -> Generator[tuple[int, int], None, None]:
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
