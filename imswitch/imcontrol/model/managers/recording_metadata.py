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

It also holds the *recording plan* helpers -- :class:`RecordingPlan`,
:func:`expected_frames_for` and :func:`build_recording_attrs`. They decide how
many frames a detector contributes to a session and which ``acquisition:*``,
``recording:*`` and ``AcquisitionLayout:*`` attributes describe it. The
``RecordingWorker`` calls them for every file it writes, and the in-process
live-reconstruction path calls the same functions for the stream it never
writes, so a reconstructor resolves the same geometry from a file, a RAM
recording and a live stream. Keep them pure: no manager, no thread, no I/O.
"""

from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

import imswitch
from imswitch.imcommon.model.acquisition_layout import (
    ACQUISITION_LAYOUT_SCHEMA,
    PAYLOAD_ASSEMBLED_IMAGE,
    PAYLOAD_DETECTOR_FRAME_STREAM,
    AcquisitionLayout,
    decode_acquisition_layout,
    encode_acquisition_layout,
)

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


# ---------------------------------------------------------------------------
# Recording plan: frame accounting and the recording attribute block
# ---------------------------------------------------------------------------

#: ``RecMode`` member names whose sessions cover exactly one scan.
SCAN_MODE_NAMES = frozenset({'ScanOnce', 'ScanLapse'})

#: ``recording:source_format`` value for a stream that is reconstructed in
#: process and never written; the file formats use ``SaveFormat.name``.
SOURCE_FORMAT_MEMORY = 'memory'


@dataclass(frozen=True)
class RecordingPlan:
    """What one recording session intends, independent of how it is stored.

    A plan is the recorder's inputs with the manager stripped away: the
    ``RecordingWorker`` builds one from the arguments ``startRecording``
    copied onto it, and the live-reconstruction path builds one from the same
    scan-controller accessors the recording controller reads. Both then get
    identical answers from :func:`expected_frames_for` and
    :func:`build_recording_attrs`.

    ``rec_mode`` is a ``RecMode`` member name (``'SpecFrames'``,
    ``'ScanOnce'``, ...). ``acquisition_layouts`` maps detector name to an
    :class:`AcquisitionLayout` or its encoded JSON.
    """

    rec_mode: str
    rec_frames: Optional[int] = None
    num_cam_ttl: Mapping[str, int] = field(default_factory=dict)
    acquisition_layouts: Mapping[str, Any] = field(default_factory=dict)
    source_format: str = 'HDF5'
    num_timepoints: int = 1
    lapse_index: int = 0
    single_lapse_file: bool = False
    lapse_interval_s: Optional[float] = None
    planned_start_time: Optional[str] = None

    @property
    def is_scan_mode(self) -> bool:
        """Whether one session covers exactly one hardware scan."""
        return self.rec_mode in SCAN_MODE_NAMES

    def layout_for(self, detector_name: str) -> Optional[AcquisitionLayout]:
        """The detector's acquisition layout, decoded, or ``None``."""
        value = (self.acquisition_layouts or {}).get(detector_name)
        if value is None:
            return None
        if isinstance(value, AcquisitionLayout):
            return value
        if isinstance(value, (str, bytes, bytearray)):
            return decode_acquisition_layout(value)
        raise TypeError(
            f"Acquisition layout for {detector_name!r} must be an "
            "AcquisitionLayout or encoded JSON"
        )


def planned_frames_from_layout(layout: AcquisitionLayout) -> Optional[int]:
    """How many frames a layout says its detector delivers, or ``None``.

    An assembled image is one frame however many positions the scan has; a
    frame stream delivers one frame per recorded event (the spans when the
    layout records some, else the product of its loops). Any other payload
    kind is not the layout's to count.
    """
    if layout.payload_kind == PAYLOAD_ASSEMBLED_IMAGE:
        return 1
    if layout.payload_kind == PAYLOAD_DETECTOR_FRAME_STREAM:
        if layout.recorded_event_spans is not None:
            return sum(
                span.count * span.repeats
                for span in layout.recorded_event_spans
            )
        return math.prod(loop.count for loop in layout.event_loops)
    return None


