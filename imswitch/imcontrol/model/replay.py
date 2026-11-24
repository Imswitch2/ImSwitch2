"""Replay: loading the settings stored in a recording back into the running setup.

This module is pure: no Qt, no hardware, no controllers.  It turns

* the attributes read from one detector group of a saved HDF5 file or Zarr
  store (:func:`parseReplayAttributes`), and
* a read-only snapshot of the devices of the running setup
  (:class:`SetupSnapshot`, built by the controller layer),

into a :class:`ReplayPlan`: one :class:`ReplayItem` per device the file refers
to, with its match status, compatible candidates in the current setup, the
proposed target and one :class:`ValueCheck` per stored value.  Nothing is
applied while parsing or resolving.  :func:`buildActions` then turns the plan,
as the user (or the unattended policy) left it, into the concrete list of
values to apply.  It is the single place where the safety rules hold:

* emission is never switched on -- a stored ``Enabled`` state is shown, never
  turned into an action;
* positions (stages, rotators) are applied only when ``plan.allowMoves`` is set;
* a value outside the target's range is applied only when the user chose
  ``clamp`` (applied at the range limit) or ``edit`` with an in-range value;
* a value is applied only to a target the plan resolved or the user picked.

The module also defines the recording metadata that makes replay robust and
traceable: the setup name and hash, the device inventory (essentials only, no
serial numbers, ports or paths), the metadata schema version and the replay
provenance written into later recordings.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# Recorded metadata (schema)
# --------------------------------------------------------------------------

#: Version of the replay-relevant metadata a recording carries.  Files written
#: before it existed carry none and are matched by device names only.
METADATA_SCHEMA_VERSION = 1

METADATA_CATEGORY = 'metadata'
SETUP_CATEGORY = 'setup'
REPLAY_CATEGORY = 'replay'

METADATA_SCHEMA_KEY = (METADATA_CATEGORY, 'schema_version')
SETUP_NAME_KEY = (SETUP_CATEGORY, 'name')
SETUP_HASH_KEY = (SETUP_CATEGORY, 'hash')
SETUP_INVENTORY_KEY = (SETUP_CATEGORY, 'inventory')

#: Per-laser recorded attributes added next to ``Value`` (schema 1).
LASER_UNITS_ATTR = 'Units'
LASER_RANGE_MIN_ATTR = 'RangeMin'
LASER_RANGE_MAX_ATTR = 'RangeMax'
#: Optional slot for a future laser power calibration identity:
#: ``{'id': str, 'contentHash': str, 'driveValue': float}``.  Nothing writes it
#: yet; when present it is shown in the replay dialog and carried into the
#: replay provenance, never interpreted.
LASER_CALIBRATION_ATTR = 'Calibration'

ROTATOR_CATEGORY = 'Rotator'
FLIP_MIRROR_CATEGORY = 'FlipMirror'
PULSE_GENERATOR_CATEGORY = 'PulseGenerator'

#: Replay provenance keys (category ``replay``) published after a replay, so
#: that every later recording says where its settings came from.
REPLAY_PROVENANCE_KEYS = (
    'source_file', 'source_dataset', 'source_format', 'source_software_version',
    'source_setup_name', 'source_setup_hash', 'source_start_time',
    'applied_at', 'mode', 'mapping',
)

#: Words that mark an inventory field as sensitive (serial numbers, ports,
#: paths, addresses).  Checked word by word on camelCase/snake_case names by
#: :func:`sanitizeInventory` and by the tests.
SENSITIVE_FIELD_WORDS = frozenset({
    'serial', 'sn', 'port', 'ports', 'com', 'path', 'paths', 'address', 'host',
    'ip', 'dir', 'directory', 'file', 'filename', 'url', 'mac', 'usb',
})

# Setup-file sections left out of the setup hash because they change without
# the hardware changing (keyboard shortcuts are saved into the setup file).
_HASH_EXCLUDED_SECTIONS = ('shortcuts',)

# --------------------------------------------------------------------------
# Kinds, statuses, actions
# --------------------------------------------------------------------------

LASER = 'laser'
POSITIONER = 'positioner'      # one item per positioner axis
DETECTOR = 'detector'
SCAN = 'scan'
SCAN_AXIS = 'scan_axis'        # a positioner named by the stored scan
TTL = 'ttl'                    # a TTL device named by the stored scan
ROTATOR = 'rotator'
FLIP_MIRROR = 'flip_mirror'
PULSE_GENERATOR = 'pulse_generator'

KIND_LABELS = {
    LASER: 'Lasers', POSITIONER: 'Positioners', DETECTOR: 'Detectors',
    SCAN: 'Scan', SCAN_AXIS: 'Scan axes', TTL: 'TTL lines',
    ROTATOR: 'Rotators', FLIP_MIRROR: 'Flip mirrors',
    PULSE_GENERATOR: 'Pulse generator',
}

#: Order in which the apply layer applies kinds: passive parameters first,
#: motion last.
APPLY_ORDER = (DETECTOR, SCAN, LASER, ROTATOR, POSITIONER)

EXACT = 'exact'
CAPABILITY_MISMATCH = 'capability_mismatch'
MISSING = 'missing'
NO_SUCH_KIND = 'no_such_kind'
NOT_REPLAYABLE = 'not_replayable'

STATUS_LABELS = {
    EXACT: 'matches',
    CAPABILITY_MISMATCH: 'same name, different device',
    MISSING: 'not in this setup',
    NO_SUCH_KIND: 'this setup has no such device',
    NOT_REPLAYABLE: 'shown only',
}

# Value check statuses
V_OK = 'ok'
V_OUT_OF_RANGE = 'out_of_range'
V_INVALID = 'invalid'          # cannot be applied to the target (option, type, ...)
V_SHOWN = 'shown'              # never applied by replay (emission, mirrors, ...)
V_NO_TARGET = 'no_target'      # item is skipped / has no target

# Value actions
APPLY = 'apply'
SKIP = 'skip'
CLAMP = 'clamp'
EDIT = 'edit'

SKIP_TARGET = None

_SCAN_DERIVED_STAGE_KEYS = frozenset({
    'positive_direction', 'scan_axis_devices', 'scan_axis_physical',
})
_SCAN_DEVICE_LIST_KEYS = ('target_device', 'scan_dim_target_device')

_WAVELENGTH_TOLERANCE_NM = 1.0


# --------------------------------------------------------------------------
# Value normalisation
# --------------------------------------------------------------------------

def plainValue(value: Any) -> Any:
    """Return ``value`` with numpy scalars/arrays and bytes turned into plain
    Python types, recursively.  HDF5 hands back ``numpy.bool_``,
    ``numpy.float64`` and arrays where Zarr hands back plain JSON types; the
    resolver compares and the controllers receive only plain values."""
    try:
        import numpy as np
    except Exception:  # pragma: no cover - numpy is a hard dependency
        np = None
    if np is not None:
        if isinstance(value, np.ndarray):
            return [plainValue(v) for v in value.tolist()]
        if isinstance(value, np.generic):
            return value.item()
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    if isinstance(value, tuple):
        return [plainValue(v) for v in value]
    if isinstance(value, list):
        return [plainValue(v) for v in value]
    if isinstance(value, dict):
        return {str(k): plainValue(v) for k, v in value.items()}
    return value


def _asFloat(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def _asBool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ('true', '1', 'yes', 'on'):
            return True
        if lowered in ('false', '0', 'no', 'off'):
            return False
    return None


# --------------------------------------------------------------------------
# Setup identity and inventory
# --------------------------------------------------------------------------

def computeSetupHash(setupDict: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical JSON of a setup file's contents.

    Only the hash is ever recorded, never the setup itself (it holds serial
    numbers and ports).  Sections that change without the hardware changing
    are left out, so a rebound shortcut does not make the same rig look new.
    """
    data = {k: v for k, v in dict(setupDict or {}).items()
            if k not in _HASH_EXCLUDED_SECTIONS}
    canonical = json.dumps(data, sort_keys=True, separators=(',', ':'), default=str)
    return 'sha256:' + hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _fieldWords(name: str) -> List[str]:
    spaced = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', str(name))
    return [w for w in re.split(r'[^A-Za-z0-9]+', spaced.lower()) if w]


