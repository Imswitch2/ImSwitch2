import dataclasses
import glob
import json
import os
import sys
from pathlib import Path

from imswitch.imcommon.model import dirtools
from .Options import Options


_LEGACY_SETUP_INFO_KEYS = {'defaultLaserPresetForScan'}


def getSetupList():
    return sorted(Path(file).name for file in glob.glob(os.path.join(_setupFilesDir, '*.json')))


def _defaultSetupFileName(setupList):
    """Choose the safest first-run setup deterministically."""
    if 'example_no_hardware.json' in setupList:
        return 'example_no_hardware.json'
    if setupList:
        return setupList[0]
    return ''


def getSetupFilesDir():
    """ The directory this installation keeps its hardware setup files in. """
    return _setupFilesDir


def getSetupFilePath(setupFileName):
    """ Where a setup file of this name lives, whether or not it exists yet. """
    return os.path.join(_setupFilesDir, setupFileName)


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
    return data


def saveSetupInfo(options, setupInfo):
    with open(os.path.join(_setupFilesDir, options.setupFileName), 'w') as setupFile:
        json.dump(pruneDefaultSetupInfoFields(setupInfo), setupFile, indent=4)


def loadOptions():
    """ The options, naming the setup this run uses: the one given with
    ``--setup`` (see useSetupForThisRun), otherwise the options file's. """
    global _options

    optionsDidNotExist = False
    if _options is None:
        if not os.path.isfile(_optionsFilePath):
            _options = Options(
                setupFileName=_defaultSetupFileName(getSetupList())
            )
            optionsDidNotExist = True
        else:
            with open(_optionsFilePath, 'r') as optionsFile:
                _options = Options.from_json(optionsFile.read(), infer_missing=True)

    if _setupForThisRun is not None:
        return dataclasses.replace(_options, setupFileName=_setupForThisRun), optionsDidNotExist
    return _options, optionsDidNotExist


def saveOptions(options):
    """ Write the options file. While a ``--setup`` setup is in force the file
    keeps naming the setup it named; only saveSetupChoice() changes that. """
    if _setupForThisRun is not None:
        loadOptions()
        options = dataclasses.replace(options, setupFileName=_options.setupFileName)
    _writeOptions(options)


def saveSetupChoice(setupFileName):
    """ Make ``setupFileName`` the setup ImSwitch starts with, as picking a
    setup does. A setup given with ``--setup`` stays in force until this run
    ends, so restart with restartArgumentsForSetupChoice() to come up with the
    choice. """
    loadOptions()
    _writeOptions(dataclasses.replace(_options, setupFileName=setupFileName))


def _writeOptions(options):
    global _options

    _options = options
    with open(_optionsFilePath, 'w') as optionsFile:
        optionsFile.write(_options.to_json(indent=4))


def resolveSetupArgument(value):
    """ The setup file ``imswitch --setup <value>`` asks for: ``value`` is the
    name of a file in the setups folder, with or without ``.json``, or its
    path. Raises ValueError, saying what there is to choose from, otherwise. """
    name = value.strip()
    if os.path.dirname(name):
        path = os.path.realpath(name)
        if os.path.normcase(os.path.dirname(path)) != os.path.normcase(os.path.realpath(_setupFilesDir)):
            raise ValueError(f'"{value}" is not in the setups folder {_setupFilesDir}; '
                             f'copy it there first.')
        name = os.path.basename(path)
    if not name.lower().endswith('.json'):
        name += '.json'

    setups = getSetupList()
    for setup in setups:
        if os.path.normcase(setup) == os.path.normcase(name):
            return setup
    raise ValueError(f'There is no setup "{name}" in {_setupFilesDir}. '
                     f'Available: {", ".join(setups) or "none"}.')


def useSetupForThisRun(setupFileName):
    """ Run with ``setupFileName`` (``imswitch --setup``) while the options
    file keeps naming the setup ImSwitch normally starts with.

    loadOptions() names it from now on, so a change saved to the running
    setup lands in its file, and saveOptions() leaves the options file's
    setup as it is. """
    global _setupForThisRun
    _setupForThisRun = setupFileName


def setupForThisRun():
    """ The setup given with ``--setup``, or None. """
    return _setupForThisRun


def restartArgumentsForSetupChoice():
    """ This run's command-line arguments without ``--setup``, which would
    otherwise override the setup just chosen when ImSwitch comes back up. """
    arguments = []
    skipValue = False
    for argument in sys.argv[1:]:
        if skipValue:
            skipValue = False
        elif argument == SETUP_ARGUMENT:
            skipValue = True
        elif not argument.startswith(SETUP_ARGUMENT + '='):
            arguments.append(argument)
    return arguments


SETUP_ARGUMENT = '--setup'

dirtools.initUserFilesIfNeeded()
_setupFilesDir = os.path.join(dirtools.UserFileDirs.Root, 'imcontrol_setups')
os.makedirs(_setupFilesDir, exist_ok=True)
_optionsFilePath = os.path.join(dirtools.UserFileDirs.Config, 'imcontrol_options.json')

_options = None  # as the options file holds them
_setupForThisRun = None


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
