"""A timelapse of tilings: finding it on disk, and reconstructing it as one.

The Timelapse panel's "tiling" kind writes one folder per run::

    timelapse_<YYYYMMDD_HHMMSS>/
        timelapse.json          kind, timepoints, interval_s, started_utc,
                                tiling_settings, points[] (one record per
                                timepoint run so far)
        t0/  t1/  t2/ ...       one tiling dataset each (tiles.json,
                                TileConfiguration.txt, tile files, mosaic);
                                zero-padded to ``len(str(timepoints))`` digits

The manifest is rewritten after every timepoint, so an interrupted run lists
fewer ``points`` than ``timepoints``; the rest are *missing*. A point whose
tiling was stopped or failed is *incomplete*.

This module is Qt-free and knows nothing about reconstructors:

* **discovery** -- :func:`find_timelapse_manifest`, :func:`read_timelapse`,
  :func:`compatibility` (do the grids of the timepoints agree?);
* **joint layout** -- :func:`solve_shared_layout`: one tile layout for every
  timepoint, from correlation surfaces accumulated over time;
* **drift** -- :func:`estimate_drift`: one rigid XY shift per timepoint;
* **common canvas** -- :func:`common_canvas`: one origin and shape that holds
  every timepoint, so nothing has to be cropped or shifted afterwards.

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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

TIMELAPSE_MANIFEST_NAME = 'timelapse.json'
TILING_MANIFEST_NAME = 'tiles.json'
KIND_TILING = 'tiling'

STATE_COMPLETE = 'complete'
STATE_INCOMPLETE = 'incomplete'
STATE_MISSING = 'missing'

_POINT_FOLDER = re.compile(r't\d+')


# ----------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class TimepointRecord:
    """One planned timepoint of a timelapse of tilings."""

    index: int
    folder: Path
    state: str
    reason: str = ''
    #: ``<folder>/tiles.json`` when it exists, else ``None``.
    manifest: Optional[Path] = None
    #: Seconds since the run started (the recorded start of this timepoint).
    time_s: Optional[float] = None
    tiles_acquired: Optional[int] = None


@dataclass(frozen=True)
class TimelapseTiling:
    """What ``timelapse.json`` says, joined with what is on disk."""

    manifest: Path
    root: Path
    kind: str
    planned: int
    interval_s: Optional[float]
    started_utc: Optional[str]
    tiling_settings: Mapping[str, Any]
    points: Tuple[TimepointRecord, ...]

    def counts(self) -> Dict[str, int]:
        out = {STATE_COMPLETE: 0, STATE_INCOMPLETE: 0, STATE_MISSING: 0}
        for point in self.points:
            out[point.state] += 1
        return out

    def describe(self) -> str:
        counts = self.counts()
        parts = [f'{self.planned} timepoint(s): {counts[STATE_COMPLETE]} complete']
        if counts[STATE_INCOMPLETE]:
            parts.append(f'{counts[STATE_INCOMPLETE]} incomplete')
        if counts[STATE_MISSING]:
            parts.append(f'{counts[STATE_MISSING]} missing')
        return ', '.join(parts)


def _folder_name(index: int, planned: int) -> str:
    return f't{index:0{len(str(planned))}d}'


def find_timelapse_manifest(path: Path | str) -> Optional[Path]:
    """The ``timelapse.json`` that ``path`` is, is in, or is one timepoint of.

    Accepts the manifest, the timelapse folder, a timepoint folder, the
    tiling manifest or any tile file inside one. Only folders named like a
    timepoint (``t<digits>``) count as timepoints of a parent's manifest, so a
    plain tiling that happens to sit under some folder with a ``timelapse.json``
    is not mistaken for part of it.
    """
    path = Path(path)
    if path.name == TIMELAPSE_MANIFEST_NAME and path.is_file():
        return path
    if path.is_dir() and (path / TIMELAPSE_MANIFEST_NAME).is_file():
        return path / TIMELAPSE_MANIFEST_NAME
    folder = path if path.is_dir() else path.parent
    # The timepoint folder itself, or one folder inside it (tiles in a subfolder).
    for candidate in (folder, folder.parent):
        if _POINT_FOLDER.fullmatch(candidate.name):
            manifest = candidate.parent / TIMELAPSE_MANIFEST_NAME
            if manifest.is_file():
                return manifest
    return None


def read_timelapse(path: Path | str) -> Optional[TimelapseTiling]:
    """Parse the timelapse that ``path`` belongs to; ``None`` if it is not one.

    Never raises for an unreadable or foreign manifest: a ``timelapse.json`` of
    another kind (a camera timelapse has none today) or one that does not parse
    is "not a timelapse of tilings", and the caller carries on with the plain file.
    """
    manifest = find_timelapse_manifest(path)
    if manifest is None:
        return None
    try:
        raw = json.loads(manifest.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get('kind') or KIND_TILING)
    if kind != KIND_TILING:
        return None
    try:
        planned = int(raw.get('timepoints'))
    except (TypeError, ValueError):
        planned = 0
    records = {}
    for point in raw.get('points') or []:
        if isinstance(point, dict) and isinstance(point.get('index'), int):
            records[int(point['index'])] = point
    if planned < 1:
        planned = (max(records) + 1) if records else 0
    if planned < 1:
        return None
    root = manifest.parent
    interval = raw.get('interval_s')
    interval = float(interval) if isinstance(interval, (int, float)) and interval > 0 else None
    points: List[TimepointRecord] = []
    for index in range(planned):
        record = records.get(index)
        name = str((record or {}).get('folder') or _folder_name(index, planned))
        folder = root / name
        tiles = folder / TILING_MANIFEST_NAME
        if not tiles.is_file():
            # One level down, in case a run kept its tiling in a subfolder.
            nested = sorted(folder.glob(f'*/{TILING_MANIFEST_NAME}')) if folder.is_dir() else []
            tiles = nested[0] if len(nested) == 1 else tiles
        has_tiles = tiles.is_file()
        offset = (record or {}).get('scheduled_offset_s')
        time_s = float(offset) if isinstance(offset, (int, float)) else None
        if time_s is None and interval is not None:
            time_s = index * interval
        acquired = (record or {}).get('tiles_acquired')
        acquired = int(acquired) if isinstance(acquired, int) else None
        state, reason = _point_state(record, has_tiles, tiles)
        points.append(TimepointRecord(
            index=index, folder=folder, state=state, reason=reason,
            manifest=tiles if has_tiles else None, time_s=time_s,
            tiles_acquired=acquired,
        ))
    started = raw.get('started_utc')
    settings = raw.get('tiling_settings')
    return TimelapseTiling(
        manifest=manifest, root=root, kind=kind, planned=planned,
        interval_s=interval, started_utc=str(started) if started else None,
        tiling_settings=dict(settings) if isinstance(settings, dict) else {},
        points=tuple(points),
    )


def _point_state(record: Optional[dict], has_tiles: bool, tiles: Path) -> Tuple[str, str]:
    saved_complete = None
    if has_tiles:
        try:
            saved_complete = json.loads(tiles.read_text(encoding='utf-8')).get('completed')
        except (OSError, ValueError, AttributeError):
            return STATE_INCOMPLETE, 'its tiles.json could not be read'
    if record is None:
        if has_tiles:
            return STATE_INCOMPLETE, 'not recorded in timelapse.json (the run was interrupted here)'
        return STATE_MISSING, 'never run'
    if not has_tiles:
        return STATE_MISSING, str(record.get('message') or 'the tiling wrote no tiles.json')
    if not record.get('completed', False):
        why = str(record.get('message') or '')
        if record.get('stopped'):
            why = why or 'stopped'
        return STATE_INCOMPLETE, why or 'the tiling did not finish'
    if saved_complete is False:
        return STATE_INCOMPLETE, 'tiles.json says the scan did not complete'
    return STATE_COMPLETE, ''


def first_member_manifest(path: Path | str) -> Optional[Path]:
    """The ``tiles.json`` to open for a timelapse manifest or folder.

    The first complete timepoint (else the first with a manifest): opening the
    series as a whole lands on one concrete tiling, and the series is found
    again from there.
    """
    series = read_timelapse(path)
    if series is None:
        return None
    for wanted in (STATE_COMPLETE, STATE_INCOMPLETE):
        for point in series.points:
            if point.state == wanted and point.manifest is not None:
                return point.manifest
    return None


# ----------------------------------------------------------------------
# Compatibility
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class GridSignature:
    """What must agree between timepoints for one layout to serve them all."""

    alignment_detector: str
    detectors: Tuple[str, ...]
    pixel_size_yx_um: Tuple[float, float]
    z_step_um: float
    orientation: Tuple[bool, bool, bool]
    grids: Tuple[Tuple[int, int], ...]
    alignment_shape: Tuple[int, ...]
    payloads: Tuple[Tuple[str, str, Tuple[int, ...]], ...]

    def differences(self, other: 'GridSignature') -> List[str]:
        out: List[str] = []
        if self.alignment_detector != other.alignment_detector:
            out.append(f'alignment detector {other.alignment_detector!r} != {self.alignment_detector!r}')
        if self.detectors != other.detectors:
            out.append(f'detectors {list(other.detectors)} != {list(self.detectors)}')
        if any(abs(a - b) > 1e-9 * max(1.0, abs(a)) for a, b in zip(self.pixel_size_yx_um, other.pixel_size_yx_um)):
            out.append(f'pixel size {other.pixel_size_yx_um} != {self.pixel_size_yx_um} um')
        if abs(self.z_step_um - other.z_step_um) > 1e-9:
            out.append(f'z step {other.z_step_um} != {self.z_step_um} um')
        if self.orientation != other.orientation:
            out.append('stage orientation differs')
        if self.grids != other.grids:
            out.append(f'grid of {len(other.grids)} tiles differs from the {len(self.grids)}-tile reference grid')
        if self.alignment_shape != other.alignment_shape:
            out.append(f'alignment tile shape {other.alignment_shape} != {self.alignment_shape}')
        if self.payloads != other.payloads:
            out.append('payload axes/shape differ')
        return out


def grid_signature(index) -> GridSignature:
    """The signature of a parsed :class:`TilingDatasetIndex`."""
    tiles = index.tiles
    alignment_shape = tuple(tiles[0].alignment.shape) if tiles else ()
    payloads = []
    for detector in sorted(index.detectors):
        refs = [tile.payloads[detector] for tile in tiles if detector in tile.payloads and tile.payloads[detector].complete]
        if refs:
            payloads.append((detector, refs[0].axes, tuple(refs[0].shape)))
    ordered = [index.alignment_detector] + [d for d in index.detectors if d != index.alignment_detector]
    return GridSignature(
        alignment_detector=index.alignment_detector,
        detectors=tuple(sorted(index.detectors)),
        pixel_size_yx_um=(float(index.pixel_size_yx_um[0]), float(index.pixel_size_yx_um[1])),
        z_step_um=float(index.z_step_um or 0.0),
        orientation=tuple(index.orientation),
        grids=tuple(tuple(tile.grid) for tile in tiles),
        alignment_shape=alignment_shape,
        payloads=tuple(payloads),
    )


@dataclass(frozen=True)
class Compatibility:
    """Which timepoints can share one layout, and why the others cannot."""

    reference_index: Optional[int]
    compatible: Tuple[int, ...]
    #: ``{timepoint index: [reasons]}`` for the complete timepoints that differ.
    incompatible: Mapping[int, Tuple[str, ...]]
    unreadable: Mapping[int, str] = field(default_factory=dict)

    @property
    def all_compatible(self) -> bool:
        return not self.incompatible and not self.unreadable


def parse_timepoints(series: TimelapseTiling, indices: Optional[Sequence[int]] = None
                     ) -> Tuple[Dict[int, Any], Dict[int, str]]:
    """Parse the tiling manifest of every complete timepoint (``indices`` to restrict).

    Returns ``(parsed, unreadable)``: the indexes by timepoint, and why the
    others could not be read.
    """
    from imswitch.imcommon.algorithms.tile_mosaic import inspect_dataset

    wanted = set(indices) if indices is not None else None
    parsed: Dict[int, Any] = {}
    unreadable: Dict[int, str] = {}
    for point in series.points:
        if point.state != STATE_COMPLETE or point.manifest is None:
            continue
        if wanted is not None and point.index not in wanted:
            continue
        try:
            parsed[point.index], _completeness = inspect_dataset(point.manifest)
        except Exception as exc:  # noqa: BLE001 - reported per timepoint
            unreadable[point.index] = str(exc)
    return parsed, unreadable


def compare_grids(parsed: Mapping[int, Any], unreadable: Optional[Mapping[int, str]] = None, *,
                  stage_tolerance_px: float = 1.0) -> Compatibility:
    """Which of the parsed timepoints share one grid.

    The reference is the signature most timepoints share (earliest on a tie),
    so one odd timepoint does not make every other one "incompatible".
    """
    unreadable = dict(unreadable or {})
    signatures = {position: grid_signature(index) for position, index in parsed.items()}
    if not signatures:
        return Compatibility(None, (), {}, unreadable)
    counts: Dict[GridSignature, List[int]] = {}
    for position, signature in signatures.items():
        counts.setdefault(signature, []).append(position)
    reference = max(counts.items(), key=lambda item: (len(item[1]), -min(item[1])))[0]
    ref_index = min(counts[reference])
    ok: List[int] = []
    bad: Dict[int, Tuple[str, ...]] = {}
    ref_stage = np.array([tile.stage_um for tile in parsed[ref_index].tiles], dtype=float)
    pixel = np.array(reference.pixel_size_yx_um, dtype=float)
    for position in sorted(signatures):
        reasons = reference.differences(signatures[position])
        if not reasons and len(ref_stage):
            stage = np.array([tile.stage_um for tile in parsed[position].tiles], dtype=float)
            # stage_um is (x, y); either pixel size is a fair yardstick.
            offset_px = float(np.max(np.abs(stage - ref_stage)) / float(np.min(pixel)))
            if offset_px > stage_tolerance_px:
                reasons = [f'stage positions differ by {offset_px:.1f} px from the reference timepoint']
        if reasons:
            bad[position] = tuple(reasons)
        else:
            ok.append(position)
    return Compatibility(ref_index, tuple(ok), bad, unreadable)


def compatibility(series: TimelapseTiling, *, indices: Optional[Sequence[int]] = None,
                  stage_tolerance_px: float = 1.0) -> Tuple[Compatibility, Dict[int, Any]]:
    """Compare the grids of the complete timepoints; returns the verdict and the parsed indexes."""
    parsed, unreadable = parse_timepoints(series, indices)
    return compare_grids(parsed, unreadable, stage_tolerance_px=stage_tolerance_px), parsed


# ----------------------------------------------------------------------
# Common canvas
# ----------------------------------------------------------------------


def placement_bounds(index, positions_yx: Mapping[int, Tuple[float, float]], selection, *,
                     shift_yx: Tuple[float, float] = (0.0, 0.0),
                     transform_resolver=None) -> Optional[Tuple[int, int, int, int]]:
    """``(row0, col0, row1, col1)`` the complete payload tiles occupy, end exclusive.

    Manifest only: no pixel is read. The same arithmetic ``assemble_payload``
    uses, so a canvas built from these bounds always holds what it will place.
    ``shift_yx`` moves every tile (a timepoint's drift correction).
    """
    from imswitch.imcommon.algorithms.tile_mosaic import (
        _affine_footprint_bounds,
        _compose_payload_matrix,
        _payload_transform,
        _selected_axes_shape,
    )

    chosen = [
        (tile, tile.payloads[selection.detector])
        for tile in index.tiles
        if selection.detector in tile.payloads
        and tile.payloads[selection.detector].complete
        and tile.tile_id in positions_yx
    ]
    if not chosen:
        return None
    transform, _source = _payload_transform(
        index, selection, [ref for _tile, ref in chosen], transform_resolver
    )
    boxes = []
    for tile, ref in chosen:
        _axes, shape = _selected_axes_shape(ref, selection)
        y, x = positions_yx[tile.tile_id]
        matrix = _compose_payload_matrix((y + shift_yx[0], x + shift_yx[1]), transform)
        boxes.append(_affine_footprint_bounds(matrix, shape[-2:]))
    return (
        min(b[0] for b in boxes), min(b[1] for b in boxes),
        max(b[2] for b in boxes), max(b[3] for b in boxes),
    )


def payload_axes_leading(indices: Sequence[Any], selection) -> Tuple[str, Tuple[int, ...]]:
    """Output axes and the leading (non Y/X) extents over every timepoint's tiles."""
    from imswitch.imcommon.algorithms.tile_mosaic import _selected_axes_shape

    axes = None
    leading: Optional[Tuple[int, ...]] = None
    for index in indices:
        for tile in index.tiles:
            ref = tile.payloads.get(selection.detector)
            if ref is None or not ref.complete:
                continue
            tile_axes, shape = _selected_axes_shape(ref, selection)
            if axes is None:
                axes = tile_axes
            elif axes != tile_axes:
                raise ValueError(f'Detector {selection.detector!r} has axes {axes!r} and {tile_axes!r} in different tiles')
            lead = tuple(shape[:-2])
            leading = lead if leading is None else tuple(max(a, b) for a, b in zip(leading, lead))
    if axes is None or leading is None:
        raise ValueError(f'No complete payload is declared for {selection.detector!r}')
    return axes, leading


def common_canvas(bounds: Sequence[Optional[Tuple[int, int, int, int]]]) -> Tuple[int, int, int, int]:
    """``(row0, col0, height, width)`` of the union of every timepoint's bounds.

    One origin and one shape for the whole stack: nothing is cropped (the
    union holds every tile of every timepoint) and nothing needs shifting
    afterwards (all timepoints are drawn on it at their own positions).
    """
    boxes = [b for b in bounds if b is not None]
    if not boxes:
        raise ValueError('No timepoint has a placeable payload')
    row0 = min(b[0] for b in boxes)
    col0 = min(b[1] for b in boxes)
    row1 = max(b[2] for b in boxes)
    col1 = max(b[3] for b in boxes)
    return row0, col0, row1 - row0, col1 - col0


# ----------------------------------------------------------------------
# Shared layout: correlation surfaces accumulated over time
# ----------------------------------------------------------------------

#: Subpixel search window (pixels either side of the integer peak) and factor.
_UPSAMPLE = 10
_WINDOW_PX = 1.5

#: Bytes of correlation accumulators held at once. More pairs than this are
#: handled in batches, each batch costing one more read pass over the tiles.
DEFAULT_ACCUMULATOR_BYTES = 256 * 1024 ** 2


@dataclass
class SharedLayoutReport:
    """What the joint estimate used, for the provenance and the log."""

    timepoints: Tuple[int, ...] = ()
    pairs: int = 0
    pairs_measured: int = 0
    passes: int = 1
    #: Per link: effective (time-averaged) confidence.
    confidences: Tuple[float, ...] = ()
    #: Per link ``(i, j, corrected dy, dx)`` in tile ids.
    corrections: Tuple[Tuple[int, int, float, float], ...] = ()


def _fft2(patch: np.ndarray) -> np.ndarray:
    from scipy.fft import fft2

    return fft2(patch.astype(np.float32, copy=False))


def _peak_shift(accumulated: np.ndarray, count: int, max_shift_px: float,
                patch_shape: Tuple[int, int]) -> Optional[Tuple[float, float, float]]:
    """``(dy, dx, per-frame confidence)`` of the peak of an accumulated spectrum.

    ``accumulated`` is ``sum_t F_ref(t) * conj(F_mov(t))`` of unit-variance
    patches, so its inverse transform is the sum of the circular
    cross-correlations. The search is limited to ``max_shift_px`` (a false
    peak further out is never reported), refined to ``1/_UPSAMPLE`` pixel by a
    local evaluation of the same transform, and the peak height is turned into
    the (conservative) correlation coefficient one frame shows there.
    """
    from scipy.fft import ifft2

    height, width = patch_shape
    surface = np.real(ifft2(accumulated))
    fy = np.fft.fftfreq(height) * height
    fx = np.fft.fftfreq(width) * width
    reach = int(np.ceil(max_shift_px))
    rows = np.where(np.abs(fy) <= reach)[0]
    cols = np.where(np.abs(fx) <= reach)[0]
    if rows.size == 0 or cols.size == 0:
        return None
    window = surface[np.ix_(rows, cols)]
    gy, gx = np.unravel_index(int(np.argmax(window)), window.shape)
    cy, cx = float(fy[rows[gy]]), float(fx[cols[gx]])
    # Local upsampled evaluation: cc(s) = Re sum_k acc[k] exp(2 pi i k s / N) / N.
    steps = np.arange(-_WINDOW_PX, _WINDOW_PX + 1e-9, 1.0 / _UPSAMPLE)
    sy, sx = cy + steps, cx + steps
    ky = np.exp(2j * np.pi * np.outer(sy, np.fft.fftfreq(height)))
    kx = np.exp(2j * np.pi * np.outer(np.fft.fftfreq(width), sx))
    local = np.real(ky @ accumulated @ kx) / (height * width)
    py, px = np.unravel_index(int(np.argmax(local)), local.shape)
    dy, dx = float(sy[py]), float(sx[px])
    if np.hypot(dy, dx) > max_shift_px:
        return None
    # The correlation coefficient of the whole patch at that displacement. A
    # circular correlation sees only the part of the patch that does not wrap
    # (a fraction (1 - |dy|/H)(1 - |dx|/W) of it), so this *under*states a true
    # peak at a large shift. That is the safe direction: dividing it back would
    # inflate the false peaks that wander to large shifts in noise by just as much.
    frame = float(local[py, px]) / (max(1, count) * float(height * width))
    return dy, dx, float(np.clip(frame, -1.0, 1.0))


def _effective_confidence(frame: float, frames: int) -> float:
    """Expected correlation of the *time-averaged* patches, from one frame's.

    With independent noise the averaged signal fraction is
    ``T r / (1 + (T - 1) r)`` for per-frame ``r`` (``T = 1`` gives ``r``).
    """
    if frame <= 0:
        return 0.0
    return float(frames * frame / (1.0 + (frames - 1) * frame))


def solve_shared_layout(
    indices: Mapping[int, Any],
    use: Sequence[int],
    options,
    *,
    accumulator_bytes: int = DEFAULT_ACCUMULATOR_BYTES,
    progress: Optional[Callable[[str], None]] = None,
    check_cancelled: Optional[Callable[[], None]] = None,
    phase_progress: Optional[Callable[[str, int, int, str], None]] = None,
):
    """One tile layout for several timepoints, from all of them at once.

    ``indices`` maps timepoint index to its parsed ``TilingDatasetIndex``
    (compatible grids, see :func:`compatibility`); ``use`` are the timepoints
    whose images take part. ``options`` is a ``LayoutOptions``: refinement off
    gives the nominal layout (the stage positions, or the per-tile median of
    the saved ones), exactly as for a single tiling.

    **Method.** For every overlapping pair of tiles the cross-correlation
    surfaces of the bandpassed overlaps are *summed over the timepoints*, and
    the peak of the sum is the pair's offset. Correlation is linear in the
    images, so the sum is the maximum-likelihood estimate of one common offset
    under independent noise: the peak grows with the number of timepoints while
    the noise floor grows with its square root. A per-timepoint estimate has to
    find the peak in noise ``T`` times and cannot recover from a false one; a
    median of per-timepoint offsets (the obvious alternative) only helps when
    most timepoints get it right, which is exactly what fails in low light.
    A rigid drift between timepoints moves both tiles of a pair together, so it
    does not blur the sum -- the pair's relative offset is drift-invariant.
    The pairs' offsets then go through the same robust global solve a single
    tiling uses (``solve_links``).

    Memory: one accumulator per overlapping pair (the overlap's size, complex),
    in batches under ``accumulator_bytes``; tiles are read one timepoint at a
    time, never all together.
    """
    from imswitch.imcommon.algorithms.tile_mosaic import (
        MIN_LINK_CONFIDENCE,
        MIN_OVERLAP_PX,
        MosaicLayout,
        MosaicTile,
        RefinementReport,
        TileLink,
        _bandpass,
        _check_cancelled,
        _nominal_index_positions,
        _normalize,
        _overlap_box,
        _phase_progress,
        _reporter,
        _components,
        read_alignment_artifact,
        solve_links,
        MOVED_EPS_PX,
    )

    emit = _reporter(progress)
    use = [int(t) for t in use]
    if not use:
        raise ValueError('No timepoint to estimate a layout from')
    reference = indices[use[0]]
    tiles = list(reference.tiles)
    ids = [tile.tile_id for tile in tiles]

    # Nominal positions: the per-tile median over the timepoints used, so one
    # timepoint's live-alignment correction cannot move the shared layout.
    per_time = []
    source = 'stage'
    for t in use:
        positions, source_t = _nominal_index_positions(indices[t], options.stage_positions)
        per_time.append([positions[i] for i in ids])
        if t == use[0]:
            source = source_t
    nominal_array = np.median(np.asarray(per_time, dtype=np.float64), axis=0)
    nominal = {tile_id: (float(y), float(x)) for tile_id, (y, x) in zip(ids, nominal_array)}
    report = RefinementReport(tiles=len(tiles))
    shared = SharedLayoutReport(timepoints=tuple(use))
    if not options.refine:
        return MosaicLayout(nominal, source, report), shared

    plane_shapes = [tuple(tile.alignment.shape[-2:]) for tile in tiles]
    max_shift = options.max_shift_px
    if max_shift is None:
        max_shift = max(float(MIN_OVERLAP_PX), 0.25 * float(min(s[0] for s in plane_shapes)))
    candidates = []
    for i in range(len(tiles)):
        for j in range(i + 1, len(tiles)):
            offset = (nominal_array[j][0] - nominal_array[i][0], nominal_array[j][1] - nominal_array[i][1])
            box = _overlap_box(plane_shapes[i], plane_shapes[j], offset)
            if box is not None:
                ref_sl, mov_sl = box
                shape = (ref_sl[0].stop - ref_sl[0].start, ref_sl[1].stop - ref_sl[1].start)
                candidates.append((i, j, offset, ref_sl, mov_sl, shape))
    shared.pairs = len(candidates)
    emit(f'Shared layout: {len(candidates)} overlapping tile pairs, {len(use)} timepoint(s)')

    # Batches of pairs under the accumulator budget.
    batches, current, used = [], [], 0
    for item in candidates:
        cost = int(item[5][0] * item[5][1] * 8)
        if current and used + cost > accumulator_bytes:
            batches.append(current)
            current, used = [], 0
        current.append(item)
        used += cost
    if current:
        batches.append(current)
    shared.passes = max(1, len(batches))
    total_steps = max(1, len(batches) * len(use))
    done_steps = 0

    links: List[TileLink] = []
    confidences: List[float] = []
    corrections: List[Tuple[int, int, float, float]] = []
    for batch in batches:
        accumulators = {(i, j): np.zeros(shape, dtype=np.complex64) for i, j, _o, _r, _m, shape in batch}
        counts = {(i, j): 0 for i, j, *_rest in batch}
        needed = sorted({n for i, j, *_rest in batch for n in (i, j)})
        for t in use:
            _check_cancelled(check_cancelled)
            index_t = indices[t]
            cache: Dict[int, np.ndarray] = {}
            for node in needed:
                try:
                    data = read_alignment_artifact(index_t.tiles[node].alignment)
                    cache[node] = MosaicTile(
                        name='', data=data, position=(0.0, 0.0),
                        axes=index_t.tiles[node].alignment.axes,
                    ).projection()
                except Exception:  # noqa: BLE001 - this tile is absent at t
                    continue
            for i, j, _offset, ref_sl, mov_sl, _shape in batch:
                if i not in cache or j not in cache:
                    continue
                ref_patch = _normalize(_bandpass(cache[i][ref_sl]))
                mov_patch = _normalize(_bandpass(cache[j][mov_sl]))
                if ref_patch.std() <= 1e-6 or mov_patch.std() <= 1e-6:
                    continue
                accumulators[(i, j)] += _fft2(ref_patch) * np.conj(_fft2(mov_patch))
                counts[(i, j)] += 1
            del cache
            done_steps += 1
            _phase_progress(phase_progress, 'align', done_steps, total_steps,
                            f'Timepoint {t}: correlated {len(batch)} tile pairs')
        for i, j, offset, _r, _m, shape in batch:
            count = counts[(i, j)]
            if count == 0:
                continue
            peak = _peak_shift(accumulators[(i, j)], count, max_shift, shape)
            if peak is None:
                continue
            dy, dx, frame = peak
            confidence = _effective_confidence(frame, count)
            if confidence < MIN_LINK_CONFIDENCE:
                continue
            measured = (offset[0] + dy, offset[1] + dx)
            links.append(TileLink(i=i, j=j, offset=measured, confidence=confidence, correction=(dy, dx)))
            confidences.append(confidence)
            corrections.append((ids[i], ids[j], dy, dx))
        del accumulators
    shared.pairs_measured = len(links)
    shared.confidences = tuple(confidences)
    shared.corrections = tuple(corrections)
    report.links = links
    if not links:
        emit('No tile pair could be correlated over time; keeping the nominal positions.')
        return MosaicLayout(nominal, source, report), shared
    nominal_list = [tuple(row) for row in nominal_array]
    solved, active = solve_links(nominal_list, links, progress)
    positions = dict(nominal)
    for node, tile_id in enumerate(ids):
        positions[tile_id] = (float(solved[node][0]), float(solved[node][1]))
        if (abs(solved[node][0] - nominal_list[node][0]) > MOVED_EPS_PX
                or abs(solved[node][1] - nominal_list[node][1]) > MOVED_EPS_PX):
            report.moved += 1
    report.components = [len(nodes) for nodes in _components(len(tiles), active)]
    emit(report.summary())
    return MosaicLayout(positions, 'refined' if report.accepted else source, report), shared


# ----------------------------------------------------------------------
# Drift between timepoints
# ----------------------------------------------------------------------


@dataclass
class DriftEstimate:
    """One rigid XY correction per timepoint, against the reference timepoint.

    ``shift_yx[t]`` is the displacement (pixels, ``(row, col)``) to *add to
    every tile position of timepoint t* so that it registers with the
    reference; the sample drifted by its negative. The reference's is zero.
    """

    reference: int
    timepoints: Tuple[int, ...]
    shift_yx: Tuple[Tuple[float, float], ...]
    #: Tiles that voted for each timepoint, and their spread (MAD, px).
    votes: Tuple[int, ...] = ()
    spread_px: Tuple[float, ...] = ()
    #: ``True`` where no tile could be measured and the value was interpolated.
    interpolated: Tuple[bool, ...] = ()
    smoothing: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            'reference': self.reference,
            'timepoints': list(self.timepoints),
            'shift_yx_px': [list(map(float, s)) for s in self.shift_yx],
            'votes': list(self.votes),
            'spread_px': list(self.spread_px),
            'interpolated': list(self.interpolated),
            'smoothing': self.smoothing,
        }


