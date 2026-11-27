"""The two-column ``calibCsvPath`` file: import into a calibration record and
export back, without losing anything.

The format labs already have and edit by hand: ``#`` comment lines, then
whitespace-separated rows read with ``np.loadtxt`` -- column 0 the raw drive
(volts, AOTF amplitude, ...), column 1 the measured power; further columns
(a standard deviation, say) are kept but not used.

Two writers exist and both are read:

* ``measureLaserPowerLut`` (``imcommon/algorithms/power_lut.py``) writes
  ``# key: value`` lines (``laser``, ``setting unit``, ``laser wavelength
  nm``, ``meter``, ``plane``, ``zeroed in the dark``, ``source run``,
  ``created``, ``imswitch version``, ``notes``, ``columns: setting power_W``)
  -- power in **W**.
* ``utility_scripts/aa_aotf_calibration.py`` writes one header line
  ``Raw AOTF power, measured optical power [mW] | laser=…, wavelength=… nm,
  frequency=… MHz`` and the notes as a footer after the rows -- power in
  **mW**, drive in AOTF amplitude.

**Units when the file does not say.** The power unit is taken from, in this
order: a ``power unit:`` field, a ``power_<unit>`` column name, a bracketed
unit (``[mW]``) anywhere in the header. A file with none of these keeps an
empty power unit: it still drives the laser in percent exactly as before, but
no absolute power is shown -- the unit is never guessed. The drive unit comes
from ``setting unit:`` / ``drive unit:``, a ``setting_<unit>`` column, the AA
utility's ``Raw AOTF`` signature (``arb``), and otherwise is *assumed* to be
the raw unit of the laser it is used for (what the old loader did silently);
that assumption is recorded in the record's provenance.

Export writes every record field as a ``# key: value`` line, every header
field and free comment of the original file, then the rows with ``repr``
precision, so ``import(export(record))`` gives back the same record (same
content hash, same annotations).
"""
from __future__ import annotations

import io
import re
from typing import Dict, List, Optional, Tuple

import numpy as np

from .record import (
    KIND_LASER_POWER,
    Calibration,
    CalibrationFormatError,
    DeviceIdentity,
    Provenance,
    Quantity,
    _float_or_none,
    _num,
    _one_line,
)

TOOL_MEASURE = 'measureLaserPowerLut'
TOOL_AA_UTILITY = 'aa_aotf_calibration utility'
TOOL_LEGACY = 'imported legacy CSV'

#: ``# key: value`` keys that map onto record fields (lower case).
_KEY_ID = ('calibration id',)
_KEY_LASER = ('laser', 'laser name')
_KEY_WAVELENGTH = ('laser wavelength nm', 'wavelength nm', 'wavelength')
_KEY_MANAGER = ('manager class',)
_KEY_IDENTIFIER = ('device identifier',)
_KEY_DRIVE_UNIT = ('setting unit', 'drive unit')
_KEY_POWER_UNIT = ('power unit',)
_KEY_PLANE = ('plane', 'measured at', 'location')
_KEY_COMMENT = ('comment',)
_KEY_NOTES = ('notes',)
_KEY_METER = ('meter', 'reference instrument')
_KEY_CREATED = ('created',)
_KEY_TOOL = ('tool',)
_KEY_RUN = ('source run',)
_KEY_SOURCE = ('source file',)
_KEY_VERSION = ('imswitch version',)
_KEY_COLUMNS = ('columns',)
#: Regenerated on export; not kept as header fields.
_KEY_IGNORED = ('format', 'content hash')

_FORMAT_LINE = ('imswitch laser power calibration (calibCsvPath compatible: '
                'column 0 raw drive, column 1 power)')

_KEY_VALUE = re.compile(r'^([A-Za-z][A-Za-z0-9 _.()/-]{0,40}?)\s*:\s?(.*)$')
_BRACKET_UNIT = re.compile(r'\[\s*(W|mW|uW|µW|nW)\s*\]')
_POWER_COLUMN = re.compile(r'^power_(W|mW|uW|µW|nW)$', re.IGNORECASE)
_DRIVE_COLUMN = re.compile(r'^(?:setting|drive)_(\w+)$', re.IGNORECASE)


def _norm_unit(unit: str) -> str:
    unit = (unit or '').strip()
    if unit.lower() in ('w', 'mw', 'uw', 'µw', 'nw'):
        return {'w': 'W', 'mw': 'mW', 'uw': 'uW', 'µw': 'uW', 'nw': 'nW'}[unit.lower()]
    return unit


