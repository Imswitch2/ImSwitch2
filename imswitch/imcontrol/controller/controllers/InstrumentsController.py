"""Measurement instruments for scripts: list, connect, read, laser power LUT.

API-only (no widget). Instruments are the setup's ``instruments`` section
(power meters, polarimeters); connecting and disconnecting go through the
device lifecycle service, exactly like the Hardware status window, and are
refused while a run or a script holds the instrument::

    api.imcontrol.connectInstrument('pm1')
    print(api.imcontrol.readInstrument('pm1', n=5))

For several reads, settings and actions that must not be interleaved with
anybody else's, reserve the instrument::

    with api.imcontrol.reserve(instruments=['pm1']) as r:
        r.instrument('pm1').set('wavelength_nm', 775)
        window = r.instrument('pm1').read(10, allow_unverified=True)

Design: ``docs/design/plans/transient-instruments-step-scans.md`` §5-7, §10.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from imswitch.imcommon.model import APIExport, dirtools, initLogger
from imswitch.imcontrol.model.devices import HardwareDeviceId
from imswitch.imcontrol.model.measurement.adapters import (
    LaserRawDriveControl,
    ManagerLaserState,
)
from imswitch.imcontrol.model.measurement.laser_lut import (
    LaserLutReport,
    LaserLutSettings,
    run_laser_lut,
)
from ..basecontrollers import ImConWidgetController

#: Shared attributes the Laser panel keeps for each laser (LaserController).
_LASER_ATTR_CATEGORY, _LASER_VALUE, _LASER_ENABLED = 'Laser', 'Value', 'Enabled'


def default_runs_folder() -> Path:
    """Where measurement runs go unless a folder is given."""
    return Path(dirtools.UserFileDirs.Root) / 'measurement_runs'


class InstrumentsController(ImConWidgetController):
    """API-only controller (no widget) for the setup's instruments."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__logger = initLogger(self, tryInheritParent=True)

    # ------------------------------------------------------------- helpers
    def _instruments(self):
        manager = getattr(self._master, 'instrumentsManager', None)
        return dict(manager) if manager is not None else {}

    def _instrument(self, name: str):
        instruments = self._instruments()
        if name not in instruments:
            known = ', '.join(sorted(instruments)) or 'none in this setup'
            raise KeyError(f'no instrument {name!r} (instruments: {known})')
        return instruments[name]

    def _lifecycle(self, name: str, action: str) -> dict:
        self._instrument(name)
        service = getattr(self._master, 'deviceLifecycleService', None)
        if service is None:
            raise RuntimeError('the device lifecycle service is not available')
        result = getattr(service, action)(HardwareDeviceId('instrument', f'instrument:{name}'))
        if not result.success:
            raise RuntimeError(f'{name}: {result.summary}'
                               + (f': {result.details}' if result.details else ''))
        return {'name': name, 'action': action, 'summary': result.summary}

    # ----------------------------------------------------------------- API
    @APIExport()
    def getInstruments(self) -> List[dict]:
        """ Instruments of this setup: name, whether connected, identity,
        quantities (name and unit), settings and whether their timing is
        verified on hardware. """
        rows = []
        for name, manager in sorted(self._instruments().items()):
            session = manager.session
            identity = session.identity
            rows.append({
                'name': name,
                'manager': type(manager).__name__,
                'connected': manager.connected,
                'state': manager.connectionState.value,
                'summary': manager.connectionStatusSummary,
                'identity': identity.as_dict() if identity is not None and manager.connected else None,
                'quantities': [{'name': q.name, 'unit': q.unit, 'quantity': q.quantity}
                               for q in session.quantities],
                'settings': session.settings() if manager.connected else {},
                'timing': session.verification.value if manager.connected else None,
            })
        return rows

    @APIExport()
    def connectInstrument(self, name: str) -> dict:
        """ Connect an instrument (raises if it does not connect, or if a run
        or script holds it). """
        return self._lifecycle(name, 'connect')

    @APIExport()
    def disconnectInstrument(self, name: str) -> dict:
        """ Disconnect an instrument so it can be unplugged. """
        return self._lifecycle(name, 'disconnect')

    @APIExport()
    def readInstrument(self, name: str, n: int = 1, deadline_s: float = 10.0) -> List[dict]:
        """ ``n`` fresh readings of a connected instrument, as dicts of
        quantity values -- a quick look, not a measurement: timing need not
        be verified, and invalid readings are skipped. Refused while a run or
        a script holds the instrument (read through its reservation then). """
        session = self._instrument(name).session
        window = session.sample_window(session.open_window(allow_unverified=True),
                                       int(n), float(deadline_s))
        if not window.samples and window.cause is not None:
            raise RuntimeError(f'{name}: no reading ({window.cause.value}: {window.detail})')
        return [dict(sample.values) for sample in window.samples]

    @APIExport()
    def measureLaserPowerLut(
        self,
        laser: str,
        meter: str,
        drive_values: Sequence[float],
        confirm_dark: Optional[Callable[[], bool]] = None,
        folder: Optional[str] = None,
        samples_per_point: int = 5,
        settle_s: float = 0.2,
        allow_unverified_timing: bool = False,
        notes: str = '',
        progress: Optional[Callable] = None,
    ) -> LaserLutReport:
        """ Measure a laser's power against its raw drive and write the
        ``calibCsvPath`` LUT if the measurement passes acceptance.

        Reserves the laser, the meter and the waveform outputs for the whole
        run: records the laser's value and emission, sets the meter to the
        laser's wavelength, switches emission off and calls ``confirm_dark()``
        -- return True only once the beam is really blocked at the sensor --,
        zeroes the meter, sweeps ``drive_values`` (raw volts / AOTF amplitude,
        bypassing any loaded LUT) and restores the laser on every exit path.

        Returns a report: ``lut_file`` (None if refused, with the reasons in
        ``refused``), ``run`` (the run file and its outcomes) and ``result``.
        Runs go to ``folder`` (default ``ImSwitchConfig/measurement_runs``).
        With ``allow_unverified_timing`` (until the meter's timing is checked
        on hardware) the run is kept but no LUT is written. """
        if confirm_dark is None:
            raise ValueError(
                'confirm_dark is required: a function that returns True once the beam '
                'is blocked at the sensor (it is called with emission off)')
        laserManager = self._master.lasersManager[laser]
        meterSession = self._instrument(meter).session
        sharedAttrs = self._commChannel.sharedAttrs

        def sharedLaserAttr(attr):
            def get(name):
                key = (_LASER_ATTR_CATEGORY, name, attr)
                if key not in sharedAttrs:
                    raise RuntimeError(
                        f'the state of laser {name!r} is unknown: the setup has no Laser '
                        f'panel, so it cannot be restored after the run')
                return sharedAttrs[key]
            return get

        state = ManagerLaserState(laserManager, get_value=sharedLaserAttr(_LASER_VALUE),
                                  get_enabled=sharedLaserAttr(_LASER_ENABLED))
        state.read_state()          # refuse now, before reserving anything
        runFolder = Path(folder) if folder else default_runs_folder()
        os.makedirs(runFolder, exist_ok=True)
        report = run_laser_lut(
            laser_control=LaserRawDriveControl(laserManager),
            laser=state,
            meter=meterSession,
            folder=runFolder,
            settings=LaserLutSettings(
                drive_values=list(drive_values),
                samples_per_point=int(samples_per_point),
                settle_s=float(settle_s),
                allow_unverified_timing=bool(allow_unverified_timing),
                notes=notes,
            ),
            confirm_dark=confirm_dark,
            progress=progress,
        )
        if report.lut_file is not None:
            self.__logger.info(f'Laser power LUT for {laser}: {report.lut_file}')
        else:
            self.__logger.warning(f'No laser power LUT for {laser}: {"; ".join(report.refused)}')
        return report
