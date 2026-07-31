"""
Shared base controller for event-triggered acquisition modules.

EtSTED and EtMonalisa are two near-identical workflows (fast widefield
detection of a transient event → coordinate transform → triggered scan
of a slower modality on the event location).  Historically each lived
in its own ~850-line controller with ~90% duplicated code.

This module hoists everything that is genuinely shared into
:class:`EventTriggeredControllerBase` and exposes a small set of
template hooks for the (small) per-modality divergences:

  * ``_pre_arm_hook`` / ``_post_stop_hook`` — extra steps around
    initiate / stop (EtMonalisa flips a microscope-stand mode flag).
  * ``_on_pause_modality_hook`` / ``_on_resume_modality_hook`` —
    extra steps when pausing/resuming the fast modality during an
    event scan (EtMonalisa switches the stand between FLUO and CS).
  * ``_validate_pre_run`` — extra pre-flight validation (EtSTED
    checks scan parameters via the triggered-scan runner).
  * ``_set_status`` / ``_set_controls_armed`` — UI feedback that only
    EtSTED currently exposes; EtMonalisa's defaults are no-ops.
  * ``_transform_apply_extra_arg`` — second arg passed to
    ``EtSTEDTransformService.apply``; EtSTED supplies setup-info,
    EtMonalisa passes ``None``.

The coordinate-transform helper is also unified in
:class:`EventTriggeredCoordTransformHelper`, using the polynomial fit
provided by :class:`EtSTEDTransformService`.  This fixes the
EtMonalisa calibration round-trip, which previously wrote only a
FOV-centre TXT file that ``loadTransform`` could not read back.
"""

from __future__ import annotations

import csv
import os
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import h5py
import numpy as np
import pyqtgraph as pg
import scipy.ndimage as ndi
from qtpy import QtCore
from qtpy.QtWidgets import QFileDialog

from imswitch.imcommon.model import dirtools, initLogger
from imswitch.imcontrol.model.EtSTEDAutoCalibration import (
    AutoCalibrationResult,
    auto_calibrate,
)
from imswitch.imcontrol.model.EtSTEDPipelineRunner import EtSTEDPipelineRunner
from imswitch.imcontrol.model.EtSTEDTransformService import EtSTEDTransformService
from imswitch.imcontrol.model.EtSTEDTriggeredScanRunner import EtSTEDTriggeredScanRunner
from imswitch.imcontrol.model.managers import LeasePurpose
from imswitch.imcontrol.model.managers._scan_execution import (
    FINISH_ABORT, FINISH_GRACEFUL, getSharedScanExecutionCoordinator,
)
from imswitch.imcontrol.model.EventTriggeredSession import (
    EventRunMode as RunMode,
    EventScanInitiationMode as ScanInitiationMode,
    EventTriggeredSessionState,
)

from ..basecontrollers import ImConWidgetController
from .SmartModeRoleMixin import SmartModeRoleMixin


def _now_us_tag() -> str:
    """High-resolution timestamp string used in per-event log lines."""
    return datetime.now().strftime('%Ss%fus')


def _now_utc_short() -> str:
    return datetime.now(timezone.utc).strftime('%Hh%Mm%Ss')


def _now_utc_long() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d-%Hh%Mm%Ss')


def _now_utc_fileus() -> str:
    return datetime.now(timezone.utc).strftime('%Hh%Mm%Ss%fus')


def _millis() -> float:
    """Monotonic millisecond timestamp."""
    return time.perf_counter_ns() / 1e6


# Hard-coded sample period fallback for legacy signal designers that
# don't expose ``sample_rate`` in the scan-info dict.  100 kHz NIDAQ.
_DEFAULT_SAMPLE_RATE_HZ = 100_000


# ─────────────────────────────────────────────────────────────────────────────
# Base controller
# ─────────────────────────────────────────────────────────────────────────────


