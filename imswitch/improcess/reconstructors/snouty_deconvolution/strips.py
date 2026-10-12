"""Colour strips of a multicolour SNOUTY frame, from a multicolor alignment file.

A multicolour MS-RESOLFT frame holds the colour channels side by side along
the camera columns. The Multicolor Registration processor finds the strip
bounds on a deskewed bead volume and saves them, with the channel
transforms, in an alignment file; the deconvolution reads the same file so
every strip is deconvolved with its own emission wavelength and lands on the
columns the alignment expects, which lets Multicolor Apply split and align
the deconvolved volume exactly as it does a deskewed one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class CameraStrip:
    """One colour strip: camera columns ``[start, stop)`` and its wavelength."""

    index: int
    start: int
    stop: int
    wavelength_nm: float

    @property
    def width(self) -> int:
        return self.stop - self.start


def parse_wavelengths(text) -> list[float]:
    """Wavelengths in nm from ``"510, 600 670"``; an empty string gives ``[]``."""
    if text is None:
        return []
    parts = [part for part in re.split(r"[,;\s]+", str(text).strip()) if part]
    try:
        values = [float(part) for part in parts]
    except ValueError as exc:
        raise ValueError(f"Wavelengths must be numbers in nm, got {text!r}") from exc
    if any(value <= 0 for value in values):
        raise ValueError(f"Wavelengths must be positive, got {text!r}")
    return values


def camera_strips(
    alignment: dict,
    n_columns: int,
    wavelengths_nm: list[float],
    default_wavelength_nm: float,
) -> list[CameraStrip]:
    """Camera-column strips for the ROIs of ``alignment`` on a frame ``n_columns`` wide.

    The alignment stores its ROIs in output voxels of the bead volume it was
    made on; the bead frame had the same width as this one, so the bounds
    scale by ``n_columns / source_width`` whatever voxel size either used.
    ``wavelengths_nm`` names one wavelength per strip, or is empty to use
    ``default_wavelength_nm`` for all of them.
    """
    axis = str(alignment.get("split_axis", "X")).upper()
    if axis != "X":
        raise ValueError(
            f"The deconvolution can only split colour strips along X, the alignment splits along {axis}"
        )
    bounds = [int(value) for value in alignment["x_bounds"]]
    roi_width = int(alignment["roi_width"])
    source_width = int(alignment["source_shape"][-1])
    n_strips = len(bounds) - 1
    if n_strips < 1 or roi_width < 1 or source_width < 1:
        raise ValueError("The alignment file defines no usable colour strips")
    if wavelengths_nm and len(wavelengths_nm) != n_strips:
        raise ValueError(
            f"The alignment file has {n_strips} colour strips but {len(wavelengths_nm)} "
            f"wavelengths were given; give one per strip, or none to use Wavelength for all"
        )
    scale = n_columns / source_width
    strips = []
    for index in range(n_strips):
        start = int(round(bounds[index] * scale))
        stop = int(round((bounds[index] + roi_width) * scale))
        start = max(0, min(start, n_columns))
        stop = max(0, min(stop, n_columns))
        if stop - start < 1:
            raise ValueError(
                f"Colour strip {index} maps to no camera columns ({start}:{stop}) on a "
                f"{n_columns}-column frame"
            )
        wavelength = wavelengths_nm[index] if wavelengths_nm else float(default_wavelength_nm)
        strips.append(CameraStrip(index, start, stop, float(wavelength)))
    return strips


def describe_strips(strips: list[CameraStrip]) -> str:
    return ", ".join(
        f"strip {s.index}: columns {s.start}-{s.stop - 1} at {s.wavelength_nm:g} nm" for s in strips
    )


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
