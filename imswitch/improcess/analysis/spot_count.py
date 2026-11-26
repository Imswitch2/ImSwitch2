"""Find diffraction-limited spots and count them: SMLM's detection, without the fit.

The detector is the SMLM localizer's Picasso net gradient
(:func:`~imswitch.improcess.reconstructors.smlm.detection.net_gradient_map`):
at every pixel the image slopes are projected onto the direction pointing
back to that pixel and summed over a small box. A spot's flanks all slope
toward its centre and add up; a smooth background does not. A linear ramp
cancels exactly over the symmetric box, and a broad hump contributes in
proportion to its curvature, which for a structure ten times wider than the
PSF is a hundredth of a spot's of the same height. That is what makes the
score usable on an inhomogeneous background without a separate background
subtraction step.

Two things are added on top of the SMLM detection, both for counting rather
than localizing:

* a **relative** threshold, in multiples of the frame's robust noise of the
  net-gradient map (``median + k * 1.4826 * MAD``), so one number carries
  over between frames and tiles of different brightness;
* **edge rejection**: just inside the edge of a large bright structure the
  net gradient is high too (higher than a spot's when the edge is brighter),
  but the image there curves across the edge and not along it. A candidate
  is kept only where the smoothed image is a round peak, the ratio of its
  Hessian eigenvalues at least :data:`MIN_ISOTROPY`. Convex corners of
  such structures still pass: locally they look like a spot.

With ``threshold_mode="absolute"`` and ``reject_edges=False`` the candidates
are exactly :func:`~imswitch.improcess.reconstructors.smlm.detection.detect_spots`'s.

The tiling half (:func:`tile_spot_targets`) turns per-tile detections into
stage positions, with the same orientation convention the tiling manifest
and :func:`~imswitch.imcommon.algorithms.tile_mosaic._positions_from_stage`
use, so a target is where the stage has to go to put the spot where the
tile's reference pixel (its centre) was.

No Qt: numpy/scipy in, plain columns out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np
from scipy.ndimage import gaussian_filter, maximum_filter, uniform_filter

THRESHOLD_MODES = ("absolute", "relative")
#: Where a tile target is placed: the tile's centre, or the mean position of
#: the spots found in it.
TARGET_MODES = ("spot centroid", "tile centre")

#: Columns every spot table carries, in order.
SPOT_COLUMNS = ("frame", "y", "x", "net_gradient", "intensity", "contrast")

_MAD_TO_SIGMA = 1.4826

#: Minimum :func:`isotropy` of a kept spot when edges are rejected. Measured on
#: synthetic Poisson frames (PSF sigma 1.4 px, a bright 800-count square on a
#: ramp and a hump): real spots scored 0.69-0.98, candidates along the
#: square's edges mostly below 0.12; only its corners scored like spots.
MIN_ISOTROPY = 0.4


@dataclass(frozen=True)
class SpotParams:
    """The detection settings, shared by the time-stack and the tiling processor."""

    threshold: float = 500.0
    threshold_mode: str = "absolute"
    sigma: float = 1.0
    roi: int = 7
    reject_edges: bool = True

    @classmethod
    def from_params(cls, params: Mapping[str, Any] | None) -> "SpotParams":
        params = dict(params or {})
        mode = str(params.get("threshold_mode", cls.threshold_mode))
        if mode not in THRESHOLD_MODES:
            raise ValueError(
                f"threshold_mode must be one of {THRESHOLD_MODES}, got {mode!r}"
            )
        roi = int(params.get("roi", cls.roi))
        if roi < 3:
            raise ValueError(f"roi must be at least 3 px, got {roi}")
        sigma = float(params.get("sigma", cls.sigma))
        if sigma < 0:
            raise ValueError(f"sigma must not be negative, got {sigma}")
        return cls(
            threshold=float(params.get("threshold", cls.threshold)),
            threshold_mode=mode,
            sigma=sigma,
            roi=roi,
            reject_edges=bool(params.get("reject_edges", cls.reject_edges)),
        )

    @property
    def box(self) -> int:
        """The odd box size the net gradient and the suppression use."""
        return max(3, int(self.roi) | 1)


def robust_noise(values: np.ndarray) -> tuple[float, float]:
    """``(median, sigma)`` of ``values``, sigma from the median absolute deviation."""
    flat = np.asarray(values, dtype=np.float64).ravel()
    if flat.size == 0:
        return 0.0, 0.0
    median = float(np.median(flat))
    mad = float(np.median(np.abs(flat - median)))
    return median, _MAD_TO_SIGMA * mad


def effective_threshold(net_gradient: np.ndarray, params: SpotParams) -> float:
    """The net-gradient value a candidate has to exceed in this frame."""
    if params.threshold_mode == "absolute":
        return float(params.threshold)
    median, sigma = robust_noise(net_gradient)
    return median + float(params.threshold) * sigma


def isotropy(smoothed: np.ndarray, coords: np.ndarray) -> np.ndarray:
    """How round the intensity peak at each ``(row, col)`` is, in ``[0, 1]``.

    The ratio of the Hessian's two eigenvalues where both are negative (a
    peak): about 1 for a round spot, about 0 along a ridge or just inside an
    edge, where the image curves across the edge but not along it. 0 where
    the image is not curved downward at all.
    """
    gy, gx = np.gradient(np.asarray(smoothed, dtype=np.float64))
    rows, cols = coords[:, 0], coords[:, 1]
    # Second derivatives only where they are needed.
    hyy = np.gradient(gy, axis=0)[rows, cols]
    hxx = np.gradient(gx, axis=1)[rows, cols]
    hxy = 0.5 * (np.gradient(gy, axis=1)[rows, cols] + np.gradient(gx, axis=0)[rows, cols])
    half_trace = 0.5 * (hyy + hxx)
    spread = np.sqrt(np.maximum(half_trace ** 2 - (hyy * hxx - hxy ** 2), 0.0))
    steep, shallow = half_trace - spread, half_trace + spread
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(steep < 0, np.minimum(shallow, 0.0) / steep, 0.0)
    return np.nan_to_num(ratio, nan=0.0)


def _empty_columns() -> dict[str, np.ndarray]:
    return {
        "frame": np.empty(0, dtype=np.int64),
        "y": np.empty(0, dtype=np.float64),
        "x": np.empty(0, dtype=np.float64),
        "net_gradient": np.empty(0, dtype=np.float64),
        "intensity": np.empty(0, dtype=np.float64),
        "contrast": np.empty(0, dtype=np.float64),
    }


def find_spots(frame: np.ndarray, params: SpotParams, *, frame_index: int = 0) -> dict[str, np.ndarray]:
    """Spots of one 2-D frame as columns (see :data:`SPOT_COLUMNS`).

    ``y``/``x`` are the pixel of the net-gradient maximum; ``intensity`` is
    the smoothed image there and ``contrast`` that minus the mean of the
    box's one-pixel rim, a local background estimate.
    """
    from imswitch.improcess.reconstructors.smlm.detection import net_gradient_map

    image = np.asarray(frame, dtype=np.float32)
    if image.ndim != 2:
        raise ValueError(f"find_spots expects a 2-D frame, got shape {image.shape}")
    box = params.box
    net_gradient = net_gradient_map(image, roi=params.roi, sigma=params.sigma)
    threshold = effective_threshold(net_gradient, params)

    candidates = (maximum_filter(net_gradient, size=box) == net_gradient) & (
        net_gradient > threshold
    )
    smoothed = gaussian_filter(image, params.sigma) if params.sigma > 0 else image

    coords = np.argwhere(candidates)
    half = params.roi // 2
    height, width = image.shape
    if coords.size:
        rows, cols = coords[:, 0], coords[:, 1]
        inside = (rows - half >= 0) & (rows + half < height) & (cols - half >= 0) & (cols + half < width)
        coords = coords[inside]
    if coords.size and params.reject_edges:
        coords = coords[isotropy(smoothed, coords) >= MIN_ISOTROPY]
    if coords.size == 0:
        return _empty_columns()

    rows, cols = coords[:, 0], coords[:, 1]
    inner = box - 2
    box_sum = uniform_filter(smoothed, size=box, mode="nearest") * (box * box)
    inner_sum = uniform_filter(smoothed, size=inner, mode="nearest") * (inner * inner)
    rim = (box_sum - inner_sum) / float(box * box - inner * inner)
    intensity = smoothed[rows, cols].astype(np.float64)
    return {
        "frame": np.full(len(rows), int(frame_index), dtype=np.int64),
        "y": rows.astype(np.float64),
        "x": cols.astype(np.float64),
        "net_gradient": net_gradient[rows, cols].astype(np.float64),
        "intensity": intensity,
        "contrast": intensity - rim[rows, cols].astype(np.float64),
    }


def _concat(tables: Sequence[dict[str, np.ndarray]]) -> dict[str, np.ndarray]:
    if not tables:
        return _empty_columns()
    return {key: np.concatenate([table[key] for table in tables]) for key in SPOT_COLUMNS}


def find_spots_in_stack(
    stack: np.ndarray, params: SpotParams, *, cancelled=None
) -> tuple[dict[str, np.ndarray], np.ndarray]:
    """``(spots, counts)`` of a ``(Y, X)`` frame or a ``(N, Y, X)`` stack.

    ``spots`` is one table over all frames (``frame`` says which);
    ``counts[i]`` is how many spots frame ``i`` has. ``cancelled``, when
    given, is called between frames and may raise to stop the run.
    """
    array = np.asarray(stack)
    if array.ndim == 2:
        array = array[np.newaxis]
    if array.ndim != 3:
        raise ValueError(
            f"Spot counting needs a (Y, X) frame or an (N, Y, X) stack, got shape {array.shape}"
        )
    tables = []
    counts = np.zeros(array.shape[0], dtype=np.int64)
    for index in range(array.shape[0]):
        if cancelled is not None:
            cancelled()
        table = find_spots(array[index], params, frame_index=index)
        counts[index] = len(table["y"])
        tables.append(table)
    return _concat(tables), counts


# -- tiles -> stage targets ----------------------------------------------------------------


@dataclass(frozen=True)
class TileOrientation:
    """How stage axes map onto image axes, as the tiling manifest records it.

    The forward map (stage delta -> image delta) is the tiling controller's
    ``_gridToImage``: swap first, then the sign flips. :meth:`to_stage`
    inverts it.
    """

    flip_x: bool = False
    flip_y: bool = False
    swap_axes: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "TileOrientation":
        value = dict(value or {})
        return cls(
            flip_x=bool(value.get("flip_x", False)),
            flip_y=bool(value.get("flip_y", False)),
            swap_axes=bool(value.get("swap_axes", False)),
        )

    def to_stage(self, d_row_um: np.ndarray, d_col_um: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Stage ``(dx, dy)`` that moves the sample by an image ``(d_row, d_col)``."""
        d_x = -np.asarray(d_col_um, dtype=np.float64) if self.flip_x else np.asarray(d_col_um, dtype=np.float64)
        d_y = -np.asarray(d_row_um, dtype=np.float64) if self.flip_y else np.asarray(d_row_um, dtype=np.float64)
        if self.swap_axes:
            d_x, d_y = d_y, d_x
        return d_x, d_y


