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

That global pass is what the offline path is for. A spiral scan gives most
tiles two to four overlapping neighbours — and the tile that closes a ring sits
right beside the one that opened it — so the layout is heavily over-determined.
Refinement measures *every* overlapping pair, rejects the links that disagree
with the consensus, and solves for all positions at once. Aligning each tile to
one neighbour instead, as the live stitcher must, accumulates error along the
acquisition order and lets a single false correlation displace everything
measured after it.

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

#: Normalized cross-correlation, measured where a link claims the tiles line
#: up, below which the measurement is not worth putting into the solve.
MIN_LINK_CONFIDENCE = 0.15

#: A link is dropped as an outlier when its residual exceeds the median
#: residual by this many robust standard deviations.
OUTLIER_SIGMAS = 4.0

#: Residual scale below which the spread of the residuals is treated as noise
#: rather than signal, so a well-behaved mosaic does not reject its own links.
RESIDUAL_FLOOR_PX = 0.5

#: However consistent the rest of the graph is, a link is never dropped for a
#: residual smaller than this — that much disagreement does not matter.
MIN_OUTLIER_PX = 1.0

#: Solve/reject rounds. Convergence is normally reached in two.
MAX_SOLVE_ROUNDS = 4

#: Position change below which a tile is not considered to have moved. A
#: global solve gives almost every tile some sub-pixel nudge as it shares out
#: the disagreement, and ``assemble`` pastes on whole pixels, so anything under
#: half a pixel changes neither where the tile lands nor what the operator sees.
MOVED_EPS_PX = 0.5


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


@dataclass
class TileLink:
    """One measured relationship between two tiles that overlap."""

    i: int
    j: int
    #: Measured position of tile ``j``'s origin relative to tile ``i``'s, as
    #: ``(dy, dx)`` in pixels. This is a total displacement, not a correction.
    offset: Tuple[float, float]
    #: Normalized cross-correlation of the overlap at ``offset``, in [0, 1].
    confidence: float
    #: How far ``offset`` sits from what the recorded stage positions implied.
    correction: Tuple[float, float]
    #: Disagreement between ``offset`` and the solved layout, in pixels.
    residual: float = 0.0
    accepted: bool = True


@dataclass
class RefinementReport:
    """What the global solve measured, used and threw away."""

    links: List[TileLink] = field(default_factory=list)
    moved: int = 0
    tiles: int = 0
    #: Tiles per connected component of the accepted link graph, largest first.
    components: List[int] = field(default_factory=list)

    @property
    def accepted(self) -> List[TileLink]:
        return [link for link in self.links if link.accepted]

    def residual_rms(self) -> Optional[float]:
        accepted = self.accepted
        if not accepted:
            return None
        return float(np.sqrt(np.mean([link.residual ** 2 for link in accepted])))

    def summary(self) -> str:
        if not self.links:
            return (
                f'Tiling refinement: no usable overlap between any of the '
                f'{self.tiles} tiles; the recorded stage positions were kept.'
            )

        accepted = self.accepted
        rejected = len(self.links) - len(accepted)
        parts = [
            f'Tiling refinement: {self.moved}/{self.tiles} tiles moved from '
            f'{len(accepted)} link(s)'
        ]
        rms = self.residual_rms()
        if rms is not None:
            parts.append(f'residual RMS {rms:.2f} px')
        if rejected:
            parts.append(f'{rejected} link(s) rejected as inconsistent')
        if len(self.components) > 1:
            parts.append(
                f'{len(self.components)} disconnected groups '
                f'({", ".join(str(size) for size in self.components[:4])}'
                f'{", ..." if len(self.components) > 4 else ""} tiles) — each '
                'is internally aligned but placed by its stage position'
            )
        return '; '.join(parts) + '.'


def _overlap_box(ref_shape, mov_shape, offset):
    """Slices of the region two planes share, given the ``mov − ref`` offset.

    Returns ``(ref_slices, mov_slices)``, or None when the shared region is too
    small to say anything useful about.
    """
    off_y, off_x = int(round(offset[0])), int(round(offset[1]))

    ref_y0, ref_x0 = max(0, off_y), max(0, off_x)
    ref_y1 = min(ref_shape[0], off_y + mov_shape[0])
    ref_x1 = min(ref_shape[1], off_x + mov_shape[1])
    if (ref_y1 - ref_y0) < MIN_OVERLAP_PX or (ref_x1 - ref_x0) < MIN_OVERLAP_PX:
        return None

    return (
        (slice(ref_y0, ref_y1), slice(ref_x0, ref_x1)),
        (slice(ref_y0 - off_y, ref_y1 - off_y),
         slice(ref_x0 - off_x, ref_x1 - off_x)),
    )


