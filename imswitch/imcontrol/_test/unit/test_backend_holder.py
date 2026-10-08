"""The backend holder (device-reconnect-2.0.md §4.1) and the default
lifecycle every manager gets with it (§4.2)."""
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.devices import (
    BackendLifecycle,
    DeviceConnectionState,
    DeviceLifecycleAction,
    DeviceLifecycleService,
    DeviceManagerStatusMixin,
    DeviceNotConnectedError,
    DeviceRuntimeMode,
    DeviceSupervisor,
    HardwareDeviceId,
    backend_attribute,
)
from imswitch.imcontrol.model.managers.MultiManager import MultiManager


class _Driver:
    instances = 0

    def __init__(self, serial):
        _Driver.instances += 1
        self.serial = serial
        self.closed = False
        self.position = 0.0

    def move(self, d):
        self.position += d

    def close(self):
        self.closed = True


class _Stage(DeviceManagerStatusMixin):
    """A manager that opens its driver through the holder; its call sites
    use ``self._stage`` as before."""

    _stage = backend_attribute()

    def __init__(self, name, *, serial='A', present=True, **rules):
        self.name = name
        self.present = present
        self.serial = serial
        self.reinitialised = 0
        self.safe = []
        self._installBackend(self._open, lambda: SimpleNamespace(mock=True, position=0.0),
                             label=f'stage {serial}', **rules)

    def _open(self):
        if not self.present:
            raise OSError(f'{self.serial} not found')
        return _Driver(self.serial)

    def move(self, d):
        self._stage.move(d)

    def _lifecycleReinitialise(self):
        self.reinitialised += 1

    def _lifecycleSafeState(self, *, verified):
        self.safe.append(verified)
        return []


class _Group(MultiManager):
    def __init__(self, entries):
        self._subManagers = dict(entries)
        self._shutdownFinalizedSubManagerObjects = []


def _service(**stages):
    master = SimpleNamespace(
        detectorsManager=_Group({}), lasersManager=_Group({}),
        positionersManager=_Group(stages), rotatorsManager=_Group({}),
        flipMirrorsManager=_Group({}), rs232sManager=_Group({}), slmsManager=_Group({}),
        nidaqManager=None, pulseGeneratorManager=None, triggerScopeManager=None,
        standManager=None,
        scanExecutionCoordinator=SimpleNamespace(activeRunToken=None, activeToken=None),
        recordingManager=SimpleNamespace(record=False),
    )
    return DeviceLifecycleService(master, DeviceSupervisor(master))


# -------------------------------------------------------------- the rules
def test_a_present_device_is_real_and_connected():
    stage = _Stage('xy')
    assert stage.backendIsReal and stage.connectionState is DeviceConnectionState.CONNECTED
    stage.move(2.0)
    assert stage._stage.position == 2.0


def test_an_absent_device_is_not_connected_and_refuses_every_use():
    stage = _Stage('xy', present=False)
    assert not stage.backendIsReal
    assert stage.runtimeMode is DeviceRuntimeMode.REAL           # never a mock
    assert stage.connectionState is DeviceConnectionState.ERROR
    assert 'not connected' in stage.connectionStatusSummary
    with pytest.raises(DeviceNotConnectedError, match='stage A: move refused'):
        stage.move(1.0)
    assert not stage._stage                                      # falsy stand-in
    stage.backendHolder.close(suppress_errors=False)             # closing it is a no-op


def test_the_opt_in_installs_a_mock_on_failure_and_a_configured_mock_always():
    fallback = _Stage('xy', present=False, use_mock_on_failure=True)
    assert fallback.runtimeMode is DeviceRuntimeMode.MOCK and fallback._stage.mock
    assert fallback.isUsable
    configured = _Stage('xy', present=True, configured_mock=True)
    assert configured.runtimeMode is DeviceRuntimeMode.MOCK and configured._stage.mock
    assert _Driver.instances == 0 or configured._stage.mock       # the real opener was not used


def test_a_transient_device_starts_absent_without_touching_hardware():
    before = _Driver.instances
    stage = _Stage('xy', transient=True)
    assert _Driver.instances == before
    assert stage.runtimeMode is DeviceRuntimeMode.ABSENT
    assert stage.connectionState is DeviceConnectionState.DISCONNECTED
    early = _Stage('xy', transient=True, connect_on_startup=True)
    assert early.backendIsReal


# ------------------------------------------------------- default lifecycle
def test_the_default_lifecycle_reconnects_by_reopening_the_backend():
    stage = _Stage('xy', present=False)
    lifecycle = stage.getDeviceLifecycle()
    assert isinstance(lifecycle, BackendLifecycle)
    assert lifecycle.capabilities.reconnect and not lifecycle.capabilities.connect
    assert stage.getDeviceLifecycle() is lifecycle

    result = lifecycle.reconnect()                     # still unplugged
    assert not result.success and 'did not connect' in result.summary

    stage.present = True
    result = lifecycle.reconnect()
    assert result.success and stage.backendIsReal
    assert stage.reinitialised == 1
    assert stage.safe == [False, False, True]           # best effort, then verified
    first = stage._stage
    stage.present = True
    lifecycle.reconnect()
    assert first.closed and stage._stage is not first   # the old driver was closed


