"""The calibration record: one typed, human-readable description of a
measured relation between what ImSwitch sets and what physically happens.

A record says *what* it maps (``input`` → ``output``, each a quantity with a
unit), *for which device* (``device``), *with which data* (``data``: a
table today; a scalar factor or an affine transform fit the same shape),
*where it may be used* (``validRange`` of the input, ``extrapolation``) and
*how it came about* (``measuredAt``, ``comment``, ``provenance``).

``contentHash`` covers exactly the fields that decide a number derived from
the record (kind, both quantities and units, the data, the valid range and
the extrapolation policy). Annotations -- where it was measured, the free
comment, the device label, the provenance -- are excluded, so correcting a
comment or a location never makes a saved state look as if it had been
recorded under a different calibration.

Qt-free and hardware-free: used by laser managers, the laser controller, the
Calibrations dialog and tests alike.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import re
import secrets
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Mapping, Optional, Tuple

#: Written into every record file; a reader refuses other schemas.
SCHEMA = 'imswitch.calibration'
SCHEMA_VERSION = 1

#: Laser power: raw drive (V, AOTF amplitude, ...) → optical power.
KIND_LASER_POWER = 'laser_power'
#: Kinds a record can hold. Only ``laser_power`` has a consumer yet; the
#: others are reserved so positioner factors, pixel sizes and detector
#: transforms can move into the same store without a format change.
KNOWN_KINDS = (KIND_LASER_POWER, 'scalar_factor', 'affine_transform', 'table')

#: Outside the measured range a request is refused -- never extrapolated,
#: never clamped. The only policy implemented.
EXTRAPOLATION_REFUSE = 'refuse'

#: The ``calibrationId`` value that means "run this laser on raw units even
#: though the setup names a ``calibCsvPath``".
RAW_UNITS_ID = 'raw'


class CalibrationError(ValueError):
    """Base class of every calibration problem."""


class CalibrationFormatError(CalibrationError):
    """A file or dict is not a readable calibration."""


class CalibrationInvalid(CalibrationError):
    """A readable calibration that cannot be applied (too few points, flat,
    not monotonic, non-finite values, wrong units ...)."""


class CalibrationRangeError(CalibrationError):
    """A request outside the calibration's measured range was refused."""


@dataclass(frozen=True)
class Quantity:
    """One side of a mapping: ``quantity`` (``'drive'``, ``'optical power'``)
    and its ``unit`` (``'V'``, ``'arb'``, ``'W'``, ``'mW'``; ``''`` when the
    source did not say)."""
    quantity: str
    unit: str = ''

    def to_dict(self) -> Dict[str, str]:
        return {'quantity': self.quantity, 'unit': self.unit}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> 'Quantity':
        return cls(str(data.get('quantity', '')), str(data.get('unit', '') or ''))


@dataclass(frozen=True)
class DeviceIdentity:
    """Which device a calibration was made for.

    The store is shared by every setup on the computer, so a bare name is not
    enough: two setups may call different lasers "488". ``wavelengthNm`` and
    ``managerClass`` (a hint) narrow it down, ``identifier`` carries a serial
    or hardware channel where the manager exposes one."""
    name: str = ''
    wavelengthNm: Optional[float] = None
    managerClass: str = ''
    identifier: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {'name': self.name, 'wavelengthNm': self.wavelengthNm,
                'managerClass': self.managerClass, 'identifier': self.identifier}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> 'DeviceIdentity':
        wavelength = data.get('wavelengthNm')
        return cls(name=str(data.get('name', '') or ''),
                   wavelengthNm=_float_or_none(wavelength),
                   managerClass=str(data.get('managerClass', '') or ''),
                   identifier=str(data.get('identifier', '') or ''))


