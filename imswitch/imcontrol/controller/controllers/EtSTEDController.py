import os
import time
import sys
import enum
import h5py
import csv

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
import scipy.ndimage as ndi
import pyqtgraph as pg
import numpy as np
from tkinter.filedialog import askopenfilename

from imswitch.imcommon.model import dirtools
from ..basecontrollers import ImConWidgetController
from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.EtSTEDPipelineRunner import EtSTEDPipelineRunner
from imswitch.imcontrol.model.EtSTEDTriggeredScanRunner import EtSTEDTriggeredScanRunner
from imswitch.imcontrol.model.EtSTEDTransformService import EtSTEDTransformService

_logsDir = os.path.join(dirtools.UserFileDirs.Root, 'recordings', 'logs_etsted')


def timestamp():
    """Return a monotonic high-resolution timestamp in nanoseconds."""
    return time.perf_counter_ns()

def micros():
    "Return a timestamp in microseconds (us). "
    return timestamp() / 1e3

def millis():
    "Return a timestamp in milliseconds (ms). "
    return timestamp() / 1e6


class RunMode(enum.Enum):
    Experiment = 1
    Visualize = 2
    Validate = 3


class ScanInitiationMode(enum.Enum):
    ScanWidget = 1
    RecordingWidget = 2


@dataclass
class EtSTEDSessionState:
    """Runtime-only state for an armed or active etSTED session."""

    runMode: RunMode = RunMode.Experiment
    scanInitiationMode: ScanInitiationMode | None = None
    detectorFast: str | None = None
    laserFast: str | None = None
    running: bool = False
    validating: bool = False
    busy: bool = False
    imageSignalConnected: bool = False
    scanEndSignalConnected: bool = False
    frame: int = 0
    validationFrames: int = 0
    tCallMs: float = 0
    maxAnaImgVal: float = 0
    detLog: dict[str, object] = field(default_factory=dict)


