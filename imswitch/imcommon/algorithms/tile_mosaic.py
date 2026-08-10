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

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import (
    Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple,
)

import numpy as np

from .detector_transform import DetectorTransform, parse_detector_transform

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

#: Gaussian sigma for the shading estimate, as a fraction of the tile's short
#: side. Large on purpose: the estimate must keep the illumination envelope and
#: discard everything that is sample.
SHADING_SMOOTH_FRACTION = 0.15
#: Below this many tiles the sample has not averaged out and the "profile"
#: would largely be the specimen.
SHADING_MIN_TILES = 4
#: Below this many, it is estimated but worth saying out loud.
SHADING_TRUSTWORTHY_TILES = 9
#: A profile is a gentle envelope. Anything outside this is a failed estimate,
#: and dividing by it would do more damage than the shading it corrects.
SHADING_CLIP = (0.2, 5.0)

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
    #: Stable version-2 manifest identity. Legacy datasets use their manifest
    #: ordinal so applying a layout never depends on which files were readable.
    tile_id: Optional[int] = None

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


@dataclass(frozen=True)
class AlignmentArtifact:
    """The singular image from which a run's geometry is solved."""

    path: Path
    detector: str
    axes: str
    stored_axes: str
    shape: Tuple[int, ...]
    stored_shape: Optional[Tuple[int, ...]] = None


@dataclass(frozen=True)
class ManifestPayloadRef:
    """Read-side view of ``RecordingManager.PayloadLocator``.

    The acquisition type is deliberately not imported across the imcommon
    boundary.  This value is parsed from its version-2 manifest record and
    carries the normalized transform that places detector-local pixels on the
    alignment grid.
    """

    path: Path
    group: Optional[str]
    detector: str
    axes: str
    stored_axes: str
    shape: Tuple[int, ...]
    stored_shape: Optional[Tuple[int, ...]]
    generation: Optional[int]
    complete: bool
    transform_to_alignment: DetectorTransform


@dataclass(frozen=True)
class IndexedTile:
    """One manifest entry, retaining its ordinal even when files are absent."""

    tile_id: int
    grid: Tuple[int, int]
    stage_um: Tuple[float, float]
    saved_position_yx: Tuple[float, float]
    alignment: AlignmentArtifact
    payloads: Dict[str, ManifestPayloadRef]


@dataclass(frozen=True)
class TilingDatasetIndex:
    """Manifest-only tiling index; constructing it never reads tile pixels."""

    manifest: Path
    alignment_detector: str
    pixel_size_yx_um: Tuple[float, float]
    z_step_um: float
    tiles: Tuple[IndexedTile, ...]
    detectors: Tuple[str, ...]
    orientation: Tuple[bool, bool, bool] = (False, False, False)
    format: str = ''


@dataclass(frozen=True)
class DatasetCompleteness:
    """Cheap declared/file-presence summary returned with an index."""

    total_tiles: int
    alignment_files_present: int
    payloads_declared: Mapping[str, int]
    payloads_complete: Mapping[str, int]
    missing_paths: Tuple[Path, ...] = ()


@dataclass(frozen=True)
class ArrayInspection:
    """Header-only description of one exact payload or alignment array."""

    stored_shape: Tuple[int, ...]
    logical_shape: Tuple[int, ...]
    dtype: np.dtype
    squeeze_axes: Tuple[int, ...]


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


class ManifestValidationError(ValueError):
    """A versioned manifest contradicts the tiling format contract."""


class LocatorReadError(ValueError):
    """A declared complete artifact cannot be opened exactly as described."""


def _manifest_error(label: str, message: str) -> ManifestValidationError:
    return ManifestValidationError(f'{label}: {message}')


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _manifest_error(label, 'must be an object')
    return value