class EventTriggeredControllerBase(SmartModeRoleMixin, ImConWidgetController):
    """Shared logic for EtSTED / EtMonalisa controllers.

    Subclasses must set the class attributes below and may override
    the ``_*_hook`` template methods.

    Smart-microscopy role switching is provided by :class:`SmartModeRoleMixin`.
    The base itself sets ``SMART_MODE_WORKFLOW = None`` so it is a no-op; each
    concrete subclass declares its own workflow name to opt in. When the rollout
    flag is off (or no service is injected), every role-application call below is
    a complete no-op and the lifecycle behaves byte-for-byte as before.
    """

    # Subclass overrides ────────────────────────────────────────────────────
    LOGS_SUBFOLDER: str = 'logs_event_triggered'
    MODALITY_LABEL: str = 'event-triggered'
    BINARY_FRAMES: int = 10
    BINARY_MASK_TIMEOUT_MS: int = 10_000
    INIT_FRAMES: int = 5
    VALIDATION_FRAMES_LIMIT: int = 5
    FLIP_WF_CALIB: bool = True
    FAST_AXIS_SHIFT_COEFFICIENTS = (
        -5.06873628,
        -80.6978355,
        104.06976744,
        -7.12113356,
        8.0065076,
        0.68227188,
    )
    ANALYSIS_SCATTER_PIPELINE_NAME_MARKERS = ('cd_vesicle_prox', 'dynamin')

    # Smart-microscopy role switching — subclasses set their own workflow name.
    SMART_MODE_WORKFLOW = None
    SMART_MODE_REQUIRED_ROLES = ('scouting', 'event')

    # ── Construction ──────────────────────────────────────────────────────── #

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._logsDir = os.path.join(
            dirtools.UserFileDirs.Root, 'recordings', self.LOGS_SUBFOLDER
        )

        self._widget.setFastDetectorList(
            self._master.detectorsManager.execOnAll(
                lambda c: c.name, condition=lambda c: c.forAcquisition
            )
        )
        self._widget.setFastLaserList(
            self._master.lasersManager.execOnAll(lambda c: c.name)
        )

        self.scanInitiationList = ['ScanWidget', 'RecordingWidget']
        self._widget.setScanInitiationList(self.scanInitiationList)

        sys.path.append(self._widget.analysisDir)
        if hasattr(self._widget, 'transformDir'):
            sys.path.append(self._widget.transformDir)

        # Service objects — used by both modalities.
        self._pipelineRunner = EtSTEDPipelineRunner()
        self._transformService = EtSTEDTransformService()
        self._triggeredScanRunner = EtSTEDTriggeredScanRunner()
        self._smartModeService = None

        # EVENT_STREAM lease on detectorFast, held for the detection loop's
        # lifetime (see _acquireDetectorFastStream).
        self._detectorFastHandle = None
        self._detectorFastReleasePending = False
        self._binaryMaskHandle = None
        self._binaryMaskGeneration = 0
        self._binaryMaskFrameSlot = None

        # The fifth NI-DAQ entry point shares the same global iteration owner as
        # the scan widget. Its token is tagged with this controller as owner, so
        # only this completion path may resume the event modality.
        self._scanCoordinator = getSharedScanExecutionCoordinator(
            self._master.detectorsManager,
            self._master.nidaqManager,
            logger=self._logger,
            scheduleTimeout=lambda delayS, callback: QtCore.QTimer.singleShot(
                int(delayS * 1000), callback
            ),
        )
        self._master.nidaqManager.sigScanDone.connect(
            self._onTriggeredNidaqScanDone
        )
        self._master.nidaqManager.sigScanBuildFailed.connect(
            self._onTriggeredNidaqScanBuildFailed
        )
        self._nidaqCompletionSignalsConnected = True

        # Helper for the coordinate-transform calibration sub-window.
        self._coordTransformHelper = EventTriggeredCoordTransformHelper(
            self, self._widget.coordTransformWidget, self._logsDir
        )

        # Backwards-compat: a couple of paths still reach for self.transform
        # / self.__transformCoeffs.  Keep those references warm.
        self._transformCoeffs = np.zeros(20)

        # UI signals
        self._widget.initiateButton.clicked.connect(self.initiate)
        self._widget.loadPipelineButton.clicked.connect(self.loadPipeline)
        self._widget.recordBinaryMaskButton.clicked.connect(self.initiateBinaryMask)
        self._widget.loadScanParametersButton.clicked.connect(self.getScanParameters)
        self._widget.setUpdatePeriodButton.clicked.connect(self.setUpdatePeriod)
        self._widget.setBusyFalseButton.clicked.connect(self.setBusyFalse)

        # Comm-channel signals — direct connections so closeEvent can disconnect.
        self._commChannel.sigSendScanParameters.connect(self.assignScanParameters)
        self._commChannel.sigSendScanFreq.connect(self.logScanFreq)

        # Runtime state and buffers.
        self._state = EventTriggeredSessionState()
        self.resetDetLog()

        self._prevFrames = deque(maxlen=10)
        self._prevAnaFrames = deque(maxlen=10)
        self._binary_mask: Optional[np.ndarray] = None
        self._binary_stack_list: list[np.ndarray] = []
        self._param_vals: list[float] = []
        self._exinfo = None
        self._analogParameterDict: dict = {}
        self._digitalParameterDict: dict = {}
        self._positionersScan: list = []
        self._updatePeriod: Optional[int] = None
        self.signalDic = None
        self.scanInfoDict = None
        # ``_state.running`` means the fast modality is currently producing
        # frames, so it becomes False while a slow scan is in progress. Keep a
        # separate session lifetime flag so Stop during that pause cannot be
        # mistaken for a new Initiate click.
        self._experimentActive = False
        self._stopRequested = False
        self._closed = False
        # Only direct ScanWidget slow scans publish/own this lifecycle.
        # RecordingWidget keeps its existing RecordingController ownership.
        self._triggeredScanRunToken = None
        self._triggeredScanStartingPublished = False
        self._triggeredScanCompletionPublishing = False
        # RecordingWidget owns its scan lifecycle, so no coordinator token can
        # distinguish an unrelated recording signal from this controller's
        # triggered recording. Keep an explicit operation gate instead.
        self._triggeredRecordingInFlight = False
        self._triggeredRecordingGeneration = None
        self._recordingGenerationBeforeTrigger = None
        self._triggeredRecordingManagerTerminal = False
        self._triggeredRecordingLifecycleEnded = False
        self._triggeredRecordingFailureMessage = None
        self._triggeredRecordingCaptured = False
        self._triggeredRecordingScanSource = None
        self._triggeredRecordingRunToken = None
        self._triggeredRecordingSourceObservedRunning = False
        self._usesDetailedTriggeredRecordingSignals = False

    # ``setSmartModeService`` is provided by SmartModeRoleMixin.

    # ── Subclass hooks (default no-ops) ──────────────────────────────────── #

    def _pre_arm_hook(self) -> None:
        """Called inside :meth:`initiate` before signal wiring & laser-on."""

    def _post_stop_hook(self, *, reset_params: bool) -> None:
        """Called at the end of :meth:`stopExperiment`."""

    def _on_pause_modality_hook(self) -> None:
        """Extra teardown when pausing the fast modality for an event scan."""

    def _on_resume_modality_hook(self) -> None:
        """Extra setup when resuming the fast modality after an event scan."""

    def _validate_pre_run(self) -> None:
        """Extra validation before arming (e.g. scan-parameter sanity check)."""

    def _set_status(self, status: str, message: str = '') -> None:
        """Optional widget status text update."""
        if hasattr(self._widget, 'setEtSTEDStatus'):
            self._widget.setEtSTEDStatus(status, message)

    def _set_controls_armed(self, armed: bool) -> None:
        """Optional widget controls-armed visual lock."""
        if hasattr(self._widget, 'setEtSTEDControlsArmed'):
            self._widget.setEtSTEDControlsArmed(armed)

    def _transform_apply_extra_arg(self):
        """Second arg passed to ``transformService.apply``.  Override per setup."""
        return None

    # ── Initiate / stop ──────────────────────────────────────────────────── #

    def initiate(self) -> None:
        """Start (or stop) an event-triggered experiment."""
        if (self.__dict__.get('_experimentActive', False)
                or self._state.running):
            self.stopExperiment(resetParams=True)
            return
        if self.__dict__.get('_closed', False):
            return

        self._stopRequested = False
        os.makedirs(self._logsDir, exist_ok=True)
        self._set_status('arming')

        try:
            self._prepareExperiment()
            if self._smartModeSwitchingEnabled() and not self._preflightSmartModeRoles():
                raise RuntimeError(
                    f'Smart microscopy mode preflight failed for {self.MODALITY_LABEL}.'
                )
            self._pre_arm_hook()
            # Apply the scouting beam-path mode *before* turning the fast laser on:
            # the mode owns the beam path, the controller owns laser emission.
            if self._smartModeSwitchingEnabled() and not self._applySmartModeRole(
                'scouting', required=True
            ):
                raise RuntimeError(
                    f'Failed to apply scouting smart microscopy mode for '
                    f'{self.MODALITY_LABEL}.'
                )
            self._connectRunSignals()
            self._setFastLaserEnabled(True, require_success=True)

            self._widget.initiateButton.setText('Stop')
            self._set_controls_armed(True)
            self._set_status('detecting')
            self._experimentActive = True
            self._state.running = True
        except Exception as e:
            self._logger.error(
                f'Failed to initiate {self.MODALITY_LABEL} experiment: {e}',
                exc_info=True,
            )
            self.stopExperiment(resetParams=True)
            self._set_status('error', str(e))

    def stopExperiment(self, resetParams: bool = False) -> None:
        """Best-effort stop path that leaves lasers and scan UI in a safe state."""
        self._stopRequested = True
        self._experimentActive = False
        self._disconnectRunSignals()
        try:
            self._setFastLaserEnabled(False)
        finally:
            # Best-effort safe idle mode once the fast laser is off; never raises,
            # since the stop path must always complete.
            if self._smartModeSwitchingEnabled():
                self._applySmartModeRoleIfConfigured('idle')
            self._cleanupBinaryMaskRecording()
            self._widget.initiateButton.setText('Initiate')
            self._set_controls_armed(False)
            if resetParams:
                self.resetParamVals()
            self.resetRunParams()
            if resetParams:
                self._set_status('idle')
            self._post_stop_hook(reset_params=resetParams)
            # A direct slow scan retains its run reservation until NI-DAQ and
            # detector finish barriers are actually complete. If none is in
            # flight, terminal cleanup can happen now.
            self._finishTriggeredScanRunIfIdle()

    def closeEvent(self) -> bool:
        """Stop safely and report whether the owned scan barrier has drained."""
        self._closed = True
        self.stopExperiment(resetParams=True)
        # Disconnect the comm-channel slots wired in __init__.
        self._safeDisconnect(self._commChannel.sigSendScanParameters, self.assignScanParameters)
        self._safeDisconnect(self._commChannel.sigSendScanFreq, self.logScanFreq)
        self._disconnectNidaqCompletionSignalsIfIdle()
        return self.shutdownComplete()

    def shutdownComplete(self) -> bool:
        """Whether close-time scan ownership and completion wiring are gone."""
        if self.__dict__.get('_closed', False):
            # A last EVENT_STREAM release can fail before consuming its handle
            # when the frame poller misses its bounded stop deadline. Keep the
            # exact handles and retry while the factory pumps close-time events;
            # otherwise a later-stopped poller would leave an ownerless lease.
            self._releaseDetectorFastStream()
            self._releaseBinaryMaskLease()

        coordinator = self.__dict__.get('_scanCoordinator')
        if coordinator is not None:
            if (
                coordinator.tokenForOwner(self) is not None
                or coordinator.runForOwner(self) is not None
            ):
                return False

        if self.__dict__.get('_closed', False):
            self._disconnectNidaqCompletionSignalsIfIdle()

        state = self.__dict__.get('_state')
        return (
            self.__dict__.get('_detectorFastHandle') is None
            and self.__dict__.get('_binaryMaskHandle') is None
            and not self.__dict__.get('_triggeredRecordingInFlight', False)
            and not self.__dict__.get(
                '_triggeredScanCompletionPublishing', False
            )
            and not self.__dict__.get(
                '_triggeredScanStartingPublished', False
            )
            and not bool(getattr(state, 'running', False))
            and (
                not self.__dict__.get('_closed', False)
                or not self.__dict__.get(
                    '_nidaqCompletionSignalsConnected', False
                )
            )
        )

    # ── Experiment preparation ───────────────────────────────────────────── #

    def _prepareExperiment(self) -> None:
        """Validate UI selections and load runtime objects before arming."""
        detectorFastIdx = self._widget.fastImgDetectorsPar.currentIndex()
        self._state.detectorFast = self._widget.fastImgDetectors[detectorFastIdx]
        self.detectorFast = self._state.detectorFast

        laserFastIdx = self._widget.fastImgLasersPar.currentIndex()
        self._state.laserFast = self._widget.fastImgLasers[laserFastIdx]
        self.laserFast = self._state.laserFast

        scanInitiationTypeIdx = self._widget.scanInitiationPar.currentIndex()
        scanInitiationType = self._widget.scanInitiation[scanInitiationTypeIdx]
        if scanInitiationType == self.scanInitiationList[0]:
            self._state.scanInitiationMode = ScanInitiationMode.ScanWidget
        elif scanInitiationType == self.scanInitiationList[1]:
            self._state.scanInitiationMode = ScanInitiationMode.RecordingWidget
        else:
            raise ValueError(f'Unknown scan initiation type: {scanInitiationType}')
        self.scanInitiationMode = self._state.scanInitiationMode

        if not self._widget.analysisPipelines:
            raise RuntimeError(f'No {self.MODALITY_LABEL} analysis pipeline is available.')
        if not self._widget.transformPipelines:
            raise RuntimeError(f'No {self.MODALITY_LABEL} coordinate transform pipeline is available.')
        if not self._widget.transformCoefs:
            raise RuntimeError(f'No {self.MODALITY_LABEL} coordinate transform coefficients are available.')
        if self._pipelineRunner.function is None:
            self.loadPipeline()

        self._param_vals = self.readParams()
        self._exinfo = None

        experimentModeIdx = self._widget.experimentModesPar.currentIndex()
        self.experimentMode = self._widget.experimentModes[experimentModeIdx]
        if self.experimentMode == 'TestVisualize':
            self._state.runMode = RunMode.Visualize
        elif self.experimentMode == 'TestValidate':
            self._state.runMode = RunMode.Validate
        else:
            self._state.runMode = RunMode.Experiment

        if self._state.runMode == RunMode.Experiment:
            self._validate_pre_run()

        if self._state.runMode in (RunMode.Validate, RunMode.Visualize):
            self.launchHelpWidget()

        self.loadTransform()

    # ── Run-signal wiring ────────────────────────────────────────────────── #

    def _connectRunSignals(self) -> None:
        if not self._state.imageSignalConnected:
            # EVENT_STREAM, not a plain arming lease: the detection loop feeds
            # off sigUpdateImage, which only fires for detectors in the
            # frame-stream membership. Without this the loop silently stalls
            # whenever live view is off — detectorFast would be armed but
            # never polled.
            self._acquireDetectorFastStream()
            self._commChannel.sigUpdateImage.connect(self.runPipeline)
            self._state.imageSignalConnected = True
        if not self._state.scanEndSignalConnected:
            if self._state.scanInitiationMode == ScanInitiationMode.ScanWidget:
                self._commChannel.sigToggleBlockScanWidget.emit(False)
            elif self._state.scanInitiationMode == ScanInitiationMode.RecordingWidget:
                recordingManager = getattr(
                    self.__dict__.get('_master'),
                    'recordingManager',
                    None,
                )
                detailedSignals = (
                    hasattr(
                        getattr(
                            recordingManager,
                            'sigRecordingEndedDetailed',
                            None,
                        ),
                        'connect',
                    )
                    and hasattr(
                        getattr(
                            recordingManager,
                            'sigRecordingFailedDetailed',
                            None,
                        ),
                        'connect',
                    )
                )
                self._usesDetailedTriggeredRecordingSignals = detailedSignals
                if detailedSignals:
                    recordingManager.sigRecordingEndedDetailed.connect(
                        self._onTriggeredRecordingEndedDetailed
                    )
                    recordingManager.sigRecordingFailedDetailed.connect(
                        self._onTriggeredRecordingFailedDetailed
                    )
                    scanEndedSignal = getattr(
                        self._commChannel, 'sigScanEnded', None
                    )
                    if hasattr(scanEndedSignal, 'connect'):
                        scanEndedSignal.connect(
                            self._onTriggeredRecordingLifecycleEnded
                        )
                else:
                    self._commChannel.sigRecordingEnded.connect(
                        self.scanEnded
                    )
                    self._commChannel.sigRecordingFailed.connect(
                        self._onTriggeredRecordingFailed
                    )
            self._state.scanEndSignalConnected = True

    def _disconnectRunSignals(self) -> None:
        if self._state.imageSignalConnected:
            self._safeDisconnect(self._commChannel.sigUpdateImage, self.runPipeline)
            self._state.imageSignalConnected = False
        # Unconditional: pauseFastModality() disconnects the image signal while
        # deliberately KEEPING the lease, so a release guarded by
        # imageSignalConnected would skip a paused run and leak the handle for
        # the rest of the session. This is the terminal cleanup path — the
        # detector goes back whatever state the run was left in.
        self._releaseDetectorFastStream()
        if self._state.scanEndSignalConnected:
            if self._state.scanInitiationMode == ScanInitiationMode.ScanWidget:
                self._commChannel.sigToggleBlockScanWidget.emit(True)
            elif self._state.scanInitiationMode == ScanInitiationMode.RecordingWidget:
                recordingManager = getattr(
                    self.__dict__.get('_master'),
                    'recordingManager',
                    None,
                )
                if self.__dict__.get(
                    '_usesDetailedTriggeredRecordingSignals', False
                ):
                    self._safeDisconnect(
                        recordingManager.sigRecordingEndedDetailed,
                        self._onTriggeredRecordingEndedDetailed,
                    )
                    self._safeDisconnect(
                        recordingManager.sigRecordingFailedDetailed,
                        self._onTriggeredRecordingFailedDetailed,
                    )
                    scanEndedSignal = getattr(
                        self._commChannel, 'sigScanEnded', None
                    )
                    if scanEndedSignal is not None:
                        self._safeDisconnect(
                            scanEndedSignal,
                            self._onTriggeredRecordingLifecycleEnded,
                        )
                else:
                    self._safeDisconnect(
                        self._commChannel.sigRecordingEnded,
                        self.scanEnded,
                    )
                    self._safeDisconnect(
                        self._commChannel.sigRecordingFailed,
                        self._onTriggeredRecordingFailed,
                    )
            self._state.scanEndSignalConnected = False
        self._triggeredRecordingInFlight = False
        self._triggeredRecordingGeneration = None
        self._recordingGenerationBeforeTrigger = None
        self._triggeredRecordingManagerTerminal = False
        self._triggeredRecordingLifecycleEnded = False
        self._triggeredRecordingFailureMessage = None
        self._triggeredRecordingCaptured = False
        self._triggeredRecordingScanSource = None
        self._triggeredRecordingRunToken = None
        self._triggeredRecordingSourceObservedRunning = False

    @staticmethod
    def _safeDisconnect(signal, slot) -> None:
        try:
            signal.disconnect(slot)
        except (TypeError, RuntimeError):
            pass

    def _disconnectNidaqCompletionSignals(self) -> None:
        if not self.__dict__.get(
            '_nidaqCompletionSignalsConnected', False
        ):
            return
        self._safeDisconnect(
            self._master.nidaqManager.sigScanDone,
            self._onTriggeredNidaqScanDone,
        )
        self._safeDisconnect(
            self._master.nidaqManager.sigScanBuildFailed,
            self._onTriggeredNidaqScanBuildFailed,
        )
        self._nidaqCompletionSignalsConnected = False

    def _disconnectNidaqCompletionSignalsIfIdle(self) -> None:
        if not self.__dict__.get('_closed', False):
            return
        coordinator = self.__dict__.get('_scanCoordinator')
        if (
            coordinator is None
            or (
                coordinator.tokenForOwner(self) is None
                and coordinator.runForOwner(self) is None
            )
        ):
            self._disconnectNidaqCompletionSignals()

    def _acquireDetectorFastStream(self) -> None:
        """Hold detectorFast for the detection loop's lifetime."""
        if self.__dict__.get('_detectorFastHandle') is not None:
            if not self.__dict__.get(
                '_detectorFastReleasePending', False
            ):
                # pauseFastModality deliberately retains a healthy stream
                # lease while the slow scan runs.
                return
            if not self._releaseDetectorFastStream():
                raise RuntimeError(
                    'The previous event-stream lease is still stopping.'
                )
        detectorFast = self._state.detectorFast
        if not detectorFast:
            return
        try:
            self._detectorFastHandle = self._master.detectorsManager.acquire(
                [detectorFast], LeasePurpose.EVENT_STREAM
            )
            self._detectorFastReleasePending = False
        except Exception as e:
            # Surface it: without the lease the loop runs but never sees a
            # frame, which is far harder to diagnose than a failed start.
            self._logger.error(
                f'Failed to lease detector "{detectorFast}" for the '
                f'{self.MODALITY_LABEL} detection loop: {e}', exc_info=True
            )
            raise

    def _releaseDetectorFastStream(self) -> bool:
        handle = self.__dict__.get('_detectorFastHandle')
        if handle is None:
            return True
        try:
            self._master.detectorsManager.release(handle)
        except Exception as e:
            self._detectorFastReleasePending = True
            self._logger.error(
                f'Failed to release the {self.MODALITY_LABEL} detection-loop '
                f'detector lease: {e}', exc_info=True
            )
            # release() can fail before consuming a last frame-stream handle
            # (notably when its poll thread has not stopped yet). Retain the
            # token so stop/close can retry the same ownership transition.
            return False
        if self._detectorFastHandle is handle:
            self._detectorFastHandle = None
        self._detectorFastReleasePending = False
        return True

    def _setFastLaserEnabled(self, enabled: bool, *, require_success: bool = False) -> None:
        if self._state.laserFast is None:
            return
        try:
            self._master.lasersManager.execOn(
                self._state.laserFast, lambda l: l.setEnabled(enabled)
            )
        except Exception as e:
            self._logger.error(
                f'Failed to set fast laser {self._state.laserFast} enabled={enabled}: {e}',
                exc_info=True,
            )
            if require_success:
                raise

    # ── Pipeline & transform loading ─────────────────────────────────────── #

    def getPipelineName(self) -> str:
        idx = self._widget.analysisPipelinePar.currentIndex()
        return self._widget.analysisPipelines[idx]

    def getTransformName(self) -> str:
        idx = self._widget.transformPipelinePar.currentIndex()
        return self._widget.transformPipelines[idx]

    def getTransformCoefName(self) -> str:
        idx = self._widget.transformCoefsPar.currentIndex()
        return self._widget.transformCoefs[idx]

    def loadPipeline(self) -> None:
        name = self.getPipelineName()
        self._pipelineName = name
        self._pipeline_params = self._pipelineRunner.load(name)
        self.pipeline = self._pipelineRunner.function
        self._widget.initParamFields(self._pipeline_params)
        self._set_status('idle', f'Loaded pipeline: {name}')

    def loadTransform(self) -> None:
        transformName = self.getTransformName()
        transformCoefName = self.getTransformCoefName()
        self._transformService.load(self._widget.transformDir, transformName, transformCoefName)
        self.transform = self._transformService.function
        self._transformCoeffs = self._transformService.coefficients

    def readParams(self) -> list[float]:
        return self._pipelineRunner.parse_parameter_values(self._widget.param_edits)

    # ── Binary mask acquisition ─────────────────────────────────────────── #

    def initiateBinaryMask(self) -> None:
        # Repeated clicks restart a capture rather than stacking signal
        # connections, leases and timeout callbacks.
        self._cleanupBinaryMaskRecording()
        if self.__dict__.get('_binaryMaskHandle') is not None:
            self._set_status(
                'error',
                'The previous binary-mask detector lease is still stopping; '
                'retry after cleanup completes.',
            )
            return
        self._binary_stack_list = []
        laserFastIdx = self._widget.fastImgLasersPar.currentIndex()
        self._state.laserFast = self._widget.fastImgLasers[laserFastIdx]
        self.laserFast = self._state.laserFast
        detectorFastIdx = self._widget.fastImgDetectorsPar.currentIndex()
        self._state.detectorFast = self._widget.fastImgDetectors[detectorFastIdx]
        self.detectorFast = self._state.detectorFast
        try:
            self._binaryMaskHandle = self._master.detectorsManager.acquire(
                [self._state.detectorFast], LeasePurpose.EVENT_STREAM
            )
            self._setFastLaserEnabled(True, require_success=True)
            self._binaryMaskGeneration += 1
            generation = self._binaryMaskGeneration
            # Capture identity on the connected callback itself. Disconnecting
            # a Qt signal does not retract deliveries that were already queued;
            # without this token, a frame from an old capture can contaminate a
            # newly started mask stack.
            self._binaryMaskFrameSlot = (
                lambda detectorName, img, init, scale, isCurrentDetector,
                       captureGeneration=generation:
                    self.addImgBinStack(
                        detectorName, img, init, scale, isCurrentDetector,
                        captureGeneration=captureGeneration,
                    )
            )
            self._commChannel.sigUpdateImage.connect(
                self._binaryMaskFrameSlot
            )
            self._state.binaryMaskSignalConnected = True
            self._widget.recordBinaryMaskButton.setText('Recording...')
            QtCore.QTimer.singleShot(
                self.BINARY_MASK_TIMEOUT_MS,
                lambda: self._onBinaryMaskTimeout(generation),
            )
        except Exception as e:
            self._logger.error(
                f'Could not start binary-mask acquisition: {e}',
                exc_info=True,
            )
            self._set_status('error', f'Could not record binary mask: {e}')
            self._cleanupBinaryMaskRecording()

    def addImgBinStack(self, detectorName, img, init, scale,
                       isCurrentDetector, *, captureGeneration=None) -> None:
        del init, scale, isCurrentDetector
        if (
            detectorName != self._state.detectorFast
            or not self._state.binaryMaskSignalConnected
            or self.__dict__.get('_binaryMaskHandle') is None
            or (
                captureGeneration is not None
                and captureGeneration != self._binaryMaskGeneration
            )
        ):
            return
        if len(self._binary_stack_list) >= self.BINARY_FRAMES:
            return  # late frame after disconnect
        self._binary_stack_list.append(np.asarray(img))
        if len(self._binary_stack_list) >= self.BINARY_FRAMES:
            stack = np.stack(self._binary_stack_list, axis=0)
            self._cleanupBinaryMaskRecording()
            self.calculateBinaryMask(stack)

    def _onBinaryMaskTimeout(self, generation: int) -> None:
        if (generation != self._binaryMaskGeneration
                or not self._state.binaryMaskSignalConnected):
            return
        self._logger.warning(
            f'Binary-mask acquisition timed out after '
            f'{self.BINARY_MASK_TIMEOUT_MS / 1000:g} s.'
        )
        self._set_status('error', 'Binary-mask acquisition timed out.')
        self._cleanupBinaryMaskRecording()

    def calculateBinaryMask(self, img_stack: np.ndarray) -> None:
        img_mean = np.mean(img_stack, 0)
        img_bin = ndi.gaussian_filter(img_mean, float(self._widget.bin_smooth_edit.text()))
        self._binary_mask = np.array(img_bin > float(self._widget.bin_thresh_edit.text()))
        self._widget.recordBinaryMaskButton.setText('Record binary mask')
        self.setAnalysisHelpImg(self._binary_mask)
        self.launchHelpWidget()

    def _cleanupBinaryMaskRecording(self) -> None:
        """Stop an interrupted capture and return every owned resource."""
        self._binaryMaskGeneration = getattr(
            self, '_binaryMaskGeneration', 0
        ) + 1
        signalConnected = bool(self._state.binaryMaskSignalConnected)
        handle = self.__dict__.get('_binaryMaskHandle')
        frameSlot = self.__dict__.get('_binaryMaskFrameSlot')
        hadActiveCapture = signalConnected or handle is not None
        if signalConnected:
            self._safeDisconnect(
                self._commChannel.sigUpdateImage,
                frameSlot if frameSlot is not None else self.addImgBinStack,
            )
            self._state.binaryMaskSignalConnected = False
        self._binaryMaskFrameSlot = None
        # initiateBinaryMask() pre-cleans stale capture state. Turning the laser
        # off when there was no mask signal/lease would also extinguish an
        # unrelated active event-detection run.
        if hadActiveCapture:
            self._setFastLaserEnabled(False)
        self._releaseBinaryMaskLease()
        self._binary_stack_list = []
        if hasattr(self._widget, 'recordBinaryMaskButton'):
            self._widget.recordBinaryMaskButton.setText('Record binary mask')

    def _releaseBinaryMaskLease(self) -> bool:
        """Release one mask-capture lease without losing retry authority."""
        handle = self.__dict__.get('_binaryMaskHandle')
        if handle is None:
            return True
        try:
            self._master.detectorsManager.release(handle)
        except Exception as e:
            self._logger.error(
                f'Failed to release binary-mask detector lease: {e}',
                exc_info=True,
            )
            return False
        if self.__dict__.get('_binaryMaskHandle') is handle:
            self._binaryMaskHandle = None
        return True

    def setAnalysisHelpImg(self, img_ana: np.ndarray, exinfo=None) -> None:
        if np.max(img_ana) > self._state.maxAnaImgVal:
            self._state.maxAnaImgVal = float(np.max(img_ana))
            autolevels = True
        else:
            autolevels = False
        if img_ana.ndim == 3:
            img_ana = img_ana[0, :, :]
        self._widget.analysisHelpWidget.img.setImage(img_ana, autoLevels=autolevels)
        self._widget.analysisHelpWidget.info_label.setText(
            f'Min: {np.min(img_ana)}, max: {np.max(img_ana)}'
        )

        if exinfo is not None and self._pipelineSupportsAnalysisScatter():
            self._widget.analysisHelpWidget.scatter.setData(
                x=np.array(exinfo['y']), y=np.array(exinfo['x']),
                pen=pg.mkPen(None), brush='g', symbol='x', size=15,
            )

    def _pipelineSupportsAnalysisScatter(self) -> bool:
        pipeline_name = self.__dict__.get('_pipelineName', '')
        return any(
            marker in pipeline_name
            for marker in self.ANALYSIS_SCATTER_PIPELINE_NAME_MARKERS
        )

    # ── Scan parameter mgmt ─────────────────────────────────────────────── #

    def getScanParameters(self) -> None:
        self._commChannel.scanWorkflow.request_scan_parameters()

    def setUpdatePeriod(self) -> None:
        self._updatePeriod = int(self._widget.update_period_edit.text())
        self._master.detectorsManager.setUpdatePeriod(self._updatePeriod)

    def setBusyFalse(self) -> None:
        self._state.busy = False

    def assignScanParameters(self, analogParams, digitalParams, positionersScan) -> None:
        self._analogParameterDict = analogParams.copy()
        self._digitalParameterDict = digitalParams.copy()
        self._positionersScan = positionersScan.copy()
        self.setScanParametersStatus(analogParams, positionersScan)

    def setScanParametersStatus(self, scanInfo: dict, positionersScan: list) -> None:
        pixel_sizes = scanInfo['axis_step_size']
        axis_lens = scanInfo['axis_length']
        pixels = np.divide(axis_lens, pixel_sizes)
        dwell_time = scanInfo['sequence_time'] * 1e6
        dwell_time_message = f'{dwell_time:.0f} µs'
        scan_axes = [p for p in positionersScan if p != 'None']
        scan_axes_message = ' x '.join(map(str, scan_axes))
        size_list = [length for length, pixel in zip(axis_lens, pixels) if pixel != 1]
        size_message = ' x '.join(map(str, size_list)) + ' µm'
        pixel_sizes_list = [pxsize for pxsize, pixel in zip(pixel_sizes, pixels) if pixel != 1]
        pixel_sizes_message = ' x '.join(map(str, pixel_sizes_list)) + ' µm'

        self._widget.loadScanParametersStatus.setText(
            f'Current scan loaded: axes: {scan_axes_message}, '
            f'axis lengths: {size_message}, pixel sizes: {pixel_sizes_message}, '
            f'dwell time: {dwell_time_message}'
        )
        self._set_status('idle', 'Scan parameters loaded.')

    # ── Per-frame pipeline driver ───────────────────────────────────────── #

    def runPipeline(self, detectorName, img, init, scale, isCurrentDetector) -> None:
        """Dispatch one fast-detector frame through the analysis pipeline."""
        del init, scale, isCurrentDetector
        if (
            detectorName != self._state.detectorFast
            or self._state.busy
            or self.__dict__.get('_closed', False)
            or self.__dict__.get('_stopRequested', False)
            or not self.__dict__.get('_experimentActive', False)
            or not self._state.running
        ):
            return

        now_ms = _millis()
        self.setDetLogLine('pipeline_rep_period', str(now_ms - self._state.tCallMs))
        self._state.tCallMs = now_ms
        self.setDetLogLine('pipeline_start', _now_us_tag())
        self._state.busy = True

        try:
            result = self._pipelineRunner.execute(
                img,
                self._prevFrames,
                self._binary_mask,
                self._state.runMode in (RunMode.Visualize, RunMode.Validate),
                self._exinfo,
                self._param_vals,
            )
            coords_detected = result.coords_detected
            self._exinfo = result.exinfo
            img_ana = result.analysis_image
        except Exception as e:
            self._logger.error(f'{self.MODALITY_LABEL} pipeline failed: {e}', exc_info=True)
            self._set_status('error', str(e))
            self.setBusyFalse()
            return

        self.setDetLogLine('pipeline_end', _now_us_tag())

        if self._state.frame <= self.INIT_FRAMES:
            # Warmup frames: just count them and stash for prev_frames context.
            self._prevFrames.append(img)
            if self._state.runMode == RunMode.Validate and img_ana is not None:
                self._prevAnaFrames.append(img_ana)
            self._state.frame += 1
            self.setBusyFalse()
            return

        run_mode = self._state.runMode
        if run_mode == RunMode.Visualize:
            self.updateScatter(coords_detected)
            if img_ana is not None:
                self.setAnalysisHelpImg(img_ana, self._exinfo)
        elif run_mode == RunMode.Validate:
            self._handle_validate_frame(coords_detected, img, img_ana)
        elif coords_detected.size != 0:
            self._handle_event_frame(coords_detected, img)
            return  # event branch handles its own _prevFrames append

        self._prevFrames.append(img)
        if run_mode == RunMode.Validate and img_ana is not None:
            self._prevAnaFrames.append(img_ana)
        self._state.frame += 1
        self.setBusyFalse()

    def _handle_validate_frame(self, coords_detected, img, img_ana) -> None:
        self.updateScatter(coords_detected)
        if img_ana is not None:
            self.setAnalysisHelpImg(img_ana)

        if self._state.validating:
            if self._state.validationFrames > self.VALIDATION_FRAMES_LIMIT:
                try:
                    self.saveValidationImages(prev=True, prev_ana=True)
                    self.pauseFastModality()
                    self.endRecording()
                except Exception as error:
                    self._logger.error(
                        'Validation terminal bookkeeping failed: %s',
                        error,
                        exc_info=True,
                    )
                    try:
                        self._set_status('error', str(error))
                    except Exception:
                        self._logger.error(
                            'Failed to surface validation terminal error',
                            exc_info=True,
                        )
                finally:
                    self._state.frame = 0
                    self._state.validating = False
                    self._continueFastModalitySafely(
                        'validation terminal recovery'
                    )
            self._state.validationFrames += 1
            return

        if coords_detected.size == 0:
            return

        coords_wf = self._first_coord(coords_detected)
        self.setDetLogLine('fastscan_x_center', coords_wf[0])
        self.setDetLogLine('fastscan_y_center', coords_wf[1])
        self._log_all_detected_coords(coords_detected)
        self._state.validating = True
        self._state.validationFrames = 0

    def _handle_event_frame(self, coords_detected, img) -> None:
        # sigUpdateImage is produced by the detector poll worker. A delivery
        # already queued before pause/Stop may still invoke this slot after the
        # signal is disconnected. Reject it before changing modes, moving a
        # positioner, replacing prepared scan data, or reserving scan ownership.
        if (
            self.__dict__.get('_closed', False)
            or self.__dict__.get('_stopRequested', False)
            or not self.__dict__.get('_experimentActive', False)
            or not self._state.running
        ):
            self.setBusyFalse()
            return

        coords_wf = np.copy(self._first_coord(coords_detected))
        self.setDetLogLine('prepause', _now_us_tag())
        self.setDetLogLine('fastscan_x_center', coords_wf[0])
        self.setDetLogLine('fastscan_y_center', coords_wf[1])
        self._set_status('triggered')
        self.pauseFastModality()

        try:
            # Claim direct-scan ownership before preparation can move static
            # positioners or overwrite signalDic/scanInfoDict. The normal
            # queued-duplicate path is rejected above, while this remains the
            # final cross-owner guard.
            if not self._beginTriggeredScanRun():
                self.setBusyFalse()
                return

            self.setDetLogLine('coord_transf_start', _now_us_tag())
            coords_scan = self._transformService.apply(
                coords_wf, self._transform_apply_extra_arg()
            )
            self.setDetLogLine('slowscan_x_center', coords_scan[0])
            self.setDetLogLine('slowscan_y_center', coords_scan[1])
            self.setDetLogLine('scan_initiate', _now_us_tag())
            self._log_all_detected_coords(coords_detected, override=coords_wf)

            slow_scan_ready = self.initiateSlowScan(position=coords_scan)
            if not slow_scan_ready:
                raise RuntimeError(
                    'Failed to initiate slow scan; scan was not started.'
                )

            self._set_status('scanning')
            if not self.runSlowScan():
                raise RuntimeError('Failed to trigger slow scan.')
            if (
                self._state.scanInitiationMode
                == ScanInitiationMode.RecordingWidget
                and not self._captureTriggeredRecordingOperation()
            ):
                raise RuntimeError(
                    'Recording controller did not accept the triggered scan.'
                )
        except Exception as e:
            self._logger.error(
                f'Failed to trigger {self.MODALITY_LABEL} slow scan: {e}',
                exc_info=True,
            )
            try:
                self._set_status('error', str(e))
            except Exception:
                self._logger.error(
                    'Failed to surface slow-scan trigger error',
                    exc_info=True,
                )
            try:
                self._finishTriggeredScanRun()
            except Exception:
                self._logger.error(
                    'Failed to terminalize rejected triggered scan',
                    exc_info=True,
                )
            finally:
                self._state.busy = False
                self._state.frame = 0
                self._continueFastModalitySafely(
                    'triggered scan arm-failure recovery'
                )
            return

        try:
            self.updateScatter(coords_detected)
            self._prevFrames.append(img)
            self.saveValidationImages(prev=True, prev_ana=False)
            self._exinfo = None
        except Exception:
            # The scan is already armed; bookkeeping failure must not publish
            # an early end or resume the fast modality over active NI-DAQ.
            self._logger.error(
                'Triggered-scan post-arm bookkeeping failed.',
                exc_info=True,
            )
        self._state.busy = False

    @staticmethod
    def _first_coord(coords_detected: np.ndarray) -> np.ndarray:
        """Pick the first coordinate as a 1-D (2,) array regardless of shape."""
        return coords_detected[0, :] if np.size(coords_detected) > 2 else coords_detected[0]

    def _log_all_detected_coords(self, coords_detected, override=None) -> None:
        if np.size(coords_detected) <= 2:
            return
        n = np.size(coords_detected, 0)
        for i in range(n):
            x = override[0] if override is not None else coords_detected[i, 0]
            y = override[1] if override is not None else coords_detected[i, 1]
            self.setDetLogLine('det_coord_x_', x, i)
            self.setDetLogLine('det_coord_y_', y, i)

    # ── Slow scan trigger ───────────────────────────────────────────────── #

    def initiateSlowScan(self, position=None) -> bool:
        # The event beam-path mode is applied here — after pauseFastModality has
        # disabled the fast laser (in _handle_event_frame) and before the scan is
        # prepared/triggered. A failure blocks the scan and recovers to idle;
        # _handle_event_frame's bool-driven failure path then resumes/stops.
        if self._smartModeSwitchingEnabled() and not self._applySmartModeRole(
            'event', required=True
        ):
            self._applySmartModeRoleIfConfigured('idle')
            return False

        result = self._triggeredScanRunner.prepare(
            position,
            self._state.scanInitiationMode.name,
            self._analogParameterDict,
            self._digitalParameterDict,
            self._positionersScan,
            scan_manager=self._master.scanManager,
            comm_channel=self._commChannel,
            positioners_manager=self._master.positionersManager,
            apply_fast_axis_shift=self._widget.fastaxisshiftCheck.isChecked(),
            fast_axis_shift_fn=self.addFastAxisShift,
        )
        if result.success:
            self.signalDic = result.signal_dict
            self.scanInfoDict = result.scan_info_dict
        else:
            self._logger.error(result.message)
            self._set_status('error', result.message)
        return result.success

    def runSlowScan(self) -> bool:
        self._state.detLog['scan_start'] = _now_us_tag()
        # Publish scan membership while the DAQ is still free — see
        # SuperScanController._armScanIteration. This is the fifth NI-DAQ entry
        # point and needs the same window, or lasers here are armed by a
        # sigScanBuilt handler whose one-shot writes are already refused.
        try:
            devices = self._master.nidaqManager.resolveScanTTLDevices(self.signalDic)
        except Exception as e:
            self._logger.error(
                f'Could not resolve the scan device list for the '
                f'{self.MODALITY_LABEL} slow scan; membership consumers are '
                f'not being notified: {e}', exc_info=True
            )
        else:
            self._commChannel.sigScanDevicesResolved.emit(devices)
        result = self._triggeredScanRunner.trigger(
            self._state.scanInitiationMode.name,
            nidaq_manager=self._master.nidaqManager,
            signal_dict=self.signalDic,
            scan_info_dict=self.scanInfoDict,
            comm_channel=self._commChannel,
            scan_coordinator=self._scanCoordinator,
            scan_owner=self,
        )
        if not result.success:
            self._logger.error(result.message)
        return result.success

    def setCenterScanParameter(self, position) -> None:
        self._triggeredScanRunner.set_center_scan_parameter(
            self._analogParameterDict,
            self._positionersScan,
            position,
            self._master.positionersManager,
            self._widget.fastaxisshiftCheck.isChecked(),
            self.addFastAxisShift,
        )

    def setCentersScanWidget(self) -> None:
        self._triggeredScanRunner.set_centers_scan_widget(
            self._analogParameterDict, self._commChannel
        )

    def addFastAxisShift(self, center: float) -> float:
        """Second-degree fit-based fast-axis shift compensation.

        Subclasses can override ``FAST_AXIS_SHIFT_COEFFICIENTS`` with the six
        fit coefficients for their setup.
        """
        dwell_time = float(self._analogParameterDict['sequence_time'])
        px_size = float(self._analogParameterDict['axis_step_size'][0])
        coefficients = np.asarray(self.FAST_AXIS_SHIFT_COEFFICIENTS, dtype=float)
        if coefficients.shape != (6,):
            raise ValueError(
                "FAST_AXIS_SHIFT_COEFFICIENTS must contain exactly 6 values"
            )
        params = np.array([px_size**2, dwell_time**2, px_size*dwell_time,
                           px_size, dwell_time, 1])
        return center - float(np.sum(params * coefficients))

    # ── Scan completion ──────────────────────────────────────────────────── #

    def _isDirectTriggeredScan(self) -> bool:
        return (
            self._state.scanInitiationMode
            == ScanInitiationMode.ScanWidget
        )

    def _beginTriggeredScanRun(self) -> bool:
        """Reserve/publish one direct slow-scan run.

        RecordingWidget intentionally does nothing here: RecordingController
        already publishes the scan lifecycle and owns its terminal signal.
        """
        if not self._isDirectTriggeredScan():
            if self.__dict__.get('_triggeredRecordingInFlight', False):
                self._logger.warning(
                    'Ignoring duplicate event-triggered recording start.'
                )
                return False
            getActiveSource = getattr(
                self.__dict__.get('_commChannel'),
                'getActiveScanSource',
                None,
            )
            if callable(getActiveSource):
                try:
                    if getActiveSource() is not None:
                        self._logger.warning(
                            'Cannot start a triggered recording while another '
                            'scan source is active.'
                        )
                        return False
                except Exception:
                    self._logger.error(
                        'Could not verify the active scan source.',
                        exc_info=True,
                    )
                    return False
            scanSource = None
            resolveSource = getattr(
                self.__dict__.get('_commChannel'),
                'getRecordingScanSource',
                None,
            )
            if callable(resolveSource):
                try:
                    scanSource = resolveSource()
                except Exception as error:
                    self._logger.error(
                        'Could not resolve the RecordingWidget scan source: %s',
                        error,
                        exc_info=True,
                    )
                    return False
            coordinator = getattr(
                self.__dict__.get('_master'),
                'scanExecutionCoordinator',
                None,
            )
            if coordinator is None and scanSource is not None:
                coordinator = getattr(
                    scanSource, '_scanCoordinator', None
                )
            if coordinator is not None:
                try:
                    if getattr(
                        coordinator, 'activeRunToken', None
                    ) is not None:
                        self._logger.warning(
                            'Cannot start a triggered recording while another '
                            'scan run is still reserved.'
                        )
                        return False
                except Exception:
                    self._logger.error(
                        'Could not verify the scan-run reservation.',
                        exc_info=True,
                    )
                    return False
            self._triggeredRecordingInFlight = True
            recordingManager = getattr(
                self.__dict__.get('_master'),
                'recordingManager',
                None,
            )
            self._recordingGenerationBeforeTrigger = getattr(
                recordingManager, 'recordingGeneration', None
            )
            self._triggeredRecordingGeneration = None
            self._triggeredRecordingManagerTerminal = False
            self._triggeredRecordingLifecycleEnded = False
            self._triggeredRecordingFailureMessage = None
            self._triggeredRecordingCaptured = False
            self._triggeredRecordingScanSource = scanSource
            self._triggeredRecordingRunToken = None
            self._triggeredRecordingSourceObservedRunning = False
            return True
        localToken = self.__dict__.get('_triggeredScanRunToken')
        activeRun = self._scanCoordinator.runForOwner(self)
        if (
            localToken is not None
            and activeRun is localToken
        ):
            self._logger.warning(
                'Ignoring duplicate event-triggered slow-scan start.'
            )
            return False
        token = self._scanCoordinator.reserveRun(self)
        self._triggeredScanRunToken = token
        if not self.__dict__.get(
            '_triggeredScanStartingPublished', False
        ):
            # Set before emitting so even a signal-slot exception is paired.
            self._triggeredScanStartingPublished = True
            self._commChannel.scanWorkflow.notify_scan_starting()
        return True

    def _finishTriggeredScanRun(self) -> bool:
        """End/release the direct slow-scan run exactly once.

        The run-level end notification belongs *after* the iteration finish
        barrier and SCAN-lease release. ``releaseRun(onReleased=...)`` provides
        that ordering even when this terminal path races an asynchronous
        detector acknowledgement.
        """
        if not self._isDirectTriggeredScan():
            hadTriggeredRecording = bool(
                self.__dict__.get('_triggeredRecordingInFlight', False)
            )
            self._triggeredRecordingInFlight = False
            self._triggeredRecordingGeneration = None
            self._recordingGenerationBeforeTrigger = None
            self._triggeredRecordingManagerTerminal = False
            self._triggeredRecordingLifecycleEnded = False
            self._triggeredRecordingFailureMessage = None
            self._triggeredRecordingCaptured = False
            self._triggeredRecordingScanSource = None
            self._triggeredRecordingRunToken = None
            self._triggeredRecordingSourceObservedRunning = False
            return hadTriggeredRecording

        token = self.__dict__.get('_triggeredScanRunToken')
        startingPublished = bool(
            self.__dict__.get('_triggeredScanStartingPublished', False)
        )
        if token is None and not startingPublished:
            self._disconnectNidaqCompletionSignalsIfIdle()
            return False

        self._triggeredScanRunToken = None
        self._triggeredScanStartingPublished = False
        self._triggeredScanCompletionPublishing = True
        terminalLock = threading.Lock()
        terminalPublished = False

        def publishTerminal():
            nonlocal terminalPublished
            with terminalLock:
                if terminalPublished:
                    return
                terminalPublished = True
            try:
                if startingPublished:
                    self._commChannel.scanWorkflow.notify_scan_ended()
            finally:
                finalizeError = None
                try:
                    finalizeRunRelease = getattr(
                        self._scanCoordinator,
                        'finalizeRunRelease',
                        None,
                    )
                    if callable(finalizeRunRelease) and token is not None:
                        finalized = finalizeRunRelease(token)
                        if not finalized and not releaseProven():
                            raise RuntimeError(
                                'The held triggered scan run was not finalized.'
                            )
                except Exception as error:
                    finalizeError = error
                    self._logger.error(
                        'Failed to finalize the triggered scan-run release',
                        exc_info=True,
                    )
                    if not releaseProven():
                        # The global end was already attempted. Retain only the
                        # exact token so a later stop/close can retry release
                        # without publishing a duplicate lifecycle terminal.
                        if self.__dict__.get(
                            '_triggeredScanRunToken'
                        ) is None:
                            self._triggeredScanRunToken = token
                finally:
                    self._triggeredScanCompletionPublishing = False
                    self._disconnectNidaqCompletionSignalsIfIdle()

        def publishTerminalOnControllerThread():
            invoke = getattr(
                self, '_invokeOnControllerThreadIfNeeded', None
            )
            if callable(invoke):
                try:
                    invoke(publishTerminal)
                    return
                except Exception:
                    # The held coordinator reservation is more important than
                    # preserving affinity after the handoff mechanism itself
                    # has failed. publishTerminal is idempotent if a custom
                    # handoff queued the callback before raising.
                    self._logger.error(
                        'Failed to hand off triggered-scan terminal '
                        'publication; using the fail-safe path',
                        exc_info=True,
                    )
            publishTerminal()

        def releaseProven():
            if bool(getattr(token, 'released', False)):
                return True
            try:
                return self._scanCoordinator.activeRunToken is not token
            except Exception:
                return False

        releaseAccepted = False
        if token is not None:
            try:
                releaseKwargs = {
                    'onReleased': publishTerminalOnControllerThread
                }
                if callable(getattr(
                    self._scanCoordinator, 'finalizeRunRelease', None
                )):
                    releaseKwargs['holdUntilFinalized'] = True
                releaseAccepted = self._scanCoordinator.releaseRun(
                    token, **releaseKwargs
                )
            except Exception:
                self._logger.error(
                    'Failed to release triggered scan-run reservation',
                    exc_info=True,
                )
                if not releaseProven():
                    self._triggeredScanRunToken = token
                    self._triggeredScanStartingPublished = startingPublished
                    self._triggeredScanCompletionPublishing = False
                    return False
        # No token means only the published start flag remained. A rejected
        # release means the token was already stale/released. Neither case has
        # a live barrier that could legitimately delay terminal notification.
        if token is None:
            publishTerminalOnControllerThread()
        elif not releaseAccepted:
            if not releaseProven():
                self._triggeredScanRunToken = token
                self._triggeredScanStartingPublished = startingPublished
                self._triggeredScanCompletionPublishing = False
                return False
            publishTerminalOnControllerThread()
        return True

    def _finishTriggeredScanRunIfIdle(self) -> bool:
        coordinator = self.__dict__.get('_scanCoordinator')
        if coordinator is None:
            return False
        if coordinator.tokenForOwner(self) is not None:
            return False
        return self._finishTriggeredScanRun()

    def _onTriggeredNidaqScanDone(self) -> None:
        token = self._scanCoordinator.tokenForOwner(self)
        if token is None:
            return
        runToken = self.__dict__.get('_triggeredScanRunToken')
        self._scanCoordinator.resolve(
            token,
            FINISH_GRACEFUL,
            onComplete=lambda: self._invokeOnControllerThread(
                lambda: self._deliverTriggeredScanEnded(runToken)
            ),
        )

    def _onTriggeredNidaqScanBuildFailed(self) -> None:
        token = self._scanCoordinator.tokenForOwner(self)
        if token is None:
            return
        runToken = self.__dict__.get('_triggeredScanRunToken')
        self._scanCoordinator.resolve(
            token,
            FINISH_ABORT,
            onComplete=lambda: self._invokeOnControllerThread(
                lambda: self._deliverTriggeredScanBuildFailed(runToken),
            ),
        )

    def _triggeredRunStillCurrent(self, runToken) -> bool:
        return (
            runToken is not None
            and self.__dict__.get('_triggeredScanRunToken') is runToken
            and self._scanCoordinator.runForOwner(self) is runToken
        )

    def _deliverTriggeredScanEnded(self, runToken) -> None:
        if self._triggeredRunStillCurrent(runToken):
            self.scanEnded()

    def _deliverTriggeredScanBuildFailed(self, runToken) -> None:
        if self._triggeredRunStillCurrent(runToken):
            self._afterTriggeredScanBuildFailed()

    def _afterTriggeredScanBuildFailed(self) -> None:
        self._logger.error('Triggered slow scan could not be built.')
        try:
            self._set_status(
                'error', 'Triggered slow scan could not be built.'
            )
        except Exception:
            self._logger.error(
                'Failed to surface triggered-scan build failure',
                exc_info=True,
            )
        try:
            self._finishTriggeredScanRun()
        except Exception as error:
            self._logger.error(
                'Failed to publish triggered-scan build failure: %s',
                error,
                exc_info=True,
            )
        finally:
            self._state.busy = False
            self._state.frame = 0
            self._continueFastModalitySafely(
                'triggered scan build-failure recovery'
            )

    def scanEnded(self) -> None:
        if (
            self._state.scanInitiationMode
            == ScanInitiationMode.RecordingWidget
        ):
            if not self.__dict__.get(
                '_triggeredRecordingInFlight', False
            ):
                return
            self._triggeredRecordingInFlight = False
            self._triggeredRecordingGeneration = None
            self._recordingGenerationBeforeTrigger = None
            self._triggeredRecordingManagerTerminal = False
            self._triggeredRecordingLifecycleEnded = False
            self._triggeredRecordingFailureMessage = None
            self._triggeredRecordingCaptured = False
            self._triggeredRecordingScanSource = None
            self._triggeredRecordingRunToken = None
            self._triggeredRecordingSourceObservedRunning = False
        try:
            # Pair/release lifecycle before any optional snapshot or log work.
            # A filesystem/UI bookkeeping exception must not retain ownership.
            self._finishTriggeredScanRun()
            self.setDetLogLine('scan_end', _now_us_tag())
            if (self.__dict__.get('_closed', False)
                    or self.__dict__.get('_stopRequested', False)
                    or not self.__dict__.get('_experimentActive', True)):
                return
            if self._state.scanInitiationMode == ScanInitiationMode.ScanWidget:
                self._commChannel.sigSnapImg.emit()
                try:
                    samples = self.scanInfoDict['scan_samples_total']
                    sample_rate = float(self.scanInfoDict.get(
                        'sample_rate', _DEFAULT_SAMPLE_RATE_HZ
                    ))
                    self.setDetLogLine(
                        'total_scan_time', samples / sample_rate
                    )
                except KeyError:
                    self._logger.info(
                        "Scan 'total_scan_time' not saved in log as "
                        "'scan_samples_total' not available in scanInfoDict "
                        "using current signal designer."
                    )
            self.endRecording()
        except Exception as error:
            # Snapshot delivery, log I/O and lifecycle notification are
            # bookkeeping. Once hardware completion is authoritative, none of
            # them may leave the fast modality paused with an active session.
            self._logger.error(
                'Triggered-scan terminal bookkeeping failed: %s',
                error,
                exc_info=True,
            )
            try:
                self._set_status('error', str(error))
            except Exception:
                self._logger.error(
                    'Failed to surface triggered-scan terminal error',
                    exc_info=True,
                )
        finally:
            self._state.busy = False
            self._state.frame = 0
            self._continueFastModalitySafely(
                'triggered scan terminal recovery'
            )

    def _onTriggeredRecordingFailed(self, message: str) -> None:
        """Recover one RecordingWidget-triggered slow-scan failure.

        Recording failures are broadcast. Only the operation explicitly armed
        by this controller is authoritative; queued failures after Stop/close
        are allowed to clean up, but never to reconnect the scouting modality.
        """
        if (
            self._state.scanInitiationMode
            != ScanInitiationMode.RecordingWidget
            or not self.__dict__.get(
                '_triggeredRecordingInFlight', False
            )
        ):
            return
        self._triggeredRecordingInFlight = False
        self._triggeredRecordingGeneration = None
        self._recordingGenerationBeforeTrigger = None
        self._triggeredRecordingManagerTerminal = False
        self._triggeredRecordingLifecycleEnded = False
        self._triggeredRecordingFailureMessage = None
        self._triggeredRecordingCaptured = False
        self._triggeredRecordingScanSource = None
        self._triggeredRecordingRunToken = None
        self._triggeredRecordingSourceObservedRunning = False
        self._logger.error(
            'Triggered slow-scan recording failed: %s', message
        )
        try:
            self._set_status('error', str(message))
        except Exception:
            self._logger.error(
                'Failed to surface triggered recording failure',
                exc_info=True,
            )
        self._state.busy = False
        self._state.frame = 0
        self._continueFastModalitySafely(
            'triggered recording failure recovery'
        )

    def _captureTriggeredRecordingOperation(self) -> bool:
        """Pin the exact RecordingManager generation started by this event."""
        recordingManager = getattr(
            self.__dict__.get('_master'),
            'recordingManager',
            None,
        )
        if recordingManager is None:
            # Legacy test/third-party wiring has no manager identity surface.
            return True
        generation = getattr(
            recordingManager, 'recordingGeneration', None
        )
        before = self.__dict__.get(
            '_recordingGenerationBeforeTrigger'
        )
        if isinstance(generation, int) and isinstance(before, int):
            # RecordingManager serializes sessions and advances this identity
            # exactly once per accepted start. Skipping generations means an
            # unrelated operation ran in this trigger's identity window.
            if generation != before + 1:
                return False
            self._triggeredRecordingGeneration = generation
            self._triggeredRecordingCaptured = True

            source = self.__dict__.get('_triggeredRecordingScanSource')
            if source is not None:
                try:
                    sourceRunning = bool(
                        getattr(source, 'isRunning', False)
                    )
                except Exception:
                    return False
                if sourceRunning:
                    self._triggeredRecordingSourceObservedRunning = True

                coordinator = getattr(
                    self.__dict__.get('_master'),
                    'scanExecutionCoordinator',
                    None,
                )
                if coordinator is None:
                    coordinator = getattr(source, '_scanCoordinator', None)
                if coordinator is not None:
                    try:
                        activeRun = getattr(
                            coordinator, 'activeRunToken', None
                        )
                    except Exception:
                        return False
                    if (
                        activeRun is not None
                        and getattr(activeRun, 'owner', None) is source
                    ):
                        self._triggeredRecordingRunToken = activeRun
                        self._triggeredRecordingSourceObservedRunning = True

                if (
                    not sourceRunning
                    and not self.__dict__.get(
                        '_triggeredRecordingLifecycleEnded', False
                    )
                ):
                    # The manager armed, but the selected scan source did not
                    # retain ownership and no exact synchronous terminal was
                    # observed. Treat this as a rejected scan, not a recording
                    # operation whose next global end may be adopted.
                    return False
            if (
                not bool(getattr(recordingManager, 'record', False))
                and not self.__dict__.get(
                    '_triggeredRecordingManagerTerminal', False
                )
            ):
                return False
            self._finishTriggeredRecordingIfReady()
            return True
        # Compatibility for managers without generation-tagged signals.
        return bool(getattr(recordingManager, 'record', True))

    def _onTriggeredRecordingEndedDetailed(self, generation: int) -> None:
        if not self._acceptTriggeredRecordingGeneration(generation):
            return
        self._triggeredRecordingManagerTerminal = True
        self._finishTriggeredRecordingIfReady()

    def _onTriggeredRecordingFailedDetailed(
        self, message: str, generation: int
    ) -> None:
        if not self._acceptTriggeredRecordingGeneration(generation):
            return
        self._triggeredRecordingManagerTerminal = True
        self._triggeredRecordingFailureMessage = str(message)
        self._finishTriggeredRecordingIfReady()

    def _acceptTriggeredRecordingGeneration(self, generation: int) -> bool:
        if not self.__dict__.get(
            '_triggeredRecordingInFlight', False
        ):
            return False
        expected = self.__dict__.get('_triggeredRecordingGeneration')
        if expected is not None:
            return expected == generation
        before = self.__dict__.get('_recordingGenerationBeforeTrigger')
        if isinstance(before, int) and generation == before + 1:
            # The manager can finish on a worker thread before the synchronous
            # external-recording call returns. Pin that exact new identity now;
            # actual recovery still waits until capture marks the dispatch
            # complete and the scan-run lifecycle has ended.
            self._triggeredRecordingGeneration = generation
            return True
        return False

    def _onTriggeredRecordingLifecycleEnded(self) -> None:
        if (
            self._state.scanInitiationMode
            != ScanInitiationMode.RecordingWidget
            or not self.__dict__.get(
                '_triggeredRecordingInFlight', False
            )
        ):
            return
        commChannel = self.__dict__.get('_commChannel')
        getActiveSource = getattr(
            commChannel, 'getActiveScanSource', None
        )
        activeSource = None
        if callable(getActiveSource):
            try:
                activeSource = getActiveSource()
            except Exception:
                # Unreadable global source state is not attribution.
                return
            if activeSource is not None:
                # A queued/global end from another run cannot terminalize the
                # recording-triggered slow scan while any exact source is
                # active.
                return

        expectedSource = self.__dict__.get(
            '_triggeredRecordingScanSource'
        )
        expectedRun = self.__dict__.get(
            '_triggeredRecordingRunToken'
        )
        coordinator = getattr(
            self.__dict__.get('_master'),
            'scanExecutionCoordinator',
            None,
        )
        if coordinator is None and expectedSource is not None:
            coordinator = getattr(
                expectedSource, '_scanCoordinator', None
            )
        activeRun = None
        if coordinator is not None:
            try:
                activeRun = getattr(
                    coordinator, 'activeRunToken', None
                )
                heldForEndPublication = (
                    activeRun is not None
                    and bool(
                        getattr(activeRun, 'releaseRequested', False)
                    )
                    and bool(
                        getattr(activeRun, 'releaseBarrierCleared', False)
                    )
                    and bool(
                        getattr(
                            activeRun,
                            'holdReleaseUntilFinalized',
                            False,
                        )
                    )
                )
            except Exception:
                # Unknown coordinator state is not proof that this global
                # lifecycle event belongs to the triggered recording.
                return
            if activeRun is not None:
                if not heldForEndPublication:
                    return
                if expectedRun is not None:
                    if activeRun is not expectedRun:
                        return
                elif expectedSource is not None:
                    try:
                        if getattr(activeRun, 'owner', None) is not expectedSource:
                            return
                    except Exception:
                        return
                    # The exact run may finish synchronously inside the
                    # external-recording dispatch, before the post-dispatch
                    # capture runs. Pin it while the coordinator deliberately
                    # holds it across this end publication.
                    self._triggeredRecordingRunToken = activeRun
                    self._triggeredRecordingSourceObservedRunning = True
                elif not self.__dict__.get(
                    '_triggeredRecordingCaptured', False
                ):
                    # With neither source nor completed manager dispatch, an
                    # arbitrary held run is still not this operation.
                    return

        if expectedSource is not None:
            try:
                sourceStillRunning = bool(
                    getattr(expectedSource, 'isRunning', False)
                )
            except Exception:
                return
            if sourceStillRunning:
                self._triggeredRecordingSourceObservedRunning = True
                return
            if not self.__dict__.get(
                '_triggeredRecordingSourceObservedRunning', False
            ):
                # Reject a stale global end delivered in the pre-arm window,
                # before this selected source has ever owned the scan.
                return
        elif (
            activeRun is None
            and not self.__dict__.get(
                '_triggeredRecordingCaptured', False
            )
        ):
            # Legacy wiring without source identity cannot safely cache a
            # pre-dispatch global terminal for adoption after capture.
            return
        self._triggeredRecordingLifecycleEnded = True
        self._finishTriggeredRecordingIfReady()

    def _finishTriggeredRecordingIfReady(self) -> None:
        if (
            not self.__dict__.get(
                '_triggeredRecordingInFlight', False
            )
            or not self.__dict__.get(
                '_triggeredRecordingCaptured', False
            )
            or not self.__dict__.get(
                '_triggeredRecordingManagerTerminal', False
            )
            or not self.__dict__.get(
                '_triggeredRecordingLifecycleEnded', False
            )
        ):
            return
        failure = self.__dict__.get(
            '_triggeredRecordingFailureMessage'
        )
        if failure:
            self._onTriggeredRecordingFailed(failure)
        else:
            self.scanEnded()

    def _continueFastModalitySafely(self, context: str) -> None:
        """Resume after terminal work, or force a safe stopped state.

        ``continueFastModality`` already handles expected resume failures. This
        outer boundary covers failures before that internal try-block (for
        example a deleted widget during shutdown), so a queued completion can
        never leave a laser/stream/session half-active.
        """
        try:
            self.continueFastModality()
            return
        except Exception as error:
            self._logger.error(
                'Failed during %s: %s', context, error, exc_info=True
            )

        try:
            self.stopExperiment(resetParams=False)
        except Exception:
            self._logger.error(
                'Failed to stop after %s; forcing safe terminal state',
                context,
                exc_info=True,
            )
            self._stopRequested = True
            self._experimentActive = False
            self._state.running = False
            self._state.busy = False
            try:
                self._disconnectRunSignals()
            except Exception:
                self._logger.error(
                    'Failed to disconnect event-triggered run signals',
                    exc_info=True,
                )
            self._setFastLaserEnabled(False)
            self._disconnectNidaqCompletionSignalsIfIdle()
        try:
            self._set_status(
                'error', f'Failed during {context}; experiment stopped.'
            )
        except Exception:
            self._logger.error(
                'Failed to surface terminal recovery status',
                exc_info=True,
            )

    def endRecording(self) -> None:
        self.setDetLogLine('pipeline', self.getPipelineName())
        self.logPipelineParamVals()
        filename = _now_utc_fileus() + '_log'
        os.makedirs(self._logsDir, exist_ok=True)
        path = os.path.join(self._logsDir, filename + '.txt')
        with open(path, 'w', encoding='utf-8') as f:
            for key in self._state.detLog:
                f.write(f'{key}: {self._state.detLog[key]}\n')
        self.resetDetLog()

    def logPipelineParamVals(self) -> None:
        for key, val in zip(self._pipelineRunner.get_user_parameter_names(), self._param_vals):
            self.setDetLogLine(key, val)

    def continueFastModality(self) -> None:
        if (self.__dict__.get('_closed', False)
                or self.__dict__.get('_stopRequested', False)
                or not self.__dict__.get('_experimentActive', True)):
            # A queued scan completion may arrive after Stop/close. It is still
            # authoritative for resolving the iteration, but it must never
            # reconnect frame signals, switch modes or re-enable a laser.
            self._state.running = False
            self._state.busy = False
            self._setFastLaserEnabled(False)
            self._disconnectNidaqCompletionSignalsIfIdle()
            return
        if self._widget.endlessScanCheck.isChecked() and not self._state.running:
            try:
                self._on_resume_modality_hook()
                # Reapply the scouting beam path before re-enabling the fast laser,
                # preferring an explicit "resume" mode when configured.
                if self._smartModeSwitchingEnabled() and not self._applySmartModeRole(
                    self._resumeSmartModeRole(), required=True
                ):
                    raise RuntimeError(
                        f'Failed to apply resume smart microscopy mode for '
                        f'{self.MODALITY_LABEL}.'
                    )
                self._connectRunSignals()
                self._setFastLaserEnabled(True, require_success=True)
            except Exception as e:
                self._logger.error(
                    f'Failed to resume {self.MODALITY_LABEL} fast modality: {e}',
                    exc_info=True,
                )
                self._disconnectRunSignals()
                self._setFastLaserEnabled(False)
                self._state.running = False
                self._state.busy = False
                self._set_status('error', str(e))
                return
            else:
                self._widget.initiateButton.setText('Stop')
                self._set_controls_armed(True)
                self._set_status('detecting')
                self._state.running = True
        elif not self._widget.endlessScanCheck.isChecked():
            self.stopExperiment(resetParams=True)

    # ── Misc state mgmt ─────────────────────────────────────────────────── #

    def setDetLogLine(self, key: str, val, *args) -> None:
        if args:
            self._state.detLog[f'{key}{args[0]}'] = val
        else:
            self._state.detLog[key] = val

    def logScanFreq(self, scanFreq) -> None:
        self.setDetLogLine('scan_period', scanFreq)

    def resetDetLog(self) -> None:
        self._state.detLog = {
            'pipeline': '',
            'pipeline_start': '',
            'pipeline_end': '',
            'coord_transf_start': '',
            'fastscan_x_center': 0,
            'fastscan_y_center': 0,
            'slowscan_x_center': 0,
            'slowscan_y_center': 0,
        }

    def resetParamVals(self) -> None:
        self._param_vals = []

    def resetRunParams(self) -> None:
        self._state.reset_runtime_counters()

    def updateScatter(self, coords) -> None:
        if np.size(coords) <= 0:
            return
        self._widget.setEventScatterData(x=coords[:, 1], y=coords[:, 0])
        self._commChannel.sigAddItemToVb.emit(self._widget.getEventScatterPlot())

    def saveValidationImages(self, prev: bool = True, prev_ana: bool = True) -> None:
        if prev:
            img = np.array(list(self._prevFrames))
            self._commChannel.sigSnapImgPrev.emit(self._state.detectorFast, img, 'raw')
            self._prevFrames.clear()
        if prev_ana:
            img = np.array(list(self._prevAnaFrames))
            self._commChannel.sigSnapImgPrev.emit(self._state.detectorFast, img, 'ana')
            self._prevAnaFrames.clear()

    def pauseFastModality(self) -> None:
        if not self._state.running:
            return
        self._safeDisconnect(self._commChannel.sigUpdateImage, self.runPipeline)
        self._state.imageSignalConnected = False
        self._setFastLaserEnabled(False)
        self._state.running = False
        self._on_pause_modality_hook()

    def launchHelpWidget(self) -> None:
        self._widget.launchHelpWidget(self._widget.analysisHelpWidget, init=True)

    def getFlipWf(self) -> bool:
        return self.FLIP_WF_CALIB


