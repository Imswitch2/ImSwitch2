"""The setup-file section a CONFIGURED panel needs, while ImSwitch runs.

For each panel that reads a section (``model/panels.py``: Tiling reads
``tiling``, Autofocus ``autofocus``) this answers four questions without Qt,
so that the dialog, the controller and the tests share one answer:

* what the running hardware lacks for the panel at all
  (:func:`prerequisiteProblem` -- e.g. no stage with two axes);
* what to propose when the section is missing (:func:`proposeSection`, which
  also says what it derived and from what);
* whether a filled-in section is usable on this hardware
  (:func:`validateSection` -- the messages the dialog shows);
* the typed section, built exactly the way the setup loader builds it
  (:func:`buildSection`).

``master`` is the running ``MasterController`` (its ``positionersManager``
and ``detectorsManager``); ``setupInfo`` the running setup.
"""

import dataclasses
import math
from typing import Dict, List, Optional, Tuple

from .setup_file_values import sanitizeSetupData


TILE_OVERLAP = 0.1
""" Overlap between neighbouring tiles the proposed tile step leaves. """

_TILING_PATTERNS = ('spiral', 'serpentine')
_TILING_MODES = ('free-running', 'triggered')
_TILING_FORMATS = ('TIFF', 'HDF5', 'ZARR')
_Z_AXIS = 'Z'  # the axis AutofocusController sweeps


# --------------------------------------------------------------------------
# What the running hardware offers
# --------------------------------------------------------------------------

def _devices(multiManager) -> Dict[str, object]:
    """{name: manager} of a MultiManager, or of a plain dict in tests."""
    if multiManager is None:
        return {}
    try:
        return dict(iter(multiManager))
    except Exception:  # noqa: BLE001 -- a manager that cannot list is empty here
        return {}


def _axes(setupInfo, name, manager) -> List[str]:
    info = (getattr(setupInfo, 'positioners', None) or {}).get(name)
    axes = getattr(info, 'axes', None)
    if axes is None:
        axes = getattr(manager, 'axes', None) or []
    return list(axes)


def _forPositioning(setupInfo, name, manager) -> bool:
    info = (getattr(setupInfo, 'positioners', None) or {}).get(name)
    value = getattr(info, 'forPositioning', None)
    if value is None:
        value = getattr(manager, 'forPositioning', True)
    return bool(value)


def xyStageCandidates(setupInfo, master) -> List[str]:
    """Positioners that can carry a tiling run: at least two axes. Those
    with both an X and a Y axis come first."""
    found = []
    for name, manager in _devices(getattr(master, 'positionersManager', None)).items():
        if not _forPositioning(setupInfo, name, manager):
            continue
        axes = _axes(setupInfo, name, manager)
        if len(axes) >= 2:
            found.append((not {'X', 'Y'} <= set(axes), name))
    return [name for _, name in sorted(found)]


def zStageCandidates(setupInfo, master) -> List[str]:
    """Positioners with the Z axis the autofocus sweep moves."""
    return [name for name, manager in
            _devices(getattr(master, 'positionersManager', None)).items()
            if _Z_AXIS in _axes(setupInfo, name, manager)]


def acquisitionDetectors(master) -> List[str]:
    return [name for name, manager in
            _devices(getattr(master, 'detectorsManager', None)).items()
            if getattr(manager, 'forAcquisition', False)]


def _usable(manager) -> Tuple[bool, str]:
    try:
        from .devices.status import device_usable, not_connected_reason
    except Exception:  # noqa: BLE001
        return True, ''
    try:
        if device_usable(manager):
            return True, ''
        return False, not_connected_reason(manager) or 'not connected'
    except Exception:  # noqa: BLE001 -- status is advisory here
        return True, ''


# --------------------------------------------------------------------------
# Prerequisites
# --------------------------------------------------------------------------

def prerequisiteProblem(key: str, setupInfo, master) -> Optional[str]:
    """Why the running hardware cannot carry panel ``key`` at all, or None."""
    if key == 'Tiling':
        if not xyStageCandidates(setupInfo, master):
            return 'Needs a stage with at least two axes; this setup has none.'
        if not acquisitionDetectors(master):
            return 'Needs a detector that acquires; this setup has none.'
    elif key == 'Autofocus':
        if not zStageCandidates(setupInfo, master):
            return 'Needs a stage with a Z axis; this setup has none.'
        if not _devices(getattr(master, 'detectorsManager', None)):
            return 'Needs a detector; this setup has none.'
    return None


# --------------------------------------------------------------------------
# Proposed values
# --------------------------------------------------------------------------

