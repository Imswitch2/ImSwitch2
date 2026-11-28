"""BeadRec on the Advanced and Base scans: where each camera frame lands in the image.

A real MasterController on the simulated NI-DAQ with a mock Hamamatsu, the real
scan controller (Advanced, then Base) and the real BeadRecController (with its
real widget; only the napari main window is missing). The camera's frames are index coded:
the k-th real trigger renders a constant frame of value k, so the
reconstruction says which trigger each pixel came from. Injected frames (a
stale or an extra trigger) carry values of 1000 and up and cannot be mistaken
for a position.

Reported on a rig: "sometimes the first pixel was lost, shifting everything by
a bit ... at the end it still got enough pixels". Three defects allowed that
picture, and each has a test here:

* the frame boundary sat at sigScanStarted, after the first trigger could have
  fired, so a first frame already in the camera was thrown away;
* the reconstruction wrapped, so one frame too many overwrote pixel 0;
* the scan ended "complete" whatever the count (progress forced full).
"""
import copy
import dataclasses
import importlib
import json
import re

import numpy as np
import pytest
from qtpy import QtTest

from imswitch.imcontrol._test import optionsBasic
from imswitch.imcontrol.controller.CommunicationChannel import CommunicationChannel
from imswitch.imcontrol.controller.MasterController import MasterController
from imswitch.imcontrol.controller.basecontrollers import ImConWidgetControllerFactory
from imswitch.imcontrol.controller.controllers.BeadRecController import BeadRecController
from imswitch.imcontrol.controller.controllers.ScanControllerAdvanced import (
    ScanControllerAdvanced,
)
from imswitch.imcontrol.controller.controllers.ScanControllerBase import ScanControllerBase
from imswitch.imcontrol.view import ViewSetupInfo
from imswitch.imcontrol.view.widgets.BeadRecWidget import BeadRecWidget
from imswitch.imcontrol.view.widgets.ScanWidgetAdvanced import ScanWidgetAdvanced
from imswitch.imcontrol.view.widgets.ScanWidgetBase import ScanWidgetBase

from .test_simple_point_scan_controller import SETUP, _Main

BASE_SETUP = 'imswitch/_data/user_defaults/imcontrol_setups/mock/monalisa/hamamatsu_mock_scan_setup.json'

_sim = importlib.import_module(
    'imswitch.imcontrol.model.managers.mockscan.ScanSimulationCoordinator')

NX, NY = 6, 4
N = NX * NY
STALE = 1000.0
EXTRA = 2000.0
IDEAL = list(range(1, N + 1))

CAMERA = {'managerName': 'HamamatsuManager',
          'managerProperties': {'cameraListIndex': 'mock', 'hamamatsu': {
              'trigger_source': 2, 'subarray_hsize': 32, 'subarray_vsize': 32}},
          'digitalLine': 'Dev1/port0/line7', 'analogChannel': None, 'forAcquisition': True}


class IndexFrames:
    """k-th real trigger -> constant frame k (1-based); ``special`` frames are
    handed out first, in order, for triggers the test injects."""

    def __init__(self):
        self.real = 0
        self.special = []

    def nextFrames(self, n, shape):
        out = []
        for _ in range(n):
            if self.special:
                value = self.special.pop(0)
            else:
                self.real += 1
                value = self.real
            out.append(np.full(shape, value, dtype=np.uint16))
        return out

    def liveFrame(self, shape):
        return np.zeros(shape, dtype=np.uint16)


