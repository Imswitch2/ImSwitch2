"""Reconnect vs acquisition and shutdown (review of the rebased branch).

1. Shutdown: no reconnect starts once it begins; a running one holds the
   controller shutdown barrier until it returns.
2. A scan or recording cannot start while a reconnect runs, and a reconnect
   cannot start while one runs -- one gate, held for the whole operation.
3. A PI connection failure while installing the replacement backend leaves
   the status a failure, not "reconnecting on mock backend".
"""
import threading
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.devices import DeviceConnectionState, DeviceRuntimeMode
from imswitch.imcontrol.model.devices.acquisition_gate import (
    AcquisitionBlockedError,
    AcquisitionGate,
    MaintenanceBlockedError,
    set_acquisition_gate,
)
from imswitch.imcontrol.model.devices.graph import HardwareDeviceId
from imswitch.imcontrol.model.devices.lifecycle import (
    DeviceLifecycleAction,
    DeviceLifecycleBlockedError,
    DeviceLifecycleResult,
)
from imswitch.imcontrol.model.devices.lifecycle_service import DeviceLifecycleService
from imswitch.imcontrol.model.devices.supervisor import DeviceSupervisor
from imswitch.imcontrol._test.unit.test_device_lifecycle import (
    _Lifecycle,
    _LifecycleManager,
    _master,
)
from imswitch.imcontrol._test.unit.test_pi_stage_manager_robustness import (  # noqa: F401
    _info,
    fake_pi,
)


@pytest.fixture(autouse=True)
def gate():
    fresh = AcquisitionGate()
    previous = set_acquisition_gate(fresh)
    yield fresh
    set_acquisition_gate(previous)


class _HookLifecycle(_Lifecycle):
    """Runs ``during`` inside the adapter call, then optionally blocks."""

    def __init__(self, hardware_id, during=None, release=None):
        super().__init__(hardware_id)
        self.during = during
        self.release = release
        self.entered = threading.Event()

    def reconnect(self):
        self.entered.set()
        if self.during is not None:
            self.during()
        if self.release is not None:
            self.release.wait(5)
        return super().reconnect()


def _service(lifecycle, **master_kw):
    manager = _LifecycleManager(lifecycle, name='managed')
    master = _master(lasers={'managed': manager}, **master_kw)
    return DeviceLifecycleService(master, DeviceSupervisor(master)), master


# ------------------------------------------------------------------ gate
def test_gate_admission_and_maintenance_exclude_each_other(gate):
    ticket = gate.admit('scan run')
    with pytest.raises(MaintenanceBlockedError, match='scan run'):
        with gate.maintenance('reconnect of X'):
            pass
    gate.release(ticket)
    with gate.maintenance('reconnect of X'):
        with pytest.raises(AcquisitionBlockedError, match='reconnect of X'):
            gate.admit('recording')
        with pytest.raises(MaintenanceBlockedError):
            with gate.maintenance('reconnect of Y'):
                pass
    gate.admit('recording')       # free again


# ------------------------------------------------- 2. scans and recordings
def test_scan_cannot_start_while_a_reconnect_runs():
    """Review reproduction: a scan reservation accepted during reconnect."""
    from imswitch.imcontrol._test.unit.test_scan_execution_coordinator import _setup
    from imswitch.imcontrol.model.managers.NidaqManager import ScanBusyError

    coordinator, _manager, _nidaq = _setup()
    attempts = []

    def try_scan():
        try:
            coordinator.reserveRun(owner='scan widget')
            attempts.append('accepted')
        except ScanBusyError as exc:
            attempts.append(str(exc))

    hardware_id = HardwareDeviceId('laser', 'managed:hook')
    service, _ = _service(_HookLifecycle(hardware_id, during=try_scan))
    result = service.reconnect(hardware_id)
    assert result.success
    assert len(attempts) == 1 and 'reconnect of' in attempts[0]
    assert coordinator.activeRunToken is None
    coordinator.releaseRun(coordinator.reserveRun(owner='scan widget'))   # free after


