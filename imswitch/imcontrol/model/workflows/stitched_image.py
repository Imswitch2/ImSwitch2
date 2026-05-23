from __future__ import annotations

from typing import Dict, Tuple

import numpy as np


class StitchedImage:
    """In-memory stitched overview from a grid of tiles."""

    def __init__(
        self,
        tile_size_px: int | None,
        tile_step_um: float,
        px_per_um: float | None = None,
        overlap: float = 0.0,
        *,
        tile_shape_px: Tuple[int, int] | None = None,
        pixel_size_um: float | Tuple[float, float] | None = None,
        blend_overlaps: bool = True,
        intensity_correction: bool = False,
    ) -> None:
        """Initialize a tile stitcher.

        Args:
            tile_size_px: Camera pixels per tile for legacy square-tile callers.
            tile_step_um: Stage step between tile centres, in µm.
            px_per_um: Legacy calibration, in pixels per µm.
            overlap: Legacy fractional overlap, used only when pixel size is unknown.
            tile_shape_px: Tile shape as ``(height, width)``.
            pixel_size_um: Detector pixel size as ``(y, x)`` or one scalar.
            blend_overlaps: Average overlapping pixels instead of overwriting.
            intensity_correction: Match each new tile's overlap mean to existing canvas.
        """
        if not 0 <= overlap < 1:
            raise ValueError(f"overlap must be in [0, 1), got {overlap}")

        if tile_shape_px is None:
            if tile_size_px is None:
                raise ValueError("tile_shape_px or tile_size_px must be provided")
            tile_shape_px = (tile_size_px, tile_size_px)

        if pixel_size_um is not None:
            if isinstance(pixel_size_um, tuple):
                pixel_size_y_um, pixel_size_x_um = pixel_size_um
            else:
                pixel_size_y_um = pixel_size_x_um = pixel_size_um
            if pixel_size_x_um <= 0 or pixel_size_y_um <= 0:
                raise ValueError(f"pixel_size_um must be positive, got {pixel_size_um}")
            px_per_um_x = 1 / pixel_size_x_um
            px_per_um_y = 1 / pixel_size_y_um
            step_x_px = max(1, int(round(tile_step_um * px_per_um_x)))
            step_y_px = max(1, int(round(tile_step_um * px_per_um_y)))
        else:
            if px_per_um is None:
                raise ValueError("px_per_um or pixel_size_um must be provided")
            px_per_um_x = px_per_um_y = px_per_um
            step_x_px = step_y_px = max(1, int(round(tile_step_um * px_per_um)))

        self.tile_size_px = max(tile_shape_px)
        self.tile_shape_px = tile_shape_px
        self.tile_step_um = tile_step_um
        self.px_per_um = px_per_um_x
        self.px_per_um_x = px_per_um_x
        self.px_per_um_y = px_per_um_y
        self.overlap = overlap
        self.effective_tile_step_px = step_x_px
        self.step_x_px = step_x_px
        self.step_y_px = step_y_px
        self.blend_overlaps = blend_overlaps
        self.intensity_correction = intensity_correction

        # Storage for tiles: {(grid_x, grid_y): np.ndarray}
        self._tiles: Dict[Tuple[int, int], np.ndarray] = {}

    def add_tile(self, image: np.ndarray, grid_x: int, grid_y: int) -> None:
        """Add one tile at grid position (grid_x, grid_y).

        Grid (0,0) is the spiral centre.
        image must be 2-D (H, W) or 3-D (H, W, C); values uint16 or float.

        Args:
            image: Tile image array.
            grid_x: Grid column position.
            grid_y: Grid row position.
        """
        if image.ndim not in (2, 3):
            raise ValueError(f"image must be 2-D or 3-D, got shape {image.shape}")

        # Normalize to float32 in [0, 1]
        if np.issubdtype(image.dtype, np.integer):
            # Integer type: normalize by max value
            max_val = np.iinfo(image.dtype).max
            normalized = image.astype(np.float32) / max_val
        else:
            # Float type: clip to [0, 1]
            normalized = np.clip(image.astype(np.float32), 0, 1)

        # If 3-D, convert to grayscale by averaging channels
        if normalized.ndim == 3:
            normalized = normalized.mean(axis=2)

        self._tiles[(grid_x, grid_y)] = normalized

    def get_overview(self) -> np.ndarray:
        """Return the current stitched image as float32 in [0, 1].

        Overlaps are averaged by default.
        Returns a 2-D array (H_total, W_total).
        Canvas grows to accommodate any grid extent seen so far.
        """
        if not self._tiles:
            return np.zeros((self.tile_size_px, self.tile_size_px), dtype=np.float32)

        # Find grid extents
        grid_positions = list(self._tiles.keys())
        grid_xs = [pos[0] for pos in grid_positions]
        grid_ys = [pos[1] for pos in grid_positions]

        min_gx, max_gx = min(grid_xs), max(grid_xs)
        min_gy, max_gy = min(grid_ys), max(grid_ys)

        # Calculate canvas size
        # Number of tiles in each direction
        n_tiles_x = max_gx - min_gx + 1
        n_tiles_y = max_gy - min_gy + 1

        # Canvas dimensions accounting for overlap
        canvas_width = self.step_x_px * (n_tiles_x - 1) + self.tile_shape_px[1]
        canvas_height = self.step_y_px * (n_tiles_y - 1) + self.tile_shape_px[0]

        canvas_sum = np.zeros((canvas_height, canvas_width), dtype=np.float32)
        canvas_weight = np.zeros((canvas_height, canvas_width), dtype=np.float32)

        # Paste tiles onto canvas
        for (grid_x, grid_y), tile in self._tiles.items():
            # Calculate pixel position in canvas
            # Relative grid position from min corner
            rel_gx = grid_x - min_gx
            rel_gy = grid_y - min_gy

            # Top-left corner of this tile in canvas
            col_start = rel_gx * self.step_x_px
            row_start = rel_gy * self.step_y_px

            # Ensure tile fits (crop if necessary)
            tile_h = min(tile.shape[0], canvas_height - row_start)
            tile_w = min(tile.shape[1], canvas_width - col_start)

            tile_view = tile[:tile_h, :tile_w]
            row_end = row_start + tile_h
            col_end = col_start + tile_w
            sum_view = canvas_sum[row_start:row_end, col_start:col_end]
            weight_view = canvas_weight[row_start:row_end, col_start:col_end]

            if self.intensity_correction:
                tile_view = self._match_overlap_intensity(tile_view, sum_view, weight_view)

            if self.blend_overlaps:
                sum_view += tile_view
                weight_view += 1.0
            else:
                sum_view[:] = tile_view
                weight_view[:] = 1.0

        canvas = np.zeros_like(canvas_sum)
        np.divide(canvas_sum, canvas_weight, out=canvas, where=canvas_weight > 0)
        return canvas

    def _match_overlap_intensity(
        self,
        tile: np.ndarray,
        existing_sum: np.ndarray,
        existing_weight: np.ndarray,
    ) -> np.ndarray:
        """Scale a tile to match the mean intensity in existing overlap."""
        overlap_mask = existing_weight > 0
        if not np.any(overlap_mask):
            return tile

        existing = existing_sum[overlap_mask] / existing_weight[overlap_mask]
        incoming = tile[overlap_mask]
        incoming_mean = float(np.mean(incoming))
        if incoming_mean <= np.finfo(np.float32).eps:
            return tile

        scale = float(np.mean(existing)) / incoming_mean
        return np.clip(tile * scale, 0, 1)

    def pixel_to_stage(
        self, row: int, col: int, origin_stage_xy: Tuple[float, float]
    ) -> Tuple[float, float]:
        """Convert a pixel coordinate in the overview to stage (x, y) in µm.

        Args:
            row: Row in the overview canvas.
            col: Column in the overview canvas.
            origin_stage_xy: Stage position (x, y) of the top-left corner of the canvas.

        Returns:
            Tuple of (stage_x, stage_y) in µm.
        """
        # Convert pixel offset to µm offset
        stage_x = origin_stage_xy[0] + col / self.px_per_um_x
        stage_y = origin_stage_xy[1] + row / self.px_per_um_y

        return (stage_x, stage_y)

    def stage_to_pixel(
        self, stage_x: float, stage_y: float, origin_stage_xy: Tuple[float, float]
    ) -> Tuple[int, int]:
        """Inverse of pixel_to_stage.

        Args:
            stage_x: Stage X position in µm.
            stage_y: Stage Y position in µm.
            origin_stage_xy: Stage position (x, y) of the top-left corner of the canvas.

        Returns:
            Tuple of (row, col) in the overview canvas.
        """
        # Convert µm offset to pixel offset
        col = int((stage_x - origin_stage_xy[0]) * self.px_per_um_x)
        row = int((stage_y - origin_stage_xy[1]) * self.px_per_um_y)

        return (row, col)
