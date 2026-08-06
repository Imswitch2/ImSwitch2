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
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

MANIFEST_NAME = 'tiles.json'

#: Minimum overlap extent, in pixels, worth correlating.
MIN_OVERLAP_PX = 16

#: Normalized cross-correlation, measured where a link claims the tiles line
#: up, below which the measurement is not worth putting into the solve. Scored
#: after :func:`_bandpass`, so this is agreement on sample structure and not on
#: the illumination profile the tiles share.
#:
#: Kept deliberately permissive. Measured end to end, admitting a weak link and
#: letting the global solve outvote it beats excluding it: a rejected link can
#: disconnect a tile, and a disconnected tile falls back to its raw stage
#: position, which is the larger error. Raising this to 0.35 or 0.5 made
#: placement worse in every case tried.
MIN_LINK_CONFIDENCE = 0.15

#: Structure varying over more than this fraction of the shared region's short
#: side is illumination, not sample, and is removed before correlating.
BACKGROUND_SCALE_FRACTION = 0.25

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

#: Axis names assumed for a tile's leading axes when the manifest does not say,
#: ordered outermost-first so the last entry sits nearest Y/X. One leading axis
#: has always meant Z here; a second is a line-step scan's channels.
_DEFAULT_LEADING_AXES = 'CZ'

#: Position change below which a tile is not considered to have moved. A
#: global solve gives almost every tile some sub-pixel nudge as it shares out
#: the disagreement, and ``assemble`` pastes on whole pixels, so anything under
#: half a pixel changes neither where the tile lands nor what the operator sees.
MOVED_EPS_PX = 0.5


@dataclass
class MosaicTile:
    """One tile of a saved dataset: its pixels and where they belong."""

    name: str
    data: np.ndarray                 # (..., Y, X)
    #: Top-left position in mosaic pixels, as ``(row, col)``.
    position: Tuple[float, float]
    grid: Tuple[int, int] = (0, 0)
    #: Axis names for ``data``, ending in ``YX``. A tile may carry any number
    #: of leading axes — ``Z`` for a stack, ``C`` for a line-step scan's
    #: channels, both for a scan that does each. Empty when the manifest did
    #: not say, in which case a leading axis is assumed to be Z, as before.
    axes: str = ''

    @property
    def plane_shape(self) -> Tuple[int, int]:
        return tuple(self.data.shape[-2:])

    @property
    def leading_shape(self) -> Tuple[int, ...]:
        """Everything before Y/X: ``()`` for a plain image."""
        return tuple(self.data.shape[:-2])

    def projection(self) -> np.ndarray:
        """The 2-D view used for alignment: a max projection over every
        leading axis, whatever those axes happen to mean."""
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
    def leading_shape(self) -> Tuple[int, ...]:
        """The leading axes the mosaic must carry, over every tile.

        A dataset need not be uniform — a tile whose stack came up short still
        belongs in the mosaic — so this takes the largest extent seen on each
        axis. Shapes of differing rank are right-aligned, so a ``(Z, Y, X)``
        tile sitting among ``(C, Z, Y, X)`` ones contributes to Z, not to C.
        """
        shapes = [tile.leading_shape for tile in self.tiles if tile.leading_shape]
        if not shapes:
            return ()
        rank = max(len(shape) for shape in shapes)
        aligned = [(1,) * (rank - len(shape)) + shape for shape in shapes]
        return tuple(max(extents) for extents in zip(*aligned))

    @property
    def axes(self) -> str:
        """Axis names for the assembled mosaic, ending in ``YX``."""
        for tile in self.tiles:
            if tile.axes and len(tile.axes) == tile.data.ndim:
                return tile.axes
        # No descriptor to go on. One leading axis has always meant Z here, and
        # a second is a line-step scan's channels; anything beyond that cannot
        # be guessed and is named so it reads as unknown rather than wrong.
        rank = len(self.leading_shape)
        if rank == 0:
            return 'YX'
        if rank <= len(_DEFAULT_LEADING_AXES):
            return _DEFAULT_LEADING_AXES[-rank:] + 'YX'
        return '?' * (rank - len(_DEFAULT_LEADING_AXES)) + _DEFAULT_LEADING_AXES + 'YX'

    @property
    def depth(self) -> int:
        """Extent of the first leading axis; 1 when the tiles are plain."""
        leading = self.leading_shape
        return int(leading[0]) if leading else 1

    @property
    def is_volumetric(self) -> bool:
        return bool(self.leading_shape) and self.depth > 1


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
    """Drop leading singleton axes a container added around the real data.

    The undescribed fallback. It cannot tell a container's wrapper axis from a
    real one that happens to have a single channel or plane, so it destroys the
    latter — which is exactly why a descriptor is worth having.
    """
    while array.ndim > 2 and array.shape[0] == 1:
        array = array[0]
    return array


