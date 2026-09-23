"""Shared image output for workflows that save their own acquisitions.

Several workflows poll a detector, hold the finished stack in memory, and save
it themselves rather than streaming through ``RecordingManager``. That is the
right shape for them -- there is nothing to stream by the time they save -- but
it left every one of them writing a bare array with no record of what the
frames mean, and the first workflow to describe itself had to assemble OME
metadata inline.

This module owns that assembly once. A caller supplies the acquisition layout,
which is domain knowledge only the workflow has, and gets an atomic write of a
file that describes itself to the ImProcess resolver.

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

import io
import logging
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import tifffile as tf

from imswitch.imcommon.model.acquisition_layout import (
    ACQUISITION_LAYOUT_SCHEMA,
    AcquisitionLayout,
    encode_acquisition_layout,
)

from .provenance import atomic_write

logger = logging.getLogger(__name__)


def acquisition_description(
    data: np.ndarray,
    *,
    layout: AcquisitionLayout | None,
    name: str,
    annotations: Mapping[str, Any] | None = None,
) -> str | None:
    """OME-XML describing one saved acquisition, or ``None``.

    Returns ``None`` rather than raising when the description cannot be built:
    a workflow that has finished measuring must still save its data, and a
    file without metadata is recoverable while a lost measurement is not.
    """
    # Imported lazily so this module stays importable in contexts that do not
    # pull in the recording stack.
    from imswitch.imcontrol.model.managers.recording_metadata import (
        build_ome_image_meta,
        build_ome_xml,
    )

    array = np.asarray(data)
    entries: dict[str, Any] = dict(annotations or {})
    try:
        if layout is not None:
            entries["AcquisitionLayout:schema"] = ACQUISITION_LAYOUT_SCHEMA
            entries["AcquisitionLayout:json"] = encode_acquisition_layout(layout)
        if not entries:
            return None
        meta = build_ome_image_meta(
            name=name,
            mode="frames",
            n_frames=int(array.shape[0]) if array.ndim >= 3 else 1,
            dtype=array.dtype,
            annotations=entries,
        )
        return build_ome_xml(meta, array.shape)
    except Exception as error:
        logger.warning("Could not describe acquisition %r: %s", name, error)
        return None


def save_acquisition_tiff(
    path: str | Path,
    data: np.ndarray,
    *,
    layout: AcquisitionLayout | None = None,
    name: str | None = None,
    annotations: Mapping[str, Any] | None = None,
) -> Path:
    """Write a TIFF that describes its own acquisition, atomically.

    The file is serialized in memory and moved into place in one step, so an
    interrupted write cannot leave a half-written stack that later reads as a
    short acquisition. ``photometric="minisblack"`` keeps a three-frame
    grayscale stack from being stored as an RGB image.
    """
    target = Path(path)
    array = np.asarray(data)
    description = acquisition_description(
        array,
        layout=layout,
        name=name or target.stem,
        annotations=annotations,
    )

    buffer = io.BytesIO()
    if description is None:
        tf.imwrite(buffer, array, photometric="minisblack")
    else:
        tf.imwrite(buffer, array, description=description, photometric="minisblack")
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(buffer.getvalue(), target)
    return target


__all__ = ["acquisition_description", "save_acquisition_tiff"]
