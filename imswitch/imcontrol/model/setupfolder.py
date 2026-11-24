"""Where hardware setup files live, and how one is named within that place.

The setup folder is a property of the computer, not of a microscope: it is
``setupFolder`` in ``imcontrol_options.json`` (Preferences > Setup folder…),
and empty there means the default, ``<ImSwitchConfig>/imcontrol_setups``,
where ImSwitch also puts its shipped example setups.

Setups may be sorted into subfolders. A setup is named by its path relative
to the setup folder, with ``/`` separators on every platform (so an options
file can move between machines): ``my_rig.json``,
``mock/point_scanning/simple_mock_setup.json``. A setup outside the folder is
named by its absolute path.

No import side effects: unlike ``configfiletools``, importing this module
does not copy the shipped defaults into the user folder, so the Config Studio
and the tests can use it freely.
"""

import os
from pathlib import Path, PurePosixPath

from imswitch.imcommon.model import dirtools

#: Name of the default setup folder inside the ImSwitch user folder, and of
#: the folder the shipped example setups live in.
DEFAULT_FOLDER_NAME = 'imcontrol_setups'

#: How many subfolder levels below the setup folder are searched for setups,
#: and how many folders at most. Bounded so that pointing the setting at a
#: large folder (a whole drive) cannot stall the setup list.
MAX_DEPTH = 4
MAX_FOLDERS = 500


def defaultSetupFolder() -> str:
    """The setup folder used when ``setupFolder`` is not set."""
    return os.path.join(dirtools.UserFileDirs.Root, DEFAULT_FOLDER_NAME)


def shippedSetupFolder() -> str:
    """The example setups that ship with ImSwitch (copied into the default
    setup folder at startup)."""
    return os.path.join(dirtools.DataFileDirs.UserDefaults, DEFAULT_FOLDER_NAME)


def setupFolderOf(setting) -> str:
    """The folder a ``setupFolder`` setting names; the default when empty."""
    folder = os.path.expanduser(str(setting or '').strip())
    return os.path.normpath(folder) if folder else defaultSetupFolder()


def setupFolderSetting(folder) -> str:
    """What to store as ``setupFolder`` for ``folder``: '' for the default
    folder, so that a moved Documents folder keeps working, else the
    normalised path."""
    folder = os.path.normpath(os.path.expanduser(str(folder or '').strip())) if folder else ''
    if not folder or _samePath(folder, defaultSetupFolder()):
        return ''
    return folder


def _isSkippedFolder(name) -> bool:
    return name.startswith('.') or name == '__pycache__'


def _sortKey(name):
    """Setups at the top of the folder first, then each subfolder in turn;
    case-insensitive alphabetical within each."""
    parts = PurePosixPath(name).parts
    return (len(parts) > 1, tuple(part.casefold() for part in parts))


def listSetupFiles(folder) -> list:
    """Every ``*.json`` setup file in ``folder`` and its subfolders, named
    relative to ``folder`` with ``/`` separators. Empty when the folder does
    not exist. Hidden folders and ``__pycache__`` are skipped, and at most
    MAX_DEPTH subfolder levels and MAX_FOLDERS folders are searched."""
    root = Path(folder)
    if not root.is_dir():
        return []
    names = []
    for visited, (current, dirs, files) in enumerate(os.walk(root)):
        if visited >= MAX_FOLDERS:
            break
        depth = len(Path(current).relative_to(root).parts)
        if depth >= MAX_DEPTH:
            dirs[:] = []
        else:
            dirs[:] = [d for d in dirs if not _isSkippedFolder(d)]
        for file in files:
            if file.lower().endswith('.json') and not file.startswith('.'):
                names.append((Path(current) / file).relative_to(root).as_posix())
    return sorted(names, key=_sortKey)


def setupFilePath(folder, name) -> str:
    """Where the setup named ``name`` lives, whether or not it exists yet.
    An absolute ``name`` is returned as it is."""
    return os.path.normpath(os.path.join(folder, str(name)))


def findSetupFile(folder, name):
    """The name under which ``folder`` holds the setup ``name`` refers to.

    ``name`` itself when that file exists. Otherwise, for a bare file name,
    the one setup of that file name in a subfolder -- so a setup keeps
    working after it was sorted into a subfolder, by the user or by an
    ImSwitch update that sorted the shipped examples. ``None`` when there is
    no such setup, or several, which a guess must not choose between.
    """
    name = str(name or '')
    if not name:
        return None
    if os.path.isfile(setupFilePath(folder, name)):
        return name
    if os.path.isabs(name) or PurePosixPath(name.replace('\\', '/')).name != name:
        return None
    matches = [candidate for candidate in listSetupFiles(folder)
               if PurePosixPath(candidate).name == name]
    return matches[0] if len(matches) == 1 else None


def setupNameFor(path, folder) -> str:
    """How ``imcontrol_options.json`` names the setup file at ``path``: its
    path relative to ``folder`` with ``/`` separators when it is inside
    ``folder``, else its absolute path."""
    path = os.path.abspath(str(path))
    folder = os.path.abspath(str(folder))
    try:
        relative = os.path.relpath(path, folder)
    except ValueError:  # another drive on Windows
        return path
    if relative == os.pardir or relative.startswith(os.pardir + os.sep) or os.path.isabs(relative):
        return path
    return PurePosixPath(*Path(relative).parts).as_posix()


def shippedSetupPath(name) -> Path:
    """The shipped example setup ``name`` refers to (a bare file name is
    enough), for tests and tools. Raises FileNotFoundError when there is
    none."""
    folder = shippedSetupFolder()
    found = findSetupFile(folder, name)
    if found is None:
        raise FileNotFoundError(f'No shipped setup named {name!r} under {folder}')
    return Path(setupFilePath(folder, found))


def _samePath(left, right) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


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
