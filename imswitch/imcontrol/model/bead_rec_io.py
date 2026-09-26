"""Saving and reloading BeadRec reconstructions as OME-TIFF.

BeadRec used to write a bare TIFF: no pixel size, no provenance, nothing a
reader could scale by -- and a bead fit without a pixel size is a number
without a unit. A reconstruction's pixel is one scan step (or the finer of
the two steps, when the image was rescaled to square pixels), so the step is
written as the OME ``PhysicalSizeY``/``PhysicalSizeX`` and read back from
there, and what BeadRec knew about the image rides along as an OME
``MapAnnotation`` through the same serializer the recording storers use.

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

import math
import xml.etree.ElementTree as ET
from typing import Any, Mapping, Optional, Sequence, Tuple

import numpy as np
import tifffile

from imswitch.imcommon.model.ome_metadata import _SPACE_UNIT, OmeAxis, OmeImageMeta

#: Units a PhysicalSize may be read back in; OME's default length unit is µm.
_MICRON_UNITS = frozenset({'µm', 'μm', 'um', 'micron', 'micrometer'})


def valid_pixel_size(pixel_size_yx_um: Optional[Sequence[float]]) -> Optional[Tuple[float, float]]:
    """``(y, x)`` in µm when both are positive and finite, else None."""
    if pixel_size_yx_um is None or len(pixel_size_yx_um) != 2:
        return None
    try:
        y, x = (float(value) for value in pixel_size_yx_um)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(y) and math.isfinite(x) and y > 0 and x > 0):
        return None
    return y, x


def write_reconstruction_tiff(
    path: str,
    image: np.ndarray,
    pixel_size_yx_um: Optional[Sequence[float]] = None,
    annotations: Optional[Mapping[str, Any]] = None,
) -> None:
    """Write one 2-D reconstruction as OME-TIFF.

    The pixel size is omitted, not invented, when it is unknown -- an image
    loaded from a file that carried none, say.
    """
    image = np.asarray(image)
    if image.ndim != 2:
        raise ValueError(f'A BeadRec reconstruction is 2-D, got shape {image.shape}')
    pixel = valid_pixel_size(pixel_size_yx_um)
    meta = OmeImageMeta(
        'BeadRec',
        [OmeAxis('y', 'space', _SPACE_UNIT), OmeAxis('x', 'space', _SPACE_UNIT)],
        list(pixel) if pixel is not None else [1.0, 1.0],
        dtype=image.dtype,
        annotations={
            key: value for key, value in dict(annotations or {}).items()
            if value is not None
        },
    )
    metadata = meta.tiff_metadata(image.shape)
    if pixel is None:
        for key in ('PhysicalSizeX', 'PhysicalSizeXUnit',
                    'PhysicalSizeY', 'PhysicalSizeYUnit'):
            metadata.pop(key, None)
    tifffile.imwrite(path, image, ome=True, metadata=metadata)


def read_pixel_size_um(path: str) -> Optional[Tuple[float, float]]:
    """``(y, x)`` in µm from a TIFF's OME-XML, or None when it carries none."""
    try:
        with tifffile.TiffFile(path) as tiff:
            xml = tiff.ome_metadata
    except Exception:
        return None
    if not xml:
        return None
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for element in root.iter():
        if element.tag.rsplit('}', 1)[-1] != 'Pixels':
            continue
        sizes = []
        for axis in ('Y', 'X'):
            value = element.get(f'PhysicalSize{axis}')
            unit = element.get(f'PhysicalSize{axis}Unit', 'µm')
            if value is None or unit not in _MICRON_UNITS:
                return None
            sizes.append(value)
        return valid_pixel_size(sizes)
    return None


def oriented_pixel_size(
    pixel_size_yx_um: Optional[Sequence[float]], rotation: int
) -> Optional[Tuple[float, float]]:
    """The pixel size after a display rotation: a quarter turn swaps Y and X."""
    pixel = valid_pixel_size(pixel_size_yx_um)
    if pixel is None:
        return None
    return (pixel[1], pixel[0]) if int(rotation) % 180 == 90 else pixel


__all__ = [
    'oriented_pixel_size', 'read_pixel_size_um', 'valid_pixel_size',
    'write_reconstruction_tiff',
]
