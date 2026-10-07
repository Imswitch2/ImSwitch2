"""Instruments as transient setup devices (plan §5-6, P-4)."""
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.devices import (
    DeviceConnectionState,
    DeviceRuntimeMode,
)
from imswitch.imcontrol.model.devices.lifecycle import DeviceLifecycleBlockedError
from imswitch.imcontrol.model.managers.InstrumentsManager import InstrumentsManager
from imswitch.imcontrol.model.managers.instruments.MockPowerMeterManager import (
    MockPowerMeterManager,
)
from imswitch.imcontrol.model.resources import (
    ResourceRegistry,
    instrument_key,
    set_resource_registry,
)
from imswitch.imcontrol.model.SetupInfo import InstrumentInfo, SetupInfo


@pytest.fixture(autouse=True)
def registry():
    fresh = ResourceRegistry()
    previous = set_resource_registry(fresh)
    yield fresh
    set_resource_registry(previous)


def _info(**kw):
    base = dict(managerName='MockPowerMeterManager', managerProperties={'powerW': 2e-3})
    base.update(kw)
    return InstrumentInfo(**base)


def test_setup_file_parses_the_instruments_section():
    setup = SetupInfo.from_dict({'instruments': {
        'pm1': {'managerName': 'MockPowerMeterManager', 'transient': True,
                'managerProperties': {'powerW': 0.001}},
        'pax1': {'managerName': 'MockPAXManager'},
    }})
    assert isinstance(setup.instruments['pm1'], InstrumentInfo)
    assert setup.instruments['pm1'].transient is True
    assert setup.instruments['pax1'].connectOnStartup is False


def test_transient_instrument_starts_absent_without_touching_hardware():
    manager = MockPowerMeterManager(_info(transient=True), 'pm1')
    assert manager.runtimeMode is DeviceRuntimeMode.ABSENT
    assert manager.connectionState is DeviceConnectionState.DISCONNECTED
    assert not manager.session.connected


def test_permanent_and_connect_on_startup_instruments_connect():
    assert MockPowerMeterManager(_info(), 'pm1').connected
    early = MockPowerMeterManager(_info(transient=True, connectOnStartup=True), 'pm2')
    assert early.connected
    assert early.connectionState is DeviceConnectionState.CONNECTED


def test_failed_startup_connect_is_disconnected_with_error_never_mock():
    class Unplugged(MockPowerMeterManager):
        def _createDriver(self, properties):
            driver = super()._createDriver(properties)
            driver.unplugged = True
            return driver

    manager = Unplugged(_info(transient=True, connectOnStartup=True), 'pm1')
    assert manager.connectionState is DeviceConnectionState.ERROR
    assert manager.runtimeMode is DeviceRuntimeMode.ABSENT
    assert not manager.connected


def test_lifecycle_connect_disconnect_reconnect():
    manager = MockPowerMeterManager(_info(transient=True), 'pm1')
    seen = []
    manager.addStatusListener(seen.append)
    lifecycle = manager.getDeviceLifecycle()
    assert lifecycle.capabilities.connect and lifecycle.capabilities.disconnect
    assert lifecycle.connect().success
    assert manager.connected and manager.runtimeMode is DeviceRuntimeMode.REAL
    assert lifecycle.reconnect().success
    assert lifecycle.disconnect().success
    assert manager.runtimeMode is DeviceRuntimeMode.ABSENT
    assert seen == ['pm1'] * 3                  # connect, reconnect, disconnect
    assert lifecycle.hardware_id.key == 'instrument:pm1'


def test_lifecycle_is_refused_while_a_run_holds_the_instrument(registry):
    manager = MockPowerMeterManager(_info(transient=True, connectOnStartup=True), 'pm1')
    run = registry.reserve([instrument_key('pm1')], 'laser power LUT')
    with pytest.raises(DeviceLifecycleBlockedError, match='laser power LUT'):
        manager.getDeviceLifecycle().disconnect()
    assert manager.connected
    registry.release(run.token)
    assert manager.getDeviceLifecycle().disconnect().success


def test_transport_fault_while_connected_is_an_error_and_notifies():
    manager = MockPowerMeterManager(_info(transient=True, connectOnStartup=True), 'pm1')
    seen = []
    manager.addStatusListener(seen.append)
    manager.session.driver.unplugged = True
    window = manager.session.sample_window(manager.session.open_window(), 3, 1.0)
    assert window.cause.value == 'transport_fault'
    assert manager.connectionState is DeviceConnectionState.ERROR
    assert not manager.connected
    assert seen == ['pm1']
    manager.session.driver.unplugged = False
    assert manager.getDeviceLifecycle().reconnect().success
    assert manager.connected


def test_instruments_manager_builds_entries_and_finalizes():
    group = InstrumentsManager({'pm1': _info(transient=True), 'pm2': _info()})
    names = dict(group)
    assert set(names) == {'pm1', 'pm2'}
    assert names['pm2'].connected and not names['pm1'].connected
    group.finalize()
    assert not names['pm2'].session.connected


def test_supervisor_lists_instruments_with_their_state():
    from imswitch.imcontrol._test.unit.test_device_lifecycle import _master
    from imswitch.imcontrol.model.devices.supervisor import DeviceSupervisor

    master = _master()
    master.instrumentsManager = InstrumentsManager({'pm1': _info(transient=True)})
    statuses = {s.hardware_id.key: s for s in DeviceSupervisor(master).getHardwareStatuses()}
    status = statuses['instrument:pm1']
    assert status.connection is DeviceConnectionState.DISCONNECTED
    assert status.mode is DeviceRuntimeMode.ABSENT


