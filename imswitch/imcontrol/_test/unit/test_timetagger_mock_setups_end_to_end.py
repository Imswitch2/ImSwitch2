"""The shipped Time Tagger mock setups, end to end without a window: a real
MasterController (simulated NI-DAQ, mock card), the Advanced scan
controller and the Settings controller, one scan from the scan panel's own
button. Every Time Tagger detector of the setup images its own input in the
same scan session, the card is free afterwards, and the attributes a
recording stores for each detector name its card input and that input's
conditioning. (napari is not started: there is no OpenGL here.)"""

from __future__ import annotations

import time

import numpy as np
import pytest
from qtpy import QtTest

from imswitch.imcontrol._test import optionsBasic
from imswitch.imcontrol._test.unit.test_scan_cloak_contract import _Main, _controllerClass
from imswitch.imcontrol.controller.CommunicationChannel import CommunicationChannel
from imswitch.imcontrol.controller.MasterController import MasterController
from imswitch.imcontrol.controller.basecontrollers import ImConWidgetControllerFactory
from imswitch.imcontrol.view import ViewSetupInfo, widgets

pytestmark = pytest.mark.nohardware

SETUPS = 'imswitch/_data/user_defaults/imcontrol_setups/'

#: setup -> {detector: (measurement, photon input, channel)}
CASES = {
    'galvo_two_apd_counting_mock_setup.json': {
        'TT_green': ('counting', 'apd_green', -1), 'TT_red': ('counting', 'apd_red', -2)},
    'galvo_two_colour_flim_mock_setup.json': {
        'FLIM_green': ('flim', 'apd_green', -1), 'FLIM_red': ('flim', 'apd_red', -2)},
    'galvo_flim_mock_scan_setup.json': {'FLIM': ('flim', 'photons', -1)},
}


@pytest.fixture(params=sorted(CASES))
def rig(request, qtbot):
    setup = ViewSetupInfo.from_json(open(SETUPS + request.param).read())
    main = _Main()
    channel = CommunicationChannel(main, setup)
    master = MasterController(setup, channel, main._moduleCommChannel)
    factory = ImConWidgetControllerFactory(setup, master, channel, main._moduleCommChannel)
    settings = factory.createController(_controllerClass('SettingsController'),
                                        widgets.SettingsWidget(optionsBasic))
    main.controllers['Settings'] = settings
    panel = widgets.ScanWidgetAdvanced(optionsBasic)
    scan = factory.createController(_controllerClass('ScanControllerAdvanced'), panel)
    main.controllers['Scan'] = scan
    events = []
    channel.sigScanEnded.connect(lambda *a: events.append('ended'))
    yield dict(name=request.param, master=master, channel=channel, panel=panel,
               events=events, settings=settings)
    try:
        factory.closeAllCreatedControllers(waitTimeoutS=5.0)
    finally:
        master.closeEvent()


def _scanOnce(rig, timeout_s=60.0):
    rig['panel'].setRepeatEnabled(False)
    rig['panel'].scanButton.click()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and 'ended' not in rig['events']:
        QtTest.QTest.qWait(50)
    return 'ended' in rig['events']


def test_every_time_tagger_detector_images_its_own_input_in_one_session(rig):
    expected = CASES[rig['name']]
    master = rig['master']
    card = master.timeTaggerManager
    detectors = master.detectorsManager
    assert _scanOnce(rig)
    assert not card.scanHeld, 'the card is free after the scan'
    session = card.scanSession
    assert session.everyone == tuple(expected) and session.closed and session.leftClean()
    images = {}
    for name, (measurement, photon_input, channel) in expected.items():
        det = detectors[name]
        assert det.measurement == measurement and det.photonInput == photon_input
        image = np.asarray(det.getLatestFrame(), dtype=np.float64)
        assert image.size > 1 and image.sum() > 0, name
        images[name] = image
    if len(images) == 2:
        a, b = images.values()
        assert not np.allclose(a / a.sum(), b / b.sum()), 'two inputs, two images'


def test_recorded_attributes_name_each_detectors_input_and_conditioning(rig):
    expected = CASES[rig['name']]
    card = rig['master'].timeTaggerManager
    rig['settings'].updateSharedAttrs()
    attrs = rig['channel'].sharedAttrs.getHDF5Attributes()
    for name, (measurement, photon_input, channel) in expected.items():
        conditioning = card.channelInfo(photon_input)

        def attr(param):
            return attrs[f'Detector:{name}:Param:{param}']
        assert attr('click_role') == photon_input
        assert attr('click_channel') == channel
        assert attr('click_trigger') == conditioning.trigger_v
        assert attr('click_deadtime_ps') == conditioning.deadtime_ps
        assert attr('click_delay_ps') == conditioning.delay_ps
        assert attr('measurement') == measurement
