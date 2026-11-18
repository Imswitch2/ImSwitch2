"""The Timelapse widget's controller.

Runs N timepoints of one kind -- camera frames, a scan, or a whole tiling --
with a delay between them, and keeps the chosen lasers on for each timepoint
only.

The split of work (see ``docs/design/plans/timelapse-module.md``): this module
decides *when* each timepoint starts and switches the light around it; the
Recording controller keeps deciding *what is written*, so camera and scan
timelapses land in the ordinary lapse format. It hands Recording an
externally paced series and installs Recording's per-point gate, which holds
every point until this module releases it. A timelapse of tilings is the
exception: each timepoint is one tiling dataset folder, run through
``TilingController.runTilingForWorkflow``.

Threads. Recording asks for points and reports their ends on the GUI thread;
both are answered at once. All waiting -- for a point to be due, for a light
source to settle, for camera frames -- happens on one worker thread per run,
which goes back to the GUI thread for every laser switch, so the Laser widget
and the shared attributes Recording writes into each point stay true.
"""

import collections
import json
import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from qtpy import QtCore

from imswitch.imcommon.model import APIExport
from imswitch.imcontrol.model.managers import LeasePurpose
from imswitch.imcontrol.model.SetupInfo import TimelapseInfo
from imswitch.imcontrol.model.workflows.positioning_request import (
    PositioningRequest,
)
from imswitch.imcontrol.model.workflows.timelapse import (
    CadenceClock,
    TimelapseKind,
    TimelapsePlan,
    point_attributes,
    timelapse_folder,
    timepoint_folder,
)
from imswitch.imcontrol.view.widgets.TilingWidget import MODE_TRIGGERED
from imswitch.imcontrol.view.widgets.TimelapseWidget import (
    KIND_CAMERA,
    KIND_SCAN,
    KIND_TILING,
)
from ..basecontrollers import ImConWidgetController
from .._fresh_frame import FRESH_FRAME_TIMEOUT_S, grab_fresh_frame
from .TilingController import _CLOSE_JOIN_TIMEOUT_S

_FRESH_FRAME_CONSUMER_KEY = 'timelapse-illumination'
_MANIFEST_NAME = 'timelapse.json'

_NOTE_SCAN_LASERS = (
    'The scan program decides which lasers fire; every other laser stays off '
    'for the whole timelapse.'
)
_NOTE_UNPUBLISHED = ' This scan source does not publish its laser selection.'
_NOTE_TILING_DETECTORS = (
    'Tiling records the detectors chosen in the Tiling and Recording panels.'
)
_NOTE_TILING_FILES = (
    'Each timepoint is a tiling dataset folder (t###) under one timelapse '
    'folder.'
)
_TOOLTIP_SCAN_DRIVEN = 'Scan-driven: records only while a scan runs'


class _Stopped(Exception):
    """The run was stopped while the worker was waiting."""


class _GuiCall:
    """One call handed from the worker to the GUI thread, waited on.

    A call still queued when the run is stopped is withdrawn (it never runs);
    one already running is always waited for, so the worker never acts on a
    half-switched light source.
    """

    def __init__(self, fn, wake: threading.Condition):
        self.fn = fn
        self.wake = wake
        self.state = 'pending'
        self.result = None
        self.error = None

    def run(self) -> None:
        with self.wake:
            if self.state != 'pending':
                return
            self.state = 'running'
        try:
            self.result = self.fn()
        except BaseException as error:  # handed to the worker
            self.error = error
        with self.wake:
            self.state = 'done'
            self.wake.notify_all()


class _Run:
    """One timelapse, shared by the GUI thread and its worker.

    Every field the two threads both touch is read and written under
    ``wake``, which is also what every wait in the worker waits on: a stop, a
    point request, a series end or a finished GUI call all notify it.
    """

    def __init__(self, plan: TimelapsePlan):
        self.plan = plan
        self.kind = TimelapseKind(plan.kind).value
        self.clock = CadenceClock(plan.interval_s, plan.interval_mode)
        self.wake = threading.Condition()
        self.stop = threading.Event()
        self.runStart = time.monotonic()
        self.runStartUtc = datetime.now(timezone.utc)
        self.requests = collections.deque()
        self.lastStart: Optional[float] = None
        self.lastEnd: Optional[float] = None
        self.released = 0          # points released to Recording
        self.done = 0              # points known to have completed
        self.releasedIndex = None  # point released and not yet ended
        self.finished = False      # Recording's series has ended
        self.lit = False           # this run's lasers are on now
        self.illumination: tuple = ()  # Live lasers the gate switches
        self.restorePowers: Dict[str, Any] = {}
        self.leaseHandle = None
        self.recording = None
        self.tiling = None
        self.tilingSettings = None
        self.scanSource = None
        self.folder: Optional[Path] = None
        self.points: List[dict] = []
        self.thread: Optional[threading.Thread] = None
        self.cleanedUp = False
        self.message = ''