class BeadRig:
    """A scan panel with one camera pulse per pixel, and BeadRec on it."""

    def __init__(self, kind):
        self.kind = kind
        if kind == 'advanced':
            setup = json.load(open(SETUP))
            setup['scan'].update({
                'scanWidgetType': 'Advanced', 'scanDesigner': 'BetaScanDesigner',
                'TTLCycleDesigner': 'AdvancedScanTTLCycleDesigner',
                'scanDesignerParams': {'return_time': 1e-3, 'move_time': 1e-4,
                                       'settle_time': 1e-4}})
            setup['detectors']['Camera'] = copy.deepcopy(CAMERA)
            panelClass, controllerClass = ScanWidgetAdvanced, ScanControllerAdvanced
        else:
            setup = json.load(open(BASE_SETUP))
            setup['scan']['scanDesignerParams'] = {
                'return_time': 1e-3, 'move_time': 1e-4, 'settle_time': 1e-4}
            setup['detectors']['Camera']['managerProperties']['hamamatsu'].update(
                subarray_hsize=32, subarray_vsize=32, image_width=32, image_height=32)
            panelClass, controllerClass = ScanWidgetBase, ScanControllerBase
        self.setup = ViewSetupInfo.from_json(json.dumps(setup))
        self.main = _Main()
        self.channel = CommunicationChannel(self.main, self.setup)
        self.master = MasterController(self.setup, self.channel, self.main._moduleCommChannel)
        self.factory = ImConWidgetControllerFactory(
            self.setup, self.master, self.channel, self.main._moduleCommChannel)
        self.panel = panelClass(optionsBasic)
        self.scan = self.factory.createController(controllerClass, self.panel)
        self.main.controllers['Scan'] = self.scan
        self._program()
        self.ended = []
        self.channel.sigScanEnded.connect(lambda *a: self.ended.append(1))

        self.master.detectorsManager.setCurrentDetector('Camera')
        self.camera = self.master.detectorsManager['Camera']._camera
        self.source = IndexFrames()
        self.camera.mock_frame_source = self.source
        self.widget = BeadRecWidget(optionsBasic)
        self.bead = self.factory.createController(BeadRecController, self.widget)
        self.progress, self.status = [], []
        progress, status = self.widget.updateProgress, self.widget.setStatusText
        self.widget.updateProgress = lambda c, t: (self.progress.append((c, t)), progress(c, t))[1]
        self.widget.setStatusText = lambda text: (self.status.append(text), status(text))[1]
        self.widget.runButton.setChecked(True)
        self.bead.run()

    def _program(self):
        """6 x 4 pixels, 1 ms each, the camera pulsing once per pixel."""
        if self.kind == 'base':
            panel = self.panel
            panel.setScanDim(0, 'X')
            panel.setScanDim(1, 'Y')
            panel.setScanDim(2, 'None')
            for name, length in (('X', 3.0), ('Y', 2.0)):
                panel.setScanSize(name, length)
                panel.setScanStepSize(name, 0.5)
                panel.setScanCenterPos(name, 0.0)
            panel.setTTLStarts('Camera', [0.0])
            panel.setTTLEnds('Camera', [0.0005])
            panel.setSeqTimePar(1e-3)
            self.scan.getParameters()
            return
        self.scan.getParameters()
        analog = copy.deepcopy(self.scan._analogParameterDict)
        digital = copy.deepcopy(self.scan._digitalParameterDict)
        analog.update(axis_length=[3.0, 2.0, 0.0], axis_step_size=[0.5, 0.5, 1.0],
                      axis_centerpos=[0.0, 0.0, 0.0])
        digital.update(sequence_time=1e-3, advanced_mode=True)
        digital['pulse_starts_s'] = {**digital['pulse_starts_s'], 'Camera': [[0.0]]}
        digital['pulse_ends_s'] = {**digital['pulse_ends_s'], 'Camera': [[0.0005]]}
        self.scan._scanParams.apply(self.panel, analog, digital,
                                    self.scan.positioners, self.scan.TTLDevices)
        self.scan.getParameters()

    # -- driving ----------------------------------------------------------

    def trigger(self, *special):
        """Queue one externally triggered frame now; ``special``: its value."""
        self.source.special.extend(special)
        self.camera.mockTrigger(1)

    def run(self, settle_ms=1200):
        """One scan; returns the image BeadRec shows. The end-of-scan drain
        gives up after 0.5 s, hence the settle."""
        self.ended.clear()
        self.progress.clear()
        self.status.clear()
        if self.kind == 'base':
            self.scan.runScanAdvanced(sigScanStartingEmitted=False)
        else:
            self.scan.runScanAdvanced()
        deadline = QtTest.QTest
        for _ in range(600):
            if self.ended:
                break
            deadline.qWait(50)
        assert self.ended, 'the scan never ended'
        QtTest.QTest.qWait(settle_ms)
        return self.image()

    def image(self):
        return np.asarray(self.bead.recIm, float).reshape(NY, NX)

    def close(self):
        self.widget.runButton.setChecked(False)
        self.bead.run()
        try:
            self.factory.closeAllCreatedControllers(waitTimeoutS=5.0)
        finally:
            self.master.closeEvent()


@pytest.fixture(params=['advanced', 'base'])
def bead(request, qtbot):
    rig = BeadRig(request.param)
    yield rig
    rig.close()


def _status(bead):
    """The last status, without the late-frame note: the simulated scan can end
    before its last frames are delivered, so how many are late varies (the
    note itself is checked where the test makes a frame late)."""
    return re.sub(r' \(\d+ arrived after the scan ended\)$', '', bead.status[-1])


def _flat(image):
    return image.ravel().tolist()


def _simulated_camera_frames(monkeypatch, delta):
    """The simulated scan fires ``delta`` more (or fewer) camera triggers than
    the scan declares."""
    original = _sim.SimulatedScanPlan.fromScan.__func__

    def changed(cls, *args, **kwargs):
        plan = original(cls, *args, **kwargs)
        counts = dict(plan.frameCounts)
        counts['Camera'] += delta
        return dataclasses.replace(plan, frameCounts=counts)

    monkeypatch.setattr(_sim.SimulatedScanPlan, 'fromScan', classmethod(changed))


def _first_frame_in_the_camera_at_sigScanBuilt(bead, monkeypatch):
    """The scan's first trigger fires between sigScanBuilt and sigScanStarted
    (as it can on a rig: the output tasks are already running when
    sigScanStarted is emitted). The simulator then delivers the other N - 1."""
    _simulated_camera_frames(monkeypatch, -1)
    bead.channel.sigScanBuilt.connect(lambda *a: bead.trigger())


