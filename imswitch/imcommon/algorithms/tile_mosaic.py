"""Offline assembly of a saved tile dataset into one mosaic.

This is the reading half of tiling. The acquisition side writes a folder of
OME images plus a ``tiles.json`` manifest; this module turns that back into a
single array, optionally refining the layout first.

It lives in imcommon because both halves need it and improcess must not import
imcontrol. It is deliberately *not* the same code as the live incremental
stitcher used during acquisition: that one places one tile at a time against a
growing canvas under a latency budget, while this one has every tile in hand at
once and can afford a global pass. Sharing an implementation between the two
would compromise both.

Copyright (C) 2020-2026 ImSwitch developers
This file is part of ImSwitch.

ImSwitch is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

ImSwitch is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

MANIFEST_NAME = 'tiles.json'

#: Minimum overlap extent, in pixels, worth correlating.
MIN_OVERLAP_PX = 16


@dataclass
class MosaicTile:
    """One tile of a saved dataset: its pixels and where they belong."""

    name: str
    data: np.ndarray                 # (Y, X) or (Z, Y, X)
    #: Top-left position in mosaic pixels, as ``(row, col)``.
    position: Tuple[float, float]
    grid: Tuple[int, int] = (0, 0)

    @property
    def plane_shape(self) -> Tuple[int, int]:
        return tuple(self.data.shape[-2:])

    def projection(self) -> np.ndarray:
        """The 2-D view used for alignment: a max projection of a stack."""
        if self.data.ndim <= 2:
            return self.data
        return self.data.max(axis=tuple(range(self.data.ndim - 2)))


@dataclass
class MosaicDataset:
    """A loaded tile dataset, ready to assemble."""

    tiles: List[MosaicTile] = field(default_factory=list)
    pixel_size_um: Tuple[float, float] = (1.0, 1.0)   # (y, x)
    z_step_um: float = 0.0
    tile_step_um: float = 0.0
    source: Optional[Path] = None
    metadata: Dict = field(default_factory=dict)

    @property
    def depth(self) -> int:
        for tile in self.tiles:
            if tile.data.ndim > 2:
                return int(tile.data.shape[0])
        return 1

    @property
    def is_volumetric(self) -> bool:
        return self.depth > 1


# ----------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------


def find_manifest(path: Path | str) -> Optional[Path]:
    """Locate ``tiles.json`` from any path inside a tiling dataset.

    Accepts the manifest itself, the dataset folder, or any tile file in it,
    so the user can open whatever they happen to have selected.
    """
    path = Path(path)
    if path.is_file() and path.name == MANIFEST_NAME:
        return path

    folder = path if path.is_dir() else path.parent
    candidate = folder / MANIFEST_NAME
    return candidate if candidate.is_file() else None


def _read_image(path: Path) -> Optional[np.ndarray]:
    """Read one saved tile, whatever container it was written in."""
    suffix = ''.join(path.suffixes).lower()

    try:
        if suffix.endswith(('.tif', '.tiff')):
            import tifffile
            return np.asarray(tifffile.imread(str(path)))
        if suffix.endswith('.h5') or suffix.endswith('.hdf5'):
            import h5py
            with h5py.File(str(path), 'r') as handle:
                for key in handle:
                    item = handle[key]
                    if hasattr(item, 'shape') and len(item.shape) >= 2:
                        return np.asarray(item)
                    if hasattr(item, 'keys'):
                        for subkey in item:
                            sub = item[subkey]
                            if hasattr(sub, 'shape') and len(sub.shape) >= 2:
                                return np.asarray(sub)
            return None
        if suffix.endswith('.zarr') or path.is_dir():
            import zarr
            group = zarr.open(str(path), mode='r')
            if hasattr(group, 'shape'):
                return np.asarray(group)
            for key in getattr(group, 'array_keys', lambda: [])():
                return np.asarray(group[key])
            return None
    except Exception as exc:
        logger.error('Could not read tile %s: %s', path, exc)
        return None

    logger.error('Unsupported tile format: %s', path)
    return None


def _squeeze_leading(array: np.ndarray) -> np.ndarray:
    """Drop leading singleton axes a container added around the real data."""
    while array.ndim > 2 and array.shape[0] == 1:
        array = array[0]
    return array


def load_dataset(path: Path | str) -> MosaicDataset:
    """Load a tiling dataset from its manifest.

    Raises FileNotFoundError when ``path`` is not part of a tiling dataset, and
    ValueError when the manifest lists no readable tile.
    """
    manifest_path = find_manifest(path)
    if manifest_path is None:
        raise FileNotFoundError(
            f'No {MANIFEST_NAME} found next to {path}. Open a file from a '
            'tiling dataset folder, or the manifest itself.'
        )

    payload = json.loads(manifest_path.read_text(encoding='utf-8'))
    folder = manifest_path.parent

    pixel = payload.get('pixel_size_um') or {}
    dataset = MosaicDataset(
        pixel_size_um=(
            float(pixel.get('y', 1.0) or 1.0),
            float(pixel.get('x', 1.0) or 1.0),
        ),
        z_step_um=float(payload.get('z_step_um', 0.0) or 0.0),
        tile_step_um=float(payload.get('tile_step_um', 0.0) or 0.0),
        source=manifest_path,
        metadata=payload,
    )

    missing = []
    for entry in payload.get('tiles', []):
        filename = entry.get('filename')
        if not filename:
            continue
        tile_path = folder / filename
        if not tile_path.exists():
            missing.append(filename)
            continue
        data = _read_image(tile_path)
        if data is None:
            missing.append(filename)
            continue
        # TileConfiguration stores (x, y); the mosaic works in (row, col).
        x, y = entry.get('pixel_xy', (0.0, 0.0))
        dataset.tiles.append(MosaicTile(
            name=filename,
            data=_squeeze_leading(data),
            position=(float(y), float(x)),
            grid=tuple(entry.get('grid', (0, 0))),
        ))

    if missing:
        logger.warning('%d tile(s) listed in the manifest could not be read: %s',
                       len(missing), ', '.join(missing[:5]))
    if not dataset.tiles:
        raise ValueError(f'No readable tiles found for {manifest_path}')

    return dataset


# ----------------------------------------------------------------------
# Refinement
# ----------------------------------------------------------------------


def _normalize(image: np.ndarray) -> np.ndarray:
    arr = np.asarray(image, dtype=np.float32)
    arr = arr - arr.mean()
    std = float(arr.std())
    return arr if std <= np.finfo(np.float32).eps else arr / std


def _pair_shift(reference, moving, offset, max_shift_px):
    """Measured correction for ``moving`` relative to ``reference``."""
    try:
        from skimage.registration import phase_cross_correlation
    except ImportError:
        return None

    off_y, off_x = int(round(offset[0])), int(round(offset[1]))
    ref_y0, ref_x0 = max(0, off_y), max(0, off_x)
    ref_y1 = min(reference.shape[0], off_y + moving.shape[0])
    ref_x1 = min(reference.shape[1], off_x + moving.shape[1])
    if (ref_y1 - ref_y0) < MIN_OVERLAP_PX or (ref_x1 - ref_x0) < MIN_OVERLAP_PX:
        return None

    ref_patch = _normalize(reference[ref_y0:ref_y1, ref_x0:ref_x1])
    mov_patch = _normalize(moving[
        ref_y0 - off_y:ref_y0 - off_y + (ref_y1 - ref_y0),
        ref_x0 - off_x:ref_x0 - off_x + (ref_x1 - ref_x0),
    ])
    if ref_patch.std() <= 1e-6 or mov_patch.std() <= 1e-6:
        return None

    try:
        # Plain cross-correlation, not phase normalization: whitening the
        # spectrum is dominated by noise on the smooth texture typical of
        # biological samples.
        shift, _error, _phase = phase_cross_correlation(
            ref_patch, mov_patch, upsample_factor=10, normalization=None,
        )
    except Exception:
        return None

    dy, dx = float(shift[0]), float(shift[1])
    if np.hypot(dy, dx) > max_shift_px:
        return None
    return dy, dx


def refine_positions(dataset: MosaicDataset,
                     max_shift_px: Optional[float] = None) -> int:
    """Refine tile positions by correlating each against its placed neighbours.

    Tiles are visited in their stored order, each aligned against the union of
    those already fixed. Corrections accumulate, so a systematic error does not
    reappear on every tile. 3D tiles are aligned on their projections and the
    whole stack moves together — the tiles share a Z range, so there is nothing
    to gain from aligning planes independently.

    Returns the number of tiles actually moved.
    """
    if len(dataset.tiles) < 2:
        return 0

    if max_shift_px is None:
        heights = [tile.plane_shape[0] for tile in dataset.tiles]
        max_shift_px = max(MIN_OVERLAP_PX, 0.25 * float(min(heights)))

    placed = [dataset.tiles[0]]
    moved = 0
    for tile in dataset.tiles[1:]:
        best = None
        for reference in reversed(placed):
            offset = (
                tile.position[0] - reference.position[0],
                tile.position[1] - reference.position[1],
            )
            shift = _pair_shift(
                reference.projection(), tile.projection(), offset, max_shift_px
            )
            if shift is not None:
                best = shift
                break
        if best is not None and (best[0] or best[1]):
            tile.position = (
                tile.position[0] + best[0], tile.position[1] + best[1]
            )
            moved += 1
        placed.append(tile)

    return moved


# ----------------------------------------------------------------------
# Assembly
# ----------------------------------------------------------------------


def assemble(dataset: MosaicDataset, blend: bool = True) -> np.ndarray:
    """Paste every tile into one array.

    Returns ``(Y, X)`` for 2D tiles and ``(Z, Y, X)`` when the dataset is
    volumetric. Overlaps are averaged when ``blend`` is set, otherwise later
    tiles overwrite earlier ones.
    """
    if not dataset.tiles:
        raise ValueError('Cannot assemble an empty dataset')

    rows = [tile.position[0] for tile in dataset.tiles]
    cols = [tile.position[1] for tile in dataset.tiles]
    row0 = int(np.floor(min(rows)))
    col0 = int(np.floor(min(cols)))
    row1 = int(np.ceil(max(
        pos + tile.plane_shape[0] for pos, tile in zip(rows, dataset.tiles)
    )))
    col1 = int(np.ceil(max(
        pos + tile.plane_shape[1] for pos, tile in zip(cols, dataset.tiles)
    )))

    height, width = max(1, row1 - row0), max(1, col1 - col0)
    depth = dataset.depth
    shape = (depth, height, width) if depth > 1 else (height, width)

    canvas_sum = np.zeros(shape, dtype=np.float32)
    canvas_weight = np.zeros((height, width), dtype=np.float32)

    for tile in dataset.tiles:
        top = int(round(tile.position[0])) - row0
        left = int(round(tile.position[1])) - col0
        tile_h, tile_w = tile.plane_shape
        tile_h = min(tile_h, height - top)
        tile_w = min(tile_w, width - left)
        if tile_h <= 0 or tile_w <= 0:
            continue

        data = np.asarray(tile.data, dtype=np.float32)
        weight_view = canvas_weight[top:top + tile_h, left:left + tile_w]

        if depth > 1:
            if data.ndim == 2:
                # A 2D tile in a volumetric set contributes to every plane.
                data = np.broadcast_to(data, (depth,) + data.shape)
            planes = min(depth, data.shape[0])
            target = canvas_sum[:planes, top:top + tile_h, left:left + tile_w]
            source = data[:planes, :tile_h, :tile_w]
        else:
            target = canvas_sum[top:top + tile_h, left:left + tile_w]
            source = data[:tile_h, :tile_w]

        if blend:
            target += source
            weight_view += 1.0
        else:
            target[:] = source
            weight_view[:] = 1.0

    out = np.zeros_like(canvas_sum)
    safe = np.where(canvas_weight > 0, canvas_weight, 1.0)
    if depth > 1:
        np.divide(canvas_sum, safe[None, :, :], out=out)
    else:
        np.divide(canvas_sum, safe, out=out)
    return out


def assemble_dataset(path: Path | str, *, blend: bool = True,
                     refine: bool = False,
                     max_shift_px: Optional[float] = None):
    """Load, optionally refine, and assemble in one call.

    Returns ``(mosaic, dataset, tiles_moved)``.
    """
    dataset = load_dataset(path)
    moved = refine_positions(dataset, max_shift_px) if refine else 0
    return assemble(dataset, blend=blend), dataset, moved