class TimelapseController(ImConWidgetController):
    """Linked to TimelapseWidget."""

    sigStatus = QtCore.Signal(str, bool)       # text, is error
    sigProgress = QtCore.Signal(int, int)      # done, total

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._run: Optional[_Run] = None
        self._lastStatus = ''
        self._closed = False

        info = getattr(self._setupInfo, 'timelapse', None) or TimelapseInfo()
        self._info = info

        self.sigStatus.connect(self._showStatus)
        self.sigProgress.connect(self._widget.setProgress)

        self._widget.setTimepoints(info.defaultTimepoints)
        self._widget.setIntervalS(info.defaultIntervalS)
        self._widget.setIntervalMode(info.defaultIntervalMode)
        self._widget.setFramesPerPoint(info.defaultFramesPerPoint)
        self._widget.setSingleFile(info.defaultSingleFile)
        self._widget.setIlluminationSettleMs(info.illuminationSettleMs)
        self._populateDetectors()
        self._refreshOffers(initial=True)
        self._widget.setProgress(0, self._widget.getTimepoints())

        self._widget.sigKindChanged.connect(self._applyKind)
        self._widget.sigParamsChanged.connect(self._refreshSaveSummary)
        self._widget.sigStartTimelapse.connect(self.startTimelapse)
        self._widget.sigStopTimelapse.connect(self.stopTimelapse)
        scanWorkflow = getattr(self._commChannel, 'scanWorkflow', None)
        signal = getattr(scanWorkflow, 'sigLaserScanStateChanged', None)
        if signal is not None:
            signal.connect(lambda *_a: self._applyKind(self._widget.getKind()))
        # Controllers are created one by one in widget order, so Tiling,
        # Laser or a scan panel may not exist yet. Look again once the event
        # loop runs, by which time every controller has been created.
        self._invokeOnControllerThread(
            lambda: self._refreshOffers(initial=True))

    def _refreshOffers(self, initial: bool = False) -> None:
        """Offer the kinds, scan sources and lasers this setup has now."""
        if self._run is not None or self._closed:
            return
        self._widget.setLasers(self._laserRows())
        self._populateScanSources()
        self._widget.setAvailableKinds(self._availableKinds())
        if initial:
            self._widget.setKind(self._info.defaultKind)
        self._applyKind(self._widget.getKind())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @APIExport(runOnUIThread=True)
    def startTimelapse(self) -> None:
        """ Starts a timelapse with the Timelapse widget's settings. The
        status line says why when it cannot start. """
        if self._run is not None:
            self._status('A timelapse is already running.', error=True)
            return
        try:
            plan = self._planFromWidget()
            problems = plan.problems()
        except Exception as error:
            self._status(f'Could not read the settings: {error}', error=True)
            return
        if problems:
            self._status(' '.join(problems), error=True)
            return

        run = _Run(plan)
        self._run = run
        self._widget.setRunning(True)
        self._widget.setProgress(0, plan.timepoints)
        try:
            refusal = self._start(run)
        except Exception as error:
            self._logger.error('Timelapse could not start', exc_info=True)
            refusal = f'Could not start: {error}'
        if refusal:
            self._finishRun(run, refusal, error=True)

    @APIExport(runOnUIThread=True)
    def stopTimelapse(self) -> None:
        """ Stops the running timelapse after its current timepoint. """
        run = self._run
        if run is None:
            return
        # Before the stop request, not after: stopping between timepoints
        # ends the run synchronously, and its final status must win.
        self._status('Stopping after the current timepoint...')
        self._requestStop(run)

    @APIExport()
    def getTimelapseStatus(self) -> Dict[str, Any]:
        """ Whether a timelapse is running, its kind, how many timepoints
        are done of how many, and the last status message. """
        run = self._run
        if run is None:
            return {'running': False, 'kind': None, 'done': 0, 'total': 0,
                    'message': self._lastStatus}
        with run.wake:
            done = run.done
        return {'running': True, 'kind': run.kind, 'done': done,
                'total': run.plan.timepoints, 'message': self._lastStatus}

    def closeEvent(self) -> bool:
        self._closed = True
        run = self._run
        if run is not None:
            self._requestStop(run)
            thread = run.thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(_CLOSE_JOIN_TIMEOUT_S)
            self._finishRun(run, 'Closed.', error=False)
        super().closeEvent()
        return self.shutdownComplete()

    def shutdownComplete(self) -> bool:
        run = self._run
        if run is None:
            return True
        thread = run.thread
        return thread is None or not thread.is_alive()

    # ------------------------------------------------------------------
    # What the widget offers
    # ------------------------------------------------------------------

    def _controllers(self) -> dict:
        try:
            return dict(self._commChannel.controllerRegistry() or {})
        except Exception:
            return {}

    def _availableKinds(self) -> List[str]:
        kinds = [KIND_CAMERA]
        if self._scanSourceNames():
            kinds.append(KIND_SCAN)
        tiling = self._controllers().get('Tiling')
        if (
            getattr(self._setupInfo, 'tiling', None) is not None
            and callable(getattr(tiling, 'runTilingForWorkflow', None))
        ):
            kinds.append(KIND_TILING)
        return kinds

    def _scanSourceNames(self) -> List[str]:
        getNames = getattr(self._commChannel, 'getRecordingScanSourceNames', None)
        if not callable(getNames):
            return []
        try:
            return list(getNames() or [])
        except Exception as error:
            self._logger.warning(f'Could not list scan sources: {error}')
            return []

    def _populateScanSources(self) -> None:
        self._widget.setScanSources(self._scanSourceNames())

    def _acquisitionDetectors(self) -> Dict[str, Any]:
        manager = self._master.detectorsManager
        names = manager.execOnAll(
            lambda c: c.name, condition=lambda c: c.forAcquisition
        )
        return {name: manager[name] for name in names.values()}

    def _populateDetectors(self) -> None:
        detectors = self._acquisitionDetectors()
        names = list(detectors)
        cameras = [n for n in names
                   if not getattr(detectors[n], 'isScanDriven', False)]
        self._widget.setDetectors(names, checked=cameras[:1] or names[:1])

    def _laserRows(self) -> List[dict]:
        rows = []
        laserController = self._controllers().get('Laser')
        for name, manager in self._master.lasersManager:
            binary = bool(getattr(manager, 'isBinary', False))
            wavelength = getattr(manager, 'wavelength', None)
            label = f'{wavelength:g} nm ({name})' if wavelength else name
            value = None
            if not binary and laserController is not None:
                try:
                    value = laserController.getLaserPowerSetting(name, 'live')
                except Exception:
                    value = None
            rows.append(dict(
                name=name, label=label, binary=binary,
                units=getattr(manager, 'valueUnits', '') or '',
                minimum=getattr(manager, 'valueRangeMin', None),
                maximum=getattr(manager, 'valueRangeMax', None),
                step=getattr(manager, 'valueRangeStep', None),
                decimals=getattr(manager, 'valueDecimals', None),
                value=value,
            ))
        return rows

    def _applyKind(self, kind: str) -> None:
        """Show what this kind of timelapse lets the operator choose."""
        w = self._widget
        if kind == KIND_SCAN:
            states = self._scanLaserStates()
            note = _NOTE_SCAN_LASERS + ('' if states else _NOTE_UNPUBLISHED)
            w.setLasersEditable(False, note)
            w.showLaserStates({
                name: bool(states.get(name, False))
                for name, _m in self._master.lasersManager
            })
            w.setDetectorsEnabled(True)
            for name in self._acquisitionDetectors():
                w.setDetectorEnabled(name, True)
            w.setFramesPerPointEnabled(False)
            w.setFramesPerPoint(1)
            w.setSingleFileEnabled(True)
            w.setScanSourceVisible(True)
        elif kind == KIND_TILING:
            triggered = self._tilingIsTriggered()
            if triggered:
                w.setLasersEditable(
                    False,
                    'Triggered tiling: ' + _NOTE_SCAN_LASERS[0].lower()
                    + _NOTE_SCAN_LASERS[1:],
                )
            else:
                w.setLasersEditable(True)
            w.setDetectorsEnabled(False, _NOTE_TILING_DETECTORS)
            w.setFramesPerPointEnabled(False)
            w.setFramesPerPoint(1)
            w.setSingleFileEnabled(False, _NOTE_TILING_FILES)
            w.setScanSourceVisible(False)
        else:
            w.setLasersEditable(True)
            w.setDetectorsEnabled(True)
            for name, detector in self._acquisitionDetectors().items():
                scanDriven = bool(getattr(detector, 'isScanDriven', False))
                w.setDetectorEnabled(
                    name, not scanDriven,
                    _TOOLTIP_SCAN_DRIVEN if scanDriven else '',
                )
            w.setFramesPerPointEnabled(True)
            w.setSingleFileEnabled(True)
            w.setScanSourceVisible(False)
        self._refreshSaveSummary()

    def _scanLaserStates(self) -> Dict[str, bool]:
        """Which lasers the selected scan fires, when it says so.

        Only a scan source that publishes its laser selection can answer
        (today the Advanced scan). The published state is global, so it is
        shown only when the selected source is such a publisher -- another
        source's lasers would be the wrong answer, which is worse than none.
        """
        source = self._resolveScanSource(quiet=True)
        if not callable(getattr(source, '_publishLaserScanState', None)):
            return {}
        scanWorkflow = getattr(self._commChannel, 'scanWorkflow', None)
        getter = getattr(scanWorkflow, 'laser_scan_state', None)
        if not callable(getter):
            return {}
        try:
            states = getter() or {}
        except Exception:
            return {}
        return {
            name: bool(getattr(state, 'active_for_scan', False))
            for name, state in states.items()
        }

    def _tilingIsTriggered(self) -> bool:
        tiling = self._controllers().get('Tiling')
        snapshot = getattr(tiling, 'snapshotRunSettings', None)
        if not callable(snapshot):
            return False
        try:
            return snapshot().mode == MODE_TRIGGERED
        except Exception:
            return False

    def _refreshSaveSummary(self) -> None:
        kind = self._widget.getKind()
        folder = self._recordingFolder() or '(no Recording panel folder)'
        if kind == KIND_TILING:
            text = f'{folder}/timelapse_<date_time>/t###/ (tiling datasets)'
        else:
            recording = self._controllers().get('Recording')
            fmt = ''
            getFormat = getattr(recording, 'getRecFileFormat', None)
            if callable(getFormat):
                try:
                    fmt = f', {getFormat()}'
                except Exception:
                    fmt = ''
            layout = (
                'one file for all timepoints' if self._widget.getSingleFile()
                else 'one file per timepoint'
            )
            text = f'Recording panel: {folder}{fmt}, {layout}'
        self._widget.setSaveSummary(text)

    def _recordingFolder(self) -> Optional[str]:
        getFolder = getattr(self._commChannel, 'getRecordingFolder', None)
        if not callable(getFolder):
            return None
        try:
            return getFolder()
        except Exception:
            return None

    def _resolveScanSource(self, quiet=False):
        resolve = getattr(self._commChannel, 'getRecordingScanSource', None)
        if not callable(resolve):
            if quiet:
                return None
            raise RuntimeError('This build cannot resolve a scan source.')
        try:
            return resolve(self._widget.getScanSource() or None)
        except Exception:
            if quiet:
                return None
            raise

    # ------------------------------------------------------------------
    # Starting
    # ------------------------------------------------------------------

    def _planFromWidget(self) -> TimelapsePlan:
        w = self._widget
        kind = w.getKind()
        lasers = () if (kind == KIND_SCAN or (
            kind == KIND_TILING and self._tilingIsTriggered()
        )) else tuple(w.getLasers())
        return TimelapsePlan(
            kind=kind,
            timepoints=w.getTimepoints(),
            interval_s=w.getIntervalS(),
            interval_mode=w.getIntervalMode(),
            detectors=tuple(w.getDetectors()) if kind != KIND_TILING else (),
            lasers=lasers,
            frames_per_point=w.getFramesPerPoint() if kind == KIND_CAMERA else 1,
            single_file=w.getSingleFile(),
            illumination_settle_ms=(
                w.getIlluminationSettleMs() if lasers else None
            ),
            scan_source=w.getScanSource() if kind == KIND_SCAN else '',
        )

    def _start(self, run: _Run) -> str:
        """Set up and launch; return a refusal reason, '' when running."""
        controllers = self._controllers()
        laser = controllers.get('Laser')
        if run.plan.lasers and laser is None:
            return 'Lasers were chosen but this setup has no Laser panel.'

        if run.kind in (KIND_CAMERA, KIND_SCAN):
            recording = controllers.get('Recording')
            if recording is None or not callable(
                getattr(recording, 'setPositioningProvider', None)
            ):
                return 'Camera and scan timelapses need the Recording panel.'
            if recording.getActiveSeriesInfo() is not None or bool(
                getattr(recording, 'recording', False)
            ):
                return 'A recording is running; wait for it to end.'
            run.recording = recording
        else:
            tiling = controllers.get('Tiling')
            if tiling is None:
                return 'Tiling is not available on this setup.'
            run.tiling = tiling
            run.tilingSettings = tiling.snapshotRunSettings()

        self._darkenAll(run, laser)
        for name, power in run.plan.lasers:
            if power is None:
                continue
            run.restorePowers[name] = laser.getLaserPowerSetting(name, 'live')
            laser.setLaserPowerSetting(name, power, 'live')
        if run.plan.lasers:
            run.illumination = tuple(laser.resolveLiveIlluminationLasers(
                [name for name, _p in run.plan.lasers]
            ))

        if run.kind == KIND_TILING:
            run.folder = timelapse_folder(self._recordingFolder())
            run.folder.mkdir(parents=True, exist_ok=True)
            self._writeManifest(run)
            return self._launchWorker(run, self._tilingWorker)

        if run.kind == KIND_CAMERA:
            # Held for the whole run: the cameras keep free-running through
            # the delays, so the frame handshake after switching on finds
            # frames flowing instead of a camera that is still starting.
            run.leaseHandle = self._master.detectorsManager.acquire(
                list(run.plan.detectors), LeasePurpose.WORKFLOW
            )

        recording = run.recording
        recording.setPositioningProvider(self._providePoint, partitionKind='time')
        recording.setCycleTerminalCallback(self._onPointTerminal)
        plan = run.plan
        if run.kind == KIND_CAMERA:
            result = recording.recordCameraSeries(
                list(plan.detectors), plan.timepoints, plan.interval_s,
                plan.single_file, framesPerPoint=plan.frames_per_point,
                externallyPaced=True, owner=self, onEnded=self._onSeriesEnded,
            )
        else:
            source = self._resolveScanSource()
            run.scanSource = source
            if plan.timepoints == 1:
                # One scan is a Scan-once recording, which never asks the
                # gate: it is released by being started.
                with run.wake:
                    run.released = 1
                    run.releasedIndex = 0
            result = recording.recordScanSeries(
                source, list(plan.detectors), plan.timepoints, plan.interval_s,
                plan.single_file, externallyPaced=plan.timepoints > 1,
                onEnded=self._onSeriesEnded,
            )
        if not result.accepted:
            return result.reason or 'Recording refused the timelapse.'
        if run.finished:
            # Ended inside the call (a one-scan series can): already cleaned.
            return ''
        refusal = self._launchWorker(run, self._seriesWorker)
        if not refusal:
            self._status(f'Timelapse running: {plan.timepoints} timepoints.')
        return refusal

    def _launchWorker(self, run: _Run, target) -> str:
        run.thread = threading.Thread(
            target=target, args=(run,), name='TimelapseWorker', daemon=True,
        )
        run.thread.start()
        return ''

    # ------------------------------------------------------------------
    # Lasers (GUI thread)
    # ------------------------------------------------------------------

    def _laserController(self):
        return self._controllers().get('Laser')

    def _darkenAll(self, run: _Run, laser=None) -> None:
        """Every laser's Live emission off. Errors are logged, not raised:
        this runs on the way out too."""
        laser = laser or self._laserController()
        if laser is None:
            return
        for name, _manager in self._master.lasersManager:
            try:
                laser.setLaserActive(name, False)
            except Exception:
                self._logger.error(
                    f'Timelapse: could not switch laser "{name}" off',
                    exc_info=True,
                )
        with run.wake:
            run.lit = False

    def _lightsOn(self, run: _Run) -> None:
        laser = self._laserController()
        with run.wake:
            run.lit = True  # first: a switch that raises half-way still gets its off
        for name in run.illumination:
            laser.setLaserActive(name, True)

    def _lightsOff(self, run: _Run) -> None:
        with run.wake:
            if not run.lit:
                return
        laser = self._laserController()
        failed = []
        for name in run.illumination:
            try:
                laser.setLaserActive(name, False)
            except Exception:
                failed.append(name)
                self._logger.error(
                    f'Timelapse: could not switch laser "{name}" off',
                    exc_info=True,
                )
        if failed:
            raise RuntimeError(
                f'Could not switch off: {", ".join(failed)}'
            )
        with run.wake:
            run.lit = False

    # ------------------------------------------------------------------
    # Recording's gate (GUI thread)
    # ------------------------------------------------------------------

    def _providePoint(self, index):
        """Recording asks for point ``index``: answer now, release it later."""
        run = self._run
        if run is None:
            return None
        # No session-side timeout: the worker is the only party that knows
        # when the point is due and fails the request itself on any error;
        # Stop cancels it. A finite timeout here would have to guess the
        # interval and the laser switching time.
        request = PositioningRequest(index, timeout_s=math.inf)
        with run.wake:
            # Asking for point i means points before it all completed.
            run.done = max(run.done, int(index))
            done = run.done
            run.requests.append(request)
            run.wake.notify_all()
        self.sigProgress.emit(done, run.plan.timepoints)
        return request

    def _onPointTerminal(self) -> None:
        """A point is wholly finished (writer and scan) -- or failed."""
        run = self._run
        if run is None:
            return
        with run.wake:
            ended = run.releasedIndex is not None
            run.releasedIndex = None
            if ended:
                run.lastEnd = time.monotonic()
            run.wake.notify_all()
        if run.illumination:
            try:
                self._lightsOff(run)
            except Exception as error:
                self._status(f'{error}. Check the lasers.', error=True)

    def _onSeriesEnded(self, failed: bool, message: str) -> None:
        run = self._run
        if run is None:
            return
        with run.wake:
            run.finished = True
            if not failed:
                run.done = run.released
            done = run.done
            stopped = run.stop.is_set()
            run.wake.notify_all()
        total = run.plan.timepoints
        if stopped:
            text, isError = f'Stopped after {done}/{total} timepoints.', False
            if failed and message:
                self._logger.info(f'Timelapse stopped: {message}')
        elif failed:
            text, isError = f'Failed after {done}/{total} timepoints: {message}', True
        else:
            text, isError = f'Done: {done}/{total} timepoints.', False
        self.sigProgress.emit(done, total)
        self._finishRun(run, text, error=isError)

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _wait(self, run: _Run, untilMonotonic: float) -> None:
        with run.wake:
            while not run.stop.is_set():
                remaining = untilMonotonic - time.monotonic()
                if remaining <= 0:
                    return
                run.wake.wait(remaining)
        raise _Stopped()

    def _callOnGui(self, run: _Run, fn, *, cancellable=True):
        if threading.current_thread() is threading.main_thread():
            return fn()
        call = _GuiCall(fn, run.wake)
        self._invokeOnControllerThread(call.run)
        with run.wake:
            while call.state != 'done':
                if cancellable and run.stop.is_set() and call.state == 'pending':
                    call.state = 'cancelled'
                    raise _Stopped()
                run.wake.wait()
        if call.error is not None:
            raise call.error
        return call.result

    def _seriesWorker(self, run: _Run) -> None:
        try:
            while True:
                with run.wake:
                    while (not run.requests and not run.finished
                           and not run.stop.is_set()):
                        run.wake.wait()
                    if run.finished:
                        return
                    if run.stop.is_set():
                        pending = list(run.requests)
                        run.requests.clear()
                    else:
                        pending = None
                        request = run.requests.popleft()
                if pending is not None:
                    for held in pending:
                        held.cancel('the timelapse was stopped')
                    # Recording ends the series; wait for that, not more work.
                    with run.wake:
                        while not run.finished and not run.requests:
                            run.wake.wait()
                        if run.finished:
                            return
                    continue
                self._releasePoint(run, request)
        except Exception:
            self._logger.error('Timelapse worker failed', exc_info=True)

    def _releasePoint(self, run: _Run, request: PositioningRequest) -> None:
        """Wait until the point is due, light it, prove the light, release."""
        index = request.index
        plan = run.plan
        with run.wake:
            previousStart, previousEnd = run.lastStart, run.lastEnd
        scheduled = run.clock.next_start(
            index, run_start_s=run.runStart, previous_start_s=previousStart,
            previous_end_s=previousEnd, now_s=time.monotonic(),
        )
        wait = scheduled.at_s - time.monotonic()
        if wait > 0:
            self.sigStatus.emit(
                f'Timepoint {index + 1}/{plan.timepoints} in {wait:.0f} s.',
                False,
            )
        try:
            self._wait(run, scheduled.at_s)
            if run.illumination:
                self._callOnGui(run, lambda: self._lightsOn(run))
                if plan.illumination_settle_ms:
                    self._wait(
                        run,
                        time.monotonic() + plan.illumination_settle_ms / 1000.0,
                    )
                problem = self._proveLight(run)
                if problem:
                    self._callOnGui(run, lambda: self._lightsOff(run),
                                    cancellable=False)
                    request.fail(problem)
                    return
        except _Stopped:
            self._switchOffFromWorker(run)
            request.cancel('the timelapse was stopped')
            return
        except Exception as error:
            self._logger.error('Timelapse: could not prepare a timepoint',
                               exc_info=True)
            self._switchOffFromWorker(run)
            request.fail(f'could not prepare timepoint {index + 1}: {error}')
            return

        request.attributes = point_attributes(
            plan, index,
            scheduled_s_since_start=scheduled.at_s - run.runStart,
            slip_s=scheduled.slip_s,
            lasers_applied=plan.lasers_dict(),
            settle_ms_applied=(
                plan.illumination_settle_ms if run.illumination else None
            ),
        )
        with run.wake:
            run.lastStart = time.monotonic()
            run.released = max(run.released, index + 1)
            run.releasedIndex = index
        if not request.resolve():
            # Cancelled under us (Stop): never leave the light on for it.
            with run.wake:
                run.releasedIndex = None
            self._switchOffFromWorker(run)
            return
        self.sigStatus.emit(
            f'Timepoint {index + 1}/{plan.timepoints} running'
            + (f' ({scheduled.slip_s:.1f} s late).' if scheduled.slip_s else '.'),
            False,
        )

    def _proveLight(self, run: _Run) -> str:
        """'' once every recorded camera delivered two frames since the
        lasers came on; otherwise why not.

        Same proof as ``_fresh_frame``: of two frames past the boundary the
        second started exposing after it. Recording then opens its own
        boundary, so every frame it writes started with the light on.
        """
        for name in run.plan.detectors:
            detector = self._master.detectorsManager[name]
            _frame, fresh = grab_fresh_frame(
                detector, _FRESH_FRAME_CONSUMER_KEY,
                should_stop=run.stop.is_set, logger=self._logger,
            )
            if run.stop.is_set():
                raise _Stopped()
            if not fresh:
                return (
                    f'{name} delivered no new frame within '
                    f'{FRESH_FRAME_TIMEOUT_S:g} s after the lasers came on'
                )
        return ''

    def _switchOffFromWorker(self, run: _Run) -> None:
        with run.wake:
            lit = run.lit
        if not lit:
            return
        try:
            self._callOnGui(run, lambda: self._lightsOff(run), cancellable=False)
        except Exception as error:
            self.sigStatus.emit(f'{error}. Check the lasers.', True)

    def _tilingWorker(self, run: _Run) -> None:
        plan = run.plan
        tiling = run.tiling
        gated = bool(run.illumination) and (
            getattr(run.tilingSettings, 'mode', None) != MODE_TRIGGERED
        )
        text, isError = '', False
        try:
            for index in range(plan.timepoints):
                with run.wake:
                    previousStart, previousEnd = run.lastStart, run.lastEnd
                scheduled = run.clock.next_start(
                    index, run_start_s=run.runStart,
                    previous_start_s=previousStart,
                    previous_end_s=previousEnd, now_s=time.monotonic(),
                )
                wait = scheduled.at_s - time.monotonic()
                if wait > 0:
                    self.sigStatus.emit(
                        f'Tiling {index + 1}/{plan.timepoints} in {wait:.0f} s.',
                        False,
                    )
                self._wait(run, scheduled.at_s)
                self.sigStatus.emit(
                    f'Tiling {index + 1}/{plan.timepoints} running.', False)
                with run.wake:
                    run.lastStart = time.monotonic()
                folder = timepoint_folder(run.folder, index, plan.timepoints)
                result = tiling.runTilingForWorkflow(
                    run.tilingSettings, folder,
                    illuminationGate=_TileIllumination(self, run) if gated else None,
                )
                with run.wake:
                    run.lastEnd = time.monotonic()
                    if result.completed:
                        run.done = index + 1
                    done = run.done
                run.points.append(dict(
                    index=index, folder=folder.name,
                    scheduled_offset_s=scheduled.at_s - run.runStart,
                    slip_s=scheduled.slip_s, completed=result.completed,
                    stopped=result.stopped, message=result.message,
                    tiles_acquired=result.tiles_acquired,
                    stale_tiles=result.stale_tiles,
                    illumination_gated=result.illumination_gated,
                ))
                self._writeManifest(run)
                self.sigProgress.emit(done, plan.timepoints)
                if not result.completed:
                    if run.stop.is_set() or result.stopped:
                        raise _Stopped()
                    text = (f'Failed at timepoint {index + 1}/{plan.timepoints}:'
                            f' {result.message}')
                    isError = True
                    break
            else:
                text = f'Done: {plan.timepoints}/{plan.timepoints} timepoints.'
        except _Stopped:
            with run.wake:
                done = run.done
            text = f'Stopped after {done}/{plan.timepoints} timepoints.'
        except Exception as error:
            self._logger.error('Timelapse of tilings failed', exc_info=True)
            text, isError = f'Failed: {error}', True
        finally:
            self._switchOffFromWorker(run)
            self._invokeOnControllerThread(
                lambda: self._finishRun(run, text, error=isError))

    def _writeManifest(self, run: _Run) -> None:
        plan = run.plan
        payload = {
            'kind': run.kind,
            'timepoints': plan.timepoints,
            'interval_s': plan.interval_s,
            'interval_mode': str(getattr(plan.interval_mode, 'value',
                                         plan.interval_mode)),
            'lasers': plan.lasers_dict(),
            'illumination_settle_ms': plan.illumination_settle_ms,
            'started_utc': run.runStartUtc.isoformat(),
            'points': list(run.points),
        }
        try:
            (run.folder / _MANIFEST_NAME).write_text(
                json.dumps(payload, indent=2), encoding='utf-8')
        except Exception:
            self._logger.error('Timelapse: could not write the manifest',
                               exc_info=True)

    # ------------------------------------------------------------------
    # Stopping and cleanup (GUI thread)
    # ------------------------------------------------------------------

    def _requestStop(self, run: _Run) -> None:
        with run.wake:
            run.stop.set()
            run.wake.notify_all()
        if run.recording is not None and not run.finished:
            try:
                if run.kind == KIND_CAMERA:
                    run.recording.stopSeries(self)
                else:
                    run.recording.stopSeries(run.scanSource)
            except Exception:
                self._logger.error('Timelapse: could not stop the recording',
                                   exc_info=True)
        if run.tiling is not None:
            try:
                run.tiling.stopTiling()
            except Exception:
                self._logger.error('Timelapse: could not stop the tiling',
                                   exc_info=True)

    def _finishRun(self, run: _Run, text: str, *, error: bool) -> None:
        """Exactly once per run: lights off, powers back, hooks released."""
        if run.cleanedUp:
            return
        run.cleanedUp = True
        with run.wake:
            run.stop.set()
            run.finished = True
            run.wake.notify_all()
        laser = self._laserController()
        # Left off, deliberately not restored: an unattended experiment must
        # not end with the light on.
        self._darkenAll(run, laser)
        for name, value in run.restorePowers.items():
            try:
                laser.setLaserPowerSetting(name, value, 'live')
            except Exception:
                self._logger.error(
                    f'Timelapse: could not restore the power of "{name}"',
                    exc_info=True,
                )
        if run.recording is not None:
            try:
                run.recording.clearPositioningProvider()
            except Exception:
                self._logger.error('Timelapse: could not release Recording',
                                   exc_info=True)
        if run.leaseHandle is not None:
            try:
                self._master.detectorsManager.release(run.leaseHandle)
            except Exception:
                self._logger.error('Timelapse: could not release the cameras',
                                   exc_info=True)
            run.leaseHandle = None
        if self._run is run:
            self._run = None
        if not self._closed:
            self._widget.setRunning(False)
            self._status(text, error=error)

    def _status(self, text: str, *, error: bool = False) -> None:
        self._lastStatus = text
        if error:
            self._logger.warning(f'Timelapse: {text}')
        self._showStatus(text, error)

    def _showStatus(self, text: str, error: bool) -> None:
        self._lastStatus = text
        if self._closed:
            return
        self._widget.setStatus(text)
        self._widget.setStatusIsError(bool(error))


class _TileIllumination:
    """The illumination gate a free-running tiling run calls per tile."""

    def __init__(self, controller: TimelapseController, run: _Run):
        self._controller = controller
        self._run = run

    def before_tile(self, index: int) -> None:
        controller, run = self._controller, self._run
        controller._callOnGui(run, lambda: controller._lightsOn(run))
        settle = run.plan.illumination_settle_ms
        if settle:
            controller._wait(run, time.monotonic() + settle / 1000.0)

    def after_tile(self, index: int) -> None:
        controller, run = self._controller, self._run
        controller._callOnGui(run, lambda: controller._lightsOff(run),
                              cancellable=False)
