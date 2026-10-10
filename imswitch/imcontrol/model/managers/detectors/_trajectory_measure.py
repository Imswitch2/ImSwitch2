"""Point detectors measuring a trajectory (docs/design/plans/
scan-engines-and-expert-scan.md §6, M4).

A trajectory scan tells the detectors, under ``scanInfoDict['trajectory']``,
where every measurement window starts in the stream, how long it is, and
which grid bin its centre lands in. A point detector then reads the stream
window by window instead of line by line, turns each window into one
measurement -- counts for a photon counter, the mean voltage for an analog
detector -- and bins it (``scan_program.trajectory.GridAccumulator``):

- the image is the count rate (counts/s) or the time-weighted mean (V), NaN
  where no window landed;
- the coverage, each bin's integration time in seconds, is kept beside it
  and recorded as an auxiliary array with its unit;
- when the program asks for it (``record_measurements``, M6), every window's
  measurement too, in acquisition order: one row per recorded frame, kept
  with the scan that took it, with each window's integration time and
  commanded centre beside them.

The detector's worker keeps everything else of a scan: its task, its
lifecycle, its end-of-scan acknowledgement.
"""

from __future__ import annotations

import json
from typing import Callable, Dict, Optional

import numpy as np

from imswitch.imcontrol.model.scan_program.scan_info import TRAJECTORY_KEY
from imswitch.imcontrol.model.scan_program.trajectory import Grid, GridAccumulator


def is_trajectory(scanInfoDict) -> bool:
    return bool((scanInfoDict or {}).get(TRAJECTORY_KEY))


class TrajectoryMeasurement:
    """The windows of one trajectory, in a detector's own samples."""

    def __init__(self, scanInfoDict, detector_per_scan_sample: int, measurement: str):
        info = scanInfoDict[TRAJECTORY_KEY]
        grid = info['grid']
        self.grid = Grid(tuple(grid['devices']), tuple(grid['first_um']), float(grid['pixel_um']),
                         tuple(int(n) for n in grid['shape']))
        self.measurement = measurement
        self.accumulator = GridAccumulator(self.grid, measurement)
        per = int(detector_per_scan_sample)
        self.start = np.asarray(info['window_start'], dtype=np.int64) * per
        self.length = np.asarray(info['window_ticks'], dtype=np.int64) * per
        self.seconds = np.asarray(info['window_ticks'], dtype=float) / float(info['rate_hz'])
        self.bin = np.asarray(info['bin'], dtype=np.int64)
        self.total = int(scanInfoDict['scan_samples_total']) * per
        self.provenance = dict(info[f'provenance_{measurement}'])
        self.record = bool(info.get('record_measurements', False))
        self.centres_um = np.asarray(info['centres_um'], dtype=float) if self.record else None
        self.devices = list(info.get('devices') or self.grid.devices)
        # Filled in place as windows are read; a window not read stays NaN.
        self.values = np.full(len(self.start), np.nan) if self.record else None
        # Read a grid row's worth of windows at a time: the live image then
        # grows about a row per read, whatever the path.
        self.per_read = max(1, self.grid.shape[0])

    def reads(self):
        """Slices of consecutive windows, one per read."""
        for first in range(0, len(self.start), self.per_read):
            yield slice(first, first + self.per_read)

    def add(self, windows: slice, values: np.ndarray):
        self.accumulator.add(self.bin[windows], values, self.seconds[windows])
        if self.values is not None:
            self.values[windows] = values


def run_trajectory(worker, window_values: Callable, publish: Callable):
    """The read loop for a worker's ``_runAcquisition``: throw the approach,
    read the path window by window, throw the way back. ``window_values(data,
    offsets, lengths)`` makes one measurement per window from the samples
    read; ``publish(measurement)`` hands the image on after every read."""
    measurement = worker._trajectory
    worker.throwdata(int(measurement.start[0]) - int(worker._samples_read))
    for windows in measurement.reads():
        if not worker.scanning:
            return
        lengths = measurement.length[windows]
        data = worker.readdata(int(lengths.sum()))
        offsets = np.concatenate(([0], np.cumsum(lengths)[:-1]))
        measurement.add(windows, window_values(data, offsets, lengths))
        publish(measurement)
    rest = measurement.total - int(worker._samples_read)
    if worker.scanning and rest > 0:
        worker.throwdata(rest)
    worker.d3Step.emit(worker.scanGeneration)


