"""Reservations for scripts: exclusive, owner-bound control of devices.

``api.imcontrol.reserve(...)`` reserves audited devices for a script and
returns a handle whose methods carry the reservation token explicitly
(``docs/design/plans/transient-instruments-step-scans.md`` §7.3). While the
handle is held, nobody else — the GUI, the workflow facade, another script,
a waveform scan — can command those devices. When it is released the token
expires, and a saved handle can never command anything again::

    with api.imcontrol.reserve(rotators=['hwp', 'qwp']) as r:
        result = r.rotator('hwp').apply(30.0, deadline_s=20)
        if not result.ok:
            print(result.cause)

Commands run on the calling (script) thread through the process-wide control
executor, with a deadline. A command whose deadline passes keeps its device
quarantined until the hardware call returns.
"""
from __future__ import annotations

from typing import Dict, Iterable, Optional, Sequence, Tuple

from imswitch.imcommon.model import APIExport, initLogger
from imswitch.imcommon.model.measurement_run import ControlResult
from imswitch.imcontrol.model.measurement.adapters import (
    LaserRawDriveControl,
    PositionerAxisControl,
    RotatorManagerControl,
)
from imswitch.imcontrol.model.measurement.controls import (
    ControlExecutor,
    RunControl,
    get_control_executor,
)
from imswitch.imcontrol.model.resources import (
    WAVEFORM_OUTPUT,
    Reservation,
    ReservationExpiredError,
    ResourceRegistry,
    get_resource_registry,
    laser_key,
)
from ..basecontrollers import ImConWidgetController


class BoundControl:
    """One reserved control; every call passes the reservation token."""

    def __init__(self, handle: 'ReservationHandle', control: RunControl) -> None:
        self._handle = handle
        self.control = control
        self.name = control.name

    @property
    def capabilities(self):
        return self.control.capabilities

    def apply(self, value: float, deadline_s: float = 30.0) -> ControlResult:
        """Set the control; returns what was requested / acknowledged / measured."""
        token = self._handle._require_active()
        return self._handle._executor.apply(self.control, value, deadline_s, token)

    def read_position(self, deadline_s: float = 5.0) -> Optional[float]:
        self._handle._require_active()
        return self._handle._executor.read_position(self.control, deadline_s)


class BoundLaser(BoundControl):
    """A reserved laser: raw drive (bypassing any LUT) and emission on/off."""

    def set_enabled(self, enabled: bool) -> None:
        token = self._handle._require_active()
        self.control.manager.setEnabled(bool(enabled), owner=token)


class BoundInstrument:
    """One reserved instrument: reads, settings and actions carry the
    reservation token, so nobody else can change it between them."""

    def __init__(self, handle: 'ReservationHandle', session) -> None:
        self._handle = handle
        self.session = session
        self.name = session.name

    @property
    def connected(self) -> bool:
        return self.session.connected and not self.session.faulted

    @property
    def identity(self):
        return self.session.identity

    @property
    def quantities(self):
        return self.session.quantities

    def settings(self) -> dict:
        return self.session.settings()

    def read(self, n: int = 1, deadline_s: float = 10.0, *,
             allow_unverified: bool = False):
        """``n`` samples acquired after this call: a ``WindowResult`` with
        ``samples`` (each has ``values``), ``complete`` and ``cause``. With
        unverified timing (until the instrument's rig check) pass
        ``allow_unverified=True``."""
        token = self._handle._require_active()
        boundary = self.session.open_window(allow_unverified=allow_unverified, owner=token)
        return self.session.sample_window(boundary, int(n), float(deadline_s), owner=token)

    def set(self, name: str, value):
        """Apply a setting; returns the value the instrument applied."""
        return self.session.set_setting(name, value, owner=self._handle._require_active())

    def action(self, name: str, *, confirm_dark: bool = False) -> None:
        """Run an action (e.g. ``zero``; actions that need the beam blocked
        require ``confirm_dark=True``)."""
        self.session.run_action(name, confirm_dark=confirm_dark,
                                owner=self._handle._require_active())