def _shape(value: Any, label: str, *, required: bool = True) -> Tuple[int, ...]:
    if value in (None, ()) or value == []:
        if required:
            raise _manifest_error(label, 'is required')
        return ()
    if isinstance(value, (str, bytes)):
        raise _manifest_error(label, 'must be a sequence of positive integers')
    try:
        parsed = tuple(int(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise _manifest_error(
            label, 'must be a sequence of positive integers'
        ) from exc
    if not parsed or any(item <= 0 for item in parsed):
        raise _manifest_error(label, 'must contain only positive integers')
    return parsed


def _axis_string(value: Any, label: str, *, required: bool = True) -> str:
    axes = str(value or '')
    if not axes and not required:
        return ''
    if not axes:
        raise _manifest_error(label, 'is required')
    if len(set(axes)) != len(axes):
        raise _manifest_error(label, f'{axes!r} repeats an axis name')
    if not axes.endswith('YX'):
        raise _manifest_error(label, f'{axes!r} must end in YX')
    return axes


def _axes(value: Any, shape: Tuple[int, ...], label: str,
          *, required: bool = True) -> str:
    axes = _axis_string(value, label, required=required)
    if not axes:
        return ''
    if len(axes) != len(shape):
        raise _manifest_error(
            label, f'{axes!r} names {len(axes)} axes for shape {shape}'
        )
    return axes


def _validate_axis_reduction(
    axes: str,
    shape: Tuple[int, ...],
    stored_axes: str,
    stored_shape: Optional[Tuple[int, ...]],
    label: str,
) -> None:
    filtered = ''.join(axis for axis in stored_axes if axis in axes)
    if filtered != axes:
        raise _manifest_error(
            label,
            f'{axes!r} is not obtained by removing axes from {stored_axes!r}',
        )
    if stored_shape is None:
        return
    extra = tuple(
        position for position, axis in enumerate(stored_axes) if axis not in axes
    )
    if any(stored_shape[position] != 1 for position in extra):
        raise _manifest_error(
            label, 'would remove a stored axis whose extent is not one'
        )
    reduced = tuple(
        extent for position, extent in enumerate(stored_shape)
        if position not in extra
    )
    if reduced != shape:
        raise _manifest_error(
            label,
            f'stored shape {stored_shape} reduces to {reduced}, not {shape}',
        )


def _pair(value: Any, label: str, caster) -> Tuple[Any, Any]:
    if isinstance(value, (str, bytes)):
        raise _manifest_error(label, 'must contain two values')
    try:
        parsed = tuple(value)
    except TypeError as exc:
        raise _manifest_error(label, 'must contain two values') from exc
    if len(parsed) < 2:
        raise _manifest_error(label, 'must contain two values')
    try:
        return caster(parsed[0]), caster(parsed[1])
    except (TypeError, ValueError) as exc:
        raise _manifest_error(label, 'contains a non-numeric value') from exc


def _v2_path(manifest: Path, value: Any, label: str, *,
             allow_absolute: bool) -> Path:
    raw = Path(str(value or ''))
    if not str(value or ''):
        raise _manifest_error(label, 'is required')
    if '..' in raw.parts:
        raise _manifest_error(label, 'must not contain .. traversal')

    root = manifest.parent.resolve()
    if raw.is_absolute():
        if not allow_absolute:
            raise _manifest_error(label, 'must be relative to the run folder')
        return raw.resolve()

    resolved = (root / raw).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise _manifest_error(
            label, 'resolves outside the run folder (including via symlink)'
        ) from exc
    return resolved


def _legacy_path(manifest: Path, value: Any, label: str) -> Path:
    if not str(value or ''):
        raise _manifest_error(label, 'is required')
    raw = Path(str(value))
    return raw if raw.is_absolute() else manifest.parent / raw


def _group(value: Any, label: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _manifest_error(label, 'must be a string or null')
    normalized = value.strip('/')
    if not normalized:
        return None
    if any(part in ('.', '..') for part in normalized.split('/')):
        raise _manifest_error(label, 'must name an exact container group')
    return normalized


def _parse_v2_index(payload: Mapping[str, Any], manifest: Path, *,
                    allow_absolute_paths: bool) -> TilingDatasetIndex:
    raw_tiles = payload.get('tiles')
    if not isinstance(raw_tiles, list):
        raise _manifest_error('tiles', 'must be an array')

    pixel = _mapping(payload.get('pixel_size_um'), 'pixel_size_um')
    try:
        pixel_size = (float(pixel['y']), float(pixel['x']))
    except (KeyError, TypeError, ValueError) as exc:
        raise _manifest_error(
            'pixel_size_um', 'must contain numeric y and x values'
        ) from exc
    if any(not np.isfinite(item) or item <= 0 for item in pixel_size):
        raise _manifest_error('pixel_size_um', 'values must be positive')

    orientation = _mapping(payload.get('orientation') or {}, 'orientation')
    orientation_tuple = (
        bool(orientation.get('flip_x', False)),
        bool(orientation.get('flip_y', False)),
        bool(orientation.get('swap_axes', False)),
    )

    parsed_tiles: List[IndexedTile] = []
    detector_names: List[str] = []
    alignment_detector: Optional[str] = None

    for tile_id, raw_entry in enumerate(raw_tiles):
        entry_label = f'tiles[{tile_id}]'
        entry = _mapping(raw_entry, entry_label)
        alignment = _mapping(entry.get('alignment'), f'{entry_label}.alignment')

        detector = str(alignment.get('detector') or '')
        if not detector:
            raise _manifest_error(
                f'{entry_label}.alignment.detector', 'is required'
            )
        if alignment_detector is None:
            alignment_detector = detector
        elif detector != alignment_detector:
            raise _manifest_error(
                f'{entry_label}.alignment.detector',
                f'{detector!r} disagrees with run alignment detector '
                f'{alignment_detector!r}',
            )

        alignment_shape = _shape(
            alignment.get('shape'), f'{entry_label}.alignment.shape'
        )
        alignment_axes = _axes(
            alignment.get('axes'), alignment_shape,
            f'{entry_label}.alignment.axes',
        )
        alignment_stored_axes = _axis_string(
            alignment.get('stored_axes'),
            f'{entry_label}.alignment.stored_axes',
        )
        alignment_stored_shape_raw = alignment.get('stored_shape')
        alignment_stored_shape = (
            _shape(
                alignment_stored_shape_raw,
                f'{entry_label}.alignment.stored_shape',
            )
            if alignment_stored_shape_raw not in (None, (), []) else None
        )
        if alignment_stored_shape is not None:
            _axes(
                alignment_stored_axes, alignment_stored_shape,
                f'{entry_label}.alignment.stored_axes',
            )
        _validate_axis_reduction(
            alignment_axes,
            alignment_shape,
            alignment_stored_axes,
            alignment_stored_shape,
            f'{entry_label}.alignment',
        )

        alignment_artifact = AlignmentArtifact(
            path=_v2_path(
                manifest, alignment.get('filename'),
                f'{entry_label}.alignment.filename',
                allow_absolute=allow_absolute_paths,
            ),
            detector=detector,
            axes=alignment_axes,
            stored_axes=alignment_stored_axes,
            shape=alignment_shape,
            stored_shape=alignment_stored_shape,
        )

        raw_payloads = _mapping(entry.get('payloads'), f'{entry_label}.payloads')
        payload_refs: Dict[str, ManifestPayloadRef] = {}
        for raw_name, raw_ref in raw_payloads.items():
            name = str(raw_name)
            ref_label = f'{entry_label}.payloads[{name!r}]'
            ref = _mapping(raw_ref, ref_label)
            declared_detector = str(ref.get('detector') or name)
            if declared_detector != name:
                raise _manifest_error(
                    f'{ref_label}.detector',
                    f'{declared_detector!r} disagrees with key {name!r}',
                )
            if 'group' not in ref:
                raise _manifest_error(f'{ref_label}.group', 'is required (null is valid)')
            if 'complete' not in ref or not isinstance(ref['complete'], bool):
                raise _manifest_error(f'{ref_label}.complete', 'must be boolean')

            logical_shape = _shape(ref.get('shape'), f'{ref_label}.shape')
            logical_axes = _axes(
                ref.get('axes'), logical_shape, f'{ref_label}.axes'
            )
            stored_axes = _axis_string(
                ref.get('stored_axes'), f'{ref_label}.stored_axes'
            )
            stored_shape_raw = ref.get('stored_shape')
            stored_shape = (
                _shape(stored_shape_raw, f'{ref_label}.stored_shape')
                if stored_shape_raw not in (None, (), []) else None
            )
            if stored_shape is not None:
                _axes(stored_axes, stored_shape, f'{ref_label}.stored_axes')
            _validate_axis_reduction(
                logical_axes,
                logical_shape,
                stored_axes,
                stored_shape,
                ref_label,
            )

            generation_raw = ref.get('generation')
            if generation_raw is None:
                generation = None
            else:
                try:
                    generation = int(generation_raw)
                except (TypeError, ValueError) as exc:
                    raise _manifest_error(
                        f'{ref_label}.generation', 'must be an integer or null'
                    ) from exc

            try:
                transform = parse_detector_transform(
                    ref.get('transform_to_alignment'),
                    allow_reference=name == alignment_detector,
                    label=f'{ref_label}.transform_to_alignment',
                )
            except ValueError as exc:
                raise ManifestValidationError(str(exc)) from exc

            payload_refs[name] = ManifestPayloadRef(
                path=_v2_path(
                    manifest, ref.get('path'), f'{ref_label}.path',
                    allow_absolute=allow_absolute_paths,
                ),
                group=_group(ref.get('group'), f'{ref_label}.group'),
                detector=name,
                axes=logical_axes,
                stored_axes=stored_axes,
                shape=logical_shape,
                stored_shape=stored_shape,
                generation=generation,
                complete=ref['complete'],
                transform_to_alignment=transform,
            )
            if name not in detector_names:
                detector_names.append(name)

        grid = _pair(entry.get('grid'), f'{entry_label}.grid', int)
        stage_um = _pair(entry.get('stage_um'), f'{entry_label}.stage_um', float)
        pixel_xy = _pair(entry.get('pixel_xy'), f'{entry_label}.pixel_xy', float)
        if any(not np.isfinite(value) for value in (*stage_um, *pixel_xy)):
            raise _manifest_error(
                entry_label, 'stage_um and pixel_xy must contain finite values'
            )
        parsed_tiles.append(IndexedTile(
            tile_id=tile_id,
            grid=grid,
            stage_um=stage_um,
            saved_position_yx=(pixel_xy[1], pixel_xy[0]),
            alignment=alignment_artifact,
            payloads=payload_refs,
        ))

    return TilingDatasetIndex(
        manifest=manifest.resolve(),
        alignment_detector=alignment_detector or '',
        pixel_size_yx_um=pixel_size,
        z_step_um=float(payload.get('z_step_um', 0.0) or 0.0),
        tiles=tuple(parsed_tiles),
        detectors=tuple(detector_names),
        orientation=orientation_tuple,
        format='imswitch-tiling/2',
    )


def _parse_legacy_index(
    payload: Mapping[str, Any],
    manifest: Path,
    *,
    format_override: Optional[str] = None,
) -> TilingDatasetIndex:
    """Index legacy manifests without changing their permissive read behavior."""
    raw_tiles = payload.get('tiles') or []
    if not isinstance(raw_tiles, list):
        raise _manifest_error('tiles', 'must be an array')

    pixel = payload.get('pixel_size_um') or {}
    pixel_size = (
        float(pixel.get('y', 1.0) or 1.0),
        float(pixel.get('x', 1.0) or 1.0),
    )
    orientation = payload.get('orientation') or {}
    detector_names: List[str] = []
    parsed_tiles: List[IndexedTile] = []
    alignment_detector = ''

    for tile_id, raw_entry in enumerate(raw_tiles):
        entry_label = f'tiles[{tile_id}]'
        entry = _mapping(raw_entry, entry_label)
        descriptor = _file_descriptor(dict(entry), None)
        filename = descriptor.get('filename') or entry.get('filename')
        axes = str(descriptor.get('axes') or '')
        logical_shape = _shape(
            descriptor.get('shape'), f'{entry_label}.shape', required=False
        )
        if axes and logical_shape:
            _axes(axes, logical_shape, f'{entry_label}.axes')
        stored_axes = str(descriptor.get('stored_axes') or axes)
        stored_shape = _shape(
            descriptor.get('stored_shape'), f'{entry_label}.stored_shape',
            required=False,
        ) or None
        alignment = entry.get('alignment') or {}
        detector = str(alignment.get('detector') or '')
        if detector and not alignment_detector:
            alignment_detector = detector

        payload_refs: Dict[str, ManifestPayloadRef] = {}
        for key in ('payloads', 'files'):
            raw_group = entry.get(key)
            if not isinstance(raw_group, Mapping):
                continue
            for raw_name, raw_ref in raw_group.items():
                if not isinstance(raw_ref, Mapping):
                    continue
                name = str(raw_name)
                if name not in detector_names:
                    detector_names.append(name)
                # Legacy loading remains in load_dataset. These references make
                # enumeration/indexing available without pretending their
                # underspecified containers satisfy the version-2 contract.
                ref_path = raw_ref.get('path') or raw_ref.get('filename')
                ref_shape = _shape(
                    raw_ref.get('shape'), f'{entry_label}.{key}[{name!r}].shape',
                    required=False,
                )
                ref_axes = str(raw_ref.get('axes') or '')
                payload_refs[name] = ManifestPayloadRef(
                    path=_legacy_path(
                        manifest, ref_path,
                        f'{entry_label}.{key}[{name!r}].path',
                    ),
                    group=_group(
                        raw_ref.get('group'),
                        f'{entry_label}.{key}[{name!r}].group',
                    ),
                    detector=name,
                    axes=ref_axes,
                    stored_axes=str(raw_ref.get('stored_axes') or ref_axes),
                    shape=ref_shape,
                    stored_shape=None,
                    generation=None,
                    complete=bool(raw_ref.get('complete', True)),
                    transform_to_alignment=parse_detector_transform('identity'),
                )

        grid = _pair(entry.get('grid', (0, 0)), f'{entry_label}.grid', int)
        stage_um = _pair(
            entry.get('stage_um', (0.0, 0.0)), f'{entry_label}.stage_um', float
        )
        pixel_xy = _pair(
            entry.get('pixel_xy', (0.0, 0.0)), f'{entry_label}.pixel_xy', float
        )
        parsed_tiles.append(IndexedTile(
            tile_id=tile_id,
            grid=grid,
            stage_um=stage_um,
            saved_position_yx=(pixel_xy[1], pixel_xy[0]),
            alignment=AlignmentArtifact(
                path=_legacy_path(manifest, filename, f'{entry_label}.filename'),
                detector=detector,
                axes=axes,
                stored_axes=stored_axes,
                shape=logical_shape,
                stored_shape=stored_shape,
            ),
            payloads=payload_refs,
        ))

    return TilingDatasetIndex(
        manifest=manifest.resolve(),
        alignment_detector=alignment_detector,
        pixel_size_yx_um=pixel_size,
        z_step_um=float(payload.get('z_step_um', 0.0) or 0.0),
        tiles=tuple(parsed_tiles),
        detectors=tuple(detector_names),
        orientation=(
            bool(orientation.get('flip_x', False)),
            bool(orientation.get('flip_y', False)),
            bool(orientation.get('swap_axes', False)),
        ),
        format=(
            str(format_override)
            if format_override is not None
            else str(payload.get('format') or '')
        ),
    )


def _is_interim_v2_manifest(payload: Mapping[str, Any]) -> bool:
    """Whether a nominal v2 file is the short-lived ``files`` layout.

    The interim writer bumped the format before the singular ``alignment`` and
    exact ``payloads`` contracts existed.  It is recognized only by its
    unmistakable shape: every tile has the old detector-keyed ``files`` map and
    none has either current field.  A malformed current manifest must still
    fail strict parsing rather than falling back to permissive discovery.
    """
    raw_tiles = payload.get('tiles')
    if not isinstance(raw_tiles, list) or not raw_tiles:
        return False
    for raw_entry in raw_tiles:
        if not isinstance(raw_entry, Mapping):
            return False
        if 'alignment' in raw_entry or 'payloads' in raw_entry:
            return False
        if not isinstance(raw_entry.get('files'), Mapping):
            return False
    return True


def _index_manifest(payload: Mapping[str, Any], manifest: Path, *,
                    allow_absolute_v2_paths: bool = False) -> TilingDatasetIndex:
    manifest_format = str(payload.get('format') or '')
    if manifest_format == 'imswitch-tiling/2':
        if _is_interim_v2_manifest(payload):
            return _parse_legacy_index(
                payload,
                manifest,
                format_override='imswitch-tiling/2-interim',
            )
        return _parse_v2_index(
            payload, manifest,
            allow_absolute_paths=allow_absolute_v2_paths,
        )
    if manifest_format in ('', 'imswitch-tiling/1'):
        return _parse_legacy_index(payload, manifest)
    raise ManifestValidationError(
        f'Unsupported tiling manifest format {manifest_format!r}'
    )


def inspect_dataset(
    path: Path | str,
    *,
    allow_absolute_v2_paths: bool = False,
) -> Tuple[TilingDatasetIndex, DatasetCompleteness]:
    """Parse a tiling run and summarize it without reading image arrays."""
    manifest = find_manifest(path)
    if manifest is None:
        raise FileNotFoundError(
            f'No {MANIFEST_NAME} found next to {path}. Open a file from a '
            'tiling dataset folder, or the manifest itself.'
        )
    try:
        raw = json.loads(manifest.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestValidationError(f'Could not parse {manifest}: {exc}') from exc
    payload = _mapping(raw, str(manifest))
    index = _index_manifest(
        payload, manifest,
        allow_absolute_v2_paths=allow_absolute_v2_paths,
    )

    declared = {name: 0 for name in index.detectors}
    complete = {name: 0 for name in index.detectors}
    alignment_present = 0
    missing: List[Path] = []
    for tile in index.tiles:
        if tile.alignment.path.exists():
            alignment_present += 1
        else:
            missing.append(tile.alignment.path)
        for name, ref in tile.payloads.items():
            declared[name] = declared.get(name, 0) + 1
            if ref.complete:
                complete[name] = complete.get(name, 0) + 1
            if not ref.path.exists():
                missing.append(ref.path)

    return index, DatasetCompleteness(
        total_tiles=len(index.tiles),
        alignment_files_present=alignment_present,
        payloads_declared=declared,
        payloads_complete=complete,
        missing_paths=tuple(dict.fromkeys(missing)),
    )


def manifest_fingerprint(path: Path | str) -> Tuple[str, int, int, str]:
    """Return a content-backed identity for a parsed tiling manifest."""
    manifest = find_manifest(path)
    if manifest is None:
        raise FileNotFoundError(f'No {MANIFEST_NAME} found for {path}')
    content = manifest.read_bytes()
    stat = manifest.stat()
    return (
        str(manifest.resolve()),
        int(stat.st_size),
        int(stat.st_mtime_ns),
        hashlib.sha256(content).hexdigest(),
    )


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


def _container_kind(path: Path) -> str:
    suffix = ''.join(path.suffixes).lower()
    if suffix.endswith(('.tif', '.tiff')):
        return 'tiff'
    if suffix.endswith(('.h5', '.hdf5')):
        return 'hdf5'
    if suffix.endswith('.zarr'):
        return 'zarr'
    raise LocatorReadError(f'Unsupported payload format: {path}')


def _logical_inspection(
    stored_shape: Sequence[int],
    dtype: Any,
    *,
    axes: str,
    stored_axes: str,
    declared_shape: Sequence[int],
    declared_stored_shape: Optional[Sequence[int]],
    label: str,
) -> ArrayInspection:
    actual = tuple(int(item) for item in stored_shape)
    if len(stored_axes) != len(actual):
        raise LocatorReadError(
            f'{label}: stored_axes {stored_axes!r} names {len(stored_axes)} '
            f'axes but the exact array has rank {len(actual)}'
        )
    if declared_stored_shape is not None and actual != tuple(declared_stored_shape):
        raise LocatorReadError(
            f'{label}: manifest declares stored shape '
            f'{tuple(declared_stored_shape)} but the exact array holds {actual}'
        )

    filtered = ''.join(axis for axis in stored_axes if axis in axes)
    if filtered != axes:
        raise LocatorReadError(
            f'{label}: logical axes {axes!r} are not obtained by removing '
            f'axes from stored_axes {stored_axes!r}'
        )
    squeeze_axes = tuple(
        position for position, axis in enumerate(stored_axes) if axis not in axes
    )
    for position in squeeze_axes:
        if actual[position] != 1:
            raise LocatorReadError(
                f'{label}: removing stored axis {stored_axes[position]!r} at '
                f'position {position} would discard {actual[position]} values'
            )

    logical = tuple(
        extent for position, extent in enumerate(actual)
        if position not in squeeze_axes
    )
    if logical != tuple(declared_shape):
        raise LocatorReadError(
            f'{label}: manifest declares logical shape {tuple(declared_shape)} '
            f'for {axes!r} but the exact array yields {logical}'
        )
    return ArrayInspection(
        stored_shape=actual,
        logical_shape=logical,
        dtype=np.dtype(dtype),
        squeeze_axes=squeeze_axes,
    )


def _hdf5_node(handle: Any, group: Optional[str], label: str):
    import h5py

    if group is None:
        raise LocatorReadError(
            f'{label}: HDF5 locator requires the exact detector group or array'
        )
    if group not in handle:
        raise LocatorReadError(f'{label}: HDF5 group {group!r} does not exist')
    node = handle[group]
    if isinstance(node, h5py.Dataset):
        return node
    if not isinstance(node, h5py.Group) or 'data' not in node:
        raise LocatorReadError(
            f'{label}: HDF5 group {group!r} has no direct data array'
        )
    data = node['data']
    if not isinstance(data, h5py.Dataset):
        raise LocatorReadError(
            f'{label}: HDF5 node {group!r}/data is not an array'
        )
    return data


def _zarr_node(root: Any, group: Optional[str], label: str):
    if group is None:
        node = root
    else:
        try:
            node = root[group]
        except (KeyError, IndexError) as exc:
            raise LocatorReadError(
                f'{label}: Zarr group {group!r} does not exist'
            ) from exc

    if hasattr(node, 'shape') and hasattr(node, 'dtype'):
        return node
    try:
        data = node['data']
    except (KeyError, IndexError, TypeError) as exc:
        shown = group if group is not None else '<root>'
        raise LocatorReadError(
            f'{label}: Zarr group {shown!r} has no direct data array'
        ) from exc
    if not hasattr(data, 'shape') or not hasattr(data, 'dtype'):
        raise LocatorReadError(f'{label}: selected Zarr data node is not an array')
    return data


def _inspect_or_read_exact(
    path: Path,
    group: Optional[str],
    *,
    axes: str,
    stored_axes: str,
    shape: Sequence[int],
    stored_shape: Optional[Sequence[int]],
    label: str,
    materialize: bool,
) -> Tuple[ArrayInspection, Optional[np.ndarray]]:
    if not path.exists():
        raise FileNotFoundError(f'{label}: declared file does not exist: {path}')
    kind = _container_kind(path)

    if kind == 'tiff':
        if group is not None:
            raise LocatorReadError(
                f'{label}: TIFF payload requires group=null, got {group!r}'
            )
        import tifffile
        try:
            with tifffile.TiffFile(str(path)) as handle:
                if len(handle.series) != 1:
                    raise LocatorReadError(
                        f'{label}: TIFF contains {len(handle.series)} series; '
                        'the version-2 locator declares exactly one'
                    )
                series = handle.series[0]
                inspection = _logical_inspection(
                    series.shape, series.dtype,
                    axes=axes, stored_axes=stored_axes,
                    declared_shape=shape,
                    declared_stored_shape=stored_shape,
                    label=label,
                )
                array = np.asarray(series.asarray()) if materialize else None
        except LocatorReadError:
            raise
        except Exception as exc:
            raise LocatorReadError(f'{label}: could not open TIFF: {exc}') from exc

    elif kind == 'hdf5':
        import h5py
        try:
            with h5py.File(str(path), 'r') as handle:
                node = _hdf5_node(handle, group, label)
                inspection = _logical_inspection(
                    node.shape, node.dtype,
                    axes=axes, stored_axes=stored_axes,
                    declared_shape=shape,
                    declared_stored_shape=stored_shape,
                    label=label,
                )
                array = np.asarray(node) if materialize else None
        except (LocatorReadError, FileNotFoundError):
            raise
        except Exception as exc:
            raise LocatorReadError(f'{label}: could not open HDF5: {exc}') from exc

    else:
        import zarr
        root = None
        try:
            root = zarr.open(str(path), mode='r')
            node = _zarr_node(root, group, label)
            inspection = _logical_inspection(
                node.shape, node.dtype,
                axes=axes, stored_axes=stored_axes,
                declared_shape=shape,
                declared_stored_shape=stored_shape,
                label=label,
            )
            array = np.asarray(node) if materialize else None
        except (LocatorReadError, FileNotFoundError):
            raise
        except Exception as exc:
            raise LocatorReadError(f'{label}: could not open Zarr: {exc}') from exc
        finally:
            store = getattr(root, 'store', None)
            close = getattr(store, 'close', None)
            if callable(close):
                close()

    if array is not None and inspection.squeeze_axes:
        array = np.squeeze(array, axis=inspection.squeeze_axes)
    return inspection, array


def inspect_manifest_payload(ref: ManifestPayloadRef) -> ArrayInspection:
    """Inspect exactly the array named by a complete payload locator."""
    if not ref.complete:
        raise LocatorReadError(
            f'tile payload {ref.detector!r} is explicitly incomplete'
        )
    inspection, _ = _inspect_or_read_exact(
        ref.path, ref.group,
        axes=ref.axes,
        stored_axes=ref.stored_axes,
        shape=ref.shape,
        stored_shape=ref.stored_shape,
        label=f'payload {ref.detector!r}',
        materialize=False,
    )
    return inspection


def read_manifest_payload(ref: ManifestPayloadRef) -> np.ndarray:
    """Materialize exactly the logical array named by a complete locator."""
    if not ref.complete:
        raise LocatorReadError(
            f'tile payload {ref.detector!r} is explicitly incomplete'
        )
    _, array = _inspect_or_read_exact(
        ref.path, ref.group,
        axes=ref.axes,
        stored_axes=ref.stored_axes,
        shape=ref.shape,
        stored_shape=ref.stored_shape,
        label=f'payload {ref.detector!r}',
        materialize=True,
    )
    assert array is not None
    return array


def inspect_alignment_artifact(artifact: AlignmentArtifact) -> ArrayInspection:
    """Inspect an alignment image through its format-defined exact node."""
    group = None if _container_kind(artifact.path) == 'tiff' else artifact.detector
    inspection, _ = _inspect_or_read_exact(
        artifact.path, group,
        axes=artifact.axes,
        stored_axes=artifact.stored_axes,
        shape=artifact.shape,
        stored_shape=artifact.stored_shape,
        label=f'alignment artifact {artifact.detector!r}',
        materialize=False,
    )
    return inspection


def read_alignment_artifact(artifact: AlignmentArtifact) -> np.ndarray:
    """Read the singular alignment image without recursive discovery."""
    group = None if _container_kind(artifact.path) == 'tiff' else artifact.detector
    _, array = _inspect_or_read_exact(
        artifact.path, group,
        axes=artifact.axes,
        stored_axes=artifact.stored_axes,
        shape=artifact.shape,
        stored_shape=artifact.stored_shape,
        label=f'alignment artifact {artifact.detector!r}',
        materialize=True,
    )
    assert array is not None
    return array


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


def detectors_in(payload: Dict | TilingDatasetIndex) -> List[str]:
    """Every saved detector, using the same parser as ``inspect_dataset``."""
    if isinstance(payload, TilingDatasetIndex):
        return list(payload.detectors)
    index = _index_manifest(
        _mapping(payload, 'tiling manifest'),
        Path.cwd() / MANIFEST_NAME,
    )
    return list(index.detectors)


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


def _check_cancelled(check_cancelled: Optional[Callable[[], None]]) -> None:
    if check_cancelled is not None:
        check_cancelled()


def _phase_progress(
    callback: Optional[Callable[[str, int, int, str], None]],
    phase: str,
    completed: int,
    total: int,
    message: str,
) -> None:
    if callback is not None:
        callback(phase, int(completed), max(1, int(total)), str(message))


def _load_v2_dataset(
    payload: Dict,
    index: TilingDatasetIndex,
    *,
    report: Callable[[str], None],
    prefer_stage_positions: bool,
    detector: Optional[str],
    check_cancelled: Optional[Callable[[], None]] = None,
    phase_progress: Optional[Callable[[str, int, int, str], None]] = None,
) -> MosaicDataset:
    """Compatibility ``MosaicDataset`` built through strict current readers."""
    dataset = MosaicDataset(
        pixel_size_um=index.pixel_size_yx_um,
        z_step_um=index.z_step_um,
        tile_step_um=float(payload.get('tile_step_um', 0.0) or 0.0),
        source=index.manifest,
        metadata=payload,
    )
    raw_entries = list(payload.get('tiles') or [])
    stage_positions = (
        _positions_from_stage(payload, raw_entries, index.pixel_size_yx_um)
        if prefer_stage_positions else None
    )
    if stage_positions is not None:
        report('  laying out from the commanded stage positions')
    elif prefer_stage_positions:
        report('  no usable stage positions; using the saved pixel positions')
    if detector:
        report(f'  reading the {detector} payloads')

    skipped: List[str] = []
    for ordinal, tile in enumerate(index.tiles):
        _check_cancelled(check_cancelled)
        position = (
            stage_positions[tile.tile_id]
            if stage_positions is not None else tile.saved_position_yx
        )
        if detector is None:
            artifact = tile.alignment
            try:
                data = read_alignment_artifact(artifact)
            except FileNotFoundError:
                skipped.append(str(artifact.path.name))
                continue
            name = artifact.path.name
            axes = artifact.axes
        else:
            ref = tile.payloads.get(detector)
            if ref is None:
                skipped.append(f'tile {tile.tile_id} ({detector}) absent')
                continue
            if not ref.complete:
                skipped.append(f'tile {tile.tile_id} ({detector}) incomplete')
                continue
            # A complete version-2 locator is a contract. Missing paths/groups
            # and descriptor contradictions deliberately propagate.
            data = read_manifest_payload(ref)
            name = ref.path.name + (f':{ref.group}' if ref.group else '')
            axes = ref.axes

        if ordinal and ordinal % 25 == 0:
            report(f'  read {ordinal}/{len(index.tiles)} tiles')
        dataset.tiles.append(MosaicTile(
            name=name,
            data=data,
            position=position,
            grid=tile.grid,
            axes=axes,
            tile_id=tile.tile_id,
        ))
        _phase_progress(
            phase_progress,
            'align',
            ordinal + 1,
            len(index.tiles),
            f'Read alignment artifact {ordinal + 1}/{len(index.tiles)}',
        )

    if skipped:
        logger.warning(
            '%d tile artifact(s) were absent or declared incomplete: %s',
            len(skipped), ', '.join(skipped[:5]),
        )
    if not dataset.tiles:
        chosen = f' for detector {detector!r}' if detector else ''
        raise ValueError(f'No readable tiles found{chosen} for {index.manifest}')

    first = dataset.tiles[0]
    report(
        f'Read {len(dataset.tiles)} tiles of {first.plane_shape[0]}x'
        f'{first.plane_shape[1]} px'
        + (f' x {dataset.depth} planes' if dataset.is_volumetric else '')
    )
    return dataset


def load_dataset(path: Path | str,
                 progress: Optional[Callable[[str], None]] = None,
                 prefer_stage_positions: bool = True,
                 detector: Optional[str] = None,
                 *,
                 check_cancelled: Optional[Callable[[], None]] = None,
                 phase_progress: Optional[
                     Callable[[str, int, int, str], None]
                 ] = None) -> MosaicDataset:
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
    _check_cancelled(check_cancelled)
    manifest_path = find_manifest(path)
    if manifest_path is None:
        raise FileNotFoundError(
            f'No {MANIFEST_NAME} found next to {path}. Open a file from a '
            'tiling dataset folder, or the manifest itself.'
        )

    payload = json.loads(manifest_path.read_text(encoding='utf-8'))
    folder = manifest_path.parent

    if payload.get('format') == 'imswitch-tiling/2':
        index = _index_manifest(payload, manifest_path)
        if index.format == 'imswitch-tiling/2':
            report(f'Reading {len(index.tiles)} tiles from {folder.name}...')
            return _load_v2_dataset(
                payload,
                index,
                report=report,
                prefer_stage_positions=prefer_stage_positions,
                detector=detector,
                check_cancelled=check_cancelled,
                phase_progress=phase_progress,
            )

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
        _check_cancelled(check_cancelled)
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
            tile_id=index,
        ))
        _phase_progress(
            phase_progress,
            'align',
            index + 1,
            len(entries),
            f'Read tile {index + 1}/{len(entries)}',
        )

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


@dataclass(frozen=True)
class LayoutOptions:
    """Only the choices that can change solved tile geometry."""

    stage_positions: bool = True
    refine: bool = True
    max_shift_px: Optional[float] = None


@dataclass(frozen=True)
class MosaicLayout:
    """Detector-independent tile placement, keyed by manifest identity."""

    positions_yx: Mapping[int, Tuple[float, float]]
    source: str
    report: RefinementReport

    def __post_init__(self):
        if self.source not in ('stage', 'saved', 'refined'):
            raise ValueError(f'Unknown mosaic layout source {self.source!r}')
        normalized = {
            int(tile_id): (float(position[0]), float(position[1]))
            for tile_id, position in self.positions_yx.items()
        }
        object.__setattr__(self, 'positions_yx', MappingProxyType(normalized))


@dataclass(frozen=True)
class PayloadSelection:
    """One detector payload and the named-axis reductions requested from it."""

    detector: str
    #: ``None`` preserves every C entry, including a length-one C axis.
    channel: Optional[int] = None
    #: ``"keep"`` preserves Z; ``"max"`` projects Z and no other axis.
    z_projection: str = 'keep'

    def __post_init__(self):
        if not self.detector:
            raise ValueError('Payload selection requires a detector name')
        if self.channel is not None and int(self.channel) < 0:
            raise ValueError('Channel index must be non-negative or None')
        if self.z_projection not in ('keep', 'max'):
            raise ValueError(
                'z_projection must be "keep" or "max", got '
                f'{self.z_projection!r}'
            )


TransformResolver = Callable[[str, str, DetectorTransform], Any]


@dataclass(frozen=True)
class PayloadAssemblyOptions:
    """Assembly choices that do not alter the frozen tile layout."""

    blend: bool = True
    #: Divide every tile by an illumination profile estimated from the run
    #: itself (see :func:`estimate_shading_profile`). Costs one extra read
    #: pass over the payload, so it is opt-in.
    shading_correction: bool = False
    transform_resolver: Optional[TransformResolver] = None
    layout_cache_key: Any = None
    progress: Optional[Callable[[str], None]] = field(
        default=None, compare=False, repr=False
    )
    check_cancelled: Optional[Callable[[], None]] = field(
        default=None, compare=False, repr=False
    )
    phase_progress: Optional[
        Callable[[str, int, int, str], None]
    ] = field(default=None, compare=False, repr=False)
    memory_budget_bytes: Optional[int] = field(
        default=None, compare=False, repr=False
    )
    confirmed_over_budget: bool = field(
        default=False, compare=False, repr=False
    )


@dataclass(frozen=True)
class SkippedPayload:
    tile_id: int
    reason: str


@dataclass(frozen=True)
class PayloadProvenance:
    detector: str
    channel: Optional[int]
    z_projection: str
    skipped: Tuple[SkippedPayload, ...]
    manifest: Path
    layout_cache_key: Any
    refinement_report: RefinementReport
    transform_source: str
    transform: Mapping[str, Any]
    placement_path: str


@dataclass(frozen=True)
class PayloadAssemblyResult:
    """A selected payload mosaic plus its calibrated description."""

    data: np.ndarray
    axes: str
    scales: Tuple[float, ...]
    #: Alignment-grid coordinate of output pixel centre ``[..., 0, 0]``.
    origin_yx: Tuple[int, int]
    provenance: PayloadProvenance


@dataclass(frozen=True)
class PayloadEstimate:
    """Manifest-only output and allocation estimate for one selection."""

    axes: str
    shape: Tuple[int, ...]
    canvas_bytes: int
    weight_bytes: int
    placement_path: str
    tiles: int


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


def estimate_shading_profile(
    planes: Iterable[np.ndarray],
    *,
    smooth_fraction: float = SHADING_SMOOTH_FRACTION,
    progress: Optional[Callable[[str], None]] = None,
) -> Optional[np.ndarray]:
    """Estimate the illumination profile every tile shares, or None.

    Shading is fixed to the *detector*: whatever the stage does, the same
    corner is dim in every tile. The sample is not -- a spiral visits a
    different piece of specimen at each stop. So averaging many tiles in
    detector coordinates lets the specimen cancel while the illumination
    envelope survives, and heavy smoothing removes what structure is left. An
    arbitrary shape falls out of this, which is what makes it right for
    one-sided vignetting: nothing here assumes the profile is centred or
    radial.

    Each tile is divided by its own mean before it is accumulated, so tiles
    contribute their *shape* and not their brightness. Without that, one
    bright field would set the profile for the whole run.

    Returns None rather than a bad correction when the estimate cannot be
    trusted: too few tiles for the sample to average out, tiles that are not
    all the same shape, or a result outside :data:`SHADING_CLIP`, which means
    the specimen dominated rather than cancelled.
    """
    from scipy.ndimage import gaussian_filter

    report = _reporter(progress)
    total: Optional[np.ndarray] = None
    shape: Optional[Tuple[int, int]] = None
    count = 0

    for plane in planes:
        arr = np.asarray(plane, dtype=np.float32)
        if arr.ndim > 2:
            # Leading axes are Z planes or channels of the same field; they see
            # the same illumination, so collapse them rather than weighting the
            # estimate by how many a tile happens to have.
            arr = arr.reshape(-1, *arr.shape[-2:]).mean(axis=0)
        if arr.ndim != 2:
            report('Shading correction skipped: tiles are not 2-D fields.')
            return None
        if shape is None:
            shape = arr.shape
            total = np.zeros(shape, dtype=np.float64)
        elif arr.shape != shape:
            report(
                'Shading correction skipped: the tiles are not all the same '
                f'shape ({shape} vs {arr.shape}), so there is no common '
                'detector frame to estimate in.'
            )
            return None
        level = float(arr.mean())
        if not np.isfinite(level) or level <= 0.0:
            continue
        total += arr / level
        count += 1

    if total is None or shape is None or count < SHADING_MIN_TILES:
        report(
            f'Shading correction skipped: {count} usable tile(s), and below '
            f'{SHADING_MIN_TILES} the sample does not average out -- the '
            'estimate would be the specimen, not the illumination.'
        )
        return None

    profile = (total / count).astype(np.float32)
    sigma = max(4.0, min(shape) * float(smooth_fraction))
    # ``nearest`` matters more than the sigma. The default zero-padding would
    # pull the estimate down at the borders, i.e. invent vignetting exactly
    # where real vignetting lives, and the correction would then brighten the
    # edges whether or not anything was wrong with them.
    profile = gaussian_filter(profile, sigma, mode='nearest')

    median = float(np.median(profile))
    if not np.isfinite(median) or median <= 0.0:
        report('Shading correction skipped: the estimated profile is degenerate.')
        return None
    profile /= median

    low, high = float(profile.min()), float(profile.max())
    if low < SHADING_CLIP[0] or high > SHADING_CLIP[1]:
        report(
            f'Shading correction skipped: the estimated profile spans '
            f'{low:.2f}-{high:.2f} of the median, which is a specimen '
            'gradient rather than an illumination envelope.'
        )
        return None

    report(
        f'Shading profile estimated from {count} tiles: {low:.2f}-{high:.2f} '
        f'of the median (sigma {sigma:.0f} px).'
    )
    if count < SHADING_TRUSTWORTHY_TILES:
        report(
            f'  only {count} tiles contributed, so some of the specimen may '
            'be baked into the correction.'
        )
    return profile


def _apply_shading(data: np.ndarray, profile: Optional[np.ndarray]):
    """Divide out an estimated profile, broadcasting over any leading axes."""
    if profile is None or data.shape[-2:] != profile.shape:
        return data
    return data / profile


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
                 progress: Optional[Callable[[str], None]] = None,
                 *,
                 check_cancelled: Optional[Callable[[], None]] = None,
                 phase_progress: Optional[
                     Callable[[str, int, int, str], None]
                 ] = None,
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
            _check_cancelled(check_cancelled)
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
        _check_cancelled(check_cancelled)
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
        _phase_progress(
            phase_progress,
            'align',
            done,
            len(candidates),
            f'Correlated {done}/{len(candidates)} overlapping pairs',
        )
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
                  progress: Optional[Callable[[str], None]] = None,
                  *,
                  check_cancelled: Optional[Callable[[], None]] = None,
                  phase_progress: Optional[
                      Callable[[str, int, int, str], None]
                  ] = None,
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
    _check_cancelled(check_cancelled)
    report.links = _build_links(
        dataset,
        max_shift_px,
        progress,
        check_cancelled=check_cancelled,
        phase_progress=phase_progress,
    )
    if not report.links:
        emit('No tile pair could be correlated; keeping the stage positions.')
        return report

    _check_cancelled(check_cancelled)
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


def _nominal_index_positions(
    index: TilingDatasetIndex, prefer_stage_positions: bool
) -> Tuple[Dict[int, Tuple[float, float]], str]:
    """Position every manifest identity before any image is opened."""
    saved = {
        tile.tile_id: tuple(tile.saved_position_yx)
        for tile in index.tiles
    }
    if not prefer_stage_positions:
        return saved, 'saved'

    stage = [tuple(tile.stage_um) for tile in index.tiles]
    finite_stage = all(
        np.isfinite(value) for point in stage for value in point
    )
    usable = (
        finite_stage
        and len({(round(x, 6), round(y, 6)) for x, y in stage}) >= 2
        and all(float(value) > 0 for value in index.pixel_size_yx_um)
        and all(np.isfinite(value) for value in index.pixel_size_yx_um)
    )
    if not usable:
        return saved, 'saved'

    flip_x, flip_y, swap_axes = index.orientation
    pixel_y, pixel_x = index.pixel_size_yx_um
    origin_x, origin_y = stage[0]
    positions: Dict[int, Tuple[float, float]] = {}
    for tile, (stage_x, stage_y) in zip(index.tiles, stage):
        delta_x, delta_y = stage_x - origin_x, stage_y - origin_y
        if swap_axes:
            delta_x, delta_y = delta_y, delta_x
        column = -delta_x if flip_x else delta_x
        row = -delta_y if flip_y else delta_y
        positions[tile.tile_id] = (
            float(row / pixel_y), float(column / pixel_x)
        )
    return positions, 'stage'


def solve_layout(
    index: TilingDatasetIndex,
    options: Optional[LayoutOptions] = None,
    *,
    progress: Optional[Callable[[str], None]] = None,
    check_cancelled: Optional[Callable[[], None]] = None,
    phase_progress: Optional[
        Callable[[str, int, int, str], None]
    ] = None,
) -> MosaicLayout:
    """Solve one reusable layout from the run's alignment artifacts only.

    Payload arrays are never opened here. Alignment failures do not renumber
    the remaining tiles: every result is joined back to the manifest by
    ``tile_id``, and an unreadable alignment artifact keeps its nominal
    position. If no alignment image can be read while refinement is requested,
    the caller gets an actionable error instead of a layout falsely labelled
    as refined.
    """
    options = options or LayoutOptions()
    emit = _reporter(progress)
    nominal, nominal_source = _nominal_index_positions(
        index, options.stage_positions
    )
    empty_report = RefinementReport(tiles=len(index.tiles))
    if not options.refine:
        return MosaicLayout(nominal, nominal_source, empty_report)

    alignment_dataset = MosaicDataset(
        pixel_size_um=index.pixel_size_yx_um,
        z_step_um=index.z_step_um,
        source=index.manifest,
    )
    unreadable: List[Tuple[int, Path, Exception]] = []
    for done, tile in enumerate(index.tiles, start=1):
        _check_cancelled(check_cancelled)
        try:
            data = read_alignment_artifact(tile.alignment)
        except Exception as exc:
            unreadable.append((tile.tile_id, tile.alignment.path, exc))
            continue
        alignment_dataset.tiles.append(MosaicTile(
            name=tile.alignment.path.name,
            data=data,
            position=nominal[tile.tile_id],
            grid=tile.grid,
            axes=tile.alignment.axes,
            tile_id=tile.tile_id,
        ))
        _phase_progress(
            phase_progress,
            'align',
            done,
            len(index.tiles),
            f'Read alignment artifact {done}/{len(index.tiles)}',
        )

    if unreadable:
        emit(
            f'Could not read {len(unreadable)}/{len(index.tiles)} alignment '
            'artifact(s); those tile identities keep their nominal positions.'
        )
        for tile_id, path, error in unreadable[:5]:
            logger.warning(
                'Alignment artifact for tile %d is unreadable (%s): %s',
                tile_id, path, error,
            )
    if not alignment_dataset.tiles:
        raise ValueError(
            'Refinement was requested, but no alignment image is readable. '
            'Disable refinement to assemble from the nominal stage/saved '
            'layout, or restore the alignment artifacts.'
        )

    report = refine_layout(
        alignment_dataset,
        max_shift_px=options.max_shift_px,
        progress=progress,
        check_cancelled=check_cancelled,
        phase_progress=phase_progress,
    )
    positions = dict(nominal)
    for tile in alignment_dataset.tiles:
        positions[int(tile.tile_id)] = tuple(tile.position)

    # Report the run, not merely the readable subset. Missing alignment images
    # are singleton components whose positions stayed nominal.
    missing_count = len(index.tiles) - len(alignment_dataset.tiles)
    report.tiles = len(index.tiles)
    if missing_count:
        report.components = sorted(
            [*report.components, *([1] * missing_count)], reverse=True
        )
    source = 'refined' if report.accepted else nominal_source

    # Make the resource boundary explicit: the returned value contains no
    # arrays, and the alignment projections become collectible immediately.
    del alignment_dataset
    return MosaicLayout(positions, source, report)


def apply_layout(dataset: MosaicDataset, layout: MosaicLayout) -> MosaicDataset:
    """Apply a frozen layout by tile identity, never by list position."""
    for tile in dataset.tiles:
        if tile.tile_id is None:
            raise ValueError(
                f'Tile {tile.name!r} has no manifest identity; a solved '
                'version-2 layout cannot be applied safely'
            )
        try:
            tile.position = layout.positions_yx[tile.tile_id]
        except KeyError as exc:
            raise ValueError(
                f'Solved layout has no position for tile {tile.tile_id}'
            ) from exc
    return dataset


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


@dataclass(frozen=True)
class _PlacedPayload:
    tile_id: int
    ref: ManifestPayloadRef
    axes: str
    shape: Tuple[int, ...]
    matrix: Tuple[Tuple[float, float, float], ...]
    #: Pixel-centre bounds as ``(row0, col0, row1, col1)``; end exclusive.
    bounds: Tuple[int, int, int, int]


def _selected_axes_shape(
    ref: ManifestPayloadRef, selection: PayloadSelection
) -> Tuple[str, Tuple[int, ...]]:
    axes = list(ref.axes)
    shape = list(ref.shape)
    if selection.channel is not None:
        if 'C' not in axes:
            raise ValueError(
                f'Detector {selection.detector!r} has axes {ref.axes!r}; '
                f'channel {selection.channel} cannot be selected without C'
            )
        position = axes.index('C')
        channel = int(selection.channel)
        if channel >= shape[position]:
            raise ValueError(
                f'Detector {selection.detector!r} channel {channel} is out of '
                f'range for tile payload shape {ref.shape} ({ref.axes})'
            )
        axes.pop(position)
        shape.pop(position)

    if selection.z_projection == 'max':
        if 'Z' not in axes:
            raise ValueError(
                f'Detector {selection.detector!r} has axes '
                f'{"".join(axes)!r}; Z projection requires a Z axis'
            )
        position = axes.index('Z')
        axes.pop(position)
        shape.pop(position)
    return ''.join(axes), tuple(int(size) for size in shape)


def _select_payload_array(
    array: np.ndarray, axes: str, selection: PayloadSelection
) -> np.ndarray:
    selected = np.asarray(array)
    current_axes = list(axes)
    if selection.channel is not None:
        position = current_axes.index('C')
        selected = np.take(selected, int(selection.channel), axis=position)
        current_axes.pop(position)
    if selection.z_projection == 'max':
        position = current_axes.index('Z')
        selected = np.max(selected, axis=position)
        current_axes.pop(position)
    return np.asarray(selected)


def _parse_resolved_transform(
    value: Any, *, allow_reference: bool, label: str
) -> DetectorTransform:
    if isinstance(value, DetectorTransform):
        value = value.as_manifest()
    elif not isinstance(value, (str, Mapping)):
        try:
            matrix = np.asarray(value, dtype=np.float64)
        except (TypeError, ValueError):
            matrix = np.empty((0, 0))
        if matrix.shape == (3, 3):
            value = {'kind': 'affine', 'matrix': matrix.tolist()}
    return parse_detector_transform(
        value, allow_reference=allow_reference, label=label
    )


def _payload_transform(
    index: TilingDatasetIndex,
    selection: PayloadSelection,
    refs: Sequence[ManifestPayloadRef],
    resolver: Optional[TransformResolver],
) -> Tuple[DetectorTransform, str]:
    manifest_transform = refs[0].transform_to_alignment
    if resolver is None:
        for ref in refs[1:]:
            if ref.transform_to_alignment != manifest_transform:
                raise ValueError(
                    f'Detector {selection.detector!r} does not have one '
                    'consistent transform across the run'
                )
        # An undeclared transform still resolves to identity, because a payload
        # that cannot be placed relative to the alignment detector is more
        # useful placed naively than not returned at all. What must not happen
        # is passing that off as a statement about the rig, so it is reported
        # as assumed rather than read from the manifest.
        source = 'manifest' if manifest_transform.is_declared else 'assumed'
        return manifest_transform, source

    resolved = resolver(
        selection.detector, index.alignment_detector, manifest_transform
    )
    return _parse_resolved_transform(
        resolved,
        allow_reference=selection.detector == index.alignment_detector,
        label=f'override transform for detector {selection.detector!r}',
    ), 'override'


def _compose_payload_matrix(
    position_yx: Tuple[float, float], transform: DetectorTransform
) -> np.ndarray:
    translation = np.eye(3, dtype=np.float64)
    translation[:2, 2] = np.asarray(position_yx, dtype=np.float64)
    return translation @ transform.as_array()


def _affine_footprint_bounds(
    matrix: np.ndarray, plane_shape: Sequence[int]
) -> Tuple[int, int, int, int]:
    height, width = (int(plane_shape[0]), int(plane_shape[1]))
    corners = np.asarray([
        (-0.5, -0.5, 1.0),
        (-0.5, width - 0.5, 1.0),
        (height - 0.5, -0.5, 1.0),
        (height - 0.5, width - 0.5, 1.0),
    ], dtype=np.float64).T
    transformed = matrix @ corners
    row0 = int(np.floor(float(np.min(transformed[0])) + 0.5))
    col0 = int(np.floor(float(np.min(transformed[1])) + 0.5))
    row1 = int(np.ceil(float(np.max(transformed[0])) + 0.5))
    col1 = int(np.ceil(float(np.max(transformed[1])) + 0.5))
    return row0, col0, max(row0 + 1, row1), max(col0 + 1, col1)


def estimate_payload_selection(
    index: TilingDatasetIndex,
    selection: PayloadSelection,
    *,
    stage_positions: bool = True,
    transform_resolver: Optional[TransformResolver] = None,
) -> PayloadEstimate:
    """Estimate a payload mosaic solely from manifest descriptors.

    This deliberately does not inspect array headers, load pixels, correlate
    alignment images, or solve a refined layout. Reconstruction repeats exact
    header validation and allocation accounting immediately before assembly.
    """
    positions, _source = _nominal_index_positions(index, stage_positions)
    chosen = [
        (tile, ref)
        for tile in index.tiles
        if (ref := tile.payloads.get(selection.detector)) is not None
        and ref.complete
        and tile.tile_id in positions
    ]
    if not chosen:
        raise ValueError(
            f'No complete payload is declared for {selection.detector!r}'
        )
    axes_set = {ref.axes for _tile, ref in chosen}
    if len(axes_set) != 1:
        raise ValueError(
            f'Detector {selection.detector!r} has inconsistent axes: '
            f'{sorted(axes_set)}'
        )

    transform, _transform_source = _payload_transform(
        index,
        selection,
        [ref for _tile, ref in chosen],
        transform_resolver,
    )
    selected = [
        (tile, *_selected_axes_shape(ref, selection))
        for tile, ref in chosen
    ]
    axes = selected[0][1]
    matrices = [
        _compose_payload_matrix(positions[tile.tile_id], transform)
        for tile, _selected_axes, _shape in selected
    ]
    bounds = [
        _affine_footprint_bounds(matrix, shape[-2:])
        for matrix, (_tile, _selected_axes, shape) in zip(matrices, selected)
    ]
    leading_shapes = [shape[:-2] for _tile, _axes, shape in selected]
    leading = tuple(
        max(shape[axis] for shape in leading_shapes)
        for axis in range(len(leading_shapes[0]))
    )
    spatial = (
        max(item[2] for item in bounds) - min(item[0] for item in bounds),
        max(item[3] for item in bounds) - min(item[1] for item in bounds),
    )
    output_shape = leading + spatial
    identity_path = bool(
        transform.is_identity
        and all(
            abs(float(value) - round(float(value))) <= 1e-9
            for matrix in matrices
            for value in matrix[:2, 2]
        )
    )
    uniform = len(set(leading_shapes)) <= 1
    weight_shape = spatial if uniform else output_shape
    weight_dtype = np.uint16 if identity_path else np.float32
    return PayloadEstimate(
        axes=axes,
        shape=output_shape,
        canvas_bytes=np.dtype(np.float32).itemsize * int(np.prod(output_shape)),
        weight_bytes=np.dtype(weight_dtype).itemsize * int(np.prod(weight_shape)),
        placement_path='identity-integer' if identity_path else 'affine',
        tiles=len(chosen),
    )


def _payload_scales(index: TilingDatasetIndex, axes: str) -> Tuple[float, ...]:
    pixel_y, pixel_x = index.pixel_size_yx_um
    scales = []
    for axis in axes:
        if axis == 'Y':
            scales.append(float(pixel_y))
        elif axis == 'X':
            scales.append(float(pixel_x))
        elif axis == 'Z':
            scales.append(float(index.z_step_um or 1.0))
        else:
            scales.append(1.0)
    return tuple(scales)


def _overlap_taper(shape: Sequence[int]) -> np.ndarray:
    """Weights for the current mean-overlap policy.

    Mean blending is the established offline behavior, so its taper is
    deliberately uniform. Keeping it explicit makes the affine validity mask
    part of the weight calculation (rather than merely zeroing intensity) and
    leaves one seam for a future feathered policy without changing placement.
    """
    return np.ones(tuple(int(size) for size in shape), dtype=np.float32)


def assemble_payload(
    index: TilingDatasetIndex,
    layout: MosaicLayout,
    selection: PayloadSelection,
    options: Optional[PayloadAssemblyOptions] = None,
) -> PayloadAssemblyResult:
    """Inspect, select and assemble one detector without retaining its tiles.

    Version-2 locator contradictions are checked before allocating a mosaic.
    Only an absent locator or one explicitly marked incomplete is skipped; a
    complete locator whose path, group or descriptor is wrong fails loudly.
    Geometry is joined by ``tile_id`` and comes exclusively from ``layout``.
    """
    options = options or PayloadAssemblyOptions()
    emit = _reporter(options.progress)
    skipped: List[SkippedPayload] = []
    chosen: List[Tuple[IndexedTile, ManifestPayloadRef]] = []
    header_total = sum(
        bool(ref is not None and ref.complete)
        for tile in index.tiles
        for ref in (tile.payloads.get(selection.detector),)
    )
    header_done = 0
    for tile in index.tiles:
        _check_cancelled(options.check_cancelled)
        ref = tile.payloads.get(selection.detector)
        if ref is None:
            skipped.append(SkippedPayload(tile.tile_id, 'absent'))
            continue
        if not ref.complete:
            skipped.append(SkippedPayload(tile.tile_id, 'incomplete'))
            continue
        if tile.tile_id not in layout.positions_yx:
            raise ValueError(
                f'Solved layout has no position for tile {tile.tile_id}'
            )
        # Header-only exact opening: every hard contradiction fails before the
        # output allocation and before any full payload is materialized.
        inspect_manifest_payload(ref)
        chosen.append((tile, ref))
        header_done += 1
        _phase_progress(
            options.phase_progress,
            'allocate',
            header_done,
            header_total + 1,
            f'Validated payload header {header_done}/{header_total}',
        )

    if not chosen:
        declared = sum(
            selection.detector in tile.payloads for tile in index.tiles
        )
        complete = sum(
            bool(tile.payloads.get(selection.detector)
                 and tile.payloads[selection.detector].complete)
            for tile in index.tiles
        )
        raise ValueError(
            f'No complete payload is available for detector '
            f'{selection.detector!r}: {declared}/{len(index.tiles)} declared, '
            f'{complete}/{len(index.tiles)} complete'
        )

    expected_axes = chosen[0][1].axes
    for tile, ref in chosen[1:]:
        if ref.axes != expected_axes:
            raise ValueError(
                f'Detector {selection.detector!r} axes differ between tiles: '
                f'tile {chosen[0][0].tile_id} has {expected_axes!r}, tile '
                f'{tile.tile_id} has {ref.axes!r}'
            )

    transform, transform_source = _payload_transform(
        index,
        selection,
        [ref for _tile, ref in chosen],
        options.transform_resolver,
    )
    placed: List[_PlacedPayload] = []
    selected_axes: Optional[str] = None
    for tile, ref in chosen:
        axes, shape = _selected_axes_shape(ref, selection)
        if selected_axes is None:
            selected_axes = axes
        elif axes != selected_axes:
            # Normally guaranteed by the original-axis check above, but keep
            # the post-selection contract explicit before allocation.
            raise ValueError(
                f'Selected axes differ between payload tiles: '
                f'{selected_axes!r} versus {axes!r}'
            )
        matrix = _compose_payload_matrix(
            layout.positions_yx[tile.tile_id], transform
        )
        bounds = _affine_footprint_bounds(matrix, shape[-2:])
        placed.append(_PlacedPayload(
            tile_id=tile.tile_id,
            ref=ref,
            axes=axes,
            shape=shape,
            matrix=tuple(tuple(float(value) for value in row) for row in matrix),
            bounds=bounds,
        ))

    assert selected_axes is not None
    leading_shapes = [item.shape[:-2] for item in placed]
    leading = tuple(
        max(shape[axis] for shape in leading_shapes)
        for axis in range(len(leading_shapes[0]))
    )
    row0 = min(item.bounds[0] for item in placed)
    col0 = min(item.bounds[1] for item in placed)
    row1 = max(item.bounds[2] for item in placed)
    col1 = max(item.bounds[3] for item in placed)
    height, width = row1 - row0, col1 - col0
    output_shape = leading + (height, width)

    identity_path = bool(
        transform.is_identity
        and all(
            abs(float(value) - round(float(value))) <= 1e-9
            for item in placed
            for value in np.asarray(item.matrix, dtype=np.float64)[:2, 2]
        )
    )
    placement_path = 'identity-integer' if identity_path else 'affine'
    uniform = len(set(leading_shapes)) <= 1
    weight_shape = (height, width) if uniform else output_shape
    weight_dtype = np.uint16 if identity_path else np.float32
    footprint = (
        np.float32().itemsize * int(np.prod(output_shape))
        + np.dtype(weight_dtype).itemsize * int(np.prod(weight_shape))
    )
    canvas_bytes = np.float32().itemsize * int(np.prod(output_shape))
    weight_bytes = np.dtype(weight_dtype).itemsize * int(np.prod(weight_shape))
    budget = options.memory_budget_bytes
    if (
        budget is not None
        and footprint > int(budget)
        and not options.confirmed_over_budget
    ):
        channel_tip = (
            ' Select one channel instead of All.'
            if selection.channel is None and 'C' in selected_axes else ''
        )
        z_tip = (
            ' Max-project Z to reduce the leading volume.'
            if selection.z_projection == 'keep' and 'Z' in selected_axes else ''
        )
        raise MemoryError(
            f'Payload mosaic {selection.detector!r} would allocate output shape '
            f'{output_shape}: canvas {canvas_bytes} bytes plus weights '
            f'{weight_bytes} bytes = {footprint} bytes, above the '
            f'{int(budget)}-byte reconstruction budget.'
            f'{channel_tip}{z_tip}'
        )
    emit(
        f'Assembling {len(placed)} {selection.detector} payload(s) into a '
        f'{"x".join(str(size) for size in output_shape)} mosaic via '
        f'{placement_path} placement ({footprint / 1e9:.2f} GB)...'
    )
    _check_cancelled(options.check_cancelled)
    canvas_sum = np.zeros(output_shape, dtype=np.float32)
    canvas_weight = np.zeros(weight_shape, dtype=weight_dtype)
    _phase_progress(
        options.phase_progress,
        'allocate',
        header_total + 1,
        header_total + 1,
        f'Allocated canvas and weights for output shape {output_shape}',
    )

    shading = None
    if options.shading_correction:
        emit(
            'Estimating the shading profile — one extra read pass over the '
            f'{len(placed)} payload tiles...'
        )

        def _estimation_planes():
            for item in placed:
                _check_cancelled(options.check_cancelled)
                yield _select_payload_array(
                    read_manifest_payload(item.ref), item.ref.axes, selection
                )

        shading = estimate_shading_profile(
            _estimation_planes(), progress=options.progress
        )
        _check_cancelled(options.check_cancelled)

    if identity_path:
        for done, item in enumerate(placed, start=1):
            _check_cancelled(options.check_cancelled)
            data = _select_payload_array(
                read_manifest_payload(item.ref), item.ref.axes, selection
            )
            _check_cancelled(options.check_cancelled)
            if tuple(data.shape) != item.shape:
                raise LocatorReadError(
                    f'payload {selection.detector!r}: selected shape changed '
                    f'between inspection and read ({item.shape} -> '
                    f'{tuple(data.shape)})'
                )
            source = _apply_shading(
                np.asarray(data, dtype=np.float32), shading
            )
            matrix = np.asarray(item.matrix, dtype=np.float64)
            top = int(round(float(matrix[0, 2]))) - row0
            left = int(round(float(matrix[1, 2]))) - col0
            tile_h, tile_w = source.shape[-2:]
            covered = tuple(slice(0, size) for size in source.shape[:-2])
            window = (
                slice(top, top + tile_h), slice(left, left + tile_w)
            )
            target = canvas_sum[covered + window]
            weight_view = (
                canvas_weight[window]
                if uniform else canvas_weight[covered + window]
            )
            if options.blend:
                target += source
                weight_view += 1
            else:
                target[...] = source
                weight_view[...] = 1
            del data, source
            _phase_progress(
                options.phase_progress,
                'assemble',
                done,
                len(placed),
                f'Assembled payload tile {done}/{len(placed)}',
            )
            if done % 25 == 0:
                emit(f'  assembled {done}/{len(placed)} payload tiles')
    else:
        from scipy.ndimage import affine_transform

        slab_total = sum(
            max(1, int(np.prod(item.shape[:-2]))) for item in placed
        )
        slab_done = 0
        for done, item in enumerate(placed, start=1):
            _check_cancelled(options.check_cancelled)
            data = _select_payload_array(
                read_manifest_payload(item.ref), item.ref.axes, selection
            )
            _check_cancelled(options.check_cancelled)
            if tuple(data.shape) != item.shape:
                raise LocatorReadError(
                    f'payload {selection.detector!r}: selected shape changed '
                    f'between inspection and read ({item.shape} -> '
                    f'{tuple(data.shape)})'
                )
            source = _apply_shading(
                np.asarray(data, dtype=np.float32), shading
            )
            matrix = np.asarray(item.matrix, dtype=np.float64)
            inverse = np.linalg.inv(matrix)
            tile_row0, tile_col0, tile_row1, tile_col1 = item.bounds
            tile_shape = (tile_row1 - tile_row0, tile_col1 - tile_col0)
            local_matrix = inverse[:2, :2]
            local_offset = (
                local_matrix @ np.asarray((tile_row0, tile_col0))
                + inverse[:2, 2]
            )
            validity = affine_transform(
                np.ones(source.shape[-2:], dtype=np.float32),
                local_matrix,
                offset=local_offset,
                output_shape=tile_shape,
                order=0,
                mode='constant',
                cval=0.0,
                prefilter=False,
            )
            valid = validity > 0.5
            tile_weight = validity * _overlap_taper(tile_shape)
            top, left = tile_row0 - row0, tile_col0 - col0
            window = (
                slice(top, top + tile_shape[0]),
                slice(left, left + tile_shape[1]),
            )
            leading_indices = (
                np.ndindex(source.shape[:-2])
                if source.ndim > 2 else iter(((),))
            )
            for leading_index in leading_indices:
                _check_cancelled(options.check_cancelled)
                warped = affine_transform(
                    source[leading_index],
                    local_matrix,
                    offset=local_offset,
                    output_shape=tile_shape,
                    order=1,
                    mode='constant',
                    cval=0.0,
                    prefilter=False,
                )
                warped *= validity
                target = canvas_sum[leading_index + window]
                weight_view = (
                    canvas_weight[window]
                    if uniform else canvas_weight[leading_index + window]
                )
                if options.blend:
                    target += warped * tile_weight
                    if not uniform:
                        weight_view += tile_weight
                else:
                    np.copyto(target, warped, where=valid)
                    if not uniform:
                        weight_view[valid] = 1.0
                slab_done += 1
                _phase_progress(
                    options.phase_progress,
                    'assemble',
                    slab_done,
                    slab_total,
                    f'Assembled affine slab {slab_done}/{slab_total}',
                )
            if uniform:
                if options.blend:
                    canvas_weight[window] += tile_weight
                else:
                    canvas_weight[window][valid] = 1.0
            del data, source, validity, valid, tile_weight
            if done % 25 == 0:
                emit(f'  assembled {done}/{len(placed)} payload tiles')

    _check_cancelled(options.check_cancelled)
    np.maximum(canvas_weight, 1, out=canvas_weight)
    divisor = (
        canvas_weight[(np.newaxis,) * len(leading) + (Ellipsis,)]
        if uniform else canvas_weight
    )
    np.divide(canvas_sum, divisor, out=canvas_sum)
    emit('Payload mosaic assembled.')
    provenance = PayloadProvenance(
        detector=selection.detector,
        channel=selection.channel,
        z_projection=selection.z_projection,
        skipped=tuple(skipped),
        manifest=index.manifest,
        layout_cache_key=options.layout_cache_key,
        refinement_report=layout.report,
        transform_source=transform_source,
        transform=MappingProxyType(transform.as_manifest()),
        placement_path=placement_path,
    )
    return PayloadAssemblyResult(
        data=canvas_sum,
        axes=selected_axes,
        scales=_payload_scales(index, selected_axes),
        origin_yx=(row0, col0),
        provenance=provenance,
    )


def assemble(
    dataset: MosaicDataset,
    blend: bool = True,
    progress: Optional[Callable[[str], None]] = None,
    *,
    shading_correction: bool = False,
    check_cancelled: Optional[Callable[[], None]] = None,
    phase_progress: Optional[Callable[[str, int, int, str], None]] = None,
    memory_budget_bytes: Optional[int] = None,
    confirmed_over_budget: bool = False,
) -> np.ndarray:
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
    if (
        memory_budget_bytes is not None
        and footprint > int(memory_budget_bytes)
        and not confirmed_over_budget
    ):
        canvas_bytes = np.float32().itemsize * int(np.prod(shape))
        weight_bytes = np.uint16().itemsize * int(np.prod(weight_shape))
        raise MemoryError(
            f'Mosaic would allocate output shape {shape}: canvas '
            f'{canvas_bytes} bytes plus weights {weight_bytes} bytes = '
            f'{footprint} bytes, above the {int(memory_budget_bytes)}-byte '
            'reconstruction budget. Project leading axes or choose a smaller '
            'payload selection.'
        )
    report(f'Assembling {len(dataset.tiles)} tiles into a '
           f'{"x".join(str(size) for size in shape)} mosaic '
           f'({footprint / 1e9:.2f} GB)...')

    _check_cancelled(check_cancelled)
    shading = (
        estimate_shading_profile(
            (tile.data for tile in dataset.tiles), progress=progress
        )
        if shading_correction else None
    )
    _check_cancelled(check_cancelled)
    canvas_sum = np.zeros(shape, dtype=np.float32)
    canvas_weight = np.zeros(weight_shape, dtype=np.uint16)
    _phase_progress(
        phase_progress,
        'allocate',
        1,
        1,
        f'Allocated canvas and weights for output shape {shape}',
    )

    for done, tile in enumerate(dataset.tiles, start=1):
        _check_cancelled(check_cancelled)
        top = int(round(tile.position[0])) - row0
        left = int(round(tile.position[1])) - col0
        tile_h, tile_w = tile.plane_shape
        tile_h = min(tile_h, height - top)
        tile_w = min(tile_w, width - left)
        if tile_h <= 0 or tile_w <= 0:
            continue

        # Corrected before cropping: the profile is in detector coordinates,
        # so it has to meet the tile at its own full extent, not at whatever
        # part of it happens to fit inside the canvas.
        data = _apply_shading(np.asarray(tile.data, dtype=np.float32), shading)

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
        _phase_progress(
            phase_progress,
            'assemble',
            done,
            len(dataset.tiles),
            f'Assembled tile {done}/{len(dataset.tiles)}',
        )

    # Divide in place. Allocating a separate quotient and a separate
    # divide-safe copy of the weights costs two more arrays the size of the
    # mosaic, which on a large run is gigabytes for nothing: the weights are
    # counts, so clamping them to a minimum of one both avoids the zero divide
    # and leaves uncovered pixels at the zero they already hold.
    _check_cancelled(check_cancelled)
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
