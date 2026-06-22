"""Shared provenance utilities for workflow metadata tracking.

Provides atomic file writing and JSON metadata sidecar generation for
acquisition workflows. Ensures provenance is captured even on partial runs.

Copyright (C) 2020-2026 ImSwitch developers
This file is part of ImSwitch.

ImSwitch is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

ImSwitch is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import imswitch

logger = logging.getLogger(__name__)


def write_acquisition_metadata(
    save_folder: Path,
    params: Any,
    completed: bool = True,
    filename: str = "acquisition_metadata.json",
) -> None:
    """Write workflow provenance metadata to a JSON sidecar file.

    Serializes the workflow parameters (typically a dataclass), adds timestamp
    and version info, and writes to a JSON file. Designed to be called from
    workflow cleanup/finally blocks so metadata is persisted even on partial runs.

    Args:
        save_folder: Directory where the acquisition data is saved.
        params: Workflow parameters object (typically a dataclass). Will be
            serialized using asdict() if it's a dataclass, otherwise must be
            a dict or JSON-serializable.
        completed: Whether the workflow completed successfully. Recorded in
            the metadata for partial-run tracking.
        filename: Name of the metadata file. Defaults to "acquisition_metadata.json".
    """
    save_folder = Path(save_folder)
    save_folder.mkdir(parents=True, exist_ok=True)
    metadata_path = save_folder / filename

    try:
        if is_dataclass(params):
            params_dict = asdict(params)
        elif isinstance(params, dict):
            params_dict = params
        else:
            params_dict = {"params": str(params)}

        params_dict = _make_json_serializable(params_dict)

        metadata = {
            "imswitch_version": imswitch.__version__,
            "acquisition_timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "completed": completed,
            "parameters": params_dict,
        }

        with open(metadata_path, "w") as f:
            json.dump(metadata, f, indent=2)

        logger.info("Wrote provenance metadata to %s", metadata_path)
    except Exception as exc:
        logger.error("Failed to write provenance metadata to %s: %s", metadata_path, exc)


def _make_json_serializable(obj: Any) -> Any:
    """Recursively convert common non-JSON types to serializable equivalents."""
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (tuple, set)):
        return list(obj)
    if isinstance(obj, dict):
        return {k: _make_json_serializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_make_json_serializable(item) for item in obj]
    return obj


def atomic_write(content: bytes, target_path: Path) -> None:
    """Write content to a file atomically using temp-then-rename pattern.

    Writes to a temporary file with .tmp suffix, then uses os.replace() to
    atomically move it to the target path. This prevents leaving partial files
    on crash or interruption.

    Args:
        content: Bytes to write to the file.
        target_path: Final destination path for the file.

    Raises:
        OSError: If write or rename fails.
    """
    target_path = Path(target_path)
    tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")

    try:
        with open(tmp_path, "wb") as f:
            f.write(content)
        os.replace(tmp_path, target_path)
        logger.debug("Atomic write completed: %s", target_path)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception as cleanup_exc:
                logger.debug("Failed to cleanup temp file %s: %s", tmp_path, cleanup_exc)
        raise
