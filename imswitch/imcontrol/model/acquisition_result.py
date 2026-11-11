"""The structured result of a recording to memory.

``recordScanToMemory(..., structured=True)`` and
``recordFramesToMemory(..., structured=True)`` return an
:class:`AcquisitionResult` instead of bare arrays: the frames of each
detector together with what they mean -- axes and pixel size, which scan
position each frame was taken at, the settings the recording wrote down,
when it started, and whether it is complete. Everything is read from the
recording's own HDF5 file, before that file is dropped, so it describes
what was recorded rather than what the widgets show now.

Scan positions come from the recording's acquisition layout. A scan that
records its layout (the Advanced, point, MoNaLISA and TriggerScope scans)
says which loop position each frame belongs to: ``source`` is
``'recorded'``. A scan that does not (the Base scan) is reconstructed from
the scan plan the file records, with ImProcess's resolver, assuming the
scan ran its plan in order: ``source`` is ``'plan'``. Either way the
positions are the commanded ones; ImSwitch does not read the stages back
frame by frame.
"""

import io
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import h5py
import numpy as np

from imswitch.imcommon.model import acquisition_layout as layouts
from imswitch.imcommon.model.acquisition_metadata import flatten_acquisition_metadata

#: Layout provenance -> what ``FramePositions.source`` says.
_SOURCES = {
    'recorded': 'recorded', 'user-override': 'recorded',
    'legacy-adapter': 'plan',
}


@dataclass(frozen=True)
class FramePositions:
    """Which scan position each stored frame was taken at.

    ``indices[i, j]`` is frame ``i``'s position along ``loops[j]`` (outermost
    loop first), counted from the loop's first position; ``devices[j]`` is
    the positioner that loop moved (None when the file does not say),
    ``steps[j]`` and ``units[j]`` its step. ``source`` is ``'recorded'``
    (the scan wrote its layout as it ran), ``'plan'`` (reconstructed from
    the scan plan the file records) or ``'inferred'`` (from the array's
    shape alone). The positions are commanded, not read back. """

    source: str
    loops: Tuple[str, ...]
    devices: Tuple[Optional[str], ...]
    steps: Tuple[Optional[float], ...]
    units: Tuple[Optional[str], ...]
    directions: Tuple[int, ...]
    indices: np.ndarray

    def offsets(self) -> np.ndarray:
        """``(frames, loops)``: each frame's offset from the scan's first
        position along each loop, ``index * step`` in the loop's unit (in the
        direction the loop ran); NaN where the step is unknown."""
        steps = np.array([np.nan if s is None else float(s) for s in self.steps])
        directions = np.array([d or 1 for d in self.directions], dtype=float)
        return self.indices * steps * directions


@dataclass
class DetectorResult:
    """One detector's part of an :class:`AcquisitionResult`."""

    name: str
    data: np.ndarray
    axes: str
    pixelSizeUm: Tuple[float, ...]
    outcome: str
    framesExpected: Optional[int]
    framesRecorded: int
    settings: Dict[str, Any]
    payload: Optional[str]
    positions: Optional[FramePositions]
    layoutNotes: Tuple[str, ...]
    fileName: str
    _bytes: bytes = field(repr=False, default=b'')

    @property
    def complete(self) -> bool:
        return self.outcome == 'complete'


