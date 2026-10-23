"""Audited run controls over real ImControl managers.

Only managers listed here can be set by a measurement run. Each adapter
states what its device really supports: stop, fresh readback, whether a
returned command proves the output changed
(``docs/design/plans/transient-instruments-step-scans.md`` §7.4). Commands go
through the manager boundary with the run's reservation token
(``owner=token``), so the resource registry admits them and refuses everybody
else's while the run holds the device.

Audited so far:

- rotators: ``KinesisRotatorManager`` (K10CR1), ``ElliptecRotatorManager``
  (ELL14). Neither interface can stop a move: a stuck move is bounded by the
  executor's deadline and keeps the device quarantined until it returns.
  Both read their position back from the hardware after every move.
- lasers, raw drive: ``NidaqLaserManager`` (analog volts) and
  ``AAAOTFLaserManager`` (channel amplitude), via ``applyRawDrive``, which
  bypasses any calibration LUT and raises instead of logging failures.

A manager that fell back to a simulated device because the hardware was
absent (``isSimulated``) is refused: a calibration must not "succeed" on a
simulation without anyone noticing.
"""
from __future__ import annotations

from typing import Optional

from imswitch.imcommon.model.measurement_run import ControlResult

from ..resources import laser_key, positioner_key, rotator_key
from .controls import (
    READBACK_CACHED,
    READBACK_FRESH,
    READBACK_NONE,
    ControlCapabilities,
    RunControl,
)

AUDITED_ROTATORS = {
    'KinesisRotatorManager': dict(can_stop=False, settle_s=0.05, tolerance=0.05),
    'ElliptecRotatorManager': dict(can_stop=False, settle_s=0.05, tolerance=0.1),
    # move_abs returns once the motor stopped (get_pos waits for stop); the
    # ximc return codes are not checked, so the fresh readback is what proves
    # a move. The soft stop (command_sstp) is not wired as a run stop yet.
    'StandaRotatorManager': dict(can_stop=False, settle_s=0.05, tolerance=0.05),
}
AUDITED_RAW_DRIVE_LASERS = {
    'NidaqLaserManager': 'V',
    'AAAOTFLaserManager': 'amplitude',
}
#: Positioners audited for runs (mock only so far).
AUDITED_POSITIONERS = {'MockPositionerManager'}


class NotAuditedError(ValueError):
    """The manager is not audited for measurement runs."""


class SimulatedDeviceError(RuntimeError):
    """The device is running as a simulation; a command would only pretend."""


def _is_mock(manager) -> bool:
    mode = getattr(manager, 'runtimeMode', None)
    return getattr(mode, 'value', None) == 'mock'


def _audited_name(manager, table, kind: str) -> str:
    # The exact class, not a subclass: a subclass may change behaviour.
    name = type(manager).__name__
    if name not in table:
        raise NotAuditedError(
            f'{kind} {getattr(manager, "name", "?")!r} uses {name}, which is not audited '
            f'for measurement runs (audited: {", ".join(sorted(table))})')
    return name


class RotatorManagerControl(RunControl):
    """A rotation mount as a run control (degrees)."""

    def __init__(self, manager, *, settle_s: Optional[float] = None,
                 tolerance: Optional[float] = None, allow_test_class: bool = False,
                 allow_simulated: bool = False) -> None:
        if allow_test_class:
            spec = dict(can_stop=hasattr(manager, 'stop'), settle_s=0.0, tolerance=0.05)
        else:
            spec = dict(AUDITED_ROTATORS[_audited_name(manager, AUDITED_ROTATORS, 'rotator')])
        #: Simulated devices only on explicit request (a mock setup, a
        #: tutorial); recorded per control in the run file.
        self.allow_simulated = bool(allow_simulated)
        self.simulated = bool(getattr(manager, 'isSimulated', False))
        if self.simulated and not self.allow_simulated:
            raise NotAuditedError(
                f'rotator {manager.name!r} fell back to a simulated device (hardware '
                f'not found); a measurement run cannot use it')
        self.manager = manager
        self.name = manager.name
        self.resource = rotator_key(manager.name)
        has_readback = manager.readPosition() is not None
        self.capabilities = ControlCapabilities(
            unit='deg', can_stop=bool(spec['can_stop']),
            readback=READBACK_FRESH if has_readback else READBACK_NONE,
            acknowledges=False,
            settle_s=spec['settle_s'] if settle_s is None else settle_s,
            tolerance=(spec['tolerance'] if tolerance is None else tolerance)
            if has_readback else None,
        )
        self.zero_reference = {'state': 'unknown'}

    def _require_real(self) -> None:
        # Checked on every command: a bus can fall back to its simulation
        # mid-run (Elliptec after a communication error), and a simulated
        # move "succeeds" without moving anything.
        if self.allow_simulated:
            return
        if getattr(self.manager, 'isSimulated', False):
            raise SimulatedDeviceError(
                f'rotator {self.name!r} is running as a simulation (hardware not '
                f'answering); nothing was moved')

    def apply(self, value: float, token: Optional[str] = None) -> ControlResult:
        self._require_real()
        self.manager.move_abs(float(value), owner=token)
        self._require_real()
        measured = self.manager.readPosition()
        self._require_real()
        return ControlResult(self.name, requested=float(value), measured=measured, ok=True)

    def stop(self) -> None:
        stop = getattr(self.manager, 'stop', None)
        if callable(stop):
            stop()

    def read_position(self) -> Optional[float]:
        self._require_real()
        position = self.manager.readPosition()
        self._require_real()
        return position


