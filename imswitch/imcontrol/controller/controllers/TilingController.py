import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple
import json

import numpy as np
from qtpy import QtCore, QtGui, QtWidgets

from imswitch.imcommon.model import APIExport
from imswitch.imcommon.algorithms.tile_mosaic import TileLink, solve_links
from imswitch.imcommon.algorithms.detector_transform import (
    parse_detector_transform,
)
from imswitch.imcontrol.model.workflows.positioning_request import (
    PositioningRequest,
)
from imswitch.imcontrol.model.managers import LeasePurpose
from imswitch.imcontrol.model.managers.RecordingManager import FailureKind
from imswitch.imcontrol.model.workflows import StitchedImage
from imswitch.imcontrol.model.workflows.spiral import SPIRAL, tile_moves
from imswitch.imcontrol.model.workflows.target import TargetList
from imswitch.imcontrol.model.workflows.tilt_sampling import calibration_visits
from imswitch.imcontrol.model.workflows.stage_tilt_calibration import (
    StageTiltRecorder, new_tilt_calibration_folder, tilt_calibrations_root,
)
from imswitch.imcontrol.model.workflows.tilt_calibration_labels import (
    calibration_display_name, save_calibration_name,
)
from imswitch.imcontrol.model.workflows.tilt_preferences import (
    selected_calibration, remember_calibration,
)
from imswitch.imcontrol.model import configfiletools
from imswitch.imcontrol.model.workflows.stage_tilt_compensation import (
    TiltCompensationRecorder, load_tilt_plane, validate_z_target,
    require_reference_signal,
)
from imswitch.imcontrol.model.workflows.tile_dataset import (
    TileDataset,
    TileRecord,
    tiling_folder,
)
from imswitch.imcontrol.model.workflows.tile_registration import (
    RegistrationReport,
    TileShift,
    estimate_shift,
    max_shift_for_step,
)
from imswitch.imcontrol.view.widgets.TilingWidget import (
    CLICK_ADD_TARGET,
    CLICK_NAVIGATE,
    MODE_FREE_RUNNING,
    MODE_TRIGGERED,
    TiltCalibrationDetailsDialog,
)
from ..basecontrollers import ImConWidgetController
from .._fresh_frame import grab_fresh_frame

_SETTLE_S = 0.15  # stage settle time after each move (seconds)
_CLOSE_JOIN_TIMEOUT_S = 2.0
_FRESH_FRAME_TIMEOUT_S = 5.0  # max wait for a post-settle frame, per tile
_FRAME_POLL_S = 0.005
_OVERVIEW_EMIT_INTERVAL_S = 0.2  # throttle live overview repaints
_CHUNK_CONSUMER_KEY = 'tiling'
_SCAN_TIMEOUT_S = 300.0  # generous: one tile is a whole scan


@dataclass(frozen=True)
class TilingRunSettings:
    """Everything one tiling run reads from the Tiling widget, taken at once.

    A timelapse of tilings snapshots this on the GUI thread when it starts and
    hands the same object to every timepoint: all timepoints then share one
    grid even if the operator edits the panel mid-experiment, and the worker
    running them never reads Qt widgets from its own thread.
    """

    n_tiles_x: int
    n_tiles_y: int
    pattern: str
    step_um: float
    blend_overlaps: bool
    intensity_correction: bool
    settle_s: float
    register_tiles: bool
    orientation: Tuple[bool, bool, bool]
    save_tiles: bool
    mode: str
    scan_source_key: Optional[str]
    detector_name: Optional[str]
    build_overview: bool = True
    record_tilt: bool = False
    tilt_record_name: str = ''
    tilt_calibration_only: bool = False
    tilt_bidirectional: bool = False
    tilt_sample_nx: Optional[int] = None
    tilt_sample_ny: Optional[int] = None
    tilt_plane: object = None
    precheck_tilt: bool = False

    @property
    def n_tiles(self) -> int:
        return self.n_tiles_x * self.n_tiles_y


@dataclass(frozen=True)
class TilingRunResult:
    """How one tiling run ended, for a caller that runs tilings itself.

    ``completed`` means every tile was acquired and the stage returned to the
    origin; ``stopped`` that the operator (or the caller) asked it to stop.
    ``illumination_gated`` says whether the caller's illumination gate was
    actually used -- it is not in triggered mode, where the scan arms its own
    lasers per tile.
    """

    completed: bool
    stopped: bool
    message: str = ''
    folder: Optional[str] = None
    tiles_acquired: int = 0
    stale_tiles: int = 0
    illumination_gated: bool = False


class _FreeRunningTileSource:
    """One tile = one frame plucked from a continuously running camera.

    The camera is armed for the whole run and each tile takes whatever frame
    is provably newer than the stage move (see
    :meth:`TilingController._grabSettledFrame`).
    """

    needsSettleWait = True
    #: Other detectors in the save set must prove their frame is post-move.
    needsFreshFrame = True

    def __init__(self, controller, detector):
        self._controller = controller
        self._detector = detector

    def prepare(self) -> None:
        pass

    def acquire(self):
        """Return ``(frame, wasFresh)``."""
        return self._controller._grabSettledFrame(self._detector)

    def finish(self) -> None:
        try:
            self._detector.releaseChunkConsumer(_CHUNK_CONSUMER_KEY)
        except Exception as e:
            self._controller._logger.error(
                f'Failed to release tiling chunk consumer: {e}', exc_info=True
            )


class _PointOutcome:
    """One tile's recording, as the tiling worker waits on it.

    Shaped like a scan completion because that is what it replaces: the caller
    was already waiting on one of those, and only the thing producing it moved.
    """

    def __init__(self):
        self._event = threading.Event()
        self.successful = False
        self.message = ''

    def wait(self, timeout=None) -> bool:
        return self._event.wait(timeout)

    def resolve(self, successful: bool, message: str = '') -> None:
        if self._event.is_set():
            return
        self.successful = bool(successful)
        self.message = str(message)
        self._event.set()


class _RecordingDispatcher:
    """Runs each tile's scan inside one recording session.

    The two halves meet here. Tiling owns the stage and knows when a tile's
    position is reached; the recording session owns the scan and the writer.
    Neither drives the other: tiling hands over a resolved positioning request
    and waits, while the session -- advancing on its own timers, on the GUI
    thread -- picks it up, arms, and reports the point finished.

    That rendezvous is the reason tiling no longer starts scans itself. Two
    dispatchers would command the same firmware for one tile, and whichever
    arrived second would be scanning a stage the first had already moved.
    """

    def __init__(self, controller, recordingController, pointTimeoutS,
                 folder=None):
        self._controller = controller
        self._recording = recordingController
        self._timeout = pointTimeoutS
        #: The run folder every payload lives under, so its locator can be
        #: recorded relative to the manifest beside it. An absolute path would
        #: break the moment the dataset is moved or archived.
        self._folder = Path(folder) if folder is not None else None
        self._lock = threading.Lock()
        self._awaiting = None      # request the gate is polling
        self._queued = None        # request tiling supplied early
        self._outcome = None       # the point tiling is waiting on
        self._locators = {}
        self._lastFailureKind = None

    # -- asked by the recording session, on its thread -----------------

    def provider(self, index):
        """A request for point ``index``, resolved once the stage is there."""
        with self._lock:
            queued, self._queued = self._queued, None
            request = queued or PositioningRequest(index, self._timeout)
            self._awaiting = None if request.settled else request
            return request

    # -- called by the tiling worker, on its thread --------------------

    def runPoint(self, request):
        """Hand over a positioned point and get something to wait on."""
        outcome = _PointOutcome()
        with self._lock:
            self._outcome = outcome
            awaiting = self._awaiting
            if awaiting is None:
                # The session has not asked yet; it will take this on its next
                # pass rather than manufacture its own.
                self._queued = request
            else:
                self._awaiting = None
        if awaiting is not None:
            # The session asked first, so the request it holds is its own,
            # not the one tiling built for this tile: carry over what the
            # point's recording should say about where it was.
            awaiting.attributes = dict(getattr(request, 'attributes', {}) or {})
            # Mirror the terminal rather than assume one. Releasing the gate
            # on an unresolved request would arm the point without the stage
            # having reported arrival -- the single thing this handshake is
            # here to prevent. A request still in flight is simply left
            # pending, and the gate's own timeout covers a stage that never
            # answers.
            if request.mayProceed:
                awaiting.resolve()
            elif request.settled:
                awaiting.fail(request.message or request.outcome.value)
        return outcome

    def locatorsForLastPoint(self):
        with self._lock:
            return dict(self._locators)

    # -- terminals published by the recording manager ------------------

    def onWriterFinalised(self, generation):
        """The writer has drained: its locators are authoritative now.

        Not the point of completion, though — the scan's own lifecycle cleanup
        is still running, and moving the stage into that would corrupt the tile
        that is still being torn down.
        """
        try:
            locators = self._recording._master.recordingManager.payloadLocators(
                generation
            )
        except Exception:
            locators = {}
        with self._lock:
            self._locators = {
                name: self._relative(locator)
                for name, locator in locators.items()
            }

    def _relative(self, locator):
        """Re-root a locator's path under the run folder, when it is beneath it."""
        if self._folder is None or not hasattr(locator, 'path'):
            return locator
        try:
            relative = Path(locator.path).relative_to(self._folder)
        except (ValueError, TypeError):
            return locator
        return type(locator)(**{**locator.asdict(), 'path': relative.as_posix()})

    def onPointFinished(self):
        """Writer drained *and* scan complete: the stage may move again."""
        with self._lock:
            outcome = self._outcome
        if outcome is not None:
            outcome.resolve(True)

    def onPointFailed(self, message, _generation, kind):
        with self._lock:
            self._locators = {}
            outcome = self._outcome
            self._lastFailureKind = kind
        if outcome is not None:
            outcome.resolve(False, f'{kind}: {message}')

    def lastFailureWasRecoverable(self, message='') -> bool:
        """Whether the run may reasonably carry on past the last failure.

        Only a writer failure: the measurement happened and the file did not,
        so the remaining tiles are unaffected. Anything touching acquisition,
        the scan or the hardware means the data itself is suspect, and an
        unclassified failure is treated as the unsafe kind.

        Storage exhaustion is excluded even though it *is* a writer failure:
        the next tile's payload and its alignment image would fail identically,
        so a run would grind through the rest of the trajectory producing
        nothing but errors.
        """
        with self._lock:
            kind = self._lastFailureKind
        if str(kind) != FailureKind.WRITER.value:
            return False
        exhausted = ('no space', 'disk full', 'quota', 'errno 28')
        return not any(token in message.lower() for token in exhausted)


class _TriggeredTileSource:
    """One tile = one scan.

    This covers both triggered cases with the same code, because they are the
    same event: a scan-driven detector (APD/PMT) builds its image *as* the scan
    runs, and a camera wired to the scan's trigger output is clocked by that
    same scan. Either way the tile is ready when the scan reports completion —
    an exact signal, so unlike free-running mode there is no stale-frame risk
    to guard against.
    """

    needsSettleWait = True
    #: The scan that produced the tile clocked every detector it drives, so
    #: their frames are already the right ones -- no handshake needed.
    needsFreshFrame = False

    def __init__(self, controller, detector, scanSource):
        self._controller = controller
        self._detector = detector
        self._scanSource = scanSource
        self._first = True
        #: Set while a recording session owns scan dispatch for this run. The
        #: two must never both arm: tiling calling ``run_scan_from`` while a
        #: lapse also starts one would command the same firmware twice.
        self._dispatcher = None
        self._pointIndex = 0

    def useDispatcher(self, dispatcher) -> None:
        """Hand scan dispatch to a recording session for this run.

        With one set, tiling stops starting scans: it says where the stage is
        and waits for the point the session runs there. Without one it keeps
        driving its own scans, which is what a run that saves no payload does.
        """
        self._dispatcher = dispatcher

    def prepare(self) -> None:
        pass

    def acquire(self):
        controller = self._controller
        if self._dispatcher is not None:
            return self._acquireFromDispatcher()
        completion = self._runScan()
        if completion is None:
            return None, False

        timeout = controller._scanTimeoutS()
        if not completion.wait(timeout):
            controller._logger.error(
                f'Tiling: scan did not finish within {timeout:g} s'
            )
            self._abort()
            return None, False
        if not completion.successful:
            controller._logger.error(
                f'Tiling: scan failed — {completion.message}'
            )
            return None, False

        frame = controller._scanFrame(self._detector)
        self._waitForFocus()
        return frame, frame is not None

    def _acquireFromDispatcher(self):
        """One point, run by the recording session that owns dispatch.

        The stage is already where this tile belongs -- the caller moved and
        settled it before asking -- so the positioning request is resolved
        immediately. It is still expressed as a request rather than skipped:
        the session refuses to arm on anything but a resolved one, and that
        refusal is the guarantee that a point is never acquired somewhere the
        stage was not.
        """
        controller = self._controller
        request = PositioningRequest(
            self._pointIndex, timeout_s=controller._scanTimeoutS(),
            attributes=self.__dict__.get('pointAttributes') or {},
        )
        request.resolve()
        self._pointIndex += 1

        completion = self._dispatcher.runPoint(request)
        if completion is None:
            controller._logger.error(
                'Tiling: the recording session did not run this point'
            )
            return None, False

        timeout = controller._scanTimeoutS()
        if not completion.wait(timeout):
            controller._logger.error(
                f'Tiling: point did not finish within {timeout:g} s'
            )
            return None, False
        if not completion.successful:
            if self._dispatcher.lastFailureWasRecoverable(completion.message):
                controller._logger.warning(
                    f'Tiling: this tile has no payload — {completion.message}. '
                    'The measurement itself was fine, so the run continues; '
                    'the manifest records the tile without one.'
                )
                frame = controller._scanFrame(self._detector)
                self._waitForFocus()
                return frame, frame is not None
            controller._logger.error(
                f'Tiling: point failed — {completion.message}'
            )
            return None, False

        frame = controller._scanFrame(self._detector)
        self._waitForFocus()
        return frame, frame is not None

    def _waitForFocus(self) -> None:
        """Hold the tile loop until the focus lock is holding again.

        Each tile's scan suspends the lock, and the scan's completion resolves
        as soon as the terminal is published -- while reacquisition is only
        just starting. Without this the loop moves the stage and fires the next
        scan straight through the barrier, which cancels it. At the shipped
        10 Hz estimate rate a five-sample window needs ~0.5 s against a 0.15 s
        settle, so every tile would cancel the previous tile's reacquisition
        and the lock would stay inactive for the entire run -- silently, since
        each individual step looks like it succeeded.

        Runs on the tiling worker thread, which is what makes the wait legal:
        the barrier is advanced by the focus lock's timer on the GUI thread.
        """
        controller = self._controller
        try:
            reacquired = controller._commChannel.waitForFocusReacquired(
                controller._focusReacquireTimeoutS()
            )
        except Exception as e:
            controller._logger.error(
                f'Tiling: failed to wait for focus reacquisition: {e}',
                exc_info=True,
            )
            return
        if not reacquired:
            controller._logger.warning(
                'Tiling: focus lock did not reacquire before the next tile; '
                'continuing unlocked. Subsequent tiles may drift out of focus.'
            )

    def _runScan(self):
        """Request one scan and return its completion terminal, or None."""
        controller = self._controller
        try:
            # Signals are recalculated only for the first tile: the scan
            # geometry does not change between tiles, and rebuilding it every
            # time would add its own latency to each one.
            result = controller._commChannel.scanWorkflow.run_scan_from(
                self._scanSource,
                recalculate_signals=self._first,
                is_non_final_part_of_sequence=False,
            )
        except Exception as e:
            controller._logger.error(f'Tiling: could not start scan: {e}',
                                     exc_info=True)
            return None
        self._first = False

        if not result.accepted:
            controller._logger.error(
                f'Tiling: scan request refused — {result.rejectionMessage}'
            )
            return None

        completions = [
            completion
            for _owner, _token, completion in result.acceptedCompletions
            if completion is not None
        ]
        if not completions:
            controller._logger.error(
                'Tiling: the scan controller accepted the request but exposes '
                'no completion terminal, so tile timing cannot be trusted. '
                'Use a coordinated Scan controller for triggered tiling.'
            )
            return None
        return completions[0]

    def _abort(self) -> None:
        try:
            self._controller._commChannel.scanWorkflow.abort_scan_from(
                self._scanSource
            )
        except Exception as e:
            self._controller._logger.error(
                f'Tiling: failed to abort a stalled scan: {e}', exc_info=True
            )

    def finish(self) -> None:
        pass


