"""Which calibration a setup file makes active: the per-laser
``managerProperties.calibrationId`` reference to a record in the global store.

Precedence (``laser_power.resolve_laser_calibration``): ``calibrationId``
over ``calibCsvPath``. ``calibrationId: "raw"`` runs the laser on raw units
even when ``calibCsvPath`` is set; no ``calibrationId`` (or ``""``) leaves
``calibCsvPath`` in charge, exactly as before.

Writing touches only those keys, in the file on disk: the setup is read from
disk, the ``calibrationId`` of the named lasers changed, and the file written
back atomically. It does *not* go through ``configfiletools.saveSetupInfo``,
which writes the whole in-memory setup -- that would also flush whatever else
the session holds in memory but has not saved, and undo edits made to the
file on disk since startup (in the config editor, say).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Dict, Mapping, Optional

from .record import RAW_UNITS_ID

#: Session selection values: a record id, ``RAW_UNITS_ID``, or this (no
#: ``calibrationId``: ``calibCsvPath`` -- if any -- decides).
FOLLOW_CSV_PATH = ''


def selection_from_properties(properties: Optional[Mapping]) -> str:
    value = str((properties or {}).get('calibrationId') or '').strip()
    return value


def read_setup_selection(setup_path) -> Dict[str, Dict[str, str]]:
    """``{laser: {'calibrationId': ..., 'calibCsvPath': ...}}`` as the file on
    disk says now ({} when it cannot be read)."""
    try:
        with open(setup_path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    result = {}
    for name, laser in (data.get('lasers') or {}).items():
        props = (laser or {}).get('managerProperties') or {}
        result[str(name)] = {
            'calibrationId': selection_from_properties(props),
            'calibCsvPath': str(props.get('calibCsvPath') or ''),
        }
    return result


def write_setup_selection(setup_path, selection: Mapping[str, str]) -> Dict[str, str]:
    """Set ``calibrationId`` of each named laser in the setup file on disk
    (``FOLLOW_CSV_PATH`` removes the key). Returns what was written; raises
    ``KeyError`` for a laser the file does not have and ``OSError`` /
    ``ValueError`` when the file cannot be read or written."""
    path = Path(setup_path)
    with open(path, 'r', encoding='utf-8') as fh:
        data = json.load(fh)
    lasers = data.get('lasers') or {}
    written = {}
    for name, value in selection.items():
        if name not in lasers:
            raise KeyError(f'the setup file has no laser {name!r}')
        laser = lasers[name]
        props = laser.get('managerProperties')
        if props is None:
            props = laser['managerProperties'] = {}
        value = str(value or '').strip()
        if value == FOLLOW_CSV_PATH:
            props.pop('calibrationId', None)
        else:
            props['calibrationId'] = value
        written[name] = value
    text = json.dumps(data, indent=4)
    descriptor, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp',
                                             dir=str(path.parent))
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as fh:
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
    return written


def describe_selection(value: str, csv_path: str = '') -> str:
    if value == RAW_UNITS_ID:
        return 'raw units'
    if value:
        return value
    if csv_path:
        return f'calibCsvPath {csv_path}'
    return 'no calibration'
