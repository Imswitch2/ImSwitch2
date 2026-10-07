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
}
AUDITED_RAW_DRIVE_LASERS = {
    'NidaqLaserManager': 'V',
    'AAAOTFLaserManager': 'amplitude',
}
#: Positioners audited for runs (mock only so far).
AUDITED_POSITIONERS = {'MockPositionerManager'}


class NotAuditedError(ValueError):
    """The manager is not audited for measurement runs."""


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
                 tolerance: Optional[float] = None, allow_test_class: bool = False) -> None:
        if allow_test_class:
            spec = dict(can_stop=hasattr(manager, 'stop'), settle_s=0.0, tolerance=0.05)
        else:
            spec = dict(AUDITED_ROTATORS[_audited_name(manager, AUDITED_ROTATORS, 'rotator')])
        if getattr(manager, 'isSimulated', False):
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

    def apply(self, value: float, token: Optional[str] = None) -> ControlResult:
        self.manager.move_abs(float(value), owner=token)
        measured = self.manager.readPosition()
        return ControlResult(self.name, requested=float(value), measured=measured, ok=True)

    def stop(self) -> None:
        stop = getattr(self.manager, 'stop', None)
        if callable(stop):
            stop()

    def read_position(self) -> Optional[float]:
        return self.manager.readPosition()


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
        self.manager = manager
        self.name = manager.name
        self.resource = laser_key(manager.name)
        self.capabilities = ControlCapabilities(
            unit=unit, can_stop=False, readback=READBACK_NONE,
            acknowledges=True, settle_s=settle_s, tolerance=None,
        )

    def apply(self, value: float, token: Optional[str] = None) -> ControlResult:
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

    def set_enabled(self, enabled: bool, token: Optional[str] = None) -> None:
        self.manager.setEnabled(bool(enabled), owner=token)

    def restore(self, value: float, enabled: bool, token: Optional[str] = None) -> None:
        self.manager.setValue(value, owner=token)
        self.manager.setEnabled(bool(enabled), owner=token)
