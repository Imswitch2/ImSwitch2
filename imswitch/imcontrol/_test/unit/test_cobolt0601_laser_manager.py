"""Tests for legacy Cobolt0601LaserManager scan-power safety."""

import logging
import threading
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.managers.lasers.Cobolt0601LaserManager import (
    Cobolt0601LaserManager,
)


class FakeLegacyCobolt:
    def __init__(self):
        self.enabled = False
        self.digital_mod = False
        self.mode = 'APC'
        self.power_sp = 0.0
        self.power_mod = 0.0
        self.queries = []
        self.enter_mod_mode_calls = 0

    def enter_mod_mode(self):
        self.enter_mod_mode_calls += 1

    def query(self, text):
        self.queries.append(text)
        return '0'


def _build_manager(num_lasers=1):
    manager = object.__new__(Cobolt0601LaserManager)
    manager._laser = FakeLegacyCobolt()
    manager._numLasers = num_lasers
    manager._digitalMod = False
    manager._enabled = False
    manager._setpoint_mw = 0.0
    manager._ioLock = threading.RLock()
    manager._backendIsMock = False
    manager._Cobolt0601LaserManager__logger = logging.getLogger(
        'test.Cobolt0601LaserManager'
    )
    return manager


@pytest.mark.nohardware
def test_zero_value_clears_stale_power_before_scan_mode():
    manager = _build_manager()
    manager._laser.power_sp = 35.0

    manager.setValue(0)

    assert manager._setpoint_mw == 0
    assert manager._laser.power_sp == 0
    assert manager._laser.mode == 'ACC'
    assert manager._laser.queries[-2:] == ['ci', 'slc 0.0']

    manager.setScanModeActive(True)

    assert manager._laser.power_mod == 0
    assert manager._laser.digital_mod is True
    assert manager._laser.enabled is False


@pytest.mark.nohardware
def test_scan_mode_uses_cached_gui_setpoint_not_hardware_power_query():
    manager = _build_manager(num_lasers=2)
    manager.setValue(80)
    manager._laser.power_sp = 999.0

    manager.setScanModeActive(True)

    assert manager._laser.power_mod == 40.0


# ---------------------------------------------------------------------------
# Legacy transport/lifecycle regression coverage
# ---------------------------------------------------------------------------


class FakeLifecycleCobolt:
    def __init__(self, serial='SERIAL-A', fail_enable_values=()):
        self.serial_number = serial
        self.idn = 'fake-cobolt'
        self._enabled = False
        self._digital_mod = False
        self.mode = 'APC'
        self.autostart = False
        self.power_sp = 0.0
        self.power_mod = 0.0
        self.queries = []
        self.mutations = []
        self.finalized = False
        self.fail_enable_values = {bool(value) for value in fail_enable_values}

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, value):
        value = bool(value)
        self.mutations.append(('enabled', value))
        if value in self.fail_enable_values:
            raise OSError(f'enable transition {value} failed')
        self._enabled = value

    @property
    def digital_mod(self):
        return self._digital_mod

    @digital_mod.setter
    def digital_mod(self, value):
        self.mutations.append(('digital_mod', bool(value)))
        self._digital_mod = bool(value)

    def enter_mod_mode(self):
        self.mutations.append(('enter_mod_mode', True))

    def query(self, text):
        self.queries.append(text)
        if text == 'gfv?':
            return '1.0'
        return '0'

    def finalize(self):
        self.finalized = True


class FakeLaserInfo:
    def __init__(self, ports=('COM1',)):
        self.managerProperties = {'digitalPorts': list(ports)}
        self.wavelength = 488
        self.valueRangeMin = 0
        self.valueRangeMax = 100
        self.valueRangeStep = 1


