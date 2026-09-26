"""RecordingController.recordScanSeries: a scan panel's "Acquire and save" (D2).

The entry point runs the Recording controller's own Scan-once transaction
(bind, geometry and layouts, arm the writer, wait, start the scan) by pressing
REC exactly as a user would, for an explicit scan source and detectors, and
answers synchronously. Several frames become a scan timelapse with no
interval in one file. It never rewrites the user's Recording panel: mode,
detector selection and lapse settings stay as set, and the controller's own
mode is restored when the recording ends.

These tests cover the entry point itself -- its refusals, what the REC press
sees, the reason it returns, and the cleanup. The end-to-end series (N
hardware iterations, N saved time partitions, one run end) needs the
SimplePointScan controller and is tested with it (plan P3).
"""
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.controller.controllers.RecordingController import (
    RecordingController,
    ScanSeriesRecordingResult,
)
from imswitch.imcontrol.model import RecMode, SaveFormat

from .test_scan_once_recording_sources import CONTROLLERS, _method_body


class _Widget:
    def __init__(self, *, saveFormat=SaveFormat.HDF5, recChecked=False):
        self.saveFormat = saveFormat
        self.recChecked = recChecked
        self.presses = []
        self.onPress = None
        self.detectorModeReads = 0

    def isRecButtonChecked(self):
        return self.recChecked

    def getSaveFormat(self):
        return self.saveFormat.value

    def getDetectorMode(self):
        self.detectorModeReads += 1
        return -3

    def getSelectedSpecificDetectors(self):
        return ['UserChoice']

    def setRecButtonChecked(self, checked):
        self.presses.append(checked)
        self.recChecked = checked
        if self.onPress is not None:
            self.onPress(checked)


class _Logger:
    def __init__(self):
        self.errors = []

    def error(self, message, *args, **kwargs):
        self.errors.append(message % args if args else message)

    def info(self, *args, **kwargs):
        pass


SOURCE = object()


def _controller(**widgetOptions):
    ctrl = RecordingController.__new__(RecordingController)
    ctrl.__dict__.update(
        _widget=_Widget(**widgetOptions),
        _RecordingController__logger=_Logger(),
        recording=False,
        recMode=RecMode.SpecFrames,
        _awaitingScanSourceArm=False,
        _seriesRequest=None,
        _lastRecordingFailure='',
        _recordingFailedCurrent=False,
    )
    return ctrl


def _nothingChanged(ctrl):
    assert ctrl._widget.presses == []
    assert ctrl.recMode == RecMode.SpecFrames
    assert ctrl._seriesRequest is None


# ---------------------------------------------------------------------------
# Refusals: nothing is touched
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('frames, reason', [
    (0, 'at least 1'),
    (-3, 'at least 1'),
    ('many', 'whole number'),
])
def test_a_frame_count_below_one_is_refused(frames, reason):
    ctrl = _controller()
    result = ctrl.recordScanSeries(SOURCE, ['APD'], frames)
    assert result.accepted is False
    assert reason in result.reason
    _nothingChanged(ctrl)


def test_no_detector_is_refused():
    ctrl = _controller()
    result = ctrl.recordScanSeries(SOURCE, [], 1)
    assert result == ScanSeriesRecordingResult(False, 'Choose at least one detector to save.')
    _nothingChanged(ctrl)


@pytest.mark.parametrize('state', [
    {'recording': True},
    {'_awaitingScanSourceArm': True},
    {'_seriesRequest': object()},
])
def test_a_second_request_while_one_runs_is_refused(state):
    ctrl = _controller()
    ctrl.__dict__.update(state)
    result = ctrl.recordScanSeries(SOURCE, ['APD'], 1)
    assert result.accepted is False
    assert 'already running' in result.reason
    assert ctrl._widget.presses == []


def test_a_checked_rec_button_is_a_running_recording():
    ctrl = _controller(recChecked=True)
    result = ctrl.recordScanSeries(SOURCE, ['APD'], 1)
    assert result.accepted is False
    assert ctrl._widget.presses == []


def test_several_frames_in_tiff_are_refused_but_one_frame_is_not():
    ctrl = _controller(saveFormat=SaveFormat.TIFF)
    result = ctrl.recordScanSeries(SOURCE, ['APD'], 5)
    assert result.accepted is False
    assert 'HDF5 or Zarr' in result.reason
    _nothingChanged(ctrl)

    ctrl._widget.onPress = lambda checked: ctrl.__dict__.update(recording=True)
    assert ctrl.recordScanSeries(SOURCE, ['APD'], 1).accepted is True