class AcquisitionResult:
    """The frames of a recording to memory, and what they mean.

    ``result['Camera']`` (or ``result.array`` for one detector) is the data,
    ``result.detector('Camera')`` everything about it: ``axes`` ('TYX'),
    ``pixelSizeUm``, ``positions`` (a :class:`FramePositions`), ``settings``
    (what the recording wrote down: the detector's parameters, the scan plan,
    stage positions, the recording mode), ``outcome`` and the frame counts.
    ``complete`` says whether every detector recorded every planned frame,
    ``startedAt`` when the recording started. ``show()`` displays every
    detector in ImControl's image view at its pixel size; ``save(folder)``
    writes the recording's HDF5 files, every attribute included. """

    def __init__(self, detectors: Sequence[DetectorResult], *, startedAt=None,
                 endedAt=None, show: Optional[Callable[[str, np.ndarray, Any], None]] = None):
        self._detectors = {d.name: d for d in detectors}
        self.startedAt: Optional[datetime] = startedAt
        self.endedAt: Optional[datetime] = endedAt
        self._show = show

    @property
    def detectors(self) -> List[str]:
        return list(self._detectors)

    def detector(self, name: str) -> DetectorResult:
        return self._detectors[name]

    def __getitem__(self, name: str) -> np.ndarray:
        return self._detectors[name].data

    def __iter__(self):
        return iter(self._detectors)

    def __len__(self):
        return len(self._detectors)

    @property
    def data(self) -> Dict[str, np.ndarray]:
        return {name: d.data for name, d in self._detectors.items()}

    @property
    def array(self) -> np.ndarray:
        """The data of the one detector; ValueError when there are several."""
        if len(self._detectors) != 1:
            raise ValueError(f'{len(self._detectors)} detectors: use result[name]')
        return next(iter(self._detectors.values())).data

    @property
    def complete(self) -> bool:
        return all(d.complete for d in self._detectors.values())

    def show(self, name: Optional[str] = None) -> None:
        """Shows every detector's data in ImControl's image view, one layer
        per detector (``name: detector`` with a name), at its pixel size."""
        if self._show is None:
            raise RuntimeError('This result cannot be shown: no image view was available')
        for detector in self._detectors.values():
            layer = f'{name}: {detector.name}' if name else detector.name
            scale = _displayScale(detector)
            self._show(layer, detector.data, scale)

    def save(self, folder: str, *, overwrite: bool = False) -> List[str]:
        """Writes the recording's HDF5 files into ``folder`` (created when
        missing) under the names the recording gave them, and returns their
        paths. Refuses to replace a file unless ``overwrite``."""
        os.makedirs(folder, exist_ok=True)
        written, seen = [], set()
        for detector in self._detectors.values():
            if detector.fileName in seen:
                continue          # several detectors in one file
            seen.add(detector.fileName)
            path = os.path.join(folder, detector.fileName)
            if os.path.exists(path) and not overwrite:
                raise FileExistsError(f'{path} exists; pass overwrite=True to replace it')
            with open(path, 'wb') as file:
                file.write(detector._bytes)
            written.append(path)
        return written

    def __repr__(self):
        parts = []
        for d in self._detectors.values():
            where = f', positions {d.positions.source}' if d.positions is not None else ''
            parts.append(f'{d.name}: {d.axes} {tuple(d.data.shape)} {d.data.dtype}, '
                         f'{d.outcome}{where}')
        return f'AcquisitionResult({"; ".join(parts)})'


def acquisitionResultFromFiles(files: Mapping[str, bytes], detectorNames: Sequence[str], *,
                               show=None, endedAt=None) -> AcquisitionResult:
    """Builds the result from a recording's HDF5 files, ``{file name: its
    bytes}``, for ``detectorNames``. Raises RuntimeError naming a detector
    the files hold no frames for."""
    found: Dict[str, DetectorResult] = {}
    startedAt = None
    for fileName, content in files.items():
        with h5py.File(io.BytesIO(content), 'r') as handle:
            for name in detectorNames:
                if name in found:
                    continue
                group = handle.get(name)
                array = group.get('data') if isinstance(group, h5py.Group) else None
                if not isinstance(array, h5py.Dataset):
                    continue
                attrs = _attrs(group, array)
                started = _datetime(attrs.get('acquisition:start_time'))
                startedAt = startedAt or started
                found[name] = _detectorResult(name, array, attrs, fileName, content)
    missing = [name for name in detectorNames if name not in found]
    if missing:
        raise RuntimeError(f'The recording holds no frames for {", ".join(missing)}: '
                           'did the scan trigger the detector?')
    return AcquisitionResult([found[name] for name in detectorNames], startedAt=startedAt,
                             endedAt=endedAt or datetime.now().astimezone(), show=show)


def _detectorResult(name, array, attrs, fileName, content) -> DetectorResult:
    data = array[()]
    pixel = tuple(float(v) for v in np.atleast_1d(attrs.get('element_size_um', ())))
    positions, payload, notes = _framePositions(attrs, data.shape, name)
    expected = attrs.get('recording:expected_frames')
    return DetectorResult(
        name=name, data=data, axes=str(attrs.get('axes') or ''),
        pixelSizeUm=pixel,
        outcome=str(attrs.get('recording:completion_outcome') or 'unknown'),
        framesExpected=int(expected) if expected is not None else None,
        framesRecorded=int(data.shape[0]) if data.ndim else 0,
        settings=_settings(attrs), payload=payload, positions=positions,
        layoutNotes=notes, fileName=fileName, _bytes=content,
    )


