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

import math
import os
import threading
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from qtpy import QtCore

from imswitch.imcommon.model import APIExport, dirtools, initLogger
from imswitch.imcommon.model.cancellation import checkpoint, currentCancelToken
from imswitch.imcontrol.model.devices import HardwareDeviceId
from imswitch.imcontrol.model.measurement.adapters import (
    LaserRawDriveControl,
    ManagerLaserState,
    RotatorManagerControl,
)
from imswitch.imcontrol.model.measurement.generators import SNAKE, grid
from imswitch.imcontrol.model.measurement.runner import (
    MeasurementRunner,
    RunReport,
    RunSettings,
)
from imswitch.imcontrol.model.measurement.laser_lut import (
    LaserLutReport,
    LaserLutSettings,
    run_laser_lut,
)
from imswitch.imcontrol.model.resources import get_resource_registry
from ..basecontrollers import ImConWidgetController

#: Shared attributes the Laser panel keeps for each laser (LaserController).
_LASER_ATTR_CATEGORY, _LASER_VALUE, _LASER_ENABLED = 'Laser', 'Value', 'Enabled'


def default_runs_folder() -> Path:
    """Where measurement runs go unless a folder is given."""
    return Path(dirtools.UserFileDirs.Root) / 'measurement_runs'


_QUANTITY_LABELS = {'power': 'Power', 'azimuth': 'Azimuth', 'ellipticity': 'Ellipticity',
                    'dop': 'DOP'}