def spot_stage_positions(
    spots: Mapping[str, np.ndarray],
    tile_stage_um: np.ndarray,
    tile_shape: tuple[int, int],
    pixel_size_um: tuple[float, float],
    orientation: TileOrientation,
) -> tuple[np.ndarray, np.ndarray]:
    """Stage ``(x, y)`` of every spot: the position that puts it at the tile centre.

    ``tile_stage_um[i]`` is tile ``i``'s commanded ``(x, y)``; ``spots["frame"]``
    names each spot's tile. The tile's stage position is taken to image its
    centre pixel; any fixed offset between that and the true optical axis is
    the same for every tile and so cancels when the stage revisits.
    """
    frames = np.asarray(spots["frame"], dtype=np.int64)
    stage = np.asarray(tile_stage_um, dtype=np.float64).reshape(-1, 2)
    centre_row = (tile_shape[0] - 1) / 2.0
    centre_col = (tile_shape[1] - 1) / 2.0
    d_row = (np.asarray(spots["y"], dtype=np.float64) - centre_row) * float(pixel_size_um[0])
    d_col = (np.asarray(spots["x"], dtype=np.float64) - centre_col) * float(pixel_size_um[1])
    d_x, d_y = orientation.to_stage(d_row, d_col)
    if frames.size == 0:
        return np.empty(0), np.empty(0)
    return stage[frames, 0] + d_x, stage[frames, 1] + d_y


