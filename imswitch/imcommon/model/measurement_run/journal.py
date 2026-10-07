"""Append-only run journal: the crash-safe storage of a running measurement.

A run directory holds::

    run.json              metadata, written atomically before the first point
    controls.bin          fixed-size control records, append-only
    samples/<inst>.bin    fixed-size sample records per instrument, append-only
    commits.log           one commit record per point, append-only
    end.json              final outcome, written atomically when the run ends

``commit_point`` writes in a fixed order: payload records first and
``fsync``; then one commit record naming the extents (offset, count) and the
CRC32 of every payload extent it refers to, and ``fsync`` again. A point
exists only through its commit record; a reader accepts a record only if it
is a complete line, its own checksum matches, and every extent it names lies
inside its payload file with a matching checksum. Payload written without a
commit record is ignored.

Guarantees (``docs/design/plans/transient-instruments-step-scans.md`` §8.3):

- application crash: every point whose commit record was appended is
  recoverable — the payload reached the OS before the record did;
- power loss / OS crash: every point whose commit ``fsync`` returned is
  recoverable, provided the storage honours ``fsync``.

The journal is the only thing written while a run is acquiring; the HDF5 run
file is produced from it afterwards (:mod:`.hdf5`), so no HDF5 structure is
ever half-written by a crash.
"""
from __future__ import annotations

import json
import math
import os
import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import numpy as np

from .types import (
    AcquisitionOutcome,
    CleanupOutcome,
    ControlResult,
    PointStatus,
    QuantitySpec,
    Sample,
    Verification,
    WindowResult,
)

JOURNAL_FORMAT = 'imswitch-run-journal'
JOURNAL_VERSION = 1

_NAME_RE = re.compile(r'^[A-Za-z0-9_.\-]+$')
_PROFILE_LEN = 32
_REASON_LEN = 64
_CAUSE_LEN = 64


#: The one journal problem a crash leaves behind by itself: the record being
#: appended when the process died. That point was never committed.
TORN_TAIL = 'last commit record is incomplete (torn write); ignored'


class JournalError(RuntimeError):
    """The journal cannot be written or read as requested."""