class TilingController(ImConWidgetController):
    """Controls spiral tiling scans with live stitching and click-to-navigate."""

    sigOverviewUpdated = QtCore.Signal(object)   # np.ndarray
    sigProgressUpdated = QtCore.Signal(int, int)  # current, total
    sigRunningChanged = QtCore.Signal(bool)       # routes setRunning across threads
    sigTargetsChanged = QtCore.Signal(object)     # list of target dicts
    sigTargetMarkers = QtCore.Signal(object)      # {target id: (row, col)}
    sigRegistrationSummary = QtCore.Signal(str)
    sigTiltCompensationError = QtCore.Signal(str)
    sigTiltCalibrationCreated = QtCore.Signal(str)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._scanning = False
        self._stopRequested = False
        self._stitcher: Optional[StitchedImage] = None
        self._originXY: Optional[Tuple[float, float]] = None
        self._gridPositions: List[Tuple[int, int]] = []
        self._lastStepUm: float = 100.0
        # Stage positions to come back to. Kept across runs: a target is a
        # place on the sample, not a pixel of one particular overview.
        self._targets = TargetList()
        self._activityLock = threading.Lock()
        self._scanThread: Optional[threading.Thread] = None
        self._scanAcqHandle = None
        self._closed = False
        self._registrationReport = None
        self._orientation = (False, False, False)
        self._lastSaveFolder = None
        #: How the last run ended (TilingRunResult), for runTilingForWorkflow.
        self._lastRunResult = None
        try:
            self._setupNameForTilt = (
                getattr(configfiletools.loadOptions()[0], 'setupFileName', '') or ''
            )
        except (OSError, ValueError) as error:
            # Optional calibration persistence must never prevent tiling startup.
            self._logger.warning('Cannot read tiling tilt setup identity: %s', error)
            self._setupNameForTilt = ''
        # Every pairwise measurement taken during a run, kept for the
        # whole-run solve that advanced alignment ends with.
        self._alignmentLinks = []
        self._alignmentIndex = {}

        tilingInfo = self._setupInfo.tiling
        if tilingInfo is None:
            return

        self._widget.setDefaultStep(tilingInfo.defaultTileStepUm)
        self._widget.setDefaultTileCounts(
            getattr(tilingInfo, 'defaultTilesX', 3) or 3,
            getattr(tilingInfo, 'defaultTilesY', 3) or 3,
        )
        self._widget.setDefaultPattern(
            getattr(tilingInfo, 'defaultPattern', SPIRAL) or SPIRAL
        )
        self._widget.setDefaultSettleTimeMs(
            getattr(tilingInfo, 'settleTimeMs', _SETTLE_S * 1000.0)
        )
        self._widget.setDefaultRegisterTiles(
            getattr(tilingInfo, 'registerTiles', False)
        )
        self._widget.setDefaultSaveTiles(getattr(tilingInfo, 'saveTiles', False))
        self._widget.setMode(getattr(tilingInfo, 'mode', MODE_FREE_RUNNING))
        self._populateScanSources(getattr(tilingInfo, 'scanSource', ''))
        self._populateDetectors(getattr(tilingInfo, 'camera', ''))
        self._widget.sigDetectorChanged.connect(self._onDetectorChanged)
        self._widget.setDefaultTileOrientation(
            getattr(tilingInfo, 'flipTileAxisX', False),
            getattr(tilingInfo, 'flipTileAxisY', False),
            getattr(tilingInfo, 'swapTileAxes', False),
        )

        self._widget.sigStartTiling.connect(self.startTiling)
        self._widget.sigStopTiling.connect(self.stopTiling)
        self._widget.sigClickOnOverview.connect(self._onOverviewClicked)
        self._widget.sigGoToTarget.connect(self.goToTarget)
        self._widget.sigRemoveTarget.connect(self.removeTarget)
        self._widget.sigClearTargets.connect(self.clearTargets)
        self._widget.sigTargetEnabledChanged.connect(self.setTargetEnabled)
        self._widget.sigSaveTargets.connect(self.saveTargets)
        self._widget.sigLoadTargets.connect(self.loadTargets)
        self.sigOverviewUpdated.connect(self._widget.updateOverview)
        self.sigProgressUpdated.connect(self._widget.setProgress)
        self.sigRunningChanged.connect(self._widget.setRunning)
        self.sigTargetsChanged.connect(self._widget.setTargets)
        self.sigTargetMarkers.connect(self._widget.setTargetMarkers)
        self.sigRegistrationSummary.connect(self._widget.setRegistrationSummary)
        self._widget.sigModeChanged.connect(self._onModeChanged)
        self._widget.sigLoadTiltCalibration.connect(self._loadTiltCalibrationFile)
        self._widget.sigShowTiltCalibration.connect(self._showTiltCalibration)
        self._widget.sigOpenTiltCalibrationFolder.connect(self._openTiltCalibrationFolder)
        self._widget.sigSetDefaultTiltCalibration.connect(self._setDefaultTiltCalibration)
        self._widget.sigTiltRecordModeSelected.connect(self._updateTiltRecordActuator)
        self.sigTiltCompensationError.connect(self._showTiltCompensationError)
        self.sigTiltCalibrationCreated.connect(self._onTiltCalibrationCreated)
        self._restoreSelectedTiltCalibration()
        self._updateTiltRecordActuator()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @QtCore.Slot(str)
    def _showTiltCompensationError(self, message: str) -> None:
        QtWidgets.QMessageBox.warning(
            self._widget, 'Tilt compensation stopped', message
        )

    def _validateSelectedTiltPlane(self, path):
        """The calibration file names the Z actuator; the setup must provide it."""
        plane = load_tilt_plane(
            path, xy_positioner=self._setupInfo.tiling.xyPositioner,
        )
        info = self._setupInfo.positioners.get(plane.focus_positioner)
        if info is None or 'Z' not in info.axes or not info.forPositioning:
            raise ValueError(
                f'Calibration Z actuator {plane.focus_positioner!r} is not a '
                'configured Z-capable positioning device'
            )
        positioner = self._master.positionersManager[plane.focus_positioner]
        if not callable(getattr(positioner, 'move', None)) or not callable(
            getattr(positioner, 'get_abs', None)
        ):
            raise ValueError('Compensation Z actuator lacks movement or position reporting')
        return plane

    def _selectTiltCalibration(self, path, plane=None):
        plane = plane or self._validateSelectedTiltPlane(path)
        self._widget.setLoadedTiltCalibration(
            plane.path, plane, calibration_display_name(plane.path)
        )
        return plane

    def _restoreSelectedTiltCalibration(self):
        try:
            path = selected_calibration(self._setupNameForTilt)
            self._widget.setDefaultTiltCalibration(path)
            if path:
                self._selectTiltCalibration(path)
        except (OSError, ValueError, TypeError, KeyError) as error:
            self._logger.warning('Could not reload default tilt calibration: %s', error)

    def _updateTiltRecordActuator(self):
        """Read the live Focus Lock choice; never query hardware for a label."""
        focus = getattr(self._commChannel, '_focusLock', None)
        actuator = getattr(focus, 'positioner', '') if focus is not None else ''
        self._widget.setTiltRecordZActuator(actuator)

    @QtCore.Slot(str)
    def _onTiltCalibrationCreated(self, path: str):
        """Expose the last recording in Record mode; also select it for Apply."""
        # Viewing a finished report must not require selecting an Apply plane.
        self._widget.setLastRecordedTiltCalibration(path)
        try:
            self._selectTiltCalibration(path)
        except (OSError, ValueError, TypeError, KeyError) as error:
            self._logger.warning('Could not select newly recorded tilt calibration: %s', error)

    def _tiltActionPath(self):
        """Resolve the menu's context: last recording or Apply selection."""
        return self._widget.getTiltActionCalibrationPath()

    def _loadTiltCalibrationFile(self) -> None:
        filename, _ = QtWidgets.QFileDialog.getOpenFileName(
            self._widget, 'Load stage tilt calibration',
            str(tilt_calibrations_root()),
            'Tilt calibration (tilt_fit.json);;JSON files (*.json)'
        )
        if not filename:
            return
        try:
            self._selectTiltCalibration(filename)
        except (OSError, ValueError, TypeError, KeyError) as error:
            QtWidgets.QMessageBox.warning(
                self._widget, 'Invalid tilt calibration', str(error)
            )

    def _setDefaultTiltCalibration(self):
        path = self._tiltActionPath()
        if not path:
            return
        if not self._setupNameForTilt:
            QtWidgets.QMessageBox.warning(
                self._widget, 'Default tilt calibration',
                'Cannot identify the current hardware setup.'
            )
            return
        try:
            plane = self._validateSelectedTiltPlane(path)
            remember_calibration(self._setupNameForTilt, plane.path)
            self._widget.setDefaultTiltCalibration(plane.path)
        except (OSError, ValueError, TypeError, KeyError) as error:
            QtWidgets.QMessageBox.warning(
                self._widget, 'Cannot set default calibration', str(error)
            )

    def _renameTiltCalibration(self, path, name: str) -> str:
        save_calibration_name(path, name)
        displayed = calibration_display_name(path)
        # Renaming never needs to revalidate or reselect the scientific fit.
        self._widget.setTiltCalibrationDisplayName(path, displayed)
        if self._widget.getLastRecordedTiltCalibrationPath() == str(Path(path).resolve()):
            self._widget.setLastRecordedTiltCalibration(path)
        return displayed

    def _showTiltCalibration(self):
        path = self._tiltActionPath()
        if not path:
            return
        try:
            fit_path = Path(path).expanduser().resolve()
            doc = json.loads(fit_path.read_text(encoding='utf-8'))
            if not isinstance(doc, dict):
                raise ValueError('Expected a calibration JSON object')
            # Inspection is deliberately independent of selecting a usable
            # Apply plane, so a recorded fit can be viewed in Record mode.
            dialog = TiltCalibrationDetailsDialog(
                fit_path, doc, calibration_display_name(fit_path),
                lambda name: self._renameTiltCalibration(fit_path, name),
                self._widget,
            )
        except (OSError, ValueError, TypeError, KeyError) as error:
            QtWidgets.QMessageBox.warning(
                self._widget, 'Cannot open calibration', str(error)
            )
            return
        dialog.exec_()

    def _openTiltCalibrationFolder(self):
        path = self._tiltActionPath()
        if not path:
            return
        folder = Path(path).parent
        if not folder.is_dir():
            QtWidgets.QMessageBox.warning(
                self._widget, 'Calibration folder',
                'The selected calibration folder no longer exists.'
            )
            return
        if not QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(folder))):
            QtWidgets.QMessageBox.warning(
                self._widget, 'Calibration folder',
                f'Could not open {folder}'
            )

    @APIExport(runOnUIThread=True)
    def startTiling(self) -> None:
        """ Starts a tiling run with the Tiling widget's settings. Does
        nothing on a setup without a ``tiling`` section, or while a run is
        busy. """
        tilingInfo = self._setupInfo.tiling
        if tilingInfo is None:
            return
        with self._getActivityLock():
            busy = self._scanning or getattr(self, '_closed', False)
            if not busy:
                self._scanning = True
                self._stopRequested = False
        if busy:
            return
        try:
            settings = self.snapshotRunSettings()
            self._resetRunState(settings)

            self._widget.setRunning(True)
            progress_tiles = (
                (settings.tilt_sample_nx or settings.n_tiles_x)
                * (settings.tilt_sample_ny or settings.n_tiles_y)
                if settings.tilt_calibration_only else settings.n_tiles
            )
            self._widget.setProgress(
                0, progress_tiles * (2 if settings.tilt_bidirectional else 1)
            )
            self._widget.setRegistrationSummary('')
            # The markers belong to the overview being replaced; the targets
            # themselves stay and are drawn again on the new one.
            self._refreshTargets()

            if settings.tilt_calibration_only:
                worker = self._runTiltCalibrationOnly
                args = (tilingInfo, settings.n_tiles_x, settings.n_tiles_y,
                        settings.pattern, settings.step_um)
                kwargs = dict(
                    sample_nx=settings.tilt_sample_nx or settings.n_tiles_x,
                    sample_ny=settings.tilt_sample_ny or settings.n_tiles_y,
                    bidirectional=settings.tilt_bidirectional,
                    settle_s=settings.settle_s,
                    tilt_record_name=settings.tilt_record_name,
                )
            else:
                worker = self._runScan
                args = self._runScanArgs(tilingInfo, settings)
                kwargs = self._runScanKwargs(settings)
            self._scanThread = threading.Thread(
                target=worker, args=args, kwargs=kwargs, daemon=True,
            )
            self._commChannel._tilingActive = True
            self._scanThread.start()
        except Exception as e:
            self._commChannel._tilingActive = False
            with self._getActivityLock():
                self._scanning = False
                self._scanThread = None
            try:
                self._widget.setRunning(False)
            except Exception:
                pass
            self._logger.error(
                f'Tiling worker could not start: {e}', exc_info=True
            )
            if bool(getattr(self._widget, 'getApplyTiltCalibration', lambda: False)()):
                self._showTiltCompensationError(str(e))

    def snapshotRunSettings(self) -> TilingRunSettings:
        """Take one GUI-thread snapshot, including tilt and preview choices.

        The same immutable settings object can be used for every timepoint of
        a workflow: no worker ever reads changing Qt controls.
        """
        widget = self._widget
        nx, ny = widget.getNTilesX(), widget.getNTilesY()
        mode = widget.getMode()
        record_tilt = bool(getattr(widget, 'getRecordTiltCalibration', lambda: False)())
        tilt_only = record_tilt and bool(getattr(
            widget, 'getTiltCalibrationOnly', lambda: False)())
        bidirectional = record_tilt and bool(getattr(
            widget, 'getTiltBidirectional', lambda: False)())
        samples = (getattr(widget, 'getTiltSamplingCounts', lambda: (nx, ny))()
                   if record_tilt else (nx, ny))
        sample_nx, sample_ny = map(int, samples)
        step = widget.getTileStepUm()
        pattern = widget.getPattern()
        overview_choice = getattr(widget, 'getBuildOverview', lambda: True)()
        # Legacy test/widget adapters can return None for unimplemented options.
        build_overview = True if overview_choice is None else bool(overview_choice)
        if tilt_only:
            calibration_visits(nx, ny, step, sample_nx, sample_ny, pattern)
        apply_tilt = bool(getattr(widget, 'getApplyTiltCalibration', lambda: False)())
        precheck_tilt = bool(getattr(widget, 'getPrecheckTiltCompensation',
                                     lambda: False)())
        tilt_plane = None
        if record_tilt and apply_tilt:
            raise RuntimeError('Cannot record and apply tilt compensation simultaneously')
        if (record_tilt or apply_tilt) and mode != MODE_FREE_RUNNING:
            raise RuntimeError('Tilt calibration and compensation need free-running tiling')
        focus = getattr(self._commChannel, '_focusLock', None)
        if record_tilt:
            self._updateTiltRecordActuator()
            if focus is None or focus.focusLockState() != 'locked':
                raise RuntimeError('Start focus lock before recording a stage tilt calibration')
        if apply_tilt:
            path = getattr(widget, 'getTiltCalibrationPath', lambda: '')()
            if not path:
                raise RuntimeError('Load a tilt calibration before enabling compensation')
            tilt_plane = self._validateSelectedTiltPlane(path)
            if (focus is not None and focus.positioner == tilt_plane.focus_positioner
                    and focus.focusLockState() != 'unlocked'):
                raise RuntimeError(
                    'Unlock focus feedback when it uses the compensation Z actuator'
                )
        return TilingRunSettings(
            n_tiles_x=nx,
            n_tiles_y=ny,
            pattern=pattern,
            step_um=step,
            blend_overlaps=widget.getBlendOverlaps(),
            intensity_correction=widget.getIntensityCorrection(),
            settle_s=max(0.0, widget.getSettleTimeMs() / 1000.0),
            register_tiles=widget.getRegisterTiles(),
            orientation=widget.getTileOrientation(),
            save_tiles=widget.getSaveTiles(),
            mode=mode,
            scan_source_key=widget.getScanSource(),
            detector_name=widget.getDetector(),
            build_overview=build_overview,
            record_tilt=record_tilt,
            tilt_record_name=(getattr(widget, 'getTiltRecordName', lambda: '')()
                              if record_tilt else ''),
            tilt_calibration_only=tilt_only,
            tilt_bidirectional=bidirectional,
            tilt_sample_nx=sample_nx,
            tilt_sample_ny=sample_ny,
            tilt_plane=tilt_plane,
            precheck_tilt=precheck_tilt,
        )

    def _resetRunState(self, settings: TilingRunSettings) -> None:
        """Forget the previous run's mosaic and layout."""
        self._stitcher = None
        self._originXY = None
        self._gridPositions = []
        self._lastStepUm = settings.step_um
        self._registrationReport = None
        self._orientation = settings.orientation

    @staticmethod
    def _runScanArgs(tilingInfo, settings: TilingRunSettings) -> tuple:
        return (tilingInfo, settings.n_tiles_x, settings.n_tiles_y,
                settings.pattern, settings.step_um, settings.blend_overlaps,
                settings.intensity_correction)

    @staticmethod
    def _runScanKwargs(settings: TilingRunSettings, **overrides) -> dict:
        # By keyword: this argument list is long enough that inserting one in
        # the middle would otherwise silently shift the rest along.
        kwargs = dict(
            settle_s=settings.settle_s,
            register_tiles=settings.register_tiles,
            orientation=settings.orientation,
            save_tiles=settings.save_tiles,
            mode=settings.mode,
            scan_source_key=settings.scan_source_key,
            detector_name=settings.detector_name,
        )
        # Preserve the existing workflow argument contract when the new options
        # have their defaults; this also avoids surprising external adapters.
        if not settings.build_overview:
            kwargs['build_overview'] = False
        if settings.record_tilt:
            kwargs.update(
                record_tilt=True,
                tilt_record_name=settings.tilt_record_name,
                tilt_bidirectional=settings.tilt_bidirectional,
            )
        if settings.tilt_plane is not None:
            kwargs.update(
                tilt_plane=settings.tilt_plane,
                precheck_tilt=settings.precheck_tilt,
            )
        kwargs.update(overrides)
        return kwargs

    def runTilingForWorkflow(self, settings: TilingRunSettings, saveFolder, *,
                             illuminationGate=None) -> TilingRunResult:
        """Run one tiling on the calling thread and say how it ended.

        For a workflow that runs tilings itself, such as a timelapse of
        tilings. ``settings`` is a :meth:`snapshotRunSettings` taken on the GUI
        thread (one snapshot can serve many runs). The run always saves, into
        ``saveFolder``, whatever the panel's Save tiles box says: a workflow
        that names the folder wants the dataset there.

        ``illuminationGate`` is duck-typed: ``before_tile(index)`` is called
        after the move and settle, right before the tile's frames are taken,
        and ``after_tile(index)`` as soon as every image the tile needs is in
        hand -- before stitching, alignment and disk writes, so the light is
        not on while the computer works. Every ``before_tile`` is paired with
        exactly one ``after_tile``, on every exit path. ``before_tile``
        raising aborts the run; ``after_tile`` raising stops it. Only
        free-running runs use the gate: in triggered mode the scan arms its
        own lasers per tile.

        Must not be called on the GUI thread: a saving run in triggered mode
        waits for recording terminals that are delivered there, so blocking it
        would deadlock. Refused (with the reason in the result) while a run is busy.
        """
        if threading.current_thread() is threading.main_thread():
            raise RuntimeError(
                'runTilingForWorkflow blocks until the run ends; call it from '
                'a worker thread, not the GUI thread.'
            )
        if settings.tilt_calibration_only:
            return TilingRunResult(
                False, False, 'Calibration-only tilt recording is not a tile-acquisition workflow.'
            )
        tilingInfo = self._setupInfo.tiling
        if tilingInfo is None:
            return TilingRunResult(
                False, False, 'This setup has no tiling section.'
            )
        with self._getActivityLock():
            closed = getattr(self, '_closed', False)
            busy = self._scanning or closed
            if not busy:
                self._scanning = True
                self._stopRequested = False
                self._scanThread = threading.current_thread()
        if busy:
            reason = (
                'Tiling is closed.' if closed
                else 'A tiling run is already in progress.'
            )
            return TilingRunResult(False, False, reason)

        self._lastRunResult = None
        try:
            self._resetRunState(settings)
            self.sigRunningChanged.emit(True)
            self.sigProgressUpdated.emit(0, settings.n_tiles)
            self.sigRegistrationSummary.emit('')
            # The markers belong to the overview being replaced; the targets
            # themselves stay and are drawn again on the new one.
            self._refreshTargets()
        except Exception as e:
            with self._getActivityLock():
                self._scanning = False
                self._scanThread = None
            return TilingRunResult(
                False, False, f'Tiling could not start: {e}'
            )

        # _runScan owns the claim from here on and releases it in its finally.
        self._runScan(
            *self._runScanArgs(tilingInfo, settings),
            **self._runScanKwargs(
                settings,
                save_tiles=True,
                save_folder=Path(saveFolder),
                illumination_gate=illuminationGate,
            ),
        )
        result = self.__dict__.get('_lastRunResult')
        if result is None:
            return TilingRunResult(
                False, bool(self._stopRequested),
                'The tiling run ended without reporting a result.',
            )
        return result

    @APIExport()
    def stopTiling(self) -> None:
        """ Asks the running tiling run to stop. """
        self._stopRequested = True

    def closeEvent(self) -> bool:
        """Request cancellation and wait briefly for owned workers.

        The acquisition handle remains owned by ``_runScan`` until that worker
        actually exits. This avoids disarming the camera underneath a blocked
        ``getLatestFrame`` call when a hardware backend is slow to cancel.
        """
        with self._getActivityLock():
            self._closed = True
            self._stopRequested = True
        thread = getattr(self, '_scanThread', None)
        if thread is not None and thread is not threading.current_thread():
            thread.join(_CLOSE_JOIN_TIMEOUT_S)
            if thread.is_alive():
                self._logger.warning(
                    'scanThread is still stopping after widget close; '
                    'owned hardware will be released when it exits.'
                )
        super().closeEvent()
        return self.shutdownComplete()

    @staticmethod
    def _threadIsAlive(thread) -> bool:
        return bool(thread is not None and thread.is_alive())

    def shutdownComplete(self) -> bool:
        """Return whether all stage/camera workers have left their finally."""
        with self._getActivityLock():
            return (
                not self._threadIsAlive(self.__dict__.get('_scanThread'))
                and self.__dict__.get('_scanAcqHandle') is None
                and not self.__dict__.get('_scanning', False)
            )

    def _getActivityLock(self):
        lock = self.__dict__.get('_activityLock')
        if lock is None:
            lock = threading.Lock()
            self._activityLock = lock
        return lock

    @APIExport(runOnUIThread=True)
    def setTileLabel(self, label: str) -> None:
        """ Sets the label the Tiling widget puts into the tiles' names. """
        self._widget.setLabel(label)

    @APIExport()
    def getRegistrationSummary(self) -> str:
        """Diagnostics from the last run with tile alignment enabled."""
        report = self.__dict__.get('_registrationReport')
        return report.summary() if report is not None else ''

    @APIExport()
    def isTilingRunning(self) -> bool:
        """ Whether a tiling run is in progress. True from the moment
        ``startTiling`` accepted the run until its tiles, mosaic and manifest
        are written and the stage is back at the start, so a script can wait
        for it: ``waitUntil(lambda: not api.imcontrol.isTilingRunning())``. """
        with self._getActivityLock():
            return bool(self.__dict__.get('_scanning', False))

    @APIExport()
    def getLastTilingFolder(self) -> str:
        """ The folder the last saved tiling run was written to (it holds
        ``tiles.json``), or an empty string when no run saved its tiles in this
        session. A run with *Save tiles* off leaves it unchanged. """
        return str(self.__dict__.get('_lastSaveFolder') or '')

    @APIExport()
    def getStitchedImage(self) -> Optional[np.ndarray]:
        """ Returns the stitched overview of the tiling run as an array, or
        None before the first run. """
        if self._stitcher is None:
            return None
        return self._stitcher.get_overview()

    # ------------------------------------------------------------------
    # Scan loop (runs in background thread)
    # ------------------------------------------------------------------

    def _tiltSnapshot(self):
        try:
            return self._commChannel.getTiltCalibrationSnapshot()
        except Exception as error:
            self._logger.warning('Tiling: focus snapshot unavailable: %s', error)
            return {'lock_state': 'snapshot_error', 'snapshot_error': str(error)}

    def _recordTiltPoint(self, recorder, *, tile_index, gx, gy,
                         x_um, y_um, pass_index=1):
        recorder.record(
            tile_index=tile_index, gx=gx, gy=gy,
            x_um=x_um, y_um=y_um,
            focus=self._tiltSnapshot(), pass_index=pass_index,
        )

    def _finishTiltRecorder(self, recorder, *, completed):
        """Shared finalization of calibration-only and imaging+calibration."""
        try:
            result = recorder.finish(completed=completed)
            if result['fit'] is not None:
                fit = result['fit']
                if result['completed'] and not getattr(self, '_closed', False):
                    self.sigTiltCalibrationCreated.emit(str(recorder.folder / 'tilt_fit.json'))
                self._logger.info(
                    'Tiling: tilt calibration %s: dZ/dX=%g, dZ/dY=%g, '
                    'RMS=%g µm, %d fit positions',
                    recorder.folder, fit['slope_x_um_per_um'],
                    fit['slope_y_um_per_um'], fit['rms_residual_um'],
                    fit['n_fit_points'],
                )
                drift = result['origin_drift']
                if drift['available']:
                    self._logger.info('Tiling: origin Z drift %+g µm', drift['delta_z_um'])
            else:
                self._logger.warning(
                    'Tiling: calibration measurements saved to %s, '
                    'but insufficient valid XY/Z points for a plane', recorder.folder,
                )
        except Exception as error:
            self._logger.error('Tiling: could not finalize tilt report: %s',
                               error, exc_info=True)

    def _runTiltCalibrationOnly(self, tilingInfo, n_tiles_x, n_tiles_y,
                                pattern, step_um, *, sample_nx, sample_ny,
                                bidirectional, settle_s, tilt_record_name):
        """Measure a sparse full-area grid without acquiring tile-camera frames."""
        positioner = None
        axes = ()
        applied_um = [0.0, 0.0]
        origin_xy = None
        recorder = None
        completed = False
        restored = False
        try:
            positioner = self._master.positionersManager[tilingInfo.xyPositioner]
            axes = tuple(self._setupInfo.positioners[tilingInfo.xyPositioner].axes[:2])
            if len(axes) != 2:
                raise RuntimeError('Tilt calibration requires two XY stage axes')
            focus = getattr(self._commChannel, '_focusLock', None)
            if focus is None or focus.focusLockState() != 'locked':
                raise RuntimeError('Focus lock must remain engaged for tilt calibration')
            z_actuator = focus.positioner
            visits = calibration_visits(n_tiles_x, n_tiles_y, step_um,
                                        sample_nx, sample_ny, pattern)
            recorder = StageTiltRecorder(
                new_tilt_calibration_folder(tilt_record_name), step_um=step_um,
                settle_s=settle_s, focus_positioner=z_actuator,
                xy_positioner=tilingInfo.xyPositioner, n_tiles=len(visits),
                setup_file_name=getattr(self, '_setupNameForTilt', ''),
                bidirectional=bidirectional, calibration_only=True,
                sampling_grid_xy=(sample_nx, sample_ny),
                area_span_um=((n_tiles_x - 1) * step_um,
                              (n_tiles_y - 1) * step_um),
            )
            if tilt_record_name:
                save_calibration_name(recorder.folder / 'tilt_fit.json', tilt_record_name)
            self._syncPositionerFromHardware(positioner)
            start = positioner.position
            origin_xy = (float(start[axes[0]]), float(start[axes[1]]))
            self._originXY = origin_xy
            time.sleep(settle_s)
            if self._stopRequested:
                return
            recorder.record_origin(
                focus=self._tiltSnapshot(), x_um=origin_xy[0], y_um=origin_xy[1]
            )
            order = [(1, point) for point in visits]
            if bidirectional:
                order += [(2, point) for point in reversed(visits)]
            for index, (pass_index, (gx, gy, x_offset, y_offset)) in enumerate(order):
                if self._stopRequested:
                    break
                for axis_index, target in enumerate((x_offset, y_offset)):
                    delta = target - applied_um[axis_index]
                    if abs(delta) > 1e-10:
                        positioner.move(delta, axes[axis_index])
                        applied_um[axis_index] += delta
                # First point also gets the normal settle interval; reverse
                # starts at the last forward point with no intervening return.
                time.sleep(settle_s)
                if self._stopRequested:
                    break
                self._recordTiltPoint(
                    recorder, tile_index=index + 1, gx=gx, gy=gy,
                    x_um=origin_xy[0] + x_offset,
                    y_um=origin_xy[1] + y_offset, pass_index=pass_index,
                )
                if not getattr(self, '_closed', False):
                    self.sigProgressUpdated.emit(index + 1, len(order))
            else:
                completed = not self._stopRequested
        except Exception as error:
            self._logger.error('Tiling: calibration-only run failed: %s',
                               error, exc_info=True)
        finally:
            restored = origin_xy is not None
            if positioner is not None and len(axes) == 2:
                for index, axis in enumerate(axes):
                    if abs(applied_um[index]) <= 1e-10:
                        continue
                    try:
                        positioner.move(-applied_um[index], axis)
                        applied_um[index] = 0.0
                    except Exception as error:
                        restored = False
                        self._logger.error('Tiling: failed to return %s: %s',
                                           axis, error, exc_info=True)
            if recorder is not None:
                if restored and origin_xy is not None:
                    # Diagnostic only. The stage was commanded home, not
                    # independently verified to have physically arrived.
                    try:
                        time.sleep(settle_s)
                        recorder.record_origin(
                            focus=self._tiltSnapshot(), x_um=origin_xy[0],
                            y_um=origin_xy[1], final=True,
                        )
                    except Exception as error:
                        # The return diagnostics must not prevent saving the
                        # measurements or releasing ownership of this worker.
                        self._logger.warning(
                            'Tiling: final origin Z unavailable: %s', error
                        )
                self._finishTiltRecorder(recorder, completed=completed and restored)
            with self._getActivityLock():
                self._scanAcqHandle = None
                self._scanning = False
                self._scanThread = None
            self._commChannel._tilingActive = False
            if not getattr(self, '_closed', False):
                self.sigRunningChanged.emit(False)

    def _runScan(
        self,
        tilingInfo,
        n_tiles_x: int,
        n_tiles_y: int,
        pattern: str,
        step_um: float,
        blend_overlaps: bool,
        intensity_correction: bool,
        settle_s: float = _SETTLE_S,
        register_tiles: bool = False,
        orientation: Tuple[bool, bool, bool] = (False, False, False),
        save_tiles: bool = False,
        build_overview: bool = True,
        record_tilt: bool = False,
        tilt_record_name: str = '',
        tilt_bidirectional: bool = False,
        tilt_plane=None,
        precheck_tilt: bool = False,
        mode: str = MODE_FREE_RUNNING,
        scan_source_key=None,
        detector_name=None,
        save_folder=None,
        illumination_gate=None,
    ) -> None:
        # The grid's total, which is what progress, the payload session and the
        # last-tile test all count. Derived rather than passed so it cannot
        # disagree with the pattern actually being walked.
        n_tiles = n_tiles_x * n_tiles_y
        # No overview means no image registration or in-memory mosaic.
        register_tiles = bool(register_tiles and build_overview)
        acqHandle = None
        positioner = None
        detector = None
        x_axis = None
        y_axis = None
        origin_xy = None
        scan_completed = False
        # Displacement actually commanded onto each axis, in µm. The scan
        # unwinds this with relative moves instead of an absolute move back to
        # the recorded origin: a positioner's tracked position and its
        # controller's absolute frame need not share an origin (they did not on
        # the Marzhauser stage), and an absolute move to a tracked coordinate
        # then flung the stage across its travel range at the end of every run.
        applied_um = {}
        staleFrames = 0
        frameFailure = False
        registration = RegistrationReport()
        self._alignmentLinks = []
        self._alignmentIndex = {}
        maxShiftPx = 0.0
        dataset = TileDataset()
        saveFolder = None
        tiltRecorder = None
        tiltRecordingFailed = False
        compensationRecorder = None
        focusForCompensation = None
        focusZPositioner = None
        compensationSharesFocusZ = False
        originZ = None
        lastCompensatedZ = None
        zCommandAttempted = False
        compensationFailure = ''
        tileSource = None
        dispatcher = None
        tilesAcquired = 0
        runMessage = ''
        # The caller's illumination gate, in free-running mode only: in
        # triggered mode the scan arms its own lasers for every tile.
        gate = illumination_gate if mode != MODE_TRIGGERED else None
        if illumination_gate is not None and gate is None:
            self._logger.info(
                'Tiling: triggered mode -- the scan arms its own lasers per '
                'tile, so the illumination gate is not used.'
            )
        try:
            positioner = self._master.positionersManager[tilingInfo.xyPositioner]
            axes = list(self._setupInfo.positioners[tilingInfo.xyPositioner].axes)
            x_axis, y_axis = axes[0], axes[1]
            applied_um = {x_axis: 0.0, y_axis: 0.0}

            # Precedence: this run's widget selection, then the setup file,
            # then the first acquisition detector. The widget only returns a
            # name when the setup actually offered a choice, so a single-camera
            # rig behaves exactly as before.
            camera = (detector_name
                      or tilingInfo.camera
                      or self._defaultCamera())
            detector = self._master.detectorsManager[camera]

            # Tiling used to arm nothing at all and silently depend on live
            # view being on: with live view off, getLatestFrame() below
            # returned stale or empty frames. A WORKFLOW lease over the whole
            # loop arms the camera for the scan's duration and releases it in
            # the finally, whatever path the loop exits by.
            tileSource = self._makeTileSource(
                mode, detector, tilingInfo, scan_source_key
            )

            # Pinned once, before the first tile: the manifest describes one
            # set of detectors for the whole dataset, so it must not follow the
            # Recording widget if the operator changes it mid-run.
            saveSet = self._resolveSaveSet(tilingInfo, camera, mode)

            # Every detector in the set is leased for the whole run, not just
            # the one being aligned on. An unleased detector is not armed, and
            # would hand back whatever was last in its buffer. Leases are
            # reference-counted, so this composes with anything else holding
            # them.
            acqHandle = self._master.detectorsManager.acquire(
                saveSet, LeasePurpose.WORKFLOW
            )
            with self._getActivityLock():
                self._scanAcqHandle = acqHandle

            # Payloads mean a recording per tile, and a recording per tile
            # means the session -- not tiling -- dispatches the scans. Only
            # triggered mode: free-running has no scan for a session to run,
            # and its payload is the snapshot it already saves.
            if save_tiles or tilt_plane is not None:
                # Allocated before the first acquisition, not on the first
                # tile: a recording session started before the folder existed
                # would write its payloads somewhere else, and every locator
                # in the manifest would then point outside the dataset.
                saveFolder = (
                    Path(save_folder) if save_folder is not None
                    else tiling_folder(self._saveRoot(tilingInfo))
                )
                saveFolder.mkdir(parents=True, exist_ok=True)
                self._logger.info(f'Tiling: saving to {saveFolder}')

            if record_tilt:
                focus = getattr(self._commChannel, '_focusLock', None)
                if focus is None or focus.focusLockState() != 'locked':
                    raise RuntimeError(
                        'Focus lock must remain engaged throughout tilt recording'
                    )
                focus_z_name = getattr(focus, 'positioner', '')
                if not focus_z_name:
                    raise RuntimeError('Focus actuator unavailable for tilt recording')
                calibrationFolder = new_tilt_calibration_folder(tilt_record_name)
                tiltRecorder = StageTiltRecorder(
                    calibrationFolder, step_um=step_um, settle_s=settle_s,
                    focus_positioner=focus_z_name,
                    xy_positioner=tilingInfo.xyPositioner, n_tiles=n_tiles,
                    setup_file_name=getattr(self, '_setupNameForTilt', ''),
                    bidirectional=tilt_bidirectional,
                    sampling_grid_xy=(n_tiles_x, n_tiles_y),
                    area_span_um=((n_tiles_x - 1) * step_um,
                                  (n_tiles_y - 1) * step_um),
                )
                if tilt_record_name:
                    try:
                        save_calibration_name(
                            calibrationFolder / 'tilt_fit.json', tilt_record_name
                        )
                    except (OSError, ValueError, TypeError) as error:
                        self._logger.warning(
                            'Could not save tilt calibration display name: %s', error
                        )

            if save_tiles and mode == MODE_TRIGGERED:
                dispatcher = self._startPayloadSession(
                    saveSet, n_tiles, tileSource, saveFolder
                )

            tileSource.prepare()

            # Give a newly armed camera one normal settle interval before the
            # first tile. Subsequent tiles already wait after each stage move;
            # previously only the first tile could be the pre-acquisition
            # cached frame when live view had been off.
            time.sleep(settle_s)
            if self._stopRequested:
                return

            # Record starting position as grid origin (0, 0). Re-read hardware
            # first where the positioner supports it: the overview's stage
            # anchor is only as good as this number, and a joystick nudge
            # between runs moves the stage without the manager noticing.
            self._syncPositionerFromHardware(positioner)
            start_pos = positioner.position
            origin_xy = (start_pos[x_axis], start_pos[y_axis])
            self._originXY = origin_xy
            if tiltRecorder is not None:
                tiltRecorder.record_origin(
                    focus=self._tiltSnapshot(), x_um=origin_xy[0], y_um=origin_xy[1]
                )

            if tilt_plane is not None:
                focusForCompensation = getattr(self._commChannel, '_focusLock', None)
                compensationSharesFocusZ = bool(
                    focusForCompensation is not None
                    and focusForCompensation.positioner == tilt_plane.focus_positioner
                )
                if (compensationSharesFocusZ
                        and focusForCompensation.focusLockState() != 'unlocked'):
                    raise RuntimeError('Focus feedback and compensation use the same Z actuator')
                focusZPositioner = self._master.positionersManager[
                    tilt_plane.focus_positioner
                ]
                originZ = float(focusZPositioner.get_abs('Z'))
                lastCompensatedZ = originZ
                # Basic finite-value and availability checks remain unconditional.
                validate_z_target(focusZPositioner, originZ)
                if precheck_tilt:
                    tilt_plane.check_grid(
                        origin_xy=origin_xy,
                        moves=tile_moves(n_tiles_x, n_tiles_y, pattern),
                        step_um=step_um,
                    )
                    validate_z_target(focusZPositioner, originZ, strict_limits=True)
                    gx_check = gy_check = 0
                    for dx_check, dy_check in tile_moves(n_tiles_x, n_tiles_y, pattern):
                        gx_check += dx_check
                        gy_check += dy_check
                        validate_z_target(
                            focusZPositioner,
                            originZ + tilt_plane.delta_z(
                                gx_check * step_um, gy_check * step_um
                            ),
                            strict_limits=True,
                        )
                # The focus camera is optional for feed-forward. When present,
                # retain the first usable estimate for offline validation.
                reference_signal = None
                if (focusForCompensation is not None
                        and focusForCompensation.focusCameraAcquisitionActive()):
                    try:
                        firstFocus = self._commChannel.getTiltCalibrationSnapshot()
                        reference_signal = require_reference_signal(
                            firstFocus, allow_locked=not compensationSharesFocusZ
                        )
                    except (RuntimeError, ValueError, TypeError) as error:
                        self._logger.warning(
                            'Tiling: tilt validation lacks first focus reference: %s', error
                        )
                compensationRecorder = TiltCompensationRecorder(
                    saveFolder, plane=tilt_plane, origin_xy=origin_xy,
                    origin_z_um=originZ, origin_signal_px=reference_signal,
                    n_tiles=n_tiles,
                )

            gx, gy = 0, 0
            forward_tilt_positions = []
            lastEmit = 0.0
            for i, (dx, dy) in enumerate(
                tile_moves(n_tiles_x, n_tiles_y, pattern)
            ):
                if self._stopRequested:
                    break

                # Move to next tile (skip move for the very first position)
                if i > 0:
                    positioner.move(dx * step_um, x_axis)
                    applied_um[x_axis] += dx * step_um
                    positioner.move(dy * step_um, y_axis)
                    applied_um[y_axis] += dy * step_um
                    if tilt_plane is not None:
                        if (compensationSharesFocusZ and
                                focusForCompensation.focusLockState() != 'unlocked'):
                            raise RuntimeError('Focus feedback engaged on compensation Z during tiling')
                        # Use an absolute target relative to the run's Z origin.
                        # get_abs() may be physical readback (e.g. Leica objective)
                        # and need not reach a command before this loop continues.
                        # Recomputing from the XY offset also avoids accumulating
                        # round-off and uncompleted relative Z movements.
                        target_z = originZ + tilt_plane.delta_z(
                            (gx + dx) * step_um, (gy + dy) * step_um
                        )
                        validate_z_target(focusZPositioner, target_z)
                        if abs(target_z - lastCompensatedZ) > 1e-9:
                            zCommandAttempted = True
                            result = focusZPositioner.setPosition(target_z, 'Z')
                            if result is False:
                                raise RuntimeError(
                                    f'Compensation Z rejected target {target_z:g} µm'
                                )
                            lastCompensatedZ = target_z
                    time.sleep(settle_s)
                    if self._stopRequested:
                        break

                gx += dx
                gy += dy

                # The same identity a snapshot tile carries, so a payload
                # recorded for this tile says which tile it is and where the
                # stage was commanded -- not only the manifest beside it.
                tileSource.pointAttributes = self._tileAttrs(
                    gx, gy,
                    (origin_xy[0] + gx * step_um, origin_xy[1] + gy * step_um),
                )
                # Observe after the *existing* settle interval, immediately
                # before acquisition. Never wait extra or control the focus Z.
                if tiltRecorder is not None and not tiltRecordingFailed:
                    try:
                        self._recordTiltPoint(
                            tiltRecorder, tile_index=i + 1, gx=gx, gy=gy,
                            x_um=origin_xy[0] + gx * step_um,
                            y_um=origin_xy[1] + gy * step_um, pass_index=1,
                        )
                        forward_tilt_positions.append((gx, gy))
                    except Exception as error:
                        tiltRecordingFailed = True
                        self._logger.error('Tiling: could not record tilt sample: %s',
                                           error, exc_info=True)
                if compensationRecorder is not None:
                    if (compensationSharesFocusZ and
                            focusForCompensation.focusLockState() != 'unlocked'):
                        raise RuntimeError('Focus feedback engaged on compensation Z during tiling')
                    try:
                        snapshot = self._commChannel.getTiltCalibrationSnapshot()
                    except (RuntimeError, AttributeError, ValueError):
                        snapshot = {'lock_state': 'unavailable'}
                    compensationRecorder.record(
                        tile_index=i + 1, gx=gx, gy=gy,
                        x_um=origin_xy[0] + gx * step_um,
                        y_um=origin_xy[1] + gy * step_um,
                        commanded_z_um=lastCompensatedZ,
                        focus=snapshot,
                    )
                # Keep the illumination gate limited to acquiring actual frames.
                # Tilt snapshots above use the focus camera, not tile illumination.
                images = None
                gateFailure = None
                lit = False
                try:
                    if gate is not None:
                        # A partially failed before_tile may already have lit
                        # hardware, so after_tile must still be invoked.
                        lit = True
                        gate.before_tile(i)
                    frame, wasFresh = tileSource.acquire()
                    if frame is not None and save_tiles:
                        images = self._captureSaveSet(
                            saveSet, camera, frame, tileSource
                        )
                finally:
                    if lit:
                        try:
                            gate.after_tile(i)
                        except Exception as e:
                            gateFailure = (
                                f'The illumination gate failed after tile '
                                f'{i + 1}/{n_tiles}: {e}'
                            )
                            self._logger.error(
                                f'Tiling: {gateFailure}. The lasers may still '
                                'be on; stopping the run.', exc_info=True,
                            )
                if gateFailure is not None:
                    runMessage = gateFailure
                    break
                if frame is None:
                    self._logger.error(
                        'Tiling: no frame available for tile '
                        f'{i + 1}/{n_tiles}; aborting scan'
                    )
                    runMessage = (
                        f'No frame was available for tile {i + 1}/{n_tiles}.'
                    )
                    frameFailure = True
                    break
                tilesAcquired += 1
                if not wasFresh:
                    staleFrames += 1

                # Keep the original frame for recording. A 2-D projection is
                # only needed for the mosaic or an alignment-sidecar image.
                displayFrame = (
                    self._displayPlane(frame)
                    if build_overview or save_tiles else None
                )

                if build_overview:
                    # The live canvas is optional: large saved-only runs must
                    # not allocate one mosaic-sized array per working buffer.
                    if self._stitcher is None:
                        pixel_size_um = self._detectorPixelSizeUm(detector)
                        self._stitcher = StitchedImage(
                            tile_size_px=None,
                            tile_step_um=step_um,
                            px_per_um=None,
                            tile_shape_px=displayFrame.shape[:2],
                            pixel_size_um=pixel_size_um,
                            blend_overlaps=blend_overlaps,
                            intensity_correction=intensity_correction,
                        )
                        maxShiftPx = max_shift_for_step(
                            self._stitcher.step_y_px,
                            self._stitcher.step_x_px,
                            getattr(tilingInfo, 'registrationMaxShiftFraction', 0.5),
                        )
                        if register_tiles and (
                            self._stitcher.step_x_px >= displayFrame.shape[1]
                            or self._stitcher.step_y_px >= displayFrame.shape[0]
                        ):
                            register_tiles = False
                            self._logger.warning(
                                'Tiling: tile alignment needs the tiles to overlap, '
                                f'but the {step_um:g} µm step is at least a full '
                                'field of view. Reduce the step, then re-enable it.'
                            )

                    ix, iy = self._gridToImage(gx, gy, orientation)
                    offset_px = (0.0, 0.0)
                    if register_tiles:
                        offset_px = self._registerTile(
                            displayFrame, ix, iy, registration, maxShiftPx
                        )
                    self._stitcher.add_tile(displayFrame, ix, iy, offset_px)
                    self._gridPositions.append((gx, gy))

                if save_tiles:
                    if not dataset.tiles and dataset.tile_shape_px == (0, 0):
                        dataset.pixel_size_um = self._detectorPixelSizeUm(detector)
                        dataset.step_um = step_um
                        dataset.tile_shape_px = tuple(displayFrame.shape[:2])
                        dataset.tile_depth = (
                            int(frame.shape[0]) if frame.ndim > 2 else 1
                        )
                        if dataset.tile_depth > 1:
                            dataset.z_step_um = self._detectorZStepUm(detector)
                        dataset.orientation = orientation
                    stage_xy = (
                        origin_xy[0] + gx * step_um,
                        origin_xy[1] + gy * step_um,
                    )
                    self._saveTile(
                        images, camera, gx, gy, stage_xy, dataset, saveFolder,
                        alignmentImage=displayFrame,
                        # Where this tile's recording finalised, if one ran.
                        # Read after its terminal, never guessed at dispatch.
                        payloads=(dispatcher.locatorsForLastPoint()
                                  if dispatcher is not None else None),
                    )

                # Repainting the whole mosaic on the GUI thread after every
                # tile starved other GUI-thread work — most visibly the focus
                # lock, whose PI update runs on the same event loop. Throttle
                # to a human-perceptible rate; the last tile always emits.
                now = time.monotonic()
                isLastTile = (i + 1) >= n_tiles
                shouldEmit = (
                    isLastTile
                    or (now - lastEmit) >= _OVERVIEW_EMIT_INTERVAL_S
                )
                if not getattr(self, '_closed', False):
                    if build_overview and shouldEmit:
                        lastEmit = now
                        self.sigOverviewUpdated.emit(self._stitcher.get_overview())
                    self.sigProgressUpdated.emit(
                        i + 1, n_tiles * (2 if tilt_bidirectional else 1)
                    )

            reverse_completed = True
            if (tiltRecorder is not None and tilt_bidirectional
                    and not self._stopRequested and not frameFailure
                    and not tiltRecordingFailed):
                if len(forward_tilt_positions) != n_tiles:
                    reverse_completed = False
                else:
                    for index, (next_gx, next_gy) in enumerate(
                            reversed(forward_tilt_positions)):
                        if self._stopRequested:
                            reverse_completed = False
                            break
                        dx_back = next_gx - gx
                        dy_back = next_gy - gy
                        for axis, diff in ((x_axis, dx_back), (y_axis, dy_back)):
                            if diff:
                                positioner.move(diff * step_um, axis)
                                applied_um[axis] += diff * step_um
                        gx, gy = next_gx, next_gy
                        # The first reverse reading is taken at the last
                        # forward position, with no return-to-start jump.
                        time.sleep(settle_s)
                        if self._stopRequested:
                            reverse_completed = False
                            break
                        try:
                            self._recordTiltPoint(
                                tiltRecorder, tile_index=n_tiles + index + 1,
                                gx=gx, gy=gy,
                                x_um=origin_xy[0] + gx * step_um,
                                y_um=origin_xy[1] + gy * step_um, pass_index=2,
                            )
                        except Exception as error:
                            tiltRecordingFailed = True
                            reverse_completed = False
                            self._logger.error('Tiling: reverse tilt sample failed: %s',
                                               error, exc_info=True)
                            break
                        if not getattr(self, '_closed', False):
                            self.sigProgressUpdated.emit(
                                n_tiles + index + 1, 2 * n_tiles
                            )
            elif tiltRecorder is not None and tilt_bidirectional:
                reverse_completed = False

            scan_completed = (not self._stopRequested and not frameFailure
                              and reverse_completed and not tiltRecordingFailed
                              and not runMessage)

            if register_tiles and self._stitcher is not None:
                # Before anything is written: the solved layout is the one that
                # should reach the mosaic, the sidecars and the operator's
                # screen, not the one the live pass arrived at tile by tile.
                self._resolveLayout(registration)
                self._syncDatasetPlacements(dataset)
                if not getattr(self, '_closed', False):
                    self.sigOverviewUpdated.emit(self._stitcher.get_overview())

            # Intensity correction solved each tile on arrival; with the whole
            # run in hand it is solved again, before the mosaic is written.
            if (self._stitcher is not None and self._stitcher.finish()
                    and not getattr(self, '_closed', False)):
                self.sigOverviewUpdated.emit(self._stitcher.get_overview())

            if save_tiles and saveFolder is not None:
                # The mosaic and the sidecars are written even for a stopped or
                # partial run: whatever tiles reached disk should still be a
                # usable dataset rather than orphaned files.
                if build_overview:
                    self._saveMosaic(camera, saveFolder, self._canvasOriginStage())
                dataset.extra['completed'] = bool(scan_completed)
                # Which stage the stage_um coordinates belong to, so a script
                # or processor can drive it back to a tile without guessing.
                dataset.extra['xy_positioner'] = tilingInfo.xyPositioner
                dataset.extra['xy_axes'] = [x_axis, y_axis]
                written = dataset.write(saveFolder)
                self._logger.info(
                    f'Tiling: saved {len(dataset.tiles)} tiles'
                    f'{", the mosaic" if build_overview else " (no mosaic)"} and '
                    f'{len(written)} sidecar file(s) to {saveFolder}'
                )
                self._lastSaveFolder = str(saveFolder)

            if register_tiles:
                self._registrationReport = registration
                summary = registration.summary()
                self._logger.info(summary)
                if not getattr(self, '_closed', False):
                    self.sigRegistrationSummary.emit(summary)

            if staleFrames:
                self._logger.warning(
                    f'Tiling: {staleFrames}/{n_tiles} tiles fell back to the '
                    'latest buffered frame because no new frame arrived within '
                    f'{_FRESH_FRAME_TIMEOUT_S:g} s. Those tiles may be smeared '
                    'or belong to the previous position — check the camera '
                    'frame rate against the tile rate.'
                )

        except Exception as e:
            runMessage = f'Tiling failed: {e}'
            compensationFailure = str(e)
            self._logger.error(f'Tiling scan failed: {e}', exc_info=True)
        finally:
            # Once the origin is known, every exit path owns returning the
            # stage: normal completion, user cancellation, camera/stitching
            # failure, and close while the worker is active.
            #
            # The return is a relative unwind of exactly what the scan
            # commanded, so it lands correctly no matter where the positioner's
            # tracked frame sits relative to its controller's absolute frame,
            # and it stays correct when the scan stopped part-way through.
            origin_restored = origin_xy is not None
            if positioner is not None and applied_um:
                for axis in (x_axis, y_axis):
                    offset = applied_um.get(axis, 0.0)
                    if not offset:
                        continue
                    try:
                        positioner.move(-offset, axis)
                        applied_um[axis] = 0.0
                    except Exception as e:
                        origin_restored = False
                        self._logger.error(
                            f'Failed to restore tiling origin on axis '
                            f'"{axis}": {e}',
                            exc_info=True,
                        )

            if tiltRecorder is not None and origin_restored and origin_xy is not None:
                try:
                    time.sleep(settle_s)
                    tiltRecorder.record_origin(
                        focus=self._tiltSnapshot(), x_um=origin_xy[0],
                        y_um=origin_xy[1], final=True,
                    )
                except Exception as error:
                    self._logger.warning('Tiling: final origin Z unavailable: %s', error)

            z_restored = True
            if focusZPositioner is not None and originZ is not None:
                # Undo only the compensation we commanded, and only after a
                # successful XY return. Never fight another user of the same Z.
                try:
                    focus_is_safe = (not compensationSharesFocusZ or
                        focusForCompensation.focusLockState() == 'unlocked')
                    if not zCommandAttempted:
                        # Do not override an actuator we never commanded.
                        pass
                    elif origin_restored and focus_is_safe:
                        validate_z_target(focusZPositioner, originZ)
                        result = focusZPositioner.setPosition(originZ, 'Z')
                        if result is False:
                            raise RuntimeError('Compensation Z rejected return-to-origin command')
                        # A successful command is not proof of physical arrival.
                        self._logger.info('Tiling: requested compensation Z return to origin')
                    else:
                        z_restored = False
                        self._logger.warning(
                            'Tiling: skipped compensation Z restoration (XY not '
                            'at origin or shared focus Z engaged)'
                        )
                except Exception as error:
                    z_restored = False
                    self._logger.error(
                        f'Tiling: failed to restore compensation Z: {error}', exc_info=True
                    )

            if compensationRecorder is not None:
                try:
                    outcome = compensationRecorder.finish(
                        completed=scan_completed and origin_restored and z_restored,
                        failure=(compensationFailure or
                                 ('XY/Z origin restoration incomplete'
                                  if not (origin_restored and z_restored) else '')),
                    )
                    self._lastSaveFolder = str(saveFolder)
                    self._logger.info(
                        'Tiling: tilt validation saved to %s (%d/%d valid focus samples)',
                        saveFolder, outcome['n_valid_focus_samples'],
                        outcome['n_recorded_tiles'],
                    )
                except Exception as error:
                    self._logger.error(
                        f'Tiling: failed to save tilt validation: {error}', exc_info=True
                    )

            if dispatcher is not None:
                try:
                    self._endPayloadSession(dispatcher)
                except Exception as e:
                    self._logger.error(
                        f'Failed to close the tiling payload session: {e}',
                        exc_info=True,
                    )

            if tileSource is not None:
                try:
                    tileSource.finish()
                except Exception as e:
                    self._logger.error(
                        f'Failed to release the tiling tile source: {e}',
                        exc_info=True,
                    )

            if tiltRecorder is not None:
                self._finishTiltRecorder(
                    tiltRecorder,
                    completed=scan_completed and not tiltRecordingFailed and origin_restored,
                )

            if not getattr(self, '_closed', False):
                if scan_completed and not origin_restored:
                    self._logger.warning(
                        'Tiling: the stage did not return to the run origin; '
                        'targets map to where the tiles were taken.'
                    )
                # The overview is final now, so targets can be placed on it.
                self._refreshTargets()

            if acqHandle is not None:
                try:
                    self._master.detectorsManager.release(acqHandle)
                except Exception as e:
                    self._logger.error(
                        f'Failed to release tiling detector lease: {e}',
                        exc_info=True,
                    )
            if (tilt_plane is not None and not getattr(self, '_closed', False)
                    and (compensationFailure or not origin_restored or not z_restored)):
                self.sigTiltCompensationError.emit(
                    compensationFailure or 'XY/Z origin restoration incomplete'
                )
            stopped = bool(self._stopRequested)
            completed = bool(scan_completed and origin_restored and z_restored)
            if scan_completed and not (origin_restored and z_restored) and not runMessage:
                runMessage = 'The XY/Z stage could not be returned to the origin.'
            elif stopped and not runMessage:
                runMessage = 'Stopped.'
            self._lastRunResult = TilingRunResult(
                completed=completed,
                stopped=stopped,
                message=runMessage,
                folder=str(saveFolder) if saveFolder is not None else None,
                tiles_acquired=tilesAcquired,
                stale_tiles=staleFrames,
                illumination_gated=gate is not None,
            )
            with self._getActivityLock():
                self._scanAcqHandle = None
                self._scanning = False
                self._scanThread = None
            if hasattr(self, '_commChannel') and self._commChannel is not None:
                self._commChannel._tilingActive = False
            # Cross-thread emit — Qt's AutoConnection becomes QueuedConnection
            # because the sender (this background thread) lives in a different
            # thread than the receiver (widget on the GUI thread).  Previously
            # this used QMetaObject.invokeMethod which silently fails when the
            # target slot isn't registered with the Qt meta-object system
            # (setRunning is a plain Python method, not @Slot-decorated).
            if not getattr(self, '_closed', False):
                self.sigRunningChanged.emit(False)

    @staticmethod
    def _gridToImage(gx: int, gy: int, orientation) -> Tuple[int, int]:
        """Map a stage-grid position to the mosaic's image grid.

        The stage traces the same physical spiral regardless; this only decides
        where each tile is laid down in the overview. ``orientation`` is
        ``(flip_x, flip_y, swap_axes)``; swap is applied first.

        There are exactly **eight** ways a camera can sit relative to the stage
        axes, and these three booleans cover all of them. Counting it the way
        you would at the microscope: a positive stage X step can send the image
        in one of four directions (+col, -col, +row, -row); once that is fixed,
        Y must land on the perpendicular axis, leaving two choices. Four times
        two is eight — the symmetry group of the square, four rotations and
        their four mirror images. ``swap_axes`` picks which stage axis drives
        image columns and the two flips pick the signs, so ``2 * 2 * 2``
        enumerates the same eight.
        """
        flip_x, flip_y, swap_axes = orientation
        if swap_axes:
            gx, gy = gy, gx
        return (-gx if flip_x else gx, -gy if flip_y else gy)

    @staticmethod
    def _imageOffsetToStage(dx_um: float, dy_um: float, orientation):
        """Inverse of :meth:`_gridToImage` for a continuous µm offset.

        Both transforms are their own inverse up to the swap, which must be
        undone after the sign flips rather than before.
        """
        flip_x, flip_y, swap_axes = orientation
        stage_dx = -dx_um if flip_x else dx_um
        stage_dy = -dy_um if flip_y else dy_um
        if swap_axes:
            stage_dx, stage_dy = stage_dy, stage_dx
        return stage_dx, stage_dy

    def _saveTile(self, images, alignmentDetector, gx, gy, stage_xy, dataset,
                  folder, payloads=None, alignmentImage=None):  # noqa: D401
        """Write one tile — every selected detector — with its stage position.

        Goes through RecordingManager's storer layer rather than writing files
        here, so tiles land in the same OME formats as everything else the
        instrument records, and the stage position rides along in the standard
        ``Plane/@PositionX|Y`` fields that make the set re-stitchable.

        ``images`` maps detector name to the array captured at this position.
        They all carry the same stage position, which is what lets a reader
        rebuild one mosaic per detector from a single solved layout.
        """
        if not images:
            return None

        name = f'tile_x{gx:+03d}_y{gy:+03d}'
        savename = str(Path(folder) / name)
        attrs = {detectorName: self._tileAttrs(gx, gy, stage_xy)
                 for detectorName in images}
        try:
            written = self._master.recordingManager.snapImagesPrev(
                images, savename, self._saveFormat, attrs,
                stagePositionUm=(stage_xy[0], stage_xy[1], 0.0),
                zStepUm=dataset.z_step_um or None,
            )
        except Exception as e:
            self._logger.error(f'Tiling: failed to save tile {name}: {e}',
                               exc_info=True)
            return None

        # What the container actually wrote. HDF5 gives a 2-D snapshot a
        # leading frame axis and OME-TIFF does not, so this is the writer's to
        # report -- deriving it from the array that went in would describe the
        # wrong file for one of the two formats.
        try:
            storedShapes = self._master.recordingManager.lastSnapStoredShapes()
        except Exception:
            storedShapes = {}

        # The storer decides the final names (it appends the detector name and
        # its own extension, and de-duplicates). The manifest and
        # TileConfiguration.txt must name files that actually exist, or no
        # stitcher will find them.
        writtenByDetector = {}
        for index, detectorName in enumerate(images):
            if written and self._saveFormat.name == 'ZARR':
                # One structured store contains every detector group.
                writtenByDetector[detectorName] = Path(written[0]).name
            elif written and index < len(written):
                writtenByDetector[detectorName] = Path(written[index]).name
            else:
                writtenByDetector[detectorName] = (
                    f'{name}_{detectorName}{self._saveExtension}'
                )

        # Keep the full-dimensional alignment-detector snapshot above: it is
        # the top-level compatibility artifact and, in free-running mode, its
        # payload.  Geometry gets a separate, genuinely 2-D projection so the
        # offline solve never has to materialize a full C/Z stack.  A source
        # that was already 2-D can safely let both descriptors share one file.
        alignmentArray = np.asarray(
            alignmentImage
            if alignmentImage is not None
            else self._displayPlane(images[alignmentDetector])
        )
        if alignmentArray.ndim != 2:
            alignmentArray = self._displayPlane(alignmentArray)
        alignmentFilename = writtenByDetector[alignmentDetector]
        alignmentStoredShape = storedShapes.get(alignmentDetector)
        if np.asarray(images[alignmentDetector]).ndim > 2:
            alignmentStoredShape = None
            alignmentBase = f'{name}_alignment'
            if self._saveFormat.name == 'ZARR':
                alignmentFilename = f'{alignmentBase}.zarr'
            else:
                alignmentFilename = (
                    f'{alignmentBase}_{alignmentDetector}'
                    f'{self._saveExtension}'
                )
            try:
                alignmentWritten = (
                    self._master.recordingManager.snapImagePrev(
                        alignmentDetector,
                        str(Path(folder) / alignmentBase),
                        self._saveFormat,
                        alignmentArray,
                        {alignmentDetector: attrs[alignmentDetector]},
                        stagePositionUm=(stage_xy[0], stage_xy[1], 0.0),
                    )
                )
                if alignmentWritten:
                    alignmentFilename = Path(alignmentWritten[0]).name
                try:
                    alignmentStoredShape = (
                        self._master.recordingManager
                        .lastSnapStoredShapes()
                        .get(alignmentDetector)
                    )
                except Exception:
                    alignmentStoredShape = None
            except Exception as e:
                # Keep the tile identity and payloads in the manifest.  The
                # predicted alignment path remains absent, so refinement skips
                # this one tile and leaves its nominal position intact instead
                # of substituting the full compatibility stack silently.
                self._logger.error(
                    f'Tiling: failed to save 2-D alignment image for {name}: '
                    f'{e}', exc_info=True
                )

        ix, iy = self._gridToImage(gx, gy, self._orientation)
        if self._stitcher is None:
            # A saved-only run still needs nominal pixel coordinates for
            # TileConfiguration.txt and offline stitching. Match the normal
            # stitcher's round-to-pixel policy, without allocating a canvas.
            step_y_px = max(1, int(round(dataset.step_um / dataset.pixel_size_um[0])))
            step_x_px = max(1, int(round(dataset.step_um / dataset.pixel_size_um[1])))
            nominal = (iy * step_y_px, ix * step_x_px)
            placement = nominal
        else:
            nominal = self._stitcher.nominal_placement(ix, iy)
            placement = self._stitcher.placement(ix, iy) or nominal
        dataset.add(TileRecord(
            # The alignment detector's file stays the record's own filename, so
            # a reader that knows nothing of save sets still finds the tile the
            # mosaic is built from.
            filename=writtenByDetector[alignmentDetector],
            grid=(int(gx), int(gy)),
            stage_um=(float(stage_xy[0]), float(stage_xy[1])),
            # TileConfiguration.txt wants (x, y) = (col, row).
            pixel_xy=(float(placement[1]), float(placement[0])),
            correction_px=(
                float(placement[0] - nominal[0]),
                float(placement[1] - nominal[1]),
            ),
            alignment=dict(
                detector=alignmentDetector,
                filename=alignmentFilename,
                **self._tileAxes(
                    alignmentArray, dataset, alignmentStoredShape
                ),
            ),
            # Phase 1 has no separate payload recording, so each detector's
            # snapshot *is* its payload; a locator names it the same way a
            # recording's would, minus the group and generation only a
            # finalised writer can supply.
            payloads={
                detectorName: dict(
                    # Snapshot-derived axes first, so a writer that described
                    # what it wrote overrides them rather than the other way
                    # round.
                    self._tileAxes(image, dataset,
                                   storedShapes.get(detectorName)),
                    # Persist the normalized descriptive schema pinned when
                    # the save set was resolved. The reader may later override
                    # it with a newer calibration, but must always know what
                    # acquisition believed for this run.
                    transform_to_alignment=self.__dict__.get(
                        '_saveSetTransforms', {}
                    ).get(
                        detectorName,
                        parse_detector_transform('identity').as_manifest(),
                    ),
                    **self._payloadLocator(payloads, detectorName,
                                           writtenByDetector[detectorName],
                                           snapshotGroup=(
                                               None
                                               if self._saveFormat.name == 'TIFF'
                                               else detectorName
                                           )),
                )
                for detectorName, image in images.items()
            },
            **self._tileAxes(images[alignmentDetector], dataset,
                             storedShapes.get(alignmentDetector)),
        ))
        return name

    @staticmethod
    def _payloadLocator(
        locators, detectorName, snapshotFilename, snapshotGroup=None
    ) -> dict:
        """Where this detector's payload for this tile actually is.

        A recording's locator when one ran and finalised — it carries the group
        within a container and the generation, which a filename cannot — and
        otherwise the snapshot that *is* the payload for a run without one.
        ``complete`` distinguishes the two rather than leaving a reader to
        infer it from which fields happen to be filled.
        """
        if locators is None:
            return {
                'path': snapshotFilename,
                'group': snapshotGroup,
                'complete': True,
            }
        found = locators.get(detectorName)
        if found is None:
            # A recording session ran, but this detector produced no finalized
            # locator (for example a recoverable writer failure).  Keep the
            # compatibility snapshot named for diagnostics while explicitly
            # preventing a version-2 reader from selecting it as the payload.
            return {
                'path': snapshotFilename,
                'group': snapshotGroup,
                'complete': False,
            }
        locator = found.asdict() if hasattr(found, 'asdict') else dict(found)
        locator.pop('detector', None)
        # The writer's own axis description wins where it has one. Replacing it
        # with axes derived from the alignment snapshot would describe the
        # wrong array: the snapshot is a reduced view, and the payload is the
        # recording -- they need not share a rank, let alone axis names.
        locator = {key: value for key, value in locator.items()
                   if not (key in ('axes', 'stored_axes', 'shape',
                                   'stored_shape')
                           and not value)}
        return locator

    @staticmethod
    def _tileAxes(frame, dataset, storedShape=None) -> dict:
        """Name the axes of a saved tile, for the manifest.

        A scan-driven tile's leading axis is a line step's channels, not
        planes, whenever the scan ran with more than one line step — and the
        manifest could previously only call it depth and give it a Z spacing,
        which mislabels a channel as a distance for every reader downstream.

        The two are told apart by the detector's own calibration: it publishes
        a Z spacing only when the scan actually moved in Z, which is the same
        fact ``_detectorZStepUm`` already relies on. Anything this cannot
        justify is left undescribed rather than guessed at, and a reader falls
        back to the older assumption.
        """
        array = np.asarray(frame)
        if array.ndim < 2:
            return {}

        leading = array.ndim - 2
        stepped_in_z = dataset.z_step_um > 0
        if leading == 0:
            axes = 'YX'
        elif leading == 1:
            axes = ('Z' if stepped_in_z else 'C') + 'YX'
        elif leading == 2 and stepped_in_z:
            axes = 'CZYX'
        else:
            return {}

        # `axes` and `shape` describe what was captured. `stored_axes` describes
        # the file, which only the writer knows -- some containers add axes of
        # their own -- so it is filled in from what the storer reported and
        # omitted when it reported nothing, rather than assumed equal to `axes`
        # and thereby contradicting the file for those formats.
        descriptor = {'axes': axes,
                      'shape': tuple(int(size) for size in array.shape)}
        if storedShape is not None and len(storedShape) >= len(axes):
            extra = len(storedShape) - len(axes)
            descriptor['stored_axes'] = 'T' * extra + axes
            descriptor['stored_shape'] = tuple(int(s) for s in storedShape)
        return descriptor

    @property
    def _saveFormat(self):
        """Save format for tiles, from config. OME-TIFF is the default.

        OME-TIFF is what Fiji, BigStitcher and BioFormats read natively, and it
        is the format whose per-plane stage position survives round-tripping,
        so it is the sensible default for a dataset meant to be re-stitched.
        """
        from imswitch.imcontrol.model.managers.RecordingManager import SaveFormat
        name = str(
            getattr(self._setupInfo.tiling, 'saveFormat', 'TIFF') or 'TIFF'
        ).upper()
        try:
            return SaveFormat[name]
        except KeyError:
            self._logger.warning(
                f'Tiling: unknown saveFormat "{name}"; using OME-TIFF'
            )
            return SaveFormat.TIFF

    @property
    def _saveExtension(self) -> str:
        from imswitch.imcontrol.model.managers.RecordingManager import SaveFormat
        return {
            SaveFormat.TIFF: '.ome.tiff',
            SaveFormat.HDF5: '.h5',
            SaveFormat.ZARR: '.zarr',
        }.get(self._saveFormat, '.ome.tiff')

    def _canvasOriginStage(self) -> Optional[Tuple[float, float]]:
        """Stage position of canvas pixel (0, 0), for the mosaic's metadata."""
        return self._canvasPixelToStage(0, 0)

    @staticmethod
    def _tileAttrs(gx, gy, stage_xy) -> dict:
        return {
            'Tiling:grid_x': int(gx),
            'Tiling:grid_y': int(gy),
            'Tiling:stage_x_um': float(stage_xy[0]),
            'Tiling:stage_y_um': float(stage_xy[1]),
        }

    def _saveMosaic(self, detectorName, folder, canvasOriginStage) -> None:
        """Write the assembled overview as a single OME image.

        Saved as uint16 rather than the float the stitcher blends in, so the
        file matches the camera's own range and opens sensibly anywhere. Its
        pixel size is the mosaic's, and its position is the canvas origin, so
        it lands in the right place next to the individual tiles.
        """
        if self._stitcher is None:
            return
        overview = self._stitcher.get_overview()
        if overview.size == 0:
            return
        image = np.clip(overview, 0.0, 1.0)
        image = (image * np.iinfo(np.uint16).max).astype(np.uint16)

        savename = str(Path(folder) / 'mosaic')
        attrs = {detectorName: {
            'Tiling:kind': 'stitched_overview',
            'Tiling:tiles': len(self._gridPositions),
        }}
        try:
            self._master.recordingManager.snapImagePrev(
                detectorName, savename, self._saveFormat, image, attrs,
                stagePositionUm=(
                    canvasOriginStage[0], canvasOriginStage[1], 0.0
                ) if canvasOriginStage else None,
            )
        except Exception as e:
            self._logger.error(f'Tiling: failed to save mosaic: {e}',
                               exc_info=True)

    def _registerTile(self, frame, gx, gy, report, maxShiftPx):
        """Register a tile against every placed neighbour, not just the canvas.

        The plain path correlates once, against the canvas region the commanded
        position points at, and applies whatever comes back. A spiral gives most
        tiles two to four already-placed neighbours, so that throws away the
        redundancy that would have caught a bad match — and a bad match here is
        not confined to its own tile: it is written into the saved layout and
        every later tile is placed relative to it.

        Each neighbour is correlated separately and the placement is the
        confidence-weighted consensus of what they imply. The individual
        measurements are kept for the whole-run solve at the end.
        """
        neighbours = self._stitcher.placed_neighbours(gx, gy)
        if not neighbours:
            return (0.0, 0.0)

        moving = np.asarray(frame, dtype=np.float32)
        nominal = self._stitcher.nominal_placement(gx, gy)
        index = self._alignmentIndex.setdefault(
            (gx, gy), len(self._alignmentIndex)
        )

        implied, weights, best = [], [], None
        for key, tile, nominalOffset in neighbours:
            shift, confidence, reason = estimate_shift(
                tile, moving, nominalOffset, max_shift_px=maxShiftPx,
            )
            if reason:
                continue
            measured = (nominalOffset[0] + shift[0], nominalOffset[1] + shift[1])
            placement = self._stitcher.placement(*key)
            implied.append((placement[0] + measured[0],
                            placement[1] + measured[1]))
            weights.append(max(confidence, 1e-3))
            if best is None or confidence > best[1]:
                best = (shift, confidence, nominalOffset)

            neighbourIndex = self._alignmentIndex.setdefault(
                key, len(self._alignmentIndex)
            )
            self._alignmentLinks.append(TileLink(
                i=neighbourIndex, j=index, offset=measured,
                confidence=confidence,
                correction=(shift[0], shift[1]),
            ))

        if not implied:
            report.add(TileShift(
                grid=(gx, gy), applied=(0.0, 0.0), measured=(0.0, 0.0),
                expected=neighbours[0][2], confidence=0.0, accepted=False,
                reason='no neighbour could be registered',
            ))
            return (0.0, 0.0)

        weightArray = np.asarray(weights, dtype=np.float64)
        consensus = np.average(np.asarray(implied, dtype=np.float64),
                               axis=0, weights=weightArray)
        applied = (float(consensus[0] - nominal[0]),
                   float(consensus[1] - nominal[1]))
        report.add(TileShift(
            grid=(gx, gy), applied=applied, measured=best[0],
            expected=best[2], confidence=float(np.max(weightArray)),
            accepted=True,
            reason='',
        ))
        return applied

    def _resolveLayout(self, report):
        """Re-solve the whole layout once every tile is in hand.

        The live pass can only ever align a tile to what came before it, so its
        errors accumulate along the acquisition order. With the run finished
        there is no such constraint: every measurement taken along the way goes
        into one least-squares fit, inconsistent ones are rejected against the
        consensus rather than against nothing, and the tiles are moved to where
        the whole set agrees they belong.
        """
        if len(self._alignmentIndex) < 2 or not self._alignmentLinks:
            return

        keys = sorted(self._alignmentIndex, key=self._alignmentIndex.get)
        nominal = [self._stitcher.nominal_placement(*key) for key in keys]
        live = [self._stitcher.placement(*key) or nominal[i]
                for i, key in enumerate(keys)]

        positions, accepted = solve_links(
            nominal, self._alignmentLinks, progress=self._logger.debug
        )
        # Measured before the move, or every tile compares equal to itself.
        moved = sum(
            1 for was, now in zip(live, positions)
            if abs(now[0] - was[0]) > 0.5 or abs(now[1] - was[1]) > 0.5
        )
        self._stitcher.set_placements(dict(zip(keys, positions)))
        rejected = len(self._alignmentLinks) - len(accepted)
        report.globalSolve = (
            f'Whole-run solve: {len(keys)} tiles from '
            f'{len(accepted)} measurement(s)'
            + (f', {rejected} rejected as inconsistent' if rejected else '')
            + f', {moved} still disagreed with the live placement.'
        )
        self._logger.info(f'Tiling: {report.globalSolve}')

    def _startPayloadSession(self, saveSet, nTiles, tileSource, folder):
        """Open one recording session for the run and hand it scan dispatch.

        The session is a lapse of ``nTiles`` points differing by where the
        stage is rather than by when a timer fires, which is what a tiling run
        already was. Returns the dispatcher, or None when the Recording
        controller cannot host one -- in which case tiling keeps dispatching
        its own scans and saves snapshots, exactly as before.
        """
        controllers = getattr(self._commChannel.__dict__.get('_main', None),
                              'controllers', None) or {}
        recording = controllers.get('Recording')
        if recording is None or not hasattr(recording, 'setPositioningProvider'):
            self._logger.info(
                'Tiling: no Recording controller can host a per-tile payload '
                'session; saving snapshots only.'
            )
            return None

        dispatcher = _RecordingDispatcher(self, recording, self._scanTimeoutS(),
                                          folder)
        manager = self._master.recordingManager
        try:
            try:
                # Each item of this session is a tile, not a timepoint; the
                # recording's acquisition layout says so.
                recording.setPositioningProvider(
                    dispatcher.provider, partitionKind='tile'
                )
            except TypeError:
                # A host predating partition kinds still runs the session.
                recording.setPositioningProvider(dispatcher.provider)
            recording.setCycleTerminalCallback(dispatcher.onPointFinished)
            manager.sigRecordingEndedDetailed.connect(
                dispatcher.onWriterFinalised)
            manager.sigRecordingFailedTyped.connect(dispatcher.onPointFailed)
        except Exception as e:
            self._logger.error(
                f'Tiling: could not start the payload session ({e}); '
                'saving snapshots only.', exc_info=True
            )
            self._endPayloadSession(dispatcher)
            return None

        tileSource.useDispatcher(dispatcher)
        self._logger.info(
            f'Tiling: recording a payload per tile for {nTiles} tiles '
            f'({", ".join(saveSet)}).'
        )
        return dispatcher

    def _endPayloadSession(self, dispatcher) -> None:
        """Release dispatch and stop listening, whatever the run did."""
        recording = dispatcher._recording
        manager = self._master.recordingManager
        for disconnect in (
            lambda: manager.sigRecordingEndedDetailed.disconnect(
                dispatcher.onWriterFinalised),
            lambda: manager.sigRecordingFailedTyped.disconnect(
                dispatcher.onPointFailed),
            lambda: recording.clearPositioningProvider(),
        ):
            try:
                disconnect()
            except Exception:
                # Already gone, or never connected: either way this run no
                # longer owns it.
                pass

    def _resolveSaveSet(self, tilingInfo, alignmentDetector, mode):
        """Which detectors this run saves at each position.

        The alignment detector always saves — it is the one the mosaic is built
        from. The rest come from the Recording widget, which is where the
        operator already says what their data is; tiling keeping a second,
        private answer to that question is how the two drift apart.

        A detector is dropped only when it *cannot produce a tile here* — it
        does not exist, or the run's timing model cannot clock it. Everything
        else is saved.

        In particular, not knowing how a detector's pixels relate to the
        alignment detector's is **not** grounds for refusing to save it.
        Writing pixels needs a stage position and nothing more; the
        relationship only matters when something tries to overlay two
        detectors, which happens offline where there is freedom to measure or
        declare it. Refusing here destroyed data to answer a question nobody
        had asked yet — and of the two options, only "did not save it" is
        irreversible.

        What the relationship *is*, or that nobody said, is recorded per tile
        so a reader can tell an assertion from its absence.
        """
        selected = []
        try:
            selected = self._commChannel.getRecordingDetectors() or []
        except Exception as e:
            self._logger.warning(
                f'Tiling: could not read the recording detector selection '
                f'({e}); saving only {alignmentDetector}.'
            )

        transforms = dict(getattr(tilingInfo, 'detectorTransforms', None) or {})
        self._saveSetTransforms = {
            alignmentDetector: parse_detector_transform('identity').as_manifest()
        }
        saveSet = [alignmentDetector]
        for name in selected:
            if name == alignmentDetector or name in saveSet:
                continue
            reason = self._rejectFromSaveSet(name, transforms, mode,
                                             alignmentDetector)
            if reason:
                self._logger.warning(f'Tiling: not saving "{name}" — {reason}')
                continue

            # A declaration when there is one, "unknown" when there is not.
            # Either way the data is saved; the manifest simply says which of
            # the two a later overlay is working from.
            declared = transforms.get(name)
            try:
                transform = parse_detector_transform(
                    declared, label=f'detectorTransforms[{name!r}]'
                )
            except ValueError as exc:
                self._logger.warning(
                    f'Tiling: saving "{name}" with no usable transform — '
                    f'{exc}. Recorded as unknown; set it in the tiling '
                    'section, or align the detectors in ImProcess.'
                )
                transform = parse_detector_transform(None)
            if not transform.is_declared:
                self._logger.info(
                    f'Tiling: saving "{name}", but nothing declares how its '
                    f'pixels relate to {alignmentDetector}. Recorded as '
                    'unknown — overlaying the two needs a transform, which '
                    'ImProcess can supply.'
                )
            saveSet.append(name)
            self._saveSetTransforms[name] = transform.as_manifest()

        if len(saveSet) > 1:
            self._logger.info(
                f'Tiling: saving {len(saveSet)} detectors per tile '
                f'({", ".join(saveSet)}); aligning on {alignmentDetector}.'
            )
        return saveSet

    def _rejectFromSaveSet(self, name, transforms, mode, alignmentDetector):
        """Why ``name`` cannot produce a tile here, or None if it can.

        Capability only. Whether two detectors can be *overlaid* is a
        registration question, answered offline against a transform that may
        not exist yet — and answering it here by discarding the data is the
        one choice that cannot be undone later.
        """
        try:
            detector = self._master.detectorsManager[name]
        except Exception:
            return 'no such detector'

        if mode != MODE_TRIGGERED and getattr(detector, 'isScanDriven', False):
            return ('it is scan-driven, so it only produces an image while a '
                    'scan runs; free-running tiling cannot clock it')

        # Geometry disagreements are worth saying out loud — a detector whose
        # declared identity contradicts its own pixel size is misconfigured —
        # but they describe how the data may be *combined*, not whether it is
        # worth keeping, so they annotate rather than refuse.
        declared = transforms.get(name)
        if declared in (None, ''):
            return None
        try:
            transform = parse_detector_transform(
                declared, label=f'detectorTransforms[{name!r}]'
            )
        except ValueError:
            return None   # reported, and recorded as unknown, by the caller
        # ``unknown`` also carries the identity matrix, but claims nothing, so
        # there is no claim for the geometry to contradict.
        if not (transform.is_declared and transform.is_identity):
            return None

        try:
            reference = self._master.detectorsManager[alignmentDetector]
            if (self._detectorPixelSizeUm(detector)
                    != self._detectorPixelSizeUm(reference)):
                self._logger.warning(
                    f'Tiling: "{name}" declares identity to {alignmentDetector} '
                    'but reports a different pixel size. Saving it anyway; one '
                    'of the two is wrong, and overlaying them will be off.'
                )
            elif tuple(detector.shape) != tuple(reference.shape):
                self._logger.warning(
                    f'Tiling: "{name}" declares identity to {alignmentDetector} '
                    'but has a different frame shape. Saving it anyway; '
                    'overlaying them will be off.'
                )
        except Exception:
            pass
        return None

    def _captureSaveSet(self, saveSet, alignmentDetector, alignmentFrame,
                        tileSource):
        """One image per detector, all taken with the stage standing still.

        The alignment detector's frame is already in hand; the others are
        grabbed now, before the caller is allowed to move on. Each gets the
        same fresh-frame proof the alignment detector gets, so a second camera
        cannot contribute a frame left over from the previous tile or exposed
        during the move.

        The whole set is returned at once: nothing may be saved until the
        slowest camera has produced its frame, which is also what sets the
        per-tile cost.
        """
        images = {alignmentDetector: alignmentFrame}
        stale = []
        for name in saveSet:
            if name == alignmentDetector:
                continue
            try:
                detector = self._master.detectorsManager[name]
            except Exception as e:
                self._logger.error(f'Tiling: cannot read "{name}": {e}')
                continue

            if getattr(tileSource, 'needsFreshFrame', True):
                frame, fresh = self._grabSettledFrame(detector)
            else:
                # Triggered: the scan that produced the tile clocked this
                # detector too, so its frame is already the right one.
                frame, fresh = self._scanFrame(detector), True
            if frame is None:
                self._logger.warning(
                    f'Tiling: "{name}" produced no frame for this tile'
                )
                continue
            if not fresh:
                stale.append(name)
            images[name] = frame

        if stale:
            self._logger.warning(
                f'Tiling: {", ".join(stale)} fell back to a buffered frame for '
                'this tile; it may belong to the previous position'
            )
        return images

    def _syncDatasetPlacements(self, dataset) -> None:
        """Rewrite the manifest's pixel positions from the solved layout.

        Each record was written as its tile was acquired, so it carries the
        placement the live pass had reached by then. Leaving those in place
        would save a layout the run itself no longer agrees with — and that
        layout is what a stitcher, or this project's own offline reconstructor,
        starts from.
        """
        for record in dataset.tiles:
            placement = self._stitcher.placement(
                *self._gridToImage(record.grid[0], record.grid[1],
                                   self._orientation)
            )
            if placement is None:
                continue
            nominal = self._stitcher.nominal_placement(
                *self._gridToImage(record.grid[0], record.grid[1],
                                   self._orientation)
            )
            # TileConfiguration.txt wants (x, y) = (col, row).
            record.pixel_xy = (float(placement[1]), float(placement[0]))
            record.correction_px = (float(placement[0] - nominal[0]),
                                    float(placement[1] - nominal[1]))

    # ------------------------------------------------------------------
    # Tile sources: how one tile's worth of signal is obtained
    # ------------------------------------------------------------------

    def _makeTileSource(self, mode, detector, tilingInfo, scanSourceKey):
        """Pick the acquisition strategy for this run.

        The operator chooses the *timing model* (free-running or triggered);
        which mechanism implements it follows from the detector, so nobody has
        to know whether their detector happens to be scan-driven.
        """
        if mode != MODE_TRIGGERED:
            if getattr(detector, 'isScanDriven', False):
                raise RuntimeError(
                    f'Detector "{detector.name}" is scan-driven: it only '
                    'produces an image while a scan runs, so it cannot be '
                    'tiled in free-running mode. Switch Mode to Triggered.'
                )
            return _FreeRunningTileSource(self, detector)

        source = self._resolveScanSource(scanSourceKey)
        self._assertScanDoesNotOwnTilingStage(tilingInfo, source)
        return _TriggeredTileSource(self, detector, source)

    def _resolveScanSource(self, preferredKey):
        resolve = getattr(self._commChannel, 'getScanSource', None)
        if not callable(resolve):
            raise RuntimeError(
                'This build has no scan-source resolution, so triggered '
                'tiling is unavailable.'
            )
        return resolve(preferredKey)

    def _assertScanDoesNotOwnTilingStage(self, tilingInfo, scanSource) -> None:
        """Refuse when the scan drives the very axes tiling steps between tiles.

        A stage scan and tiling would both command the same positioner — one
        stepping between tiles, the other rastering within one — and the run
        would fail confusingly partway through a sample. Galvo scanning is the
        combination that works: the scan covers the tile, the stage moves
        between tiles.
        """
        tilingPositioner = getattr(tilingInfo, 'xyPositioner', None)
        if not tilingPositioner:
            return

        scanPositioners = self._scanPositionerNames(scanSource)
        if tilingPositioner in scanPositioners:
            raise RuntimeError(
                f'The selected scan drives positioner "{tilingPositioner}", '
                'which is the same one tiling steps between tiles. A stage '
                'scan cannot be tiled this way — use a galvo/beam scan for '
                'the tile, or tile with a different positioner.'
            )

    def _scanPositionerNames(self, scanSource) -> set:
        """Positioners the given scan source drives, best-effort.

        Falls back to every ``forScanning`` positioner in the setup when the
        controller cannot report its own devices — conservative, since a false
        positive only blocks a combination that is very likely broken anyway.
        """
        for accessor in ('getScanPositionerNames', 'getScanDevices'):
            getter = getattr(scanSource, accessor, None)
            if callable(getter):
                try:
                    return {str(name) for name in (getter() or ())}
                except Exception:
                    break

        return {
            name
            for name, info in self._setupInfo.positioners.items()
            if getattr(info, 'forScanning', False)
        }

    @staticmethod
    def _detectorZStepUm(detector) -> float:
        """Z spacing of a stacked tile, from the detector's own calibration.

        For a scan-driven detector this is the scan's Z step, published as the
        leading entry of ``pixelSizeUm`` ([Z, Y, X]).
        """
        try:
            sizes = list(detector.pixelSizeUm)
        except Exception:
            return 0.0
        if len(sizes) >= 3:
            step = float(sizes[0])
            return step if step > 0 else 0.0
        return 0.0

    def _saveRoot(self, tilingInfo):
        """Where tiling datasets go, in order of precedence.

        The Recording widget's folder wins: that is where the operator has
        already said their data belongs, and having tiling invent a separate
        root is how a run ends up written somewhere nobody looks (or, on
        Windows, to a ``D:`` drive that may not exist).
        """
        configured = getattr(tilingInfo, 'measurementsRoot', '') or ''
        if configured:
            return configured

        getFolder = getattr(self._commChannel, 'getRecordingFolder', None)
        if callable(getFolder):
            try:
                folder = getFolder()
            except Exception as e:
                self._logger.warning(
                    f'Tiling: could not read the recording folder: {e}'
                )
                folder = None
            if folder:
                return folder

        self._logger.warning(
            'Tiling: no recording folder available; falling back to the '
            'default measurements root. Set the folder in the Recording '
            'widget to control where tiling datasets are written.'
        )
        return None

    def _populateScanSources(self, preferredKey: str) -> None:
        """Offer the scan-source choice, but only when there is one to make."""
        names = []
        getNames = getattr(self._commChannel, 'getScanSourceNames', None)
        if callable(getNames):
            try:
                names = list(getNames() or [])
            except Exception as e:
                self._logger.warning(f'Could not list scan sources: {e}')
        self._widget.setScanSources(names)
        if preferredKey and preferredKey in names:
            self._widget.setScanSource(preferredKey)

    def _populateDetectors(self, preferredName: str) -> None:
        """Offer the detector choice, but only when there is one to make.

        Deliberately *not* persisted with the widget state. The setup file is
        the default every session and the combo is a session override; saving
        the selection would silently outrank ``tiling.camera`` at every
        startup, which is the failure this codebase already hit once with
        ``cameraPixelSizeUm``.
        """
        try:
            started = set(self._master.detectorsManager.getAllDeviceNames())
        except Exception:
            started = None      # cannot tell: offer every configured detector
        names = [
            name for name, info in self._setupInfo.detectors.items()
            if getattr(info, 'forAcquisition', False)
            # A detector that failed to start cannot be tiled with.
            and (started is None or name in started)
        ]
        self._widget.setDetectors(names)
        if preferredName and preferredName in names:
            self._widget.setDetector(preferredName)

    def _onDetectorChanged(self) -> None:
        """Discard an overview that the new detector cannot be placed against.

        The mosaic's geometry comes from the detector's pixel size, and
        click-to-navigate maps overview pixels back to stage coordinates
        through it. Keeping a mosaic built with a different pixel size would
        send the stage somewhere other than where the operator clicked.
        """
        if self.__dict__.get('_scanning', False):
            return
        if self._stitcher is None:
            return
        self._stitcher = None
        self._originXY = None
        self._gridPositions = []
        self._refreshTargets()
        self._logger.info(
            'Tiling: detector changed, so the previous overview was discarded '
            '— its pixel size no longer applies.'
        )

    def _onModeChanged(self, mode: str) -> None:
        """Re-list scan sources when triggered mode is selected.

        Controllers register as the app starts, and the tiling controller may
        be constructed before the scan controllers exist, so the list is
        refreshed at the moment it becomes relevant.
        """
        if mode == MODE_TRIGGERED:
            self._populateScanSources(
                getattr(self._setupInfo.tiling, 'scanSource', '')
            )

    def _scanTimeoutS(self) -> float:
        return float(
            getattr(self._setupInfo.tiling, 'scanTimeoutS', _SCAN_TIMEOUT_S)
            or _SCAN_TIMEOUT_S
        )

    def _focusReacquireTimeoutS(self) -> float:
        """How long a tile may wait for the focus lock to come back.

        Generously longer than the focus lock's own ``reacquireTimeoutS``: the
        lock decides when to give up, and this only has to outlast that
        decision so the tile loop observes the verdict rather than timing out
        first and drawing its own.
        """
        focusLock = getattr(self._setupInfo, 'focusLock', None)
        lockTimeout = float(
            getattr(focusLock, 'reacquireTimeoutS', 1.0) or 1.0
        )
        return lockTimeout * 2.0 + 1.0

    def _scanFrame(self, detector):
        """Read the image a completed scan produced.

        Scan-driven detectors report their raster behind a leading frame axis.
        That wrapper is dropped, but a real Z stack is kept: a 3D scan's tile
        is the whole stack, and throwing away all but one plane here would
        silently discard most of the acquisition. The mosaic preview projects
        it; saving keeps every plane.
        """
        try:
            frame = detector.getLatestFrameShared()
        except Exception as e:
            self._logger.error(f'Tiling: could not read the scan frame: {e}',
                               exc_info=True)
            return None
        if frame is None:
            return None

        frame = np.asarray(frame)
        while frame.ndim > 2 and frame.shape[0] == 1:
            frame = frame[0]
        return frame if frame.ndim >= 2 and frame.size else None

    @staticmethod
    def _displayPlane(frame):
        """Collapse a tile to the 2-D plane the mosaic preview shows.

        A 3D tile is shown as a maximum projection — the conventional way to
        make a stack legible at a glance, and the one that keeps sparse
        structure visible where a mid-slice would miss it. The saved tile and
        the offline reconstruction still use every plane.
        """
        frame = np.asarray(frame)
        if frame.ndim <= 2:
            return frame
        return frame.max(axis=tuple(range(frame.ndim - 2)))

    def _grabSettledFrame(self, detector):
        """Return ``(frame, was_fresh)`` for a tile, avoiding in-motion frames.

        See :mod:`imswitch.imcontrol.controller._fresh_frame` for the
        handshake; it is shared with autofocus.
        """
        return grab_fresh_frame(
            detector, _CHUNK_CONSUMER_KEY,
            timeout_s=_FRESH_FRAME_TIMEOUT_S, poll_s=_FRAME_POLL_S,
            should_stop=lambda: self._stopRequested or getattr(self, '_closed', False),
            logger=self._logger, release=False,
        )

    @staticmethod
    def _latestFrame(detector):
        frame = detector.getLatestFrameShared()
        return None if frame is None else np.asarray(frame)

    # ------------------------------------------------------------------
    # Click-to-navigate
    # ------------------------------------------------------------------

    def _navigateToPixel(self, row: int, col: int) -> None:
        # Reject clicks while the scan thread is still active.  The acquisition
        # loop exits as soon as the last tile is captured (line ~134), but the
        # thread then performs a blocking return-to-origin (lines ~137-138)
        # before reaching the ``finally`` block that clears ``_scanning`` and
        # restores button state.  A click during that window issues a second
        # ``setPosition`` on the same positioner from the UI thread; on most
        # backends that aborts the scan thread's pending hardware ACK and
        # leaves its ``setPosition`` call hung forever — ``finally`` never runs,
        # ``_scanning`` stays True, ``setRunning(False)`` never fires, Start
        # remains disabled and Stop becomes a no-op (the scan loop has already
        # exited, so flipping ``_stopRequested`` has nothing to react to).
        if self._scanning:
            self._logger.info(
                'Tiling click-to-navigate ignored: automated stage movement '
                'is still in progress. Wait for it to finish.'
            )
            return
        self._moveStageToPixel(row, col)

    def _canvasPixelToStage(self, row: int, col: int) -> Optional[Tuple[float, float]]:
        """Stage (x, y) for a canvas pixel, honouring the mosaic orientation.

        Measures the pixel's offset from the centre of the grid-(0, 0) tile —
        whose stage position is known — converts that to µm in image space, and
        maps it back through the same axis transform used to assemble the
        mosaic. Reading the stitcher's own canvas origin keeps this correct
        when registration has nudged tiles off their nominal layout.
        """
        if self._stitcher is None or self._originXY is None or not self._gridPositions:
            return None

        canvas_row0, canvas_col0 = self._stitcher.canvas_origin_px
        tile_h, tile_w = self._stitcher.tile_shape_px

        # Offset from the centre of the tile at image grid (0, 0), in pixels.
        d_col = (col + canvas_col0) - tile_w / 2
        d_row = (row + canvas_row0) - tile_h / 2

        dx_um = d_col / self._stitcher.px_per_um_x
        dy_um = d_row / self._stitcher.px_per_um_y

        orientation = self.__dict__.get('_orientation', (False, False, False))
        stage_dx, stage_dy = self._imageOffsetToStage(dx_um, dy_um, orientation)
        return self._originXY[0] + stage_dx, self._originXY[1] + stage_dy

    def _moveStageToPixel(self, row: int, col: int) -> Optional[Tuple[float, float]]:
        target = self._canvasPixelToStage(row, col)
        if target is None:
            return None
        return self._moveStageTo(*target)

    def _moveStageTo(
        self, stage_x: float, stage_y: float
    ) -> Optional[Tuple[float, float]]:
        tilingInfo = self._setupInfo.tiling
        if tilingInfo is None:
            return None
        axes = list(self._setupInfo.positioners[tilingInfo.xyPositioner].axes)
        positioner = self._master.positionersManager[tilingInfo.xyPositioner]
        positioner.setPosition(stage_x, axes[0])
        positioner.setPosition(stage_y, axes[1])
        return stage_x, stage_y

    @staticmethod
    def _stageOffsetToImage(stage_dx: float, stage_dy: float, orientation):
        """Inverse of :meth:`_imageOffsetToStage`: undo the swap, then the flips."""
        flip_x, flip_y, swap_axes = orientation
        if swap_axes:
            stage_dx, stage_dy = stage_dy, stage_dx
        dx_um = -stage_dx if flip_x else stage_dx
        dy_um = -stage_dy if flip_y else stage_dy
        return dx_um, dy_um

    def _stageToCanvasPixel(
        self, stage_x: float, stage_y: float
    ) -> Optional[Tuple[float, float]]:
        """Overview pixel ``(row, col)`` of a stage position, or None.

        None without an overview to place it on, and for a position outside
        the overview: a marker off the canvas would only drag the view out.
        """
        if self._stitcher is None or self._originXY is None or not self._gridPositions:
            return None
        orientation = self.__dict__.get('_orientation', (False, False, False))
        dx_um, dy_um = self._stageOffsetToImage(
            stage_x - self._originXY[0], stage_y - self._originXY[1], orientation
        )
        canvas_row0, canvas_col0 = self._stitcher.canvas_origin_px
        tile_h, tile_w = self._stitcher.tile_shape_px
        col = dx_um * self._stitcher.px_per_um_x + tile_w / 2 - canvas_col0
        row = dy_um * self._stitcher.px_per_um_y + tile_h / 2 - canvas_row0
        height, width = self._stitcher.canvas_shape_px
        if not (0 <= row < height and 0 <= col < width):
            return None
        return row, col

    # ------------------------------------------------------------------
    # Targets
    # ------------------------------------------------------------------

    def _onOverviewClicked(self, row: int, col: int) -> None:
        mode = self._widget.getClickMode()
        if mode == CLICK_ADD_TARGET:
            self._addTargetAtPixel(row, col)
        elif mode == CLICK_NAVIGATE:
            self._navigateToPixel(row, col)

    def _addTargetAtPixel(self, row: int, col: int):
        stage_xy = self._canvasPixelToStage(row, col)
        if stage_xy is None:
            self._logger.info('Tiling: no overview to place a target on yet')
            return None
        target = self._targets.add(
            stage_xy=stage_xy, source='manual', row_col=(int(row), int(col))
        )
        self._refreshTargets()
        return target

    def _refreshTargets(self) -> None:
        """Hand the list and its overview markers to the widget.

        Through signals, so the scan worker can call it when the overview is
        final without touching the widget from its own thread.
        """
        targets = list(self._targets.targets)
        self.sigTargetsChanged.emit([t.to_dict() for t in targets])
        markers = {}
        for target in targets:
            marker = self._stageToCanvasPixel(*target.stage_xy)
            if marker is not None:
                markers[target.id] = marker
        self.sigTargetMarkers.emit(markers)

    @staticmethod
    def _targetKwargs(record: dict) -> dict:
        """Lenient reading of one target dict from a script or a file."""
        if 'stage_xy' in record:
            x_um, y_um = record['stage_xy']
        elif 'x' in record and 'y' in record:
            x_um, y_um = record['x'], record['y']
        else:
            raise ValueError("A target needs 'stage_xy': (x_um, y_um)")
        source = str(record.get('source', 'imported'))
        if source not in ('manual', 'segmentation', 'imported'):
            raise ValueError(
                "Target source must be 'manual', 'segmentation' or "
                f"'imported', not {source!r}"
            )
        row_col = record.get('row_col')
        z_um = record.get('z_um')
        return dict(
            stage_xy=(float(x_um), float(y_um)),
            source=source,
            enabled=bool(record.get('enabled', True)),
            row_col=None if row_col is None else (int(row_col[0]), int(row_col[1])),
            z_um=None if z_um is None else float(z_um),
            props=dict(record.get('props') or {}),
        )

    @APIExport()
    def getTargets(self) -> List[dict]:
        """The target list, one dict per target in acquisition order.

        Each has ``id``, ``stage_xy`` (x, y in µm), ``enabled``, ``source``
        (``manual`` for a click on the overview, ``segmentation``,
        ``imported``), ``row_col`` (the overview pixel it was clicked at, or
        None), ``z_um`` and ``props``. For a target workflow, build a
        :class:`~imswitch.imcontrol.model.workflows.target.TargetList` with
        ``TargetList([Target.from_dict(t) for t in api.imcontrol.getTargets()])``.
        """
        return self._targets.to_records()

    @APIExport(runOnUIThread=True)
    def setTargets(self, targets: List[dict]) -> int:
        """Replace the target list; returns how many targets it now holds.

        Each entry needs ``stage_xy`` (x, y in µm); ``enabled``, ``source``,
        ``z_um`` and ``props`` are optional. Ids are assigned afresh.
        """
        new = TargetList()
        for record in targets or []:
            new.add(**self._targetKwargs(record))
        self._targets = new
        self._refreshTargets()
        return len(new)

    @APIExport(runOnUIThread=True)
    def addTarget(self, x_um: float, y_um: float, enabled: bool = True,
                  source: str = 'imported') -> int:
        """Append a target at a stage position and return its id."""
        target = self._targets.add(**self._targetKwargs(
            {'stage_xy': (x_um, y_um), 'enabled': enabled, 'source': source}
        ))
        self._refreshTargets()
        return target.id

    @APIExport(runOnUIThread=True)
    def removeTarget(self, targetId: int) -> bool:
        """Drop one target by id; returns whether it existed."""
        removed = self._targets.remove(targetId)
        if removed:
            self._refreshTargets()
        return removed

    @APIExport(runOnUIThread=True)
    def clearTargets(self) -> None:
        """Empty the target list."""
        self._targets.clear()
        self._refreshTargets()

    @APIExport(runOnUIThread=True)
    def setTargetEnabled(self, targetId: int, enabled: bool) -> bool:
        """Keep a target listed but include or skip it; False if no such id."""
        target = self._targets.find(targetId)
        if target is None:
            return False
        target.enabled = bool(enabled)
        self._refreshTargets()
        return True

    @APIExport(runOnUIThread=True)
    def goToTarget(self, targetId: int) -> Optional[Tuple[float, float]]:
        """Drive the XY stage to a target; returns the position commanded.

        Refused (None, with a log line) while a tiling run owns the stage.
        """
        target = self._targets.find(targetId)
        if target is None:
            raise ValueError(f'No target with id {targetId}')
        if self.__dict__.get('_scanning', False):
            self._logger.info(
                'Tiling: go-to-target ignored while a tiling run owns the stage'
            )
            return None
        return self._moveStageTo(*target.stage_xy)

    @APIExport()
    def saveTargets(self, path: str) -> str:
        """Write the target list as JSON; returns the path written."""
        path = Path(path)
        if not path.suffix:
            path = path.with_suffix('.json')
        self._targets.save_json(path)
        self._logger.info(f'Tiling: {len(self._targets)} targets saved to {path}')
        return str(path)

    @APIExport(runOnUIThread=True)
    def loadTargets(self, path: str) -> int:
        """Replace the target list with one saved by :meth:`saveTargets`."""
        self._targets = TargetList.load_json(path)
        self._refreshTargets()
        return len(self._targets)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _syncPositionerFromHardware(self, positioner) -> None:
        """Re-read the positioner's true position, where the driver offers it.

        Optional: positioners that cannot report an absolute position simply
        keep their tracked value.
        """
        sync = getattr(positioner, 'syncPositionFromHardware', None)
        if sync is None:
            return
        try:
            if not sync():
                self._logger.warning(
                    'Tiling: stage could not report its absolute position; '
                    'the overview anchor and click-to-navigate may be offset.'
                )
        except Exception as e:
            self._logger.warning(f'Tiling: stage position sync failed: {e}')

    def _defaultCamera(self) -> str:
        for name, info in self._setupInfo.detectors.items():
            if info.forAcquisition:
                return name
        return next(iter(self._setupInfo.detectors))

    def _detectorPixelSizeUm(self, detector) -> Tuple[float, float]:
        """Return the delivered-frame pixel size as ``(y, x)`` in micrometres.

        ``DetectorManager.pixelSizeUm`` reports the *unbinned* sample-plane
        pixel size — ``setBinning`` changes the frame the camera delivers but
        never touches that parameter. Stitching needs the size of a pixel in
        the array it actually receives, so binning is applied here. Without it
        a binned scan lays tiles out ``binning`` times too far apart and the
        mosaic never overlaps cleanly, which looks exactly like stage error.
        """
        pixel_size_um = detector.pixelSizeUm
        if len(pixel_size_um) >= 3:
            size_y, size_x = float(pixel_size_um[-2]), float(pixel_size_um[-1])
        elif len(pixel_size_um) == 2:
            size_y, size_x = float(pixel_size_um[0]), float(pixel_size_um[1])
        elif len(pixel_size_um) == 1:
            size_y = size_x = float(pixel_size_um[0])
        else:
            raise ValueError(
                f'Detector {detector.name} has invalid pixelSizeUm: {pixel_size_um}'
            )

        # Binning is a camera concept. A scan-driven detector's pixel size IS
        # the scan step (see scanPixelSizesToZYX), already in the value above,
        # so scaling it by the inherited binning field would corrupt it.
        binning = 1
        if not getattr(detector, 'isScanDriven', False):
            try:
                binning = max(1, int(getattr(detector, 'binning', 1) or 1))
            except (TypeError, ValueError):
                binning = 1
        if binning != 1:
            self._logger.info(
                f'Tiling: detector binning {binning} — effective tile pixel '
                f'size {size_y * binning:.4g} x {size_x * binning:.4g} µm'
            )
        return size_y * binning, size_x * binning


# Copyright (C) 2020-2021 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