def _ncc(first: np.ndarray, second: np.ndarray) -> float:
    """Normalized cross-correlation of two equally shaped patches."""
    first, second = _normalize(first), _normalize(second)
    if first.std() <= 1e-6 or second.std() <= 1e-6:
        return 0.0
    return float(np.clip(np.mean(first * second), -1.0, 1.0))


def _measure_link(reference, moving, offset, max_shift_px):
    """Measure where ``moving`` actually sits relative to ``reference``.

    Returns ``(measured_offset, confidence)``, or None when the pair cannot be
    measured, the correction is implausibly large, or the match is too weak to
    be worth a vote in the solve.
    """
    try:
        from skimage.registration import phase_cross_correlation
    except ImportError:
        return None

    box = _overlap_box(reference.shape, moving.shape, offset)
    if box is None:
        return None

    ref_slices, mov_slices = box
    ref_patch = _normalize(reference[ref_slices])
    mov_patch = _normalize(moving[mov_slices])
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
    if not (np.isfinite(dy) and np.isfinite(dx)):
        return None
    if np.hypot(dy, dx) > max_shift_px:
        return None

    # Score the match where it claims the tiles line up, not where they were
    # expected to: skimage's own error is a residual of the correlation, which
    # says little about whether the two tiles really show the same sample.
    measured = (offset[0] + dy, offset[1] + dx)
    check = _overlap_box(reference.shape, moving.shape, measured)
    if check is None:
        return None
    confidence = _ncc(reference[check[0]], moving[check[1]])
    if confidence < MIN_LINK_CONFIDENCE:
        return None

    return measured, confidence


def _build_links(dataset: MosaicDataset, max_shift_px: float) -> List[TileLink]:
    """Measure every pair of tiles the recorded layout says should overlap.

    This is the point of the offline path: a spiral gives most tiles two to
    four overlapping neighbours, and the tile that closes a ring sits next to
    the one that opened it. Every one of those pairs is a constraint.
    """
    projections = [tile.projection() for tile in dataset.tiles]
    positions = [tile.position for tile in dataset.tiles]
    shapes = [tile.plane_shape for tile in dataset.tiles]

    links: List[TileLink] = []
    for i in range(len(dataset.tiles)):
        for j in range(i + 1, len(dataset.tiles)):
            offset = (
                positions[j][0] - positions[i][0],
                positions[j][1] - positions[i][1],
            )
            if _overlap_box(shapes[i], shapes[j], offset) is None:
                continue
            measured = _measure_link(
                projections[i], projections[j], offset, max_shift_px
            )
            if measured is None:
                continue
            offset_measured, confidence = measured
            links.append(TileLink(
                i=i, j=j,
                offset=offset_measured,
                confidence=confidence,
                correction=(offset_measured[0] - offset[0],
                            offset_measured[1] - offset[1]),
            ))
    return links


def _components(count: int, links: Sequence[TileLink]) -> List[List[int]]:
    """Connected groups of tiles, each sorted, largest group first."""
    parent = list(range(count))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for link in links:
        root_i, root_j = find(link.i), find(link.j)
        if root_i != root_j:
            parent[root_j] = root_i

    groups: Dict[int, List[int]] = {}
    for node in range(count):
        groups.setdefault(find(node), []).append(node)
    return sorted(groups.values(), key=len, reverse=True)