def tile_spot_targets(
    counts: np.ndarray,
    spots: Mapping[str, np.ndarray],
    spot_x_um: np.ndarray,
    spot_y_um: np.ndarray,
    tile_stage_um: np.ndarray,
    *,
    min_spots: int = 1,
    target: str = "spot centroid",
) -> dict[str, np.ndarray]:
    """One row per tile, most spots first, with the stage target to revisit.

    Columns: ``tile`` (index into the tile list), ``spots`` (count),
    ``rank`` (1 = most spots; ties keep acquisition order), ``selected``
    (``spots >= min_spots``), ``tile_x_um``/``tile_y_um`` (the tile's
    commanded position) and ``target_x_um``/``target_y_um`` -- the tile
    position, or the mean of its spots' positions when ``target`` is
    ``"spot centroid"`` and it has any.
    """
    if target not in TARGET_MODES:
        raise ValueError(f"target must be one of {TARGET_MODES}, got {target!r}")
    counts = np.asarray(counts, dtype=np.int64)
    stage = np.asarray(tile_stage_um, dtype=np.float64).reshape(-1, 2)
    if len(stage) != len(counts):
        raise ValueError(f"{len(counts)} tile counts for {len(stage)} tile positions")
    frames = np.asarray(spots["frame"], dtype=np.int64)
    target_x = stage[:, 0].copy()
    target_y = stage[:, 1].copy()
    if target == "spot centroid":
        for tile in np.flatnonzero(counts):
            mine = frames == tile
            target_x[tile] = float(np.mean(np.asarray(spot_x_um)[mine]))
            target_y[tile] = float(np.mean(np.asarray(spot_y_um)[mine]))
    # Stable: equal counts stay in acquisition order, so the route is predictable.
    order = np.argsort(-counts, kind="stable")
    rank = np.empty(len(counts), dtype=np.int64)
    rank[order] = np.arange(1, len(counts) + 1)
    return {
        "tile": order.astype(np.int64),
        "spots": counts[order],
        "rank": rank[order],
        "selected": counts[order] >= int(min_spots),
        "tile_x_um": stage[order, 0],
        "tile_y_um": stage[order, 1],
        "target_x_um": target_x[order],
        "target_y_um": target_y[order],
    }


