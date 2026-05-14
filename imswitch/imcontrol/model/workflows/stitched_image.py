"""In-memory tile stitching for microscopy overviews."""
from __future__ import annotations
import numpy as np
from typing import Dict, Tuple


class StitchedImage:
    """In-memory stitched overview from a grid of tiles."""

    def __init__(
        self,
        tile_size_px: int,
        tile_step_um: float,
        px_per_um: float,
        overlap: float = 0.1,
    ) -> None:
        """Initialize a tile stitcher.

        Args:
            tile_size_px: Camera pixels per tile (assumed square).
            tile_step_um: Stage step between tile centres, in µm.
            px_per_um: Calibration: pixels per µm in the final image.
            overlap: Fraction of tile_size_px that overlaps [0, 1).
        """
        if not 0 <= overlap < 1:
            raise ValueError(f"overlap must be in [0, 1), got {overlap}")
        
        self.tile_size_px = tile_size_px
        self.tile_step_um = tile_step_um
        self.px_per_um = px_per_um
        self.overlap = overlap
        
        # Effective step in pixels after accounting for overlap
        self.effective_tile_step_px = int(tile_size_px * (1 - overlap))
        
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

        Tiles placed with simple paste (no blending needed for v1).
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
        canvas_width = self.effective_tile_step_px * (n_tiles_x - 1) + self.tile_size_px
        canvas_height = self.effective_tile_step_px * (n_tiles_y - 1) + self.tile_size_px
        
        canvas = np.zeros((canvas_height, canvas_width), dtype=np.float32)
        
        # Paste tiles onto canvas
        for (grid_x, grid_y), tile in self._tiles.items():
            # Calculate pixel position in canvas
            # Relative grid position from min corner
            rel_gx = grid_x - min_gx
            rel_gy = grid_y - min_gy
            
            # Top-left corner of this tile in canvas
            col_start = rel_gx * self.effective_tile_step_px
            row_start = rel_gy * self.effective_tile_step_px
            
            # Paste tile (last writer wins at overlaps)
            row_end = row_start + tile.shape[0]
            col_end = col_start + tile.shape[1]
            
            # Ensure tile fits (crop if necessary)
            tile_h = min(tile.shape[0], canvas_height - row_start)
            tile_w = min(tile.shape[1], canvas_width - col_start)
            
            canvas[row_start:row_start + tile_h, col_start:col_start + tile_w] = \
                tile[:tile_h, :tile_w]
        
        return canvas

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
        stage_x = origin_stage_xy[0] + col / self.px_per_um
        stage_y = origin_stage_xy[1] + row / self.px_per_um
        
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
        col = int((stage_x - origin_stage_xy[0]) * self.px_per_um)
        row = int((stage_y - origin_stage_xy[1]) * self.px_per_um)
        
        return (row, col)


if __name__ == "__main__":
    print("=== StitchedImage self-test ===")
    
    # Create stitcher with 20% overlap
    stitcher = StitchedImage(
        tile_size_px=100,
        tile_step_um=80,
        px_per_um=1.0,
        overlap=0.2
    )
    
    print(f"Effective tile step: {stitcher.effective_tile_step_px} px")
    
    # Add 4 tiles in a 2x2 grid
    np.random.seed(42)
    for gx in [0, 1]:
        for gy in [0, 1]:
            tile = np.random.randint(0, 65535, (100, 100), dtype=np.uint16)
            stitcher.add_tile(tile, gx, gy)
            print(f"Added tile at grid ({gx}, {gy})")
    
    # Get stitched overview
    overview = stitcher.get_overview()
    print(f"\nOverview shape: {overview.shape}")
    print(f"Overview dtype: {overview.dtype}")
    print(f"Overview value range: [{overview.min():.3f}, {overview.max():.3f}]")
    
    # Test coordinate conversion
    origin = (0.0, 0.0)
    stage_pos = stitcher.pixel_to_stage(50, 50, origin)
    print(f"\nPixel (50, 50) → Stage {stage_pos}")
    
    pixel_pos = stitcher.stage_to_pixel(stage_pos[0], stage_pos[1], origin)
    print(f"Stage {stage_pos} → Pixel {pixel_pos}")
    
    # Test with a different origin
    origin2 = (100.0, 200.0)
    stage_pos2 = stitcher.pixel_to_stage(50, 50, origin2)
    print(f"\nWith origin {origin2}:")
    print(f"Pixel (50, 50) → Stage {stage_pos2}")
    
    print("\n✓ Self-test completed successfully")
