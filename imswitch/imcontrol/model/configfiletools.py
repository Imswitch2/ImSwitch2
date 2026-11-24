import dataclasses
import json
import os
import sys

from imswitch.imcommon.model import dirtools
from . import setupfolder
from .Options import Options


_LEGACY_SETUP_INFO_KEYS = {'defaultLaserPresetForScan'}


def getSetupList(folder=None):
    """ The setup files in ``folder`` -- by default this computer's setup
    folder -- and its subfolders, named relative to it ('/' separators):
    those at the top first, then each subfolder in turn. """
    return setupfolder.listSetupFiles(getSetupFilesDir() if folder is None else folder)


def _defaultSetupFileName(setupList):
    """Choose the safest first-run setup deterministically."""
    if 'example_no_hardware.json' in setupList:
        return 'example_no_hardware.json'
    if setupList:
        return setupList[0]
    return ''


def getSetupFilesDir(options=None):
    """ The folder this computer keeps its hardware setup files in: the
    ``setupFolder`` of ``options`` (by default the options in use), or
    ``<ImSwitchConfig>/imcontrol_setups`` when that is not set. """
    if options is None:
        options, _ = loadOptions()
    return setupfolder.setupFolderOf(getattr(options, 'setupFolder', ''))


def getSetupFilePath(setupFileName, options=None):
    """ Where a setup file of this name lives, whether or not it exists yet:
    ``setupFileName`` is relative to the setup folder of ``options`` (by
    default the options in use), or absolute. """
    return setupfolder.setupFilePath(getSetupFilesDir(options), setupFileName)


def loadSetupInfo(options, setupInfoType):
    with open(getSetupFilePath(options.setupFileName, options)) as setupFile:
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
    with open(getSetupFilePath(options.setupFileName, options), 'w') as setupFile:
        json.dump(pruneDefaultSetupInfoFields(setupInfo), setupFile, indent=4)


def loadOptions():
    """ The options, naming the setup this run uses: the one given with
    ``--setup`` (see useSetupForThisRun), otherwise the options file's. """
    global _options

    optionsDidNotExist = False
    if _options is None:
        if not os.path.isfile(_optionsFilePath):
            _options = Options(
                setupFileName=_defaultSetupFileName(
                    getSetupList(setupfolder.defaultSetupFolder())
                )
            )
            optionsDidNotExist = True
        else:
            with open(_optionsFilePath, 'r') as optionsFile:
                _options = Options.from_json(optionsFile.read(), infer_missing=True)
            _options = _followMovedSetup(_options)

    if _setupForThisRun is not None:
        return dataclasses.replace(_options, setupFileName=_setupForThisRun), optionsDidNotExist
    return _options, optionsDidNotExist


def _followMovedSetup(options):
    """ Options naming a setup that is no longer where they say, while
    exactly one setup of that file name is in a subfolder of the setup
    folder, name that one instead -- the setup was sorted into a subfolder,
    by the user or by an update that sorted the shipped examples. Written
    back, so that the Config Studio and the next start agree; a name that
    matches nothing, or several setups, is left for the setup picker. """
    name = options.setupFileName
    found = setupfolder.findSetupFile(getSetupFilesDir(options), name)
    if found is None or found == name:
        return options
    moved = dataclasses.replace(options, setupFileName=found)
    from imswitch.imcommon.model import initLogger
    logger = initLogger('setup files')
    logger.info(f'Setup "{name}" is now "{found}" in the setup folder; using it')
    try:
        with open(_optionsFilePath, 'w') as optionsFile:
            optionsFile.write(moved.to_json(indent=4))
    except OSError as error:
        logger.warning(f'Could not record the new place of the setup in the options: {error}')
    return moved


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
    name of a file in the setup folder (or in one of its subfolders, as
    ``subfolder/name``), with or without ``.json``, or its path. A bare file
    name that only one subfolder holds finds that one. Raises ValueError,
    saying what there is to choose from, otherwise. """
    folder = getSetupFilesDir()
    name = value.strip()
    if os.path.dirname(name):
        path = os.path.realpath(name)
        root = os.path.realpath(folder)
        if os.path.normcase(path).startswith(os.path.normcase(root) + os.sep):
            name = setupfolder.setupNameFor(path, root)
        elif os.path.isabs(name) or os.path.isfile(path):
            raise ValueError(f'"{value}" is not in the setup folder {folder}; '
                             f'copy it there first.')
        # otherwise a name relative to the setup folder, e.g. "mock/rig"
    if not name.lower().endswith('.json'):
        name += '.json'

    setups = getSetupList(folder)
    for setup in setups:
        if os.path.normcase(setup) == os.path.normcase(name):
            return setup
    found = setupfolder.findSetupFile(folder, name)
    if found is not None:
        return found
    raise ValueError(f'There is no setup "{name}" in {folder}. '
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
os.makedirs(setupfolder.defaultSetupFolder(), exist_ok=True)
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