def _file_descriptor(entry: Dict, detector: Optional[str]) -> Dict:
    """The entry describing one tile file, for ``detector`` or the default.

    Three manifest layouts are read, oldest last:

    * ``alignment`` + ``payloads`` — the current one. The alignment image is
      singular; payloads are per detector and carry a locator rather than a
      bare filename.
    * ``files`` — an interim layout keyed by detector.
    * a flat ``filename`` on the entry itself — the original.

    Asking for a detector a tile does not have returns nothing, so that tile is
    skipped rather than silently substituted with a different detector's image.
    """
    if not detector:
        alignment = entry.get('alignment')
        if isinstance(alignment, dict) and alignment.get('filename'):
            return alignment

    payloads = entry.get('payloads')
    if isinstance(payloads, dict) and payloads:
        if detector:
            found = payloads.get(detector)
            if not isinstance(found, dict):
                return {}
            # A locator names its file under `path`; the loader wants the
            # same key every layout uses.
            resolved = dict(found)
            resolved.setdefault('filename', resolved.get('path'))
            return resolved
        # No detector asked for and no alignment entry: fall through.

    files = entry.get('files')
    if isinstance(files, dict) and files:
        if detector:
            found = files.get(detector)
            return found if isinstance(found, dict) else {}
        return entry

    return {} if detector else entry


def detectors_in(payload: Dict) -> List[str]:
    """Every detector a manifest saved, in the order it first saw them."""
    names: List[str] = []
    for entry in payload.get('tiles', []) or []:
        for key in ('payloads', 'files'):
            group = entry.get(key)
            if not isinstance(group, dict):
                continue
            for name in group:
                if name not in names:
                    names.append(str(name))
    return names


def _apply_descriptor(array: np.ndarray, descriptor: Dict, name: str):
    """Reduce a stored array to the logical one its descriptor promises.

    ``axes`` is what a reader should end up with. What is actually on disk is
    taken from the array in hand, not from the manifest: a container adds
    axes of its own — the HDF5 snapshot writer gives every 2-D image a leading
    frame axis — so a manifest written before saving cannot know the stored
    rank, and one that guessed would contradict the file it describes.
    ``stored_axes`` is therefore treated as a hint and cross-checked, never
    trusted.

    Only leading axes may be dropped, only at length 1, and the result is
    validated against the declared ``shape``. Anything else raises: a tile
    whose shape contradicts its own manifest is not a tile to reinterpret.

    Returns ``(array, axes)``.
    """
    axes = str(descriptor.get('axes') or '')
    if not axes:
        return _squeeze_leading(array), ''

    extra = array.ndim - len(axes)
    if extra < 0:
        raise ValueError(
            f'{name}: manifest declares {axes!r} ({len(axes)} axes) but the '
            f'file has only {array.ndim}'
        )
    for position in range(extra):
        if array.shape[position] != 1:
            raise ValueError(
                f'{name}: the file has {array.ndim} axes where {axes!r} needs '
                f'{len(axes)}, and the extra axis at position {position} has '
                f'length {array.shape[position]} — dropping it would discard '
                'data'
            )
    reduced = array.reshape(array.shape[extra:])

    declared = tuple(descriptor.get('shape') or ())
    if declared and tuple(reduced.shape) != declared:
        raise ValueError(
            f'{name}: manifest declares shape {declared} for {axes!r} but the '
            f'file holds {tuple(reduced.shape)}'
        )

    # When the writer reported what it stored, a disagreement is a real
    # contradiction rather than a stale guess, and is refused like any other.
    stored = str(descriptor.get('stored_axes') or '')
    if stored and len(stored) != array.ndim:
        raise ValueError(
            f'{name}: the writer reported stored axes {stored!r} '
            f'({len(stored)}) but the file has {array.ndim}'
        )
    return reduced, axes


