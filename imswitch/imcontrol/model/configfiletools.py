import dataclasses
import glob
import json
import os
from pathlib import Path

from imswitch.imcommon.model import dirtools
from .Options import Options


_LEGACY_SETUP_INFO_KEYS = {'defaultLaserPresetForScan'}
_SLMS_NULL_COMPAT_KEYS = {
    'analogChannel',
    'digitalLine',
    'serial_number',
    'width',
    'height',
    'pixelSize',
    'monitorIdx',
    'correctionPatternsDir',
    'wavelengthTableFile',
    'nSections',
    'widgetOptions',
}


def getSetupList():
    return [Path(file).name for file in glob.glob(os.path.join(_setupFilesDir, '*.json'))]


def loadSetupInfo(options, setupInfoType):
    with open(os.path.join(_setupFilesDir, options.setupFileName)) as setupFile:
        return setupInfoType.from_json(setupFile.read(), infer_missing=True)


def pruneDefaultSetupInfoFields(setupInfo) -> dict:
    """Serialize a SetupInfo to a dict, omitting top-level fields whose value
    equals the field default (e.g. ``rotators: null``, an all-default
    ``nidaq`` section).

    loadSetupInfo() parses with ``infer_missing=True``, so a missing key and
    an explicit default/null value load identically — the round-trip is
    lossless. Without this, every full-file rewrite (laser preset save,
    camera ROI save) pollutes hand-maintained setup files with machine-added
    default sections for hardware the setup does not have.
    """
    data = setupInfo.to_dict()
    for key in _LEGACY_SETUP_INFO_KEYS:
        data.pop(key, None)
    catchAll = data.get('_catchAll')
    if isinstance(catchAll, dict):
        for key in _LEGACY_SETUP_INFO_KEYS:
            catchAll.pop(key, None)
        if not catchAll:
            data.pop('_catchAll', None)
    for fieldInfo in dataclasses.fields(type(setupInfo)):
        name = fieldInfo.name
        if name not in data:
            continue
        if fieldInfo.default is not dataclasses.MISSING:
            default = fieldInfo.default
        elif fieldInfo.default_factory is not dataclasses.MISSING:
            default = fieldInfo.default_factory()
        else:
            continue
        if getattr(setupInfo, name) == default:
            del data[name]
    _pruneSlmsNullCompatFields(data)
    return data


def _pruneSlmsNullCompatFields(data: dict) -> None:
    slms = data.get('slms')
    if not isinstance(slms, dict):
        return
    for slmInfo in slms.values():
        if not isinstance(slmInfo, dict):
            continue
        for key in _SLMS_NULL_COMPAT_KEYS:
            if slmInfo.get(key) is None:
                slmInfo.pop(key, None)


def saveSetupInfo(options, setupInfo):
    with open(os.path.join(_setupFilesDir, options.setupFileName), 'w') as setupFile:
        json.dump(pruneDefaultSetupInfoFields(setupInfo), setupFile, indent=4)


def loadOptions():
    global _options

    if _options is not None:
        return _options, False

    optionsDidNotExist = False
    if not os.path.isfile(_optionsFilePath):
        _options = Options(
            setupFileName=getSetupList()[0]
        )
        optionsDidNotExist = True
    else:
        with open(_optionsFilePath, 'r') as optionsFile:
            _options = Options.from_json(optionsFile.read(), infer_missing=True)

    return _options, optionsDidNotExist


def saveOptions(options):
    global _options

    _options = options
    with open(_optionsFilePath, 'w') as optionsFile:
        optionsFile.write(_options.to_json(indent=4))


dirtools.initUserFilesIfNeeded()
_setupFilesDir = os.path.join(dirtools.UserFileDirs.Root, 'imcontrol_setups')
os.makedirs(_setupFilesDir, exist_ok=True)
_optionsFilePath = os.path.join(dirtools.UserFileDirs.Config, 'imcontrol_options.json')

_options = None


# Copyright (C) 2020-2021 ImSwitch developers
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