class EtSTEDController(ImConWidgetController):
    """ Linked to EtSTEDWidget."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._widget.setFastDetectorList(
            self._master.detectorsManager.execOnAll(lambda c: c.name,
                                                    condition=lambda c: c.forAcquisition)
        )

        self._widget.setFastLaserList(
            self._master.lasersManager.execOnAll(lambda c: c.name)
        )

        self.scanInitiationList = ['ScanWidget','RecordingWidget']
        self._widget.setScanInitiationList(self.scanInitiationList)

        sys.path.append(self._widget.analysisDir)

        # create a helper controller for the coordinate transform pop-out widget
        self.__coordTransformHelper = EtSTEDCoordTransformHelper(self, self._widget.coordTransformWidget, _logsDir)

        # Initiate coordinate transform coeffs
        self.__transformCoeffs = np.zeros(20)

        # Connect EtSTEDWidget and communication channel signals
        self._widget.initiateButton.clicked.connect(self.initiate)
        self._widget.loadPipelineButton.clicked.connect(self.loadPipeline)
        self._widget.recordBinaryMaskButton.clicked.connect(self.initiateBinaryMask)
        self._widget.loadScanParametersButton.clicked.connect(self.getScanParameters)
        self._widget.setUpdatePeriodButton.clicked.connect(self.setUpdatePeriod)
        self._widget.setBusyFalseButton.clicked.connect(self.setBusyFalse)
        self._commChannel.sigSendScanParameters.connect(lambda analogParams, digitalParams, positionersScan: self.assignScanParameters(analogParams, digitalParams, positionersScan))
        self._commChannel.sigSendScanFreq.connect(lambda scanFreq: self.logScanFreq(scanFreq))

        # initiate flags and params
        self.__state = EtSTEDSessionState()
        self.__pipelineRunner = EtSTEDPipelineRunner()
        self.__transformService = EtSTEDTransformService()
        self.__triggeredScanRunner = EtSTEDTriggeredScanRunner()

        # initiate log for each detected event
        self.resetDetLog()

        self.__prevFrames = deque(maxlen=10)
        self.__prevAnaFrames = deque(maxlen=10)
        self.__binary_mask = None
        self.__binary_stack = None
        self.__binary_frames = 10
        self.__init_frames = 5
        self.__flipwfcalib = True  # flipping widefield image when loading for transformation calibration
        self._analogParameterDict = {}
        self._digitalParameterDict = {}
        self._positionersScan = []

        # Leica stand command example
        ####self._master.standManager._subManager.setILShutter(0)

    def initiate(self):
        """ Initiate or stop an etSTED experiment. """
        if not self.__state.running:
            os.makedirs(_logsDir, exist_ok=True)
            self._setEtSTEDStatus('arming')

            try:
                self._prepareExperiment()
                # connect communication channel signals and turn on wf laser
                self._connectRunSignals()
                self._setFastLaserEnabled(True)

                self._widget.initiateButton.setText('Stop')
                self._widget.setEtSTEDControlsArmed(True)
                self._setEtSTEDStatus('detecting')
                self.__state.running = True
            except Exception as e:
                self._logger.error(f'Failed to initiate etSTED experiment: {e}', exc_info=True)
                self.stopExperiment(resetParams=True)
                self._setEtSTEDStatus('error', str(e))
        else:
            self.stopExperiment(resetParams=True)

    def _setEtSTEDStatus(self, status: str, message: str = ''):
        if hasattr(self._widget, 'setEtSTEDStatus'):
            self._widget.setEtSTEDStatus(status, message)

    def _prepareExperiment(self):
        """Validate UI selections and load runtime objects before arming EtSTED."""
        detectorFastIdx = self._widget.fastImgDetectorsPar.currentIndex()
        self.__state.detectorFast = self._widget.fastImgDetectors[detectorFastIdx]
        self.detectorFast = self.__state.detectorFast
        laserFastIdx = self._widget.fastImgLasersPar.currentIndex()
        self.__state.laserFast = self._widget.fastImgLasers[laserFastIdx]
        self.laserFast = self.__state.laserFast
        scanInitiationTypeIdx = self._widget.scanInitiationPar.currentIndex()
        scanInitiationType = self._widget.scanInitiation[scanInitiationTypeIdx]
        if scanInitiationType == self.scanInitiationList[0]:
            self.__state.scanInitiationMode = ScanInitiationMode.ScanWidget
        elif scanInitiationType == self.scanInitiationList[1]:
            self.__state.scanInitiationMode = ScanInitiationMode.RecordingWidget
        else:
            raise ValueError(f'Unknown scan initiation type: {scanInitiationType}')
        self.scanInitiationMode = self.__state.scanInitiationMode

        if not self._widget.analysisPipelines:
            raise RuntimeError('No etSTED analysis pipeline is available.')
        if not self._widget.transformPipelines:
            raise RuntimeError('No etSTED coordinate transform pipeline is available.')
        if not self._widget.transformCoefs:
            raise RuntimeError('No etSTED coordinate transform coefficients are available.')
        if self.__pipelineRunner.function is None:
            self.loadPipeline()

        self.__param_vals = self.readParams()
        # Reset parameter for extra information that pipelines can input and output
        self.__exinfo = None

        # Check if visualization mode, in case launch help widget
        experimentModeIdx = self._widget.experimentModesPar.currentIndex()
        self.experimentMode = self._widget.experimentModes[experimentModeIdx]
        if self.experimentMode == 'TestVisualize':
            self.__state.runMode = RunMode.Visualize
        elif self.experimentMode == 'TestValidate':
            self.__state.runMode = RunMode.Validate
        else:
            self.__state.runMode = RunMode.Experiment

        if self.__state.runMode == RunMode.Experiment:
            self.__triggeredScanRunner.validate_scan_parameters(
                self._analogParameterDict,
                self._digitalParameterDict,
                self._positionersScan
            )

        # check if visualization or validation mode
        if self.__state.runMode == RunMode.Validate or self.__state.runMode == RunMode.Visualize:
            self.launchHelpWidget()
        # load selected coordinate transform
        self.loadTransform()

    def _connectRunSignals(self):
        if not self.__state.imageSignalConnected:
            self._commChannel.sigUpdateImage.connect(self.runPipeline)
            self.__state.imageSignalConnected = True
        if not self.__state.scanEndSignalConnected:
            if self.__state.scanInitiationMode == ScanInitiationMode.ScanWidget:
                self._commChannel.sigToggleBlockScanWidget.emit(False)
                self._commChannel.sigScanEnded.connect(self.scanEnded)
            elif self.__state.scanInitiationMode == ScanInitiationMode.RecordingWidget:
                self._commChannel.sigRecordingEnded.connect(self.scanEnded)
            self.__state.scanEndSignalConnected = True

    def _disconnectRunSignals(self):
        if self.__state.imageSignalConnected:
            self._safeDisconnect(self._commChannel.sigUpdateImage, self.runPipeline)
            self.__state.imageSignalConnected = False
        if self.__state.scanEndSignalConnected:
            if self.__state.scanInitiationMode == ScanInitiationMode.ScanWidget:
                self._commChannel.sigToggleBlockScanWidget.emit(True)
                self._safeDisconnect(self._commChannel.sigScanEnded, self.scanEnded)
            elif self.__state.scanInitiationMode == ScanInitiationMode.RecordingWidget:
                self._safeDisconnect(self._commChannel.sigRecordingEnded, self.scanEnded)
            self.__state.scanEndSignalConnected = False

    def _safeDisconnect(self, signal, slot):
        try:
            signal.disconnect(slot)
        except (TypeError, RuntimeError):
            pass

    def _setFastLaserEnabled(self, enabled):
        if self.__state.laserFast is not None:
            try:
                self._master.lasersManager.execOn(self.__state.laserFast, lambda l: l.setEnabled(enabled))
            except Exception as e:
                self._logger.error(
                    f'Failed to set fast laser {self.__state.laserFast} enabled={enabled}: {e}',
                    exc_info=True
                )

    def stopExperiment(self, resetParams=False):
        """Best-effort stop path that leaves lasers and scan UI in a safe state."""
        self._disconnectRunSignals()
        try:
            self._setFastLaserEnabled(False)
        finally:
            self._widget.initiateButton.setText('Initiate')
            self._widget.setEtSTEDControlsArmed(False)
            if resetParams:
                self.resetParamVals()
            self.resetRunParams()
            if resetParams:
                self._setEtSTEDStatus('idle')

    def scanEnded(self):
        """ End an etSTED slow method scan. """
        self.setDetLogLine("scan_end",datetime.now().strftime('%Ss%fus'))
        if self.__state.scanInitiationMode == ScanInitiationMode.ScanWidget:
            self._commChannel.sigSnapImg.emit()
            try:
                total_scan_time = self.scanInfoDict['scan_samples_total'] * 10e-6  # length (s) of total scan signal
                self.setDetLogLine("total_scan_time", total_scan_time)
            except:
                self._logger.info("Scan 'total_scan_time' not saved in log as 'scan_samples_total' not available in scanInfoDict using current signal designer.")
        self.endRecording()
        self.continueFastModality()
        self.__state.frame = 0

    def setDetLogLine(self, key, val, *args):
        if args:
            self.__state.detLog[f"{key}{args[0]}"] = val
        else:
            self.__state.detLog[key] = val

    def runSlowScan(self):
        """ Run a scan of the slow method (STED). """
        self.__state.detLog[f"scan_start"] = datetime.now().strftime('%Ss%fus')
        result = self.__triggeredScanRunner.trigger(
            self.__state.scanInitiationMode.name,
            nidaq_manager=self._master.nidaqManager,
            signal_dict=getattr(self, 'signalDic', None),
            scan_info_dict=getattr(self, 'scanInfoDict', None),
            comm_channel=self._commChannel
        )
        if not result.success:
            self._logger.error(result.message)
        return result.success

    def endRecording(self):
        """ Save an etSTED slow method scan. """
        self.setDetLogLine("pipeline", self.getPipelineName())
        self.logPipelineParamVals()
        # save log file with temporal info of trigger event
        filename = datetime.utcnow().strftime('%Hh%Mm%Ss%fus')
        name = os.path.join(_logsDir, filename) + '_log'
        log = [f'{key}: {self.__state.detLog[key]}' for key in self.__state.detLog]
        os.makedirs(_logsDir, exist_ok=True)
        with open(f'{name}.txt', 'w') as f:
            [f.write(f'{st}\n') for st in log]
        self.resetDetLog()

    def getTransformName(self):
        """ Get the name of the pipeline currently used. """
        transformidx = self._widget.transformPipelinePar.currentIndex()
        transformname = self._widget.transformPipelines[transformidx]
        return transformname
    
    def getTransformCoefName(self):
        """ Get the name of the file with the selected coordinate transform parameters. """
        transformidx = self._widget.transformCoefsPar.currentIndex()
        transformname = self._widget.transformCoefs[transformidx]
        return transformname

    def getPipelineName(self):
        """ Get the name of the pipeline currently used. """
        pipelineidx = self._widget.analysisPipelinePar.currentIndex()
        pipelinename = self._widget.analysisPipelines[pipelineidx]
        return pipelinename

    def logPipelineParamVals(self):
        """ Put analysis pipeline parameter values in the log file. """
        for key, val in zip(self.__pipelineRunner.get_user_parameter_names(), self.__param_vals):
            self.setDetLogLine(key, val)

    def continueFastModality(self):
        """ Continue the fast method, after an event scan has been performed. """
        if self._widget.endlessScanCheck.isChecked() and not self.__state.running:
            # connect communication channel signals
            self._connectRunSignals()
            self._setFastLaserEnabled(True)
            
            self._widget.initiateButton.setText('Stop')
            self._widget.setEtSTEDControlsArmed(True)
            self._setEtSTEDStatus('detecting')
            self.__state.running = True
        elif not self._widget.endlessScanCheck.isChecked():
            self.stopExperiment(resetParams=True)

    def loadTransform(self):
        """ Load a coordinate transform and previously saved transform coefficients. """
        transformname = self.getTransformName()
        transformCoefName = self.getTransformCoefName()
        self.__transformService.load(self._widget.transformDir, transformname, transformCoefName)
        self.transform = self.__transformService.function
        self.__transformCoeffs = self.__transformService.coefficients

    def loadPipeline(self):
        """ Load the selected analysis pipeline, and its parameters into the GUI. """
        self.__pipelinename = self.getPipelineName()
        self.__pipeline_params = self.__pipelineRunner.load(self.__pipelinename)
        self.pipeline = self.__pipelineRunner.function
        self._widget.initParamFields(self.__pipeline_params)
        self._setEtSTEDStatus('idle', f'Loaded pipeline: {self.__pipelinename}')

    def initiateBinaryMask(self):
        """ Initiate the process of calculating a binary mask of the region of interest. """
        self.__binary_stack = None
        laserFastIdx = self._widget.fastImgLasersPar.currentIndex()
        self.__state.laserFast = self._widget.fastImgLasers[laserFastIdx]
        self.laserFast = self.__state.laserFast
        detectorFastIdx = self._widget.fastImgDetectorsPar.currentIndex()
        self.__state.detectorFast = self._widget.fastImgDetectors[detectorFastIdx]
        self.detectorFast = self.__state.detectorFast
        self._master.lasersManager.execOn(self.__state.laserFast, lambda l: l.setEnabled(True))
        self._commChannel.sigUpdateImage.connect(self.addImgBinStack)
        self._widget.recordBinaryMaskButton.setText('Recording...')

    def addImgBinStack(self, detectorName, img, init, scale, isCurrentDetector):
        """ Add image to the stack of images used to calculate a binary mask of the region of interest. """
        del init, scale, isCurrentDetector
        if detectorName == self.__state.detectorFast:
            if self.__binary_stack is None:
                self.__binary_stack = img
            elif len(self.__binary_stack) == self.__binary_frames:
                self._commChannel.sigUpdateImage.disconnect(self.addImgBinStack)
                self._master.lasersManager.execOn(self.__state.laserFast, lambda l: l.setEnabled(False))
                self.calculateBinaryMask(self.__binary_stack)
            else:
                if np.ndim(self.__binary_stack) == 2:
                    self.__binary_stack = np.stack((self.__binary_stack, img))
                else:
                    self.__binary_stack = np.concatenate((self.__binary_stack,  [img]), axis=0)

    def calculateBinaryMask(self, img_stack):
        """ Calculate the binary mask of the region of interest. """
        img_mean = np.mean(img_stack, 0)
        img_bin = ndi.filters.gaussian_filter(img_mean, float(self._widget.bin_smooth_edit.text()))
        self.__binary_mask = np.array(img_bin > float(self._widget.bin_thresh_edit.text()))
        self._widget.recordBinaryMaskButton.setText('Record binary mask')
        self.setAnalysisHelpImg(self.__binary_mask)
        self.launchHelpWidget()

    def setAnalysisHelpImg(self, img_ana, exinfo=None):
        """ Set the preprocessed image in the analysis help widget. """
        if np.max(img_ana) > self.__state.maxAnaImgVal:
            self.__state.maxAnaImgVal = np.max(img_ana)
            autolevels = True
        else:
            autolevels = False
        if img_ana.ndim == 3:
            img_ana = img_ana[0,:,:]
        self._widget.analysisHelpWidget.img.setImage(img_ana, autoLevels=autolevels)
        infotext = f'Min: {np.min(img_ana)}, max: {np.max(img_ana)}'
        self._widget.analysisHelpWidget.info_label.setText(infotext)

        # scatter plot exinfo if there is something (cdvesprox or dynamin)
        if exinfo is not None:
            if any(name in self.__pipelinename for name in ['cd_vesicle_prox', 'dynamin']):
                self._widget.analysisHelpWidget.scatter.setData(x=np.array(exinfo['y']), y=np.array(exinfo['x']), pen=pg.mkPen(None), brush='g', symbol='x', size=15)

        #self._widget.analysisHelpWidget.img.render()

    def getScanParameters(self):
        """ Load STED scan parameters from the scanning widget. """
        self._commChannel.sigRequestScanParameters.emit()

    def setUpdatePeriod(self):
        """ Set the update period for the fast method. """
        self.__updatePeriod = int(self._widget.update_period_edit.text())
        self._master.detectorsManager.setUpdatePeriod(self.__updatePeriod)

    def setBusyFalse(self):
        self.__state.busy = False

    def assignScanParameters(self, analogParams, digitalParams, positionersScan):
        """ Assign scan parameters from the scanning widget. """
        self._analogParameterDict = analogParams.copy()
        self._digitalParameterDict = digitalParams.copy()
        self._positionersScan = positionersScan.copy()
        self.setScanParametersStatus(analogParams, positionersScan)

    def readParams(self):
        """ Read user-provided analysis pipeline parameter values. """
        return self.__pipelineRunner.parse_parameter_values(self._widget.param_edits)

    def launchHelpWidget(self):
        """ Launch help widget that shows the preprocessed images in real-time. """
        self._widget.launchHelpWidget(self._widget.analysisHelpWidget, init=True)

    def resetDetLog(self):
        """ Reset the event log file. """
        self.__state.detLog = {
            "pipeline": "",
            "pipeline_start": "",
            "pipeline_end": "",
            "coord_transf_start": "",
            "fastscan_x_center": 0,
            "fastscan_y_center": 0,
            "slowscan_x_center": 0,
            "slowscan_y_center": 0
        }

    def resetParamVals(self):
        self.__param_vals = list()

    def resetRunParams(self):
        self.__state.running = False
        self.__state.validating = False
        self.__state.busy = False
        self.__state.frame = 0
        self.__state.validationFrames = 0
        self.__state.tCallMs = 0
        self.__state.maxAnaImgVal = 0

    def runPipeline(self, detectorName, img, init, scale, isCurrentDetector):
        """ If detector is detectorFast: run the analyis pipeline, called after every fast method frame. """
        del init, scale, isCurrentDetector
        if detectorName == self.__state.detectorFast:
            if not self.__state.busy:
                t_sincelastcall = millis() - self.__state.tCallMs
                self.__state.tCallMs = millis()
                self.setDetLogLine("pipeline_rep_period", str(t_sincelastcall))
                self.setDetLogLine("pipeline_start", datetime.now().strftime('%Ss%fus'))
                self.__state.busy = True
                try:
                    #t_pre = millis()
                    pipeline_result = self.__pipelineRunner.execute(
                        img,
                        self.__prevFrames,
                        self.__binary_mask,
                        self.__state.runMode in (RunMode.Visualize, RunMode.Validate),
                        self.__exinfo,
                        self.__param_vals
                    )
                    coords_detected = pipeline_result.coords_detected
                    self.__exinfo = pipeline_result.exinfo
                    img_ana = pipeline_result.analysis_image
                except Exception as e:
                    self._logger.error(f'etSTED pipeline failed: {e}', exc_info=True)
                    self._setEtSTEDStatus('error', str(e))
                    self.setBusyFalse()
                    return
                #t_post = millis()
                self.setDetLogLine("pipeline_end", datetime.now().strftime('%Ss%fus'))
                #self._logger.debug(f'Pipeline time: {t_post-t_pre} ms')

                if self.__state.frame > self.__init_frames:
                    # run if the initial frames have passed
                    if self.__state.runMode == RunMode.Visualize:
                        self.updateScatter(coords_detected, clear=True)
                        self.setAnalysisHelpImg(img_ana, self.__exinfo)
                    elif self.__state.runMode == RunMode.Validate:
                        self.updateScatter(coords_detected, clear=True)
                        self.setAnalysisHelpImg(img_ana)
                        if self.__state.validating:
                            if self.__state.validationFrames > 5:
                                self.saveValidationImages(prev=True, prev_ana=True)
                                self.pauseFastModality()
                                self.endRecording()
                                self.continueFastModality()
                                self.__state.frame = 0
                                self.__state.validating = False
                            self.__state.validationFrames += 1
                        elif coords_detected.size != 0:
                            # if some events where detected
                            if np.size(coords_detected) > 2:
                                coords_wf = coords_detected[0,:]
                            else:
                                coords_wf = coords_detected[0]
                            # log detected center coordinate
                            self.setDetLogLine("fastscan_x_center", coords_wf[0])
                            self.setDetLogLine("fastscan_y_center", coords_wf[1])
                            # log all detected coordinates
                            if np.size(coords_detected) > 2:
                                for i in range(np.size(coords_detected,0)):
                                    self.setDetLogLine("det_coord_x_", coords_detected[i,0], i)
                                    self.setDetLogLine("det_coord_y_", coords_detected[i,1], i)
                            self.__state.validating = True
                            self.__state.validationFrames = 0
                    elif coords_detected.size != 0:
                        # if some events were detected
                        if np.size(coords_detected) > 2:
                            coords_wf = np.copy(coords_detected[0,:])
                        else:
                            coords_wf = np.copy(coords_detected[0])
                        self.setDetLogLine("prepause", datetime.now().strftime('%Ss%fus'))
                        self.setDetLogLine("fastscan_x_center", coords_wf[0])
                        self.setDetLogLine("fastscan_y_center", coords_wf[1])
                        self._setEtSTEDStatus('triggered')
                        self.pauseFastModality()
                        self.setDetLogLine("coord_transf_start", datetime.now().strftime('%Ss%fus'))
                        coords_scan = self.__transformService.apply(
                            coords_wf, getattr(self._setupInfo, 'etSTED', None)
                        )
                        self.setDetLogLine("slowscan_x_center", coords_scan[0])
                        self.setDetLogLine("slowscan_y_center", coords_scan[1])
                        self.setDetLogLine("scan_initiate", datetime.now().strftime('%Ss%fus'))
                        # save all detected coordinates in the log
                        if np.size(coords_detected) > 2:
                            for i in range(np.size(coords_detected,0)):
                                self.setDetLogLine("det_coord_x_", coords_wf[0], i)
                                self.setDetLogLine("det_coord_y_", coords_wf[1], i)
                        
                        #self._logger.debug(f'coords_wf: {coords_wf}')
                        #self._logger.debug(f'coords_scan: {coords_scan}')
                        try:
                            slow_scan_ready = self.initiateSlowScan(position=coords_scan)
                        except Exception as e:
                            self._logger.error(f"Failed to initiate slow scan, likely due to not having loaded scanning parameters. Error message: {e}")
                            self.setBusyFalse()
                            self.continueFastModality()
                            self._setEtSTEDStatus('error', str(e))
                            return
                        if not slow_scan_ready:
                            self._logger.error("Failed to initiate slow scan; scan was not started.")
                            self.setBusyFalse()
                            self.continueFastModality()
                            self._setEtSTEDStatus('error', 'Failed to initiate slow scan.')
                            return
                        # trigger scan starting signal emission or not - if triggered, use scan-standard laser preset
                        if not self._widget.useScanLaserPresetCheck.isChecked():
                            self._commChannel.sigScanStarting.emit()
                        
                        self._setEtSTEDStatus('scanning')
                        if not self.runSlowScan():
                            self.setBusyFalse()
                            self.continueFastModality()
                            self._setEtSTEDStatus('error', 'Failed to trigger slow scan.')
                            return

                        # update scatter plot of event coordinates in the shown fast method image
                        self.updateScatter(coords_detected, clear=True)

                        self.__prevFrames.append(img)
                        self.saveValidationImages(prev=True, prev_ana=False)
                        self.__exinfo = None
                        self.__state.busy = False
                        return
                #self.__bkg = img
                self.__prevFrames.append(img)
                if self.__state.runMode == RunMode.Validate:
                    self.__prevAnaFrames.append(img_ana)
                self.__state.frame += 1
                self.setBusyFalse()

    def initiateSlowScan(self, position=None):
        """ Initiate a STED scan. """
        result = self.__triggeredScanRunner.prepare(
            position,
            self.__state.scanInitiationMode.name,
            self._analogParameterDict,
            self._digitalParameterDict,
            self._positionersScan,
            scan_manager=self._master.scanManager,
            comm_channel=self._commChannel,
            positioners_manager=self._master.positionersManager,
            apply_fast_axis_shift=self._widget.fastaxisshiftCheck.isChecked(),
            fast_axis_shift_fn=self.addFastAxisShift
        )
        if result.success:
            self.signalDic = result.signal_dict
            self.scanInfoDict = result.scan_info_dict
        else:
            self._logger.error(result.message)
            self._setEtSTEDStatus('error', result.message)
        return result.success

    def setCenterScanParameter(self, position):
        """ Set the scanning center from the detected event coordinates. """
        self.__triggeredScanRunner.set_center_scan_parameter(
            self._analogParameterDict,
            self._positionersScan,
            position,
            self._master.positionersManager,
            self._widget.fastaxisshiftCheck.isChecked(),
            self.addFastAxisShift
        )

    def logScanFreq(self, scanFreq):
        self.setDetLogLine("scan_period", scanFreq)

    def addFastAxisShift(self, center):
        """ Add a scanning-method and microscope-specific shift to the fast axis scanning. 
        Based on second-degree curved surface fit to 2D-sampling of dwell time and pixel size induced shifts. """
        dwell_time = float(self._analogParameterDict['sequence_time'])
        px_size = float(self._analogParameterDict['axis_step_size'][0])
        C = np.array([-5.06873628, -80.6978355, 104.06976744, -7.12113356, 8.0065076, 0.68227188])  # second order plane fit
        params = np.array([px_size**2, dwell_time**2, px_size*dwell_time, px_size, dwell_time, 1])  # for use with second order plane fit
        shift_compensation = np.sum(params*C)
        center -= shift_compensation
        return(center)

    def setScanParametersStatus(self, scanInfo, positionersScan):
        pixel_sizes = scanInfo['axis_step_size']
        axis_lens = scanInfo['axis_length']
        pixels = np.divide(axis_lens,pixel_sizes)
        dwell_time = scanInfo['sequence_time'] * 1E6
        dwell_time_message = f'{dwell_time:.0f} µs'
        scan_axes = [positioner for positioner in positionersScan if positioner != 'None']
        scan_axes_message = ' x '.join(map(str, scan_axes))
        size_list = [len for len, pixel in zip(axis_lens, pixels) if pixel != 1]
        size_message = ' x '.join(map(str, size_list)) + ' µm'
        pixel_sizes_list = [pxsize for pxsize, pixel in zip(pixel_sizes, pixels) if pixel != 1]
        pixel_sizes_message = ' x '.join(map(str, pixel_sizes_list)) + ' µm'
        
        text = f'Current scan loaded: axes: {scan_axes_message}, axis lengths: {size_message}, pixel sizes: {pixel_sizes_message}, dwell time: {dwell_time_message}'
        self._widget.loadScanParametersStatus.setText(text)
        self._setEtSTEDStatus('idle', 'Scan parameters loaded.')

    def setCentersScanWidget(self):
        self.__triggeredScanRunner.set_centers_scan_widget(
            self._analogParameterDict, self._commChannel
        )

    def triggerRecordingWidgetScan(self):
        return self.__triggeredScanRunner.trigger(
            ScanInitiationMode.RecordingWidget.name,
            comm_channel=self._commChannel
        ).success

    def updateScatter(self, coords, clear=True):
        """ Update the scatter plot of detected event coordinates. """
        if np.size(coords) > 0:
            self._widget.setEventScatterData(x=coords[:,1],y=coords[:,0])
            # possibly not the below more than one time. Maybe it is enough to then update it, if the reference to the same object is kept throughout all function calls
            self._commChannel.sigAddItemToVb.emit(self._widget.getEventScatterPlot())

    def saveValidationImages(self, prev=True, prev_ana=True):
        """ Save the widefield validation images of an event detection. """
        if prev:
            img = np.array(list(self.__prevFrames))
            self._commChannel.sigSnapImgPrev.emit(self.__state.detectorFast, img, 'raw')
            self.__prevFrames.clear()
        if prev_ana:
            img = np.array(list(self.__prevAnaFrames))
            self._commChannel.sigSnapImgPrev.emit(self.__state.detectorFast, img, 'ana')
            self.__prevAnaFrames.clear()

    def pauseFastModality(self):
        """ Pause the fast method, when an event has been detected. """
        if self.__state.running:
            self._safeDisconnect(self._commChannel.sigUpdateImage, self.runPipeline)
            self.__state.imageSignalConnected = False
            self._setFastLaserEnabled(False)
            self.__state.running = False

    def getFlipWf(self):
        return self.__flipwfcalib

    def closeEvent(self):
        self.stopExperiment(resetParams=True)


class EtSTEDCoordTransformHelper():
    """ Coordinate transform help widget controller. """
    def __init__(self, etSTEDController, coordTransformWidget, saveFolder, *args, **kwargs):
        
        self.__logger = initLogger(self)

        self.etSTEDController = etSTEDController
        self._widget = coordTransformWidget
        self.__saveFolder = saveFolder
        self.__transformService = EtSTEDTransformService()

        # initiate coordinate transform parameters
        self.__transformCoeffs = np.zeros(20)
        self.__loResCoords = list()
        self.__hiResCoords = list()
        self.__loResCoordsPx = list()
        self.__hiResCoordsPx = list()
        self.__hiResPxSize = 1
        #self.__loResPxSize = 1
        self.__hiResSize = 1
        self.__loResSize = 1

        # connect signals from widget
        self.etSTEDController._widget.coordTransfCalibButton.clicked.connect(self.calibrationLaunch)
        self._widget.saveCalibButton.clicked.connect(self.calibrationFinish)
        self._widget.resetCoordsButton.clicked.connect(self.resetCalibrationCoords)
        self._widget.loadLoResButton.clicked.connect(lambda: self.loadCalibImage('lo'))
        self._widget.loadHiResButton.clicked.connect(lambda: self.loadCalibImage('hi'))

    def getTransformCoeffs(self):
        """ Get transformation coefficients. """
        return self.__transformCoeffs

    def calibrationLaunch(self):
        """ Launch calibration. """
        self.etSTEDController._widget.launchHelpWidget(self.etSTEDController._widget.coordTransformWidget, init=True)

    def calibrationFinish(self):
        """ Finish calibration. """
        # get annotated coordinates in both images and translate to real space coordinates
        self.__loResCoordsPx = self._widget.pointsLayerLo.data
        for pos_px in self.__loResCoordsPx:
            #pos = (np.around(pos_px[0]*self.__loResPxSize, 3), np.around(pos_px[1]*self.__loResPxSize, 3))
            pos = (np.around(pos_px[0], 3), np.around(pos_px[1], 3))
            self.__loResCoords.append(pos)
        self.__hiResCoordsPx = self._widget.pointsLayerHi.data
        for pos_px in self.__hiResCoordsPx:
            # the following depends on the array viewing/axes order for camera and scan images, works for the current napari viewer (ImSwitch v1.2.1)
            pos = (np.around((self.__loResSize-pos_px[1])*self.__hiResPxSize - self.__hiResSize/2, 3), np.around((self.__loResSize-pos_px[0])*self.__hiResPxSize - self.__hiResSize/2, 3))
            self.__hiResCoords.append(pos)

        # calibrate coordinate transform
        self.coordinateTransformCalibrate()
        name_short = datetime.utcnow().strftime('%Hh%Mm%Ss')
        name_long = datetime.utcnow().strftime('%Y-%m-%d-%Hh%Mm%Ss')
        filename_txt = os.path.join(self.__saveFolder, name_short+'_transformCoeffs.txt')
        filename_json = os.path.join(self.__saveFolder, name_short+'_transformMetadata.json')
        filename_csv = os.path.join(self.etSTEDController._widget.transformDir, name_long+'.csv')
        os.makedirs(self.__saveFolder, exist_ok=True)
        os.makedirs(self.etSTEDController._widget.transformDir, exist_ok=True)
        np.savetxt(fname=filename_txt, X=self.__transformCoeffs)
        self.__transformService.save_calibration_metadata(
            filename_json,
            self.__loResCoords,
            self.__hiResCoords,
            self.__transformCoeffs
        )
        with open(filename_csv, 'w', newline='') as csvfile:
            writer = csv.writer(csvfile, delimiter=' ', quotechar='|', quoting=csv.QUOTE_MINIMAL)
            for el in self.__transformCoeffs:
                writer.writerow([str(el)])

        # plot the resulting transformed low-res coordinates on the hi-res image
        coords_transf = []
        for i in range(0,len(self.__loResCoords)):
            pos = self.__transformService.poly_thirdorder_transform(self.__transformCoeffs, self.__loResCoords[i])
            # the following depends on the array viewing/axes order for camera and scan images, works for the current napari viewer (ImSwitch v1.2.1)
            pos_px = (np.around(self.__loResSize-(pos[1] + self.__hiResSize/2)/self.__hiResPxSize, 3), np.around(self.__loResSize-(pos[0] + self.__hiResSize/2)/self.__hiResPxSize, 3))
            coords_transf.append(pos_px)
        coords_transf = np.array(coords_transf)
        self._widget.pointsLayerTransf.data = coords_transf

        # update list of available coordinate transform coefficients
        self.etSTEDController._widget.updateCoordTransformCoeffPar()

    def resetCalibrationCoords(self):
        """ Reset all selected coordinates. """
        self.__loResCoords = list()
        self.__loResCoordsPx = list()
        self.__hiResCoords = list()
        self.__hiResCoordsPx = list()
        self._widget.pointsLayerLo.data = []
        self._widget.pointsLayerHi.data = []
        self._widget.pointsLayerTransf.data = []

    def loadCalibImage(self, modality):
        """ Load low or high resolution calibration image. """
        # open gui to choose file
        img_filename = self.openFolder()
        # load img data from file
        with h5py.File(img_filename, "r") as f:
            img_key = list(f.keys())[0]
            pixelsize = f[img_key].attrs['element_size_um'][1]
            img_data = np.array(f[img_key])
            imgsize = pixelsize*np.size(img_data,0)
        # view data in corresponding viewbox
        self.updateCalibImage(img_data, modality)
        if modality == 'hi':
            self.__hiResCoords = list()
            self.__hiResPxSize = pixelsize
            self.__hiResSize = imgsize
        elif modality == 'lo':
            self.__loResCoords = list()
            self.__loResSize = np.shape(img_data)[1]
            #self.__loResPxSize = pixelsize

    def openFolder(self):
        """ Opens current folder in File Explorer and returns chosen filename. """
        filename = askopenfilename()
        return filename

    def updateCalibImage(self, img_data, modality):
        """ Update new image in the viewbox. """
        if modality == 'hi':
            viewer = self._widget.napariViewerHi
        elif modality == 'lo':
            viewer = self._widget.napariViewerLo
            if self.etSTEDController.getFlipWf():
                img_data = np.moveaxis(img_data, 0, 1)
        viewer.add_image(img_data)
        viewer.layers.unselect_all()
        viewer.layers.move_selected(len(viewer.layers)-1,0)

    def coordinateTransformCalibrate(self):
        """ Third-order polynomial fitting with least-squares Levenberg-Marquart algorithm. """
        # prepare data and init guess
        xdata = np.array([*self.__loResCoords]).astype(np.float32)
        ydata = np.array([*self.__hiResCoords]).astype(np.float32)
        self.__transformCoeffs = self.__transformService.calibrate(xdata, ydata)

    def poly_thirdorder(self, a, x, y):
        """ Polynomial function that will be fit in the least-squares fit. """
        return self.__transformService.poly_thirdorder_residuals(a, x, y)
    
    def poly_thirdorder_transform(self, a, x):
        """ Use for plotting the least-squares fit results. """
        return self.__transformService.poly_thirdorder_transform(a, x)

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