class LaserRawDriveControl(RunControl):
    """A laser's raw drive (volts / amplitude), bypassing its calibration LUT."""

    def __init__(self, manager, *, settle_s: float = 0.05, allow_test_class: bool = False) -> None:
        if allow_test_class:
            unit = getattr(manager, 'valueUnits', '')
        else:
            unit = AUDITED_RAW_DRIVE_LASERS[
                _audited_name(manager, AUDITED_RAW_DRIVE_LASERS, 'laser')]
        if not getattr(manager, 'supportsRawDrive', False):
            raise NotAuditedError(f'laser {manager.name!r} has no raw-drive command')
        if _is_mock(manager):
            raise NotAuditedError(
                f'laser {manager.name!r} runs on a simulated backend; a measurement run '
                f'cannot use it')
        self.manager = manager
        self.name = manager.name
        self.resource = laser_key(manager.name)
        self.capabilities = ControlCapabilities(
            unit=unit, can_stop=False, readback=READBACK_NONE,
            acknowledges=True, settle_s=settle_s, tolerance=None,
        )

    def apply(self, value: float, token: Optional[str] = None) -> ControlResult:
        if _is_mock(self.manager):
            raise SimulatedDeviceError(
                f'laser {self.name!r} runs on a simulated backend; nothing was sent')
        applied = self.manager.applyRawDrive(float(value), owner=token)
        return ControlResult(self.name, requested=float(value),
                             acknowledged=float(applied), ok=True)


class PositionerAxisControl(RunControl):
    """One axis of an audited positioner; the controller is the resource."""

    def __init__(self, manager, axis: str, *, settle_s: float = 0.0,
                 allow_test_class: bool = False) -> None:
        if not allow_test_class and type(manager).__name__ not in AUDITED_POSITIONERS:
            raise NotAuditedError(
                f'positioner {manager.name!r} uses {type(manager).__name__}, which is '
                f'not audited for measurement runs')
        if axis not in manager.axes:
            raise ValueError(f'{manager.name} has no axis {axis!r}')
        self.manager = manager
        self.axis = axis
        self.name = f'{manager.name}.{axis}'
        self.resource = positioner_key(manager.name)
        self.capabilities = ControlCapabilities(
            unit='um', can_stop=False, readback=READBACK_CACHED,
            acknowledges=False, settle_s=settle_s, tolerance=None,
        )

    def apply(self, value: float, token: Optional[str] = None) -> ControlResult:
        self.manager.setPosition(float(value), self.axis, owner=token)
        return ControlResult(self.name, requested=float(value), ok=True)

    def read_position(self) -> Optional[float]:
        return float(self.manager.position[self.axis])


class ManagerLaserState:
    """The laser state the power-LUT procedure records and restores, for a
    real laser manager.

    Managers keep no readable value / enabled state; ``LaserController``
    does. ``read_state`` therefore takes the controller's getters
    (``api.imcontrol.getLaserValue`` / ``getLaserActive``). Restoring goes to
    the manager with the reservation token — value first, then the enabled
    state — so it is admitted while the procedure holds the laser.
    """

    def __init__(self, manager, *, get_value, get_enabled) -> None:
        self.manager = manager
        self.name = manager.name
        self.wavelength_nm = float(manager.wavelength)
        self.raw_unit = AUDITED_RAW_DRIVE_LASERS.get(type(manager).__name__, '')
        self.uses_lut = bool(manager.usesCalibrationLookup())
        self._get_value = get_value
        self._get_enabled = get_enabled

    def read_state(self):
        return float(self._get_value(self.name)), bool(self._get_enabled(self.name))

    def set_enabled(self, enabled: bool, token: Optional[str] = None) -> bool:
        """Checked emission switch (raises on failure); ``False`` when the
        laser has no switch -- then only the dark confirmation protects the
        zero."""
        return bool(self.manager.applyEnabled(bool(enabled), owner=token))

    def restore(self, value: float, enabled: bool, token: Optional[str] = None) -> None:
        """Put the laser back, failing loudly but always ending safe.

        Checked commands: a failed step fails the cleanup, never reported as
        done. The emission switch is handled independently of the value:
        a laser that was off is switched off first, so it never emits at a
        sweep value; if the value cannot be restored, emission is switched
        off and stays off -- never re-enabled at whatever the sweep left.
        """
        if not enabled:
            off_error = self._try_off(token)
            try:
                self.manager.applyValue(value, owner=token)
            except Exception as value_error:
                state = ('emission is off' if off_error is None
                         else f'switching emission off failed too: {off_error}')
                raise RuntimeError(
                    f'{self.name}: restoring the value failed ({value_error}); {state}'
                ) from value_error
            if off_error is not None:
                raise off_error
            return
        try:
            self.manager.applyValue(value, owner=token)
        except Exception as value_error:
            off_error = self._try_off(token)
            state = ('emission switched off' if off_error is None
                     else f'switching emission off failed too: {off_error}')
            raise RuntimeError(
                f'{self.name}: restoring the value failed ({value_error}); {state}'
            ) from value_error
        self.manager.applyEnabled(True, owner=token)

    def _try_off(self, token) -> Optional[Exception]:
        try:
            self.manager.applyEnabled(False, owner=token)
        except Exception as exc:
            return exc
        return None