def check_name(kind: str, name: str) -> str:
    """Instrument and control names become file and dataset names."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise JournalError(
            f'{kind} name {name!r} must consist of letters, digits, '
            f'".", "_" or "-"'
        )
    return name


def quantity_field(name: str) -> str:
    return f'q_{name}'


def sample_dtype(quantities: Sequence[QuantitySpec]) -> np.dtype:
    fields = [
        ('point', '<i8'),
        ('generation', '<i8'),
        ('sequence', '<i8'),
        ('t_host', '<f8'),
        ('device_t', '<f8'),
        ('device_id', '<i8'),
        ('valid', 'u1'),
        ('verified', 'u1'),
        ('profile', f'S{_PROFILE_LEN}'),
        ('reason', f'S{_REASON_LEN}'),
    ]
    fields += [(quantity_field(q.name), '<f8') for q in quantities]
    return np.dtype(fields)


CONTROL_DTYPE = np.dtype([
    ('point', '<i8'),
    ('control', '<i4'),
    ('requested', '<f8'),
    ('acknowledged', '<f8'),
    ('measured', '<f8'),
    ('ok', 'u1'),
    ('cause', f'S{_CAUSE_LEN}'),
])


def _encode(text: Optional[str], length: int) -> bytes:
    raw = (text or '').encode('utf-8', 'replace')
    return raw[:length]


def _nan(value: Optional[float]) -> float:
    return math.nan if value is None else float(value)


def _fsync_dir(path: Path) -> None:
    # Directory fsync makes a newly created file's name durable. Not available
    # on Windows, where NTFS journals metadata itself.
    if os.name == 'nt':
        return
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json_atomic(path: Path, data: Mapping[str, Any], *, fsync: bool = True) -> None:
    path = Path(path)
    tmp = path.with_name(f'.{path.name}.tmp')
    with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump(data, fh, indent=2, sort_keys=True, default=_json_default)
        fh.flush()
        if fsync:
            os.fsync(fh.fileno())
    os.replace(tmp, path)
    if fsync:
        _fsync_dir(path.parent)


def _json_default(value):
    if isinstance(value, (Verification, AcquisitionOutcome, CleanupOutcome, PointStatus)):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f'not JSON serialisable: {type(value).__name__}')


def _line_crc(text: str) -> str:
    return f'{zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF:08x}'


class RunJournalWriter:
    """The only writer of one run directory. Not thread-safe: one thread."""

    def __init__(
        self,
        run_dir: Path,
        *,
        metadata: Mapping[str, Any],
        instruments: Mapping[str, Sequence[QuantitySpec]],
        controls: Sequence[str],
        fsync: bool = True,
    ) -> None:
        self.run_dir = Path(run_dir)
        self._fsync = bool(fsync)
        self._controls = [check_name('control', c) for c in controls]
        self._control_index = {name: i for i, name in enumerate(self._controls)}
        self._quantities = {
            check_name('instrument', name): tuple(qs)
            for name, qs in instruments.items()
        }
        self._dtypes = {name: sample_dtype(qs) for name, qs in self._quantities.items()}
        if self.run_dir.exists():
            # Never append to an existing (possibly interrupted) journal.
            raise JournalError(f'run directory already exists: {self.run_dir}')
        (self.run_dir / 'samples').mkdir(parents=True)

        header = dict(metadata)
        header['journal'] = {'format': JOURNAL_FORMAT, 'version': JOURNAL_VERSION}
        header['controls'] = _merge_named(header.get('controls'), self._controls)
        header['instruments'] = _merge_instruments(header.get('instruments'), self._quantities)
        write_json_atomic(self.run_dir / 'run.json', header, fsync=self._fsync)

        self._control_fh = open(self.run_dir / 'controls.bin', 'ab')
        self._sample_fh = {
            name: open(self.run_dir / 'samples' / f'{name}.bin', 'ab')
            for name in self._quantities
        }
        self._commit_fh = open(self.run_dir / 'commits.log', 'ab')
        if self._fsync:
            _fsync_dir(self.run_dir / 'samples')
            _fsync_dir(self.run_dir)
        self._control_count = 0
        self._sample_count = {name: 0 for name in self._quantities}
        self._committed: set = set()
        self._closed = False

    # ------------------------------------------------------------------ write
    def commit_point(
        self,
        *,
        point: int,
        status: PointStatus,
        t_start: float,
        t_end: float,
        controls: Sequence[ControlResult],
        windows: Mapping[str, WindowResult],
        grid_index: Optional[Sequence[int]] = None,
        causes: Sequence[str] = (),
    ) -> None:
        if self._closed:
            raise JournalError('journal is closed')
        point = int(point)
        if point in self._committed:
            raise JournalError(f'point {point} was already committed')
        unknown = set(windows) - set(self._quantities)
        if unknown:
            raise JournalError(f'unknown instruments: {sorted(unknown)}')

        control_rows = np.zeros(len(controls), dtype=CONTROL_DTYPE)
        for row, result in zip(control_rows, controls):
            if result.control not in self._control_index:
                raise JournalError(f'unknown control {result.control!r}')
            row['point'] = point
            row['control'] = self._control_index[result.control]
            row['requested'] = float(result.requested)
            row['acknowledged'] = _nan(result.acknowledged)
            row['measured'] = _nan(result.measured)
            row['ok'] = 1 if result.ok else 0
            row['cause'] = _encode(result.cause, _CAUSE_LEN)

        sample_blocks: Dict[str, np.ndarray] = {}
        for name, window in windows.items():
            samples = sorted(
                list(window.samples) + list(window.invalid),
                key=lambda s: (s.generation, s.sequence),
            )
            sample_blocks[name] = self._sample_rows(name, point, samples)

        # 1. payload, then fsync.
        record: Dict[str, Any] = {
            'v': JOURNAL_VERSION,
            'point': point,
            'status': PointStatus(status).value,
            't_start': float(t_start),
            't_end': float(t_end),
            'grid_index': None if grid_index is None else [int(i) for i in grid_index],
            'causes': [str(c) for c in causes],
        }
        control_bytes = control_rows.tobytes()
        record['controls'] = {
            'offset': self._control_count,
            'count': int(len(control_rows)),
            'crc': zlib.crc32(control_bytes) & 0xFFFFFFFF,
        }
        self._write_payload(self._control_fh, control_bytes)

        window_records: Dict[str, Any] = {}
        for name, window in windows.items():
            block = sample_blocks[name]
            raw = block.tobytes()
            window_records[name] = {
                'offset': self._sample_count[name],
                'count': int(len(block)),
                'crc': zlib.crc32(raw) & 0xFFFFFFFF,
                'accepted': len(window.samples),
                'invalid': len(window.invalid),
                'requested': int(window.requested),
                'complete': bool(window.complete),
                'cause': None if window.cause is None else window.cause.value,
                'discarded': int(window.discarded),
                'verification': Verification(window.verification).value,
                'profile': window.profile_id,
                'boundary_t': float(window.boundary_t),
                'detail': window.detail,
            }
            self._write_payload(self._sample_fh[name], raw)
        record['windows'] = window_records

        if self._fsync:
            os.fsync(self._control_fh.fileno())
            for name in windows:
                os.fsync(self._sample_fh[name].fileno())

        # 2. commit record, then fsync. Only now does the point exist.
        text = json.dumps(record, sort_keys=True, separators=(',', ':'),
                          default=_json_default)
        line = f'{text}\t{_line_crc(text)}\n'.encode('utf-8')
        self._commit_fh.write(line)
        self._commit_fh.flush()
        if self._fsync:
            os.fsync(self._commit_fh.fileno())

        self._control_count += len(control_rows)
        for name, block in sample_blocks.items():
            self._sample_count[name] += len(block)
        self._committed.add(point)

    def finish(
        self,
        acquisition: AcquisitionOutcome,
        *,
        cleanup: CleanupOutcome = CleanupOutcome.PENDING,
        detail: str = '',
        extra: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Record the final acquisition outcome and close the payload files."""
        if self._closed:
            return
        end = {
            'acquisition': AcquisitionOutcome(acquisition).value,
            'cleanup': CleanupOutcome(cleanup).value,
            'detail': detail,
            'points_committed': len(self._committed),
        }
        if extra:
            end.update(extra)
        self.close()
        write_json_atomic(self.run_dir / 'end.json', end, fsync=self._fsync)

    def update_end(self, **fields: Any) -> None:
        """Amend ``end.json`` after finishing (e.g. the cleanup outcome)."""
        path = self.run_dir / 'end.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data.update(fields)
        write_json_atomic(path, data, fsync=self._fsync)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for fh in [self._control_fh, self._commit_fh, *self._sample_fh.values()]:
            try:
                fh.close()
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------------- helpers
    def _sample_rows(self, name: str, point: int, samples: Sequence[Sample]) -> np.ndarray:
        dtype = self._dtypes[name]
        rows = np.zeros(len(samples), dtype=dtype)
        for row, sample in zip(rows, samples):
            row['point'] = point
            row['generation'] = int(sample.generation)
            row['sequence'] = int(sample.sequence)
            row['t_host'] = float(sample.t_host)
            row['device_t'] = _nan(sample.device_t)
            row['device_id'] = -1 if sample.device_id is None else int(sample.device_id)
            row['valid'] = 1 if sample.valid else 0
            row['verified'] = 1 if sample.verification == Verification.VERIFIED else 0
            row['profile'] = _encode(sample.profile_id, _PROFILE_LEN)
            row['reason'] = _encode(sample.reason, _REASON_LEN)
            for quantity in self._quantities[name]:
                value = sample.values.get(quantity.name)
                try:
                    row[quantity_field(quantity.name)] = _nan(value)
                except (TypeError, ValueError):
                    row[quantity_field(quantity.name)] = math.nan
        return rows

    @staticmethod
    def _write_payload(fh, raw: bytes) -> None:
        fh.write(raw)
        fh.flush()