def _patch_openers(
    monkeypatch, startup, reconnect=None, mock=None, reconnect_error=None,
    *, startup_is_mock=False, startup_error=None,
):
    import importlib
    from imswitch.imcontrol.model.interfaces.lantzlasers import LantzLaserOpenResult

    base_module = importlib.import_module(
        'imswitch.imcontrol.model.managers.lasers.LantzLaserManager'
    )
    cobolt_module = importlib.import_module(
        'imswitch.imcontrol.model.managers.lasers.Cobolt0601LaserManager'
    )

    calls = []

    def opener(driver, ports, allowMockFallback=True):
        calls.append((tuple(ports), allowMockFallback))
        if allowMockFallback:
            return LantzLaserOpenResult(
                startup, startup_is_mock, startup_error
            )
        if reconnect_error is not None:
            raise reconnect_error
        return LantzLaserOpenResult(reconnect, False, None)

    monkeypatch.setattr(base_module, 'openLantzLaser', opener)
    monkeypatch.setattr(cobolt_module, 'openLantzLaser', opener)
    if mock is not None:
        monkeypatch.setattr(cobolt_module, 'openMockLantzLaser', lambda *_args: mock)
    return calls


@pytest.mark.nohardware
def test_lifecycle_reconnect_preserves_setpoint_and_forces_off(monkeypatch):
    startup = FakeLifecycleCobolt('SERIAL-A')
    reconnect = FakeLifecycleCobolt('SERIAL-A')
    _patch_openers(monkeypatch, startup, reconnect=reconnect)

    manager = Cobolt0601LaserManager(FakeLaserInfo(), '488')
    manager.setValue(42)
    manager.setEnabled(True)

    result = manager.getDeviceLifecycle().reconnect()

    assert result.success is True
    assert result.deactivated_device_ids[0].name == '488'
    assert startup.finalized is True
    assert manager._laser is reconnect
    assert manager._setpoint_mw == 42
    assert manager._enabled is False
    assert manager._digitalMod is False
    assert reconnect.enabled is False
    assert manager.runtimeMode.value == 'real'
    assert manager.connectionState.value == 'connected'


@pytest.mark.nohardware
def test_startup_mock_fallback_can_reconnect_to_real(monkeypatch):
    startup_mock = FakeLifecycleCobolt('MOCK')
    reconnect = FakeLifecycleCobolt('SERIAL-A')
    _patch_openers(
        monkeypatch,
        startup_mock,
        reconnect=reconnect,
        startup_is_mock=True,
        startup_error=OSError('COM1 unavailable'),
    )

    manager = Cobolt0601LaserManager(FakeLaserInfo(), '488')
    assert manager.runtimeMode.value == 'mock'
    assert manager.connectionState.value == 'error'

    result = manager.getDeviceLifecycle().reconnect()

    assert result.success is True
    assert manager._laser is reconnect
    assert manager.runtimeMode.value == 'real'
    assert manager.connectionState.value == 'connected'
    assert manager._enabled is False


@pytest.mark.nohardware
def test_lifecycle_failed_reconnect_uses_mock_and_reports_unknown_beam(monkeypatch):
    startup = FakeLifecycleCobolt('SERIAL-A')
    mock = FakeLifecycleCobolt('MOCK')
    _patch_openers(
        monkeypatch,
        startup,
        mock=mock,
        reconnect_error=OSError('port unavailable'),
    )

    manager = Cobolt0601LaserManager(FakeLaserInfo(), '488')
    manager.setValue(33)

    result = manager.getDeviceLifecycle().reconnect()

    assert result.success is False
    assert 'Physical emission state could not be verified' in result.details
    assert manager._laser is mock
    assert manager._setpoint_mw == 33
    assert manager._enabled is False
    assert manager.runtimeMode.value == 'mock'
    assert manager.connectionState.value == 'error'


@pytest.mark.nohardware
def test_lifecycle_rejects_changed_known_serial_before_safe_init(monkeypatch):
    startup = FakeLifecycleCobolt('SERIAL-A')
    reconnect = FakeLifecycleCobolt('SERIAL-B')
    mock = FakeLifecycleCobolt('MOCK')
    _patch_openers(monkeypatch, startup, reconnect=reconnect, mock=mock)

    manager = Cobolt0601LaserManager(FakeLaserInfo(), '488')
    reconnect.mutations.clear()
    reconnect.queries.clear()

    result = manager.getDeviceLifecycle().reconnect()

    assert result.success is False
    assert 'identity mismatch' in result.details.lower()
    assert reconnect.finalized is True
    # The candidate was rejected after read-only identity access and before
    # the safe-init mutating sequence.
    assert reconnect.mutations == []
    assert 'cp' not in reconnect.queries
    assert manager._laser is mock


