"""``recordScanToMemory()`` and ``recordFramesToMemory()``: a recording to
RAM whose frames come back as an array, with the Recording widget put back
as it was and nothing left listed in ImProcess unless asked for.

The widget stand-in plays the whole recording inside "REC on": the files are
announced, then the end (or the failure), as the writer and the controller do
on their threads. The controller is built without Qt, so its GUI-thread
hand-offs run synchronously (the non-Qt adapter path of the base class)."""
import time
from io import BytesIO
from types import SimpleNamespace

import h5py
import numpy as np
import pytest

from imswitch.imcontrol.controller.controllers.RecordingController import (
    RecordingController,
)
from imswitch.imcontrol.model import RecMode, SaveMode


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def disconnect(self, slot):
        self.slots.remove(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class _MemoryRecordings(dict):
    """The module channel's VFileCollection: deleting closes the bytes."""

    def __init__(self):
        super().__init__()
        self.removed = []

    def __delitem__(self, name):
        self.removed.append(name)
        self[name].close()
        super().__delitem__(name)


def _hdf5(frames):
    buffer = BytesIO()
    with h5py.File(buffer, 'w') as file:
        for name, data in frames.items():
            file.create_dataset(f'{name}/data', data=data)
    return buffer


class _Widget:
    def __init__(self):
        self.saveMode = SaveMode.Disk.value
        self.detectorMode = -1
        self.specific = []
        self.singleFile = False
        self.filename = 'mine'
        self.numFrames = 7
        self.recChecked = False
        self.modes = []
        self.onRec = lambda: None

    def getRecSaveMode(self):
        return self.saveMode

    def setRecSaveMode(self, value):
        self.saveMode = value

    def getDetectorMode(self):
        return self.detectorMode

    def setDetectorMode(self, value):
        self.detectorMode = value

    def getSelectedSpecificDetectors(self):
        return list(self.specific)

    def setSelectedSpecificDetectors(self, names):
        self.specific = list(names)

    def getMultiDetectorSingleFile(self):
        return self.singleFile

    def setMultiDetectorSingleFile(self, value):
        self.singleFile = value

    def getCustomFilename(self):
        return self.filename

    def getNumExpositions(self):
        return self.numFrames

    def setNumExpositions(self, value):
        self.numFrames = value

    def setCustomFilename(self, name):
        self.filename = name

    def clearCustomFilename(self):
        self.filename = None

    def setRecButtonChecked(self, checked):
        self.recChecked = checked
        if checked:
            self.onRec()

    def setEnabledParams(self, **_kwargs):
        pass

    def checkSpecFrames(self):
        self.modes.append(RecMode.SpecFrames)

    def checkScanOnce(self):
        self.modes.append(RecMode.ScanOnce)

    def state(self):
        return (self.saveMode, self.detectorMode, self.specific, self.singleFile,
                self.filename, self.numFrames)


def _controller():
    ctrl = RecordingController.__new__(RecordingController)
    ctrl._widget = _Widget()
    ctrl.recording = False
    ctrl.recMode = RecMode.SpecFrames
    ctrl._master = SimpleNamespace(
        recordingManager=SimpleNamespace(sigMemoryRecordingAvailable=_Signal()),
        detectorsManager=SimpleNamespace(getCurrentDetectorName=lambda: 'Camera'),
    )
    ctrl._commChannel = SimpleNamespace(
        sigRecordingEnded=_Signal(), sigRecordingFailed=_Signal(),
        hasScanWidget=lambda: True,
    )
    ctrl._moduleCommChannel = SimpleNamespace(memoryRecordings=_MemoryRecordings())
    return ctrl


def _plays(ctrl, frames, *, fail=None, end=True, recMode=RecMode.ScanOnce):
    """REC on: the recording runs and announces its files, then its end."""
    def onRec():
        assert ctrl._widget.saveMode == SaveMode.RAM.value
        assert ctrl.recMode == recMode
        assert ctrl._widget.filename is None, 'named by the time, like any RAM recording'
        ctrl.recording = True
        for name, data in frames.items():
            buffer = _hdf5({name: data})
            fileName = f'10h00m00s_rec_{name}.h5'
            ctrl._moduleCommChannel.memoryRecordings[fileName] = buffer
            ctrl._master.recordingManager.sigMemoryRecordingAvailable.emit(
                fileName, buffer, f'/recordings/{fileName}', False)
        if fail is not None:
            ctrl._commChannel.sigRecordingFailed.emit(fail)
        elif end:
            # The controller's own flag flips later, on the GUI thread.
            ctrl._commChannel.sigRecordingEnded.emit()
    ctrl._widget.onRec = onRec


def test_one_detector_gives_its_frames_and_leaves_no_trace():
    ctrl = _controller()
    frames = np.arange(2 * 3 * 4, dtype=np.uint16).reshape(2, 3, 4)
    _plays(ctrl, {'Camera': frames})
    before = ctrl._widget.state()

    result = ctrl.recordScanToMemory('Camera', timeout=5)

    np.testing.assert_array_equal(result, frames)
    assert ctrl._widget.state() == before
    assert ctrl._widget.modes == [RecMode.ScanOnce, RecMode.SpecFrames]
    assert ctrl.recMode == RecMode.SpecFrames
    assert ctrl._moduleCommChannel.memoryRecordings == {}
    assert ctrl._moduleCommChannel.memoryRecordings.removed == ['10h00m00s_rec_Camera.h5']
    assert not any(s.slots for s in (ctrl._commChannel.sigRecordingEnded,
                                     ctrl._commChannel.sigRecordingFailed,
                                     ctrl._master.recordingManager.sigMemoryRecordingAvailable))


def test_several_detectors_give_a_dict_and_keep_leaves_them_listed():
    ctrl = _controller()
    camera = np.ones((2, 3, 3), dtype=np.uint16)
    apd = np.zeros((2, 5, 5), dtype=np.float32)
    _plays(ctrl, {'Camera': camera, 'APD': apd})

    result = ctrl.recordScanToMemory(['Camera', 'APD'], timeout=5, keep=True)

    assert set(result) == {'Camera', 'APD'}
    np.testing.assert_array_equal(result['APD'], apd)
    assert sorted(ctrl._moduleCommChannel.memoryRecordings) == [
        '10h00m00s_rec_APD.h5', '10h00m00s_rec_Camera.h5']
    assert ctrl._widget.detectorMode == -1          # the widget's own choice again


def test_the_widgets_own_detector_choice_is_used_when_none_is_given():
    ctrl = _controller()
    ctrl._widget.detectorMode = -3
    ctrl._widget.specific = ['APD']
    _plays(ctrl, {'APD': np.zeros((1, 2, 2))})

    assert ctrl.recordScanToMemory(timeout=5).shape == (1, 2, 2)
    assert ctrl._widget.state()[1:3] == (-3, ['APD'])


def test_a_failed_recording_raises_and_restores_the_widget():
    ctrl = _controller()
    _plays(ctrl, {'Camera': np.zeros((1, 2, 2))}, fail='the DAQ went away')
    before = ctrl._widget.state()

    with pytest.raises(RuntimeError, match='the DAQ went away'):
        ctrl.recordScanToMemory('Camera', timeout=5)
    assert ctrl._widget.state() == before
    assert ctrl._moduleCommChannel.memoryRecordings == {}


def test_a_refused_start_raises_at_once_instead_of_waiting():
    ctrl = _controller()
    ctrl._widget.onRec = lambda: None               # toggleREC refused: recording stays False
    before = ctrl._widget.state()

    started = time.monotonic()
    with pytest.raises(RuntimeError, match='did not start'):
        ctrl.recordScanToMemory('Camera', timeout=60)
    assert time.monotonic() - started < 1
    assert ctrl._widget.state() == before


def test_a_detector_the_scan_never_triggered_is_named():
    ctrl = _controller()
    _plays(ctrl, {'Camera': np.zeros((1, 2, 2))})

    with pytest.raises(RuntimeError, match='no frames for APD'):
        ctrl.recordScanToMemory(['Camera', 'APD'], timeout=5)
    assert ctrl._moduleCommChannel.memoryRecordings == {}


def test_a_recording_that_never_ends_is_stopped_on_timeout():
    ctrl = _controller()
    _plays(ctrl, {'Camera': np.zeros((1, 2, 2))}, end=False)
    before = ctrl._widget.state()

    with pytest.raises(TimeoutError):
        ctrl.recordScanToMemory('Camera', timeout=0.2)
    assert ctrl._widget.recChecked is False
    assert ctrl._widget.state() == before


def test_it_is_refused_where_only_a_scan_widget_could_start_the_scan():
    ctrl = _controller()
    ctrl._commChannel.hasScanWidget = lambda: False
    with pytest.raises(RuntimeError, match='no Scan widget'):
        ctrl.recordScanToMemory('Camera', timeout=5)


def test_frames_to_memory_records_a_number_of_frames_and_restores_the_count():
    ctrl = _controller()
    frames = np.ones((3, 2, 2), dtype=np.uint16)
    _plays(ctrl, {'Camera': frames}, recMode=RecMode.SpecFrames)
    seen = {}
    ctrl._widget.onRec, play = (lambda: (seen.update(n=ctrl._widget.numFrames), play())), ctrl._widget.onRec
    before = ctrl._widget.state()

    result = ctrl.recordFramesToMemory('Camera', 3, timeout=5)

    np.testing.assert_array_equal(result, frames)
    assert seen['n'] == 3                        # the widget's frame count while recording
    assert ctrl._widget.state() == before        # 7 again afterwards
    assert ctrl._widget.modes == [RecMode.SpecFrames, RecMode.SpecFrames]


def test_frames_to_memory_refuses_less_than_one_frame():
    ctrl = _controller()
    with pytest.raises(ValueError):
        ctrl.recordFramesToMemory('Camera', 0, timeout=5)


def test_frames_to_memory_needs_no_scan_widget():
    ctrl = _controller()
    ctrl._commChannel.hasScanWidget = lambda: False
    _plays(ctrl, {'Camera': np.zeros((1, 2, 2))}, recMode=RecMode.SpecFrames)
    assert ctrl.recordFramesToMemory('Camera', 1, timeout=5).shape == (1, 2, 2)


def test_they_run_on_the_callers_thread():
    for name in ('recordScanToMemory', 'recordFramesToMemory'):
        export = getattr(RecordingController, name)
        assert export._APIExport is True
        assert export._APIRunOnUIThread is False


def test_structured_returns_a_result_read_before_the_recording_is_dropped():
    ctrl = _controller()
    frames = np.arange(12, dtype=np.uint16).reshape(3, 2, 2)
    _plays(ctrl, {'Camera': frames})

    result = ctrl.recordScanToMemory('Camera', timeout=5, structured=True)

    np.testing.assert_array_equal(result['Camera'], frames)
    assert result.detectors == ['Camera']
    assert result.detector('Camera').fileName == '10h00m00s_rec_Camera.h5'
    assert ctrl._moduleCommChannel.memoryRecordings == {}     # dropped all the same


@pytest.mark.parametrize('method, args, recMode, ends', [
    ('recordScanToMemory', ('Camera',), RecMode.ScanOnce, True),
    ('recordFramesToMemory', ('Camera', 1), RecMode.SpecFrames, True),
    ('recordScanToMemory', ('Camera',), RecMode.ScanOnce, False),     # the timeout path
], ids=['scan', 'frames', 'timeout'])
def test_every_widget_access_happens_on_the_controller_thread(method, args, recMode, ends):
    """The API exports run on the script thread (they block until the end);
    the static audit allows them only because every widget access goes
    through _onControllerThread. A widget touched outside it fails here."""
    ctrl = _controller()
    _plays(ctrl, {'Camera': np.zeros((1, 2, 2))}, end=ends, recMode=recMode)
    inside = []
    hop = RecordingController._onControllerThread

    def onControllerThread(func, timeout):
        inside.append(True)
        try:
            return hop(ctrl, func, timeout)
        finally:
            inside.pop()

    ctrl._onControllerThread = onControllerThread
    widget = ctrl._widget

    class _Guarded:
        def __getattr__(self, name):
            assert inside, f'widget.{name} touched outside _onControllerThread'
            return getattr(widget, name)

    ctrl._widget = _Guarded()
    if ends:
        getattr(ctrl, method)(*args, timeout=5)
    else:
        with pytest.raises(TimeoutError):
            getattr(ctrl, method)(*args, timeout=0.2)