def test_a_transient_device_connects_and_disconnects():
    stage = _Stage('xy', transient=True)
    lifecycle = stage.getDeviceLifecycle()
    assert lifecycle.capabilities.connect and lifecycle.capabilities.disconnect
    assert lifecycle.connect().success and stage.backendIsReal
    assert lifecycle.connect().summary == 'Already connected'
    driver = stage._stage
    assert lifecycle.disconnect().success
    assert driver.closed and stage.runtimeMode is DeviceRuntimeMode.ABSENT
    with pytest.raises(DeviceNotConnectedError):
        stage.move(1.0)


def test_a_failed_reinitialisation_fails_the_reconnect():
    stage = _Stage('xy')

    def broken():
        raise RuntimeError('position read failed')
    stage._lifecycleReinitialise = broken
    result = stage.getDeviceLifecycle().reconnect()
    assert not result.success and 'position read failed' in result.summary
    assert stage.connectionState is DeviceConnectionState.ERROR


# ---------------------------------------------- through the service / graph
def test_the_service_binds_the_graph_id_and_offers_reconnect_without_a_declaration():
    stage = _Stage('xy', present=False)
    service = _service(xy=stage)
    hardware_id = HardwareDeviceId('positioner', 'positioner:xy')
    assert service.canReconnect(hardware_id)
    stage.present = True
    result = service.reconnect(hardware_id)
    assert result.success
    assert result.hardware_id == hardware_id
    assert [d.name for d in result.affected_device_ids] == ['xy']
    assert stage.backendIsReal


def test_a_detector_replacement_runs_in_the_maintenance_window():
    events = []

    class _Detectors:
        def detectorLifecycleMaintenance(self, name):
            import contextlib

            @contextlib.contextmanager
            def window():
                events.append(('enter', name))
                yield
                events.append(('exit', name))
            return window()

        def clearFaultAfterHardwareReplacement(self, name):
            events.append(('clear', name))

    camera = _Stage('cam')
    camera._bindDetectorLifecycleHost(_Detectors(), 'cam')
    assert camera.getDeviceLifecycle().reconnect().success
    assert events == [('enter', 'cam'), ('clear', 'cam'), ('exit', 'cam')]


# ------------------------------------------------------------- probe (R-5)
def test_probe_asks_the_device_and_updates_its_status_without_replacing_it():
    stage = _Stage('xy')
    assert not stage.getDeviceLifecycle().capabilities.probe

    class _Probing(_Stage):
        def _lifecycleProbe(self):
            return f'stage {self.serial} answers: {self._stage.position:.1f}'

    probing = _Probing('xy')
    lifecycle = probing.getDeviceLifecycle()
    assert lifecycle.capabilities.probe
    driver = probing._stage
    result = lifecycle.probe()
    assert result.success and result.summary == 'stage A answers: 0.0'
    assert probing._stage is driver                        # nothing replaced
    assert probing.connectionStatusSummary == 'stage A answers: 0.0'

    probing._stage.position = float('nan')
    probing._lifecycleProbe = lambda: (_ for _ in ()).throw(OSError('no reply'))
    result = lifecycle.probe()
    assert not result.success and 'did not answer' in result.summary
    assert probing.connectionState is DeviceConnectionState.ERROR

    absent = _Probing('xy', present=False)
    result = absent.getDeviceLifecycle().probe()
    assert not result.success and 'not connected' in result.summary


def test_the_service_offers_check_and_never_takes_the_acquisition_gate_for_it():
    from imswitch.imcontrol.model.devices.acquisition_gate import get_acquisition_gate

    class _Probing(_Stage):
        def _lifecycleProbe(self):
            return 'answers'

    stage = _Probing('xy')
    service = _service(xy=stage)
    hardware_id = HardwareDeviceId('positioner', 'positioner:xy')
    assert service.canPerform(hardware_id, DeviceLifecycleAction.PROBE)
    assert service.getActionableHardwareIds(DeviceLifecycleAction.PROBE) == (hardware_id,)
    with get_acquisition_gate().maintenance('a scan is running'):
        result = service.probe(hardware_id)                # a check is allowed meanwhile
    assert result.success and result.action is DeviceLifecycleAction.PROBE


def test_a_transport_backed_device_probes_through_its_managers():
    from imswitch.imcontrol._test.unit.test_device_lifecycle import (
        FIRST_ID, _HookedManager, _ReopenableTransport, _transport_rig,
    )

    class _Probing(_HookedManager):
        def _lifecycleProbe(self):
            return 'unit SN 42 answers'

    transport = _ReopenableTransport()
    first = _Probing(FIRST_ID, name='first')
    service = _transport_rig(transport, first)
    assert service.canPerform(FIRST_ID, DeviceLifecycleAction.PROBE)
    result = service.probe(FIRST_ID)
    assert result.success and result.summary == 'unit SN 42 answers'
    assert transport.reconnects == 0                        # a check reopens nothing