def _merge_named(existing, names: Sequence[str]) -> List[Dict[str, Any]]:
    by_name = {}
    for entry in existing or ():
        by_name[entry['name']] = dict(entry)
    return [dict(by_name.get(name, {}), name=name) for name in names]


def _merge_instruments(existing, quantities) -> Dict[str, Dict[str, Any]]:
    merged = {name: dict(info) for name, info in (existing or {}).items()}
    for name, qs in quantities.items():
        entry = merged.setdefault(name, {})
        entry['quantities'] = [
            {
                'name': q.name, 'unit': q.unit, 'quantity': q.quantity,
                'valid_min': None if math.isinf(q.valid_min) else q.valid_min,
                'valid_max': None if math.isinf(q.valid_max) else q.valid_max,
            }
            for q in qs
        ]
    return merged


def quantities_from_metadata(entry: Mapping[str, Any]) -> tuple:
    return tuple(
        QuantitySpec(
            name=q['name'], unit=q['unit'], quantity=q['quantity'],
            valid_min=-math.inf if q.get('valid_min') is None else q['valid_min'],
            valid_max=math.inf if q.get('valid_max') is None else q['valid_max'],
        )
        for q in entry.get('quantities', ())
    )


# ------------------------------------------------------------------- reading
@dataclass
class JournalContents:
    """The validated content of a run directory."""

    run_dir: Path
    metadata: Dict[str, Any]
    end: Optional[Dict[str, Any]]
    #: Valid commit records, in commit order.
    commits: List[Dict[str, Any]]
    controls: np.ndarray
    #: Per instrument: every sample row referenced by a valid commit.
    samples: Dict[str, np.ndarray]
    #: Why reading stopped early or what was ignored.
    problems: List[str] = field(default_factory=list)

    @property
    def damaged(self) -> bool:
        """Committed points were lost, not just an uncommitted tail.

        A finished run (``end.json`` present) never has a torn tail, so any
        problem there is damage; so is any problem other than the torn tail,
        and fewer valid points than the writer recorded committing.
        """
        if any(problem != TORN_TAIL for problem in self.problems):
            return True
        if self.end is not None:
            if self.problems:
                return True
            expected = self.end.get('points_committed')
            if expected is not None and int(expected) != len(self.commits):
                return True
        return False

    @property
    def acquisition(self) -> AcquisitionOutcome:
        if self.damaged:
            return AcquisitionOutcome.CORRUPT
        if self.end is None:
            return AcquisitionOutcome.INTERRUPTED
        return AcquisitionOutcome(self.end.get('acquisition', 'interrupted'))


