"""Read-only description of the running setup for replay and provenance.

:func:`buildInventory` describes the devices the managers hold with the
essentials replay needs to remap a recording onto another setup (name, kind,
manager class, wavelength, value units and range, axes, ...).  It reads only
static manager properties; it never sends a command to hardware.  Only
whitelisted fields are written, and :func:`replay.sanitizeInventory` drops
anything that looks like a serial number, port or path on top of that.

A device that failed at startup is either absent from its manager (it never
got built) or present but not usable; both are reported as unavailable in
the snapshot and never offered as a replay target.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from imswitch.imcontrol.model import replay
from imswitch.imcontrol.model.calibrations import (
    laser_value_scale,
    power_table_of,
    raw_units_of,
    resolution_of,
)
from imswitch.imcontrol.model.devices.status import device_usable


def _items(manager: Any) -> Iterable:
    if manager is None:
        return []
    try:
        return list(manager)
    except Exception:  # noqa: BLE001 - a broken manager must not stop a load
        return []


def _get(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name, default)
    except Exception:  # noqa: BLE001 - properties may raise on a dead device
        return default


def _num(value: Any) -> Any:
    value = replay.plainValue(value)
    if isinstance(value, (int, float)) or value is None:
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positionerAxisRanges(manager: Any, axes) -> Dict[str, list]:
    """Travel limits a positioner's configuration declares, per axis.

    There is no common range property on positioner managers; this reads the
    managerProperties the shipped managers that enforce a range use. Unknown
    ranges are simply left out (the dialog then says the range is unknown)."""
    info = _get(manager, '_positionerInfo') or _get(manager, 'positionerInfo')
    props = dict(_get(info, 'managerProperties', None) or {})
    if not props:
        return {}
    limits = None
    if isinstance(props.get('posRangeUm'), (list, tuple)) and len(props['posRangeUm']) == 2:
        limits = [_num(props['posRangeUm'][0]), _num(props['posRangeUm'][1])]
    elif _num(props.get('travelRangeUm')) is not None:
        limits = [0.0, _num(props.get('travelRangeUm'))]
    if limits is None or None in limits:
        return {}
    return {axis: list(limits) for axis in axes}


def _detectorParameters(manager: Any) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    configOwned = set(_get(manager, 'configOwnedParameters', None) or ())
    for name, parameter in dict(_get(manager, 'parameters', None) or {}).items():
        spec: Dict[str, Any] = {'editable': bool(_get(parameter, 'editable', True))}
        options = _get(parameter, 'options')
        if options is not None:
            spec['kind'] = 'list'
            spec['options'] = [str(o) for o in options]
        else:
            spec['kind'] = 'number'
            spec['units'] = _get(parameter, 'valueUnits')
        if name in configOwned:
            spec['configOwned'] = True
        out[str(name)] = spec
    return out


def buildInventory(master: Any, setupInfo: Any = None,
                   scanController: Any = None) -> Dict[str, Any]:
    """The device inventory recorded with every recording (schema 1)."""
    inventory = replay.emptyInventory()

    for name, laser in _items(_get(master, 'lasersManager')):
        # The value's scale as the Laser panel shows it: 0-100 % while a power
        # calibration is in force, the manager's own unit and range otherwise.
        # The raw drive's unit and range, and which calibration is in force
        # (id and content hash only -- never a file path), say what a stored
        # number meant.
        try:
            rangeMin, rangeMax, units, _decimals, step = laser_value_scale(laser)
        except Exception:  # noqa: BLE001 - a dead device must not stop a load
            rangeMin, rangeMax, units, step = (_get(laser, 'valueRangeMin'),
                                               _get(laser, 'valueRangeMax'),
                                               _get(laser, 'valueUnits'),
                                               _get(laser, 'valueRangeStep'))
        table = power_table_of(laser)
        inventory['lasers'][name] = {
            'managerClass': type(laser).__name__,
            'wavelength': _num(_get(laser, 'wavelength')),
            'valueUnits': units,
            'valueRangeMin': _num(rangeMin),
            'valueRangeMax': _num(rangeMax),
            'valueRangeStep': _num(step),
            'isBinary': bool(_get(laser, 'isBinary', False)),
            'isModulated': bool(_get(laser, 'isModulated', False)),
            'valueIs': 'percent' if table is not None else 'raw',
            'rawUnits': raw_units_of(laser),
            'rawRangeMin': _num(_get(laser, 'valueRangeMin')),
            'rawRangeMax': _num(_get(laser, 'valueRangeMax')),
            'calibration': ({'id': table.id, 'contentHash': table.contentHash()}
                            if table is not None else None),
        }

    for name, positioner in _items(_get(master, 'positionersManager')):
        axes = [str(a) for a in (_get(positioner, 'axes') or [])]
        record = {
            'managerClass': type(positioner).__name__,
            'axes': axes,
            'units': 'µm',
            'forScanning': bool(_get(positioner, 'forScanning', False)),
            'forPositioning': bool(_get(positioner, 'forPositioning', False)),
        }
        ranges = _positionerAxisRanges(positioner, axes)
        if ranges:
            record['axisRanges'] = ranges
        inventory['positioners'][name] = record

    for name, detector in _items(_get(master, 'detectorsManager')):
        inventory['detectors'][name] = {
            'managerClass': type(detector).__name__,
            'model': replay.plainValue(_get(detector, 'model')),
            'fullShape': replay.plainValue(list(_get(detector, 'fullShape', None) or [])),
            'supportedBinnings': replay.plainValue(
                list(_get(detector, 'supportedBinnings', None) or [])),
            'forAcquisition': bool(_get(detector, 'forAcquisition', False)),
            'parameters': _detectorParameters(detector),
        }

    for name, rotator in _items(_get(master, 'rotatorsManager')):
        inventory['rotators'][name] = {'managerClass': type(rotator).__name__,
                                       'units': '°'}

    flipInfos = dict(_get(setupInfo, 'flipMirrors', None) or {})
    for name, mirror in _items(_get(master, 'flipMirrorsManager')):
        info = flipInfos.get(name)
        inventory['flipMirrors'][name] = {
            'managerClass': type(mirror).__name__,
            'stateNames': dict(_get(info, 'state_names', None) or {}),
        }

    pulse = _get(master, 'pulseGeneratorManager')
    if pulse is not None:
        pulseInfo = _get(setupInfo, 'teensyPulse')
        inventory['pulseGenerators'][pulseGeneratorName(setupInfo)] = {
            'managerClass': type(pulse).__name__,
            'nDigitalChannels': _num(_get(pulse, 'n_digital_channels')),
            'jitterNs': _num(_get(pulse, 'jitter_ns')),
            'minPulseWidthNs': _num(_get(pulse, 'min_pulse_width_ns')),
            'supportsAnalog': bool(_get(pulse, 'supports_analog', False)),
            # Channel names -> output pins: what the sequence lines mean.
            'pinMap': {str(k): _num(v) for k, v in
                       dict(_get(pulseInfo, 'pinMap', None) or {}).items()},
        }

    if scanController is not None:
        scanInfo = _get(setupInfo, 'scan')
        analog = _get(scanController, '_analogParameterDict', None) or {}
        digital = _get(scanController, '_digitalParameterDict', None) or {}
        inventory['scan'] = {
            'controller': type(scanController).__name__,
            'scanWidgetType': _get(scanInfo, 'scanWidgetType'),
            'positioners': [str(n) for n in dict(_get(scanController, 'positioners', None) or {})],
            'ttlDevices': [str(n) for n in dict(_get(scanController, 'TTLDevices', None) or {})],
            'analogKeys': sorted(str(k) for k in analog),
            'digitalKeys': sorted(str(k) for k in digital),
        }

    return replay.sanitizeInventory(inventory)


def pulseGeneratorName(setupInfo: Any) -> str:
    """The name the pulse generator is recorded under (its setup section)."""
    return 'teensyPulse'


def unavailableDevices(master: Any) -> set:
    """(kind, name) of devices present in a manager but not usable."""
    out = set()
    for kind, attr in ((replay.LASER, 'lasersManager'),
                       (replay.POSITIONER, 'positionersManager'),
                       (replay.DETECTOR, 'detectorsManager'),
                       (replay.ROTATOR, 'rotatorsManager')):
        for name, manager in _items(_get(master, attr)):
            if not device_usable(manager):
                out.add((kind, name))
    return out


def buildSnapshot(master: Any, setupInfo: Any, *, setupName: Optional[str],
                  setupHash: Optional[str], currentAttrs: Any,
                  scanController: Any = None, laserEnabled: Iterable[str] = ()) -> replay.SetupSnapshot:
    items = currentAttrs.items() if hasattr(currentAttrs, 'items') else currentAttrs
    current = {tuple(k): replay.plainValue(v) for k, v in items}
    calibrations, failed = {}, set()
    for name, laser in _items(_get(master, 'lasersManager')):
        table = power_table_of(laser)
        if table is not None:
            calibrations[name] = table
        try:
            if resolution_of(laser).failed:
                failed.add(name)
        except Exception:  # noqa: BLE001
            pass
    return replay.SetupSnapshot(
        laserCalibrations=calibrations,
        laserCalibrationFailed=failed,
        setupName=setupName,
        setupHash=setupHash,
        inventory=buildInventory(master, setupInfo, scanController),
        unavailable=unavailableDevices(master),
        current=current,
        laserEnabled=set(laserEnabled),
    )
