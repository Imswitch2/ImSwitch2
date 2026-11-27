"""Remember the default tiling tilt calibration per hardware setup.

The legacy selection map now represents an explicit default. Merely loading
or recording another calibration no longer overwrites it. Compensation mode
always starts Off and is never persisted.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from imswitch.imcommon.model import dirtools


def preferences_path() -> Path:
    return Path(dirtools.UserFileDirs.Config) / 'tiling_tilt_preferences.json'


def _read(path: Path) -> dict:
    try:
        content = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    if not isinstance(content, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in content.items()
    ):
        raise ValueError('Invalid tiling tilt preferences; expected setup-to-path mapping')
    return content


def selected_calibration(setup_name: str, path: Path | None = None) -> str:
    if not setup_name:
        return ''
    return _read(path or preferences_path()).get(str(setup_name), '')


def remember_calibration(setup_name: str, calibration: str | Path,
                         path: Path | None = None) -> None:
    if not setup_name:
        return
    target = path or preferences_path()
    content = _read(target)
    content[str(setup_name)] = str(Path(calibration).expanduser().resolve())
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8',
                prefix='.tilt_', suffix='.tmp', dir=target.parent, delete=False) as handle:
            temp = handle.name
            json.dump(content, handle, indent=2)
            handle.write('\n')
        os.replace(temp, target)
        temp = None
    finally:
        if temp is not None and os.path.exists(temp):
            os.unlink(temp)
