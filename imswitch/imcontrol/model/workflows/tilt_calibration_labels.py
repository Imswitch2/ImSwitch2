"""User-facing tilt calibration names, separate from immutable fit data.

The fitted ``tilt_fit.json`` is SHA-256 identified by compensation reports.
Changing a display name must never rewrite or re-hash that scientific data.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path


_METADATA_NAME = 'tilt_metadata.json'


def calibration_name(fit_path: str | Path) -> str:
    """Optional user label; absent/legacy calibrations return an empty string."""
    path = Path(fit_path).parent / _METADATA_NAME
    try:
        metadata = json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        # Names are optional UI metadata and must not make a valid fit unusable.
        return ''
    if not isinstance(metadata, dict) or not isinstance(metadata.get('name'), str):
        return ''
    return metadata['name'].strip()


def calibration_display_name(fit_path: str | Path) -> str:
    """Human-readable name, with a stable fallback for older calibrations."""
    path = Path(fit_path)
    name = calibration_name(path)
    if name:
        return name
    try:
        metadata = json.loads(path.read_text(encoding='utf-8'))
        created = metadata.get('created_utc', '')
        if isinstance(created, str) and created:
            stamp = datetime.fromisoformat(created.replace('Z', '+00:00'))
            return f'Calibration {stamp:%Y-%m-%d %H:%M}'
    except (FileNotFoundError, ValueError, TypeError, AttributeError):
        pass
    return path.parent.name or path.stem


def save_calibration_name(fit_path: str | Path, name: str) -> None:
    """Atomically write a name alongside the fit, never modifying the fit."""
    if not isinstance(name, str):
        raise TypeError('Calibration name must be text')
    path = Path(fit_path).parent / _METADATA_NAME
    clean_name = name.strip()
    if not clean_name:
        # Clearing a user name restores the date/folder fallback.
        path.unlink(missing_ok=True)
        return
    if len(clean_name) > 160:
        raise ValueError('Calibration name must be at most 160 characters')
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(
            'w', encoding='utf-8', prefix='.tilt_name_', suffix='.tmp',
            dir=path.parent, delete=False,
        ) as handle:
            temp = handle.name
            json.dump({'name': clean_name}, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
        os.replace(temp, path)
        temp = None
    finally:
        if temp is not None and os.path.exists(temp):
            os.unlink(temp)
