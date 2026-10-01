"""A positioner moves to 0 when ImSwitch closes only if its setup asks for it."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from imswitch.imcontrol.controller.controllers.PositionerController import PositionerController
from imswitch.imcontrol.model.SetupInfo import PositionerInfo, SetupInfo
from imswitch.imcontrol.model.managers.PositionersManager import PositionersManager

pytestmark = pytest.mark.nohardware


def _positioners(entries):
    setupInfo = SetupInfo.from_dict({'positioners': entries})
    return PositionersManager(setupInfo.positioners)


def _closeWith(positionersManager):
    ctrl = PositionerController.__new__(PositionerController)
    ctrl._master = SimpleNamespace(positionersManager=positionersManager)
    ctrl._widget = MagicMock()
    ctrl._joystickAutoReenableTimers = {}
    ctrl._joystickAutoReenablePendingAxes = {}
    ctrl._liveUpdateTimer = SimpleNamespace(isActive=lambda: False)
    ctrl.closeEvent()


def test_reset_on_close_is_off_by_default():
    assert PositionerInfo(managerName='MockPositionerManager', axes=['X']).resetOnClose is False


def test_a_setup_that_omits_the_key_leaves_the_stage_where_it_is():
    manager = _positioners({
        'Stage': {'managerName': 'MockPositionerManager', 'axes': ['X'],
                  'forPositioning': True},
    })
    manager['Stage'].setPosition(12.5, 'X')

    _closeWith(manager)

    assert manager['Stage'].position['X'] == 12.5


def test_only_a_positioner_that_asks_for_it_is_moved_to_zero():
    manager = _positioners({
        'Resetting': {'managerName': 'MockPositionerManager', 'axes': ['X'],
                      'forPositioning': True, 'resetOnClose': True},
        'Staying': {'managerName': 'MockPositionerManager', 'axes': ['Y'],
                    'forPositioning': True},
    })
    manager['Resetting'].setPosition(3.0, 'X')
    manager['Staying'].setPosition(4.0, 'Y')

    _closeWith(manager)

    assert manager['Resetting'].position['X'] == 0
    assert manager['Staying'].position['Y'] == 4.0
