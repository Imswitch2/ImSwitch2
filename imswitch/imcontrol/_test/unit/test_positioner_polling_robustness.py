"""No-hardware tests for defensive PositionerController polling."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from imswitch.imcontrol.controller.controllers.PositionerController import PositionerController

pytestmark = pytest.mark.nohardware


class _Managers:
    def __init__(self, entries):
        self._entries = list(entries)
        self._by_name = dict(entries)

    def __iter__(self):
        return iter(self._entries)

    def __getitem__(self, name):
        return self._by_name[name]


class _Timer:
    def __init__(self):
        self.started = []
        self._active = False

    def start(self, interval=None):
        self.started.append(interval)
        self._active = True

    def stop(self):
        self._active = False

    def isActive(self):
        return self._active


def _controller(entries=()):
    ctrl = PositionerController.__new__(PositionerController)
    ctrl._master = SimpleNamespace(positionersManager=_Managers(entries))
    ctrl._widget = MagicMock()
    ctrl._PositionerController__logger = MagicMock()
    ctrl._liveUpdateFailureCount = {}
    ctrl._liveUpdateRetryAfter = {}
    ctrl._pollBackoffSeconds = (1.0, 2.0, 5.0, 10.0)
    ctrl._joystickAutoReenableFailureCount = {}
    ctrl._joystickAutoReenablePendingAxes = {}
    ctrl._joystickAutoReenableTimers = {}
    ctrl._joystickAutoReenablePollIntervalMs = 200
    ctrl._joystickAutoReenable = True
    return ctrl


def test_widget_visibility_is_configuration_driven_when_hardware_unavailable():
    ctrl = _controller()
    unavailable = SimpleNamespace(
        forPositioning=True,
        hide=False,
        isAvailable=False,
    )

    assert ctrl._isPositionerShownInWidget(unavailable) is True


def test_widget_visibility_still_respects_hide_and_for_positioning():
    ctrl = _controller()

    assert ctrl._isPositionerShownInWidget(SimpleNamespace(
        forPositioning=True, hide=True, isAvailable=False
    )) is False
    assert ctrl._isPositionerShownInWidget(SimpleNamespace(
        forPositioning=False, hide=False, isAvailable=False
    )) is False


def test_live_poll_failure_isolated_per_positioner(monkeypatch):
    bad = SimpleNamespace(name='Bad')
    good = SimpleNamespace(name='Good')
    ctrl = _controller([('Bad', bad), ('Good', good)])
    ctrl._isPositionerShownInWidget = lambda manager: True
    ctrl._isLiveUpdateEnabled = lambda name, manager: True

    calls = []

    def update(name, axis):
        calls.append((name, axis))
        if name == 'Bad':
            raise OSError('lost stage')

    ctrl.updatePosition = update
    monkeypatch.setattr(
        'imswitch.imcontrol.controller.controllers.PositionerController.time.monotonic',
        lambda: 100.0,
    )

    ctrl._refreshLiveUpdatedPositioners()

    assert calls == [('Bad', 'all'), ('Good', 'all')]
    assert ctrl._liveUpdateFailureCount['Bad'] == 1
    assert ctrl._liveUpdateRetryAfter['Bad'] == 101.0


def test_live_poll_backoff_skips_then_recovers(monkeypatch):
    manager = SimpleNamespace(name='Stage')
    ctrl = _controller([('Stage', manager)])
    clock = {'now': 10.0}
    monkeypatch.setattr(
        'imswitch.imcontrol.controller.controllers.PositionerController.time.monotonic',
        lambda: clock['now'],
    )

    updates = MagicMock(side_effect=[OSError('temporary failure'), None])
    ctrl.updatePosition = updates

    assert ctrl._pollPositionerPosition('Stage', manager) is False
    assert ctrl._liveUpdateFailureCount['Stage'] == 1
    assert ctrl._liveUpdateRetryAfter['Stage'] == 11.0

    clock['now'] = 10.5
    assert ctrl._pollPositionerPosition('Stage', manager) is False
    assert updates.call_count == 1

    clock['now'] = 11.1
    assert ctrl._pollPositionerPosition('Stage', manager) is True
    assert updates.call_count == 2
    assert 'Stage' not in ctrl._liveUpdateFailureCount
    assert 'Stage' not in ctrl._liveUpdateRetryAfter


def test_explicit_update_position_remains_strict():
    manager = SimpleNamespace(
        axes=['X'],
        position={'X': 1.0},
        updatePosition=MagicMock(side_effect=OSError('hardware error')),
    )
    ctrl = _controller([('Stage', manager)])

    with pytest.raises(OSError, match='hardware error'):
        ctrl.updatePosition('Stage', 'all')


def test_joystick_reenable_communication_failure_retries_without_enabling():
    manager = SimpleNamespace(
        name='Stage',
        joystickStatus=False,
        isAvailable=True,
        isMovementFinished=MagicMock(side_effect=OSError('status timeout')),
    )
    ctrl = _controller([('Stage', manager)])
    timer = _Timer()
    ctrl._joystickAutoReenableTimers['Stage'] = timer
    ctrl._joystickAutoReenablePendingAxes['Stage'] = {'X'}
    ctrl.requestJoystickStatus = MagicMock()

    ctrl._checkJoystickAutoReenable('Stage')

    ctrl.requestJoystickStatus.assert_not_called()
    assert ctrl._joystickAutoReenablePendingAxes['Stage'] == {'X'}
    assert ctrl._joystickAutoReenableFailureCount['Stage'] == 1
    assert timer.started == [1000]


def test_unsupported_movement_status_keeps_delay_only_fallback():
    manager = SimpleNamespace(
        name='Stage',
        joystickStatus=False,
        isAvailable=True,
    )
    ctrl = _controller([('Stage', manager)])
    timer = _Timer()
    ctrl._joystickAutoReenableTimers['Stage'] = timer
    ctrl._joystickAutoReenablePendingAxes['Stage'] = {'X'}
    ctrl.requestJoystickStatus = MagicMock()

    ctrl._checkJoystickAutoReenable('Stage')

    ctrl.requestJoystickStatus.assert_called_once_with(True, 'Stage')
    assert 'Stage' not in ctrl._joystickAutoReenablePendingAxes