def _tileStepFromFieldOfView(detectorName, manager) -> Tuple[Optional[float], str]:
    """A tile step that leaves TILE_OVERLAP between neighbours, from the
    detector's delivered frame size and sample-plane pixel size -- or None,
    with the reason, when the pixel size is only an assumed default."""
    try:
        from .managers.detectors.DetectorManager import (
            CAMERA_PIXEL_SIZE_SOURCE_PARAM, PIXEL_SIZE_SOURCE_DEFAULT,
        )
        source = manager.parameters.get(CAMERA_PIXEL_SIZE_SOURCE_PARAM)
        if source is not None and getattr(source, 'value', None) == PIXEL_SIZE_SOURCE_DEFAULT:
            return None, (f'the pixel size of {detectorName} is an assumed default, '
                          f'so the tile step was not derived from its field of view')
    except Exception:  # noqa: BLE001 -- not a camera with that parameter
        pass
    try:
        width, height = (int(v) for v in tuple(manager.shape)[:2])
        sizes = [float(v) for v in manager.pixelSizeUm]
        sizeY, sizeX = (sizes[-2], sizes[-1]) if len(sizes) >= 2 else (sizes[0], sizes[0])
        binning = 1
        if not getattr(manager, 'isScanDriven', False):
            binning = max(1, int(getattr(manager, 'binning', 1) or 1))
    except Exception:  # noqa: BLE001
        return None, f'the field of view of {detectorName} is not known'
    fieldX, fieldY = width * sizeX * binning, height * sizeY * binning
    if not (fieldX > 0 and fieldY > 0 and math.isfinite(fieldX) and math.isfinite(fieldY)):
        return None, f'the field of view of {detectorName} is not known'
    step = round(min(fieldX, fieldY) * (1.0 - TILE_OVERLAP), 1)
    return step, (f'tile step {step:g} µm: {int(TILE_OVERLAP * 100)} % overlap on the '
                  f'{fieldX:g} × {fieldY:g} µm field of view of {detectorName} '
                  f'({width} × {height} px at {sizeX * binning:g} × {sizeY * binning:g} µm)')


def proposeSection(key: str, setupInfo, master, base: Optional[dict] = None
                   ) -> Tuple[dict, List[str]]:
    """Values to open the configuration form with, and what was derived.

    ``base`` is the form's own defaults (``_build_default_section`` of the
    schema); the running setup fills in what it can tell: the one stage that
    fits, the one detector that acquires, a tile step from its field of view.
    """
    values = dict(base or {})
    notes: List[str] = []
    detectors = _devices(getattr(master, 'detectorsManager', None))
    acquiring = acquisitionDetectors(master)
    if key == 'Tiling':
        stages = xyStageCandidates(setupInfo, master)
        if stages:
            values['xyPositioner'] = stages[0]
            notes.append(f'XY positioner {stages[0]}: '
                         + ('the only stage with two axes' if len(stages) == 1
                            else f'the first of {len(stages)} stages with two axes'))
        if len(acquiring) == 1:
            values['camera'] = acquiring[0]
            notes.append(f'aligned on {acquiring[0]}: the only detector that acquires')
        reference = values.get('camera') or (acquiring[0] if acquiring else None)
        if reference in detectors:
            step, note = _tileStepFromFieldOfView(reference, detectors[reference])
            if step is not None:
                values['defaultTileStepUm'] = step
            notes.append(note)
    elif key == 'Autofocus':
        stages = zStageCandidates(setupInfo, master)
        if stages:
            values['positioner'] = stages[0]
            notes.append(f'Z positioner {stages[0]}: '
                         + ('the only stage with a Z axis' if len(stages) == 1
                            else f'the first of {len(stages)} stages with a Z axis'))
        if len(acquiring) == 1:
            values['camera'] = acquiring[0]
            notes.append(f'detector {acquiring[0]}: the only detector that acquires')
        camera = values.get('camera')
        if camera in detectors:
            try:
                width, height = (int(v) for v in tuple(detectors[camera].shape)[:2])
                values.update(frameCropx=0, frameCropy=0, frameCropw=width, frameCroph=height)
                notes.append(f'focus metric on the whole {width} × {height} px frame of {camera}')
            except Exception:  # noqa: BLE001
                pass
    return values, notes


# --------------------------------------------------------------------------
# Validation and the typed section
# --------------------------------------------------------------------------

def _number(data, field, errors, *, minimum=None, positive=False, integer=False,
            label=None):
    if field not in data:
        return  # the loader's default applies
    value = data.get(field)
    label = label or field
    if (value is None or isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value)):
        errors.append(f'{label} must be a number.')
        return
    if integer and int(value) != value:
        errors.append(f'{label} must be a whole number.')
    elif positive and value <= 0:
        errors.append(f'{label} must be more than 0.')
    elif minimum is not None and value < minimum:
        errors.append(f'{label} must be at least {minimum:g}.')


