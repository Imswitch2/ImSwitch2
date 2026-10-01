import pytest
"""Scan recordings expect the right frame count per detector kind (rig finding).

``expected_frames`` was ``recFrames * numCamTTL.get(name, 1)`` for every
detector, with ``recFrames`` set to the scan's position count. That is the
camera answer applied to everything:

- a trigger-driven **camera** emits one frame per scan position, so N is right;
- a scan-driven **point detector** (APD/PMT/TimeTagger) integrates the whole
  scan into ONE assembled image, so N is wrong by a factor of N and the
  recording waits for frames that are never produced.

Design reference: docs/design/plans/detector-acquisition-selection.md.
"""

from imswitch.imcontrol.model.managers.RecordingManager import (
    RecMode, RecordingWorker,
)


class _Detector:
    def __init__(self, isScanDriven):
        self.isScanDriven = isScanDriven


class _DetectorsManager:
    def __init__(self):
        self._detectors = {
            'Camera': _Detector(False),
            'APD': _Detector(True),
            'PMT': _Detector(True),
        }

    def __getitem__(self, name):
        return self._detectors[name]


class _RecordingManager:
    def __init__(self):
        self.detectorsManager = _DetectorsManager()
        self.completionTimes = {}

    def scanCompletionTime(self, generation):
        return self.completionTimes.get(generation)


def _worker(recMode):
    worker = RecordingWorker.__new__(RecordingWorker)
    worker._RecordingWorker__recordingManager = _RecordingManager()
    worker.recMode = recMode
    worker.recordingGeneration = 7
    return worker


def test_camera_expects_one_frame_per_scan_position():
    worker = _worker(RecMode.ScanOnce)

    assert worker._expectedFramesFor('Camera', 100, {'Camera': 1}) == 100


def test_point_detector_expects_a_single_assembled_frame():
    """The bug: APD/PMT produce one image for the whole scan, not one per
    position, so the recording used to wait for 99 frames that never come."""
    worker = _worker(RecMode.ScanOnce)

    assert worker._expectedFramesFor('APD', 100, {}) == 1
    assert worker._expectedFramesFor('PMT', 100, {}) == 1


def test_mixed_setup_gets_both_answers_in_one_recording():
    worker = _worker(RecMode.ScanOnce)

    expected = {
        name: worker._expectedFramesFor(name, 64, {'Camera': 1})
        for name in ('Camera', 'APD')
    }

    assert expected == {'Camera': 64, 'APD': 1}


def test_scan_lapse_keeps_one_frame_per_scan_for_point_detectors():
    """Each lapse session covers one scan; the timepoint loop supplies the
    repetition, so the per-session answer stays 1."""
    worker = _worker(RecMode.ScanLapse)

    assert worker._expectedFramesFor('APD', 100, {}) == 1
    assert worker._expectedFramesFor('Camera', 100, {'Camera': 1}) == 100


def test_multi_pulse_camera_ttl_still_multiplies():
    """A camera pulsed more than once per position keeps its multiplier."""
    worker = _worker(RecMode.ScanOnce)

    assert worker._expectedFramesFor('Camera', 10, {'Camera': 3}) == 30


def test_non_scan_modes_are_untouched():
    """SpecFrames means 'this many frames' for whatever detector is named."""
    worker = _worker(RecMode.SpecFrames)

    assert worker._expectedFramesFor('APD', 100, {}) == 100
    assert worker._expectedFramesFor('Camera', 100, {'Camera': 1}) == 100


def test_undeclared_detector_is_refused_in_scan_modes():
    """A detector the scan declares no pulse for cannot be tied to positions.

    "One frame per position" used to be the fallback here AND in the layout
    producer, so the gate compared a default with itself and a free-running
    camera was recorded as a certain, complete scan. In scan modes the answer
    is a refusal that names the detector; a plain frame-count recording has no
    scan and keeps one frame per count.
    """
    worker = _worker(RecMode.ScanOnce)

    with pytest.raises(ValueError, match="declares no TTL pulse per position"):
        worker._expectedFramesFor('Nonexistent', 42, {})

    worker = _worker(RecMode.SpecFrames)
    assert worker._expectedFramesFor('Nonexistent', 42, {}) == 42


def test_point_detector_watchdog_waits_for_the_full_scan():
    """A 20-second two-linestep scan must not trip a 10-second frame timer."""
    worker = _worker(RecMode.ScanOnce)

    assert worker._stallReferenceTimeFor('APD', 100.0) is None

    worker._RecordingWorker__recordingManager.completionTimes[7] = 120.0
    assert worker._stallReferenceTimeFor('APD', 100.0) == 120.0


def test_camera_watchdog_still_starts_when_recording_is_armed():
    worker = _worker(RecMode.ScanOnce)

    assert worker._stallReferenceTimeFor('Camera', 100.0) == 100.0


def test_non_scan_point_detector_watchdog_is_unchanged():
    worker = _worker(RecMode.SpecFrames)

    assert worker._stallReferenceTimeFor('APD', 100.0) == 100.0


# Copyright (C) 2020-2021 ImSwitch developers
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
