from imswitch.imcontrol.model.managers.lasers.Cobolt0601NewLaserManager import (
    Cobolt0601NewLaserManager,
)


class _Logger:
    def __init__(self):
        self.warnings = []

    def warning(self, message):
        self.warnings.append(message)


class _LaserWithUnsupportedPause:
    def __init__(self):
        self.pause_calls = 0
        self.current_calls = []

    def pause_emission(self):
        self.pause_calls += 1
        return 'Syntax error: illegal command'

    def constant_current(self, current):
        self.current_calls.append(current)


class _LaserWithRaisingPause(_LaserWithUnsupportedPause):
    def pause_emission(self):
        self.pause_calls += 1
        raise RuntimeError('unsupported')


def _manager_with_laser(laser):
    manager = object.__new__(Cobolt0601NewLaserManager)
    manager._laser = laser
    manager._pause_supported = None
    manager._Cobolt0601NewLaserManager__logger = _Logger()
    return manager


def test_cobolt_pause_falls_back_to_zero_current_on_illegal_command():
    laser = _LaserWithUnsupportedPause()
    manager = _manager_with_laser(laser)

    manager._pause_emission_safe()
    manager._pause_emission_safe()

    assert laser.pause_calls == 1
    assert laser.current_calls == [0, 0]
    assert manager._pause_supported is False


def test_cobolt_pause_falls_back_to_zero_current_on_exception():
    laser = _LaserWithRaisingPause()
    manager = _manager_with_laser(laser)

    manager._pause_emission_safe()

    assert laser.pause_calls == 1
    assert laser.current_calls == [0]
    assert manager._pause_supported is False
