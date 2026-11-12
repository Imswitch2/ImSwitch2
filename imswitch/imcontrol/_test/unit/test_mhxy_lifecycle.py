"""Marzhauser USB reconnect without moving or inventing an XY coordinate frame."""
import threading
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.devices import (
    DeviceConnectionState, DeviceRuntimeMode, DeviceLifecycleService,
    DeviceSupervisor, DeviceLifecycleBusyError,
)
from imswitch.imcontrol.model.managers.positioners.MHXYStageManager import MHXYStageManager
from .test_device_lifecycle import _master, _Group


class Transport:
    def __init__(self):
        self.runtimeMode = DeviceRuntimeMode.REAL
        self.reply = '10 20'
        self.open_ok = True
        self.commands = []
        self.reopens = 0
        self.error = None

    def reconnectTransport(self):
        self.reopens += 1
        self.runtimeMode = DeviceRuntimeMode.REAL if self.open_ok else DeviceRuntimeMode.MOCK
        return self.open_ok

    def query(self, command):
        self.commands.append(command)
        if self.error:
            raise self.error
        return self.reply if command == '?pos' else 'serial-123'


def stage(transport):
    info = SimpleNamespace(axes=['X', 'Y'], managerProperties={'rs232device': 'serial'},
                           forPositioning=True, forScanning=False, resetOnClose=False,
                           joystick=False, liveUpdate=False)
    return MHXYStageManager(info, 'XY', rs232sManager={'serial': transport})


def test_service_discovers_stage_and_reconnect_resyncs_without_motion():
    transport = Transport()
    manager = stage(transport)
    master = _master()
    master.positionersManager = _Group({'XY': manager})
    service = DeviceLifecycleService(master, DeviceSupervisor(master))
    hardware_id = manager.getDeviceLifecycle().hardware_id
    assert service.canReconnect(hardware_id)
    transport.reply = '123 456'
    transport.commands.clear()
    result = service.reconnect(hardware_id)
    assert result.success
    assert transport.reopens == 1
    assert transport.commands == ['?pos']
    assert manager.position == {'X': 123, 'Y': 456}
    assert manager.connectionState is DeviceConnectionState.CONNECTED
    manager.move(2, 'X')
    assert manager.position['X'] == 125


def test_reconnect_recovers_startup_mock():
    transport = Transport()
    transport.runtimeMode = DeviceRuntimeMode.MOCK
    manager = stage(transport)
    assert not manager.positionSynced
    assert manager.getDeviceLifecycle().reconnect().success
    assert manager.position == {'X': 10, 'Y': 20}


@pytest.mark.parametrize('reply', [None, '', 'ERR', '10', 'nan 20', '10 inf'])
def test_invalid_readback_keeps_old_coordinates_but_blocks_motion(reply):
    transport = Transport()
    manager = stage(transport)
    transport.reply = reply
    assert not manager.getDeviceLifecycle().reconnect().success
    assert manager.connectionState is DeviceConnectionState.ERROR
    assert manager.position == {'X': 10, 'Y': 20}
    assert not manager.positionSynced
    transport.commands.clear()
    with pytest.raises(RuntimeError, match='position is unknown'):
        manager.setPosition(0, 'X')
    assert not transport.commands


def test_failed_port_open_can_be_retried():
    transport = Transport()
    manager = stage(transport)
    transport.open_ok = False
    assert not manager.getDeviceLifecycle().reconnect().success
    assert not manager.positionSynced
    transport.open_ok = True
    assert manager.getDeviceLifecycle().reconnect().success


def test_unplug_during_move_invalidates_position_and_reconnect_heals():
    transport = Transport()
    manager = stage(transport)
    transport.error = OSError('USB removed')
    with pytest.raises(OSError):
        manager.move(1, 'X')
    assert not manager.positionSynced
    assert manager.position['X'] == 10
    transport.error = None
    assert manager.getDeviceLifecycle().reconnect().success


def test_move_is_refused_while_reconnect_is_opening_port():
    transport = Transport()
    manager = stage(transport)
    entered, release = threading.Event(), threading.Event()
    def reopen():
        entered.set()
        assert release.wait(5)
        return True
    transport.reconnectTransport = reopen
    results = []
    thread = threading.Thread(target=lambda: results.append(manager.getDeviceLifecycle().reconnect()))
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(DeviceLifecycleBusyError):
            manager.move(1, 'X')
    finally:
        release.set()
        thread.join(5)
    assert results[0].success


def test_overlapping_ordinary_operations_wait_instead_of_failing():
    """Two moves overlapping (scan thread + GUI) is normal, not 'busy'."""
    transport = Transport()
    manager = stage(transport)
    entered, release = threading.Event(), threading.Event()
    original_query = transport.query

    def slow_query(command):
        if command.startswith('mor'):
            entered.set()
            assert release.wait(5)
        return original_query(command)

    transport.query = slow_query
    first = threading.Thread(target=manager.move, args=(1, 'X'))
    first.start()
    assert entered.wait(5)
    second = threading.Thread(target=manager.setPosition, args=(50, 'Y'))
    second.start()
    release.set()
    first.join(5)
    second.join(5)
    assert manager.position == {'X': 11, 'Y': 50}


def test_reconnect_waits_for_a_move_in_flight():
    transport = Transport()
    manager = stage(transport)
    entered, release = threading.Event(), threading.Event()
    order = []
    original_query = transport.query

    def slow_query(command):
        if command.startswith('mor'):
            entered.set()
            assert release.wait(5)
            order.append('move done')
        return original_query(command)

    original_reopen = transport.reconnectTransport

    def reopen():
        order.append('reopen')
        return original_reopen()

    transport.query = slow_query
    transport.reconnectTransport = reopen
    mover = threading.Thread(target=manager.move, args=(1, 'X'))
    mover.start()
    assert entered.wait(5)
    results = []
    reconnector = threading.Thread(
        target=lambda: results.append(manager.getDeviceLifecycle().reconnect()))
    reconnector.start()
    release.set()
    mover.join(5)
    reconnector.join(5)
    assert order == ['move done', 'reopen']
    assert results[0].success


def test_reconnect_through_the_shared_port_resyncs_the_stage_too():
    """Reconnect 2.0 R-1 routed an RS232-backed device through its port; the
    stage's own lifecycle was bypassed, and without transport hooks its
    position sync after the reopen was skipped. The hooks restore it."""
    from imswitch.imcontrol.model.devices.graph import DeviceDescriptorSpec, DeviceRole

    class _Port(Transport):
        def getDeviceDescriptorSpec(self):
            return DeviceDescriptorSpec(role=DeviceRole.RESOURCE)

    transport = _Port()
    manager = stage(transport)
    master = _master()
    master.positionersManager = _Group({'XY': manager})
    master.rs232sManager = _Group({'serial': transport})      # the port is in the graph
    service = DeviceLifecycleService(master, DeviceSupervisor(master))
    hardware_id = manager.getDeviceLifecycle().hardware_id
    assert service.transportOf(hardware_id) is not None       # the transport path
    transport.reply = '7 8'
    transport.commands.clear()
    result = service.reconnect(hardware_id)
    assert result.success, result
    assert transport.reopens == 1
    assert transport.commands == ['?pos']                     # re-synced, nothing moved
    assert manager.position == {'X': 7, 'Y': 8}
    assert manager.connectionState is DeviceConnectionState.CONNECTED

    transport.open_ok = False
    result = service.reconnect(hardware_id)
    assert not result.success and manager.positionSynced is False
    with pytest.raises(Exception):
        manager.move(1, 'X')                                   # motion disabled