def validateSection(key: str, data: dict, setupInfo, master,
                    setupInfoType=None) -> List[str]:
    """What is wrong with ``data`` as this panel's section on this hardware,
    in words for the dialog. Empty when it is usable."""
    errors: List[str] = []
    positioners = _devices(getattr(master, 'positionersManager', None))
    detectors = _devices(getattr(master, 'detectorsManager', None))

    def stage(field, *, label, need):
        name = data.get(field) or ''
        if not name:
            if need:
                errors.append(f'Choose the {label}.')
            return None
        if name not in positioners:
            errors.append(f'{label}: there is no positioner "{name}" in this setup.')
            return None
        usable, why = _usable(positioners[name])
        if not usable:
            errors.append(f'{label} "{name}" is not connected ({why}).')
        return name

    def detector(field, *, label, need, acquiring):
        name = data.get(field) or ''
        if not name:
            if need:
                errors.append(f'Choose the {label}.')
            elif acquiring and not acquisitionDetectors(master):
                errors.append('This setup has no detector that acquires.')
            return None
        if name not in detectors:
            errors.append(f'{label}: there is no detector "{name}" in this setup.')
            return None
        if acquiring and not getattr(detectors[name], 'forAcquisition', False):
            errors.append(f'{label} "{name}" is not an acquisition detector '
                          f'(forAcquisition is off).')
        usable, why = _usable(detectors[name])
        if not usable:
            errors.append(f'{label} "{name}" is not connected ({why}).')
        return name

    if key == 'Tiling':
        name = stage('xyPositioner', label='XY positioner', need=True)
        if name is not None:
            axes = _axes(setupInfo, name, positioners[name])
            if len(axes) < 2:
                errors.append(f'XY positioner "{name}" has {len(axes)} axis '
                              f'({", ".join(axes) or "none"}); tiling moves two.')
        stage('zPositioner', label='Z positioner', need=False)
        detector('camera', label='Detector to align on', need=False, acquiring=True)
        _number(data, 'defaultTileStepUm', errors, positive=True, label='Default tile step')
        _number(data, 'defaultTilesX', errors, minimum=1, integer=True, label='Default tiles X')
        _number(data, 'defaultTilesY', errors, minimum=1, integer=True, label='Default tiles Y')
        if data.get('defaultPattern', 'spiral') not in _TILING_PATTERNS:
            errors.append(f'Default pattern must be one of {", ".join(_TILING_PATTERNS)}.')
        if data.get('mode', 'free-running') not in _TILING_MODES:
            errors.append(f'Mode must be one of {", ".join(_TILING_MODES)}.')
        if data.get('saveFormat', 'TIFF') not in _TILING_FORMATS:
            errors.append(f'Save format must be one of {", ".join(_TILING_FORMATS)}.')
        _number(data, 'settleTimeMs', errors, minimum=0, label='Settle time')
        if not isinstance(data.get('detectorTransforms', {}), dict):
            errors.append('Detector transforms must be a JSON object, e.g. {"APD1": "identity"}.')
    elif key == 'Autofocus':
        name = stage('positioner', label='Z positioner', need=True)
        if name is not None and _Z_AXIS not in _axes(setupInfo, name, positioners[name]):
            errors.append(f'Z positioner "{name}" has no Z axis; the focus search moves Z.')
        detector('camera', label='Detector', need=True, acquiring=False)
        for field in ('frameCropx', 'frameCropy'):
            _number(data, field, errors, minimum=0, integer=True)
        for field in ('frameCropw', 'frameCroph'):
            _number(data, field, errors, minimum=1, integer=True)
        _number(data, 'updateFreq', errors, minimum=1, integer=True, label='Update freq')
    if not errors and setupInfoType is not None:
        try:
            buildSection(key, data, setupInfoType)
        except Exception as e:  # noqa: BLE001 -- shown to the operator
            errors.append(f'The setup loader cannot read this section: {e}')
    return errors


def sectionKey(key: str) -> str:
    from .panels import PANELS
    return PANELS[key].section


def buildSection(key: str, data: dict, setupInfoType):
    """The typed section for ``data``, built the way the setup loader builds
    the whole file: the blank/unreadable-value pass, then ``from_dict``."""
    section = sectionKey(key)
    cleaned, _fixes = sanitizeSetupData({section: dict(data)}, setupInfoType)
    typed = getattr(setupInfoType.from_dict(cleaned, infer_missing=True), section)
    if typed is None:
        raise ValueError(f'no {section} section')
    return typed


def sectionSummary(typed) -> str:
    """One line naming the devices a typed section refers to."""
    if typed is None:
        return ''
    parts = []
    for field in dataclasses.fields(typed):
        if field.name in ('xyPositioner', 'positioner', 'camera', 'zPositioner'):
            value = getattr(typed, field.name)
            if value:
                parts.append(f'{field.name} {value}')
    return ', '.join(parts)
