import glob
import hashlib
import json
import os
from abc import ABC
from dataclasses import dataclass, field
from pathlib import Path
from shutil import copy2

import imswitch


def getSystemUserDir():
    """ Returns the user's documents folder if they are using a Windows system,
    or their home folder if they are using another operating system. """

    if os.name == 'nt':  # Windows system, try to return documents directory
        try:
            import ctypes.wintypes
            CSIDL_PERSONAL = 5  # Documents
            SHGFP_TYPE_CURRENT = 0  # Current value

            buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
            ctypes.windll.shell32.SHGetFolderPathW(0, CSIDL_PERSONAL, 0, SHGFP_TYPE_CURRENT, buf)

            return buf.value
        except ImportError:
            pass
        #TOOD: How can we ensure that configuration files are updated automatically.. 
    return os.path.expanduser('~')  # Non-Windows system, return home directory


_baseDataFilesDir = os.path.join(os.path.dirname(os.path.realpath(imswitch.__file__)), '_data')
_baseUserFilesDir = os.path.join(getSystemUserDir(), 'ImSwitchConfig')

#: sha256 of every version ever shipped of each default file, written by
#: tools/update_user_defaults_history.py. It is how an untouched copy of an
#: older default is told apart from one the user edited.
_defaultsHistoryPath = os.path.join(_baseDataFilesDir, 'user_defaults_history.json')

#: Only files under these user-folder subdirectories are ever deleted: a
#: script is self-contained, while a setup or SLM file may be what a rig's
#: options name as its configuration.
_REMOVABLE_ROOTS = ('scripts',)

_userDefaultsSynced = False


def isShippedDefault(relativePath):
    """ Whether a file under user_defaults is one that is copied to the user
    folder (readme.txt files, caches and Finder metadata are not). """
    parts = Path(relativePath).parts
    name = parts[-1]
    return not (
        name.lower() == 'readme.txt'
        or name == '.DS_Store'
        or '__pycache__' in parts
        or name.endswith(('.pyc', '.pyo'))
    )


def defaultContentHash(data):
    """ sha256 of a default file's bytes, line endings normalised: a checkout
    with CRLF endings holds the same file as one with LF. """
    return hashlib.sha256(data.replace(b'\r\n', b'\n')).hexdigest()


def defaultFileHash(path):
    with open(path, 'rb') as file:
        return defaultContentHash(file.read())


