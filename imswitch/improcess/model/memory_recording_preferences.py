"""What ImProcess does with a recording ImControl kept in memory.

A recording saved with *Save in memory for reconstruction* (or *Save on disk
and keep in memory*) reaches ImProcess the moment it is finished, over the
module channel. This preference decides what happens then:

- ``list``: it appears as a row in the Multidata list, to be opened by hand
  (the historical behaviour);
- ``current``: it is opened as the current data straight away;
- ``reconstruct``: it is opened as the current data and run through the
  active reconstructor, so the result is in the results list before the user
  looks.

It is a property of how the computer is used rather than of the microscope,
so it lives in ``improcess_options.json`` next to the default folders, and
never in a setup file. Keys this module does not know are left untouched.
"""

import json
import os

from imswitch.imcommon.model import initLogger

from .folder_preferences import options_file_path

POLICY_LIST = 'list'
POLICY_CURRENT = 'current'
POLICY_RECONSTRUCT = 'reconstruct'
POLICIES = (POLICY_LIST, POLICY_CURRENT, POLICY_RECONSTRUCT)
DEFAULT_POLICY = POLICY_LIST

KEY = 'memoryRecordingsPolicy'


def normalize_policy(value) -> str:
    """``value`` as one of :data:`POLICIES`, else the default."""
    text = str(value or '').strip().lower()
    return text if text in POLICIES else DEFAULT_POLICY


def _read(path) -> dict:
    with open(path, encoding='utf-8') as file:
        content = json.load(file)
    if not isinstance(content, dict):
        raise ValueError(f'expected a JSON object, found {type(content).__name__}')
    return content


def load_memory_recording_policy(path=None) -> str:
    """The stored policy; the default when the file is missing or unreadable.

    Never raises: this runs while ImProcess starts.
    """
    path = path or options_file_path()
    try:
        content = _read(path)
    except FileNotFoundError:
        return DEFAULT_POLICY
    except Exception as error:  # noqa: BLE001 -- startup must go on
        initLogger('ImProcessMemoryRecordings').warning(
            f'Could not read the memory-recording policy from {path} ({error}); '
            f'recordings kept in memory are listed and not opened.'
        )
        return DEFAULT_POLICY
    return normalize_policy(content.get(KEY))


def save_memory_recording_policy(policy, path=None) -> None:
    """Write the policy, keeping every other key the file holds.

    Raises OSError when the file cannot be written.
    """
    path = path or options_file_path()
    try:
        content = _read(path)
    except Exception:  # noqa: BLE001 -- missing or unreadable: start afresh
        content = {}
    content[KEY] = normalize_policy(policy)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as file:
        json.dump(content, file, indent=4, sort_keys=True)


__all__ = [
    'DEFAULT_POLICY', 'KEY', 'POLICIES', 'POLICY_CURRENT', 'POLICY_LIST',
    'POLICY_RECONSTRUCT', 'load_memory_recording_policy', 'normalize_policy',
    'save_memory_recording_policy',
]
