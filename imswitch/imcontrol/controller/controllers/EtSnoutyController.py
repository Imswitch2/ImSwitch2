"""Inspired from EtMonalisaController"""

import configparser
import enum
import importlib
import os
import sys
import time
from ast import literal_eval
from collections import deque
from datetime import datetime
from inspect import signature

import imageio
import numpy as np
import pyqtgraph as pg
from pyqtgraph import RectROI
from qtpy.QtCore import QObject, QTimer
from qtpy.QtWidgets import QMessageBox

from ..basecontrollers import ImConWidgetController
from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.EtSnoutyPaths import getEtSnoutyPath
from imswitch.imcontrol.view import guitools

from .SmartModeRoleMixin import SmartModeRoleMixin


_logsDir = getEtSnoutyPath('recordings', 'logs_et')
_paramsDir = getEtSnoutyPath('pipelinesParams')
_binaryMask = getEtSnoutyPath('binaryMask')


def _millis():
    return time.perf_counter_ns() / 1e6


class PeriodicSignalEmitter(QObject):
    def __init__(self, signal_to_emit, interval_ms=1000):
        super().__init__()
        self.signal_to_emit = signal_to_emit
        self.timer = QTimer()
        self.timer.setInterval(interval_ms)
        self.timer.timeout.connect(self.emit_signal)

    def start(self):
        self.timer.start()

    def stop(self):
        self.timer.stop()

    def emit_signal(self):
        self.signal_to_emit.emit()