@dataclass(frozen=True)
class Provenance:
    """How the record came about. ``tool`` names the writer
    (``'measureLaserPowerLut'``, ``'aa_aotf_calibration utility'``,
    ``'imported legacy CSV'``, ...); ``header`` keeps every header field of
    an imported file that has no field of its own, and ``comments`` every
    free comment line, so an export reproduces them."""
    created: str = ''
    tool: str = ''
    referenceInstrument: str = ''
    sourceRun: str = ''
    sourceFile: str = ''
    imswitchVersion: str = ''
    header: Tuple[Tuple[str, str], ...] = ()
    comments: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {'created': self.created, 'tool': self.tool,
                'referenceInstrument': self.referenceInstrument,
                'sourceRun': self.sourceRun, 'sourceFile': self.sourceFile,
                'imswitchVersion': self.imswitchVersion,
                # An object reads best; a repeated key (rare) keeps a list.
                'header': (dict(self.header)
                           if len({k for k, _ in self.header}) == len(self.header)
                           else [[k, v] for k, v in self.header]),
                'comments': list(self.comments)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> 'Provenance':
        return cls(created=str(data.get('created', '') or ''),
                   tool=str(data.get('tool', '') or ''),
                   referenceInstrument=str(data.get('referenceInstrument', '') or ''),
                   sourceRun=str(data.get('sourceRun', '') or ''),
                   sourceFile=str(data.get('sourceFile', '') or ''),
                   imswitchVersion=str(data.get('imswitchVersion', '') or ''),
                   header=tuple((str(k), str(v)) for k, v in _pairs(data.get('header'))),
                   comments=tuple(str(c) for c in data.get('comments') or ()))


@dataclass(frozen=True)
class Calibration:
    """One calibration record (see the module docstring).

    For ``laser_power``: ``input`` is the raw drive, ``output`` the optical
    power, ``data = {'columns': [...], 'table': [[drive, power, ...], ...]}``
    with rows in the order they were measured or written (that order is part
    of the legacy percent semantics, so it is kept), ``validRange`` the
    measured drive span."""
    id: str
    kind: str
    device: DeviceIdentity
    input: Quantity
    output: Quantity
    data: Mapping[str, Any]
    validRange: Optional[Tuple[float, float]] = None
    extrapolation: str = EXTRAPOLATION_REFUSE
    measuredAt: str = ''
    comment: str = ''
    provenance: Provenance = field(default_factory=Provenance)

    # -- identity ---------------------------------------------------------

    def contentHash(self) -> str:
        """sha256 over the physically meaningful fields (not annotations)."""
        # Column labels are annotation: renaming one changes no number.
        data = {k: v for k, v in _canonical(self.data).items() if k != 'columns'}
        payload = {
            'kind': self.kind,
            'input': self.input.to_dict(),
            'output': self.output.to_dict(),
            'data': data,
            'validRange': list(self.validRange) if self.validRange else None,
            'extrapolation': self.extrapolation,
        }
        text = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
        return 'sha256:' + hashlib.sha256(text.encode('utf-8')).hexdigest()

    def shortHash(self) -> str:
        return self.contentHash().split(':', 1)[1][:12]

    def withAnnotations(self, *, measuredAt: Optional[str] = None,
                        comment: Optional[str] = None) -> 'Calibration':
        """A copy with the user-editable annotations changed (hash unchanged)."""
        return replace(
            self,
            measuredAt=self.measuredAt if measuredAt is None else _one_line(measuredAt),
            comment=self.comment if comment is None else _one_line(comment),
        )

    # -- laser power conveniences ----------------------------------------

    @property
    def table(self) -> List[List[float]]:
        return [list(row) for row in (self.data.get('table') or [])]

    @property
    def columns(self) -> List[str]:
        return [str(c) for c in (self.data.get('columns') or [])]

    def title(self) -> str:
        """Short human label: where, comment, date."""
        bits = [self.measuredAt or 'location not recorded']
        if self.comment:
            bits.append(self.comment)
        date = self.provenance.created[:10]
        if date:
            bits.append(date)
        return ' · '.join(bits)

    # -- (de)serialisation -------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            'schema': SCHEMA,
            'schemaVersion': SCHEMA_VERSION,
            'id': self.id,
            'kind': self.kind,
            'device': self.device.to_dict(),
            'input': self.input.to_dict(),
            'output': self.output.to_dict(),
            'validRange': list(self.validRange) if self.validRange else None,
            'extrapolation': self.extrapolation,
            'measuredAt': self.measuredAt,
            'comment': self.comment,
            'provenance': self.provenance.to_dict(),
            'contentHash': self.contentHash(),
            'data': _canonical(self.data),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> 'Calibration':
        if not isinstance(data, Mapping):
            raise CalibrationFormatError('a calibration record is a JSON object')
        if data.get('schema') != SCHEMA:
            raise CalibrationFormatError(
                f'not a calibration record (schema {data.get("schema")!r}, '
                f'expected {SCHEMA!r})')
        version = data.get('schemaVersion')
        if not isinstance(version, int) or version > SCHEMA_VERSION:
            raise CalibrationFormatError(
                f'calibration schema version {version!r} is newer than this '
                f'ImSwitch understands ({SCHEMA_VERSION})')
        try:
            record_id = str(data['id'])
            kind = str(data['kind'])
            payload = data['data']
            if not isinstance(payload, Mapping):
                raise CalibrationFormatError('"data" must be an object')
            valid = data.get('validRange')
            record = cls(
                id=record_id, kind=kind,
                device=DeviceIdentity.from_dict(data.get('device') or {}),
                input=Quantity.from_dict(data.get('input') or {}),
                output=Quantity.from_dict(data.get('output') or {}),
                data=_canonical(payload),
                validRange=(float(valid[0]), float(valid[1])) if valid else None,
                extrapolation=str(data.get('extrapolation') or EXTRAPOLATION_REFUSE),
                measuredAt=str(data.get('measuredAt', '') or ''),
                comment=str(data.get('comment', '') or ''),
                provenance=Provenance.from_dict(data.get('provenance') or {}),
            )
        except CalibrationFormatError:
            raise
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise CalibrationFormatError(f'malformed calibration record: {exc}') from exc
        if not record.id.strip():
            raise CalibrationFormatError('calibration record without an id')
        stored = data.get('contentHash')
        if stored and stored != record.contentHash():
            raise CalibrationFormatError(
                f'calibration {record.id}: content hash does not match its data '
                f'(file edited by hand?). Stored {stored}, computed '
                f'{record.contentHash()}. Re-import it from CSV to accept the edit.')
        return record

    def to_json(self) -> str:
        """Pretty JSON with one table row per line (diffable, hand-readable)."""
        body = self.to_dict()
        rows = body['data'].get('table')
        if rows is not None:
            body['data'] = dict(body['data'], table='__TABLE__')
        text = json.dumps(body, indent=2, ensure_ascii=False, allow_nan=False)
        if rows is not None:
            rendered = ',\n'.join(
                '      [' + ', '.join(_num(v) for v in row) + ']' for row in rows)
            text = text.replace('"__TABLE__"', '[\n' + rendered + '\n    ]')
        return text + '\n'

    @classmethod
    def from_json(cls, text: str) -> 'Calibration':
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CalibrationFormatError(f'not JSON: {exc}') from exc
        return cls.from_dict(data)


def new_calibration_id(device_name: str = '', when: Optional[_dt.datetime] = None) -> str:
    """A fresh, readable, unique id: ``<laser>-<YYYYmmdd>-<6 hex>``."""
    when = when or _dt.datetime.now()
    slug = slugify(device_name) or 'calibration'
    return f'{slug}-{when:%Y%m%d}-{secrets.token_hex(3)}'


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec='seconds')


def slugify(text: str, *, limit: int = 40) -> str:
    slug = re.sub(r'[^A-Za-z0-9.]+', '-', str(text or '')).strip('-.').lower()
    return slug[:limit].strip('-.')


def _one_line(text: str) -> str:
    return ' '.join(str(text or '').split())


def _num(value: Any) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    value = float(value)
    if not math.isfinite(value):
        raise CalibrationFormatError('non-finite number in a calibration table')
    return repr(value)      # shortest text that reads back as the same float


def _canonical(value: Any) -> Any:
    """Plain JSON types (tuples → lists, numpy scalars → floats)."""
    if isinstance(value, Mapping):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if hasattr(value, 'tolist'):
        return _canonical(value.tolist())
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def _pairs(value: Any):
    if isinstance(value, Mapping):
        return list(value.items())
    return list(value or ())


def _float_or_none(value: Any) -> Optional[float]:
    if value in (None, ''):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


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