def loadDefaultsHistory(path=None):
    """ {relative path: set of hashes}; empty when the file is missing or
    unreadable, which leaves only the copying of missing files. """
    try:
        with open(path or _defaultsHistoryPath, 'r', encoding='utf-8') as file:
            files = json.load(file)['files']
        return {relativePath: set(hashes) for relativePath, hashes in files.items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


@dataclass
class UserDefaultsReport:
    """ What syncUserDefaults did, as paths relative to the user folder. """
    copied: list = field(default_factory=list)
    updated: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    keptEdited: list = field(default_factory=list)
    failed: list = field(default_factory=list)


def syncUserDefaults(sourceRoot, userRoot, history, *, trash=None):
    """ Brings the user folder's copies of the shipped defaults up to date.

    * A default the user folder does not have is copied, as it always was.
    * A copy that is an older shipped version -- its hash is in ``history``
      -- was never edited, so it is replaced by the current version.
    * A copy with any other content was edited (by the user, or by ImSwitch
      saving a setup) and is kept.
    * A file ImSwitch no longer ships is moved to the trash when it is an
      untouched shipped version and lives under a removable root (scripts).

    ``trash(path)`` deletes a file; it defaults to send2trash. Every failure
    is recorded rather than raised: this runs at startup.
    """
    sourceRoot, userRoot = Path(sourceRoot), Path(userRoot)
    report = UserDefaultsReport()

    shipped = {}
    for file in glob.glob(os.path.join(sourceRoot, '**'), recursive=True):
        filePath = Path(file)
        if filePath.is_file():
            relativePath = filePath.relative_to(sourceRoot).as_posix()
            if isShippedDefault(relativePath):
                shipped[relativePath] = filePath

    for relativePath, sourcePath in sorted(shipped.items()):
        destination = userRoot / relativePath
        try:
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                copy2(sourcePath, destination)
                report.copied.append(relativePath)
                continue
            if not destination.is_file():
                continue
            current = defaultFileHash(destination)
            if current == defaultFileHash(sourcePath):
                continue
            if current in history.get(relativePath, ()):
                copy2(sourcePath, destination)
                report.updated.append(relativePath)
            else:
                report.keptEdited.append(relativePath)
        except OSError as error:
            report.failed.append(f'{relativePath}: {error}')

    if trash is None:
        from send2trash import send2trash as trash
    for relativePath in sorted(set(history) - set(shipped)):
        if not relativePath.startswith(tuple(root + '/' for root in _REMOVABLE_ROOTS)):
            continue
        destination = userRoot / relativePath
        try:
            if not destination.is_file():
                continue
            if defaultFileHash(destination) not in history[relativePath]:
                continue  # edited: it is the user's now
            trash(str(destination))
            report.removed.append(relativePath)
            _removeEmptyParents(destination.parent, userRoot)
        except Exception as error:  # send2trash raises its own types
            report.failed.append(f'{relativePath}: {error}')
    return report


def _removeEmptyParents(directory, userRoot):
    """ Removes directories left empty by a removal, up to (not including)
    the user folder's top-level subdirectory. """
    directory = Path(directory)
    while directory != userRoot and directory.parent != userRoot:
        try:
            directory.rmdir()  # only succeeds when empty
        except OSError:
            return
        directory = directory.parent


def initUserFilesIfNeeded():
    """ Initializes all directories that will be used to store user data, and
    copies the shipped default files -- or brings untouched copies of older
    ones up to date (see syncUserDefaults). The sync runs once per process. """
    global _userDefaultsSynced

    # Initialize directories
    for userFileDir in UserFileDirs.list():
        os.makedirs(userFileDir, exist_ok=True)

    if _userDefaultsSynced:
        return
    _userDefaultsSynced = True

    report = syncUserDefaults(DataFileDirs.UserDefaults, _baseUserFilesDir,
                              loadDefaultsHistory())
    _logUserDefaultsReport(report)


def _logUserDefaultsReport(report):
    if not (report.updated or report.removed or report.keptEdited or report.failed):
        return
    from .logging import initLogger

    logger = initLogger('user files')
    if report.updated:
        logger.info(f'Updated {len(report.updated)} default file(s) you had not edited '
                    f'to this version: {", ".join(report.updated)}')
    if report.removed:
        logger.info(f'Moved {len(report.removed)} default file(s) ImSwitch no longer ships, '
                    f'and you had not edited, to the trash: {", ".join(report.removed)}')
    if report.keptEdited:
        logger.info(f'Kept {len(report.keptEdited)} edited file(s) that differ from this '
                    f'version\'s defaults (delete one and restart ImSwitch to get the '
                    f'current version): {", ".join(report.keptEdited)}')
    for failure in report.failed:
        logger.warning(f'Could not update a default file: {failure}')


class FileDirs(ABC):
    """ Base class for directory catalog classes. """

    @classmethod
    def list(cls):
        """ Returns all directories in the catalog. """
        return [cls.__dict__.get(name) for name in dir(cls)
                if not callable(getattr(cls, name)) and not name.startswith('_')]


class DataFileDirs(FileDirs):
    """ Catalog of directories that contain program data/library/resource
    files. """
    Root = _baseDataFilesDir
    Libs = os.path.join(_baseDataFilesDir, 'libs')
    UserDefaults = os.path.join(_baseDataFilesDir, 'user_defaults')


class UserFileDirs(FileDirs):
    """ Catalog of directories that contain user configuration files. """
    Root = _baseUserFilesDir
    Config = os.path.join(_baseUserFilesDir, 'config')


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