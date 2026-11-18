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
            intensity_correction: Scale each tile so it matches its
                already-placed neighbours where they overlap. The comparison
                leaves out what the detector does: vignetting is fixed to the
                detector, so a tile's bright edge always overlaps a
                neighbour's dim one, and matching those as if they were sample
                darkened every tile further than the last -- a mosaic bright on
                one side and dark on the other (found on the rig, 2026-11-18).
                The shared profile is divided out (:meth:`_estimate_shading`)
                and whatever every overlap of one direction still agrees on is
                taken out too (:meth:`_direction_bias`). What is corrected is
                what changes from tile to tile; a brightness trend running
                uniformly across the whole mosaic cannot be told from
                vignetting and is left as captured. Call :meth:`finish` when a
                run ends.
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
        self.step_x_px = step_x_px
        self.step_y_px = step_y_px
        self.blend_overlaps = blend_overlaps
        self.intensity_correction = intensity_correction

        # Storage for tiles: {(grid_x, grid_y): np.ndarray}
        self._tiles: Dict[Tuple[int, int], np.ndarray] = {}

        # Intensity correction: each tile's gain, solved in insertion order
        # against the tiles placed before it, and the shared illumination
        # profile left out of that comparison (None until it can be estimated).
        self._gains: Dict[Tuple[int, int], float] = {}
        self._shading: np.ndarray | None = None
        # Tile count at the last full solve (profile, every overlap, every
        # gain); see _full_solve_due.
        self._fullSolveAt: int = 0
        # Every overlap measured so far, keyed by the pair in canonical order
        # (see :meth:`_canonical_pair`): its grid direction and the summed
        # intensity of each side over the overlap, profile divided out.
        self._links: Dict[
            Tuple[Tuple[int, int], Tuple[int, int]],
            Tuple[Tuple[int, int], float, float],
        ] = {}

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
            if self.intensity_correction:
                # Moved tiles overlap differently, so their gains change too.
                self._recompute_gains()
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

        if self.intensity_correction:
            if self._full_solve_due():
                self._full_solve()
                return
            if replacing:
                # Every gain depends on the tiles before it, so a replaced
                # tile re-solves them all and redraws once.
                self._recompute_gains()
                self._rebuild_canvas()
                return
            earlier = [key for key in self._tiles if key != (grid_x, grid_y)]
            self._gains[(grid_x, grid_y)] = self._gain_for((grid_x, grid_y), earlier)

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
            gain = self._gains.get((grid_x, grid_y), 1.0)
            tile_view = np.clip(tile_view * gain, 0, 1)

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

    # ------------------------------------------------------------------
    # Intensity correction
    # ------------------------------------------------------------------

    def finish(self) -> bool:
        """Solve every gain again against everything placed; True if redrawn.

        Between checkpoints (see :meth:`_full_solve_due`) a tile's gain is
        solved once, on arrival, against the profile and the direction biases
        known then. When a run ends everything is known, and solving again
        is what keeps the finished mosaic level: measured with 5 % tile
        flicker on a serpentine, 19 % left without it and 3 % with it. A
        no-op when nothing arrived since the last full solve.
        """
        if not self.intensity_correction or not self._tiles:
            return False
        if self._fullSolveAt == len(self._tiles):
            return False
        self._full_solve()
        return True

    def _full_solve_due(self) -> bool:
        """Whether this tile is a checkpoint for re-solving everything.

        First at the two counts the offline estimator itself defines -- the
        first at which a profile can be estimated at all, and the one from
        which it is trustworthy -- then each time the count doubles. A full
        solve redraws every tile, so doubling keeps the redraw work of a
        whole run under twice that of drawing each tile once; and the
        estimates' noise falls as one over the square root of the count, so
        each doubling is where they improve by the same factor.
        """
        from imswitch.imcommon.algorithms.tile_mosaic import (
            SHADING_MIN_TILES,
            SHADING_TRUSTWORTHY_TILES,
        )

        count = len(self._tiles)
        last = self._fullSolveAt
        for threshold in (SHADING_MIN_TILES, SHADING_TRUSTWORTHY_TILES):
            if last < threshold:
                return count >= threshold
        return count >= 2 * last

    def _full_solve(self) -> None:
        """Estimate the profile from every tile, re-solve every gain, redraw."""
        self._fullSolveAt = len(self._tiles)
        self._shading = self._estimate_shading()
        self._recompute_gains()
        self._rebuild_canvas()

    def _estimate_shading(self) -> np.ndarray | None:
        """The illumination profile every tile shares, or None.

        The offline tiling reconstructor's estimate ("Correct vignetting"):
        averaged in detector coordinates, the specimen cancels and the
        illumination envelope remains. None when it cannot be trusted; the
        comparison then uses the tiles as captured, as it always did.

        Two choices differ from the offline default, both because of where
        and when this runs. The border is fitted as a local plane rather than
        held, since overlaps are exactly the tile borders and a held border
        flattens the falloff there -- enough to leave a 15 % drift across five
        tiles. And the smoothing runs on a block-averaged copy: at full
        resolution it took 8.5 s per call on 2048 px tiles, in the tiling
        worker, between two tiles.
        """
        from imswitch.imcommon.algorithms.tile_mosaic import (
            SHADING_EDGES_LINEAR,
            SHADING_SAMPLES_PER_SIGMA,
            estimate_shading_profile,
        )

        try:
            profile = estimate_shading_profile(
                self._tiles.values(),
                edges=SHADING_EDGES_LINEAR,
                samples_per_sigma=SHADING_SAMPLES_PER_SIGMA,
            )
        except Exception:
            return None
        if profile is None or tuple(profile.shape) != tuple(self.tile_shape_px):
            return None
        return profile

    def _recompute_gains(self) -> None:
        """Measure every overlap again, then solve every gain in arrival order.

        Measuring all of them first means each direction's bias (see
        :meth:`_direction_bias`) is taken over the whole mosaic rather than
        over whatever had arrived when a tile was placed.
        """
        self._gains = {}
        self._links = {}
        keys = list(self._tiles)
        for index, key in enumerate(keys):
            for other in keys[:index]:
                self._link(other, key)
        earlier = []
        for key in keys:
            self._gains[key] = self._gain_for(key, earlier)
            earlier.append(key)

    @staticmethod
    def _canonical_pair(first, second):
        """The pair ordered so its grid step points right, or down when vertical.

        Returns ``((a, b), direction)`` with ``direction = b - a`` in grid
        units. Every overlap in the same direction compares the same two
        stretches of the detector, which is what makes them comparable.
        """
        step = (second[0] - first[0], second[1] - first[1])
        if step[0] < 0 or (step[0] == 0 and step[1] < 0):
            return (second, first), (-step[0], -step[1])
        return (first, second), step

    def _link(self, first, second):
        """Measure (once) and remember the overlap between two placed tiles.

        Returns ``((a, b), direction, sum_a, sum_b)`` in canonical order, or
        None when they do not overlap or one side is empty. The sums are over
        the shared pixels with the illumination profile divided out of both
        sides when it is known: the two sides sit at different places on the
        detector, so without that the shading itself reads as a brightness
        difference.
        """
        pair, direction = self._canonical_pair(first, second)
        cached = self._links.get(pair)
        if cached is not None:
            return (pair,) + cached
        a, b = pair
        tile_a, tile_b = self._tiles.get(a), self._tiles.get(b)
        place_a, place_b = self._placements.get(a), self._placements.get(b)
        if tile_a is None or tile_b is None or place_a is None or place_b is None:
            return None
        r0 = max(place_a[0], place_b[0])
        r1 = min(place_a[0] + tile_a.shape[0], place_b[0] + tile_b.shape[0])
        c0 = max(place_a[1], place_b[1])
        c1 = min(place_a[1] + tile_a.shape[1], place_b[1] + tile_b.shape[1])
        if r1 <= r0 or c1 <= c0:
            return None
        in_a = (slice(r0 - place_a[0], r1 - place_a[0]),
                slice(c0 - place_a[1], c1 - place_a[1]))
        in_b = (slice(r0 - place_b[0], r1 - place_b[0]),
                slice(c0 - place_b[1], c1 - place_b[1]))
        patch_a, patch_b = tile_a[in_a], tile_b[in_b]
        if self._shading is not None:
            patch_a = patch_a / self._shading[in_a]
            patch_b = patch_b / self._shading[in_b]
        sum_a, sum_b = float(patch_a.sum()), float(patch_b.sum())
        floor = float(np.finfo(np.float32).eps)
        if sum_a <= floor or sum_b <= floor:
            return None
        self._links[pair] = (direction, sum_a, sum_b)
        return pair, direction, sum_a, sum_b

    def _direction_bias(self, direction) -> float:
        """What every overlap in one grid direction disagrees by, as a log ratio.

        The median over all of them of ``log(sum_b / sum_a)``. Whatever the
        profile estimate still gets wrong at the tile border is the same in
        every tile, so it biases every overlap of that direction alike -- and
        chained from tile to tile, a bias of a few percent per overlap became
        a mosaic a third darker on one side (measured on blobby specimens).
        A real brightness change belongs to one tile and so to a minority of
        overlaps, which the median leaves out; it is still corrected. What
        cannot be told apart from vignetting -- a brightness trend that runs
        uniformly across the whole mosaic -- is left alone, as with the
        correction off.
        """
        ratios = [
            np.log(sum_b / sum_a)
            for step, sum_a, sum_b in self._links.values()
            if step == direction
        ]
        return float(np.median(ratios)) if ratios else 0.0

    def _gain_for(self, key, earlier) -> float:
        """The gain that makes ``key`` agree with ``earlier`` tiles where they overlap.

        Pooled over every overlap with an earlier tile (each at its own
        gain), each overlap's direction bias taken out. 1.0 when nothing
        overlaps yet.
        """
        existing = 0.0
        incoming = 0.0
        for other in earlier:
            link = self._link(other, key)
            if link is None:
                continue
            (a, _), direction, sum_a, sum_b = link
            bias = self._direction_bias(direction)
            if a == other:
                theirs, mine, correction = sum_a, sum_b, np.exp(bias)
            else:
                theirs, mine, correction = sum_b, sum_a, np.exp(-bias)
            existing += theirs * self._gains.get(other, 1.0) * correction
            incoming += mine
        if incoming <= 0.0 or existing <= 0.0:
            return 1.0
        return existing / incoming

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