def isSensitiveField(name: str) -> bool:
    """Whether a field name names a serial number, port, path or address."""
    return any(word in SENSITIVE_FIELD_WORDS for word in _fieldWords(name))


def sanitizeInventory(inventory: Any) -> Any:
    """Drop any inventory field whose name says serial, port, path, address...

    The inventory builder only ever writes whitelisted fields; this is the
    second line of defence for fields a future manager adds.
    """
    if isinstance(inventory, dict):
        return {key: sanitizeInventory(value) for key, value in inventory.items()
                if not isSensitiveField(key)}
    if isinstance(inventory, list):
        return [sanitizeInventory(v) for v in inventory]
    return inventory


def emptyInventory() -> Dict[str, Any]:
    return {
        'schema': METADATA_SCHEMA_VERSION,
        'lasers': {}, 'positioners': {}, 'detectors': {}, 'rotators': {},
        'flipMirrors': {}, 'pulseGenerators': {}, 'scan': None,
    }


@dataclass
class SetupSnapshot:
    """Read-only description of the running setup, for the resolver.

    ``inventory`` has the same shape as the recorded inventory.  Devices that
    failed at startup are either absent from it (they never made it into a
    manager) or listed in ``unavailable`` as ``(kind, name)`` (present but not
    connected); neither is offered as a replay target.  ``current`` holds the
    values the application currently publishes, keyed like shared attributes,
    for the "current -> stored" display.  ``laserEnabled`` names the lasers
    whose emission is on now (a value change on them changes emitted power).
    """
    setupName: Optional[str] = None
    setupHash: Optional[str] = None
    inventory: Dict[str, Any] = field(default_factory=emptyInventory)
    unavailable: set = field(default_factory=set)
    current: Dict[Tuple[str, ...], Any] = field(default_factory=dict)
    laserEnabled: set = field(default_factory=set)

    def devices(self, section: str) -> Dict[str, Dict[str, Any]]:
        return dict((self.inventory or {}).get(section) or {})

    def usable(self, kind: str, name: str) -> bool:
        return (kind, name) not in self.unavailable

    def scanInfo(self) -> Optional[Dict[str, Any]]:
        return (self.inventory or {}).get('scan')


# --------------------------------------------------------------------------
# Parsing a file
# --------------------------------------------------------------------------

@dataclass
class ParsedReplayFile:
    """The replay-relevant content of one detector group of a recording."""
    attrs: Dict[Tuple[str, ...], Any]
    inventory: Optional[Dict[str, Any]] = None
    setupName: Optional[str] = None
    setupHash: Optional[str] = None
    schemaVersion: Optional[int] = None
    softwareVersion: Optional[str] = None
    startTime: Optional[str] = None
    lasers: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    positioners: Dict[Tuple[str, str], Any] = field(default_factory=dict)
    detectors: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    scanStage: Dict[str, Any] = field(default_factory=dict)
    scanTTL: Dict[str, Any] = field(default_factory=dict)
    rotators: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    flipMirrors: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    pulseGenerators: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    informationalKeys: List[str] = field(default_factory=list)
    # Identity of the source, filled by the caller (file name only, no path).
    sourceFile: Optional[str] = None
    sourceDataset: Optional[str] = None
    sourceFormat: Optional[str] = None

    @property
    def hasInventory(self) -> bool:
        return isinstance(self.inventory, dict)


def _normalizeKey(key: Any) -> Tuple[str, ...]:
    if isinstance(key, tuple):
        return tuple(str(k) for k in key)
    return tuple(str(key).split(':'))


def parseReplayAttributes(attrs: Any) -> ParsedReplayFile:
    """Group the flat attributes of a recording by device.

    ``attrs`` is a mapping (or an iterable of ``(key, value)`` pairs, like a
    :class:`SharedAttributes`) with tuple or ``':'``-joined keys.  Values are
    normalised to plain Python types.  Nothing is interpreted against the
    running setup here.
    """
    items = attrs.items() if hasattr(attrs, 'items') else attrs
    flat: Dict[Tuple[str, ...], Any] = {}
    for key, value in items:
        flat[_normalizeKey(key)] = plainValue(value)

    parsed = ParsedReplayFile(attrs=flat)
    inventory = flat.get(SETUP_INVENTORY_KEY)
    if isinstance(inventory, str):
        try:
            inventory = json.loads(inventory)
        except (TypeError, ValueError):
            inventory = None
    parsed.inventory = inventory if isinstance(inventory, dict) else None
    name = flat.get(SETUP_NAME_KEY)
    parsed.setupName = str(name) if name not in (None, '') else None
    setupHash = flat.get(SETUP_HASH_KEY)
    parsed.setupHash = str(setupHash) if setupHash not in (None, '') else None
    schema = _asFloat(flat.get(METADATA_SCHEMA_KEY))
    parsed.schemaVersion = int(schema) if schema is not None else None
    version = flat.get(('acquisition', 'software_version'))
    parsed.softwareVersion = str(version) if version is not None else None
    start = flat.get(('acquisition', 'start_time'))
    parsed.startTime = str(start) if start is not None else None

    for key, value in flat.items():
        category = key[0]
        if category == 'Laser' and len(key) == 3:
            parsed.lasers.setdefault(key[1], {})[key[2]] = value
        elif category == 'Positioner' and len(key) == 4 and key[3] == 'Position':
            parsed.positioners[(key[1], key[2])] = value
        elif category == 'Detector' and len(key) == 3:
            parsed.detectors.setdefault(key[1], {'params': {}})[key[2]] = value
        elif category == 'Detector' and len(key) == 4 and key[2] == 'Param':
            parsed.detectors.setdefault(key[1], {'params': {}})['params'][key[3]] = value
        elif category == 'ScanStage' and len(key) == 2:
            parsed.scanStage[key[1]] = value
        elif category == 'ScanTTL' and len(key) == 2:
            parsed.scanTTL[key[1]] = value
        elif category == ROTATOR_CATEGORY and len(key) == 3:
            parsed.rotators.setdefault(key[1], {})[key[2]] = value
        elif category == FLIP_MIRROR_CATEGORY and len(key) == 3:
            parsed.flipMirrors.setdefault(key[1], {})[key[2]] = value
        elif category == PULSE_GENERATOR_CATEGORY and len(key) == 3:
            parsed.pulseGenerators.setdefault(key[1], {})[key[2]] = value
        else:
            parsed.informationalKeys.append(':'.join(key))
    parsed.informationalKeys.sort()
    return parsed


# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------

@dataclass
class ValueCheck:
    """One stored value of one item, checked against the item's target."""
    key: str
    label: str
    stored: Any
    current: Any = None
    status: str = V_OK
    reason: str = ''
    range: Optional[Tuple[float, float]] = None
    units: Optional[str] = None
    action: str = APPLY
    editedValue: Any = None
    requiresMoves: bool = False

    @property
    def allowedActions(self) -> Tuple[str, ...]:
        if self.status == V_OK:
            return (APPLY, SKIP)
        if self.status == V_OUT_OF_RANGE:
            if self.range is not None:
                return (SKIP, CLAMP, EDIT)
            return (SKIP,)
        return (SKIP,)


@dataclass
class ReplayItem:
    """One device reference of the file."""
    id: str
    kind: str
    storedName: str
    status: str
    candidates: List[str] = field(default_factory=list)
    target: Optional[str] = None
    note: str = ''
    values: List[ValueCheck] = field(default_factory=list)
    storedInfo: Dict[str, Any] = field(default_factory=dict)
    storedValues: Dict[str, Any] = field(default_factory=dict)

    @property
    def replayable(self) -> bool:
        return self.status not in (NOT_REPLAYABLE, NO_SUCH_KIND) and bool(self.candidates)


@dataclass
class ReplayPlan:
    """Everything the dialog shows and the apply layer needs."""
    parsed: ParsedReplayFile
    snapshot: SetupSnapshot
    items: List[ReplayItem] = field(default_factory=list)
    allowMoves: bool = False
    sameSetup: Optional[bool] = None
    warnings: List[str] = field(default_factory=list)

    def item(self, itemId: str) -> ReplayItem:
        for item in self.items:
            if item.id == itemId:
                return item
        raise KeyError(itemId)

    def itemsOfKind(self, kind: str) -> List[ReplayItem]:
        return [item for item in self.items if item.kind == kind]

    def retarget(self, itemId: str, target: Optional[str]) -> ReplayItem:
        """Point ``itemId`` at ``target`` (or ``None`` to skip) and recheck its
        values against the new target.  A target that is not a candidate is
        refused with ``ValueError``."""
        item = self.item(itemId)
        if target is not None and target not in item.candidates:
            raise ValueError(f'{target!r} is not a compatible {item.kind} for {item.storedName!r}')
        item.target = target
        _checkItem(item, self)
        if item.kind in (SCAN_AXIS, TTL):
            for scanItem in self.itemsOfKind(SCAN):
                _checkItem(scanItem, self)
        return item

    def hasAnythingToApply(self) -> bool:
        return bool(buildActions(self).actions)

    def targetMapping(self) -> Dict[str, Optional[str]]:
        return {item.id: item.target for item in self.items if item.replayable}


# --------------------------------------------------------------------------
# Resolving
# --------------------------------------------------------------------------

def _wavelengthMatches(a: Any, b: Any) -> bool:
    fa, fb = _asFloat(a), _asFloat(b)
    return fa is not None and fb is not None and abs(fa - fb) <= _WAVELENGTH_TOLERANCE_NM


def _storedRecord(parsed: ParsedReplayFile, section: str, name: str) -> Dict[str, Any]:
    if not parsed.hasInventory:
        return {}
    return dict(((parsed.inventory or {}).get(section) or {}).get(name) or {})


def resolveReplay(parsed: ParsedReplayFile, snapshot: SetupSnapshot,
                  rememberedTargets: Optional[Mapping[str, Optional[str]]] = None) -> ReplayPlan:
    """Build the replay plan.  Pure: reads ``parsed`` and ``snapshot`` only.

    ``rememberedTargets`` (item id -> target name) pre-fills targets the user
    picked for the same source/current setup pair before; a remembered target
    that is no longer a compatible candidate is ignored.
    """
    plan = ReplayPlan(parsed=parsed, snapshot=snapshot)
    if parsed.setupHash and snapshot.setupHash:
        plan.sameSetup = parsed.setupHash == snapshot.setupHash

    _resolveLasers(plan)
    _resolveDetectors(plan)
    _resolvePositioners(plan)
    _resolveScan(plan)
    _resolveRotators(plan)
    _resolveShownOnly(plan, FLIP_MIRROR, parsed.flipMirrors, 'flipMirrors',
                      'Flipping a mirror changes the beam path; replay shows the stored '
                      'state but never moves a mirror.')
    _resolveShownOnly(plan, PULSE_GENERATOR, parsed.pulseGenerators, 'pulseGenerators',
                      'Pulse sequences are programmed by the scan when it runs, and setting '
                      'outputs directly could open laser gates; shown only.')

    for itemId, target in dict(rememberedTargets or {}).items():
        try:
            item = plan.item(itemId)
        except KeyError:
            continue
        if not item.replayable or item.status == EXACT:
            continue
        if target is None or target in item.candidates:
            item.target = target
            item.note = (item.note + ' ' if item.note else '') + '(remembered mapping)'
    for item in plan.items:
        _checkItem(item, plan)
    return plan


def _usableNames(snapshot: SetupSnapshot, section: str, kind: str) -> List[str]:
    return [name for name in snapshot.devices(section) if snapshot.usable(kind, name)]


