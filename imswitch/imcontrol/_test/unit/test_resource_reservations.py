"""Reservations and command admission (plan §7): registry, manager guards,
audited adapters, the waveform-output bridge, script handles, shutdown."""
import threading
import time
from types import SimpleNamespace

import pytest

from imswitch.imcommon.model.shutdown import ShutdownState
from imswitch.imcontrol.model.managers.lasers.LaserManager import RawDriveError
from imswitch.imcontrol.model.managers.lasers.NidaqLaserManager import NidaqLaserManager
from imswitch.imcontrol.model.managers.positioners.MockPositionerManager import (
    MockPositionerManager,
)
from imswitch.imcontrol.model.managers.positioners.PositionerManager import PositionerManager
from imswitch.imcontrol.model.managers.rotators.KinesisRotatorManager import (
    KinesisRotatorManager,
)
from imswitch.imcontrol.model.managers.rotators.RotatorManager import RotatorManager
from imswitch.imcontrol.model.measurement.adapters import (
    LaserRawDriveControl,
    NotAuditedError,
    PositionerAxisControl,
    RotatorManagerControl,
)
from imswitch.imcontrol.model.measurement.controls import ControlExecutor
from imswitch.imcontrol.model.resources import (
    WAVEFORM_OUTPUT,
    ReservationExpiredError,
    ResourceRegistry,
    ResourceReservedError,
    laser_key,
    positioner_key,
    rotator_key,
    set_resource_registry,
)


@pytest.fixture(autouse=True)
def registry():
    fresh = ResourceRegistry()
    previous = set_resource_registry(fresh)
    yield fresh
    set_resource_registry(previous)


# ------------------------------------------------------------------ fakes
class FakeRotator(RotatorManager):
    """Moves instantly, or blocks on ``gate`` (a stuck driver ignoring stop)."""

    def __init__(self, name='hwp'):
        super().__init__(None, name)
        self.gate = None
        self.entered = threading.Event()
        self.moves = []

    def move_abs(self, pos):
        self.entered.set()
        if self.gate is not None:
            self.gate.wait()
        self.moves.append(pos)
        self._position = pos

    def move_rel(self, dist):
        self.move_abs(self._position + dist)   # nested: shares the outer ticket

    def readPosition(self):
        return self._position


class TwoAxisStage(PositionerManager):
    def __init__(self, name='stage'):
        info = SimpleNamespace(axes=['X', 'Y'], forPositioning=True, forScanning=False,
                               resetOnClose=False, joystick=False, liveUpdate=False)
        super().__init__(info, name, initialPosition={'X': 0.0, 'Y': 0.0})

    def move(self, dist, axis):
        self.setPosition(self._position[axis] + dist, axis)

    def setPosition(self, position, axis):
        self._position[axis] = position


