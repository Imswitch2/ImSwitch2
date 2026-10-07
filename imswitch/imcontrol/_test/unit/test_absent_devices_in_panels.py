"""P-2 in the panels (device-reconnect-2.0.md §4.5): a device that is not
connected is greyed out with the reason, never driven; a lifecycle result
brings it back."""
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.devices import (
    DeviceId,
    DeviceManagerStatusMixin,
    DeviceNotConnectedError,
)


class _Device(DeviceManagerStatusMixin):
    def __init__(self, name, *, connected=True, axes=('X',)):
        self.name = name
        self.axes = list(axes)
        self.position = {axis: 0.0 for axis in axes}
        self.calls = []
        if connected:
            self._setConnected('ok')
        else:
            self._setConnectionError(OSError('USB gone'), summary='stage not connected')

    def _requireUp(self):
        self._requireConnected('move')

    def move(self, dist, axis):
        self._requireUp()
        self.calls.append(('move', dist, axis))
        return {axis: dist}

    def setPosition(self, position, axis):
        self._requireUp()
        return {axis: position}

    def move_rel(self, dist):
        self._requireUp()
        self.calls.append(('rel', dist))

    def move_abs(self, pos):
        self._requireUp()
        self.calls.append(('abs', pos))

    def setEnabled(self, enabled):
        self._requireConnected('emission switch')
        self.calls.append(('enabled', enabled))
        return True

    def setValue(self, value):
        self._requireConnected('power')
        self.calls.append(('value', value))

    isBinary = False


# ------------------------------------------------------------------ widgets
def test_laser_widget_greys_an_absent_laser_with_the_reason(qtbot):
    from imswitch.imcontrol.view.widgets.LaserWidget import LaserWidget

    widget = LaserWidget(None)
    qtbot.addWidget(widget)
    widget.addLaser('488', 'mW', 0, 488, (0, 100), 1, (0, 0, 0))
    widget.setLaserUsable('488', False, 'not connected: USB gone')
    module = widget.laserModules['488']
    assert not module.enableButton.isEnabled() and 'USB gone' in module.toolTip()
    widget.setLaserUsable('488', True)
    assert module.enableButton.isEnabled() and module.toolTip() == ''


def test_positioner_widget_greys_every_axis_row_of_an_absent_stage(qtbot):
    from imswitch.imcontrol.view.widgets.PositionerWidget import PositionerWidget

    widget = PositionerWidget(None)
    qtbot.addWidget(widget)
    widget.addPositioner('XY', ['X', 'Y'], False, False)
    widget.setPositionerUsable('XY', False, 'stage not connected')
    for axis in ('X', 'Y'):
        suffix = widget._getParNameSuffix('XY', axis)
        assert not widget.pars['UpButton' + suffix].isEnabled()
        assert 'not connected' in widget.pars['Label' + suffix].toolTip()
    widget.setPositionerUsable('XY', True)
    assert widget.pars['UpButton' + widget._getParNameSuffix('XY', 'X')].isEnabled()


def test_rotator_widget_greys_an_absent_rotator(qtbot):
    from imswitch.imcontrol.view.widgets.RotatorWidget import RotatorWidget

    widget = RotatorWidget(None)
    qtbot.addWidget(widget)
    widget.addRotator('HWP')
    widget.addRotator('QWP')
    widget.setRotatorUsable('HWP', False, 'mount not connected')
    assert not widget.pars['ForwButtonHWP'].isEnabled()
    assert widget.pars['ForwButtonQWP'].isEnabled()           # the other row stays live
    assert 'not connected' in widget.pars['LabelHWP'].toolTip()


# -------------------------------------------------------------- controllers
def _laser_controller(lasers):
    from imswitch.imcontrol.controller.controllers.LaserController import LaserController

    calls = []
    controller = LaserController.__new__(LaserController)
    controller.__dict__.update(
        _master=SimpleNamespace(lasersManager=lasers),
        _widget=SimpleNamespace(
            setLaserUsable=lambda n, u, r='': calls.append(('usable', n, u, bool(r))),
            setLaserActive=lambda n, a, emitSignal=True: calls.append(('active', n, a)),
            setValue=lambda n, v, emitSignal=True: calls.append(('value', n, v)),
            isLaserActive=lambda n: False,
        ),
        _commChannel=SimpleNamespace(sharedAttrs={}),
        _logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: calls.append(('warn',))),
        settingAttr=False,
    )
    controller.__dict__['_invokeOnControllerThreadIfNeeded'] = lambda cb: cb()
    return controller, calls