def _resolveLasers(plan: ReplayPlan) -> None:
    parsed, snapshot = plan.parsed, plan.snapshot
    current = snapshot.devices('lasers')
    usable = _usableNames(snapshot, 'lasers', LASER)
    for name in sorted(parsed.lasers):
        stored = parsed.lasers[name]
        record = _storedRecord(parsed, 'lasers', name)
        storedWavelength = record.get('wavelength')
        item = ReplayItem(id=f'{LASER}:{name}', kind=LASER, storedName=name,
                          status=MISSING, candidates=list(usable),
                          storedInfo=record, storedValues=dict(stored))
        if not current:
            item.status = NO_SUCH_KIND
            item.candidates = []
            item.note = 'This setup has no lasers.'
        elif name in current:
            if not snapshot.usable(LASER, name):
                item.status = MISSING
                item.note = f'"{name}" is not connected in this setup.'
                item.target = None
            else:
                mismatch = _laserMismatch(record, stored, current[name])
                if mismatch:
                    item.status = CAPABILITY_MISMATCH
                    item.note = mismatch
                    item.target = None
                else:
                    item.status = EXACT
                    item.target = name
        if item.status == MISSING and item.target is None and storedWavelength is not None:
            matches = [c for c in usable
                       if _wavelengthMatches(current[c].get('wavelength'), storedWavelength)]
            if len(matches) == 1:
                item.target = matches[0]
                item.note = (item.note + ' ' if item.note else '') + (
                    f'Pre-selected "{matches[0]}": same wavelength ({storedWavelength} nm).')
            elif len(matches) > 1:
                item.note = (item.note + ' ' if item.note else '') + (
                    f'Several lasers emit at {storedWavelength} nm; pick one.')
        elif item.status == MISSING and storedWavelength is None and item.target is None:
            item.note = (item.note + ' ' if item.note else '') + (
                'The file does not record this laser\'s wavelength; pick a laser or skip.')
        plan.items.append(item)


def _laserMismatch(record: Dict[str, Any], stored: Dict[str, Any],
                   currentRecord: Dict[str, Any]) -> str:
    storedWavelength = record.get('wavelength')
    if storedWavelength is not None and not _wavelengthMatches(
            storedWavelength, currentRecord.get('wavelength')):
        return (f'Recorded at {storedWavelength} nm, this setup\'s laser of the same name '
                f'is {currentRecord.get("wavelength")} nm.')
    storedUnits = stored.get(LASER_UNITS_ATTR, record.get('valueUnits'))
    if storedUnits not in (None, '') and currentRecord.get('valueUnits') not in (None, '') \
            and str(storedUnits) != str(currentRecord.get('valueUnits')):
        return (f'Value recorded in {storedUnits}, this setup\'s laser of the same name '
                f'takes {currentRecord.get("valueUnits")}.')
    storedBinary = record.get('isBinary')
    if storedBinary is not None and bool(storedBinary) != bool(currentRecord.get('isBinary')):
        return 'One of the two lasers is on/off only, the other has a power setpoint.'
    return ''


def _resolveDetectors(plan: ReplayPlan) -> None:
    parsed, snapshot = plan.parsed, plan.snapshot
    current = snapshot.devices('detectors')
    usable = _usableNames(snapshot, 'detectors', DETECTOR)
    for name in sorted(parsed.detectors):
        stored = parsed.detectors[name]
        record = _storedRecord(parsed, 'detectors', name)
        item = ReplayItem(id=f'{DETECTOR}:{name}', kind=DETECTOR, storedName=name,
                          status=MISSING, candidates=list(usable),
                          storedInfo=record, storedValues=copy.deepcopy(stored))
        if not current:
            item.status, item.candidates = NO_SUCH_KIND, []
            item.note = 'This setup has no detectors.'
        elif name in current and snapshot.usable(DETECTOR, name):
            storedClass = record.get('managerClass')
            storedModel = stored.get('Model', record.get('model'))
            cur = current[name]
            if storedClass and cur.get('managerClass') and storedClass != cur.get('managerClass'):
                item.status = CAPABILITY_MISMATCH
                item.note = (f'Recorded with a {storedClass}, this setup\'s "{name}" is a '
                             f'{cur.get("managerClass")}.')
                item.target = name
            elif storedModel and cur.get('model') and str(storedModel) != str(cur.get('model')):
                item.status = CAPABILITY_MISMATCH
                item.note = f'Recorded with model {storedModel}, this one is {cur.get("model")}.'
                item.target = name
            else:
                item.status = EXACT
                item.target = name
        elif name in current:
            item.note = f'"{name}" is not connected in this setup.'
        plan.items.append(item)


def _resolvePositioners(plan: ReplayPlan) -> None:
    parsed, snapshot = plan.parsed, plan.snapshot
    current = snapshot.devices('positioners')
    candidates = [f'{p}:{axis}' for p, rec in current.items()
                  if snapshot.usable(POSITIONER, p)
                  for axis in (rec.get('axes') or [])]
    for (pName, axis) in sorted(parsed.positioners):
        storedName = f'{pName}:{axis}'
        item = ReplayItem(id=f'{POSITIONER}:{storedName}', kind=POSITIONER,
                          storedName=storedName, status=MISSING, candidates=list(candidates),
                          storedInfo=_storedRecord(parsed, 'positioners', pName),
                          storedValues={'Position': parsed.positioners[(pName, axis)]})
        if not current:
            item.status, item.candidates = NO_SUCH_KIND, []
            item.note = 'This setup has no positioners.'
        elif storedName in candidates:
            item.status = EXACT
            item.target = storedName
        elif pName in current and snapshot.usable(POSITIONER, pName):
            item.status = CAPABILITY_MISMATCH
            item.note = (f'"{pName}" has no axis {axis} here (axes: '
                         f'{", ".join(current[pName].get("axes") or [])}).')
        elif pName in current:
            item.note = f'"{pName}" is not connected in this setup.'
        plan.items.append(item)


def _resolveRotators(plan: ReplayPlan) -> None:
    parsed, snapshot = plan.parsed, plan.snapshot
    current = snapshot.devices('rotators')
    usable = _usableNames(snapshot, 'rotators', ROTATOR)
    for name in sorted(parsed.rotators):
        item = ReplayItem(id=f'{ROTATOR}:{name}', kind=ROTATOR, storedName=name,
                          status=MISSING, candidates=list(usable),
                          storedInfo=_storedRecord(parsed, 'rotators', name),
                          storedValues=dict(parsed.rotators[name]))
        if not current:
            item.status, item.candidates = NO_SUCH_KIND, []
            item.note = 'This setup has no rotators.'
        elif name in usable:
            item.status, item.target = EXACT, name
        plan.items.append(item)


def _resolveShownOnly(plan: ReplayPlan, kind: str, stored: Mapping[str, Dict[str, Any]],
                      section: str, reason: str) -> None:
    for name in sorted(stored):
        values = dict(stored[name])
        item = ReplayItem(id=f'{kind}:{name}', kind=kind, storedName=name,
                          status=NOT_REPLAYABLE, note=reason,
                          storedInfo=_storedRecord(plan.parsed, section, name),
                          storedValues=values)
        plan.items.append(item)


