# Copyright (C) 2020-2021 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Stitching animation GIF writer.

Provides a standalone function for creating animated GIFs that show the
tile-by-tile stitching process, with a final frame showing the weight-normalized
composite.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import numpy as np


def write_stitching_animation(
    tiles: Sequence[np.ndarray],
    grid_coords: Sequence[tuple[int, int]],
    step_px: tuple[int, int],
    pixel_size_um: float,
    outfile: str | Path,
    fps: int = 2,
    vmin: float = 0.0,
    vmax: float = 0.7,
) -> None:
    """Write an animated GIF showing tile-by-tile stitching process.
    
    Creates an animation where each frame shows a new tile being added to the
    stitched canvas. The final frames show the weight-normalized composite
    (accounting for overlapping regions).
    
    Parameters
    ----------
    tiles : Sequence[np.ndarray]
        List of 2-D tile images. All tiles must have the same shape.
    grid_coords : Sequence[tuple[int, int]]
        List of (sx, sy) grid coordinates for each tile, where sx and sy are
        integer grid positions (0-indexed).
    step_px : tuple[int, int]
        (step_x, step_y) step size in pixels between adjacent tiles.
    pixel_size_um : float
        Physical pixel size in micrometers for axis labels.
    outfile : str or Path
        Output GIF file path.
    fps : int, optional
        Frames per second for the animation. Default is 2.
    vmin : float, optional
        Minimum value for colormap normalization. Default is 0.0.
    vmax : float, optional
        Maximum value for colormap normalization. Default is 0.7.
    
    Returns
    -------
    None
        Writes the GIF to `outfile`.
    
    Notes
    -----
    The animation includes `len(tiles)` frames showing each tile addition,
    plus `3 * fps` extra frames showing the final weight-normalized result
    (to give viewers time to see the final image).
    
    Examples
    --------
    >>> tiles = [np.random.rand(64, 64) for _ in range(16)]
    >>> coords = [(i % 4, i // 4) for i in range(16)]
    >>> write_stitching_animation(
    ...     tiles, coords, step_px=(32, 32), pixel_size_um=0.5,
    ...     outfile='stitch.gif', fps=2
    ... )
    """
    import matplotlib.pyplot as plt
    from matplotlib import animation

    outfile = Path(outfile)

    if not tiles:
        raise ValueError("tiles list cannot be empty")
    
    if len(tiles) != len(grid_coords):
        raise ValueError(
            f"Number of tiles ({len(tiles)}) must match number of grid_coords ({len(grid_coords)})"
        )
    
    # Get tile shape (assume all tiles have same shape)
    img_shape = tiles[0].shape
    if len(img_shape) != 2:
        raise ValueError(f"Tiles must be 2-D arrays, got shape {img_shape}")
    
    # Normalize tiles to [0, 1]
    tiles_normalized = []
    all_max = max(np.max(t) for t in tiles)
    for tile in tiles:
        tiles_normalized.append(tile.astype(np.float32) / all_max)
    
    # Compute canvas dimensions from grid coordinates
    # Convention: (sx, sy) are (x, y) grid coords
    # Array indexing: [rows, cols] = [y, x]
    # So we need: rows for y-extent, cols for x-extent
    max_sx = max(sx for sx, sy in grid_coords)
    max_sy = max(sy for sx, sy in grid_coords)
    
    canvas_height = img_shape[0] + max_sy * step_px[1]
    canvas_width = img_shape[1] + max_sx * step_px[0]
    
    # Initialize canvas and weights
    full_image = np.zeros((canvas_height, canvas_width), dtype=np.float32)
    weights = np.zeros_like(full_image)
    
    # Setup figure
    fig, ax = plt.subplots(figsize=(6, 6))
    
    extent = (
        0,
        canvas_width * pixel_size_um,
        0,
        canvas_height * pixel_size_um,
    )
    
    im = ax.imshow(
        full_image,
        cmap='hot',
        origin="lower",
        vmin=vmin,
        vmax=vmax,
        extent=extent,
    )
    ax.set_title(f"Tiling Animation - Step 0")
    ax.set_xlabel("X [μm]")
    ax.set_ylabel("Y [μm]")
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    
    n_tiles = len(tiles)
    
    def _update(frame_idx: int):
        """Update function for animation."""
        nonlocal full_image, weights
        
        if frame_idx < n_tiles:
            # Add tile to canvas
            # Grid coords (sx, sy) are (x, y) positions
            # Array indexing is [rows, cols] = [y, x]
            tile = tiles_normalized[frame_idx]
            sx, sy = grid_coords[frame_idx]
            offset_x = sx * step_px[0]
            offset_y = sy * step_px[1]
            
            # Place tile: array[y_start:y_end, x_start:x_end]
            full_image[offset_y:offset_y + img_shape[0], offset_x:offset_x + img_shape[1]] += tile
            weights[offset_y:offset_y + img_shape[0], offset_x:offset_x + img_shape[1]] += 1
            
            stitched_image = full_image.copy()
            ax.set_title(f"Tiling Animation - Step {frame_idx + 1}")
        else:
            # Final frames: show weight-normalized composite
            weights_safe = weights.copy()
            weights_safe[weights_safe < 1] = 1
            stitched_image = full_image.copy() / weights_safe * np.max(weights_safe)
            ax.set_title(f"Final Tiled Image")
        
        im.set_data(stitched_image)
        return (im,)
    
    # Create animation
    anim = animation.FuncAnimation(
        fig,
        _update,
        frames=n_tiles + 3 * fps,
        interval=1000 / max(fps, 1),
        blit=False,
        repeat=True,
    )
    
    # Save as GIF
    writer = animation.PillowWriter(fps=fps, metadata=dict(loop=0))
    anim.save(str(outfile), writer=writer, dpi=100)
    
    plt.close(fig)