def test_reconnect_cannot_start_while_a_scan_holds_the_gate():
    from imswitch.imcontrol._test.unit.test_scan_execution_coordinator import _setup

    coordinator, _manager, _nidaq = _setup()
    run = coordinator.reserveRun(owner='scan widget')
    hardware_id = HardwareDeviceId('laser', 'managed:hook')
    lifecycle = _HookLifecycle(hardware_id)
    service, _ = _service(lifecycle)       # its master reports no scan: the gate decides
    with pytest.raises(DeviceLifecycleBlockedError, match='scan run'):
        service.reconnect(hardware_id)
    assert lifecycle.calls == 0
    coordinator.releaseRun(run)
    assert service.reconnect(hardware_id).success


def test_recording_cannot_start_while_a_reconnect_runs(gate):
    from imswitch.imcontrol._test.unit.test_recording import _OmeMetaDetectors
    from imswitch.imcontrol.model.managers.RecordingManager import RecordingManager

    manager = RecordingManager(_OmeMetaDetectors())
    with gate.maintenance('reconnect of camera'):
        with pytest.raises(RuntimeError, match='reconnect of camera'):
            manager.startRecording(['Cam'], None, 'x', None, {})
    assert manager.record is False
    assert gate.active() == []


# ------------------------------------------------------------ 1. shutdown
def test_no_reconnect_starts_after_shutdown_began():
    hardware_id = HardwareDeviceId('laser', 'managed:hook')
    lifecycle = _HookLifecycle(hardware_id)
    service, _ = _service(lifecycle)
    service.beginShutdown()
    with pytest.raises(DeviceLifecycleBlockedError, match='shutting down'):
        service.reconnect(hardware_id)
    assert lifecycle.calls == 0


def test_running_reconnect_holds_the_controller_shutdown_barrier():
    from imswitch.imcontrol.controller.controllers.HardwareStatusController import (
        HardwareStatusController,
    )

    release = threading.Event()
    hardware_id = HardwareDeviceId('laser', 'managed:hook')
    lifecycle = _HookLifecycle(hardware_id, release=release)
    service, master = _service(lifecycle)
    master.deviceLifecycleService = service

    worker = threading.Thread(target=service.reconnect, args=(hardware_id,))
    worker.start()
    assert lifecycle.entered.wait(2)

    controller = HardwareStatusController.__new__(HardwareStatusController)
    controller.__dict__.update(_master=master, _reconnectWorker=None,
                               _closing=False, _reconnectMessage='')
    controller.__dict__['_widget'] = SimpleNamespace(
        setReconnectBusy=lambda *a: None)
    assert HardwareStatusController.closeEvent(controller) is False   # still running
    assert controller.shutdownComplete() is False
    with pytest.raises(DeviceLifecycleBlockedError, match='shutting down'):
        service.reconnect(hardware_id)                                  # refused now
    controller.reconnect(hardware_id)                                   # GUI path refused too
    assert controller._reconnectWorker is None
    release.set()
    worker.join(5)
    assert controller.shutdownComplete() is True


# ------------------------------------------------------------ 3. PI stage
def test_pi_failure_while_installing_the_replacement_reports_failure(fake_pi, monkeypatch):
    """Review reproduction: USB lost while reading the joystick state of the
    new backend left 'PI stage reconnecting on mock backend'."""
    from imswitch.imcontrol.model.managers.positioners.PIStageManager import PIStageManager

    manager = PIStageManager(_info(), 'PI')
    started = len(fake_pi.created)
    original_qjon = fake_pi.qJON

    def qjon(self):
        if fake_pi.created.index(self) >= started:     # the replacement device
            raise OSError('USB lost while reading joystick state')
        return original_qjon(self)

    monkeypatch.setattr(fake_pi, 'qJON', qjon)
    result = manager.getDeviceLifecycle().reconnect()

    assert result.success is False
    assert 'USB lost' in result.details
    assert manager.runtimeMode is DeviceRuntimeMode.MOCK
    assert manager.connectionState is DeviceConnectionState.ERROR
