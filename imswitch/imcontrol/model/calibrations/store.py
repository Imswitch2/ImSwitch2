"""The global calibration store: one folder of record files, shared by every
setup on this computer (``<ImSwitchConfig>/calibrations``).

One file per record, ``<laser>_<wavelength>nm_<where>_<date>_<id suffix>.calib.json``.
The name is chosen once, when the record is added, for people browsing the
folder; it never changes afterwards (editing the comment or the location
rewrites the file in place). The ``id`` *inside* the file is what setups
reference -- a renamed file still resolves.

Nothing is created or written at import time: the folder is resolved when a
store is used, and created on the first write.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import List, Optional

from .record import (
    Calibration,
    CalibrationError,
    CalibrationFormatError,
    new_calibration_id,
    now_iso,
    slugify,
)

SUFFIX = '.calib.json'


def default_store_root() -> Path:
    """``<ImSwitchConfig>/calibrations``, resolved through dirtools."""
    from imswitch.imcommon.model import dirtools

    return Path(dirtools.UserFileDirs.Calibrations)


@dataclass(frozen=True)
class StoredCalibration:
    record: Calibration
    path: Path


@dataclass(frozen=True)
class UnreadableCalibration:
    path: Path
    error: str


class CalibrationStore:
    """Read, add and annotate calibration records in one folder."""

    def __init__(self, root=None):
        self._root = Path(root) if root is not None else None

    @property
    def root(self) -> Path:
        return self._root if self._root is not None else default_store_root()

    # -- reading -----------------------------------------------------------

    def scan(self):
        """``(records, unreadable)``: every record file in the folder, and
        the files that could not be read (with the reason)."""
        records: List[StoredCalibration] = []
        unreadable: List[UnreadableCalibration] = []
        root = self.root
        if not root.is_dir():
            return records, unreadable
        seen = {}
        for path in sorted(root.glob('*' + SUFFIX)):
            try:
                record = Calibration.from_json(path.read_text(encoding='utf-8'))
            except (OSError, CalibrationError) as exc:
                unreadable.append(UnreadableCalibration(path, str(exc)))
                continue
            if record.id in seen:
                unreadable.append(UnreadableCalibration(
                    path, f'duplicate id {record.id!r} (also in {seen[record.id].name}); '
                          f'ignored'))
                continue
            seen[record.id] = path
            records.append(StoredCalibration(record, path))
        return records, unreadable

    def list(self) -> List[StoredCalibration]:
        return self.scan()[0]

    def get(self, record_id: str) -> Optional[StoredCalibration]:
        for stored in self.list():
            if stored.record.id == record_id:
                return stored
        return None

    # -- writing -----------------------------------------------------------

    def add(self, record: Calibration) -> StoredCalibration:
        """Store a new record (an id is assigned when it has none, a created
        time when it has none). Refuses an id already in the store."""
        if not record.id or record.id.startswith('csv:'):
            record = replace(record, id=new_calibration_id(record.device.name))
        if not record.provenance.created:
            record = replace(record, provenance=replace(record.provenance, created=now_iso()))
        if self.get(record.id) is not None:
            raise CalibrationFormatError(
                f'the store already holds a calibration with id {record.id!r}')
        root = self.root
        root.mkdir(parents=True, exist_ok=True)
        path = root / self.file_name(record)
        counter = 2
        while path.exists():
            path = root / self.file_name(record, counter)
            counter += 1
        _atomic_write(path, record.to_json())
        return StoredCalibration(record, path)

    def update_annotations(self, record_id: str, *, measuredAt: Optional[str] = None,
                           comment: Optional[str] = None) -> StoredCalibration:
        """Change where it was measured / the comment; the file keeps its
        name and the content hash does not change."""
        stored = self.get(record_id)
        if stored is None:
            raise CalibrationFormatError(f'no calibration with id {record_id!r}')
        record = stored.record.withAnnotations(measuredAt=measuredAt, comment=comment)
        _atomic_write(stored.path, record.to_json())
        return StoredCalibration(record, stored.path)

    @staticmethod
    def file_name(record: Calibration, counter: int = 0) -> str:
        bits = [slugify(record.device.name) or 'laser']
        if record.device.wavelengthNm is not None:
            bits.append(f'{record.device.wavelengthNm:g}nm')
        if record.measuredAt:
            bits.append(slugify(record.measuredAt, limit=24))
        created = record.provenance.created[:10].replace('-', '')
        if created:
            bits.append(created)
        bits.append(slugify(record.id.rsplit('-', 1)[-1], limit=8) or 'x')
        if counter:
            bits.append(str(counter))
        return '_'.join(bit for bit in bits if bit) + SUFFIX


def _atomic_write(path: Path, text: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp',
                                             dir=str(path.parent))
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


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
