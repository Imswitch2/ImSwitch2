"""PhotometricsManager against its own mock: construction, and the trigger table.

The fallback used to hand the manager a MockHamamatsu, which answers none of
the PVCAM surface it reads, so a rig without pyvcam failed to load the whole
imcontrol module one line after the "loading mocker" warning. And the trigger
source had two maps, a write map and a read-back map, that disagreed on two
of three values.
"""

import logging

from imswitch.imcontrol.model.SetupInfo import DetectorInfo
from imswitch.imcontrol.model.managers.detectors.PhotometricsManager import (
    PhotometricsManager, TRIGGER_SOURCE_CODES,
)


def _make_manager():
    info = DetectorInfo(
        analogChannel=None, digitalLine=None, managerName='PhotometricsManager',
        managerProperties={'cameraListIndex': 'mock'},
        forAcquisition=True, forFocusLock=False,
    )
    return PhotometricsManager(info, 'Prime')


def test_the_mock_stands_in_for_the_camera():
    mgr = _make_manager()
    assert mgr.model == 'Mock Photometrics camera'
    assert tuple(mgr.fullShape) == (512, 512)
    assert mgr.parameters['Trigger source'].value == 'Internal trigger'


def test_every_trigger_source_round_trips_through_the_camera():
    mgr = _make_manager()
    for label, code in TRIGGER_SOURCE_CODES.items():
        mgr.setParameter('Trigger source', label)
        assert mgr._camera.exp_mode == code, label
        mgr._updatePropertiesFromCamera()
        assert mgr.parameters['Trigger source'].value == label, label


def test_trigger_codes_are_pvcam_extended_trigger_modes():
    assert TRIGGER_SOURCE_CODES == {
        'Internal trigger': 1792,
        'External "start-trigger"': 2048,
        'External "frame-trigger"': 2304,
    }


def test_an_unknown_exposure_mode_is_reported_not_guessed(caplog):
    mgr = _make_manager()
    mgr._camera.exp_mode = 2560  # EXT_TRIG_LEVEL: nothing here names it
    with caplog.at_level(logging.WARNING):
        mgr._updatePropertiesFromCamera()
    assert mgr.parameters['Trigger source'].value == 'Internal trigger'
    assert any('exposure mode 2560' in r.getMessage() for r in caplog.records)


def test_externally_triggered_frames_arrive_only_on_a_trigger():
    mgr = _make_manager()
    mgr.setParameter('Trigger source', 'External "frame-trigger"')
    mgr.startAcquisition()
    assert mgr.getChunk() == []
    mgr._camera.mockTrigger(2)
    first = mgr.getChunk()
    assert len(first) == 1 and first[0].shape == (512, 512)
    assert len(mgr.getChunk()) == 1
    assert mgr.getChunk() == []
    mgr.stopAcquisition()