# ------------------------------------------- lifecycle service (P-4 step 2)
def _serviceWith(instruments, **master_kw):
    from imswitch.imcontrol._test.unit.test_device_lifecycle import _master
    from imswitch.imcontrol.model.devices import DeviceLifecycleService, DeviceSupervisor

    master = _master(**master_kw)
    master.instrumentsManager = InstrumentsManager(instruments)
    return DeviceLifecycleService(master, DeviceSupervisor(master)), master


def test_service_offers_connect_disconnect_reconnect_for_instruments_only():
    from imswitch.imcontrol.model.devices import DeviceLifecycleAction, HardwareDeviceId

    service, _ = _serviceWith({'pm1': _info(transient=True)})
    pm1 = HardwareDeviceId('instrument', 'instrument:pm1')
    for action in (DeviceLifecycleAction.CONNECT, DeviceLifecycleAction.DISCONNECT,
                   DeviceLifecycleAction.RECONNECT):
        assert service.getActionableHardwareIds(action) == (pm1,)
    assert not service.canPerform(pm1, DeviceLifecycleAction.PROBE)


def test_service_connects_and_disconnects_and_publishes_results():
    from imswitch.imcontrol.model.devices import DeviceLifecycleAction, HardwareDeviceId

    service, master = _serviceWith({'pm1': _info(transient=True)})
    pm1 = HardwareDeviceId('instrument', 'instrument:pm1')
    results = []
    service.addListener(results.append)
    assert service.connect(pm1).success
    assert master.instrumentsManager['pm1'].connected
    assert service.disconnect(pm1).success
    assert master.instrumentsManager['pm1'].runtimeMode is DeviceRuntimeMode.ABSENT
    assert [r.action for r in results] == [DeviceLifecycleAction.CONNECT,
                                           DeviceLifecycleAction.DISCONNECT]


def test_instruments_connect_during_a_scan_or_recording():
    """Instruments never take part in scans or recordings: their reservation
    guards them, not the acquisition gate."""
    from imswitch.imcontrol.model.devices import HardwareDeviceId
    from imswitch.imcontrol.model.devices.acquisition_gate import get_acquisition_gate

    service, master = _serviceWith({'pm1': _info(transient=True)},
                                   active_run='scan-1', recording=True)
    pm1 = HardwareDeviceId('instrument', 'instrument:pm1')
    with get_acquisition_gate().maintenance('someone else'):
        assert service.connect(pm1).success
    assert master.instrumentsManager['pm1'].connected


def test_service_refuses_instrument_transitions_after_shutdown_began():
    from imswitch.imcontrol.model.devices import HardwareDeviceId

    service, master = _serviceWith({'pm1': _info(transient=True)})
    service.beginShutdown()
    with pytest.raises(DeviceLifecycleBlockedError, match='connect is refused'):
        service.connect(HardwareDeviceId('instrument', 'instrument:pm1'))
    assert not master.instrumentsManager['pm1'].connected


def test_service_refuses_disconnect_while_reserved(registry):
    from imswitch.imcontrol.model.devices import HardwareDeviceId

    service, master = _serviceWith({'pm1': _info(transient=True, connectOnStartup=True)})
    registry.reserve([instrument_key('pm1')], 'polarisation map')
    with pytest.raises(DeviceLifecycleBlockedError, match='polarisation map'):
        service.disconnect(HardwareDeviceId('instrument', 'instrument:pm1'))
    assert master.instrumentsManager['pm1'].connected


def test_instrument_fault_reaches_service_status_listeners():
    from imswitch.imcontrol.model.devices import HardwareDeviceId

    service, master = _serviceWith({'pm1': _info(transient=True, connectOnStartup=True)})
    seen = []
    service.addStatusListener(seen.append)
    manager = master.instrumentsManager['pm1']
    manager.session.driver.unplugged = True
    manager.session.sample_window(manager.session.open_window(), 3, 1.0)
    assert seen == [HardwareDeviceId('instrument', 'instrument:pm1')]
    service.removeStatusListener(seen.append)


def test_hardware_status_controller_refreshes_on_an_instrument_fault():
    from imswitch.imcontrol.controller.controllers.HardwareStatusController import (
        HardwareStatusController,
    )
    from imswitch.imcontrol.model.devices import DeviceSupervisor, HardwareDeviceId

    service, master = _serviceWith({'pm1': _info(transient=True, connectOnStartup=True)})
    master.deviceLifecycleService = service
    master.deviceSupervisor = DeviceSupervisor(master)
    calls = []
    controller = HardwareStatusController.__new__(HardwareStatusController)
    controller.__dict__.update(_master=master, _closing=False)
    controller.__dict__['_widget'] = SimpleNamespace(
        setStatuses=lambda statuses, **ids: calls.append((statuses, ids)))
    controller.__dict__['_invokeOnControllerThreadIfNeeded'] = lambda callback: callback()
    service.addStatusListener(lambda _id: controller._invokeOnControllerThreadIfNeeded(
        controller.refresh))

    manager = master.instrumentsManager['pm1']
    manager.session.driver.unplugged = True
    manager.session.sample_window(manager.session.open_window(), 3, 1.0)

    statuses, ids = calls[-1]
    pm1 = HardwareDeviceId('instrument', 'instrument:pm1')
    status = {s.hardware_id: s for s in statuses}[pm1]
    assert status.connection is DeviceConnectionState.ERROR
    assert ids['connectableHardwareIds'] == (pm1,)
    assert ids['disconnectableHardwareIds'] == (pm1,)
