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

_SPACE_UNIT = 'µm'   # OME UnitsLength for micrometer
_TIME_UNIT = 's'     # OME UnitsTime for second


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


@dataclass(frozen=True)
class OmeAxis:
    """A single OME axis: ``name`` in {x,y,z,t,c}, ``type`` in {space,time,channel}."""
    name: str
    type: str
    unit: Optional[str] = None


@dataclass
class OmeImageMeta:
    """Format-agnostic description of one recorded image (one detector)."""
    name: str
    axes: List[OmeAxis]
    scale: List[float]                 # physical size per axis, same order as ``axes``
    dtype: Optional[np.dtype] = None
    channels: List[Dict[str, Any]] = field(default_factory=list)
    acquisition_time: Optional[str] = None
    annotations: Dict[str, Any] = field(default_factory=dict)
    #: Stage position of this image as ``(x, y, z)`` in micrometres, or None.
    #: OME's standard place for "where on the sample was this taken", written as
    #: ``Plane/@PositionX|Y|Z`` in OME-XML and as a ``translation`` coordinate
    #: transformation in OME-NGFF. This is what makes a set of tiles a
    #: reconstructable mosaic rather than unrelated images.
    stage_position_um: Optional[Tuple[float, float, float]] = None

    def __post_init__(self):
        if len(self.axes) != len(self.scale):
            raise ValueError(
                f'axes ({len(self.axes)}) and scale ({len(self.scale)}) length mismatch')

    @property
    def axes_string(self) -> str:
        """Upper-case axis string, e.g. ``'TYX'`` (the form tifffile expects)."""
        return ''.join(a.name.upper() for a in self.axes)

    def padded_to(self, ndim: int) -> 'OmeImageMeta':
        """Return a copy whose axes/scale match ``ndim``.

        Missing leading dimensions follow OME's ``T, C, Z, Y, X`` order.
        This matters for scan-driven detector frames shaped ``(T, C, Y, X)``,
        where ``C`` represents separately retained line-step planes.
        """
        axes = list(self.axes)
        scale = list(self.scale)
        missing = max(0, ndim - len(axes))
        existing = {axis.name for axis in axes}
        candidates = [
            OmeAxis('t', 'time', _TIME_UNIT),
            OmeAxis('c', 'channel'),
            OmeAxis('z', 'space', _SPACE_UNIT),
        ]
        additions = [
            axis for axis in candidates if axis.name not in existing
        ][:missing]
        while len(additions) < missing:
            additions.insert(0, OmeAxis('t', 'time', _TIME_UNIT))
        axes = additions + axes
        scale = [1.0] * len(additions) + scale
        canonical = {'t': 0, 'c': 1, 'z': 2, 'y': 3, 'x': 4}
        if len({axis.name for axis in axes}) == len(axes):
            ordered = sorted(
                zip(axes, scale),
                key=lambda item: canonical.get(item[0].name, 99),
            )
            axes = [item[0] for item in ordered]
            scale = [item[1] for item in ordered]
        if len(axes) > ndim:
            axes = axes[len(axes) - ndim:]
            scale = scale[len(scale) - ndim:]
        return OmeImageMeta(self.name, axes, scale, self.dtype, self.channels,
                            self.acquisition_time, self.annotations,
                            self.stage_position_um)

    # ---- serializers -------------------------------------------------------

    def tiff_metadata(self, shape: Optional[Sequence[int]] = None) -> Dict[str, Any]:
        """``metadata=`` dict for ``tifffile.imwrite(..., ome=True, metadata=...)``.

        Pass the shape of the array being written whenever stage positions are
        attached: OME records one position per plane, and tifffile indexes that
        list per plane, so a 3D tile written with a single-entry list raises
        ``IndexError: list index out of range for attribute 'PositionX'``.
        """
        md: Dict[str, Any] = {'axes': self.axes_string}
        for axis, size in zip(self.axes, self.scale):
            if axis.name == 'x':
                md['PhysicalSizeX'] = float(size); md['PhysicalSizeXUnit'] = axis.unit
            elif axis.name == 'y':
                md['PhysicalSizeY'] = float(size); md['PhysicalSizeYUnit'] = axis.unit
            elif axis.name == 'z':
                md['PhysicalSizeZ'] = float(size); md['PhysicalSizeZUnit'] = axis.unit
            elif axis.name == 't':
                md['TimeIncrement'] = float(size); md['TimeIncrementUnit'] = axis.unit
        if self.channels:
            md['Channel'] = {'Name': [c.get('name', self.name) for c in self.channels]}
        md.update(self.plane_position_metadata(shape))
        return md

    def n_planes(self, shape: Optional[Sequence[int]] = None) -> int:
        """Number of OME planes: the product of every non-YX dimension.

        ``shape`` is the array actually being written and is authoritative.
        The axis list says which dimensions exist but not how many samples
        each holds -- the scale list carries physical sizes, not counts -- so
        the plane count genuinely cannot be derived from the metadata alone.
        This used to return a hardcoded ``1`` regardless, which is right for a
        single-plane snap and wrong for every Z stack: a 5-plane tile got one
        stage-position entry where tifffile indexes five.

        Without a shape the only safe answer is still one plane.
        """
        if shape is None:
            return 1
        planes = 1
        for size in tuple(int(s) for s in shape)[:-2]:
            planes *= max(1, size)
        return max(1, planes)

    def plane_position_metadata(
        self, shape: Optional[Sequence[int]] = None
    ) -> Dict[str, Any]:
        """``Plane`` position entries for tifffile's OME metadata dict.

        OME records stage position per *plane*, so the values are lists, one
        entry per plane of ``shape``. Empty when no position is known, which
        keeps the metadata identical to before for every non-tiled recording.
        """
        if self.stage_position_um is None:
            return {}
        x, y, z = self.stage_position_um
        n = self.n_planes(shape)
        plane: Dict[str, Any] = {
            'PositionX': [float(x)] * n,
            'PositionXUnit': [_SPACE_UNIT] * n,
            'PositionY': [float(y)] * n,
            'PositionYUnit': [_SPACE_UNIT] * n,
        }
        if z is not None:
            plane['PositionZ'] = [float(z)] * n
            plane['PositionZUnit'] = [_SPACE_UNIT] * n
        return {'Plane': plane}

    def ngff_ome_metadata(self, path: str = '0', ndim: Optional[int] = None) -> Dict[str, Any]:
        """The ``ome`` group attribute for OME-NGFF 0.5 (``zarr.json`` ``attributes.ome``).

        Single-resolution image: one dataset at ``path`` (default ``'0'``).
        ``ndim`` pads/truncates the axes to match the stored array's rank -- some
        stores always carry a leading frame axis (e.g. a 2D snap stored as
        ``(1, Y, X)``), so a leading ``t`` axis (scale 1) is prepended as needed.
        """
        src = self.padded_to(ndim) if ndim is not None else self
        axes_objs = list(src.axes)
        scale = [float(s) for s in src.scale]

        axes = []
        for a in axes_objs:
            entry: Dict[str, Any] = {'name': a.name, 'type': a.type}
            if a.unit:
                entry['unit'] = 'micrometer' if a.unit == _SPACE_UNIT else (
                    'second' if a.unit == _TIME_UNIT else a.unit)
            axes.append(entry)
        transforms: List[Dict[str, Any]] = [{'type': 'scale', 'scale': scale}]
        if src.stage_position_um is not None:
            # OME-NGFF places a tile in the mosaic with a translation transform,
            # in the same physical units as the scale. Axis order follows the
            # axes list, so a leading t/c axis translates by zero.
            x, y, z = src.stage_position_um
            by_name = {'x': float(x), 'y': float(y), 'z': float(z or 0.0)}
            transforms.append({
                'type': 'translation',
                'translation': [by_name.get(a.name, 0.0) for a in axes_objs],
            })

        return {
            'version': '0.5',
            'multiscales': [{
                'name': self.name,
                'axes': axes,
                'datasets': [{
                    'path': path,
                    'coordinateTransformations': transforms,
                }],
            }],
        }

    def element_size_um(self) -> List[float]:
        """Fiji/ilastik ``element_size_um`` triplet ``[z, y, x]`` (HDF5 interop)."""
        by_name = {a.name: s for a, s in zip(self.axes, self.scale)}
        return [float(by_name.get('z', 1.0)), float(by_name.get('y', 1.0)),
                float(by_name.get('x', 1.0))]