def _solve(nominal: Sequence[Tuple[float, float]],
           links: Sequence[TileLink]) -> List[Tuple[float, float]]:
    """Least-squares tile positions consistent with all the measured links.

    Each link asks for ``p[j] − p[i]`` to equal what was measured, weighted by
    how much that measurement is trusted. No single link decides a position, so
    the error left over is spread across the mosaic instead of accumulating
    along the acquisition order.

    Only the differences between positions are measured, so each connected
    group is anchored at its earliest-acquired tile's recorded position to fix
    the otherwise free origin. A tile nothing could be measured against forms a
    group of its own, and so stays exactly where the stage said it was.
    """
    from scipy.sparse import coo_matrix
    from scipy.sparse.linalg import lsqr

    positions = [tuple(pos) for pos in nominal]
    components = _components(len(nominal), links)
    component_of = {node: index
                    for index, nodes in enumerate(components)
                    for node in nodes}
    grouped: Dict[int, List[TileLink]] = {}
    for link in links:
        grouped.setdefault(component_of[link.i], []).append(link)

    for index, nodes in enumerate(components):
        member_links = grouped.get(index)
        if not member_links:
            continue

        anchor, *free = nodes
        column = {node: k for k, node in enumerate(free)}

        rows, cols, vals = [], [], []
        rhs = np.zeros((len(member_links), 2), dtype=np.float64)
        for row, link in enumerate(member_links):
            weight = max(link.confidence, 1e-3)
            target = np.array(link.offset, dtype=np.float64) * weight
            if link.i in column:
                rows.append(row)
                cols.append(column[link.i])
                vals.append(-weight)
            else:
                target += weight * np.asarray(nominal[anchor], dtype=np.float64)
            if link.j in column:
                rows.append(row)
                cols.append(column[link.j])
                vals.append(weight)
            else:
                target -= weight * np.asarray(nominal[anchor], dtype=np.float64)
            rhs[row] = target

        matrix = coo_matrix(
            (vals, (rows, cols)), shape=(len(member_links), len(column))
        ).tocsr()
        solved = [lsqr(matrix, rhs[:, axis])[0] for axis in (0, 1)]

        for node, index in column.items():
            positions[node] = (float(solved[0][index]), float(solved[1][index]))

    return positions


def _update_residuals(links: Sequence[TileLink],
                      positions: Sequence[Tuple[float, float]]) -> None:
    """Record how far each link is from the layout that was solved for."""
    for link in links:
        link.residual = float(np.hypot(
            positions[link.j][0] - positions[link.i][0] - link.offset[0],
            positions[link.j][1] - positions[link.i][1] - link.offset[1],
        ))


def _drop_outliers(links: List[TileLink]) -> List[TileLink]:
    """Reject links that disagree with the layout the others agree on.

    A false correlation is not detectable on its own — it looks like any other
    match. It is detectable against its neighbours, which is what makes the
    graph worth building: one bad link against a consistent majority stands
    out, where in a chain it would simply displace everything after it.
    """
    if len(links) < 3:
        return links

    residuals = np.array([link.residual for link in links], dtype=np.float64)
    median = float(np.median(residuals))
    spread = 1.4826 * float(np.median(np.abs(residuals - median)))
    limit = max(
        median + OUTLIER_SIGMAS * max(spread, RESIDUAL_FLOOR_PX),
        MIN_OUTLIER_PX,
    )

    kept = []
    for link in links:
        if link.residual > limit:
            link.accepted = False
        else:
            kept.append(link)
    return kept


def refine_layout(dataset: MosaicDataset,
                  max_shift_px: Optional[float] = None) -> RefinementReport:
    """Solve for every tile position at once, and report what it took.

    Every overlapping pair is correlated, not just consecutive ones, and the
    resulting over-determined system is solved in one least-squares pass with
    outlying links rejected between rounds. 3D tiles are aligned on their
    projections and the whole stack moves together — the tiles share a Z range,
    so there is nothing to gain from aligning planes independently.

    ``dataset`` is modified in place.
    """
    report = RefinementReport(tiles=len(dataset.tiles))
    if len(dataset.tiles) < 2:
        return report

    if max_shift_px is None:
        heights = [tile.plane_shape[0] for tile in dataset.tiles]
        max_shift_px = max(MIN_OVERLAP_PX, 0.25 * float(min(heights)))

    nominal = [tile.position for tile in dataset.tiles]
    report.links = _build_links(dataset, max_shift_px)
    if not report.links:
        return report

    active = list(report.links)
    positions = _solve(nominal, active)
    for _round in range(MAX_SOLVE_ROUNDS):
        _update_residuals(report.links, positions)
        kept = _drop_outliers(active)
        if len(kept) == len(active) or not kept:
            break
        active = kept
        positions = _solve(nominal, active)
    _update_residuals(report.links, positions)

    for tile, position, before in zip(dataset.tiles, positions, nominal):
        tile.position = position
        if (abs(position[0] - before[0]) > MOVED_EPS_PX
                or abs(position[1] - before[1]) > MOVED_EPS_PX):
            report.moved += 1

    report.components = [
        len(nodes) for nodes in _components(len(dataset.tiles), active)
    ]
    return report


def refine_positions(dataset: MosaicDataset,
                     max_shift_px: Optional[float] = None) -> int:
    """Refine tile positions in place; returns the number of tiles moved.

    A thin wrapper over :func:`refine_layout` for callers that only want the
    count.
    """
    return refine_layout(dataset, max_shift_px).moved


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
