"""Scan geometry that travels with a point-scan frame to the viewer.

A point-detector frame (APD, PMT, Time Tagger) is shown in napari with a pixel
size but no position: world coordinates are micrometres from the first pixel
of whatever frame is on screen. Mapping something drawn over it back to the
scanners needs the geometry of *that* frame -- not of the scan the panel is
set to now, and not of a scan that was built but has not yet delivered pixels
(docs/simple-point-scan-plan.md, D1).

So the geometry rides on the frame itself. A scan controller that wants this
puts a :class:`FrameGeometry` (as a plain dict) into the ``scanInfoDict`` the
detectors receive, under :data:`FRAME_GEOMETRY_KEY`. A scan-driven manager
then publishes that generation's frames as :class:`ScanFrame` -- an
``ndarray`` view carrying the geometry -- so every existing subscriber still
receives an array, and the image controller files the geometry with the
layer in the same call that sets its pixels.

Scans without the key (every scan the existing panels run) are published
exactly as before.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping, Optional, Sequence, Tuple

import numpy as np

from .scan_parameters import pixels_for_length_step

#: ``scanInfoDict`` key under which a controller hands detectors the geometry.
FRAME_GEOMETRY_KEY = 'frame_geometry'

#: napari ``layer.metadata`` key for the geometry of the pixels on screen.
SCAN_GEOMETRY_METADATA_KEY = 'imswitch.scanGeometry'


@dataclass(frozen=True)
class AxisGeometry:
    """One scanned axis: which device moved, and where its pixels are.

    ``first_um`` is the centre of the first pixel along the axis, in the
    device's own micrometres; pixel ``j`` is centred at
    ``first_um + j * step_um``.
    """

    device: str
    step_um: float
    count: int
    first_um: float

    def position_um(self, index: float) -> float:
        """Centre of (fractional) pixel ``index`` along this axis."""
        return self.first_um + float(index) * self.step_um


@dataclass(frozen=True)
class FrameGeometry:
    """Where the pixels of one scan generation are.

    ``axes`` are the scanned axes in scan order, fast axis first -- the order
    of ``scanInfoDict['img_dims']``. A published frame's last ``len(axes)``
    array dimensions are these axes reversed (rows are the second axis,
    columns the first).
    """

    axes: Tuple[AxisGeometry, ...]
    run: Optional[int] = None
    iteration: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            'axes': [asdict(axis) for axis in self.axes],
            'run': self.run,
            'iteration': self.iteration,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> 'FrameGeometry':
        return cls(
            axes=tuple(
                AxisGeometry(
                    device=str(axis['device']),
                    step_um=float(axis['step_um']),
                    count=int(axis['count']),
                    first_um=float(axis['first_um']),
                )
                for axis in data.get('axes', ())
            ),
            run=data.get('run'),
            iteration=data.get('iteration'),
        )


def frame_geometry_from_scan_info(scanInfoDict: Mapping[str, Any]) -> Optional[FrameGeometry]:
    """The geometry a controller put into ``scanInfoDict``, or ``None``."""
    data = (scanInfoDict or {}).get(FRAME_GEOMETRY_KEY)
    if not data:
        return None
    if isinstance(data, FrameGeometry):
        return data
    return FrameGeometry.from_dict(data)


def frame_geometry_from_scan(
    analogParameterDict: Mapping[str, Sequence[Any]],
    scanInfoDict: Mapping[str, Any],
    *,
    run: Optional[int] = None,
    iteration: Optional[int] = None,
) -> FrameGeometry:
    """Pixel positions of a scan built by the galvo designer.

    Follows the designer's own conventions, for any length -- a length need
    not be a whole number of steps:

    * a smoothly swept fast axis sweeps ``centre +- length / 2`` at one step
      per dwell, so its first pixel is centred at ``centre - length/2 +
      step/2``;
    * a stepped axis visits ``axis_pixel_positions``, centred on the axis
      centre: ``centre - (N - 1)/2 * step``; a virtual ('mock') stepped axis
      is re-zeroed by the designer, so its first pixel is at 0.

    ``scanInfoDict['axis_names']`` and ``['smooth_axes']`` say which devices
    are active, in scan order; ``img_dims`` gives their pixel counts.
    """
    targets = list(analogParameterDict['target_device'])
    names = list(scanInfoDict['axis_names'])
    dims = list(scanInfoDict['img_dims'])
    smooth = list(scanInfoDict.get('smooth_axes', [False] * len(names)))
    axes = []
    for position, name in enumerate(names):
        index = targets.index(name)
        length = float(analogParameterDict['axis_length'][index])
        step = float(analogParameterDict['axis_step_size'][index])
        centre = float(analogParameterDict['axis_centerpos'][index])
        count = int(dims[position])
        if count != pixels_for_length_step(length, step):
            raise ValueError(
                f'{name}: scan info has {count} pixels, but length {length} '
                f'at step {step} gives {pixels_for_length_step(length, step)}'
            )
        swept = position == 0 and bool(smooth[position])
        if swept:
            first = centre - length / 2.0 + step / 2.0
        elif 'mock' in name.lower():
            first = 0.0
        else:
            first = centre - (count - 1) / 2.0 * step
        axes.append(AxisGeometry(device=name, step_um=step, count=count,
                                 first_um=first))
    return FrameGeometry(axes=tuple(axes), run=run, iteration=iteration)


class ScanFrame(np.ndarray):
    """An ``ndarray`` view whose pixels belong to one :class:`FrameGeometry`.

    Only the exact published object carries the geometry. Anything derived
    from it -- a slice, arithmetic, a display rotation -- is a plain array as
    far as geometry goes (``geometry is None``): a rotated view would otherwise
    claim the unrotated geometry.
    """

    geometry: Optional[FrameGeometry]

    def __array_finalize__(self, obj):
        self.geometry = None


def with_frame_geometry(array: np.ndarray, geometry: Optional[FrameGeometry]) -> np.ndarray:
    """``array`` as a :class:`ScanFrame` carrying ``geometry``.

    With no geometry the array is returned unchanged, so a scan without one
    publishes exactly what it did before. The pixels are not copied.
    """
    if geometry is None:
        return array
    frame = np.asarray(array).view(ScanFrame)
    frame.geometry = geometry
    return frame


def frame_geometry_of(obj: Any) -> Optional[FrameGeometry]:
    """The geometry an object was published with, or ``None``."""
    if isinstance(obj, ScanFrame):
        return obj.geometry
    return None


@dataclass(frozen=True)
class DisplayedScanGeometry:
    """What a live layer needs to map its pixels back to the scanners.

    ``raw_shape`` is the published frame's shape before the detector's display
    transform, ``display_transform`` that transform.
    """

    geometry: FrameGeometry
    display_transform: Any
    raw_shape: Tuple[int, ...]


__all__ = [
    'AxisGeometry',
    'DisplayedScanGeometry',
    'FRAME_GEOMETRY_KEY',
    'FrameGeometry',
    'SCAN_GEOMETRY_METADATA_KEY',
    'ScanFrame',
    'frame_geometry_from_scan',
    'frame_geometry_from_scan_info',
    'frame_geometry_of',
    'with_frame_geometry',
]
