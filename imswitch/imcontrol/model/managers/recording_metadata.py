"""Shared, format-agnostic image metadata model for recordings.

Phase 1 of the OME standardization plan (docs/recording_ome_standardization_plan.md).
A single :class:`OmeImageMeta` is built once per detector per recording and then
serialized into each container's native standard:

- OME-TIFF  -> :meth:`OmeImageMeta.tiff_metadata`  (for ``tifffile.imwrite(ome=True, metadata=...)``)
- OME-NGFF  -> :meth:`OmeImageMeta.ngff_ome_metadata` (the ``ome`` group attribute, spec 0.5)
- HDF5      -> Fiji ``element_size_um`` + embedded OME-XML (serializer added in a later phase)

This module deliberately has NO dependency on RecordingManager (the caller maps its
``RecMode`` to one of the normalized mode strings below), so it stays trivially unit
testable.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

# Normalized recording modes (decoupled from RecordingManager.RecMode).
MODE_SNAP = 'snap'
MODE_TIMELAPSE = 'timelapse'   # camera time series: SpecFrames / SpecTime / UntilStop
MODE_SCAN = 'scan'             # ScanOnce
MODE_SCAN_LAPSE = 'scan_lapse' # ScanLapse

# Map RecMode.name -> normalized mode. Kept as plain strings to avoid importing
# RecordingManager (which would be a circular import).
_RECMODE_TO_MODE = {
    'SpecFrames': MODE_TIMELAPSE,
    'SpecTime': MODE_TIMELAPSE,
    'CameraLapse': MODE_TIMELAPSE,
    'UntilStop': MODE_TIMELAPSE,
    'ScanOnce': MODE_SCAN,
    'ScanLapse': MODE_SCAN_LAPSE,
}

from imswitch.imcommon.model.ome_metadata import _SPACE_UNIT, _TIME_UNIT  # noqa: E402



def normalize_mode(rec_mode_name: Optional[str], *, is_snap: bool = False) -> str:
    """Map a ``RecMode`` member name (or snap) to a normalized mode string."""
    if is_snap:
        return MODE_SNAP
    return _RECMODE_TO_MODE.get(rec_mode_name or '', MODE_TIMELAPSE)


def axes_for_recording(mode: str, n_frames: int,
                       scan_dims: Optional[Sequence[int]] = None) -> List[str]:
    """Return the ordered OME axis names for a recorded ``(frames, Y, X)`` stack.

    The recorded data is always frame-stacked as ``(N, Y, X)``; this decides what the
    leading ``N`` axis *means* (and whether it collapses to a plain ``YX`` image):

    - ``snap``       -> ``YX`` (or ``TYX`` for a multi-plane snapshot)
    - ``timelapse``  -> ``TYX``                  (frames are a real time series)
    - ``scan``       -> ``ZYX`` if the scan has a Z/slow axis (Nz > 1),
                        else ``TYX`` (sequence of exposures), else ``YX`` (single frame)
    - ``scan_lapse`` -> same per-scan axes as ``scan``. ImSwitch stores each
                        lapse as its own scan dataset/group today; the timepoint
                        belongs in recording attrs / series metadata, not a
                        leading array axis.

    ``scan_dims`` is ``(Nx, Ny, Nz)`` from ``getDimsScan()`` (0 / 1 for inactive axes).
    """
    yx = ['y', 'x']
    has_z = bool(scan_dims) and len(scan_dims) >= 3 and int(scan_dims[2]) > 1
    multi = int(n_frames) > 1

    if mode == MODE_SNAP:
        lead: List[str] = ['t'] if multi else []
    elif mode == MODE_TIMELAPSE:
        lead = ['t'] if multi else []
    elif mode == MODE_SCAN:
        lead = ['z'] if has_z else (['t'] if multi else [])
    elif mode == MODE_SCAN_LAPSE:
        lead = ['z'] if has_z else (['t'] if multi else [])
    else:
        lead = ['t'] if multi else []

    return lead + yx


# The format-agnostic core lives in imcommon: ImProcess writes images too, and
# a reconstruction whose calibration disagreed with the recording it came from
# would be a file that cannot be compared with its own source. Re-exported here
# so every existing importer of this module keeps working unchanged.
from imswitch.imcommon.model.ome_metadata import (  # noqa: F401
    ANNOTATION_NAMESPACE,
    NOTE_KEY,
    OmeAxis,
    OmeImageMeta,
    build_ome_xml,
)


def build_ome_image_meta(
    name: str,
    mode: str,
    n_frames: int,
    *,
    pixel_size_yx_um: Sequence[float] = (1.0, 1.0),
    scan_dims: Optional[Sequence[int]] = None,
    z_step_um: float = 1.0,
    t_interval_s: float = 1.0,
    dtype: Any = None,
    channels: Optional[List[Dict[str, Any]]] = None,
    acquisition_time: Optional[str] = None,
    annotations: Optional[Dict[str, Any]] = None,
    stage_position_um: Optional[Sequence[float]] = None,
) -> OmeImageMeta:
    """Build an :class:`OmeImageMeta` from recording context.

    ``pixel_size_yx_um`` is ``(y, x)`` (matches ``DetectorManager.pixelSizeUm[1:]``);
    ``z_step_um`` is the scan Z step, ``t_interval_s`` the frame interval.
    """
    axes_names = axes_for_recording(mode, n_frames, scan_dims)
    axes: List[OmeAxis] = []
    scale: List[float] = []
    py, px = float(pixel_size_yx_um[0]), float(pixel_size_yx_um[1])
    for nm in axes_names:
        if nm == 'x':
            axes.append(OmeAxis('x', 'space', _SPACE_UNIT)); scale.append(px)
        elif nm == 'y':
            axes.append(OmeAxis('y', 'space', _SPACE_UNIT)); scale.append(py)
        elif nm == 'z':
            axes.append(OmeAxis('z', 'space', _SPACE_UNIT)); scale.append(float(z_step_um))
        elif nm == 't':
            axes.append(OmeAxis('t', 'time', _TIME_UNIT)); scale.append(float(t_interval_s))

    return OmeImageMeta(
        name=name,
        axes=axes,
        scale=scale,
        dtype=np.dtype(dtype) if dtype is not None else None,
        channels=channels or [{'name': name}],
        acquisition_time=acquisition_time or datetime.now(timezone.utc).isoformat(),
        annotations=annotations or {},
        stage_position_um=(
            tuple(float(v) for v in stage_position_um)
            if stage_position_um is not None else None
        ),
    )