def _positions_from_stage(payload: Dict, entries: List[Dict],
                          pixel_size_um: Tuple[float, float]):
    """Tile positions derived from the commanded stage coordinates.

    Preferred over the manifest's ``pixel_xy``, because that is written *after*
    the live registration pass and therefore has any correction that pass made
    baked into it. A bad live correction is not something refinement can undo:
    it crops its correlation windows from these very positions, so a tile that
    starts far enough out is measured against the wrong part of its neighbour,
    or is no longer seen to overlap it at all. Observed on a real run: live
    corrections up to 111 px where the tiles overlapped by only 46, leaving the
    mosaic in five pieces that no measurement could tie together.

    The stage coordinates carry no such history — they are what the stage was
    told — so refinement starts from the commanded layout and does its own
    alignment. Returns None when the manifest cannot support this, leaving the
    caller to fall back on ``pixel_xy``.
    """
    stage = []
    for entry in entries:
        value = entry.get('stage_um')
        if value is None or len(value) < 2:
            return None
        stage.append((float(value[0]), float(value[1])))

    # Older writers, and rigs that never reported a position, put the same
    # coordinate on every tile; that says nothing about the layout.
    if len({(round(x, 6), round(y, 6)) for x, y in stage}) < 2:
        return None

    pixel_y, pixel_x = pixel_size_um
    if not pixel_y or not pixel_x:
        return None

    orientation = payload.get('orientation') or {}
    flip_x = bool(orientation.get('flip_x', False))
    flip_y = bool(orientation.get('flip_y', False))
    swap_axes = bool(orientation.get('swap_axes', False))

    origin_x, origin_y = stage[0]
    positions = []
    for stage_x, stage_y in stage:
        # The same transform the acquisition side applies in
        # TilingController._gridToImage: swap first, then the sign flips.
        delta_x, delta_y = stage_x - origin_x, stage_y - origin_y
        if swap_axes:
            delta_x, delta_y = delta_y, delta_x
        column = -delta_x if flip_x else delta_x
        row = -delta_y if flip_y else delta_y
        positions.append((row / pixel_y, column / pixel_x))
    return positions


def _reporter(progress: Optional[Callable[[str], None]]):
    """Where to send stage/progress messages.

    A real tiling run is a hundred multi-megapixel tiles, and every stage here
    takes long enough to look like a hang. Callers pass their own logger so the
    messages carry their prefix; otherwise these land on the module logger.
    """
    return logger.info if progress is None else progress


