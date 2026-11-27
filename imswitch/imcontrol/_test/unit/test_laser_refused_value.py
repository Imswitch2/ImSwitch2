"""A laser value the laser refused never reaches the recorded metadata.

``LaserController._setLaserValue`` used to publish the requested value to
the shared attributes -- and so into every later recording -- whatever the
manager answered.  A manager that refuses (``setValue`` returns ``False``)
now leaves the value the laser actually has.
"""
from types import SimpleNamespace

from imswitch.imcontrol.model import LaserPowerContext, LaserPowerMode


class _Lasers(dict):
    def __iter__(self):
        return iter(self.items())


class _Laser:
    isBinary = False
    valueUnits, valueRangeMin, valueRangeMax = 'mW', 0, 100

    def __init__(self, accept):
        self.accept, self.calls = accept, []

    def setValue(self, value):
        self.calls.append(('value', value))
        return None if self.accept else False

    def setEnabled(self, enabled):
        self.calls.append(('enabled', enabled))


def _controller(laser):
    from imswitch.imcontrol.controller.controllers.LaserController import LaserController

    shown = []
    controller = LaserController.__new__(LaserController)
    controller.__dict__.update(
        _master=SimpleNamespace(lasersManager=_Lasers({'488': laser})),
        _widget=SimpleNamespace(
            setValue=lambda n, v, emitSignal=True: shown.append(v),
            setLaserActive=lambda n, a, emitSignal=True: None,
            getValue=lambda n: 5,
        ),
        _commChannel=SimpleNamespace(sharedAttrs={('Laser', '488', 'Value'): 5}),
        _logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
        settingAttr=False, is_scanning=False, _powerMode=LaserPowerMode.COUPLED,
        _resumableLiveEnabledSnapshot=None, _livePowerValues={'488': 5},
        _currentPowerContext=LaserPowerContext.LIVE, _scanPowerValues={},
    )
    controller.__dict__['_refreshLaserRow'] = lambda *a, **k: None
    return controller, shown


def test_refused_value_is_not_published_and_reported():
    laser = _Laser(accept=False)
    controller, shown = _controller(laser)
    assert controller.setLaserValue('488', 80) is False
    assert laser.calls == [('value', 80)]
    assert controller._commChannel.sharedAttrs[('Laser', '488', 'Value')] == 5
    assert controller._livePowerValues == {'488': 5}
    assert 80 not in shown


def test_accepted_value_is_published():
    laser = _Laser(accept=True)
    controller, _ = _controller(laser)
    assert controller.setLaserValue('488', 80) is True
    assert controller._commChannel.sharedAttrs[('Laser', '488', 'Value')] == 80


def test_units_and_range_are_published_next_to_the_value():
    laser = _Laser(accept=True)
    controller, _ = _controller(laser)
    controller._publishLaserValueContext('488', laser)
    attrs = controller._commChannel.sharedAttrs
    assert attrs[('Laser', '488', 'Units')] == 'mW'
    assert attrs[('Laser', '488', 'RangeMin')] == 0
    assert attrs[('Laser', '488', 'RangeMax')] == 100
    assert ('Laser', '488', 'Calibration') not in attrs  # reserved slot, empty for now