# ─────────────────────────────────────────────────────────────────────────────
# Unified coordinate-transform helper
# ─────────────────────────────────────────────────────────────────────────────


class EventTriggeredCoordTransformHelper:
    """Polynomial coordinate-transform calibration shared by both modalities.

    Manually-clicked bead pairs in low- and high-resolution images are fed
    to :class:`EtSTEDTransformService.calibrate`, which fits a third-order
    polynomial; coefficients are persisted both as a metadata JSON (with
    timestamps and source points) and as the CSV consumed by
    :meth:`EventTriggeredControllerBase.loadTransform`.
    """

    def __init__(self, parentController, coordTransformWidget, saveFolder: str):
        self.__logger = initLogger(self)
        self._parent = parentController
        self._widget = coordTransformWidget
        self.__saveFolder = saveFolder
        self.__transformService = EtSTEDTransformService()

        self.__transformCoeffs = np.zeros(20)
        self.__loResCoords: list[tuple[float, float]] = []
        self.__hiResCoords: list[tuple[float, float]] = []
        self.__loResCoordsPx = np.empty((0, 2))
        self.__hiResCoordsPx = np.empty((0, 2))
        self.__loResImage: np.ndarray | None = None
        self.__hiResImage: np.ndarray | None = None
        self.__hiResPxSize = 1.0
        self.__hiResSize = 1.0
        self.__loResSize = 1.0
        self.__loResPxSize = 1.0
        self.__autoCalibWorker: '_AutoCalibrationWorker | None' = None

        # Wire UI signals — both widget variants expose the same controls.
        self._parent._widget.coordTransfCalibButton.clicked.connect(self.calibrationLaunch)
        self._widget.saveCalibButton.clicked.connect(self.calibrationFinish)
        self._widget.resetCoordsButton.clicked.connect(self.resetCalibrationCoords)
        self._widget.loadLoResButton.clicked.connect(lambda: self.loadCalibImage('lo'))
        self._widget.loadHiResButton.clicked.connect(lambda: self.loadCalibImage('hi'))
        if hasattr(self._widget, 'autoCalibButton'):
            self._widget.autoCalibButton.clicked.connect(self.autoCalibrateLaunch)

    # ── Public API ──────────────────────────────────────────────────────── #

    def getTransformCoeffs(self) -> np.ndarray:
        return self.__transformCoeffs

    def calibrationLaunch(self) -> None:
        self._parent._widget.launchHelpWidget(
            self._parent._widget.coordTransformWidget, init=True
        )

    def calibrationFinish(self) -> None:
        """Fit the third-order polynomial transform from clicked points."""
        self.__loResCoordsPx = np.asarray(self._widget.pointsLayerLo.data)
        self.__hiResCoordsPx = np.asarray(self._widget.pointsLayerHi.data)

        self.__loResCoords = [
            (round(float(p[0]), 3), round(float(p[1]), 3))
            for p in self.__loResCoordsPx
        ]
        self.__hiResCoords = [
            (round((self.__loResSize - float(p[1])) * self.__hiResPxSize
                   - self.__hiResSize / 2, 3),
             round((self.__loResSize - float(p[0])) * self.__hiResPxSize
                   - self.__hiResSize / 2, 3))
            for p in self.__hiResCoordsPx
        ]

        self.coordinateTransformCalibrate()

        name_short = _now_utc_short()
        name_long = _now_utc_long()
        os.makedirs(self.__saveFolder, exist_ok=True)
        os.makedirs(self._parent._widget.transformDir, exist_ok=True)
        np.savetxt(
            fname=os.path.join(self.__saveFolder, name_short + '_transformCoeffs.txt'),
            X=self.__transformCoeffs,
        )
        self.__transformService.save_calibration_metadata(
            os.path.join(self.__saveFolder, name_short + '_transformMetadata.json'),
            self.__loResCoords, self.__hiResCoords, self.__transformCoeffs,
        )
        csv_path = os.path.join(self._parent._widget.transformDir, name_long + '.csv')
        with open(csv_path, 'w', newline='') as csvfile:
            writer = csv.writer(
                csvfile, delimiter=' ', quotechar='|', quoting=csv.QUOTE_MINIMAL
            )
            for el in self.__transformCoeffs:
                writer.writerow([str(el)])

        # Show transformed lo-res points on the hi-res image for verification.
        transformed = []
        for pt in self.__loResCoords:
            pos = self.__transformService.poly_thirdorder_transform(
                self.__transformCoeffs, pt
            )
            transformed.append((
                round(self.__loResSize - (pos[1] + self.__hiResSize / 2)
                      / self.__hiResPxSize, 3),
                round(self.__loResSize - (pos[0] + self.__hiResSize / 2)
                      / self.__hiResPxSize, 3),
            ))
        self._widget.pointsLayerTransf.data = np.asarray(transformed)

        self._parent._widget.updateCoordTransformCoeffPar()

    def resetCalibrationCoords(self) -> None:
        self.__loResCoords = []
        self.__hiResCoords = []
        self.__loResCoordsPx = np.empty((0, 2))
        self.__hiResCoordsPx = np.empty((0, 2))
        self._widget.pointsLayerLo.data = []
        self._widget.pointsLayerHi.data = []
        self._widget.pointsLayerTransf.data = []

    def loadCalibImage(self, modality: str) -> None:
        img_filename = self.openFolder()
        if not img_filename:
            return
        with h5py.File(img_filename, 'r') as f:
            img_key = list(f.keys())[0]
            pixelsize = f[img_key].attrs['element_size_um'][1]
            img_data = np.array(f[img_key])
            imgsize = pixelsize * np.size(img_data, 0)

        self.updateCalibImage(img_data, modality)
        if modality == 'hi':
            self.__hiResCoords = []
            self.__hiResPxSize = pixelsize
            self.__hiResSize = imgsize
            self.__hiResImage = np.asarray(img_data)
        elif modality == 'lo':
            self.__loResCoords = []
            self.__loResSize = float(np.shape(img_data)[1])
            self.__loResPxSize = pixelsize
            self.__loResImage = np.asarray(img_data)

    @staticmethod
    def openFolder() -> str:
        filename, _ = QFileDialog.getOpenFileName(
            None,
            'Select calibration image',
            '',
            'HDF5 images (*.h5 *.hdf5);;All files (*)',
        )
        return filename

    def updateCalibImage(self, img_data: np.ndarray, modality: str) -> None:
        if modality == 'hi':
            viewer = self._widget.napariViewerHi
        elif modality == 'lo':
            viewer = self._widget.napariViewerLo
            if self._parent.getFlipWf():
                img_data = np.moveaxis(img_data, 0, 1)
        else:
            return
        viewer.add_image(img_data)
        viewer.layers.unselect_all()
        viewer.layers.move_selected(len(viewer.layers) - 1, 0)

    def coordinateTransformCalibrate(self) -> None:
        xdata = np.asarray(self.__loResCoords, dtype=np.float32)
        ydata = np.asarray(self.__hiResCoords, dtype=np.float32)
        self.__transformCoeffs = self.__transformService.calibrate(xdata, ydata)

    # ── Auto-calibration ────────────────────────────────────────────────── #

    def autoCalibrateLaunch(self) -> None:
        """Kick off auto bead-detect + match + polynomial fit.

        Runs in a ``QThread`` so the napari viewers stay responsive while
        the bead detector and RANSAC churn.  Result is funnelled back to
        :meth:`_onAutoCalibFinished` on the GUI thread.
        """
        if self.__loResImage is None or self.__hiResImage is None:
            self._setAutoCalibStatus(
                'Load both low-res and high-res calibration images first.'
            )
            return
        if self.__autoCalibWorker is not None:
            self._setAutoCalibStatus('Auto-calibration already running…')
            return

        self._setAutoCalibStatus('Detecting beads…')
        self._setAutoCalibButtonEnabled(False)

        worker = _AutoCalibrationWorker(
            lo_image=self.__loResImage,
            hi_image=self.__hiResImage,
            lo_pixel_size_um=self.__loResPxSize,
            hi_pixel_size_um=self.__hiResPxSize,
            lo_size_um=self.__loResSize,
            hi_size_um=self.__hiResSize,
        )
        worker.finished.connect(self._onAutoCalibFinished)
        worker.failed.connect(self._onAutoCalibFailed)
        worker.start()
        self.__autoCalibWorker = worker

    def _onAutoCalibFinished(self, result: AutoCalibrationResult) -> None:
        self.__autoCalibWorker = None
        self._setAutoCalibButtonEnabled(True)

        # Surface the matched centroids in the napari point layers so the
        # user can visually verify before clicking Save.
        if len(result.lo_coords_px):
            self._widget.pointsLayerLo.data = np.asarray(result.lo_coords_px)
        if len(result.hi_coords_px):
            self._widget.pointsLayerHi.data = np.asarray(result.hi_coords_px)

        # Stage the polynomial coefficients so a follow-up "Save calibration"
        # click writes them out the same way as a manual run.
        self.__transformCoeffs = result.coefficients

        # Pre-populate the coords lists in case the user clicks Save next.
        self.__loResCoordsPx = np.asarray(result.lo_coords_px)
        self.__hiResCoordsPx = np.asarray(result.hi_coords_px)
        self.__loResCoords = [
            (round(float(p[0]), 3), round(float(p[1]), 3))
            for p in self.__loResCoordsPx
        ]
        self.__hiResCoords = [
            (round((self.__loResSize - float(p[1])) * self.__hiResPxSize
                   - self.__hiResSize / 2, 3),
             round((self.__loResSize - float(p[0])) * self.__hiResPxSize
                   - self.__hiResSize / 2, 3))
            for p in self.__hiResCoordsPx
        ]

        msg = (f'{result.n_inliers}/{result.n_matched} beads matched, '
               f'RMS={result.rms_residual_px:.3f} px')
        if result.warnings:
            msg += '  ⚠ ' + '; '.join(result.warnings)
        self._setAutoCalibStatus(msg)

    def _onAutoCalibFailed(self, message: str) -> None:
        self.__autoCalibWorker = None
        self._setAutoCalibButtonEnabled(True)
        self._setAutoCalibStatus(f'Failed: {message}')

    def _setAutoCalibStatus(self, text: str) -> None:
        label = getattr(self._widget, 'autoCalibStatusLabel', None)
        if label is not None:
            label.setText(text)
        self.__logger.info(f'Auto-calibration: {text}')

    def _setAutoCalibButtonEnabled(self, enabled: bool) -> None:
        button = getattr(self._widget, 'autoCalibButton', None)
        if button is not None:
            button.setEnabled(enabled)

    # Test-friendly wrappers (the unit tests reach for these names).
    def poly_thirdorder(self, a, x, y):
        return self.__transformService.poly_thirdorder_residuals(a, x, y)

    def poly_thirdorder_transform(self, a, x):
        return self.__transformService.poly_thirdorder_transform(a, x)


class _AutoCalibrationWorker(QtCore.QThread):
    """QThread wrapper around :func:`auto_calibrate`."""

    finished = QtCore.Signal(object)  # AutoCalibrationResult
    failed = QtCore.Signal(str)

    def __init__(
        self,
        *,
        lo_image: np.ndarray,
        hi_image: np.ndarray,
        lo_pixel_size_um: float,
        hi_pixel_size_um: float,
        lo_size_um: float,
        hi_size_um: float,
    ):
        super().__init__()
        self._args = dict(
            lo_image=lo_image,
            hi_image=hi_image,
            lo_pixel_size_um=lo_pixel_size_um,
            hi_pixel_size_um=hi_pixel_size_um,
            lo_size_um=lo_size_um,
            hi_size_um=hi_size_um,
        )

    def run(self) -> None:
        try:
            result = auto_calibrate(**self._args)
        except Exception as exc:  # noqa: BLE001 — re-emit as signal
            tb = traceback.format_exc(limit=2)
            self.failed.emit(f'{type(exc).__name__}: {exc}\n{tb}')
            return
        self.finished.emit(result)
