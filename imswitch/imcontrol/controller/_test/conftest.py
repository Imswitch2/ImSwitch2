"""A drivable RecordingController for the series tests.

The real ``toggleREC`` / ``nextCameraLapse`` / ``recordingCycleEnded`` run
against lightweight doubles: a widget whose REC button is wired to
``toggleREC``, a recording manager that records what it was asked to start,
and a Timer that never fires on its own (a test fires it explicitly), so no Qt
event loop is needed.
"""
from types import SimpleNamespace

import pytest

import importlib
from imswitch.imcontrol.controller.controllers.RecordingController import (
    RecordingController,
)
from imswitch.imcontrol.model import RecMode, SaveFormat

module = importlib.import_module(
    'imswitch.imcontrol.controller.controllers.RecordingController'
)


class FakeTimer:
    created = []

    def __init__(self, singleShot=False):
        self.active = False
        self.interval = None
        self.callbacks = []
        self.timeout = SimpleNamespace(connect=self.callbacks.append)
        FakeTimer.created.append(self)

    def start(self, interval):
        self.interval = interval
        self.active = True

    def stop(self):
        self.active = False

    def isActive(self):
        return self.active

    def fire(self):
        self.active = False
        for callback in list(self.callbacks):
            callback()


class FakeManager:
    def __init__(self):
        self.record = False
        self.recordingGeneration = 0
        self.started = []
        self.aborted = 0

    def startRecording(self, **kwargs):
        self.started.append(kwargs)
        self.record = True
        self.recordingGeneration += 1
        return self.recordingGeneration

    def shutdownComplete(self):
        return True

    def abortRecording(self, **kwargs):
        self.aborted += 1
        self.record = False

    def finishPoint(self):
        self.record = False


class FakeWidget:
    def __init__(self, tmp_path, saveFormat):
        self.controller = None
        self.checked = False
        self.tmp_path = tmp_path
        self.saveFormat = saveFormat
        # Reads of the widget's own series settings: a caller's series must
        # make none of them.
        self.settingReads = []
        self.progress = []

    def isRecButtonChecked(self):
        return self.checked

    def setRecButtonChecked(self, checked):
        self.checked = checked
        self.controller.toggleREC(checked)

    def getRecFolder(self):
        return str(self.tmp_path)

    def getCustomFilename(self):
        return 'series'

    def getRecSaveMode(self):
        return 1

    def getSaveFormat(self):
        return self.saveFormat.value

    def getMultiDetectorSingleFile(self):
        return False

    def updateCameraLapseNum(self, value):
        self.progress.append(value)

    def getTimelapseNumFrames(self):
        self.settingReads.append('numFrames')
        return 99

    def getSpecTimelapseFrameTime(self):
        self.settingReads.append('interval')
        return 99.0

    def getTimelapseSingleFile(self):
        self.settingReads.append('singleFile')
        return False

    def getDetectorMode(self):
        self.settingReads.append('detectorMode')
        return -3

    def getSelectedSpecificDetectors(self):
        self.settingReads.append('detectors')
        return ['WidgetCam']

    def __getattr__(self, name):
        # Every other widget call is a no-op the controller makes for the UI.
        if name.startswith('_'):
            raise AttributeError(name)
        return lambda *args, **kwargs: None


class Harness:
    def __init__(self, tmp_path, monkeypatch, saveFormat=SaveFormat.HDF5):
        FakeTimer.created = []
        monkeypatch.setattr(module, 'Timer', FakeTimer)
        self.manager = FakeManager()
        self.shared = {}
        self.detectors = {
            'CamA': SimpleNamespace(isScanDriven=False, parameters={}),
            'CamB': SimpleNamespace(isScanDriven=False, parameters={}),
            'Scanned': SimpleNamespace(isScanDriven=True, parameters={}),
        }
        class _Shared(dict):
            def getHDF5Attributes(inner):
                return {'base': 'attr'}

        self.sharedAttrs = _Shared()
        ctrl = RecordingController.__new__(RecordingController)
        self.widget = FakeWidget(tmp_path, saveFormat)
        self.widget.controller = ctrl
        logger = SimpleNamespace(
            error=lambda *a, **k: None, info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
        )
        ctrl.__dict__.update(
            _widget=self.widget,
            _master=SimpleNamespace(
                recordingManager=self.manager,
                detectorsManager=self.detectors,
            ),
            _commChannel=SimpleNamespace(
                sharedAttrs=self.sharedAttrs, recordingWorkflow=None,
            ),
            _RecordingController__logger=logger,
            settingAttr=False,
            recording=False,
            doneScan=False,
            endedRecording=False,
            _finalizingRecCycle=False,
            lapseCurrent=-1,
            lapseTotal=0,
            stopRequested=False,
            timer=None,
            recMode=RecMode.SpecFrames,
            _seriesRequest=None,
            _seriesOnEnded=None,
            _lastRecordingFailure='',
            _recordingFailedCurrent=False,
            _recordingCycleTerminalHandled=False,
            _recordingFailureHandled=False,
            _cameraLapseIntervalS=None,
            _cameraLapseNextDeadline=None,
            _cameraLapsePlannedStart=None,
            _awaitingScanSourceArm=False,
            _scanStartPublished=False,
            _scanRequestAccepted=False,
        )
        self.ctrl = ctrl

    @property
    def timers(self):
        return FakeTimer.created

    def finishPoint(self):
        """The point's writer and scan are done: the cycle terminal."""
        self.manager.finishPoint()
        self.ctrl.recordingCycleEnded()


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


@pytest.fixture
def harness_factory(tmp_path, monkeypatch):
    return lambda **kwargs: Harness(tmp_path, monkeypatch, **kwargs)