@dataclass
class TilingSpots:
    """What :func:`count_spots_in_tiling` found in one saved tiling run.

    ``tiles`` is the ranked per-tile table of :func:`tile_spot_targets` plus
    ``grid_row``/``grid_col``/``name``; ``spots`` the spot table with, per
    spot, its tile (``frame`` = position in the tile list), its pixel in the
    tile, its stage ``stage_x_um``/``stage_y_um`` and its approximate
    ``mosaic_y``/``mosaic_x`` (the commanded layout, before refinement).
    """

    manifest: str
    detector: str | None
    pixel_size_um: tuple[float, float]
    orientation: TileOrientation
    tile_shape: tuple[int, int]
    tiles: dict[str, np.ndarray]
    spots: dict[str, np.ndarray]
    #: The XY stage the run moved and its (x, y) axis names, when the
    #: manifest records them (runs saved before this did: ``None``).
    positioner: str | None = None
    axes: tuple[str, str] | None = None

    @property
    def selected(self) -> dict[str, np.ndarray]:
        """The rows of :attr:`tiles` at or above the minimum count, best first."""
        keep = np.asarray(self.tiles["selected"], dtype=bool)
        return {key: np.asarray(value)[keep] for key, value in self.tiles.items()}

    def targets_um(self) -> list[tuple[float, float]]:
        """Stage ``(x, y)`` of every selected tile, most spots first."""
        chosen = self.selected
        return [
            (float(x), float(y))
            for x, y in zip(chosen["target_x_um"], chosen["target_y_um"])
        ]


