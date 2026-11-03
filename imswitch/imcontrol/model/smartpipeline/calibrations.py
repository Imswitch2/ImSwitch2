"""Calibration files: the transforms a pipeline may name, one JSON per file
under ``<ImSwitchConfig>/calibrations/``.

A file is a transform's ``to_dict`` (``type`` picks the class: ``identity``,
``scale-flip``, ``polynomial3``). :func:`load_calibrations` reads a folder
into a registry; :func:`save_calibration` writes one; :func:`et_polynomial`
turns an EtSTED calibration -- the 20 coefficients its procedure writes to
``transform_pipelines/<timestamp>.csv`` and the setup's swap/invert flags --
into a ``polynomial3`` transform, so the existing calibrations drive a
pipeline without being redone. Qt-free.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import numpy as np

from imswitch.imcommon.model.smartpipeline import FrameIdentity, Polynomial3, TransformRegistry, transform_from_dict

CALIBRATIONS_DIRNAME = "calibrations"


class CalibrationError(ValueError):
    """A calibration file cannot be read or written."""


def calibrations_folder(root=None) -> Path:
    """The folder of calibration files (``<ImSwitchConfig>/calibrations``)."""
    if root is None:
        from imswitch.imcommon.model import dirtools

        root = dirtools.UserFileDirs.Root
    return Path(root) / CALIBRATIONS_DIRNAME


def load_calibrations(folder=None) -> TransformRegistry:
    """Every ``*.json`` in ``folder`` as a transform, by id; a file that
    cannot be read raises :class:`CalibrationError` naming it."""
    folder = Path(folder) if folder is not None else calibrations_folder()
    registry = TransformRegistry()
    if not folder.is_dir():
        return registry
    for name in sorted(os.listdir(folder)):
        if not name.lower().endswith(".json"):
            continue
        path = folder / name
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            registry.register(transform_from_dict(payload))
        except (OSError, ValueError) as exc:
            raise CalibrationError(f"calibration {name}: {exc}") from exc
    return registry


def _file_name(transform_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]+", "_", str(transform_id)).strip("_") or "calibration"


def save_calibration(transform, folder=None, *, overwrite: bool = True) -> Path:
    """Write ``transform`` as ``<folder>/<id>.json``; returns the path."""
    folder = Path(folder) if folder is not None else calibrations_folder()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{_file_name(transform.id)}.json"
    if path.exists() and not overwrite:
        raise CalibrationError(f"{path.name} exists")
    path.write_text(json.dumps(transform.to_dict(), indent=2) + "\n", encoding="utf-8")
    return path


def read_et_coefficients(path) -> tuple[float, ...]:
    """The 20 coefficients of an EtSTED transform file (one per line, as the
    calibration writes them, or whitespace-separated)."""
    try:
        values = np.loadtxt(Path(path), dtype=float).reshape(-1)
    except OSError as exc:
        raise CalibrationError(f"transform coefficient file not found: {path}") from exc
    except ValueError as exc:
        raise CalibrationError(f"transform coefficient file is not numeric: {path}") from exc
    return tuple(float(v) for v in values)


def et_polynomial(transform_id: str, coefficients, *, detector: str, target_frame: str, swap_xy: bool = False,
                  invert_x: bool = False, invert_y: bool = False, calibrated_on: FrameIdentity | dict | None = None,
                  columns: str = "yx") -> Polynomial3:
    """An EtSTED calibration as a pipeline transform from ``detector:<detector>``
    (pixels) into ``target_frame`` (the scan coordinates). ``coefficients``
    is the vector or a path to the file; the flags are the setup's
    ``etSTED`` ones."""
    if isinstance(coefficients, (str, os.PathLike)):
        coefficients = read_et_coefficients(coefficients)
    if isinstance(calibrated_on, FrameIdentity):
        calibrated_on = calibrated_on.to_dict()
    return Polynomial3(
        transform_id, f"detector:{detector}", target_frame, tuple(coefficients), swap_xy=bool(swap_xy),
        invert_x=bool(invert_x), invert_y=bool(invert_y), columns=columns, calibrated_on=calibrated_on,
    )


def identity_problems(transform, identity: FrameIdentity | None) -> list[str]:
    """Why ``identity`` (the grid the pipeline is armed on) is not the one
    ``transform`` was calibrated on; empty when it is, or when the transform
    does not say."""
    recorded = getattr(transform, "calibrated_on", None)
    if not recorded or identity is None:
        return []
    try:
        expected = FrameIdentity.from_dict(dict(recorded))
    except (TypeError, ValueError):
        return [f"transform {transform.id!r}: its calibrated_on is not a frame identity"]
    reasons = expected.differs_from(identity)
    if not reasons:
        return []
    return [f"transform {transform.id!r} was calibrated on another grid: " + "; ".join(reasons)]


__all__ = [
    "CALIBRATIONS_DIRNAME",
    "CalibrationError",
    "calibrations_folder",
    "et_polynomial",
    "identity_problems",
    "load_calibrations",
    "read_et_coefficients",
    "save_calibration",
]


# Copyright (C) 2020-2026 ImSwitch developers
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