class _Group(dict):
    def __iter__(self):
        return iter(self.items())


def test_laser_panel_greys_absent_lasers_and_refuses_commands_quietly():
    absent = _Device('775', connected=False)
    live = _Device('488')
    controller, calls = _laser_controller(_Group({'775': absent, '488': live}))

    controller._refreshLaserUsability()
    assert ('usable', '775', False, True) in calls and ('usable', '488', True, False) in calls

    calls.clear()
    controller.toggleLaser('775', True)                 # a click on a greyed row, or a script
    assert absent.calls == []                           # nothing reached the laser
    assert ('warn',) in calls and ('active', '775', False) in calls
    assert controller._commChannel.sharedAttrs[('Laser', '775', 'Enabled')] is False

    controller.toggleLaser('488', True)
    assert live.calls == [('enabled', True)]


def test_laser_panel_brings_a_laser_back_on_its_lifecycle_result():
    laser = _Device('775', connected=False)
    controller, calls = _laser_controller(_Group({'775': laser}))
    controller._refreshLaserUsability()
    assert calls[-1] == ('usable', '775', False, True)
    laser._setConnected('reconnected')
    controller._deviceLifecycleChanged(SimpleNamespace(
        affected_device_ids=(DeviceId('laser', '775'),), deactivated_device_ids=()))
    assert calls[-1] == ('usable', '775', True, False)


def test_positioner_panel_greys_absent_stages_and_skips_their_refresh():
    from imswitch.imcontrol.controller.controllers.PositionerController import (
        PositionerController,
    )

    absent = _Device('XY', connected=False, axes=('X', 'Y'))
    live = _Device('Z', axes=('Z',))
    calls = []
    controller = PositionerController.__new__(PositionerController)
    controller.__dict__.update(
        _master=SimpleNamespace(positionersManager=_Group({'XY': absent, 'Z': live})),
        _widget=SimpleNamespace(
            setPositionerUsable=lambda n, u, r='': calls.append(('usable', n, u))),
        _logger=SimpleNamespace(warning=lambda *a, **k: None),
        _joystickAutoReenable=False,
    )
    controller.__dict__['_isPositionerShownInWidget'] = lambda m: True
    controller.__dict__['updatePosition'] = lambda n, a: calls.append(('refresh', n))

    controller._refreshLifecycleAffectedPositioners((DeviceId('positioner', 'XY'),
                                                     DeviceId('positioner', 'Z')))
    assert ('usable', 'XY', False) in calls and ('usable', 'Z', True) in calls
    assert ('refresh', 'Z') in calls and ('refresh', 'XY') not in calls

    with pytest.raises(DeviceNotConnectedError, match='move refused'):
        controller.move('XY', 'X', 1.0)
    assert absent.calls == []


def test_rotator_panel_greys_absent_rotators():
    from imswitch.imcontrol.controller.controllers.RotatorController import RotatorController

    absent = _Device('HWP', connected=False)
    calls = []
    controller = RotatorController.__new__(RotatorController)
    controller.__dict__.update(
        _master=SimpleNamespace(rotatorsManager=_Group({'HWP': absent})),
        _widget=SimpleNamespace(setRotatorUsable=lambda n, u, r='': calls.append(('usable', n, u)),
                                getRelStepSize=lambda n: 5.0),
    )
    controller.__dict__['updatePosition'] = lambda n: calls.append(('refresh', n))
    controller._refreshLifecycleAffectedRotators((DeviceId('rotator', 'HWP'),))
    assert calls == [('usable', 'HWP', False)]
    with pytest.raises(DeviceNotConnectedError):
        controller.moveRel('HWP', 1)
    assert absent.calls == []