def load_dataset(path: Path | str,
                 progress: Optional[Callable[[str], None]] = None,
                 prefer_stage_positions: bool = True,
                 detector: Optional[str] = None) -> MosaicDataset:
    """Load a tiling dataset from its manifest.

    ``prefer_stage_positions`` starts the layout from the commanded stage
    coordinates rather than the manifest's saved pixel positions, which carry
    whatever the live registration pass did during acquisition — see
    :func:`_positions_from_stage`. Set it False to reconstruct exactly the
    layout that was saved.

    ``detector`` picks one detector out of a run that saved several. They were
    all captured at the same stage positions, so every detector shares one
    layout — solve it once on any of them and the rest follow. None loads the
    detector the run aligned on, which is what a single-detector dataset has.

    Raises FileNotFoundError when ``path`` is not part of a tiling dataset, and
    ValueError when the manifest lists no readable tile.
    """
    report = _reporter(progress)
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

    entries = [entry for entry in payload.get('tiles', []) if entry.get('filename')]
    report(f'Reading {len(entries)} tiles from {folder.name}...')

    stage_positions = (
        _positions_from_stage(payload, entries, dataset.pixel_size_um)
        if prefer_stage_positions else None
    )
    if stage_positions is not None:
        report('  laying out from the commanded stage positions')
    elif prefer_stage_positions:
        report('  no usable stage positions; using the saved pixel positions')

    if detector:
        report(f'  reading the {detector} images')

    missing = []
    for index, entry in enumerate(entries):
        # A multi-detector run names one file per detector; a single-detector
        # one names it flat, as it always did.
        descriptor = _file_descriptor(entry, detector)
        filename = descriptor.get('filename') or (
            entry['filename'] if not detector else ''
        )
        if not filename:
            missing.append(f'{entry["filename"]} ({detector})')
            continue
        tile_path = folder / filename
        if not tile_path.exists():
            missing.append(filename)
            continue
        data = _read_image(tile_path)
        if data is None:
            missing.append(filename)
            continue
        if index and index % 25 == 0:
            report(f'  read {index}/{len(entries)} tiles')
        if stage_positions is not None:
            position = stage_positions[index]
        else:
            # TileConfiguration stores (x, y); the mosaic works in (row, col).
            x, y = entry.get('pixel_xy', (0.0, 0.0))
            position = (float(y), float(x))

        # The descriptor says what the file holds; without one, fall back to
        # the old guess that leading singletons are container padding.
        # A descriptor that contradicts its own file is not a tile to skip.
        # Skipping means a mosaic that assembles, looks plausible and is quietly
        # missing data — the failure mode this whole descriptor exists to stop.
        # A *missing* file is different, and stays a skip: nothing about the
        # remaining tiles is in doubt.
        data, axes = _apply_descriptor(data, descriptor, filename)

        dataset.tiles.append(MosaicTile(
            name=filename,
            data=data,
            position=position,
            grid=tuple(entry.get('grid', (0, 0))),
            axes=axes,
        ))

    if missing:
        logger.warning('%d tile(s) listed in the manifest could not be read: %s',
                       len(missing), ', '.join(missing[:5]))
    if not dataset.tiles:
        raise ValueError(f'No readable tiles found for {manifest_path}')

    first = dataset.tiles[0]
    report(f'Read {len(dataset.tiles)} tiles of {first.plane_shape[0]}x'
           f'{first.plane_shape[1]} px'
           + (f' x {dataset.depth} planes' if dataset.is_volumetric else ''))
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
                f'{len(self.components)} groups nothing could be measured '
                f'across ({", ".join(str(size) for size in self.components[:4])}'
                f'{", ..." if len(self.components) > 4 else ""} tiles) — each '
                'is aligned within itself but placed by its stage position, '
                'so the groups are only as well placed relative to each other '
                'as the stage was'
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


def _bandpass(patch: np.ndarray) -> np.ndarray:
    """Strip the illumination profile and the pixel noise from a patch.

    Vignetting is fixed to the camera, not the sample, so where two tiles meet
    they show the *same* shading curve at *different* points on it — the
    falloff at one tile's right edge against the rise toward the other's
    centre. Those ramp in opposite directions and anti-correlate hard enough to
    bury the sample underneath: measured NCC of -0.99 on overlaps whose
    structure had in fact aligned to a third of a pixel. Left in, that costs
    good links, and losing links fragments the mosaic into groups that each
    fall back to raw stage coordinates — an internally fine region landing in
    the wrong place, which is exactly the artefact this module exists to stop.

    Subtracting a coarse local mean leaves the structure alignment actually
    rides on, and makes the score mean what it claims to.
    """
    from scipy.ndimage import gaussian_filter, uniform_filter

    arr = np.asarray(patch, dtype=np.float32)
    size = max(16, int(min(arr.shape) * BACKGROUND_SCALE_FRACTION))
    # A running-sum box mean rather than a Gaussian: at a radius this large a
    # Gaussian kernel would cost more than the correlation it prepares for.
    return gaussian_filter(arr, 1.0) - uniform_filter(arr, size=size)


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
    ref_patch = _normalize(_bandpass(reference[ref_slices]))
    mov_patch = _normalize(_bandpass(moving[mov_slices]))
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
    confidence = _ncc(_bandpass(reference[check[0]]),
                      _bandpass(moving[check[1]]))
    if confidence < MIN_LINK_CONFIDENCE:
        return None

    return measured, confidence