def read_journal(run_dir: Path) -> JournalContents:
    run_dir = Path(run_dir)
    try:
        metadata = json.loads((run_dir / 'run.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise JournalError(f'not a run journal (no run.json): {run_dir}') from None
    journal = metadata.get('journal', {})
    if journal.get('format') != JOURNAL_FORMAT:
        raise JournalError(f'not a run journal: {run_dir}')
    if journal.get('version') != JOURNAL_VERSION:
        raise JournalError(f'unsupported journal version {journal.get("version")!r}')

    end_path = run_dir / 'end.json'
    end = json.loads(end_path.read_text(encoding='utf-8')) if end_path.exists() else None

    instruments = metadata.get('instruments', {})
    dtypes = {
        name: sample_dtype(quantities_from_metadata(entry))
        for name, entry in instruments.items()
    }
    control_raw = _read_bytes(run_dir / 'controls.bin')
    sample_raw = {name: _read_bytes(run_dir / 'samples' / f'{name}.bin') for name in dtypes}

    problems: List[str] = []
    commits: List[Dict[str, Any]] = []
    control_blocks: List[np.ndarray] = []
    sample_blocks: Dict[str, List[np.ndarray]] = {name: [] for name in dtypes}
    seen = set()

    log = _read_bytes(run_dir / 'commits.log')
    lines = log.split(b'\n')
    if lines and lines[-1] != b'':
        problems.append(TORN_TAIL)
    for number, raw_line in enumerate(lines[:-1], start=1):
        record, reason = _parse_commit(raw_line)
        if record is None:
            problems.append(f'commit record {number}: {reason}; stopped reading')
            break
        blocks, reason = _extract(record, control_raw, sample_raw, dtypes)
        if blocks is None:
            problems.append(
                f'commit record {number} (point {record.get("point")}): '
                f'{reason}; stopped reading'
            )
            break
        if record['point'] in seen:
            problems.append(f'point {record["point"]} committed twice; stopped reading')
            break
        seen.add(record['point'])
        control_block, per_instrument = blocks
        control_blocks.append(control_block)
        for name, block in per_instrument.items():
            sample_blocks[name].append(block)
        commits.append(record)

    controls = (
        np.concatenate(control_blocks) if control_blocks
        else np.zeros(0, dtype=CONTROL_DTYPE)
    )
    samples = {
        name: np.concatenate(blocks) if blocks else np.zeros(0, dtype=dtypes[name])
        for name, blocks in sample_blocks.items()
    }
    return JournalContents(
        run_dir=run_dir, metadata=metadata, end=end, commits=commits,
        controls=controls, samples=samples, problems=problems,
    )


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b''


def _parse_commit(raw_line: bytes):
    try:
        text, crc = raw_line.decode('utf-8').rsplit('\t', 1)
    except (UnicodeDecodeError, ValueError):
        return None, 'malformed line'
    if _line_crc(text) != crc:
        return None, 'checksum mismatch'
    try:
        record = json.loads(text)
    except json.JSONDecodeError:
        return None, 'invalid JSON'
    if record.get('v') != JOURNAL_VERSION or 'point' not in record:
        return None, 'unknown record version'
    return record, ''


def _extract(record, control_raw: bytes, sample_raw: Mapping[str, bytes], dtypes):
    extent = record.get('controls') or {}
    block, reason = _slice(control_raw, CONTROL_DTYPE, extent)
    if block is None:
        return None, f'controls: {reason}'
    per_instrument = {}
    for name, window in (record.get('windows') or {}).items():
        if name not in dtypes:
            return None, f'unknown instrument {name!r}'
        sblock, reason = _slice(sample_raw[name], dtypes[name], window)
        if sblock is None:
            return None, f'samples of {name}: {reason}'
        per_instrument[name] = sblock
    return (block, per_instrument), ''


def _slice(raw: bytes, dtype: np.dtype, extent: Mapping[str, Any]):
    try:
        offset, count, crc = int(extent['offset']), int(extent['count']), int(extent['crc'])
    except (KeyError, TypeError, ValueError):
        return None, 'missing extent'
    start, stop = offset * dtype.itemsize, (offset + count) * dtype.itemsize
    if offset < 0 or count < 0 or stop > len(raw):
        return None, 'extent outside the payload file'
    chunk = raw[start:stop]
    if (zlib.crc32(chunk) & 0xFFFFFFFF) != crc:
        return None, 'payload checksum mismatch'
    return np.frombuffer(chunk, dtype=dtype).copy(), ''