def format_quantity(spec, value) -> str:
    """A live value as a person reads it: power with an SI prefix, angles in
    degrees, ratios with three decimals."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return '—'
    if not math.isfinite(value):
        return '—'
    if spec.unit == 'W':
        for factor, prefix in ((1.0, ''), (1e-3, 'm'), (1e-6, 'µ'), (1e-9, 'n')):
            if abs(value) >= factor or factor == 1e-9:
                return f'{value / factor:.4g} {prefix}W'
    if spec.unit == 'rad':
        return f'{math.degrees(value):+.2f}°'
    if not spec.unit:
        return f'{value:.3f}'
    return f'{value:.4g} {spec.unit}'


def stokes_text(values) -> Optional[str]:
    """Normalised Stokes s1 / s2 / s3 of an azimuth / ellipticity reading."""
    try:
        azimuth, ellipticity = float(values['azimuth']), float(values['ellipticity'])
    except (KeyError, TypeError, ValueError):
        return None
    c = math.cos(2 * ellipticity)
    return (f'{c * math.cos(2 * azimuth):+.3f} / {c * math.sin(2 * azimuth):+.3f} / '
            f'{math.sin(2 * ellipticity):+.3f}')


class InstrumentsController(ImConWidgetController):
    """The setup's instruments: the Instruments panel (live readings) when
    the setup shows it, and the scripting API either way."""

    #: Pause between live reads of one instrument.
    POLL_INTERVAL_S = 0.2

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__logger = initLogger(self, tryInheritParent=True)
        self._live = {}
        self._pollStop = threading.Event()
        self._pollThread = None
        self._sampleListeners = {}
        if self._widget is not None:
            self._setUpPanel()

    # ----------------------------------------------------------- the panel
    def _setUpPanel(self):
        for name, manager in sorted(self._instruments().items()):
            session = manager.session
            quantities = [(q.name, f'{_QUANTITY_LABELS.get(q.name, q.name)}'
                           + (f' ({q.unit})' if q.unit and q.unit not in ('W', 'rad') else ''))
                          for q in session.quantities]
            names = {q.name for q in session.quantities}
            if {'azimuth', 'ellipticity'} <= names:
                quantities.append(('stokes', 's1 / s2 / s3'))
            # Settings with a unit (a wavelength); modes stay with the driver.
            settings = [spec for spec in session.driver.settings_spec if spec.unit]
            self._widget.addInstrument(name, quantities, settings,
                                       list(session.driver.actions_spec))
            self._live[name] = True

            def onSample(instrument, sample, name=name):
                if sample.valid:
                    self._invokeOnControllerThreadIfNeeded(
                        lambda: self._showSample(name, sample.values))
            session.add_sample_listener(onSample)
            self._sampleListeners[name] = onSample

        self._widget.sigConnectRequested.connect(self._connectRequested)
        self._widget.sigLiveToggled.connect(self._liveToggled)
        self._widget.sigSettingRequested.connect(self._settingRequested)
        self._widget.sigActionRequested.connect(self._actionRequested)

        self._stateTimer = QtCore.QTimer()
        self._stateTimer.timeout.connect(self._refreshStates)
        self._stateTimer.start(500)
        self._refreshStates()
        if self._instruments():
            self._pollThread = threading.Thread(target=self._pollLoop,
                                                name='instrument-live-poll', daemon=True)
            self._pollThread.start()

    def _showSample(self, name, values):
        manager = self._instruments().get(name)
        if manager is None:
            return
        specs = {q.name: q for q in manager.session.quantities}
        texts = {key: format_quantity(specs[key], value)
                 for key, value in values.items() if key in specs}
        stokes = stokes_text(values)
        if stokes is not None:
            texts['stokes'] = stokes
        self._widget.setValues(name, texts)

    def _refreshStates(self):
        registry = get_resource_registry()
        for name, manager in self._instruments().items():
            session = manager.session
            holder = registry.holder(session.resource)
            if manager.connected:
                identity = session.identity
                text = (f'{identity.model} {identity.serial}'.strip()
                        if identity is not None else 'Connected')
                if holder is not None:
                    text += f' — in use by {holder.owner}'
                elif session.verification.value != 'verified':
                    text += ' — timing not verified'
            elif manager.connectionState.value == 'error':
                text = f'Error: {manager.connectionStatusSummary or "connection failed"}'
                details = getattr(manager, 'connectionStatusDetails', None)
                if details:
                    text += f' ({details})'
            else:
                text = 'Not connected'
            self._widget.setState(name, text, connected=manager.connected or
                                  manager.runtimeMode.value != 'absent',
                                  usable=manager.connected and holder is None)
            settings = session.settings() if manager.connected else {}
            for setting, value in settings.items():
                if isinstance(value, (int, float)):
                    self._widget.setSettingValue(name, setting, value)

    def _pollLoop(self):
        """Live reads, one at a time, of connected instruments with Live on
        that nobody holds. Samples reach the panel through the sample
        listener -- like the samples of runs and scripts."""
        registry = get_resource_registry()
        while not self._pollStop.wait(self.POLL_INTERVAL_S):
            for name, manager in list(self._instruments().items()):
                if self._pollStop.is_set():
                    return
                session = manager.session
                if (not self._live.get(name) or not manager.connected or session.reading()
                        or registry.holder(session.resource) is not None):
                    continue
                try:
                    session.sample_window(session.open_window(allow_unverified=True), 1, 2.0)
                except Exception as exc:     # reserved meanwhile, disconnected, ...
                    self.__logger.debug(f'live read of {name} skipped: {exc}')

    def _liveToggled(self, name, on):
        self._live[name] = bool(on)

    def _inBackground(self, name, label, work):
        """Run ``work()`` off the GUI thread; report the outcome in the panel."""
        self._widget.setBusy(name, True)
        self._widget.setMessage(name, f'{label}…')

        def run():
            try:
                message = work() or f'{label}: done'
            except Exception as exc:
                message = f'{label} failed: {exc}'
            self._invokeOnControllerThreadIfNeeded(lambda: self._finished(name, message))
        threading.Thread(target=run, name=f'instrument-{label}', daemon=True).start()

    def _finished(self, name, message):
        self._widget.setBusy(name, False)
        self._widget.setMessage(name, message)
        self._refreshStates()

    def _connectRequested(self, name, connect):
        if connect:
            self._inBackground(name, 'Connect', lambda: self._lifecycle(name, 'connect')['summary'])
        else:
            self._inBackground(name, 'Disconnect',
                               lambda: self._lifecycle(name, 'disconnect')['summary'])

    def _settingRequested(self, name, setting, value):
        session = self._instrument(name).session
        self._inBackground(
            name, f'Set {setting}',
            lambda: f'{setting} = {session.set_setting(setting, value):g}')

    def _actionRequested(self, name, action):
        session = self._instrument(name).session
        spec = next(a for a in session.driver.actions_spec if a.name == action)
        # The panel asked for the dark confirmation before emitting.
        self._inBackground(name, spec.label,
                           lambda: session.run_action(action, confirm_dark=True))

    def closeEvent(self):
        self._pollStop.set()
        timer = self.__dict__.get('_stateTimer')
        if timer is not None:
            timer.stop()
        for name, listener in self.__dict__.get('_sampleListeners', {}).items():
            manager = self._instruments().get(name)
            if manager is not None:
                manager.session.remove_sample_listener(listener)
        thread = self.__dict__.get('_pollThread')
        if thread is not None:
            thread.join(3.0)
        super().closeEvent()

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
    def measureRotatorGrid(
        self,
        axes: Sequence[Sequence],
        instruments: Sequence[str],
        samples_per_point: int = 5,
        settle_s: float = 0.2,
        traversal: str = SNAKE,
        folder: Optional[str] = None,
        allow_unverified_timing: bool = False,
        allow_simulated: bool = False,
        return_to_start: bool = True,
        plane_label: str = '',
        notes: str = '',
        progress: Optional[Callable] = None,
    ) -> RunReport:
        """ Measure instruments on a grid of rotator positions.

        ``axes`` lists ``(rotator, [angles in degrees])`` pairs, outermost
        first; ``traversal`` is ``'snake'`` (default: the inner axis reverses
        on every other line, so it never travels back) or ``'raster'``. At
        every point the rotators move, settle (their own settle time plus
        ``settle_s``), and each instrument takes ``samples_per_point``
        samples acquired after the move.

        The rotators, the instruments and the waveform outputs are reserved
        for the whole run; the rotators return to where they started.
        ``progress(event)`` is called after each point (``event.index``,
        ``event.total``, ``event.status``). Stop in ImScripting ends the run
        after the current point; the data so far is kept.

        Returns the run report: ``run_file`` (open it in ImProcess),
        ``acquisition`` / ``cleanup`` outcomes and point counts. Runs go to
        ``folder`` (default ``ImSwitchConfig/measurement_runs``).
        ``allow_unverified_timing``: needed until the instrument's timing is
        checked on hardware. ``allow_simulated``: accept simulated rotators
        (mock setups); recorded in the run file. """
        controls, values = [], []
        for name, angles in axes:
            controls.append(RotatorManagerControl(self._master.rotatorsManager[name],
                                                  allow_simulated=allow_simulated))
            values.append((name, [float(a) for a in angles]))
        sessions = {name: self._instrument(name).session for name in instruments}
        if not sessions:
            raise ValueError('measureRotatorGrid needs at least one instrument')
        runFolder = Path(folder) if folder else default_runs_folder()
        os.makedirs(runFolder, exist_ok=True)
        runner = MeasurementRunner(
            sequence=grid(values, traversal=traversal),
            controls=controls,
            instruments=sessions,
            folder=runFolder,
            settings=RunSettings(
                samples_per_point=int(samples_per_point),
                settle_s=float(settle_s),
                allow_unverified_timing=bool(allow_unverified_timing),
                return_to_start=bool(return_to_start),
                plane_label=plane_label,
                notes=notes,
            ),
            owner='rotator grid (script)',
            progress=progress,
        )
        return self._runStoppable(runner)

    def _runStoppable(self, runner) -> RunReport:
        """Run on the calling thread; ImScripting's Stop ends the run after
        the current point, and is delivered to the script once the run's
        data and devices are in order."""
        token = currentCancelToken()
        finished = threading.Event()

        def watch():
            while not finished.wait(0.1):
                if token is not None and token.isStopRequested():
                    runner.stop('stopped from ImScripting')
                    return
        watcher = threading.Thread(target=watch, name='measurement-run-stop-watch', daemon=True)
        watcher.start()
        try:
            report = runner.run()
        finally:
            finished.set()
        self.__logger.info(
            f'Measurement run {report.run_id}: {report.acquisition.value}, '
            f'{report.points_committed}/{report.points_total} points, cleanup '
            f'{report.cleanup.value}' + (f' -> {report.run_file}' if report.run_file else ''))
        checkpoint()        # deliver a pending Stop to the script now
        return report

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