def _build_links(dataset: MosaicDataset, max_shift_px: float,
                 progress: Optional[Callable[[str], None]] = None
                 ) -> List[TileLink]:
    """Measure every pair of tiles the recorded layout says should overlap.

    This is the point of the offline path: a spiral gives most tiles two to
    four overlapping neighbours, and the tile that closes a ring sits next to
    the one that opened it. Every one of those pairs is a constraint.

    Correlating them is by far the slowest part of a run — the pair count grows
    with the mosaic and each one is an FFT over the shared region — so progress
    is reported as it goes.
    """
    report = _reporter(progress)
    projections = [tile.projection() for tile in dataset.tiles]
    positions = [tile.position for tile in dataset.tiles]
    shapes = [tile.plane_shape for tile in dataset.tiles]

    # Finding the candidates is pure bounding-box arithmetic, so do it up front
    # to know how much work there is before starting any of it.
    candidates = []
    for i in range(len(dataset.tiles)):
        for j in range(i + 1, len(dataset.tiles)):
            offset = (
                positions[j][0] - positions[i][0],
                positions[j][1] - positions[i][1],
            )
            if _overlap_box(shapes[i], shapes[j], offset) is not None:
                candidates.append((i, j, offset))

    report(f'Correlating {len(candidates)} overlapping tile pairs '
           f'({len(dataset.tiles)} tiles)...')
    step = max(1, len(candidates) // 10)

    links: List[TileLink] = []
    for done, (i, j, offset) in enumerate(candidates, start=1):
        measured = _measure_link(
            projections[i], projections[j], offset, max_shift_px
        )
        if measured is not None:
            offset_measured, confidence = measured
            links.append(TileLink(
                i=i, j=j,
                offset=offset_measured,
                confidence=confidence,
                correction=(offset_measured[0] - offset[0],
                            offset_measured[1] - offset[1]),
            ))
        if done % step == 0 or done == len(candidates):
            report(f'  correlated {done}/{len(candidates)} pairs, '
                   f'{len(links)} usable')
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


def solve_links(nominal: Sequence[Tuple[float, float]],
                links: List[TileLink],
                progress: Optional[Callable[[str], None]] = None,
                rounds: int = MAX_SOLVE_ROUNDS):
    """Solve a layout from measured links, rejecting the inconsistent ones.

    The solver on its own, for callers that measured their own links — the live
    acquisition path does, one tile at a time, and wants the same global answer
    at the end of a run that the offline path gives. ``links`` is annotated in
    place with each link's residual and whether it survived.

    Returns ``(positions, accepted_links)``.
    """
    emit = _reporter(progress)
    active = list(links)
    positions = _solve(nominal, active)
    for round_index in range(rounds):
        _update_residuals(links, positions)
        kept = _drop_outliers(active)
        if len(kept) == len(active) or not kept:
            break
        emit(f'  solve round {round_index + 1}: dropped '
             f'{len(active) - len(kept)} inconsistent link(s), re-solving')
        active = kept
        positions = _solve(nominal, active)
    _update_residuals(links, positions)
    return positions, active


def refine_layout(dataset: MosaicDataset,
                  max_shift_px: Optional[float] = None,
                  progress: Optional[Callable[[str], None]] = None
                  ) -> RefinementReport:
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

    emit = _reporter(progress)
    nominal = [tile.position for tile in dataset.tiles]
    report.links = _build_links(dataset, max_shift_px, progress)
    if not report.links:
        emit('No tile pair could be correlated; keeping the stage positions.')
        return report

    positions, active = solve_links(nominal, report.links, progress)

    for tile, position, before in zip(dataset.tiles, positions, nominal):
        tile.position = position
        if (abs(position[0] - before[0]) > MOVED_EPS_PX
                or abs(position[1] - before[1]) > MOVED_EPS_PX):
            report.moved += 1

    report.components = [
        len(nodes) for nodes in _components(len(dataset.tiles), active)
    ]
    emit(report.summary())
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


def assemble(dataset: MosaicDataset, blend: bool = True,
             progress: Optional[Callable[[str], None]] = None) -> np.ndarray:
    """Paste every tile into one array.

    The mosaic keeps whatever leading axes the tiles have — none for a plain
    image, ``Z`` for a stack, ``CZ`` for a line-step scan of one — and the
    tiles are laid out in Y/X regardless. Overlaps are averaged when ``blend``
    is set, otherwise later tiles overwrite earlier ones.

    Tiles need not all be the same rank: one that came up short is broadcast
    over the axes it lacks, so a plain image in a volumetric set contributes to
    every plane rather than being dropped or forcing the mosaic flat.
    """
    if not dataset.tiles:
        raise ValueError('Cannot assemble an empty dataset')
    report = _reporter(progress)

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
    leading = dataset.leading_shape
    shape = leading + (height, width)

    # Overlap counts, so a small integer type is exact.
    #
    # The weights need the mosaic's leading rank whenever the tiles disagree
    # about it: a tile covering fewer planes than the mosaic still overlaps in
    # Y/X, so a plane-blind count would credit every plane with a contributor
    # that only reached one of them, and divide the rest down. When every tile
    # covers the same leading extent, coverage is identical on every plane and
    # one Y/X plane of counts is exactly right -- which is the ordinary case,
    # and worth keeping cheap.
    uniform = len({tile.leading_shape for tile in dataset.tiles}) <= 1
    weight_shape = (height, width) if uniform else shape

    # A hundred 2000x2000 tiles make a ~15000x15000 mosaic, so say how much
    # memory is about to be asked for before asking for it: an allocation that
    # fails, or starts the machine swapping, is otherwise a silent hang.
    footprint = (np.float32().itemsize * int(np.prod(shape))
                 + np.uint16().itemsize * int(np.prod(weight_shape)))
    report(f'Assembling {len(dataset.tiles)} tiles into a '
           f'{"x".join(str(size) for size in shape)} mosaic '
           f'({footprint / 1e9:.2f} GB)...')

    canvas_sum = np.zeros(shape, dtype=np.float32)
    canvas_weight = np.zeros(weight_shape, dtype=np.uint16)

    for tile in dataset.tiles:
        top = int(round(tile.position[0])) - row0
        left = int(round(tile.position[1])) - col0
        tile_h, tile_w = tile.plane_shape
        tile_h = min(tile_h, height - top)
        tile_w = min(tile_w, width - left)
        if tile_h <= 0 or tile_w <= 0:
            continue

        data = np.asarray(tile.data, dtype=np.float32)

        # Crop Y/X to what fits, then match the mosaic's leading rank. A tile
        # with fewer leading axes is broadcast over the ones it lacks; one with
        # a shorter extent fills only as far as it goes, so the rest of that
        # axis keeps whatever other tiles put there.
        source = data[..., :tile_h, :tile_w]
        missing = len(shape) - source.ndim
        if missing > 0:
            # Fewer leading axes than the mosaic has. The tile says nothing
            # about the axes it lacks, so it contributes equally to all of
            # them — a plain image in a volumetric set belongs on every plane.
            # This is not the same as a genuine length-1 axis, which covers
            # one position and must not be spread across the rest.
            source = np.broadcast_to(source, leading[:missing] + source.shape)
        covered = tuple(
            slice(0, min(extent, size))
            for extent, size in zip(source.shape[:-2], leading)
        )
        window = (slice(top, top + tile_h), slice(left, left + tile_w))
        source = source[covered + (slice(None), slice(None))]
        target = canvas_sum[covered + window]
        # The weights are indexed the same way as the pixels they divide, so a
        # tile can only ever credit the planes it actually contributed to.
        weight_view = (canvas_weight[window] if uniform
                       else canvas_weight[covered + window])

        if blend:
            target += source
            weight_view += 1
        else:
            target[...] = source
            weight_view[:] = 1

    # Divide in place. Allocating a separate quotient and a separate
    # divide-safe copy of the weights costs two more arrays the size of the
    # mosaic, which on a large run is gigabytes for nothing: the weights are
    # counts, so clamping them to a minimum of one both avoids the zero divide
    # and leaves uncovered pixels at the zero they already hold.
    np.maximum(canvas_weight, 1, out=canvas_weight)
    divisor = (canvas_weight[(np.newaxis,) * len(leading) + (Ellipsis,)]
               if uniform else canvas_weight)
    np.divide(canvas_sum, divisor, out=canvas_sum)
    report('Mosaic assembled.')
    return canvas_sum


def assemble_dataset(path: Path | str, *, blend: bool = True,
                     refine: bool = False,
                     max_shift_px: Optional[float] = None):
    """Load, optionally refine, and assemble in one call.

    Returns ``(mosaic, dataset, tiles_moved)``.
    """
    dataset = load_dataset(path)
    moved = refine_positions(dataset, max_shift_px) if refine else 0
    return assemble(dataset, blend=blend), dataset, moved