class ReservationHandle:
    """Owner-bound access to the devices of one reservation."""

    def __init__(self, reservation: Reservation, registry: ResourceRegistry,
                 executor: ControlExecutor, controls: Dict[Tuple[str, str], RunControl]) -> None:
        self._reservation = reservation
        self._registry = registry
        self._executor = executor
        self._controls = controls

    # ------------------------------------------------------------- access
    @property
    def token(self) -> str:
        return self._reservation.token

    @property
    def owner(self) -> str:
        return self._reservation.owner

    @property
    def resources(self) -> Tuple[str, ...]:
        return tuple(sorted(self._reservation.resources))

    @property
    def active(self) -> bool:
        return self._registry.is_active(self.token)

    def rotator(self, name: str) -> BoundControl:
        return BoundControl(self, self._get('rotator', name))

    def laser(self, name: str) -> BoundLaser:
        return BoundLaser(self, self._get('laser', name))

    def instrument(self, name: str) -> BoundInstrument:
        return BoundInstrument(self, self._get('instrument', name))

    def positioner(self, name: str, axis: str) -> BoundControl:
        return BoundControl(self, self._get('positioner', f'{name}.{axis}'))

    def control(self, name: str) -> RunControl:
        """The underlying run control (e.g. to hand to a measurement runner)."""
        for (kind, key), control in self._controls.items():
            if kind == 'instrument':
                continue
            if key == name or control.name == name:
                return control
        raise KeyError(f'{name!r} is not part of this reservation')

    # ---------------------------------------------------------- lifecycle
    def release(self) -> None:
        """End the reservation; the token expires and this handle is dead."""
        self._registry.release(self.token)

    def __enter__(self) -> 'ReservationHandle':
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def _require_active(self) -> str:
        if not self._registry.is_active(self.token):
            raise ReservationExpiredError(
                f'the reservation of {", ".join(self.resources)} has ended')
        return self.token

    def _get(self, kind: str, name: str) -> RunControl:
        try:
            return self._controls[(kind, name)]
        except KeyError:
            raise KeyError(f'{kind} {name!r} is not part of this reservation') from None

    def __repr__(self) -> str:
        state = 'active' if self.active else 'ended'
        return f'<Reservation {self.token} by {self.owner!r} ({state}): {", ".join(self.resources)}>'


class ReservationController(ImConWidgetController):
    """API-only controller (no widget) exporting device reservations."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__logger = initLogger(self, tryInheritParent=True)

    @APIExport()
    def reserve(
        self,
        rotators: Iterable[str] = (),
        lasers: Iterable[str] = (),
        positioners: Iterable[Sequence[str]] = (),
        instruments: Iterable[str] = (),
        *,
        waveform_outputs: bool = True,
        owner: str = 'script',
        deadline_s: float = 5.0,
    ) -> ReservationHandle:
        """ Reserve audited devices for exclusive, owner-bound use.

        ``positioners`` is a list of ``(positioner, axis)`` pairs;
        ``instruments`` names entries of the setup's ``instruments`` section
        (``handle.instrument(name).read(...)``). With
        ``waveform_outputs`` (default) no waveform scan can start while the
        reservation is held. Waits up to ``deadline_s`` for commands already
        running on those devices; refuses if one is still running, if another
        owner holds a device, or if a device is not audited for exclusive
        control. """
        controls: Dict[Tuple[str, str], RunControl] = {}
        for name in rotators:
            controls[('rotator', name)] = RotatorManagerControl(self._master.rotatorsManager[name])
        for name in lasers:
            controls[('laser', name)] = LaserRawDriveControl(self._master.lasersManager[name])
        for name, axis in positioners:
            controls[('positioner', f'{name}.{axis}')] = PositionerAxisControl(
                self._master.positionersManager[name], axis)
        for name in instruments:
            controls[('instrument', name)] = self._master.instrumentsManager[name].session
        keys = {c.resource for c in controls.values()}
        if waveform_outputs:
            keys.add(WAVEFORM_OUTPUT)
        if not keys:
            raise ValueError('reserve() needs at least one device')
        registry = get_resource_registry()
        reservation = registry.reserve(keys, owner, deadline_s=deadline_s)
        self.__logger.info(f'Reserved {", ".join(sorted(keys))} for {owner}')
        return ReservationHandle(reservation, registry, get_control_executor(), controls)

    @APIExport()
    def getReservations(self) -> list:
        """ Active reservations: owner and reserved resources. """
        return [
            {'owner': r.owner, 'token': r.token, 'resources': sorted(r.resources)}
            for r in get_resource_registry().reservations()
        ]