def read_legacy_csv(path, **kwargs) -> Calibration:
    """:func:`parse_legacy_csv` on a file (``sourceFile`` set to ``path``)."""
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            text = fh.read()
    except UnicodeDecodeError:
        with open(path, 'r', encoding='latin-1') as fh:
            text = fh.read()
    kwargs.setdefault('source_file', str(path))
    return parse_legacy_csv(text, **kwargs)


def parse_legacy_csv(text: str, *, source_file: str = '', laser_name: str = '',
                     drive_unit: str = '', wavelength_nm: Optional[float] = None,
                     record_id: str = '') -> Calibration:
    """A laser power calibration record from the text of a ``calibCsvPath``
    file. The keyword arguments fill what the file does not say (the file
    wins where it does); ``drive_unit`` is the raw unit of the laser the
    file is meant for. Raises :class:`CalibrationFormatError` when the rows
    cannot be read (the validity of the curve is checked separately, see
    ``laser_power.validate_laser_power``)."""
    try:
        rows = np.loadtxt(io.StringIO(text), ndmin=2)
    except Exception as exc:  # noqa: BLE001 - every loadtxt failure is a format error
        raise CalibrationFormatError(f'rows are not numbers: {exc}') from exc
    if rows.size == 0:
        raise CalibrationFormatError('no data rows')
    if rows.shape[1] < 2:
        raise CalibrationFormatError(
            f'{rows.shape[1]} column(s); a calibration needs the raw drive in column 0 '
            f'and the measured power in column 1')

    header_lines, footer_lines = _comment_blocks(text)
    fields: Dict[str, str] = {}
    extra: List[Tuple[str, str]] = []
    free: List[str] = []
    bracket_unit = ''
    aa_signature = False
    for body in header_lines:
        unit_match = _BRACKET_UNIT.search(body)
        if unit_match and not bracket_unit:
            bracket_unit = _norm_unit(unit_match.group(1))
        if '|' in body and '=' in body and ':' not in body.split('|', 1)[0]:
            # aa_aotf_calibration utility: "<description> | k=v, k=v"
            description, _, pairs = body.partition('|')
            aa_signature = aa_signature or 'raw aotf' in description.lower()
            for pair in pairs.split(','):
                key, eq, value = pair.partition('=')
                if eq:
                    key = key.strip().lower()
                    value = value.strip()
                    if key == 'wavelength':
                        value = re.sub(r'\s*nm$', '', value)
                        fields.setdefault('laser wavelength nm', value)
                    elif key == 'laser':
                        fields.setdefault('laser', value)
                    else:
                        extra.append((key, value))
            continue
        match = _KEY_VALUE.match(body)
        if match:
            key, value = match.group(1).strip(), match.group(2).strip()
            lower = key.lower()
            if lower in _known_keys():
                fields.setdefault(lower, value)
            elif lower not in _KEY_IGNORED:
                extra.append((key, value))
            continue
        if body:
            free.append(body)

    def get(keys) -> str:
        for key in keys:
            if fields.get(key):
                return fields[key]
        return ''

    columns = _columns(get(_KEY_COLUMNS), rows.shape[1])
    power_unit = _norm_unit(get(_KEY_POWER_UNIT))
    if not power_unit:
        for name in columns:
            match = _POWER_COLUMN.match(name)
            if match:
                power_unit = _norm_unit(match.group(1))
                break
    power_unit = power_unit or bracket_unit

    file_drive_unit = get(_KEY_DRIVE_UNIT)
    if not file_drive_unit:
        for name in columns:
            match = _DRIVE_COLUMN.match(name)
            if match:
                file_drive_unit = match.group(1)
                break
    if not file_drive_unit and aa_signature:
        file_drive_unit = 'arb'
    assumed = not file_drive_unit and bool(drive_unit)
    unit = file_drive_unit or drive_unit
    if assumed:
        extra.append(('drive unit assumed', f'{drive_unit} (the file does not say)'))

    comment = get(_KEY_COMMENT)
    notes = get(_KEY_NOTES)
    footer = ' | '.join(line for line in footer_lines if line)
    if not comment:
        comment = notes or footer
        notes_used = True
    else:
        notes_used = False
    if notes and not notes_used:
        extra.append(('notes', notes))
    if footer and comment != footer:
        free.append(footer)

    if get(_KEY_TOOL):
        tool = get(_KEY_TOOL)
    elif 'calibcsvpath lut' in (fields.get('format', '') + ' '.join(header_lines)).lower() \
            or get(_KEY_RUN):
        tool = TOOL_MEASURE
    elif aa_signature:
        tool = TOOL_AA_UTILITY
    else:
        tool = TOOL_LEGACY

    drive = rows[:, 0]
    name = get(_KEY_LASER) or laser_name
    record = Calibration(
        id=get(_KEY_ID) or record_id or '',
        kind=KIND_LASER_POWER,
        device=DeviceIdentity(
            name=name,
            wavelengthNm=_float_or_none(get(_KEY_WAVELENGTH)) if get(_KEY_WAVELENGTH)
            else _float_or_none(wavelength_nm),
            managerClass=get(_KEY_MANAGER),
            identifier=get(_KEY_IDENTIFIER),
        ),
        input=Quantity('drive', unit),
        output=Quantity('optical power', power_unit),
        data={'columns': columns, 'table': rows.tolist()},
        validRange=(float(np.min(drive)), float(np.max(drive))) if np.all(np.isfinite(drive))
        else None,
        measuredAt=_one_line(get(_KEY_PLANE)),
        comment=_one_line(comment),
        provenance=Provenance(
            created=get(_KEY_CREATED), tool=tool,
            referenceInstrument=get(_KEY_METER), sourceRun=get(_KEY_RUN),
            sourceFile=get(_KEY_SOURCE) or str(source_file or ''),
            imswitchVersion=get(_KEY_VERSION),
            header=tuple(extra), comments=tuple(free),
        ),
    )
    return record


