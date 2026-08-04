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

        # Incremental canvas state. The canvas is accumulated as tiles arrive
        # instead of being rebuilt from every tile on each get_overview() call:
        # a live scan asks for the overview once per tile, and rebuilding made
        # that O(n_tiles^2) work plus two full-canvas allocations per tile,
        # which stalled the GUI thread badly enough to starve the focus lock.
        self._canvas_sum: np.ndarray | None = None
        self._canvas_weight: np.ndarray | None = None
        self._overview_cache: np.ndarray | None = None

        # Each tile's top-left corner in a virtual pixel space where the tile
        # at grid (0, 0) sits at (0, 0). Placement lives here rather than being
        # derived from the grid index, so a tile can be nudged off its nominal
        # position by image registration without disturbing the rest.
        self._placements: Dict[Tuple[int, int], Tuple[int, int]] = {}
        self._canvas_row0: int = 0
        self._canvas_col0: int = 0

    def nominal_placement(self, grid_x: int, grid_y: int) -> Tuple[int, int]:
        """Top-left virtual pixel coordinate a tile would occupy uncorrected."""
        return (grid_y * self.step_y_px, grid_x * self.step_x_px)

    @property
    def canvas_origin_px(self) -> Tuple[int, int]:
        """Virtual coordinate of canvas pixel (0, 0), as ``(row, col)``."""
        return (self._canvas_row0, self._canvas_col0)

    def placement(self, grid_x: int, grid_y: int) -> Tuple[int, int] | None:
        """Where a placed tile actually sits, in virtual pixels."""
        return self._placements.get((grid_x, grid_y))

    def canvas_region_for(
        self, grid_x: int, grid_y: int, offset_px: Tuple[float, float] = (0.0, 0.0)
    ):
        """Return ``(canvas_patch, nominal_offset)`` for registering a new tile.

        ``canvas_patch`` is the current canvas, and ``nominal_offset`` is where
        the incoming tile's origin would land inside it. Returns None before
        any tile has been placed.
        """
        if self._canvas_sum is None:
            return None
        row0, col0 = self.nominal_placement(grid_x, grid_y)
        overview = self.get_overview()
        return overview, (
            row0 + offset_px[0] - self._canvas_row0,
            col0 + offset_px[1] - self._canvas_col0,
        )

    def placed_neighbours(
        self,
        grid_x: int,
        grid_y: int,
        offset_px: Tuple[float, float] = (0.0, 0.0),
        min_overlap_px: int = 16,
    ):
        """Already-placed tiles that overlap where a new tile is about to go.

        Returns ``[(key, tile, nominal_offset), ...]``, where ``nominal_offset``
        is the incoming tile's origin expressed in that neighbour's own pixel
        coordinates — what :func:`estimate_shift` wants as its expected offset.

        Registering against each neighbour separately, rather than against the
        one canvas region, is what makes a spiral's redundancy usable: most
        tiles touch two to four already-placed ones, so a single bad match can
        be outvoted instead of deciding the placement by itself.
        """
        if not self._placements:
            return []

        row0, col0 = self.nominal_placement(grid_x, grid_y)
        row0 += offset_px[0]
        col0 += offset_px[1]
        height, width = self.tile_shape_px

        found = []
        for key, placement in self._placements.items():
            if key == (grid_x, grid_y):
                continue
            tile = self._tiles.get(key)
            if tile is None:
                continue
            offset = (row0 - placement[0], col0 - placement[1])
            shared_rows = min(tile.shape[0], offset[0] + height) - max(0, offset[0])
            shared_cols = min(tile.shape[1], offset[1] + width) - max(0, offset[1])
            if shared_rows < min_overlap_px or shared_cols < min_overlap_px:
                continue
            found.append((key, tile, offset))
        return found

    def set_placements(self, placements: Dict[Tuple[int, int], Tuple[int, int]]
                       ) -> None:
        """Move already-placed tiles wholesale, then redraw.

        For a post-run global solve, which revisits every tile at once rather
        than one at a time. Keys not already placed are ignored.
        """
        changed = False
        for key, placement in placements.items():
            if key not in self._placements:
                continue
            rounded = (int(round(placement[0])), int(round(placement[1])))
            if self._placements[key] != rounded:
                self._placements[key] = rounded
                changed = True
        if changed:
            self._overview_cache = None
            self._rebuild_canvas()

    def add_tile(
        self,
        image: np.ndarray,
        grid_x: int,
        grid_y: int,
        offset_px: Tuple[float, float] = (0.0, 0.0),
    ) -> None:
        """Add one tile at grid position (grid_x, grid_y).

        Grid (0,0) is the spiral centre.
        image must be 2-D (H, W) or 3-D (H, W, C); values uint16 or float.

        Args:
            image: Tile image array.
            grid_x: Grid column position.
            grid_y: Grid row position.
            offset_px: Correction ``(dy, dx)`` applied to the nominal placement,
                in canvas pixels. Used by image-registration refinement; the
                default of zero reproduces pure commanded-position placement.
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

        replacing = (grid_x, grid_y) in self._tiles
        nominal_row, nominal_col = self.nominal_placement(grid_x, grid_y)
        placement = (
            int(round(nominal_row + offset_px[0])),
            int(round(nominal_col + offset_px[1])),
        )

        self._tiles[(grid_x, grid_y)] = normalized
        self._placements[(grid_x, grid_y)] = placement
        self._overview_cache = None

        if replacing:
            # A tile's old contribution cannot be subtracted back out of the
            # accumulator, so replacement falls back to a full rebuild. This
            # does not happen during a normal scan, where each grid position
            # is visited once.
            self._rebuild_canvas()
        else:
            self._grow_canvas_for()
            self._paste_tile(normalized, grid_x, grid_y)

    def get_overview(self) -> np.ndarray:
        """Return the current stitched image as float32 in [0, 1].

        Overlaps are averaged by default.
        Returns a 2-D array (H_total, W_total).
        Canvas grows to accommodate any grid extent seen so far.
        """
        if not self._tiles or self._canvas_sum is None:
            return np.zeros((self.tile_size_px, self.tile_size_px), dtype=np.float32)

        if self._overview_cache is None:
            canvas = np.zeros_like(self._canvas_sum)
            np.divide(
                self._canvas_sum,
                self._canvas_weight,
                out=canvas,
                where=self._canvas_weight > 0,
            )
            self._overview_cache = canvas

        # Callers have always received an array they may modify freely, so hand
        # out a copy rather than the cached accumulator result.
        return self._overview_cache.copy()

    # ------------------------------------------------------------------
    # Incremental canvas maintenance
    # ------------------------------------------------------------------

    def _canvas_bounds(self):
        """Virtual-coordinate bounding box over all placed tiles."""
        rows = [placement[0] for placement in self._placements.values()]
        cols = [placement[1] for placement in self._placements.values()]
        tile_h, tile_w = self.tile_shape_px
        return (
            min(rows), min(cols),
            max(row + tile_h for row in rows),
            max(col + tile_w for col in cols),
        )

    def _grow_canvas_for(self) -> None:
        """Ensure the canvas covers every placed tile, growing if needed.

        Growth reallocates and copies the existing accumulator, which happens
        only when the mosaic's bounding box expands — O(sqrt(n_tiles)) times
        for a spiral, not once per tile.
        """
        row0, col0, row1, col1 = self._canvas_bounds()
        height, width = row1 - row0, col1 - col0

        if self._canvas_sum is None:
            self._canvas_sum = np.zeros((height, width), dtype=np.float32)
            self._canvas_weight = np.zeros((height, width), dtype=np.float32)
            self._canvas_row0 = row0
            self._canvas_col0 = col0
            return

        if (height, width) == self._canvas_sum.shape and row0 == self._canvas_row0 \
                and col0 == self._canvas_col0:
            return

        # Offset of the old canvas inside the new, larger one.
        row_offset = self._canvas_row0 - row0
        col_offset = self._canvas_col0 - col0

        old_h, old_w = self._canvas_sum.shape
        new_sum = np.zeros((height, width), dtype=np.float32)
        new_weight = np.zeros((height, width), dtype=np.float32)
        new_sum[row_offset:row_offset + old_h, col_offset:col_offset + old_w] = self._canvas_sum
        new_weight[row_offset:row_offset + old_h, col_offset:col_offset + old_w] = self._canvas_weight

        self._canvas_sum = new_sum
        self._canvas_weight = new_weight
        self._canvas_row0 = row0
        self._canvas_col0 = col0

    def _paste_tile(self, tile: np.ndarray, grid_x: int, grid_y: int) -> None:
        """Accumulate one already-normalized tile into the canvas."""
        canvas_height, canvas_width = self._canvas_sum.shape

        placement = self._placements[(grid_x, grid_y)]
        row_start = placement[0] - self._canvas_row0
        col_start = placement[1] - self._canvas_col0

        # Ensure tile fits (crop if necessary)
        tile_h = min(tile.shape[0], canvas_height - row_start)
        tile_w = min(tile.shape[1], canvas_width - col_start)

        tile_view = tile[:tile_h, :tile_w]
        sum_view = self._canvas_sum[row_start:row_start + tile_h, col_start:col_start + tile_w]
        weight_view = self._canvas_weight[row_start:row_start + tile_h, col_start:col_start + tile_w]

        if self.intensity_correction:
            tile_view = self._match_overlap_intensity(tile_view, sum_view, weight_view)

        if self.blend_overlaps:
            sum_view += tile_view
            weight_view += 1.0
        else:
            sum_view[:] = tile_view
            weight_view[:] = 1.0

    def _rebuild_canvas(self) -> None:
        """Rebuild the accumulator from every stored tile, in insertion order."""
        self._canvas_sum = None
        self._canvas_weight = None
        self._overview_cache = None
        self._grow_canvas_for()
        for (grid_x, grid_y), tile in self._tiles.items():
            self._paste_tile(tile, grid_x, grid_y)

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