def count_spots_in_tiling(
    path,
    params: SpotParams,
    *,
    detector: str | None = None,
    min_spots: int = 1,
    target: str = "spot centroid",
    progress=None,
    cancelled=None,
) -> TilingSpots:
    """Find spots in every tile of a saved tiling run and rank the tiles.

    ``path`` is the run's ``tiles.json`` or any file beside it. Tiles with
    leading axes (Z planes, line-step channels) are searched on their maximum
    projection. ``detector`` picks one detector of a multi-detector run; the
    default is the one the run aligned on.
    """
    from imswitch.imcommon.algorithms.tile_mosaic import load_dataset

    dataset = load_dataset(
        path,
        progress=progress,
        prefer_stage_positions=True,
        detector=detector,
        check_cancelled=cancelled,
    )
    manifest = dict(dataset.metadata or {})
    entries = list(manifest.get("tiles") or [])
    orientation = TileOrientation.from_mapping(manifest.get("orientation"))
    pixel_size = (float(dataset.pixel_size_um[0]), float(dataset.pixel_size_um[1]))

    stage = []
    for ordinal, tile in enumerate(dataset.tiles):
        tile_id = tile.tile_id if tile.tile_id is not None else ordinal
        value = entries[tile_id].get("stage_um") if tile_id < len(entries) else None
        if value is None or len(value) < 2:
            raise ValueError(
                f"Tile {tile.name} has no stage position in {dataset.source}; "
                f"its spots cannot be revisited."
            )
        stage.append((float(value[0]), float(value[1])))
    stage = np.asarray(stage, dtype=np.float64).reshape(-1, 2)

    planes = [np.asarray(tile.projection(), dtype=np.float32) for tile in dataset.tiles]
    tables, counts = [], np.zeros(len(planes), dtype=np.int64)
    for ordinal, plane in enumerate(planes):
        if cancelled is not None:
            cancelled()
        table = find_spots(plane, params, frame_index=ordinal)
        counts[ordinal] = len(table["y"])
        tables.append(table)
    spots = _concat(tables)

    # Tiles of one run share a shape; if one came up short, its own centre
    # still images the stage position, so map each spot with its own tile.
    spot_x = np.empty(len(spots["y"]))
    spot_y = np.empty(len(spots["y"]))
    for ordinal, plane in enumerate(planes):
        mine = spots["frame"] == ordinal
        if mine.any():
            sub = {key: value[mine] for key, value in spots.items()}
            sub["frame"] = np.zeros(int(mine.sum()), dtype=np.int64)
            spot_x[mine], spot_y[mine] = spot_stage_positions(
                sub, stage[ordinal : ordinal + 1], plane.shape, pixel_size, orientation
            )
    positions = np.asarray([tile.position for tile in dataset.tiles], dtype=np.float64).reshape(-1, 2)
    origin = positions.min(axis=0) if len(positions) else np.zeros(2)
    tile_of_spot = spots["frame"]
    spots.update({
        "tile_id": np.asarray(
            [dataset.tiles[i].tile_id if dataset.tiles[i].tile_id is not None else i for i in tile_of_spot],
            dtype=np.int64,
        ),
        "stage_x_um": spot_x,
        "stage_y_um": spot_y,
        "mosaic_y": spots["y"] + (positions[tile_of_spot, 0] - origin[0] if len(tile_of_spot) else 0.0),
        "mosaic_x": spots["x"] + (positions[tile_of_spot, 1] - origin[1] if len(tile_of_spot) else 0.0),
    })

    tiles = tile_spot_targets(counts, spots, spot_x, spot_y, stage, min_spots=min_spots, target=target)
    order = tiles["tile"]
    # Where each target lies on the mosaic, for drawing it there.
    mosaic_targets = np.zeros((len(planes), 2))
    for ordinal, plane in enumerate(planes):
        mine = spots["frame"] == ordinal
        if target == "spot centroid" and mine.any():
            mosaic_targets[ordinal] = (spots["mosaic_y"][mine].mean(), spots["mosaic_x"][mine].mean())
        else:
            mosaic_targets[ordinal] = (
                positions[ordinal, 0] - origin[0] + (plane.shape[0] - 1) / 2.0,
                positions[ordinal, 1] - origin[1] + (plane.shape[1] - 1) / 2.0,
            )
    tiles["target_mosaic_y"] = mosaic_targets[order, 0] if len(order) else np.empty(0)
    tiles["target_mosaic_x"] = mosaic_targets[order, 1] if len(order) else np.empty(0)
    tiles["grid_row"] = np.asarray([dataset.tiles[i].grid[0] for i in order], dtype=np.int64)
    tiles["grid_col"] = np.asarray([dataset.tiles[i].grid[1] for i in order], dtype=np.int64)
    tiles["name"] = np.asarray([dataset.tiles[i].name for i in order], dtype=object)
    return TilingSpots(
        manifest=str(dataset.source),
        detector=detector,
        pixel_size_um=pixel_size,
        orientation=orientation,
        tile_shape=tuple(planes[0].shape) if planes else (0, 0),
        tiles=tiles,
        spots=spots,
        positioner=manifest.get("xy_positioner") or None,
        axes=tuple(manifest["xy_axes"][:2]) if len(manifest.get("xy_axes") or ()) >= 2 else None,
    )


__all__ = [
    "SPOT_COLUMNS",
    "TARGET_MODES",
    "THRESHOLD_MODES",
    "SpotParams",
    "TileOrientation",
    "TilingSpots",
    "count_spots_in_tiling",
    "effective_threshold",
    "find_spots",
    "find_spots_in_stack",
    "robust_noise",
    "spot_stage_positions",
    "tile_spot_targets",
]


# Copyright (C) 2020-2026 ImSwitch developers
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