# ----- scan ---------------------------------------------------------------

def _scanDeviceNames(stage: Mapping[str, Any], ttl: Mapping[str, Any]) -> Tuple[List[str], List[str]]:
    axes: List[str] = []
    for key in _SCAN_DEVICE_LIST_KEYS:
        for name in _asList(stage.get(key)):
            if name not in (None, 'None', '') and str(name) not in axes:
                axes.append(str(name))
    ttls: List[str] = []
    for name in _asList(ttl.get('target_device')):
        if name not in (None, 'None', '') and str(name) not in ttls:
            ttls.append(str(name))
    return axes, ttls


def _asList(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _resolveScan(plan: ReplayPlan) -> None:
    parsed, snapshot = plan.parsed, plan.snapshot
    stage = {k: v for k, v in parsed.scanStage.items() if k not in _SCAN_DERIVED_STAGE_KEYS}
    ttl = dict(parsed.scanTTL)
    if not stage and not ttl:
        return
    scanInfo = snapshot.scanInfo()
    storedScan = (parsed.inventory or {}).get('scan') if parsed.hasInventory else None
    scanItem = ReplayItem(id=f'{SCAN}:parameters', kind=SCAN, storedName='Scan parameters',
                          status=EXACT, candidates=['Scan'] if scanInfo else [],
                          storedInfo=dict(storedScan or {}),
                          storedValues={'analog': stage, 'digital': ttl})
    if not scanInfo:
        scanItem.status = NO_SUCH_KIND
        scanItem.note = 'This setup has no scan panel.'
        plan.items.append(scanItem)
        return

    axisNames, ttlNames = _scanDeviceNames(stage, ttl)
    scanPositioners = list(scanInfo.get('positioners') or [])
    ttlDevices = list(scanInfo.get('ttlDevices') or [])
    for name in axisNames:
        item = ReplayItem(id=f'{SCAN_AXIS}:{name}', kind=SCAN_AXIS, storedName=name,
                          status=MISSING, candidates=list(scanPositioners))
        if name in scanPositioners:
            item.status, item.target = EXACT, name
        elif not scanPositioners:
            item.status, item.candidates = NO_SUCH_KIND, []
            item.note = 'This scan panel drives no positioners.'
        plan.items.append(item)
    for name in ttlNames:
        item = ReplayItem(id=f'{TTL}:{name}', kind=TTL, storedName=name,
                          status=MISSING, candidates=list(ttlDevices))
        if name in ttlDevices:
            item.status, item.target = EXACT, name
        elif not ttlDevices:
            item.status, item.candidates = NO_SUCH_KIND, []
            item.note = 'This scan panel has no TTL lines.'
        plan.items.append(item)

    storedType = (storedScan or {}).get('scanWidgetType')
    currentType = scanInfo.get('scanWidgetType')
    analogKeys = set(scanInfo.get('analogKeys') or [])
    digitalKeys = set(scanInfo.get('digitalKeys') or [])
    unknown = [k for k in stage if analogKeys and k not in analogKeys] + \
              [k for k in ttl if digitalKeys and k not in digitalKeys]
    scanItem.target = 'Scan'
    if storedType and currentType and storedType != currentType:
        scanItem.status = CAPABILITY_MISMATCH
        scanItem.note = (f'Recorded with the {storedType} scan panel, this setup uses '
                         f'{currentType}.')
        scanItem.target = None
    elif unknown:
        scanItem.status = CAPABILITY_MISMATCH
        scanItem.note = ('The file has scan parameters this scan panel does not know '
                         f'({", ".join(sorted(unknown)[:6])}{"..." if len(unknown) > 6 else ""}); '
                         'it was probably recorded with a different scan type.')
        scanItem.target = None
    plan.items.append(scanItem)


# --------------------------------------------------------------------------
# Value checks (depend on the chosen target)
# --------------------------------------------------------------------------

def _checkItem(item: ReplayItem, plan: ReplayPlan) -> None:
    previous = {v.key: v for v in item.values}
    checker = {
        LASER: _checkLaser, DETECTOR: _checkDetector, POSITIONER: _checkPositioner,
        ROTATOR: _checkRotator, SCAN: _checkScan,
    }.get(item.kind)
    if checker is None:
        item.values = [ValueCheck(key=str(k), label=str(k), stored=v, status=V_SHOWN,
                                  reason=item.note if item.status == NOT_REPLAYABLE else '',
                                  action=SKIP)
                       for k, v in sorted(item.storedValues.items())]
        return
    item.values = checker(item, plan)
    for value in item.values:
        if value.status in (V_SHOWN, V_INVALID, V_NO_TARGET):
            value.action = SKIP
        old = previous.get(value.key)
        # Keep a user's explicit choice when the recheck still allows it.
        if old is not None and old.action in value.allowedActions and old.status == value.status \
                and getattr(old, '_userSet', False):
            value.action = old.action
            value.editedValue = old.editedValue
            value._userSet = True  # type: ignore[attr-defined]


def setValueAction(value: ValueCheck, action: str, editedValue: Any = None) -> None:
    """Record a user's choice for one value (the dialog's per-value combo)."""
    if action not in value.allowedActions:
        raise ValueError(f'{action!r} is not allowed for {value.label!r} ({value.status})')
    value.action = action
    value.editedValue = editedValue
    value._userSet = True  # type: ignore[attr-defined]


def _noTarget(item: ReplayItem, key: str, label: str, stored: Any,
              requiresMoves: bool = False) -> ValueCheck:
    reason = 'Skipped: no device chosen.' if item.replayable else (item.note or 'Not replayable.')
    return ValueCheck(key=key, label=label, stored=stored, status=V_NO_TARGET,
                      reason=reason, action=SKIP, requiresMoves=requiresMoves)


def _rangeCheck(value: ValueCheck, number: Optional[float],
                lo: Optional[float], hi: Optional[float]) -> None:
    if number is None:
        value.status, value.reason = V_INVALID, 'Not a number.'
        return
    if lo is not None and hi is not None:
        value.range = (float(lo), float(hi))
        if number < lo or number > hi:
            value.status = V_OUT_OF_RANGE
            value.reason = f'Outside {lo:g} to {hi:g}{(" " + value.units) if value.units else ""}.'
            value.action = SKIP


def _checkLaser(item: ReplayItem, plan: ReplayPlan) -> List[ValueCheck]:
    stored = item.storedValues
    checks: List[ValueCheck] = []
    target = item.target
    record = plan.snapshot.devices('lasers').get(target, {}) if target else {}
    storedUnits = stored.get(LASER_UNITS_ATTR, item.storedInfo.get('valueUnits'))
    if 'Value' in stored:
        if target is None:
            checks.append(_noTarget(item, 'Value', 'Power', stored['Value']))
        elif record.get('isBinary'):
            # Same on/off-only laser: nothing to say. A power setpoint sent to
            # an on/off-only replacement: a value that cannot be applied.
            sameBinary = bool(item.storedInfo.get('isBinary')) or target == item.storedName
            checks.append(ValueCheck(key='Value', label='Power', stored=stored['Value'],
                                     status=V_SHOWN if sameBinary else V_INVALID, action=SKIP,
                                     reason=f'"{target}" is on/off only; it has no power value.'))
        else:
            check = ValueCheck(key='Value', label='Power', stored=stored['Value'],
                               current=plan.snapshot.current.get(('Laser', target, 'Value')),
                               units=record.get('valueUnits'))
            if storedUnits not in (None, '') and record.get('valueUnits') not in (None, '') \
                    and str(storedUnits) != str(record.get('valueUnits')):
                check.status = V_INVALID
                check.reason = (f'Recorded in {storedUnits}, "{target}" takes '
                                f'{record.get("valueUnits")}; not converted.')
            else:
                _rangeCheck(check, _asFloat(stored['Value']),
                            _asFloat(record.get('valueRangeMin')),
                            _asFloat(record.get('valueRangeMax')))
                if check.status == V_OK and target in plan.snapshot.laserEnabled:
                    check.reason = (f'"{target}" is emitting now: applying changes its '
                                    'output power.')
            checks.append(check)
    if 'Enabled' in stored:
        enabled = _asBool(stored['Enabled'])
        checks.append(ValueCheck(
            key='Enabled', label='Emission', stored=('on' if enabled else 'off'),
            current=(('on' if target in plan.snapshot.laserEnabled else 'off')
                     if target else None),
            status=V_SHOWN, action=SKIP,
            reason='Replay never switches a laser on (or off); shown only.'))
    if LASER_CALIBRATION_ATTR in stored:
        checks.append(ValueCheck(key=LASER_CALIBRATION_ATTR, label='Calibration',
                                 stored=stored[LASER_CALIBRATION_ATTR], status=V_SHOWN,
                                 action=SKIP, reason='Calibration identity; shown only.'))
    return checks


def _checkDetector(item: ReplayItem, plan: ReplayPlan) -> List[ValueCheck]:
    stored = item.storedValues
    target = item.target
    record = plan.snapshot.devices('detectors').get(target, {}) if target else {}
    current = plan.snapshot.current
    checks: List[ValueCheck] = []

    if 'Binning' in stored:
        if target is None:
            checks.append(_noTarget(item, 'Binning', 'Binning', stored['Binning']))
        else:
            check = ValueCheck(key='Binning', label='Binning', stored=stored['Binning'],
                               current=current.get(('Detector', target, 'Binning')))
            supported = record.get('supportedBinnings')
            number = _asFloat(stored['Binning'])
            if number is None or number != int(number):
                check.status, check.reason = V_INVALID, 'Not a whole number.'
            elif supported and int(number) not in [int(b) for b in supported]:
                check.status = V_INVALID
                check.reason = f'"{target}" supports binning {supported}.'
            checks.append(check)
    if 'ROI' in stored:
        if target is None:
            checks.append(_noTarget(item, 'ROI', 'ROI (x, y, w, h)', stored['ROI']))
        else:
            check = ValueCheck(key='ROI', label='ROI (x, y, w, h)', stored=stored['ROI'],
                               current=current.get(('Detector', target, 'ROI')))
            roi = _asList(stored['ROI'])
            numbers = [_asFloat(v) for v in roi]
            full = _asList(record.get('fullShape'))
            if len(numbers) != 4 or any(n is None for n in numbers):
                check.status, check.reason = V_INVALID, 'Not four numbers.'
            else:
                x0, y0, w, h = numbers
                if w <= 0 or h <= 0 or x0 < 0 or y0 < 0:
                    check.status, check.reason = V_INVALID, 'Negative or empty frame.'
                elif len(full) >= 2 and _asFloat(full[0]) is not None and (
                        x0 + w > float(full[0]) or y0 + h > float(full[1])):
                    check.status = V_INVALID
                    check.reason = f'Larger than "{target}"\'s full frame {full[0]} x {full[1]}.'
            checks.append(check)
    params = stored.get('params') or {}
    targetParams = record.get('parameters') or {}
    for pName in sorted(params):
        key = f'Param:{pName}'
        if target is None:
            checks.append(_noTarget(item, key, pName, params[pName]))
            continue
        check = ValueCheck(key=key, label=pName, stored=params[pName],
                           current=current.get(('Detector', target, 'Param', pName)))
        spec = targetParams.get(pName)
        if targetParams and spec is None:
            check.status, check.reason = V_INVALID, f'"{target}" has no such parameter.'
        elif spec is not None and (not spec.get('editable', True) or spec.get('configOwned')):
            check.status = V_SHOWN
            check.reason = ('Set by the setup file.' if spec.get('configOwned')
                            else 'Read-only on this detector.')
        elif spec is not None and spec.get('options') is not None:
            if str(params[pName]) not in [str(o) for o in spec.get('options') or []]:
                check.status = V_INVALID
                check.reason = f'Not one of {", ".join(map(str, spec.get("options") or []))}.'
        elif spec is not None and spec.get('kind') == 'number':
            if _asFloat(params[pName]) is None:
                check.status, check.reason = V_INVALID, 'Not a number.'
            check.units = spec.get('units')
        if current.get(('Detector', target, 'Param', pName)) is None and not targetParams:
            check.reason = check.reason or 'Detector parameters of this setup are unknown.'
        checks.append(check)
    for infoKey in ('Model', 'Pixel size'):
        if infoKey in stored:
            checks.append(ValueCheck(key=infoKey, label=infoKey, stored=stored[infoKey],
                                     current=current.get(('Detector', target, infoKey))
                                     if target else None,
                                     status=V_SHOWN, action=SKIP,
                                     reason='Property of the detector; shown only.'))
    return checks


def _checkPositioner(item: ReplayItem, plan: ReplayPlan) -> List[ValueCheck]:
    stored = item.storedValues.get('Position')
    if item.target is None:
        return [_noTarget(item, 'Position', 'Position', stored, requiresMoves=True)]
    pName, _, axis = item.target.partition(':')
    record = plan.snapshot.devices('positioners').get(pName, {})
    check = ValueCheck(key='Position', label=f'Position ({axis})', stored=stored,
                       current=plan.snapshot.current.get(('Positioner', pName, axis, 'Position')),
                       units=record.get('units', 'µm'), requiresMoves=True)
    limits = (record.get('axisRanges') or {}).get(axis)
    lo, hi = (limits if isinstance(limits, (list, tuple)) and len(limits) == 2 else (None, None))
    _rangeCheck(check, _asFloat(stored), _asFloat(lo), _asFloat(hi))
    if check.status == V_OK and check.range is None:
        check.reason = 'Travel range unknown for this positioner.'
    return [check]


def _checkRotator(item: ReplayItem, plan: ReplayPlan) -> List[ValueCheck]:
    checks = []
    for key, stored in sorted(item.storedValues.items()):
        if key != 'Position':
            checks.append(ValueCheck(key=key, label=key, stored=stored, status=V_SHOWN,
                                     action=SKIP, reason='Shown only.'))
            continue
        if item.target is None:
            checks.append(_noTarget(item, 'Position', 'Angle', stored, requiresMoves=True))
            continue
        check = ValueCheck(key='Position', label='Angle', stored=stored, units='°',
                           current=plan.snapshot.current.get(
                               (ROTATOR_CATEGORY, item.target, 'Position')),
                           requiresMoves=True)
        if _asFloat(stored) is None:
            check.status, check.reason = V_INVALID, 'Not a number.'
        checks.append(check)
    return checks


def _scanNameMap(plan: ReplayPlan) -> Tuple[Dict[str, Optional[str]], List[str]]:
    mapping: Dict[str, Optional[str]] = {}
    unresolved: List[str] = []
    for item in plan.items:
        if item.kind in (SCAN_AXIS, TTL):
            mapping[item.storedName] = item.target
            if item.target is None:
                unresolved.append(item.storedName)
    return mapping, unresolved


def _checkScan(item: ReplayItem, plan: ReplayPlan) -> List[ValueCheck]:
    analog = item.storedValues.get('analog') or {}
    digital = item.storedValues.get('digital') or {}
    scanInfo = plan.snapshot.scanInfo() or {}
    analogKeys = set(scanInfo.get('analogKeys') or [])
    digitalKeys = set(scanInfo.get('digitalKeys') or [])
    current = plan.snapshot.current
    _, unresolved = _scanNameMap(plan)
    checks: List[ValueCheck] = []
    for prefix, values, known, category in (('ScanStage', analog, analogKeys, 'ScanStage'),
                                            ('ScanTTL', digital, digitalKeys, 'ScanTTL')):
        for key in sorted(values):
            check = ValueCheck(key=f'{prefix}:{key}', label=f'{prefix}: {key}',
                               stored=values[key], current=current.get((category, key)))
            if item.target is None:
                check.status = V_NO_TARGET
                check.reason = item.note or 'Scan parameters skipped.'
            elif known and key not in known:
                check.status = V_SHOWN
                check.reason = 'This scan panel has no such parameter; left out.'
            checks.append(check)
    if item.target is not None and unresolved:
        for check in checks:
            if check.status == V_OK:
                check.status = V_INVALID
                check.reason = ('The scan names devices that are skipped '
                                f'({", ".join(unresolved)}); scan parameters are applied '
                                'all together or not at all.')
    return checks


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------

@dataclass
class ReplayAction:
    """One concrete thing the apply layer will do."""
    itemId: str
    kind: str
    target: str
    key: str
    value: Any
    note: str = ''


@dataclass
class ReplayActionSet:
    actions: List[ReplayAction] = field(default_factory=list)
    #: (item id, key) -> reason, for every stored value that is not applied.
    skipped: Dict[Tuple[str, str], str] = field(default_factory=dict)


def _clamp(number: float, valueRange: Tuple[float, float]) -> float:
    lo, hi = valueRange
    return min(max(number, lo), hi)


def buildActions(plan: ReplayPlan) -> ReplayActionSet:
    """Turn the plan, as decided, into the values to apply.  Enforces the
    replay safety rules (see the module docstring)."""
    result = ReplayActionSet()
    used: Dict[Tuple[str, str, str], str] = {}
    scanMap, _ = _scanNameMap(plan)

    for item in plan.items:
        if item.kind in (SCAN_AXIS, TTL):
            continue  # applied through the scan item
        for value in item.values:
            ref = (item.id, value.key)
            if value.status in (V_SHOWN, V_NO_TARGET, V_INVALID):
                result.skipped[ref] = value.reason or 'Not applied.'
                continue
            if item.target is None or not item.replayable:
                result.skipped[ref] = 'Skipped: no device chosen.'
                continue
            if item.kind == SCAN:
                continue  # collected below, all-or-nothing
            if value.key == 'Enabled':  # belt and braces: never switch emission
                result.skipped[ref] = 'Replay never switches emission.'
                continue
            if value.requiresMoves and not plan.allowMoves:
                result.skipped[ref] = 'Moves not enabled ("Move stages and rotators" is off).'
                continue
            if value.action == SKIP:
                result.skipped[ref] = (value.reason if value.status == V_OUT_OF_RANGE
                                       else 'Skipped by choice.')
                continue
            final, refusal = _finalValue(value)
            if refusal:
                result.skipped[ref] = refusal
                continue
            usage = (item.kind, item.target, value.key)
            if usage in used:
                result.skipped[ref] = (f'"{item.target}" already receives this value from '
                                       f'"{used[usage]}".')
                continue
            used[usage] = item.storedName
            note = ''
            if value.action == CLAMP:
                note = f'clamped from {value.stored}'
            elif value.action == EDIT:
                note = f'edited from {value.stored}'
            result.actions.append(ReplayAction(item.id, item.kind, item.target,
                                               value.key, final, note))

    for item in plan.itemsOfKind(SCAN):
        applicable = [v for v in item.values if v.status == V_OK and v.action != SKIP]
        if item.target is None or not applicable:
            continue
        if any(v.status == V_INVALID for v in item.values):
            continue
        analog: Dict[str, Any] = {}
        digital: Dict[str, Any] = {}
        for value in applicable:
            prefix, _, key = value.key.partition(':')
            target = analog if prefix == 'ScanStage' else digital
            target[key] = _remapScanValue(key, copy.deepcopy(value.stored), scanMap)
        result.actions.append(ReplayAction(item.id, SCAN, item.target, 'parameters',
                                           {'analog': analog, 'digital': digital}))
    return result


def _finalValue(value: ValueCheck) -> Tuple[Any, str]:
    """(value to apply, refusal reason)."""
    if value.action == APPLY:
        if value.status != V_OK:
            return None, value.reason or 'Out of range.'
        number = _asFloat(value.stored)
        if value.range is not None and number is not None and not (
                value.range[0] <= number <= value.range[1]):
            return None, 'Out of range.'
        return value.stored, ''
    if value.action == CLAMP:
        number = _asFloat(value.stored)
        if value.range is None or number is None:
            return None, 'Cannot clamp: range unknown.'
        return _clamp(number, value.range), ''
    if value.action == EDIT:
        number = _asFloat(value.editedValue)
        if number is None:
            return None, 'Edited value is not a number.'
        if value.range is not None and not (value.range[0] <= number <= value.range[1]):
            return None, (f'Edited value {number:g} is outside '
                          f'{value.range[0]:g} to {value.range[1]:g}.')
        return number, ''
    return None, 'Skipped by choice.'


def _remapScanValue(key: str, value: Any, mapping: Mapping[str, Optional[str]]) -> Any:
    if key in _SCAN_DEVICE_LIST_KEYS + ('target_device',) and isinstance(value, list):
        return [mapping.get(str(v), v) if v not in (None, 'None') else v for v in value]
    if isinstance(value, dict):
        return {mapping.get(k, k) or k: v for k, v in value.items()}
    return value


# --------------------------------------------------------------------------
# Unattended policy
# --------------------------------------------------------------------------

def applyUnattendedPolicy(plan: ReplayPlan, *, allowMoves: bool = False) -> ReplayPlan:
    """Restrict ``plan`` to what may be applied with nobody watching: exact
    matches only, in-range values only (out-of-range stays skipped, never
    clamped), positions only when the caller explicitly allowed moves."""
    plan.allowMoves = bool(allowMoves)
    for item in plan.items:
        if item.status != EXACT:
            item.target = None
        _checkItem(item, plan)
        for value in item.values:
            if value.status != V_OK:
                value.action = SKIP
    return plan


# --------------------------------------------------------------------------
# Results and provenance
# --------------------------------------------------------------------------

@dataclass
class ReplayResult:
    applied: List[Tuple[str, str, Any, str]] = field(default_factory=list)  # item, key, value, note
    skipped: List[Tuple[str, str, str]] = field(default_factory=list)       # item, key, reason
    failed: List[Tuple[str, str, str]] = field(default_factory=list)        # item, key, reason
    cancelled: bool = False

    def summaryLines(self) -> List[str]:
        lines = [f'Applied: {len(self.applied)}, skipped: {len(self.skipped)}, '
                 f'failed: {len(self.failed)}.']
        for itemId, key, value, note in self.applied:
            lines.append(f'applied  {itemId} {key} = {_short(value)}' + (f' ({note})' if note else ''))
        for itemId, key, reason in self.failed:
            lines.append(f'FAILED   {itemId} {key}: {reason}')
        for itemId, key, reason in self.skipped:
            lines.append(f'skipped  {itemId} {key}: {reason}')
        return lines

    def toDict(self) -> Dict[str, Any]:
        return {
            'cancelled': self.cancelled,
            'applied': [{'item': i, 'key': k, 'value': v, 'note': n}
                        for i, k, v, n in self.applied],
            'skipped': [{'item': i, 'key': k, 'reason': r} for i, k, r in self.skipped],
            'failed': [{'item': i, 'key': k, 'reason': r} for i, k, r in self.failed],
        }


def _short(value: Any, limit: int = 80) -> str:
    text = json.dumps(value, default=str) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[:limit - 3] + '...'


def planToDict(plan: ReplayPlan) -> Dict[str, Any]:
    """JSON-able description of a plan (API return value)."""
    return {
        'sourceSetupName': plan.parsed.setupName,
        'sourceSetupHash': plan.parsed.setupHash,
        'currentSetupHash': plan.snapshot.setupHash,
        'sameSetup': plan.sameSetup,
        'allowMoves': plan.allowMoves,
        'items': [{
            'id': item.id, 'kind': item.kind, 'stored': item.storedName,
            'status': item.status, 'target': item.target,
            'candidates': list(item.candidates), 'note': item.note,
            'values': [{'key': v.key, 'stored': v.stored, 'current': v.current,
                        'status': v.status, 'action': v.action, 'reason': v.reason,
                        'range': list(v.range) if v.range else None}
                       for v in item.values],
        } for item in plan.items],
    }


def buildProvenance(plan: ReplayPlan, result: ReplayResult, *, appliedAt: str,
                    mode: str) -> Dict[Tuple[str, str], Any]:
    """Shared-attribute entries (category ``replay``) describing a replay.

    The mapping lists every device reference with where it went (or that it
    was skipped) and which values were applied, skipped or failed and why.
    Only the source's file name is recorded, never its path.
    """
    parsed = plan.parsed
    perItem: Dict[str, Dict[str, Any]] = {}
    for item in plan.items:
        perItem[item.id] = {
            'kind': item.kind, 'stored': item.storedName, 'target': item.target,
            'status': item.status, 'applied': {}, 'skipped': {}, 'failed': {},
        }
        if item.kind == LASER and LASER_CALIBRATION_ATTR in item.storedValues:
            perItem[item.id]['calibration'] = item.storedValues[LASER_CALIBRATION_ATTR]
    for itemId, key, value, note in result.applied:
        entry = perItem.setdefault(itemId, {'applied': {}, 'skipped': {}, 'failed': {}})
        entry['applied'][key] = {'value': value, 'note': note} if note else value
    for itemId, key, reason in result.skipped:
        perItem.setdefault(itemId, {'applied': {}, 'skipped': {}, 'failed': {}})['skipped'][key] = reason
    for itemId, key, reason in result.failed:
        perItem.setdefault(itemId, {'applied': {}, 'skipped': {}, 'failed': {}})['failed'][key] = reason
    mapping = [dict(id=itemId, **entry) for itemId, entry in perItem.items()]
    return {
        (REPLAY_CATEGORY, 'source_file'): parsed.sourceFile or '',
        (REPLAY_CATEGORY, 'source_dataset'): parsed.sourceDataset or '',
        (REPLAY_CATEGORY, 'source_format'): parsed.sourceFormat or '',
        (REPLAY_CATEGORY, 'source_software_version'): parsed.softwareVersion or '',
        (REPLAY_CATEGORY, 'source_setup_name'): parsed.setupName or '',
        (REPLAY_CATEGORY, 'source_setup_hash'): parsed.setupHash or '',
        (REPLAY_CATEGORY, 'source_start_time'): parsed.startTime or '',
        (REPLAY_CATEGORY, 'applied_at'): appliedAt,
        (REPLAY_CATEGORY, 'mode'): mode,
        (REPLAY_CATEGORY, 'mapping'): json.dumps(mapping, default=str, sort_keys=True),
    }


def mappingMemoryKey(sourceHash: Optional[str], currentHash: Optional[str]) -> Optional[str]:
    """Key under which a user's target choices are remembered, or None when
    either setup is unidentified (old files)."""
    if not sourceHash or not currentHash:
        return None
    return f'{sourceHash}->{currentHash}'


def rememberableTargets(plan: ReplayPlan) -> Dict[str, Optional[str]]:
    """The non-exact choices worth remembering for this setup pair."""
    return {item.id: item.target for item in plan.items
            if item.replayable and item.status != EXACT}


__all__ = [name for name in dir() if not name.startswith('_')]