def _attrs(group, array) -> Dict[str, Any]:
    """The attributes as ImProcess reads them: the dataset's own, and the
    metadata tree flattened to ``Group:attribute`` keys."""
    attrs = dict(array.attrs)
    metadata = group.get('metadata')
    if isinstance(metadata, h5py.Group):
        def nested(node):
            mapping = dict(node.attrs)
            for key in node.keys():
                if isinstance(node[key], h5py.Group):
                    mapping[key] = nested(node[key])
            return mapping
        attrs.update(flatten_acquisition_metadata(nested(metadata)))
    return {key: (value.decode() if isinstance(value, bytes) else value)
            for key, value in attrs.items()}


def _settings(attrs) -> Dict[str, Any]:
    """What the recording wrote down, by metadata group: ``Detector``,
    ``Positioner``, ``Rec``, ``ScanStage``, ``ScanTTL``, ``acquisition``,
    ``recording`` (completion), ..."""
    grouped: Dict[str, Dict[str, Any]] = {}
    for key, value in attrs.items():
        if ':' not in key or key.startswith('AcquisitionLayout:'):
            continue
        group, rest = key.split(':', 1)
        grouped.setdefault(group, {})[rest] = _plain(value)
    return grouped


def _framePositions(attrs, shape, detector):
    """(FramePositions or None, payload kind, notes) for a detector."""
    layout, notes = _layout(attrs, shape, detector)
    if layout is None:
        return None, None, notes
    if layout.payload_kind != layouts.PAYLOAD_DETECTOR_FRAME_STREAM:
        # An assembled image: its scan positions are its pixel axes, whose
        # size pixelSizeUm gives; no frame stands for one position.
        return None, layout.payload_kind, notes
    loops = [loop for loop in layout.event_loops
             if layouts.LOOP_KIND_ADVANCES_POSITION.get(loop.kind, False)]
    frames = int(shape[0]) if shape else 0
    indices = np.zeros((frames, len(loops)), dtype=int)
    try:
        for i in range(min(frames, layouts.recorded_frame_count(layout))):
            coordinates = layouts.recorded_frame_coordinates(layout, i)
            indices[i] = [coordinates.get(loop.id, 0) for loop in loops]
    except (layouts.AcquisitionLayoutError, ValueError, IndexError) as error:
        return None, layout.payload_kind, notes + (f'positions unavailable: {error}',)
    positions = FramePositions(
        source=_SOURCES.get(layout.provenance, 'inferred'),
        loops=tuple(loop.id for loop in loops),
        devices=tuple(loop.device for loop in loops),
        steps=tuple(loop.step for loop in loops),
        units=tuple(loop.unit for loop in loops),
        directions=tuple(loop.direction or 1 for loop in loops),
        indices=indices,
    )
    return positions, layout.payload_kind, notes


def _layout(attrs, shape, detector):
    """The recording's layout -- recorded, or adapted from its plan by
    ImProcess's resolver -- and the resolver's notes."""
    try:
        # Lazy: ImControl does not depend on ImProcess at import time (see
        # imcontrol/_test/unit/test_layering_boundaries.py).
        from imswitch.improcess.model.acquisition_layout_resolver import (
            resolve_acquisition_layout,
        )
    except ImportError:
        encoded = attrs.get('AcquisitionLayout:json')
        if not encoded:
            return None, ('no recorded acquisition layout, and ImProcess is not '
                          'available to reconstruct one from the scan plan',)
        return layouts.decode_acquisition_layout(encoded), ()
    try:
        resolution = resolve_acquisition_layout(attrs, shape=tuple(shape), detector=detector)
    except Exception as error:  # a file the resolver cannot interpret
        return None, (f'no acquisition layout: {error}',)
    notes = tuple(issue.message for issue in resolution.issues)
    return resolution.layout, notes


def _displayScale(detector: DetectorResult):
    pixel = detector.pixelSizeUm
    if len(pixel) < 2:
        return None
    return tuple(pixel[-2:])


def _datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _plain(value):
    tolist = getattr(value, 'tolist', None)
    if callable(tolist) and type(value).__module__ == 'numpy':
        return tolist()
    return value


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