class _Nidaq:
    def __init__(self, result=True, error=None):
        self.result, self.error, self.calls = result, error, []

    def setAnalog(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None and kwargs.get('raise_on_error'):
            raise self.error
        return self.result


def _nidaq_laser(nidaq, name='775'):
    info = SimpleNamespace(managerProperties={}, wavelength=775, valueRangeMin=0.0,
                           valueRangeMax=5.0, valueRangeStep=0.01,
                           getAnalogChannel=lambda: 'ao0')
    return NidaqLaserManager(info, name, nidaqManager=nidaq)


# --------------------------------------------------------------- registry
def test_admission_refused_for_foreign_owner_and_allowed_for_the_holder(registry):
    rsv = registry.reserve(['a'], 'run')
    with pytest.raises(ResourceReservedError, match='reserved by run'):
        registry.admit('a')
    ticket = registry.admit('a', rsv.token)
    registry.release_ticket(ticket)
    registry.admit('b')  # other resources unaffected


def test_paused_command_after_admission_delays_the_reservation(registry):
    """The race the review asked for: a command admitted just before a
    reservation must finish before the reservation is granted, and nothing
    new is admitted meanwhile."""
    admitted = threading.Event()
    proceed = threading.Event()

    def command():
        with registry.command('dev', label='slow move'):
            admitted.set()
            proceed.wait(5)

    worker = threading.Thread(target=command)
    worker.start()
    assert admitted.wait(2)
    result = {}

    def reserve():
        result['rsv'] = registry.reserve(['dev'], 'run', deadline_s=5)

    reserver = threading.Thread(target=reserve)
    reserver.start()
    time.sleep(0.05)
    assert 'rsv' not in result                      # still waiting
    with pytest.raises(ResourceReservedError, match='being reserved by run'):
        registry.admit('dev')                       # pending: new commands refused
    proceed.set()
    reserver.join(5)
    worker.join(5)
    assert result['rsv'].resources == frozenset({'dev'})


def test_reservation_refused_when_a_command_outlasts_the_deadline(registry):
    ticket = registry.admit('dev', label='stuck move')
    with pytest.raises(ResourceReservedError, match='stuck move'):
        registry.reserve(['dev'], 'run', deadline_s=0.05)
    registry.release_ticket(ticket)
    registry.admit('dev')  # the refused reservation left no pending mark


def test_tokens_expire_for_good(registry):
    rsv = registry.reserve(['a'], 'run')
    registry.release(rsv.token)
    with pytest.raises(ReservationExpiredError):
        registry.admit('a', rsv.token)
    registry.reserve(['a'], 'next')
    with pytest.raises(ReservationExpiredError):
        registry.admit('a', rsv.token)


def test_all_or_nothing(registry):
    registry.reserve(['b'], 'first')
    with pytest.raises(ResourceReservedError):
        registry.reserve(['a', 'b'], 'second')
    registry.admit('a')  # nothing of the refused reservation remains


def test_non_reentrant_holds_do_not_exempt_later_calls(registry):
    hold = registry.admit(WAVEFORM_OUTPUT, label='scan', reentrant=False)
    rsv_thread = threading.Thread(
        target=lambda: pytest.raises(ResourceReservedError, registry.reserve,
                                     [WAVEFORM_OUTPUT], 'run', deadline_s=0.05))
    rsv_thread.start()
    rsv_thread.join()
    registry.release_ticket(hold)
    registry.reserve([WAVEFORM_OUTPUT], 'run')
    with pytest.raises(ResourceReservedError):
        registry.admit(WAVEFORM_OUTPUT)   # same thread as the old hold: no exemption


def test_end_all_reservations(registry):
    rsv = registry.reserve(['a', 'b'], 'script')
    assert [r.token for r in registry.end_all_reservations()] == [rsv.token]
    registry.admit('a')


# ----------------------------------------------------------- manager guards
def test_rotator_commands_are_admitted_at_the_manager(registry):
    rotator = FakeRotator()
    rsv = registry.reserve([rotator_key('hwp')], 'run')
    with pytest.raises(ResourceReservedError):
        rotator.move_abs(10.0)
    rotator.move_abs(10.0, owner=rsv.token)
    rotator.move_rel(5.0, owner=rsv.token)   # nested move_abs shares the ticket
    assert rotator.moves == [10.0, 15.0]


def test_workflow_facade_is_refused_too(registry):
    from imswitch.imcontrol.model.workflows.facade import RotatorFacade

    rotator = FakeRotator()
    registry.reserve([rotator_key('hwp')], 'run')
    with pytest.raises(ResourceReservedError):
        RotatorFacade(rotator).move_abs(45.0)


def test_sibling_axis_of_a_reserved_controller_is_refused(registry):
    stage = TwoAxisStage()
    control = PositionerAxisControl(stage, 'X', allow_test_class=True)
    rsv = registry.reserve([control.resource], 'run')
    assert control.resource == positioner_key('stage')
    with pytest.raises(ResourceReservedError):
        stage.setPosition(1.0, 'Y')
    assert control.apply(2.0, rsv.token).ok
    assert stage.position['X'] == 2.0


def test_laser_value_enable_and_raw_drive_are_guarded(registry):
    laser = _nidaq_laser(_Nidaq())
    rsv = registry.reserve([laser_key('775')], 'run')
    for call in (lambda: laser.setValue(1.0), lambda: laser.setEnabled(True),
                 lambda: laser.applyRawDrive(1.0)):
        with pytest.raises(ResourceReservedError):
            call()
    assert laser.applyRawDrive(1.5, owner=rsv.token) == 1.5


def test_mock_positioner_is_audited_and_its_nested_set_position_works(registry):
    info = SimpleNamespace(axes=['Z'], forPositioning=True, forScanning=False,
                           resetOnClose=False, joystick=False, liveUpdate=False)
    stage = MockPositionerManager(info, 'mockZ')
    control = PositionerAxisControl(stage, 'Z')
    rsv = registry.reserve([control.resource], 'run')
    stage.move(3.0, 'Z', owner=rsv.token)   # move → setPosition, one ticket
    assert stage.position['Z'] == 3.0


# ------------------------------------------------------------ raw drive
def test_nidaq_raw_drive_failure_is_observable():
    quiet = _nidaq_laser(_Nidaq(result=False))
    with pytest.raises(RawDriveError, match='analog write failed'):
        quiet.applyRawDrive(1.0)
    loud = _nidaq_laser(_Nidaq(error=RuntimeError('DAQ gone')))
    with pytest.raises(RawDriveError, match='DAQ gone'):
        loud.applyRawDrive(1.0)
    with pytest.raises(RawDriveError, match='outside'):
        _nidaq_laser(_Nidaq()).applyRawDrive(7.0)


def test_raw_drive_control_reports_failure_as_a_result(registry):
    laser = _nidaq_laser(_Nidaq(result=False))
    control = LaserRawDriveControl(laser)
    result = ControlExecutor().apply(control, 1.0, deadline_s=2.0)
    assert not result.ok
    assert 'analog write failed' in result.cause
    ok_laser = _nidaq_laser(_Nidaq(), name='488')
    nidaq_calls = ok_laser._nidaqManager.calls
    result = ControlExecutor().apply(LaserRawDriveControl(ok_laser), 2.0, deadline_s=2.0)
    assert result.ok and result.acknowledged == 2.0
    assert nidaq_calls[-1]['raise_on_error'] is True


def test_raw_drive_bypasses_a_loaded_lut():
    laser = _nidaq_laser(_Nidaq())
    laser._lut = lambda value: 99.0       # what setValue would send
    laser.applyRawDrive(1.25)
    assert laser._nidaqManager.calls[-1]['voltage'] == 1.25


def test_aa_aotf_raw_drive_refuses_mock_mode():
    from imswitch.imcontrol._test.unit.test_aa_aotf_laser_manager import _build

    manager, rs232 = _build()
    assert manager.applyRawDrive(512) == 512.0
    manager._controllerAnswered = False        # as after a failed startup exchange
    with pytest.raises(RawDriveError, match='mock mode'):
        manager.applyRawDrive(100)


# --------------------------------------------------------------- audits
def test_unaudited_and_simulated_devices_are_refused():
    with pytest.raises(NotAuditedError, match='not audited'):
        RotatorManagerControl(FakeRotator())
    kinesis = KinesisRotatorManager(
        SimpleNamespace(managerProperties={'snr': '55000000'}), 'k10')
    assert kinesis.isSimulated   # no hardware here: it fell back to a mock
    with pytest.raises(NotAuditedError, match='simulated'):
        RotatorManagerControl(kinesis)


# -------------------------------------------------- stuck move / quarantine
def test_stuck_move_keeps_the_device_held_until_it_returns(registry):
    rotator = FakeRotator()
    rotator.gate = threading.Event()
    control = RotatorManagerControl(rotator, allow_test_class=True)
    executor = ControlExecutor()
    rsv = registry.reserve([control.resource], 'run')
    result = executor.apply(control, 30.0, deadline_s=0.1, token=rsv.token)
    assert not result.ok and 'did not finish' in result.cause
    assert executor.is_quarantined(control.resource)
    registry.release(rsv.token)
    # The move is still running: nobody may reserve or command the device.
    with pytest.raises(ResourceReservedError, match='still running'):
        registry.reserve([control.resource], 'next', deadline_s=0.05)
    rotator.gate.set()
    assert executor.wait_released(control.resource, 2.0)
    registry.reserve([control.resource], 'next', deadline_s=1.0)


# --------------------------------------------------------- waveform bridge
def test_waveform_scan_and_measurement_run_exclude_each_other(registry):
    from imswitch.imcontrol._test.unit.test_scan_execution_coordinator import _setup
    from imswitch.imcontrol.model.managers.NidaqManager import ScanBusyError

    coordinator, _manager, _nidaq = _setup()
    rsv = registry.reserve([WAVEFORM_OUTPUT], 'measurement run')
    with pytest.raises(ScanBusyError, match='measurement run'):
        coordinator.reserveRun(owner='scan widget')
    registry.release(rsv.token)

    run = coordinator.reserveRun(owner='scan widget')
    with pytest.raises(ResourceReservedError, match='scan run'):
        registry.reserve([WAVEFORM_OUTPUT], 'measurement run', deadline_s=0.05)
    coordinator.releaseRun(run)
    registry.reserve([WAVEFORM_OUTPUT], 'measurement run', deadline_s=0.5)


def test_bare_scan_iteration_holds_the_outputs_until_it_completes(registry):
    from imswitch.imcontrol._test.unit.test_scan_execution_coordinator import _setup

    coordinator, _manager, _nidaq = _setup()
    token = coordinator.arm({}, {})          # no owner, no run
    assert registry.in_flight(WAVEFORM_OUTPUT)
    coordinator.resolve(token, timeoutS=None)
    assert not registry.in_flight(WAVEFORM_OUTPUT)


# ------------------------------------------------------------ script API
def _controller():
    from imswitch.imcontrol.controller.controllers.ReservationController import (
        ReservationController,
    )
    controller = ReservationController.__new__(ReservationController)
    controller._ReservationController__logger = SimpleNamespace(info=lambda *a: None)
    return controller


def test_script_handle_is_owner_bound_and_dies_with_its_reservation(registry, monkeypatch):
    import imswitch.imcontrol.controller.controllers.ReservationController as module

    rotator = FakeRotator()
    monkeypatch.setattr(module, 'RotatorManagerControl',
                        lambda m: RotatorManagerControl(m, allow_test_class=True))
    controller = _controller()
    controller._master = SimpleNamespace(rotatorsManager={'hwp': rotator})
    with controller.reserve(rotators=['hwp'], owner='script') as handle:
        assert handle.rotator('hwp').apply(12.0, deadline_s=2).ok
        with pytest.raises(ResourceReservedError):
            rotator.move_abs(1.0)          # the GUI path
        with pytest.raises(ResourceReservedError):
            controller.reserve(rotators=['hwp'], owner='other script', deadline_s=0.05)
        assert controller.getReservations()[0]['owner'] == 'script'
    with pytest.raises(ReservationExpiredError):
        handle.rotator('hwp').apply(3.0)
    rotator.move_abs(1.0)                  # free again
    assert rotator.moves == [12.0, 1.0]


def test_reserve_refuses_unaudited_devices_before_reserving(registry):
    controller = _controller()
    controller._master = SimpleNamespace(rotatorsManager={'hwp': FakeRotator()})
    with pytest.raises(NotAuditedError):
        controller.reserve(rotators=['hwp'])
    assert registry.reservations() == []


# --------------------------------------------------------------- shutdown
def test_busy_backends_make_finalization_fail_closed():
    state = ShutdownState()
    assert state.hardwareFinalizationAllowed()
    state.recordBusyBackends(['rotator:hwp'], 'hardware commands still running: rotator:hwp')
    assert not state.hardwareFinalizationAllowed()
    assert 'rotator:hwp' in state.reasons[0]


def test_instrument_settings_actions_and_windows_are_admitted(registry):
    from imswitch.imcontrol.model.measurement import ActionSpec, InstrumentSession
    from imswitch.imcontrol.model.measurement.mocks import MockPAXDriver, MockRotatorControl

    driver = MockPAXDriver(MockRotatorControl('a'), MockRotatorControl('b'))
    driver.actions_spec = (ActionSpec('zero', 'Zero', requires_dark=True),)
    driver.run_action = lambda name, **kw: None
    session = InstrumentSession('pax1', driver)
    session.connect()
    rsv = registry.reserve([session.resource], 'run')
    with pytest.raises(ResourceReservedError):
        session.set_setting('wavelength_nm', 532)
    with pytest.raises(ResourceReservedError):
        session.run_action('zero', confirm_dark=True)
    with pytest.raises(ResourceReservedError):
        session.open_window()
    assert session.set_setting('wavelength_nm', 532, owner=rsv.token) == 532.0
    session.run_action('zero', confirm_dark=True, owner=rsv.token)
    with pytest.raises(PermissionError, match='beam to be blocked'):
        session.run_action('zero', owner=rsv.token)


# ---------------------------------------------------------- Standa rotators
def _standa(monkeypatch, motor):
    from imswitch.imcontrol.model.managers.rotators.StandaRotatorManager import (
        StandaRotatorManager,
    )

    monkeypatch.setattr(StandaRotatorManager, '_getMotorObj', lambda self, *a: motor)
    info = SimpleNamespace(managerProperties={'motorListIndex': 0, 'ximcLibLocation': 'x',
                                              'stepsPerTurn': 200, 'microstepsPerStep': 256})
    return StandaRotatorManager(info, 'hwp')


class _StandaMotor:
    """The StandaMotor surface the manager uses (the real class needs libximc)."""

    def __init__(self, *, emulated=False, imported=True):
        self.emulated, self._imported, self.pos = emulated, imported, 0.0

    def get_pos(self):
        return self.pos

    def moveabs(self, deg):
        self.pos = float(deg)


def test_standa_is_audited_and_reads_back_fresh(monkeypatch, registry):
    from imswitch.imcontrol.model.measurement.adapters import RotatorManagerControl
    from imswitch.imcontrol.model.measurement.controls import READBACK_FRESH

    motor = _StandaMotor()
    manager = _standa(monkeypatch, motor)
    assert manager.isSimulated is False
    control = RotatorManagerControl(manager)
    assert control.capabilities.readback == READBACK_FRESH
    result = control.apply(30.0)
    assert result.ok and result.measured == 30.0
    motor.pos = 31.0                                    # moved by hand
    assert control.read_position() == 31.0


@pytest.mark.parametrize('motor', [_StandaMotor(emulated=True), _StandaMotor(imported=False)],
                         ids=['libximc virtual controller', 'libximc not loaded'])
def test_standa_without_a_controller_is_simulated_and_refused(monkeypatch, motor):
    from imswitch.imcontrol.model.measurement.adapters import (
        NotAuditedError,
        RotatorManagerControl,
    )

    manager = _standa(monkeypatch, motor)
    assert manager.isSimulated is True
    with pytest.raises(NotAuditedError, match='simulated'):
        RotatorManagerControl(manager)