def build_ome_xml(meta: 'OmeImageMeta', shape: Sequence[int]) -> str:
    """ASCII-safe OME-XML string for a data array of ``shape`` described by ``meta``.

    Used where OME metadata can't be written natively at stream time and must be
    injected/embedded after the fact: OME-TIFF finalize (``tifffile.tiffcomment``)
    and HDF5. ``len(shape)`` must equal ``len(meta.axes)`` -- the leading axes
    collapse into the OME plane count. Non-ASCII units (``µ``) are emitted as XML
    character references so the string is valid in a 7-bit-ASCII TIFF tag.
    """
    import tifffile  # lazy: only needed when actually serializing

    shp = tuple(int(s) for s in shape)
    ny, nx = shp[-2], shp[-1]
    planecount = 1
    for s in shp[:-2]:
        planecount *= int(s)
    stored = (planecount, 1, 1, ny, nx, 1)  # tifffile StoredShape 6-tuple

    md: Dict[str, Any] = {}
    for axis, size in zip(meta.axes, meta.scale):
        if axis.name == 'x':
            md['PhysicalSizeX'] = float(size); md['PhysicalSizeXUnit'] = axis.unit
        elif axis.name == 'y':
            md['PhysicalSizeY'] = float(size); md['PhysicalSizeYUnit'] = axis.unit
        elif axis.name == 'z':
            md['PhysicalSizeZ'] = float(size); md['PhysicalSizeZUnit'] = axis.unit
        elif axis.name == 't':
            md['TimeIncrement'] = float(size); md['TimeIncrementUnit'] = axis.unit

    md.update(meta.plane_position_metadata(shp))

    dtype = str(np.dtype(meta.dtype)) if meta.dtype is not None else 'uint16'
    xml = tifffile.OmeXml()
    xml.addimage(dtype, shp, stored, axes=meta.axes_string, **md)
    return xml.tostring().encode('ascii', 'xmlcharrefreplace').decode('ascii')


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
