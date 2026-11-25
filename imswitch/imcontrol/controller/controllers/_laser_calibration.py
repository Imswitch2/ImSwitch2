"""The Laser panel's side of power calibrations (mixed into LaserController).

Kept in its own module so the laser controller -- touched by several open
branches -- changes in a few hooks only. Decisions documented in
``docs/devices/lasers.rst`` (Calibrations):

* **Percent iff a calibration is in force.** The control shows 0-100 % only
  while the manager reports a calibration in force; otherwise the manager's
  real unit and range. A referenced calibration that failed is shown on the
  row (indicator + tooltip), not only logged.
* **Swap only while the laser is off.** Changing the calibration changes the
  drive a percentage means, so a swap is refused while the laser emits (or a
  scan runs, or the laser is not connected). After a swap the setpoint goes
  to the bottom of the new scale -- 0 % (the lowest measured power) or the
  raw minimum -- exactly as at startup, and is applied; a Scan-power override
  saved in the old scale is dropped. So a swap can only ever lower the drive
  the laser would get when switched on.
* **Provenance.** ``Laser:<name>:Calibration`` = ``{id, contentHash,
  driveValue}`` (the slot PR #98 reserves) is published after every value
  change and swap, and recorded in component state and presets.
* **Saved values under another calibration** are never reinterpreted: the
  recorded *drive* is applied (converted into the current scale), which
  reproduces the output the value was saved with and cannot exceed it; when
  that drive is outside the current calibration or range, or no drive was
  recorded, the value is skipped -- with a warning either way.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from imswitch.imcommon.model import APIExport, RestoreWarning
from imswitch.imcontrol.model.calibrations import (
    SKIP_VALUE,
    APPLY_UNVERIFIED,
    APPLY_AS_SAVED,
    CalibrationError,
    LaserPowerTable,
    decide_saved_value,
    laser_value_scale,
    power_table_of,
    raw_units_of,
    resolution_of,
)
from imswitch.imcontrol.model.calibrations.display import (
    laser_calibration_tooltip,
)
from imswitch.imcontrol.model.devices.status import device_usable, not_connected_reason

#: ``Laser:<name>:Calibration`` -- the key PR #98 reserves.
CALIBRATION_ATTR = 'Calibration'
_LASER = 'Laser'


class CalibrationSwapRefused(RuntimeError):
    """The calibration of a laser cannot be changed now (it is on, a scan
    runs, it is not connected, ...). Nothing was changed."""


#: Kept under their earlier names for the controllers that import them.
powerTableOf = power_table_of
resolutionOf = resolution_of


class LaserCalibrationMixin:
    """Requires LaserController's ``_master``, ``_widget``, ``_logger``,
    ``setSharedAttr``, ``_setLaserValue``, ``_livePowerValue`` and
    ``_laserDisplayMeta``."""

    # -- scale ---------------------------------------------------------------

    @staticmethod
    def _laserScale(manager) -> Tuple[Any, Any, str, int, Optional[float]]:
        """``(min, max, units, decimals, step)`` the control shows: percent
        while a calibration is in force, the manager's raw scale otherwise."""
        return laser_value_scale(manager)

    # -- provenance ------------------------------------------------------------

    def _laserCalibrationIdentity(self, laserName) -> Optional[Dict[str, Any]]:
        """``{'id', 'contentHash', 'driveValue'}`` of the calibration behind the
        laser's Live value, or None on raw units (PR #98's recording slot)."""
        manager = self._master.lasersManager[laserName]
        table = powerTableOf(manager)
        if table is None or manager.isBinary:
            return None
        return table.identity(manager.driveForValue(self._livePowerValue(laserName)))

    @staticmethod
    def _recordsCalibration(manager) -> bool:
        """Whether saved values of this laser carry a calibration identity:
        only for managers that can apply one (the others' values are always
        raw, so there is nothing to reinterpret)."""
        return getattr(type(manager), 'supportsPowerCalibration', False) is True

    def _calibrationIdentityFor(self, laserName, value) -> Optional[Dict[str, Any]]:
        manager = self._master.lasersManager[laserName]
        table = powerTableOf(manager)
        if table is None:
            return None
        return table.identity(manager.driveForValue(value))

    def _publishLaserCalibration(self, laserName) -> None:
        identity = self._laserCalibrationIdentity(laserName)
        published = self.__dict__.setdefault('_calibrationPublished', set())
        if identity is None and laserName not in published:
            return          # never calibrated this session: nothing to clear
        self.setSharedAttr(laserName, CALIBRATION_ATTR, identity)
        if identity is not None:
            published.add(laserName)

    def _afterLaserValueChanged(self, laserName) -> None:
        """Hook after a Live value reached the laser."""
        self._publishLaserCalibration(laserName)
        self._refreshLaserCalibrationDisplay(laserName)

    # -- display ---------------------------------------------------------------

    def laserCalibrationTooltip(self, laserName) -> str:
        manager = self._master.lasersManager[laserName]
        return laser_calibration_tooltip(
            resolutionOf(manager), self._livePowerValue(laserName),
            rawUnits=manager.rawValueUnits,
            rawRange=(manager.valueRangeMin, manager.valueRangeMax))

    def _refreshLaserCalibrationDisplay(self, laserName) -> None:
        manager = self._master.lasersManager[laserName]
        if manager.isBinary:
            return
        setInfo = getattr(type(self._widget), 'setLaserCalibrationInfo', None)
        if not callable(setInfo):
            return
        resolution = resolutionOf(manager)
        setInfo = self._widget.setLaserCalibrationInfo
        setInfo(laserName, self.laserCalibrationTooltip(laserName), resolution.state)

    # -- swap ------------------------------------------------------------------

    def setLaserCalibrationTable(self, laserName: str, table: Optional[LaserPowerTable], *,
                                 requested: str = '', resolvedPath: str = '') -> str:
        """Put ``table`` (None: raw units) in force for ``laserName``.

        Refused (:class:`CalibrationSwapRefused`) while the laser is on, a
        scan runs, or it is not connected. On success the setpoint is reset
        to the bottom of the new scale and applied; returns a log line."""
        manager = self._master.lasersManager[laserName]
        if manager.isBinary:
            raise CalibrationSwapRefused(f'{laserName} is switched on and off only; it has '
                                         f'no power setpoint to calibrate')
        if not getattr(manager, 'supportsPowerCalibration', False):
            raise CalibrationSwapRefused(
                f'{laserName} runs on raw units only: {type(manager).__name__} does not '
                f'apply power calibrations')
        if not device_usable(manager):
            raise CalibrationSwapRefused(f'{laserName} is not connected '
                                         f'({not_connected_reason(manager)})')
        if self.__dict__.get('is_scanning'):
            raise CalibrationSwapRefused(
                f'{laserName}: a scan is running; change calibrations between scans')
        try:
            self._requireLaserEditsAllowed()
        except Exception as exc:
            raise CalibrationSwapRefused(f'{laserName}: {exc}') from exc
        sharedAttrs = getattr(self._commChannel, 'sharedAttrs', {})
        try:
            appliedOn = bool(sharedAttrs[(_LASER, laserName, 'Enabled')])
        except (KeyError, TypeError):
            appliedOn = False
        # Either view of "on" refuses: the panel's toggle, or the state the
        # controller last applied (what getLaserActive reports).
        if self._widget.isLaserActive(laserName) or appliedOn:
            raise CalibrationSwapRefused(
                f'{laserName} is on. Switch it off first: a new calibration changes the '
                f'drive a percentage means.')
        before = powerTableOf(manager)
        try:
            manager.setPowerCalibration(table, requested=requested, resolvedPath=resolvedPath)
        except CalibrationError as exc:
            raise CalibrationSwapRefused(str(exc)) from exc
        except Exception as exc:  # e.g. refused by a reservation
            raise CalibrationSwapRefused(f'{laserName}: {exc}') from exc
        rangeMin, rangeMax, units, decimals, step = self._laserScale(manager)
        self._laserDisplayMeta[laserName] = (units, decimals)
        setScale = getattr(self._widget, 'setLaserScale', None)
        if callable(setScale):
            setScale(laserName, units, decimals, (rangeMin, rangeMax), step)
        # What the value means changed: publish the new units and range next
        # to it (replay reads them) before the value itself.
        publishContext = getattr(self, '_publishLaserValueContext', None)
        if callable(publishContext):
            publishContext(laserName, manager)
        dropped = getattr(self, '_scanPowerValues', {}).pop(laserName, None)
        self._setLaserValue(laserName, rangeMin)
        self._publishLaserCalibration(laserName)
        self._refreshLaserCalibrationDisplay(laserName)
        describe = (lambda t: t.id if t is not None else 'raw units')
        message = (f'Laser {laserName}: calibration {describe(before)} -> {describe(table)}; '
                   f'laser off, setpoint reset to {rangeMin} {units}'
                   + (f'; Scan power {dropped} (old scale) dropped' if dropped is not None
                      else ''))
        self._logger.info(message)
        signal = getattr(self, 'sigLaserCalibrationChanged', None)
        if signal is not None:
            try:
                signal.emit(laserName)
            except (AttributeError, RuntimeError):
                pass
        return message

    # -- saved values ------------------------------------------------------------

    def _calibrationAdjustedValue(self, laserName, value, saved: Dict[str, Any], *,
                                  what: str):
        """``(value to apply or None, warning or None)`` for a value saved in
        a state, setup mode or preset entry ``saved``.

        Same calibration (or same content): the value as saved. Different:
        the recorded drive, converted into the current scale -- never the
        saved percentage reinterpreted; skipped when that is impossible.
        Nothing recorded (older states): the value as saved, unless its
        recorded unit differs from the current one or the laser's referenced
        calibration failed to load -- then skipped, since a percentage would
        be taken as a raw drive."""
        manager = self._master.lasersManager[laserName]
        low, high = getattr(manager, 'valueRangeMin', None), getattr(manager, 'valueRangeMax', None)
        currentUnits = getattr(manager, 'valueUnits', None)
        decision = decide_saved_value(
            value, recorded=saved.get('calibration'), recorded_known='calibration' in saved,
            table=power_table_of(manager), raw_range=(low, high), raw_units=raw_units_of(manager),
            current_units=currentUnits if isinstance(currentUnits, str) else '',
            saved_units=saved.get('valueUnits'),
            calibration_failed=resolution_of(manager).failed)
        if decision.outcome in (APPLY_AS_SAVED, APPLY_UNVERIFIED):
            # An unverified old value applies as before, without a warning
            # (presets and states saved before calibration provenance).
            return value, None
        warning = RestoreWarning(f'{what} for laser "{laserName}": {decision.reason}')
        return (None if decision.outcome == SKIP_VALUE else decision.value), warning

    def _showPresetCalibrationWarnings(self, presetName) -> None:
        warnings = self.__dict__.get('_presetCalibrationWarnings') or []
        self._presetCalibrationWarnings = []
        if not warnings:
            return
        try:
            from imswitch.imcommon.view.guitools import showWarning

            showWarning(self._widget, f'Laser preset "{presetName}"',
                        'Some laser values were saved under a different power calibration:\n\n'
                        + '\n'.join(f'• {w}' for w in warnings), unattendedAfterS=60)
        except Exception as exc:  # noqa: BLE001 - the warnings are logged already
            self._logger.debug(f'Could not show preset calibration warnings: {exc}')

    # -- scripting -------------------------------------------------------------

    @APIExport()
    def getLaserCalibration(self, laserName: str) -> Dict[str, Any]:
        """ What the laser's value means now: ``state`` (``'calibrated'``,
        ``'raw'`` or ``'failed'``), ``units``, the calibration ``id``,
        ``contentHash``, ``measuredAt``, ``comment``, ``powerUnit`` and the
        predicted ``power`` at the current setpoint (None without a power
        unit), and ``error`` when a referenced calibration did not load. """
        manager = self._master.lasersManager[laserName]
        resolution = resolutionOf(manager)
        table = resolution.table
        value = None if manager.isBinary else self._livePowerValue(laserName)
        info = {'state': resolution.state, 'units': manager.valueUnits,
                'rawUnits': manager.rawValueUnits, 'requested': resolution.requested,
                'resolvedPath': resolution.resolvedPath, 'error': resolution.error,
                'id': None, 'contentHash': None, 'measuredAt': None, 'comment': None,
                'powerUnit': None, 'power': None, 'driveValue': None}
        if table is not None:
            info.update(id=table.id, contentHash=table.contentHash(),
                        measuredAt=table.record.measuredAt, comment=table.record.comment,
                        powerUnit=table.powerUnit or None,
                        power=table.power_for_percent(value) if value is not None else None,
                        driveValue=manager.driveForValue(value) if value is not None else None)
        return info