class TrajectoryDetectorMixin:
    """What a point-detector manager adds for trajectories: the float image,
    the coverage, and what recording takes with the image."""

    _trajectoryMode = False
    _trajectoryState: Optional[Dict] = None

    def _trackTrajectory(self, generation, measurement):
        """The measurement scan ``generation``'s worker fills: what its raw
        frame is finished from, whatever the queued live images have reached."""
        self.__dict__.setdefault('_trajectoryByGeneration', {})[generation] = measurement

    def _onTrajectoryImage(self, image, coverage, unit, provenance, generation,
                           measurement=None):
        if generation != getattr(self, '_scanGeneration', generation):
            return
        # A live image queued before its scan finished, delivered after:
        # the finished image is already in place.
        if generation <= self.__dict__.get('_trajectoryFinishedThrough', -1):
            return
        self._image[...] = np.asarray(image, dtype=self._image.dtype).reshape(self._image.shape)
        self._trajectoryState = {'coverage': np.asarray(coverage), 'unit': unit,
                                 'provenance': provenance, 'generation': generation,
                                 'measurement': measurement}
        self._publishTrajectoryImage()

    def _finishTrajectory(self, generation):
        """Scan ``generation`` is complete and its worker done: its image,
        coverage and measurements from the measurement itself, before its raw
        frame is published. The live images arrive queued, and a scan can
        complete (a teardown on another thread) before the last of them.
        Its measurements are kept apart from the state the next scan resets,
        so a frame drained after the next scan began still carries its own."""
        measurement = self.__dict__.get('_trajectoryByGeneration', {}).pop(generation, None)
        if measurement is None:
            return
        self.__dict__['_trajectoryFinishedThrough'] = generation
        accumulator = measurement.accumulator
        self._image[...] = np.asarray(accumulator.image(), dtype=self._image.dtype).reshape(
            self._image.shape)
        self._trajectoryState = {'coverage': accumulator.coverage(), 'unit': accumulator.unit,
                                 'provenance': measurement.provenance,
                                 'generation': generation, 'measurement': measurement}
        if measurement.values is not None:
            self.__dict__['_trajectoryMeasurements'] = (generation, measurement.values.copy())
        self._publishTrajectoryImage()

    def _rawFrameAuxiliary(self, generation):
        """What travels with this generation's raw frame to a recording:
        ``[{'measurements': values}]``, or ``None``."""
        kept = self.__dict__.get('_trajectoryMeasurements')
        if kept is None or kept[0] != generation:
            return None
        return [{'measurements': kept[1]}]

    def _publishTrajectoryImage(self):
        """Hand the image to the live view (throttled), as a line would."""

    def recordingAuxiliary(self):
        """``{'attrs': {...}, 'arrays': {name: (array, attrs)}}`` recorded
        with this detector's image, or ``None``: the unit and the reduction
        of a gridded trajectory, and its coverage in seconds."""
        state = self._trajectoryState
        if not self._trajectoryMode or state is None:
            return None
        provenance = json.dumps(state['provenance'])
        out = {
            'attrs': {'measurement:unit': state['unit'], 'measurement:reduction': provenance},
            'arrays': {
                'coverage': (np.asarray(state['coverage'], dtype=np.float64), {
                    'unit': 's', 'auxiliary': True,
                    'meaning': 'integration time per bin, summed over the windows in it',
                    'of': 'data', 'reduction': provenance}),
            },
        }
        measurement = state.get('measurement')
        if measurement is not None and measurement.record:
            windows = len(measurement.seconds)
            order = 'acquisition order: window k of every recorded frame'
            out['arrays']['measurement_seconds'] = (measurement.seconds.copy(), {
                'unit': 's', 'auxiliary': True, 'order': order,
                'meaning': 'each window\'s realized integration time'})
            out['arrays']['measurement_centre_um'] = (measurement.centres_um.copy(), {
                'unit': 'um', 'auxiliary': True, 'order': order,
                'axes': json.dumps(measurement.devices),
                'meaning': 'each window\'s commanded position at its centre'})
            out['streams'] = {
                'measurements': ((windows,), 'float64', {
                    'unit': _RAW_UNITS[measurement.measurement], 'auxiliary': True,
                    'order': order, 'frames': 'one row per recorded frame',
                    'meaning': _RAW_MEANINGS[measurement.measurement],
                    'missing': 'NaN: a frame whose measurements did not arrive'}),
            }
        return out


#: What one window's measurement is, per measurement type (plan §6).
_RAW_UNITS = {'counts': 'counts', 'mean': 'V'}
_RAW_MEANINGS = {'counts': 'the counts in each window',
                 'mean': 'each window\'s mean voltage, the offset removed'}


__all__ = ['TrajectoryDetectorMixin', 'TrajectoryMeasurement', 'is_trajectory',
           'run_trajectory']


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
