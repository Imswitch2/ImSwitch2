"""The finished measurement run file (HDF5), and its loader.

A run file is written once, from a validated journal (:mod:`.journal`), when
the run has ended — or by recovery from an interrupted journal. It is never
written incrementally, so a crash cannot leave a half-written HDF5 structure.

Layout (``schema_version`` 1)::

    /                         attrs: imswitch_measurement_run=1, schema_version,
                                     run_id, acquisition, cleanup, metadata (JSON)
    /points                   one row per committed point, commit order
        point, status, t_start, t_end, grid_index (n, ndim; -1 if no grid), causes
    /controls/<name>          aligned with /points rows
        requested, acknowledged, measured, ok, cause
    /instruments/<name>/samples   every sample row (valid and invalid)
    /instruments/<name>/windows   one row per point: extent + window outcome

Angles and other control values are plain coordinates with their units in
the metadata. There is no spatial calibration in a run file.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

import numpy as np

from .journal import (
    CONTROL_DTYPE,
    JournalContents,
    _fsync_dir,
    quantities_from_metadata,
    quantity_field,
    read_journal,
)
from .types import (
    AcquisitionOutcome,
    CleanupOutcome,
    GridInfo,
    PointStatus,
    QuantitySpec,
)

SCHEMA_MARKER = 'imswitch_measurement_run'
SCHEMA_VERSION = 1
RUN_FILE_SUFFIX = '.run.h5'

WINDOW_DTYPE = np.dtype([
    ('point', '<i8'),
    ('offset', '<i8'),
    ('count', '<i8'),
    ('accepted', '<i8'),
    ('invalid', '<i8'),
    ('requested', '<i8'),
    ('complete', 'u1'),
    ('cause', 'S24'),
    ('discarded', '<i8'),
    ('verification', 'S12'),
    ('profile', 'S32'),
    ('boundary_t', '<f8'),
])


class RunFileError(RuntimeError):
    """A run file cannot be written or read as requested."""


def default_run_file_path(run_dir: Path) -> Path:
    run_dir = Path(run_dir)
    stem = run_dir.name
    if stem.endswith('.journal'):
        stem = stem[:-len('.journal')]
    return run_dir.with_name(stem + RUN_FILE_SUFFIX)


def finalize_journal(
    run_dir: Path,
    out_path: Optional[Path] = None,
    *,
    fsync: bool = True,
) -> Path:
    """Convert a run journal into one HDF5 run file and verify it.

    Works on a finished journal and on an interrupted one (recovery): an
    interrupted journal becomes a run file with acquisition outcome
    ``interrupted`` holding every point whose commit record validated.
    The journal itself is left in place; the caller removes it once this
    returns, i.e. after the file was written, synced and re-read.
    """
    import h5py

    contents = read_journal(run_dir)
    out_path = Path(out_path) if out_path is not None else default_run_file_path(run_dir)
    tmp = out_path.with_name(f'.{out_path.name}.tmp')
    if tmp.exists():
        tmp.unlink()
    with h5py.File(tmp, 'w') as fh:
        _write(fh, contents)
        fh.flush()
    if fsync:
        with open(tmp, 'rb+') as raw:
            os.fsync(raw.fileno())
    os.replace(tmp, out_path)
    if fsync:
        _fsync_dir(out_path.parent)

    loaded = MeasurementRunFile.load(out_path)
    if loaded.n_points != len(contents.commits):
        raise RunFileError(
            f'run file {out_path} holds {loaded.n_points} points, '
            f'journal had {len(contents.commits)}'
        )
    return out_path


def _write(fh, contents: JournalContents) -> None:
    import h5py

    metadata = dict(contents.metadata)
    end = contents.end or {}
    acquisition = contents.acquisition
    cleanup = end.get('cleanup', CleanupOutcome.PENDING.value)
    metadata['end'] = end
    metadata['journal_problems'] = list(contents.problems)

    fh.attrs[SCHEMA_MARKER] = 1
    fh.attrs['schema_version'] = SCHEMA_VERSION
    fh.attrs['run_id'] = str(metadata.get('run_id', ''))
    fh.attrs['acquisition'] = acquisition.value
    fh.attrs['cleanup'] = str(cleanup)
    fh.attrs['metadata'] = json.dumps(metadata, sort_keys=True)

    commits = contents.commits
    n = len(commits)
    grid_ndim = len((metadata.get('grid') or {}).get('axes') or ())
    points = fh.create_group('points')
    points.create_dataset('point', data=np.array([c['point'] for c in commits], dtype='<i8'))
    points.create_dataset(
        'status', data=np.array([c['status'].encode() for c in commits], dtype='S16'))
    points.create_dataset('t_start', data=np.array([c['t_start'] for c in commits], dtype='<f8'))
    points.create_dataset('t_end', data=np.array([c['t_end'] for c in commits], dtype='<f8'))
    grid_index = np.full((n, max(grid_ndim, 1)), -1, dtype='<i8')
    for row, commit in enumerate(commits):
        if commit.get('grid_index') is not None and grid_ndim:
            grid_index[row, :grid_ndim] = commit['grid_index']
    points.create_dataset('grid_index', data=grid_index)
    points.create_dataset(
        'causes',
        data=np.array([json.dumps(c.get('causes') or []) for c in commits], dtype=object),
        dtype=h5py.string_dtype(),
    )

    row_of_point = {c['point']: i for i, c in enumerate(commits)}
    control_names = [c['name'] for c in metadata.get('controls', ())]
    controls_group = fh.create_group('controls')
    for index, name in enumerate(control_names):
        group = controls_group.create_group(name)
        requested = np.full(n, np.nan)
        acknowledged = np.full(n, np.nan)
        measured = np.full(n, np.nan)
        ok = np.zeros(n, dtype='u1')
        cause = np.zeros(n, dtype=CONTROL_DTYPE['cause'])
        rows = contents.controls[contents.controls['control'] == index]
        for rec in rows:
            row = row_of_point[int(rec['point'])]
            requested[row] = rec['requested']
            acknowledged[row] = rec['acknowledged']
            measured[row] = rec['measured']
            ok[row] = rec['ok']
            cause[row] = rec['cause']
        group.create_dataset('requested', data=requested)
        group.create_dataset('acknowledged', data=acknowledged)
        group.create_dataset('measured', data=measured)
        group.create_dataset('ok', data=ok)
        group.create_dataset('cause', data=cause)

    instruments_group = fh.create_group('instruments')
    for name, samples in contents.samples.items():
        group = instruments_group.create_group(name)
        group.create_dataset('samples', data=samples)
        windows = np.zeros(n, dtype=WINDOW_DTYPE)
        offset = 0
        for row, commit in enumerate(commits):
            window = (commit.get('windows') or {}).get(name)
            windows[row]['point'] = commit['point']
            windows[row]['offset'] = offset
            if window is None:
                windows[row]['cause'] = b'not_sampled'
                windows[row]['boundary_t'] = np.nan
                continue
            windows[row]['count'] = window['count']
            windows[row]['accepted'] = window.get('accepted', 0)
            windows[row]['invalid'] = window.get('invalid', 0)
            windows[row]['requested'] = window.get('requested', 0)
            windows[row]['complete'] = 1 if window.get('complete') else 0
            windows[row]['cause'] = (window.get('cause') or '').encode()
            windows[row]['discarded'] = window.get('discarded', 0)
            windows[row]['verification'] = (window.get('verification') or '').encode()
            windows[row]['profile'] = (window.get('profile') or '').encode()
            windows[row]['boundary_t'] = window.get('boundary_t', np.nan)
            offset += int(window['count'])
        group.create_dataset('windows', data=windows)


# ------------------------------------------------------------------- loading
def is_measurement_run_file(path: Union[str, Path]) -> bool:
    """True if ``path`` is an HDF5 file carrying the run schema marker."""
    try:
        import h5py
        with h5py.File(path, 'r') as fh:
            return int(fh.attrs.get(SCHEMA_MARKER, 0)) == 1
    except Exception:
        return False


@dataclass
class InstrumentData:
    name: str
    quantities: Tuple[QuantitySpec, ...]
    metadata: Dict[str, Any]
    samples: np.ndarray
    windows: np.ndarray

    def quantity(self, quantity_id: str) -> Optional[QuantitySpec]:
        for spec in self.quantities:
            if spec.quantity == quantity_id:
                return spec
        return None

    def column(self, rows: np.ndarray, quantity_id: str) -> Optional[np.ndarray]:
        spec = self.quantity(quantity_id)
        if spec is None:
            return None
        return rows[quantity_field(spec.name)]


@dataclass
class MeasurementRunFile:
    """A loaded run file. Runs are small; everything is held in memory."""

    path: Path
    metadata: Dict[str, Any]
    run_id: str
    acquisition: AcquisitionOutcome
    cleanup: str
    points: Dict[str, np.ndarray]
    controls: Dict[str, Dict[str, np.ndarray]]
    instruments: Dict[str, InstrumentData]
    grid: Optional[GridInfo]

    @classmethod
    def load(cls, path: Union[str, Path]) -> 'MeasurementRunFile':
        import h5py

        path = Path(path)
        with h5py.File(path, 'r') as fh:
            if int(fh.attrs.get(SCHEMA_MARKER, 0)) != 1:
                raise RunFileError(f'not a measurement run file: {path}')
            version = int(fh.attrs.get('schema_version', 0))
            if version != SCHEMA_VERSION:
                raise RunFileError(f'unsupported run schema version {version}')
            metadata = json.loads(fh.attrs['metadata'])
            points = {key: fh['points'][key][()] for key in fh['points']}
            controls = {
                name: {key: grp[key][()] for key in grp}
                for name, grp in fh['controls'].items()
            }
            instruments = {}
            for name, grp in fh['instruments'].items():
                entry = (metadata.get('instruments') or {}).get(name, {})
                instruments[name] = InstrumentData(
                    name=name,
                    quantities=quantities_from_metadata(entry),
                    metadata=entry,
                    samples=grp['samples'][()],
                    windows=grp['windows'][()],
                )
            acquisition = AcquisitionOutcome(str(fh.attrs.get('acquisition', 'interrupted')))
            cleanup = str(fh.attrs.get('cleanup', ''))
            run_id = str(fh.attrs.get('run_id', ''))
        return cls(
            path=path, metadata=metadata, run_id=run_id, acquisition=acquisition,
            cleanup=cleanup, points=points, controls=controls,
            instruments=instruments, grid=_grid_from_metadata(metadata),
        )

    # ------------------------------------------------------------ queries
    @property
    def n_points(self) -> int:
        return len(self.points['point'])

    def status(self) -> List[PointStatus]:
        return [PointStatus(s.decode()) for s in self.points['status']]

    def committed_mask(self) -> np.ndarray:
        return self.points['status'] == PointStatus.COMMITTED.value.encode()

    def control_names(self) -> List[str]:
        return [c['name'] for c in self.metadata.get('controls', ())]

    def control_unit(self, name: str) -> str:
        for entry in self.metadata.get('controls', ()):
            if entry['name'] == name:
                return entry.get('unit', '')
        return ''

    def control_values(self, name: str, *, prefer_measured: bool = True) -> Tuple[np.ndarray, np.ndarray]:
        """Per point: the value to report and whether it was measured.

        Measured (fresh readback) where available, else the requested value.
        """
        data = self.controls[name]
        measured = data['measured']
        has = np.isfinite(measured) if prefer_measured else np.zeros(len(measured), bool)
        return np.where(has, measured, data['requested']), has

    def instruments_with_quantity(self, quantity_id: str) -> List[str]:
        return [
            name for name, inst in self.instruments.items()
            if inst.quantity(quantity_id) is not None
        ]

    def point_samples(self, instrument: str, row: int, *, valid_only: bool = True) -> np.ndarray:
        """Sample rows of ``instrument`` belonging to points row ``row``."""
        inst = self.instruments[instrument]
        window = inst.windows[row]
        block = inst.samples[int(window['offset']):int(window['offset']) + int(window['count'])]
        if valid_only:
            block = block[block['valid'] == 1]
        return block

    def window_verified(self, instrument: str, row: int) -> bool:
        window = self.instruments[instrument].windows[row]
        return window['verification'] == b'verified'


def _grid_from_metadata(metadata: Mapping[str, Any]) -> Optional[GridInfo]:
    grid = metadata.get('grid')
    if not grid:
        return None
    return GridInfo(
        axes=tuple((a['name'], tuple(a['values'])) for a in grid['axes']),
        traversal=grid.get('traversal', 'raster'),
        index=tuple(tuple(i) for i in grid.get('index', ())),
    )