def test_the_scan_is_one_camera_frame_per_pixel(bead):
    assert tuple(bead.channel.getDimsScan())[:2] == (NX, NY)
    assert bead.scan.getNumCamTTL().get('Camera') == 1
    assert bead.channel.getFramesPerScanPixel() == 1


def test_a_clean_scan_places_every_frame_at_its_position(bead):
    image = bead.run()
    assert _flat(image) == IDEAL
    assert bead.progress[-1] == (N, N)
    assert _status(bead) == 'Bead reconstruction scan complete'
    assert bead.bead._scanDiag['boundary_excluded'] == 0


def test_a_first_frame_taken_before_sigScanStarted_is_placed_at_pixel_zero(bead, monkeypatch):
    """Was: the boundary at sigScanStarted drained that frame (the image came
    out shifted by -1 with an empty last pixel)."""
    _first_frame_in_the_camera_at_sigScanBuilt(bead, monkeypatch)
    assert _flat(bead.run()) == IDEAL
    assert _status(bead) == 'Bead reconstruction scan complete'
    assert bead.bead._scanDiag['t_built'] <= bead.bead._scanDiag['t_started']


def test_the_old_boundary_loses_that_frame(bead, monkeypatch):
    """The harness check: with the boundary where it used to be, the same
    sequence is shifted by one and the last pixel is empty."""
    monkeypatch.setattr(type(bead.scan), 'beadRecBoundary', 'started')
    _first_frame_in_the_camera_at_sigScanBuilt(bead, monkeypatch)
    assert _flat(bead.run()) == IDEAL[1:] + [0]
    assert _status(bead).startswith('Bead reconstruction incomplete: 23 of 24')
    assert bead.progress[-1] == (N - 1, N)


def test_a_stale_frame_from_before_the_scan_is_excluded(bead):
    """A frame in the camera when the scan is armed (a leftover trigger, a
    snap, live view in internal trigger) is drained at the boundary."""
    bead.channel.sigScanStarting.connect(lambda *a: bead.trigger(STALE))
    image = bead.run()
    assert _flat(image) == IDEAL
    assert bead.bead._scanDiag['boundary_excluded'] == 1
    assert _status(bead) == 'Bead reconstruction scan complete'


def test_an_extra_trigger_is_ignored_and_reported_and_pixel_zero_stays(bead, monkeypatch):
    """Was: the frame after the last pixel wrapped onto pixel 0."""
    _simulated_camera_frames(monkeypatch, +1)
    image = bead.run()
    assert _flat(image) == IDEAL
    assert _status(bead) == f'Bead reconstruction: {N} of {N} frames, 1 extra frame(s) ignored'
    assert bead.progress[-1] == (N, N)
    assert bead.bead.beadWorker.diagnostics()['surplus'] == 1


def test_a_short_scan_is_reported_as_incomplete(bead, monkeypatch):
    """Was: 'scan complete' and a full progress bar, with an empty last pixel."""
    _simulated_camera_frames(monkeypatch, -1)
    image = bead.run()
    assert _flat(image) == IDEAL[:-1] + [0]
    assert _status(bead) == f'Bead reconstruction incomplete: {N - 1} of {N} frames'
    assert bead.progress[-1] == (N - 1, N)


def test_a_late_last_frame_is_accepted_and_reported(bead, monkeypatch):
    """The final frame can arrive after sigScanEnded: kept, and counted as late."""
    _simulated_camera_frames(monkeypatch, -1)
    bead.channel.sigScanEnded.connect(lambda *a: bead.trigger())
    image = bead.run()
    assert _flat(image) == IDEAL
    assert bead.status[-1].startswith('Bead reconstruction scan complete (')
    assert bead.status[-1].endswith(' arrived after the scan ended)')
    assert bead.bead._lateFrames() >= 1


def test_a_scan_that_is_built_but_never_started_leaves_beadrec_clean(bead):
    channel, scan = bead.channel, bead.scan
    channel.setActiveScanSource(scan)
    channel.sigScanStarting.emit()
    channel.sigScanBuilt.emit([])
    assert bead.bead._scanArmed is True and bead.bead._scanDetectorHandle is not None
    channel.clearActiveScanSource(scan)
    channel.sigScanEnded.emit()                      # the build failed: the run ends
    QtTest.QTest.qWait(800)
    assert bead.bead._scanArmed is False
    assert bead.bead._scanDetectorHandle is None
    assert bead.bead._builtBoundaryPending is False
    assert 'detector frames received' in bead.status[-1]
    # and the next scan is unaffected
    bead.source.real = 0
    assert _flat(bead.run()) == IDEAL


def test_back_to_back_scans_do_not_share_frames(bead):
    assert _flat(bead.run()) == IDEAL
    bead.source.real = 0
    assert _flat(bead.run()) == IDEAL
    assert _status(bead) == 'Bead reconstruction scan complete'