def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    order = np.argsort(values)
    values, weights = values[order], weights[order]
    cumulative = np.cumsum(weights)
    return float(values[int(np.searchsorted(cumulative, 0.5 * cumulative[-1]))])


def sample_tiles(count: int, wanted: int) -> List[int]:
    """Up to ``wanted`` tile positions spread evenly over ``count`` tiles."""
    if count <= wanted:
        return list(range(count))
    return sorted({int(round(v)) for v in np.linspace(0, count - 1, wanted)})


def estimate_drift(
    indices: Mapping[int, Any],
    use: Sequence[int],
    reference: int,
    *,
    max_drift_px: Optional[float] = None,
    tiles: int = 9,
    crop_px: int = 1024,
    smoothing: int = 0,
    check_cancelled: Optional[Callable[[], None]] = None,
    phase_progress: Optional[Callable[[str, int, int, str], None]] = None,
) -> DriftEstimate:
    """The rigid XY shift of every timepoint in ``use`` against ``reference``.

    A handful of tiles spread over the grid (``tiles``; their central
    ``crop_px`` square) are each correlated with the *same tile at the
    reference timepoint* -- same stage position, so any shift is the sample's
    -- and the shifts combined by a confidence-weighted median, which a tile on
    empty background or a false peak cannot move. ``smoothing`` is the width of
    a running median over timepoints (``0``/``1``: none), for a stage whose
    drift is slow against the noise of the estimate. A timepoint no tile could
    measure is interpolated from its neighbours and flagged.
    """
    from imswitch.imcommon.algorithms.tile_mosaic import (
        MIN_OVERLAP_PX,
        MosaicTile,
        _check_cancelled,
        _measure_link,
        _phase_progress,
        read_alignment_artifact,
    )

    use = [int(t) for t in use]
    if reference not in use:
        raise ValueError(f'Drift reference {reference} is not among the timepoints {use}')
    ref_index = indices[reference]
    pick = sample_tiles(len(ref_index.tiles), tiles)

    def read_crop(index, node):
        data = read_alignment_artifact(index.tiles[node].alignment)
        plane = MosaicTile(name='', data=data, position=(0.0, 0.0),
                           axes=index.tiles[node].alignment.axes).projection()
        h, w = plane.shape
        ch, cw = min(h, crop_px), min(w, crop_px)
        y0, x0 = (h - ch) // 2, (w - cw) // 2
        return np.asarray(plane[y0:y0 + ch, x0:x0 + cw], dtype=np.float32)

    references: Dict[int, np.ndarray] = {}
    for node in pick:
        try:
            references[node] = read_crop(ref_index, node)
        except Exception:  # noqa: BLE001
            continue
    if not references:
        raise ValueError('No alignment image of the reference timepoint is readable; cannot estimate drift')
    min_side = min(min(a.shape) for a in references.values())
    limit = max_drift_px if max_drift_px else max(float(MIN_OVERLAP_PX), 0.25 * float(min_side))

    shifts: List[Optional[Tuple[float, float]]] = []
    votes: List[int] = []
    spread: List[float] = []
    for step, t in enumerate(use):
        _check_cancelled(check_cancelled)
        if t == reference:
            shifts.append((0.0, 0.0)); votes.append(len(references)); spread.append(0.0)
            continue
        found: List[Tuple[float, float, float]] = []
        for node, ref_crop in references.items():
            try:
                crop = read_crop(indices[t], node)
            except Exception:  # noqa: BLE001
                continue
            if crop.shape != ref_crop.shape:
                continue
            measured = _measure_link(ref_crop, crop, (0.0, 0.0), limit)
            if measured is not None:
                (dy, dx), confidence = measured
                found.append((dy, dx, confidence))
        if found:
            arr = np.asarray(found)
            w = np.maximum(arr[:, 2], 1e-3)
            dy, dx = _weighted_median(arr[:, 0], w), _weighted_median(arr[:, 1], w)
            shifts.append((dy, dx))
            votes.append(len(found))
            spread.append(float(np.median(np.hypot(arr[:, 0] - dy, arr[:, 1] - dx))))
        else:
            shifts.append(None); votes.append(0); spread.append(float('nan'))
        _phase_progress(phase_progress, 'align', step + 1, len(use), f'Drift of timepoint {t}')
    interpolated = [s is None for s in shifts]
    known = [k for k, s in enumerate(shifts) if s is not None]
    arr = np.zeros((len(use), 2), dtype=np.float64)
    for axis in (0, 1):
        if known:
            arr[:, axis] = np.interp(
                np.arange(len(use)), known, [shifts[k][axis] for k in known]
            )
    if smoothing and smoothing > 1:
        half = int(smoothing) // 2
        smooth = arr.copy()
        for k in range(len(use)):
            lo, hi = max(0, k - half), min(len(use), k + half + 1)
            smooth[k] = np.median(arr[lo:hi], axis=0)
        # The reference stays the reference.
        smooth -= smooth[use.index(reference)]
        arr = smooth
    return DriftEstimate(
        reference=reference, timepoints=tuple(use),
        shift_yx=tuple((float(a), float(b)) for a, b in arr),
        votes=tuple(votes), spread_px=tuple(spread),
        interpolated=tuple(interpolated), smoothing=int(smoothing or 0),
    )