def expected_frames_for(plan: RecordingPlan, detector_name: str, *,
                        is_scan_driven: bool) -> int:
    """How many frames this detector produces for the session.

    The two detector families answer this completely differently, and
    treating them alike is what made scan recordings hang or truncate:

    - A **free-running/trigger-driven camera** emits one frame per scan
      position (per camera TTL pulse), so it yields
      ``rec_frames * num_cam_ttl``.
    - A **scan-driven** detector (APD, PMT, TimeTagger) integrates the whole
      scan into a single assembled image and emits exactly ONE frame per
      scan, whatever the position count. Expecting one frame per position
      means waiting for frames that are never produced.

    A recorded acquisition layout, when the plan carries one, answers before
    either rule. In the lapse modes each session covers one scan, so the
    scan-driven answer stays 1 there too; the per-timepoint loop supplies the
    repetition.
    """
    layout = plan.layout_for(detector_name)
    if layout is not None:
        counted = planned_frames_from_layout(layout)
        if counted is not None:
            return int(counted)
    if plan.is_scan_mode and is_scan_driven:
        return 1
    if plan.rec_frames is None:
        raise ValueError(
            f'recFrames must be specified in {plan.rec_mode} mode to derive '
            f'the number of frames to record for detector {detector_name!r}'
        )
    num_cam_ttl = plan.num_cam_ttl or {}
    if plan.is_scan_mode:
        declared = num_cam_ttl.get(detector_name)
        if declared is None:
            raise ValueError(
                f'The scan declares no TTL pulse per position for detector '
                f'{detector_name!r}, so the number of frames to record '
                f'cannot be derived. Gate it in the scan, deselect it, or '
                f'record it in a non-scan mode.'
            )
        return int(plan.rec_frames) * int(declared)
    return int(plan.rec_frames) * int(num_cam_ttl.get(detector_name, 1))


def exposure_time_ms_for(detector):
    """The detector's exposure time in ms for the attrs, or ``None``.

    Different detectors expose it differently; the common spellings are
    tried in turn and an unanswerable detector simply records none. The
    recorder and the live reconstruction both use this, so a recording and
    the live view of the same acquisition carry the same value.
    """
    try:
        if hasattr(detector, 'getExposureTime'):
            return detector.getExposureTime()
        if hasattr(detector, 'exposure'):
            return detector.exposure
    except Exception:
        return None
    return None


def build_recording_attrs(
    plan: RecordingPlan,
    detector_name: str,
    detector_attrs: Optional[Mapping[str, Any]] = None,
    *,
    expected_frames: Optional[int] = None,
    exposure_time_ms: Any = None,
    start_time: Optional[str] = None,
    software_version: Optional[str] = None,
) -> Dict[str, Any]:
    """The flat attribute dict a recording carries for one detector.

    Starts from the shared attributes the controller collected
    (``detector_attrs``, ``'Category:key'`` keys) and adds the
    ``acquisition:*`` block, the ``recording:*`` block and, when the plan
    carries a layout for the detector, ``AcquisitionLayout:schema`` and
    ``AcquisitionLayout:json``. ``start_time`` is shared by every detector of
    one session, so the caller passes it in rather than each call reading
    the clock.
    """
    attrs: Dict[str, Any] = dict(detector_attrs) if detector_attrs else {}

    attrs['acquisition:software_version'] = software_version or imswitch.__version__
    attrs['acquisition:start_time'] = (
        start_time or datetime.now(timezone.utc).isoformat()
    )
    if exposure_time_ms is not None:
        attrs['acquisition:exposure_time_ms'] = str(exposure_time_ms)

    attrs['recording:detector_name'] = detector_name
    attrs['recording:source_format'] = plan.source_format
    if expected_frames is not None:
        frame_count = int(expected_frames)
        attrs['recording:expected_frames'] = frame_count
        attrs['recording:planned_frames'] = frame_count
        # No generic multi-stack boundary exists yet. For single-stack
        # recording modes, the expected frame count is the stack size.
        attrs['recording:frames_per_stack'] = frame_count
    attrs.setdefault('recording:planned_partitions', 1)

    layout = plan.layout_for(detector_name)
    if layout is not None:
        attrs['AcquisitionLayout:schema'] = ACQUISITION_LAYOUT_SCHEMA
        attrs['AcquisitionLayout:json'] = encode_acquisition_layout(layout)

    attrs['recording:num_timepoints'] = int(plan.num_timepoints or 1)
    attrs['recording:lapse_index'] = int(plan.lapse_index or 0)
    attrs['recording:single_lapse_file'] = bool(plan.single_lapse_file)
    if plan.lapse_interval_s is not None:
        attrs['recording:lapse_interval_s'] = float(plan.lapse_interval_s)
    if plan.planned_start_time:
        attrs['recording:planned_start_time'] = str(plan.planned_start_time)

    return attrs
