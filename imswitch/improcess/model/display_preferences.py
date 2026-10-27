"""Which image display ImProcess uses, kept per computer.

Preferences > Image display edits :attr:`DisplayPreferences.imageDisplay`. It
is a property of the computer and the person using it (a big monitor and a
habit of ImageJ windows), so it lives in ``improcess_options.json`` in the
ImSwitch config folder with the other per-computer options, and not in a setup
file. Keys this module does not know are left in the file untouched.

The display is chosen when ImProcess starts: the embedded viewer is built into
the main window, so there is no live switch.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from imswitch.imcommon.model import initLogger
from imswitch.improcess.model.folder_preferences import options_file_path

#: The embedded napari viewer (the default).
IMAGE_DISPLAY_NAPARI = "napari"
#: One free-floating pyqtgraph window per result.
IMAGE_DISPLAY_WINDOWS = "windows"
IMAGE_DISPLAY_CHOICES = (IMAGE_DISPLAY_NAPARI, IMAGE_DISPLAY_WINDOWS)

_KEY = "imageDisplay"


@dataclass(frozen=True)
class DisplayPreferences:
    imageDisplay: str = IMAGE_DISPLAY_NAPARI


def _read(path) -> dict:
    with open(path, encoding="utf-8") as file:
        content = json.load(file)
    if not isinstance(content, dict):
        raise ValueError(f"expected a JSON object, found {type(content).__name__}")
    return content


def load_display_preferences(path=None) -> DisplayPreferences:
    """What the options file holds; the defaults when it is missing. Never raises.

    A value that is not one of :data:`IMAGE_DISPLAY_CHOICES` counts as unset,
    so an unreadable or hand-edited file leaves the embedded viewer in place.
    """
    path = path or options_file_path()
    try:
        content = _read(path)
    except FileNotFoundError:
        return DisplayPreferences()
    except Exception as error:  # noqa: BLE001 -- startup must go on
        initLogger("ImProcessDisplay").warning(
            f"Could not read the image display preference from {path} ({error}); "
            "using the embedded viewer."
        )
        return DisplayPreferences()
    value = content.get(_KEY)
    return DisplayPreferences(
        imageDisplay=value if value in IMAGE_DISPLAY_CHOICES else IMAGE_DISPLAY_NAPARI
    )


def save_display_preferences(preferences: DisplayPreferences, path=None) -> None:
    """Write the preference, keeping every other key the file holds.

    A file that cannot be read as a JSON object is replaced rather than merged
    into. Raises OSError when the file cannot be written.
    """
    path = path or options_file_path()
    try:
        content = _read(path)
    except Exception:  # noqa: BLE001 -- missing or unreadable: start afresh
        content = {}
    content[_KEY] = preferences.imageDisplay
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(content, file, indent=4, sort_keys=True)
