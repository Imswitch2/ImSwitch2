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
from imswitch.imcontrol.model.EventTriggeredSession import (
    EventRunMode as RunMode,
    EventScanInitiationMode as ScanInitiationMode,
    EventTriggeredSessionState,
)

from ..basecontrollers import ImConWidgetController


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


class EventTriggeredControllerBase(ImConWidgetController):
    """Shared logic for EtSTED / EtMonalisa controllers.

    Subclasses must set the class attributes below and may override
    the ``_*_hook`` template methods.
    """

    # Subclass overrides ────────────────────────────────────────────────────
    LOGS_SUBFOLDER: str = 'logs_event_triggered'
    MODALITY_LABEL: str = 'event-triggered'
    BINARY_FRAMES: int = 10
    INIT_FRAMES: int = 5
    VALIDATION_FRAMES_LIMIT: int = 5
    FLIP_WF_CALIB: bool = True

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
        if self._state.running:
            self.stopExperiment(resetParams=True)
            return

        os.makedirs(self._logsDir, exist_ok=True)
        self._set_status('arming')

        try:
            self._prepareExperiment()
            self._pre_arm_hook()
            self._connectRunSignals()
            self._setFastLaserEnabled(True, require_success=True)

            self._widget.initiateButton.setText('Stop')
            self._set_controls_armed(True)
            self._set_status('detecting')
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
        self._disconnectRunSignals()
        try:
            self._setFastLaserEnabled(False)
        finally:
            self._cleanupBinaryMaskRecording()
            self._widget.initiateButton.setText('Initiate')
            self._set_controls_armed(False)
            if resetParams:
                self.resetParamVals()
            self.resetRunParams()
            if resetParams:
                self._set_status('idle')
            self._post_stop_hook(reset_params=resetParams)

    def closeEvent(self) -> None:
        self.stopExperiment(resetParams=True)
        # Disconnect the comm-channel slots wired in __init__.
        self._safeDisconnect(self._commChannel.sigSendScanParameters, self.assignScanParameters)
        self._safeDisconnect(self._commChannel.sigSendScanFreq, self.logScanFreq)

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
            self._commChannel.sigUpdateImage.connect(self.runPipeline)
            self._state.imageSignalConnected = True
        if not self._state.scanEndSignalConnected:
            if self._state.scanInitiationMode == ScanInitiationMode.ScanWidget:
                self._commChannel.sigToggleBlockScanWidget.emit(False)
                self._commChannel.sigScanEnded.connect(self.scanEnded)
            elif self._state.scanInitiationMode == ScanInitiationMode.RecordingWidget:
                self._commChannel.sigRecordingEnded.connect(self.scanEnded)
            self._state.scanEndSignalConnected = True

    def _disconnectRunSignals(self) -> None:
        if self._state.imageSignalConnected:
            self._safeDisconnect(self._commChannel.sigUpdateImage, self.runPipeline)
            self._state.imageSignalConnected = False
        if self._state.scanEndSignalConnected:
            if self._state.scanInitiationMode == ScanInitiationMode.ScanWidget:
                self._commChannel.sigToggleBlockScanWidget.emit(True)
                self._safeDisconnect(self._commChannel.sigScanEnded, self.scanEnded)
            elif self._state.scanInitiationMode == ScanInitiationMode.RecordingWidget:
                self._safeDisconnect(self._commChannel.sigRecordingEnded, self.scanEnded)
            self._state.scanEndSignalConnected = False

    @staticmethod
    def _safeDisconnect(signal, slot) -> None:
        try:
            signal.disconnect(slot)
        except (TypeError, RuntimeError):
            pass

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
        self._binary_stack_list = []
        laserFastIdx = self._widget.fastImgLasersPar.currentIndex()
        self._state.laserFast = self._widget.fastImgLasers[laserFastIdx]
        self.laserFast = self._state.laserFast
        detectorFastIdx = self._widget.fastImgDetectorsPar.currentIndex()
        self._state.detectorFast = self._widget.fastImgDetectors[detectorFastIdx]
        self.detectorFast = self._state.detectorFast
        self._master.lasersManager.execOn(
            self._state.laserFast, lambda l: l.setEnabled(True)
        )
        self._commChannel.sigUpdateImage.connect(self.addImgBinStack)
        self._state.binaryMaskSignalConnected = True
        self._widget.recordBinaryMaskButton.setText('Recording...')

    def addImgBinStack(self, detectorName, img, init, scale, isCurrentDetector) -> None:
        del init, scale, isCurrentDetector
        if detectorName != self._state.detectorFast:
            return
        if len(self._binary_stack_list) >= self.BINARY_FRAMES:
            return  # late frame after disconnect
        self._binary_stack_list.append(np.asarray(img))
        if len(self._binary_stack_list) >= self.BINARY_FRAMES:
            self._safeDisconnect(self._commChannel.sigUpdateImage, self.addImgBinStack)
            self._state.binaryMaskSignalConnected = False
            self._master.lasersManager.execOn(
                self._state.laserFast, lambda l: l.setEnabled(False)
            )
            stack = np.stack(self._binary_stack_list, axis=0)
            self._binary_stack_list = []
            self.calculateBinaryMask(stack)

    def calculateBinaryMask(self, img_stack: np.ndarray) -> None:
        img_mean = np.mean(img_stack, 0)
        img_bin = ndi.gaussian_filter(img_mean, float(self._widget.bin_smooth_edit.text()))
        self._binary_mask = np.array(img_bin > float(self._widget.bin_thresh_edit.text()))
        self._widget.recordBinaryMaskButton.setText('Record binary mask')
        self.setAnalysisHelpImg(self._binary_mask)
        self.launchHelpWidget()

    def _cleanupBinaryMaskRecording(self) -> None:
        """Stop any interrupted binary-mask capture without touching acquisition."""
        if self._state.binaryMaskSignalConnected:
            self._safeDisconnect(self._commChannel.sigUpdateImage, self.addImgBinStack)
            self._state.binaryMaskSignalConnected = False
        self._binary_stack_list = []
        if hasattr(self._widget, 'recordBinaryMaskButton'):
            self._widget.recordBinaryMaskButton.setText('Record binary mask')

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

        if exinfo is not None and any(
            name in getattr(self, '_pipelineName', '') for name in ['cd_vesicle_prox', 'dynamin']
        ):
            self._widget.analysisHelpWidget.scatter.setData(
                x=np.array(exinfo['y']), y=np.array(exinfo['x']),
                pen=pg.mkPen(None), brush='g', symbol='x', size=15,
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
        if detectorName != self._state.detectorFast or self._state.busy:
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
                self.saveValidationImages(prev=True, prev_ana=True)
                self.pauseFastModality()
                self.endRecording()
                self.continueFastModality()
                self._state.frame = 0
                self._state.validating = False
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
        coords_wf = np.copy(self._first_coord(coords_detected))
        self.setDetLogLine('prepause', _now_us_tag())
        self.setDetLogLine('fastscan_x_center', coords_wf[0])
        self.setDetLogLine('fastscan_y_center', coords_wf[1])
        self._set_status('triggered')
        self.pauseFastModality()

        self.setDetLogLine('coord_transf_start', _now_us_tag())
        coords_scan = self._transformService.apply(
            coords_wf, self._transform_apply_extra_arg()
        )
        self.setDetLogLine('slowscan_x_center', coords_scan[0])
        self.setDetLogLine('slowscan_y_center', coords_scan[1])
        self.setDetLogLine('scan_initiate', _now_us_tag())
        self._log_all_detected_coords(coords_detected, override=coords_wf)

        try:
            slow_scan_ready = self.initiateSlowScan(position=coords_scan)
        except Exception as e:
            self._logger.error(
                f'Failed to initiate slow scan, likely due to not having loaded '
                f'scanning parameters. Error message: {e}'
            )
            self._set_status('error', str(e))
            self.setBusyFalse()
            self.continueFastModality()
            return

        if not slow_scan_ready:
            self._logger.error('Failed to initiate slow scan; scan was not started.')
            self._set_status('error', 'Failed to initiate slow scan.')
            self.setBusyFalse()
            self.continueFastModality()
            return

        if not self._widget.useScanLaserPresetCheck.isChecked():
            self._commChannel.scanWorkflow.notify_scan_starting()

        self._set_status('scanning')
        if not self.runSlowScan():
            self._set_status('error', 'Failed to trigger slow scan.')
            self.setBusyFalse()
            self.continueFastModality()
            return

        self.updateScatter(coords_detected)
        self._prevFrames.append(img)
        self.saveValidationImages(prev=True, prev_ana=False)
        self._exinfo = None
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
        result = self._triggeredScanRunner.trigger(
            self._state.scanInitiationMode.name,
            nidaq_manager=self._master.nidaqManager,
            signal_dict=self.signalDic,
            scan_info_dict=self.scanInfoDict,
            comm_channel=self._commChannel,
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

        Coefficients are setup-specific; both modalities historically use the
        same MoNaLISA fit, which is what's encoded here.
        """
        dwell_time = float(self._analogParameterDict['sequence_time'])
        px_size = float(self._analogParameterDict['axis_step_size'][0])
        C = np.array([-5.06873628, -80.6978355, 104.06976744,
                      -7.12113356, 8.0065076, 0.68227188])
        params = np.array([px_size**2, dwell_time**2, px_size*dwell_time,
                           px_size, dwell_time, 1])
        return center - float(np.sum(params * C))

    # ── Scan completion ──────────────────────────────────────────────────── #

    def scanEnded(self) -> None:
        self.setDetLogLine('scan_end', _now_us_tag())
        if self._state.scanInitiationMode == ScanInitiationMode.ScanWidget:
            self._commChannel.sigSnapImg.emit()
            try:
                samples = self.scanInfoDict['scan_samples_total']
                sample_rate = float(self.scanInfoDict.get(
                    'sample_rate', _DEFAULT_SAMPLE_RATE_HZ
                ))
                self.setDetLogLine('total_scan_time', samples / sample_rate)
            except KeyError:
                self._logger.info(
                    "Scan 'total_scan_time' not saved in log as 'scan_samples_total' "
                    "not available in scanInfoDict using current signal designer."
                )
        self.endRecording()
        self.continueFastModality()
        self._state.frame = 0

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
        if self._widget.endlessScanCheck.isChecked() and not self._state.running:
            try:
                self._on_resume_modality_hook()
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
