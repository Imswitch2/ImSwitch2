"""Where ImProcess's open and save dialogs start, kept per computer.

Preferences > Default folders… edits them. They describe the computer --
which disk the data is on -- so they live in ``improcess_options.json`` in
the ImSwitch config folder, next to ``imcontrol_options.json``, and not in a
setup file or the widget state. Until they were kept here they lasted only
until ImSwitch closed.

An empty folder means no default: the dialog starts wherever the system puts
it, and the save dialogs fall back to the data folder. Keys this module does
not know are left in the file untouched, so a later version's settings
survive a round trip through this one.
"""

import json
import os
from dataclasses import dataclass

from imswitch.imcommon.model import dirtools, initLogger

OPTIONS_FILENAME = 'improcess_options.json'

_FIELDS = ('dataFolder', 'saveFolder')


@dataclass(frozen=True)
class FolderPreferences:
    #: Where the open dialogs (Quick load, Virtual load, add data) start.
    dataFolder: str = ''
    #: Where the save dialogs start; the data folder when empty.
    saveFolder: str = ''


def options_file_path() -> str:
    return os.path.join(dirtools.UserFileDirs.Config, OPTIONS_FILENAME)


def _read(path) -> dict:
    with open(path, encoding='utf-8') as file:
        content = json.load(file)
    if not isinstance(content, dict):
        raise ValueError(f'expected a JSON object, found {type(content).__name__}')
    return content


def load_folder_preferences(path=None) -> FolderPreferences:
    """What the options file holds; the defaults when it is missing.

    Never raises: this runs while ImProcess starts. An unreadable file is
    reported and the defaults stand, and a value that is not a string is
    treated as unset.
    """
    path = path or options_file_path()
    try:
        content = _read(path)
    except FileNotFoundError:
        return FolderPreferences()
    except Exception as error:  # noqa: BLE001 -- startup must go on
        initLogger('ImProcessFolders').warning(
            f'Could not read the default folders from {path} ({error}); '
            f'the open and save dialogs start where the system puts them.'
        )
        return FolderPreferences()
    values = {field: content.get(field) for field in _FIELDS}
    return FolderPreferences(**{
        field: value if isinstance(value, str) else '' for field, value in values.items()
    })


def save_folder_preferences(preferences: FolderPreferences, path=None) -> None:
    """Write the folders, keeping every other key the file holds.

    A file that cannot be read as a JSON object is replaced rather than
    merged into. Raises OSError when the file cannot be written.
    """
    path = path or options_file_path()
    try:
        content = _read(path)
    except Exception:  # noqa: BLE001 -- missing or unreadable: start afresh
        content = {}
    for field in _FIELDS:
        content[field] = str(getattr(preferences, field) or '')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as file:
        json.dump(content, file, indent=4, sort_keys=True)