@pytest.mark.nohardware
def test_lifecycle_probe_queries_every_real_port(monkeypatch):
    from imswitch.imcontrol.model.interfaces.lantzlasers import LinkedLantzLaser

    first = FakeLifecycleCobolt('A')
    second = FakeLifecycleCobolt('B')
    linked = LinkedLantzLaser([first, second])
    _patch_openers(monkeypatch, linked)

    manager = Cobolt0601LaserManager(FakeLaserInfo(('COM1', 'COM2')), 'linked')
    first.queries.clear()
    second.queries.clear()

    result = manager.getDeviceLifecycle().probe()

    assert result.success is True
    assert first.queries == ['gfv?']
    assert second.queries == ['gfv?']


@pytest.mark.nohardware
def test_linked_enable_partial_failure_best_effort_darkens_all_ports(monkeypatch):
    from imswitch.imcontrol.model.interfaces.lantzlasers import LinkedLantzLaser

    first = FakeLifecycleCobolt('A')
    second = FakeLifecycleCobolt('B', fail_enable_values=(True,))
    linked = LinkedLantzLaser([first, second])
    _patch_openers(monkeypatch, linked)

    manager = Cobolt0601LaserManager(FakeLaserInfo(('COM1', 'COM2')), 'linked')
    first.mutations.clear()
    second.mutations.clear()

    with pytest.raises(OSError, match='enable transition True failed'):
        manager.setEnabled(True)

    assert first.enabled is False
    assert second.enabled is False
    assert ('enabled', False) in first.mutations
    assert ('enabled', False) in second.mutations
    assert manager._enabled is False
    assert manager.connectionState.value == 'error'


@pytest.mark.nohardware
def test_legacy_driver_transport_failure_does_not_fake_enabled_off():
    from imswitch.imcontrol.model.lantzdrivers.cobolt.cobolt0601 import Cobolt0601_f2

    driver = object.__new__(Cobolt0601_f2)
    driver._enabled = True
    driver.query = lambda _cmd: (_ for _ in ()).throw(OSError('cable removed'))

    with pytest.raises(OSError, match='cable removed'):
        driver.enabled = False

    assert driver._enabled is True


@pytest.mark.nohardware
def test_legacy_driver_optional_unsupported_query_still_uses_local_state():
    from imswitch.imcontrol.model.lantzdrivers.cobolt.cobolt0601 import Cobolt0601_f2

    driver = object.__new__(Cobolt0601_f2)
    driver._enabled = True
    driver.query = lambda _cmd: 'Syntax error: illegal command'

    assert driver.enabled is True

@pytest.mark.nohardware
def test_lantz_open_is_atomic_across_linked_ports(monkeypatch):
    import imswitch.imcontrol.model.interfaces.lantzlasers as lantzlasers

    real_instances = []
    mock_instances = []

    class RealDriver:
        def __init__(self, port):
            self.port = port
            self.finalized = False
            real_instances.append(self)

        def initialize(self):
            if self.port == 'COM2':
                raise OSError('COM2 unavailable')

        def finalize(self):
            self.finalized = True

    class MockDriver:
        def __init__(self, port):
            self.port = port
            mock_instances.append(self)

        def initialize(self):
            pass

        def finalize(self):
            pass

    monkeypatch.setattr(
        lantzlasers,
        '_load_driver',
        lambda _name, mock=False: MockDriver if mock else RealDriver,
    )

    result = lantzlasers.openLantzLaser(
        'fake.Driver', ['COM1', 'COM2'], allowMockFallback=True
    )

    assert result.is_mock is True
    assert real_instances[0].finalized is True
    assert real_instances[1].finalized is True
    assert isinstance(result.laser, lantzlasers.LinkedLantzLaser)
    assert [laser.port for laser in result.laser.lasers] == ['COM1', 'COM2']
    assert all(isinstance(laser, MockDriver) for laser in result.laser.lasers)