# ---------------------------------------------------------------------------
# What the REC press sees, and the answer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('frames, mode', [(1, RecMode.ScanOnce), (4, RecMode.ScanLapse)])
def test_the_rec_press_records_the_callers_scan_and_detectors(frames, mode):
    ctrl = _controller()
    seen = {}

    def press(checked):
        seen['mode'] = ctrl.recMode
        seen['detectors'] = ctrl.getDetectorNamesToCapture()
        seen['request'] = ctrl._seriesRequest
        ctrl.recording = True

    ctrl._widget.onPress = press
    result = ctrl.recordScanSeries(SOURCE, ['APD', 'PMT'], frames)

    assert result == ScanSeriesRecordingResult(True)
    assert ctrl._widget.presses == [True]
    assert seen['mode'] == mode
    assert seen['detectors'] == ['APD', 'PMT']
    assert seen['request'].source is SOURCE
    assert seen['request'].frames == frames
    # The widget's own detector selection was never consulted.
    assert ctrl._widget.detectorModeReads == 0


def test_the_users_mode_comes_back_when_the_recording_ends():
    ctrl = _controller()
    ctrl._widget.onPress = lambda checked: ctrl.__dict__.update(recording=True)
    ctrl.recordScanSeries(SOURCE, ['APD'], 3)
    assert ctrl.recMode == RecMode.ScanLapse

    # The terminal reset every recording ends in calls this.
    RecordingController._endScanSeriesRequest(ctrl)

    assert ctrl.recMode == RecMode.SpecFrames
    assert ctrl._seriesRequest is None
    assert ctrl.getDetectorNamesToCapture() == ['UserChoice']


def test_a_failed_start_answers_with_its_reason_and_cleans_up():
    """A writer that cannot open fails before the manager has a generation,
    which publishes no sigRecordingFailed: the reason has to come back here."""
    ctrl = _controller()
    ctrl.__dict__.update(
        _recordingFailureHandled=False,
        _recordingFailureAwaitingScanEnd=False,
        _recordingOperationActive=True,
        _recordingManagerGeneration=None,
        stopRequested=False,
        _master=SimpleNamespace(recordingManager=SimpleNamespace()),
    )
    ctrl.recordingCycleEnded = lambda: RecordingController._endScanSeriesRequest(ctrl)
    ctrl._notifyScanEndedIfPending = lambda: None
    ctrl._scanRunIsActive = lambda: False
    ctrl._abortOwnedScanSequence = lambda: None
    ctrl._widget.onPress = lambda checked: ctrl._handleRecordingFailure(
        'The writer could not open the file.', abortManager=False,
    )

    result = ctrl.recordScanSeries(SOURCE, ['APD'], 1)

    assert result == ScanSeriesRecordingResult(
        False, 'The writer could not open the file.'
    )
    assert ctrl.recMode == RecMode.SpecFrames
    assert ctrl._seriesRequest is None


def test_a_start_that_ends_silently_still_answers():
    ctrl = _controller()
    ctrl._widget.onPress = lambda checked: None   # e.g. shutting down

    result = ctrl.recordScanSeries(SOURCE, ['APD'], 1)

    assert result == ScanSeriesRecordingResult(False, 'The recording did not start.')
    assert ctrl.recMode == RecMode.SpecFrames
    assert ctrl._seriesRequest is None


def test_the_rec_flow_reads_the_request_before_the_widget():
    """Until the end-to-end series test in P3: the four places the REC flow
    would otherwise ask the widget or the chooser consult the request first."""
    source = (CONTROLLERS / 'RecordingController.py').read_text(encoding='utf-8')

    toggle = _method_body(source, 'toggleREC')
    assert toggle.index('self._recordingScanSource = seriesRequest.source') < (
        toggle.index('elif self._commChannel.hasScanWidget():')
    )
    assert 'seriesRequest.frames if seriesRequest is not None' in toggle
    assert 'True if seriesRequest is not None' in toggle

    nextLapse = _method_body(source, 'nextLapse')
    assert nextLapse.index('self._recordingScanSource = seriesRequest.source') < (
        nextLapse.index('self._scanSourceChoiceRequired()')
    )

    cycle = _method_body(source, 'recordingCycleEnded')
    assert "0 if self.__dict__.get('_seriesRequest') is not None" in cycle
    assert 'RecordingController._endScanSeriesRequest(self)' in cycle

    failure = _method_body(source, '_handleRecordingFailure')
    assert failure.index('self._lastRecordingFailure = str(message)') < (
        failure.index("self.__logger.error('Recording failed: %s', message)")
    )


def test_without_a_request_the_widget_decides_as_before():
    """Every recording the REC button starts itself: unchanged."""
    ctrl = _controller()
    assert ctrl.getDetectorNamesToCapture() == ['UserChoice']
    assert ctrl._widget.detectorModeReads == 1


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