def export_legacy_csv(record: Calibration) -> str:
    """The record as a ``calibCsvPath`` file (``#`` header, then rows)."""
    if record.kind != KIND_LASER_POWER:
        raise CalibrationFormatError(f'only laser_power records export to CSV, not {record.kind}')
    prov = record.provenance
    lines = [f'format: {_FORMAT_LINE}']

    def add(key, value):
        if value not in (None, ''):
            lines.append(f'{key}: {_one_line(str(value))}')

    add('calibration id', record.id)
    add('laser', record.device.name)
    if record.device.wavelengthNm is not None:
        add('laser wavelength nm', _num(record.device.wavelengthNm))
    add('manager class', record.device.managerClass)
    add('device identifier', record.device.identifier)
    add('setting unit', record.input.unit)
    add('power unit', record.output.unit)
    add('plane', record.measuredAt)
    add('comment', record.comment)
    add('meter', prov.referenceInstrument)
    add('created', prov.created)
    add('tool', prov.tool)
    add('source run', prov.sourceRun)
    add('source file', prov.sourceFile)
    add('imswitch version', prov.imswitchVersion)
    for key, value in prov.header:
        lines.append(f'{key}: {_one_line(value)}')
    for comment in prov.comments:
        lines.append(_one_line(comment))
    lines.append(f'content hash: {record.contentHash()}')
    lines.append('columns: ' + ' '.join(record.columns or _columns('', len(record.table[0]))))
    out = ['# ' + line for line in lines]
    out += [' '.join(_num(v) for v in row) for row in record.table]
    return '\n'.join(out) + '\n'


def _known_keys():
    return set(_KEY_ID + _KEY_LASER + _KEY_WAVELENGTH + _KEY_MANAGER + _KEY_IDENTIFIER
               + _KEY_DRIVE_UNIT + _KEY_POWER_UNIT + _KEY_PLANE + _KEY_COMMENT
               + _KEY_NOTES + _KEY_METER + _KEY_CREATED + _KEY_TOOL + _KEY_RUN
               + _KEY_SOURCE + _KEY_VERSION + _KEY_COLUMNS + ('format',))


def _comment_blocks(text: str) -> Tuple[List[str], List[str]]:
    """Comment bodies before the first data row (header) and after it."""
    header: List[str] = []
    footer: List[str] = []
    seen_data = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#'):
            body = line.lstrip('#').strip()
            (footer if seen_data else header).append(body)
        else:
            seen_data = True
    return header, footer


def _columns(text: str, count: int) -> List[str]:
    tokens = text.split('(')[0].split() if text else []
    if len(tokens) >= count:
        return tokens[:count]
    defaults = ['drive', 'power'] + [f'column{i}' for i in range(2, count)]
    return tokens + defaults[len(tokens):count]


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
