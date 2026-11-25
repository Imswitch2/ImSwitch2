"""A scan-once recording of a Time Tagger detector together with a camera
still completes, with the scan's frame boundary (PR #95) in force.

The Time Tagger detectors on dev are scan-driven and publish their image
when the scan ends; the recording that holds every frame until the scan's
boundary (``beadRecBoundary``) must still take that image, and the camera's
frames, and end. A real MasterController on the shipped FLIM mock setup
(simulated NI-DAQ, mock card) with a mock camera pulsed once per pixel, the
real Advanced scan controller and Recording controller.
"""
import copy
import json
from io import BytesIO

import h5py
import numpy as np
from qtpy import QtTest

from imswitch.imcontrol._test import optionsBasic
from imswitch.imcontrol._test.unit.test_scan_cloak_contract import _Main, _controllerClass
from imswitch.imcontrol.controller.CommunicationChannel import CommunicationChannel
from imswitch.imcontrol.controller.MasterController import MasterController
from imswitch.imcontrol.controller.basecontrollers import ImConWidgetControllerFactory
from imswitch.imcontrol.controller.controllers.RecordingController import RecordingController
from imswitch.imcontrol.view import ViewSetupInfo, widgets
from imswitch.imcontrol.view.widgets.RecordingWidget import RecordingWidget

from .test_beadrec_frame_alignment import CAMERA, IndexFrames

SETUP = 'imswitch/_data/user_defaults/imcontrol_setups/mock/time_tagger/galvo_flim_mock_scan_setup.json'
NX, NY = 4, 3


def test_a_scan_once_recording_of_the_time_tagger_and_a_camera_completes(qtbot):
    setupDict = json.load(open(SETUP))
    camera = copy.deepcopy(CAMERA)
    camera['digitalLine'] = 'Dev1/port0/line7'
    setupDict['detectors']['Camera'] = camera
    setup = ViewSetupInfo.from_json(json.dumps(setupDict))
    main = _Main()
    channel = CommunicationChannel(main, setup)
    master = MasterController(setup, channel, main._moduleCommChannel)
    factory = ImConWidgetControllerFactory(setup, master, channel, main._moduleCommChannel)
    try:
        panel = widgets.ScanWidgetAdvanced(optionsBasic)
        scan = factory.createController(_controllerClass('ScanControllerAdvanced'), panel)
        main.controllers['Scan'] = scan
        assert scan.beadRecBoundary == 'built'
        rec = factory.createController(RecordingController, RecordingWidget(optionsBasic))
        main.controllers['Recording'] = rec

        cam = master.detectorsManager['Camera']._camera
        source = IndexFrames()
        cam.mock_frame_source = source

        scan.getParameters()
        analog = copy.deepcopy(scan._analogParameterDict)
        digital = copy.deepcopy(scan._digitalParameterDict)
        analog.update(axis_length=[(NX - 1) * 0.5, (NY - 1) * 0.5, 0.0],
                      axis_step_size=[0.5, 0.5, 1.0], axis_centerpos=[0.0, 0.0, 0.0])
        digital.update(sequence_time=1e-3, advanced_mode=True)
        digital['pulse_starts_s'] = {**digital['pulse_starts_s'], 'Camera': [[0.0]]}
        digital['pulse_ends_s'] = {**digital['pulse_ends_s'], 'Camera': [[0.0005]]}
        scan._scanParams.apply(panel, analog, digital, scan.positioners, scan.TTLDevices)
        scan.getParameters()

        files, ended = {}, []
        master.recordingManager.sigMemoryRecordingAvailable.connect(
            lambda name, file, path, saved: files.__setitem__(str(name), file))
        channel.sigRecordingEnded.connect(lambda *a: ended.append(1))
        # A frame queued after arming and before the boundary is not the scan's.
        channel.sigScanDevicesResolved.connect(
            lambda *a: (source.special.append(1000), cam.mockTrigger(1)))
        rec._startRecordingToMemory(['FLIM', 'Camera'], rec.recScanOnce,
                                    'scan-once recording')
        for _ in range(600):
            QtTest.QTest.qWait(50)
            if ended:
                break
        assert ended, 'the recording never ended'
        QtTest.QTest.qWait(500)

        found = {}
        for name, file in files.items():
            with h5py.File(BytesIO(file.getvalue()), 'r') as handle:
                for detector in ('FLIM', 'Camera'):
                    node = handle.get(f'{detector}/data')
                    if node is not None:
                        found[detector] = (np.asarray(node[...]), dict(node.attrs))
        assert set(found) == {'FLIM', 'Camera'}, sorted(files)
        frames, attrs = found['Camera']
        values = frames.reshape(frames.shape[0], -1)[:, 0].astype(int).tolist()
        assert 1000 not in values
        # every planned camera frame, in order, from the first trigger on
        assert values == list(range(1, scan.getNumScanPositions() + 1))
        assert int(attrs['recording:pre_scan_frames_discarded']) == 1
        assert found['FLIM'][0].size > 0
    finally:
        try:
            factory.closeAllCreatedControllers(waitTimeoutS=5.0)
        finally:
            master.closeEvent()