class EtSnoutyController(SmartModeRoleMixin, ImConWidgetController):
    """Linked to EtSnoutyWidget."""

    SMART_MODE_WORKFLOW = 'EtSnouty'
    SMART_MODE_REQUIRED_ROLES = ('scouting', 'event')
    ANALYSIS_SCATTER_PIPELINE_NAME_MARKERS = ('cd_vesicle_prox', 'dynamin')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__logger = initLogger(self, instanceName='EtSnoutyController')

        self._widget.setFastDetectorList(
            self._master.detectorsManager.execOnAll(
                lambda c: c.name, condition=lambda c: c.forAcquisition
            )
        )
        self._widget.setFastLaserList(
            self._master.lasersManager.execOnAll(lambda c: c.name)
        )

        sys.path.append(self._widget.analysisDir)
        sys.path.append(self._widget.transformDir)
        self._ensureEtSnoutyFolders()

        self._widget.initiateButton.clicked.connect(self.initiate)
        self._widget.loadPipelineButton.clicked.connect(self.loadPipeline)
        self._widget.recordBinaryMaskButton.clicked.connect(self.initiateBinaryMask)
        self._widget.loadBinaryMaskButton.clicked.connect(self.loadBinaryMask)
        self._widget.clearBinaryMaskButton.clicked.connect(self.clearBinaryMask)
        self._widget.showBinaryMaskButton.clicked.connect(self.showBinaryMask)
        self._widget.setBusyFalseButton.clicked.connect(self.setBusyFalse)
        self._widget.sigSavePipelineClicked.connect(self.savePipelineParams)
        self._widget.sigLoadPipelineClicked.connect(self.loadPipelineParams)

        self.resetDetLog()

        self.ClockWidefield = False
        self.__runMode = RunMode.Experiment
        self.__running = False
        self.__validating = False
        self.__busy = False
        self.__clock_busy = False
        self.__prevFrames = deque(maxlen=10)
        self.__prevAnaFrames = deque(maxlen=10)
        self.__binary_mask = None
        self.__binary_stack = None
        self.__binary_frames = 10
        self.__init_frames = 5
        self.__validationFrames = 0
        self.__frame = 0
        self.__t_call = 0
        self.__maxAnaImgVal = 0
        self.__flipwfcalib = True
        self.__param_vals = []
        self.__pipeline_params = {}
        self.__pipelinename = ''
        self.__exinfo = None
        self._smartModeService = None

    # ------------------------------------------------------------------
    # Save / load pipeline parameters
    # ------------------------------------------------------------------

    def savePipelineParams(self):
        filePath = guitools.askForFilePath(
            self._widget, 'Save pipeline parameters', _paramsDir, isSaving=True
        )
        if filePath:
            self.savePipelineParamsToFile(filePath)

    def loadPipelineParams(self):
        filePath = guitools.askForFilePath(
            self._widget, 'Load pipeline parameters', _paramsDir
        )
        if filePath:
            self.loadPipelineParamsFromFile(filePath)

    def savePipelineParamsToFile(self, filePath: str) -> None:
        config = configparser.ConfigParser()
        config.optionxform = str
        pipeline_param_dict = {}
        for name_label, edit in zip(self._widget.param_names, self._widget.param_edits):
            pipeline_param_dict[name_label.text()] = edit.text()
        config['pipelineParameterDict'] = pipeline_param_dict
        with open(filePath, 'w') as configfile:
            config.write(configfile)

    def loadPipelineParamsFromFile(self, filePath: str) -> None:
        config = configparser.ConfigParser()
        config.optionxform = str
        config.read(filePath)
        if 'pipelineParameterDict' not in config.sections():
            self.__logger.error(
                f"Section 'pipelineParameterDict' missing in file: {filePath}"
            )
            return
        pipeline_param_dict = config['pipelineParameterDict']
        for name_label, edit in zip(self._widget.param_names, self._widget.param_edits):
            param_name = name_label.text()
            if param_name in pipeline_param_dict:
                raw_value = pipeline_param_dict[param_name]
                try:
                    value = literal_eval(raw_value)
                except Exception:
                    value = raw_value
                edit.setText(str(value))

    # ------------------------------------------------------------------
    # Experiment lifecycle
    # ------------------------------------------------------------------

    @staticmethod
    def _ensureEtSnoutyFolders():
        for folder in (
            _logsDir,
            os.path.join(_logsDir, 'frames'),
            _paramsDir,
            _binaryMask,
        ):
            os.makedirs(folder, exist_ok=True)

    def initiate(self):
        """Start or stop an EtSnouty experiment."""
        if not self.__running:
            self.resetParamVals()
            self.resetRunParams()

            self._commChannel.sigInitiateEtSnouty.emit(True)

            detectorFastIdx = self._widget.fastImgDetectorsPar.currentIndex()
            self.detectorFast = self._widget.fastImgDetectors[detectorFastIdx]

            laserFastIdx = self._widget.fastImgLasersPar.currentIndex()
            self.laserFast = self._widget.fastImgLasers[laserFastIdx]
            self.laserFastpower = float(self._widget.fastImgLasersPower_edit.text())

            self.__param_vals = self.readParams()
            self.__exinfo = None

            experimentModeIdx = self._widget.experimentModesPar.currentIndex()
            self.experimentMode = self._widget.experimentModes[experimentModeIdx]
            if self.experimentMode == 'TestVisualize':
                self.__runMode = RunMode.Visualize
            elif self.experimentMode == 'TestValidate':
                self.__runMode = RunMode.Validate
            else:
                self.__runMode = RunMode.Experiment

            if not self._preflightSmartModeRolesForStart():
                self._commChannel.sigInitiateEtSnouty.emit(False)
                self.resetRunParams()
                return

            if not self.setConfig(widefield=True):
                self._commChannel.sigInitiateEtSnouty.emit(False)
                self.resetRunParams()
                return

            self.detectorFast_controller = (
                self._master.detectorsManager.getDevice(self.detectorFast)
            )

            if self._widget.setUpdatePeriodCheck.isChecked():
                self.ClockWidefield = False
            else:
                self.ClockWidefield = True
                self.setUpdatePeriod()

            if self.__runMode in (RunMode.Validate, RunMode.Visualize):
                self.launchHelpWidget()

            if self.ClockWidefield:
                self._commChannel.sigClockWidefield.connect(self.clockWidefield_fct)
            else:
                self._commChannel.sigUpdateImage.connect(self.runPipeline)

            self._commChannel.sigToggleBlockScanWidget.emit(False)
            self._commChannel.sigScanEnded.connect(self.scanEnded)

            self._widget.initiateButton.setText('Stop')
            self.__running = True

        else:
            self._commChannel.sigInitiateEtSnouty.emit(False)

            if self.ClockWidefield:
                self._safeDisconnect(
                    self._commChannel.sigClockWidefield, self.clockWidefield_fct
                )
            else:
                self._safeDisconnect(self._commChannel.sigUpdateImage, self.runPipeline)

            self._commChannel.sigToggleBlockScanWidget.emit(True)
            self._safeDisconnect(self._commChannel.sigScanEnded, self.scanEnded)

            self._master.lasersManager.execOn(
                self.laserFast, lambda l: l.setEnabled(False)
            )
            self._applySmartModeRoleIfConfigured('idle')

            self._widget.initiateButton.setText('Initiate')
            self.resetParamVals()
            self.resetRunParams()

    def setConfig(self, widefield=True, role=None):
        if widefield:
            if self._smartModeSwitchingEnabled():
                if not self._applySmartModeRole(role or 'scouting', required=True):
                    self._recoverSmartModeFailure()
                    return False
                self._master.lasersManager.execOn(
                    self.laserFast, lambda l: l.setEnabled(True)
                )
            else:
                self._master.lasersManager.execOn(
                    self.laserFast, lambda l: l.setEnabled(True)
                )
                self._commChannel.sigSetConfig.emit('Widefield imaging')
            self._commChannel.sigSetVisibleLayers.emit((self.detectorFast,))
        else:
            self._master.lasersManager.execOn(
                self.laserFast, lambda l: l.setEnabled(False)
            )
            if not self._applyModeOrLegacyConfig(
                role or 'event',
                legacyConfigName='Light sheet imaging',
                required=True,
            ):
                self._recoverSmartModeFailure()
                return False
        return True

    def _applyModeOrLegacyConfig(self, role, legacyConfigName, required):
        if not self._smartModeSwitchingEnabled():
            self._commChannel.sigSetConfig.emit(legacyConfigName)
            return True
        return self._applySmartModeRole(role, required=required)

    def _preflightSmartModeRolesForStart(self):
        # Thin wrapper kept for EtSnouty's lifecycle; the workflow-agnostic logic
        # (required + configured-optional role resolution, preflight, logging)
        # lives in SmartModeRoleMixin._preflightSmartModeRoles.
        return self._preflightSmartModeRoles()

    def _recoverSmartModeFailure(self):
        self._master.lasersManager.execOn(
            self.laserFast, lambda l: l.setEnabled(False)
        )
        self._applySmartModeRoleIfConfigured('idle')

    def _stopAfterSmartModeFailure(self):
        self._commChannel.sigInitiateEtSnouty.emit(False)
        if self.ClockWidefield:
            self._safeDisconnect(
                self._commChannel.sigClockWidefield, self.clockWidefield_fct
            )
        else:
            self._safeDisconnect(self._commChannel.sigUpdateImage, self.runPipeline)
        self._commChannel.sigToggleBlockScanWidget.emit(True)
        self._safeDisconnect(self._commChannel.sigScanEnded, self.scanEnded)
        self._widget.initiateButton.setText('Initiate')
        self.__running = False
        self.resetParamVals()

    def scanEnded(self):
        self.setDetLogLine('scan_end', datetime.now().strftime('%Ss%fus'))
        self._commChannel.sigSnapImg.emit()
        try:
            total_scan_time = self.scanInfoDict['scan_samples_total'] * 10e-6
            self.setDetLogLine('total_scan_time', total_scan_time)
        except Exception:
            self.__logger.info(
                "Scan 'total_scan_time' not saved — 'scan_samples_total' unavailable."
            )
        self.endRecording()
        self.continueFastModality()
        self.__frame = 0

    def setDetLogLine(self, key, val, *args):
        if args:
            self.__detLog[f'{key}{args[0]}'] = val
        else:
            self.__detLog[key] = val

    def runSlowScan(self):
        self.__detLog['scan_start'] = datetime.now().strftime('%Ss%fus')
        if not self.setConfig(widefield=False):
            self._stopAfterSmartModeFailure()
            return False
        self._master.lasersManager.execOn(self.laserFast, lambda l: l.setEnabled(True))
        self._commChannel.sigRunScanTriggerScopePLSRMulticolor.emit()
        return True

    def endRecording(self):
        self.setDetLogLine('pipeline', self.getPipelineName())
        self.logPipelineParamVals()
        os.makedirs(_logsDir, exist_ok=True)
        filename = datetime.utcnow().strftime('%Hh%Mm%Ss%fus')
        name = os.path.join(_logsDir, filename) + '_log'
        log = [f'{key}: {self.__detLog[key]}' for key in self.__detLog]
        with open(f'{name}.txt', 'w') as f:
            for st in log:
                f.write(f'{st}\n')
        self.resetDetLog()

    def getPipelineName(self):
        pipelineidx = self._widget.analysisPipelinePar.currentIndex()
        return self._widget.analysisPipelines[pipelineidx]

    def logPipelineParamVals(self):
        params_ignore = ['img', 'prev_frames', 'binary_mask', 'testmode', 'exinfo']
        param_names = [
            name for name in self.__pipeline_params if name not in params_ignore
        ]
        for key, val in zip(param_names, self.__param_vals):
            self.setDetLogLine(key, val)

    def loadPipeline(self):
        self.__pipelinename = self.getPipelineName()
        self.pipeline = getattr(
            importlib.import_module(f'{self.__pipelinename}'),
            f'{self.__pipelinename}',
        )
        self.__pipeline_params = signature(self.pipeline).parameters
        self._widget.initParamFields(self.__pipeline_params)

    def getScanParameters(self):
        self._commChannel.sigRequestScanParameters.emit()

    def continueFastModality(self):
        if self._widget.endlessScanCheck.isChecked() and not self.__running:
            if not self.setConfig(widefield=True, role=self._resumeSmartModeRole()):
                self._stopAfterSmartModeFailure()
                return
            self._master.lasersManager.execOn(
                self.laserFast, lambda l: l.setEnabled(False)
            )
            self.updateScatter([], clear=True)

            if self.ClockWidefield:
                self._commChannel.sigClockWidefield.connect(self.clockWidefield_fct)
            else:
                self._commChannel.sigUpdateImage.connect(self.runPipeline)

            self._widget.initiateButton.setText('Stop')
            self.__running = True

        elif not self._widget.endlessScanCheck.isChecked():
            self.updateScatter([], clear=True)
            self._widget.initiateButton.setText('Initiate')
            self._commChannel.sigToggleBlockScanWidget.emit(True)
            self._safeDisconnect(self._commChannel.sigScanEnded, self.scanEnded)
            self.__running = False
            self.resetParamVals()

    def setBusyFalse(self):
        self.__logger.debug('setBusyFalse')
        self.__busy = False
        self.__clock_busy = False

    def readParams(self):
        return [float(item.text()) for item in self._widget.param_edits]

    def launchHelpWidget(self):
        self._widget.launchHelpWidget(self._widget.analysisHelpWidget, init=True)

    def resetDetLog(self):
        self.__detLog = {
            'pipeline': '',
            'pipeline_start': '',
            'pipeline_end': '',
            'coord_transf_start': '',
            'fastscan_x_center': 0,
            'fastscan_y_center': 0,
            'slowscan_x_center': 0,
            'slowscan_y_center': 0,
        }

    def resetParamVals(self):
        self.__param_vals = []

    def resetRunParams(self):
        self.__running = False
        self.__validating = False
        self.__frame = 0
        self.__maxAnaImgVal = 0
        self.__busy = False
        self.__clock_busy = False
        self.__prevFrames.clear()
        self.__prevAnaFrames.clear()

    # ------------------------------------------------------------------
    # Image pipeline
    # ------------------------------------------------------------------

    def runPipeline(self, detectorName, img, init=None, scale=None):
        """Run the analysis pipeline on a new fast-detector frame."""
        self.__logger.debug('runPipeline')
        del init, scale
        if detectorName != self.detectorFast:
            return
        if self.__busy:
            return

        t_sincelastcall = _millis() - self.__t_call
        self.__t_call = _millis()
        self.setDetLogLine('pipeline_rep_period', str(t_sincelastcall))
        self.setDetLogLine('pipeline_start', datetime.now().strftime('%Ss%fus'))
        self.__busy = True

        try:
            testmode = (
                self.__runMode in (RunMode.Visualize, RunMode.Validate)
                or self._widget.TestModeCheck.isChecked()
            )
            if testmode:
                coords_detected, self.__exinfo, img_ana = self.pipeline(
                    img, self.__prevFrames, self.__binary_mask, True,
                    self.__exinfo, *self.__param_vals
                )
            else:
                coords_detected, self.__exinfo = self.pipeline(
                    img, self.__prevFrames, self.__binary_mask, False,
                    self.__exinfo, *self.__param_vals
                )

            self.setDetLogLine('pipeline_end', datetime.now().strftime('%Ss%fus'))

            if self.__frame > self.__init_frames:
                if self.__runMode == RunMode.Visualize:
                    self.updateScatter(coords_detected, clear=True)
                    self.setAnalysisHelpImg(img_ana, self.__exinfo)

                elif self.__runMode == RunMode.Validate:
                    self.updateScatter(coords_detected, clear=True)
                    self.setAnalysisHelpImg(img_ana)
                    if self.__validating:
                        if self.__validationFrames > 5:
                            self.saveValidationImages(prev=True, prev_ana=True)
                            self.pauseFastModality()
                            self.endRecording()
                            self.continueFastModality()
                            self.__frame = 0
                            self.__validating = False
                        self.__validationFrames += 1
                    elif coords_detected.size != 0:
                        coords_wf = (
                            coords_detected[0, :] if np.size(coords_detected) > 2
                            else coords_detected[0]
                        )
                        self.setDetLogLine('fastscan_x_center', coords_wf[0])
                        self.setDetLogLine('fastscan_y_center', coords_wf[1])
                        if np.size(coords_detected) > 2:
                            for i in range(np.size(coords_detected, 0)):
                                self.setDetLogLine('det_coord_x_', coords_detected[i, 0], i)
                                self.setDetLogLine('det_coord_y_', coords_detected[i, 1], i)
                        self.__validating = True
                        self.__validationFrames = 0

                elif coords_detected.size != 0:
                    coords_wf = (
                        np.copy(coords_detected[0, :]) if np.size(coords_detected) > 2
                        else np.copy(coords_detected[0])
                    )
                    self.setDetLogLine('prepause', datetime.now().strftime('%Ss%fus'))
                    self.setDetLogLine('fastscan_x_center', coords_wf[0])
                    self.setDetLogLine('fastscan_y_center', coords_wf[1])
                    self.pauseFastModality()
                    self.setDetLogLine('coord_transf_start', datetime.now().strftime('%Ss%fus'))
                    self.setDetLogLine('scan_initiate', datetime.now().strftime('%Ss%fus'))
                    if np.size(coords_detected) > 2:
                        for i in range(np.size(coords_detected, 0)):
                            self.setDetLogLine('det_coord_x_', coords_wf[0], i)
                            self.setDetLogLine('det_coord_y_', coords_wf[1], i)
                    self.runSlowScan()
                    self.updateScatter(coords_detected, clear=True)
                    self.__prevFrames.append(img)
                    self.__busy = False
                    return

            self.__prevFrames.append(img)
            if self.__runMode == RunMode.Validate:
                self.__prevAnaFrames.append(img_ana)
            self.__frame += 1

        finally:
            try:
                if self._widget.TestModeCheck.isChecked() and 'img_ana' in dir():
                    folder_path = _logsDir
                    imageio.imwrite(
                        os.path.join(folder_path, f'frames/frame_{self.__frame}.png'), img_ana
                    )
                    imageio.imwrite(
                        os.path.join(folder_path, f'frames/frame_unmodif_{self.__frame}.png'),
                        img,
                    )
            except Exception:
                self.__logger.debug('Frame not saved')

            self.setDetLogLine('pipeline_end', datetime.now().strftime('%Ss%fus'))
            self.setBusyFalse()

    # ------------------------------------------------------------------
    # Clock-driven widefield acquisition
    # ------------------------------------------------------------------

    def setUpdatePeriod(self):
        self._master.lasersManager.execOn(self.laserFast, lambda l: l.setEnabled(False))
        self.__logger.debug('setUpdatePeriod')
        self.ClockWidefield = True
        self.__updatePeriod = int(self._widget.update_period_edit.text())
        self.emitter = PeriodicSignalEmitter(
            self._commChannel.sigClockWidefield, self.__updatePeriod
        )
        self.emitter.start()

    def clockWidefield_fct(self):
        self.__logger.debug('clockWidefield_fct_busy ?')
        if self.__clock_busy:
            self.__logger.debug(
                'clockWidefield_fct called but already busy — update period may be too short'
            )
            return
        self.__clock_busy = True
        self.__logger.debug('clockWidefield_fct')
        self._master.lasersManager.execOn(self.laserFast, lambda l: l.setEnabled(True))
        time.sleep(0.5)
        img = self.detectorFast_controller.wait_and_get_NewFrame(True)
        self._master.lasersManager.execOn(self.laserFast, lambda l: l.setEnabled(False))
        self.runPipeline(self.detectorFast, img)

    # ------------------------------------------------------------------
    # Analysis help widget
    # ------------------------------------------------------------------

    def setAnalysisHelpImg(self, img_ana, exinfo=None):
        if np.max(img_ana) > self.__maxAnaImgVal:
            self.__maxAnaImgVal = np.max(img_ana)
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
        self._widget.analysisHelpWidget.img.render()

    def _pipelineSupportsAnalysisScatter(self):
        return any(
            marker in self.__pipelinename
            for marker in self.ANALYSIS_SCATTER_PIPELINE_NAME_MARKERS
        )

    def updateScatter(self, coords, clear=True):
        if clear:
            self._commChannel.sigRemoveItemFromVb.emit(self._widget.getEventScatterPlot())
        if np.size(coords) > 0:
            self._widget.setEventScatterData(x=coords[:, 1], y=coords[:, 0])
            self._commChannel.sigAddItemToVb.emit(self._widget.getEventScatterPlot())

    def saveValidationImages(self, prev=True, prev_ana=True):
        if prev:
            img = np.array(list(self.__prevFrames))
            self._commChannel.sigSnapImgPrev.emit(self.detectorFast, img, 'raw')
            self.__prevFrames.clear()
        if prev_ana:
            img = np.array(list(self.__prevAnaFrames))
            self._commChannel.sigSnapImgPrev.emit(self.detectorFast, img, 'ana')
            self.__prevAnaFrames.clear()

    def pauseFastModality(self):
        if self.__running:
            if self.ClockWidefield:
                self._safeDisconnect(
                    self._commChannel.sigClockWidefield, self.clockWidefield_fct
                )
            else:
                self._safeDisconnect(self._commChannel.sigUpdateImage, self.runPipeline)
            self._master.lasersManager.execOn(
                self.laserFast, lambda l: l.setEnabled(False)
            )
            self.__running = False

    @staticmethod
    def _safeDisconnect(signal, slot):
        try:
            signal.disconnect(slot)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Binary mask
    # ------------------------------------------------------------------

    def initiateBinaryMask(self):
        detectorFastIdx = self._widget.fastImgDetectorsPar.currentIndex()
        self.detectorFast = self._widget.fastImgDetectors[detectorFastIdx]
        self.detectorFast_controller = (
            self._master.detectorsManager.getDevice(self.detectorFast)
        )
        time.sleep(1)
        self.latest_image = self.detectorFast_controller.getLatestFrame()

        self.roi_win = pg.GraphicsLayoutWidget(title='Define ROI for mask')
        self.view = self.roi_win.addViewBox()
        self.view.setAspectLocked(True)
        self.img_item = pg.ImageItem(self.latest_image)
        self.view.addItem(self.img_item)
        self.roi = RectROI([100, 100], [150, 150], pen='r')
        self.view.addItem(self.roi)
        self.roi_win.show()

        self._widget.recordBinaryMaskButton.clicked.disconnect(self.initiateBinaryMask)
        self._widget.recordBinaryMaskButton.clicked.connect(self.saveBinaryMask)
        self._widget.recordBinaryMaskButton.setText('Save and load')

    def saveBinaryMask(self):
        img_shape = self.latest_image.shape
        mask = np.zeros(img_shape, dtype=np.uint8)
        x, y = map(int, self.roi.pos())
        w, h = map(int, self.roi.size())
        x = max(0, x)
        y = max(0, y)
        x_end = min(x + w, img_shape[1])
        y_end = min(y + h, img_shape[0])
        mask[y:y_end, x:x_end] = 1
        self.__binary_mask = mask

        filePath = guitools.askForFilePath(
            self._widget, 'Save and load binary mask', _binaryMask, isSaving=True
        )
        if filePath:
            np.save(filePath, mask)

        self.roi_win.close()
        self._widget.recordBinaryMaskButton.clicked.disconnect(self.saveBinaryMask)
        self._widget.recordBinaryMaskButton.clicked.connect(self.initiateBinaryMask)
        self._widget.recordBinaryMaskButton.setText('Record binary mask')

    def loadBinaryMask(self):
        filePath = guitools.askForFilePath(
            self._widget, 'Load binary mask', _binaryMask
        )
        if filePath:
            self.__binary_mask = np.load(filePath)

    def clearBinaryMask(self):
        self.__binary_mask = None

    def showBinaryMask(self):
        mask = self.__binary_mask
        if mask is None:
            QMessageBox.information(self._widget, 'Warning', 'No binary mask loaded.')
            return

        detectorFastIdx = self._widget.fastImgDetectorsPar.currentIndex()
        self.detectorFast = self._widget.fastImgDetectors[detectorFastIdx]
        self.detectorFast_controller = (
            self._master.detectorsManager.getDevice(self.detectorFast)
        )
        time.sleep(1)
        img = self.detectorFast_controller.getLatestFrame()

        self.roi_win = pg.GraphicsLayoutWidget(title='Actual Binary Mask')
        self.view = self.roi_win.addViewBox()
        self.view.setAspectLocked(True)
        self.view.addItem(pg.ImageItem(img))

        overlay = np.zeros((mask.shape[0], mask.shape[1], 4), dtype=np.uint8)
        overlay[..., 0] = 255
        overlay[..., 3] = (mask * 120).astype(np.uint8)
        self.view.addItem(pg.ImageItem(overlay))
        self.roi_win.show()


class RunMode(enum.Enum):
    Experiment = 1
    Visualize = 2
    Validate = 3
