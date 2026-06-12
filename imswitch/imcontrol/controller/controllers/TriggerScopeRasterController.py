import os
import configparser
from ast import literal_eval
from ..basecontrollers import ImConWidgetController, ScanLifecycleMixin
import numpy as np
import traceback
from imswitch.imcommon.model import APIExport, dirtools
from imswitch.imcontrol.view import guitools
from imswitch.imcommon.view.guitools import colorutils
from ._beadrec_scan_source import BeadRecScanSourceMixin


class TriggerScopeRasterController(BeadRecScanSourceMixin, ScanLifecycleMixin, ImConWidgetController):
    """Linked to TriggerScopeRasterWidget."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.settingAttr = False
        self.settingParameters = False

        self._analogParameterDict = {}
        self._digitalParameterDict = {}
        self.signalDict = None
        self.scanInfoDict = None
        self.isRunning = False
        self.doingNonFinalPartOfSequence = False

        self.positioners = {
            pName: pManager for pName, pManager in self._setupInfo.positioners.items()
            if pManager.forScanning
        }
        self.TTLDevices = self._setupInfo.getTTLDevices()

        self._widget.initControls(
            self.positioners.keys(),
            self.TTLDevices.keys(),
            self._master.scanManager.TTLTimeUnits
        )

        self.scanDir = os.path.join(dirtools.UserFileDirs.Root, 'imcontrol_scans')
        if not os.path.exists(self.scanDir):
            os.makedirs(self.scanDir)

        self.getParameters()
        self.updateSteps()
        self.plotSignalGraph()
        self.updateScanStageAttrs()
        self.updateScanTTLAttrs()

        self._master.scanManager.sigScanStarted.connect(
            lambda: self.emitScanSignal(self._commChannel.sigScanStarted)
        )
        self._master.scanManager.sigScanDone.connect(self.scanDone)

        self._commChannel.sigRunScan.connect(self.runScanExternal)
        self._commChannel.sigAbortScan.connect(self.abortScan)
        self._commChannel.sharedAttrs.sigAttributeSet.connect(self.attrChanged)

        self._widget.sigSaveScanClicked.connect(self.saveScan)
        self._widget.sigLoadScanClicked.connect(self.loadScan)
        self._widget.sigRunScanClicked.connect(self.runScan)
        self._widget.sigSeqTimeParChanged.connect(self.plotSignalGraph)
        self._widget.sigSeqTimeParChanged.connect(self.updateScanTTLAttrs)
        self._widget.sigStageParChanged.connect(self.updateSteps)
        self._widget.sigStageParChanged.connect(self.updateScanStageAttrs)
        self._widget.sigSignalParChanged.connect(self.plotSignalGraph)
        self._widget.sigSignalParChanged.connect(self.updateScanTTLAttrs)

    def saveScan(self):
        fileName = guitools.askForFilePath(self._widget, 'Save scan', self.scanDir, isSaving=True)
        if not fileName:
            return
        self.saveScanParamsToFile(fileName)

    @APIExport(runOnUIThread=True)
    def saveScanParamsToFile(self, filePath: str) -> None:
        """Saves the set scanning parameters to the specified file."""
        self.getParameters()
        config = configparser.ConfigParser()
        config.optionxform = str
        config['analogParameterDict'] = self._analogParameterDict
        config['digitalParameterDict'] = self._digitalParameterDict
        with open(filePath, 'w') as configfile:
            config.write(configfile)

    def loadScan(self):
        fileName = guitools.askForFilePath(self._widget, 'Load scan', self.scanDir)
        if not fileName:
            return
        self.loadScanParamsFromFile(fileName)

    @APIExport(runOnUIThread=True)
    def loadScanParamsFromFile(self, filePath: str) -> None:
        """Loads scanning parameters from the specified file."""
        config = configparser.ConfigParser()
        config.optionxform = str
        config.read(filePath)
        for key in self._analogParameterDict:
            self._analogParameterDict[key] = literal_eval(
                config._sections['analogParameterDict'][key]
            )
        for key in self._digitalParameterDict:
            self._digitalParameterDict[key] = literal_eval(
                config._sections['digitalParameterDict'][key]
            )
        self.setParameters()

    def setParameters(self):
        self.settingParameters = True
        try:
            for i in range(len(self._analogParameterDict['target_device'])):
                positionerName = self._analogParameterDict['target_device'][i]
                self._widget.setScanDim(i, positionerName)
                self._widget.setScanSize(positionerName,
                                         self._analogParameterDict['axis_length'][i])
                self._widget.setScanStepSize(positionerName,
                                             self._analogParameterDict['axis_step_size'][i])

            setTTLDevices = []
            for i in range(len(self._digitalParameterDict['target_device'])):
                deviceName = self._digitalParameterDict['target_device'][i]
                self._widget.setTTLStarts(deviceName, self._digitalParameterDict['TTL_start'][i])
                self._widget.setTTLEnds(deviceName, self._digitalParameterDict['TTL_end'][i])
                setTTLDevices.append(deviceName)

            for deviceName in self.TTLDevices:
                if deviceName not in setTTLDevices:
                    self._widget.unsetTTL(deviceName)

            self._widget.setSeqTimePar(self._digitalParameterDict['sequence_time'])
        finally:
            self.settingParameters = False
            self.plotSignalGraph()

    def getTriggerscopeParameters(self):
        AOtargets = []
        lengthsVolt = []
        stepSizesVolt = []
        startPosVolt = []
        rasterScanParameters = {'Analog': None, 'Digital': None}
        for dim in range(len(self._analogParameterDict['target_device'])):
            target = self._widget.scanPar['scanDim' + str(dim)].currentText()
            convFactor = self.positioners[target].managerProperties['conversionFactor']
            index = self._analogParameterDict['target_device'].index(target)
            AOtargets.append(target)
            lengthsVolt.append(self._analogParameterDict['axis_length'][index] / convFactor)
            stepSizesVolt.append(self._analogParameterDict['axis_step_size'][index] / convFactor)
            startPosVolt.append(self._analogParameterDict['axis_startpos'][index] / convFactor)

        rasterScanParameters['Analog'] = {'targets': AOtargets,
                                          'lengths': lengthsVolt,
                                          'stepSizes': stepSizesVolt,
                                          'startPos': startPosVolt}
        rasterScanParameters['Digital'] = self._digitalParameterDict
        return rasterScanParameters

    def runScanExternal(self, recalculateSignals, isNonFinalPartOfSequence):
        self._widget.setRepeatEnabled(False)
        self.runScanAdvanced(recalculateSignals=recalculateSignals,
                             isNonFinalPartOfSequence=isNonFinalPartOfSequence,
                             sigScanStartingEmitted=True)

    def runScanAdvanced(self, *, recalculateSignals=True, isNonFinalPartOfSequence=False,
                        sigScanStartingEmitted):
        """Runs a scan with the set scanning parameters."""
        try:
            self._widget.setScanButtonChecked(True)
            self.isRunning = True
            self.doingNonFinalPartOfSequence = isNonFinalPartOfSequence
            if not sigScanStartingEmitted:
                self.emitScanSignal(self._commChannel.sigScanStarting)
            # Tell the LaserController which lasers participate in this scan so
            # it can arm them (digital-modulation / external-control mode) and
            # leave the rest off. The TriggerScope firmware runs the scan
            # autonomously, so unlike the Advanced/Nidaq path this signal must
            # be emitted here rather than from the DAQ manager.
            self.emitScanSignal(self._commChannel.sigScanBuilt,
                                self._getScanLaserDevices())
            self._logRasterTTLDiagnostics()
            triggerscopeParameters = self.getTriggerscopeParameters()
            self._master.scanManager.runScan(triggerscopeParameters, scan_type='rasterScan')
        except Exception:
            self._logger.error(traceback.format_exc())
            self.isRunning = False

    def abortScan(self):
        self.doingNonFinalPartOfSequence = False
        if not self.isRunning:
            self.scanFailed()

    def scanDone(self):
        # All TriggerScope scan controllers share the board-level sigScanDone, so
        # every one of them receives this when the firmware reports end-of-scan.
        # Only the controller that actually started the scan should tear down;
        # the rest must ignore it (their isRunning is False) to avoid redundant
        # laser disarm rounds and duplicate sigScanEnded emissions.
        if not self.isRunning:
            return
        self._logger.debug('Scan done')
        self.isRunning = False
        if not self._widget.repeatEnabled():
            self.emitScanSignal(self._commChannel.sigScanDone)
            if not self.doingNonFinalPartOfSequence:
                self._widget.setScanButtonChecked(False)
                self.emitScanSignal(self._commChannel.sigScanEnded)
        else:
            self._logger.debug('Repeat scan')
            self.runScanAdvanced(sigScanStartingEmitted=True)

    def scanFailed(self):
        self._logger.error('Scan failed')
        self.isRunning = False
        self.doingNonFinalPartOfSequence = False
        self._widget.setScanButtonChecked(False)
        self.emitScanSignal(self._commChannel.sigScanEnded)

    def getParameters(self):
        if self.settingParameters:
            return

        self._analogParameterDict['target_device'] = []
        self._analogParameterDict['axis_length'] = []
        self._analogParameterDict['axis_step_size'] = []
        self._analogParameterDict['axis_startpos'] = []
        for i in range(len(self.positioners)):
            positionerName = self._widget.getScanDim(i)
            size = self._widget.getScanSize(positionerName)
            stepSize = self._widget.getScanStepSize(positionerName)
            start = list(self._master.positionersManager[positionerName].position.values())[0]
            self._analogParameterDict['target_device'].append(positionerName)
            self._analogParameterDict['axis_length'].append(size)
            self._analogParameterDict['axis_step_size'].append(stepSize)
            self._analogParameterDict['axis_startpos'].append(start)

        self._digitalParameterDict['target_device'] = []
        self._digitalParameterDict['TTL_start'] = []
        self._digitalParameterDict['TTL_end'] = []
        for deviceName, deviceInfo in self.TTLDevices.items():
            if not self._widget.getTTLIncluded(deviceName):
                continue
            self._digitalParameterDict['target_device'].append(deviceName)
            self._digitalParameterDict['TTL_start'].append(self._widget.getTTLStarts(deviceName)[0])
            self._digitalParameterDict['TTL_end'].append(self._widget.getTTLEnds(deviceName)[0])

        self._digitalParameterDict['sequence_time'] = self._widget.getSeqTimePar()

    def updateSteps(self):
        self.getParameters()
        for index, positionerName in enumerate(self.positioners):
            if float(self._analogParameterDict['axis_step_size'][index]) != 0:
                steps = round(float(self._analogParameterDict['axis_length'][index]) /
                               float(self._analogParameterDict['axis_step_size'][index]))
                self._widget.setScanSteps(positionerName, steps)

    def plotSignalGraph(self):
        dwellTime = float(self._widget.seqTimePar.text())
        graphSamples = 10000
        areas = []
        signals = []
        colors = []
        for deviceName in self.TTLDevices.keys():
            isLaser = deviceName in self._setupInfo.lasers
            x = np.linspace(0, dwellTime, graphSamples)
            signal = np.zeros(graphSamples)
            try:
                start = float(self._widget.pxParameters['sta' + deviceName].text())
                end = float(self._widget.pxParameters['end' + deviceName].text())
                signal[x > start] = 1
                signal[x > end] = 0
            except ValueError:
                pass
            areas.append(x)
            signals.append(signal)
            colors.append(
                colorutils.wavelengthToHex(
                    self._setupInfo.lasers[deviceName].wavelength
                ) if isLaser else '#ffffff'
            )
        self._widget.plotSignalGraph(areas, signals, colors)

    def emitScanSignal(self, signal, *args):
        signal.emit(*args)

    def attrChanged(self, key, value):
        if self.settingAttr or len(key) != 2:
            return
        if key[0] == _attrCategoryStage:
            self._analogParameterDict[key[1]] = value
            self.setParameters()
        elif key[0] == _attrCategoryTTL:
            self._digitalParameterDict[key[1]] = value
            self.setParameters()

    def setSharedAttr(self, category, attr, value):
        self.settingAttr = True
        try:
            self._commChannel.sharedAttrs[(category, attr)] = value
        finally:
            self.settingAttr = False

    def updateScanStageAttrs(self):
        self.getParameters()
        for key, value in self._analogParameterDict.items():
            self.setSharedAttr(_attrCategoryStage, key, value)
        positiveDirections = []
        for i in range(len(self.positioners)):
            positionerName = self._analogParameterDict['target_device'][i]
            positiveDirection = self._setupInfo.positioners[positionerName].isPositiveDirection
            positiveDirections.append(positiveDirection)
        self.setSharedAttr(_attrCategoryStage, 'positive_direction', positiveDirections)

    def updateScanTTLAttrs(self):
        self.getParameters()
        for key, value in self._digitalParameterDict.items():
            self.setSharedAttr(_attrCategoryTTL, key, value)

    def _logRasterTTLDiagnostics(self):
        """Log what the firmware actually does with the TTL settings, and
        warn when a triggered camera cannot keep up with the dwell time.

        The deployed TriggerSwitch 0.1 firmware contains a "temporary fix"
        in runPixelCycle(): every pixel it pulses TTL lines 0-3 TOGETHER in
        a single window defined by the earliest TTL row's start/end times
        (sent as p1StartUs/p1EndUs). The per-device line selection (p1Line)
        and all other TTL rows are ignored, so which device sits on which of
        TTL0-3 does not affect the pulse pattern — emission control of the
        lasers comes from arming (sigScanBuilt) alone.

        A camera in external frame-trigger mode silently drops triggers that
        arrive while it is still exposing/reading out — the typical symptom
        is roughly (but never exactly) half the expected frames.
        """
        included = self._digitalParameterDict.get('target_device', [])
        starts = self._digitalParameterDict.get('TTL_start', [])
        ends = self._digitalParameterDict.get('TTL_end', [])
        seqTime = self._digitalParameterDict.get('sequence_time')
        if not included or len(starts) != len(included) or not seqTime:
            return
        p1Index = starts.index(min(starts))
        p1End = ends[p1Index] if p1Index < len(ends) else float('nan')
        xDim, yDim = self.getBeadRecScanDims()
        self._logger.info(
            f'Raster scan TTL: firmware pulses TTL lines 0-3 together, '
            f'window from earliest row "{included[p1Index]}" '
            f'(start {starts[p1Index] * 1e3:.3f} ms, end {p1End * 1e3:.3f} ms, '
            f'dwell {seqTime * 1e3:.3f} ms). Individual TTL rows / line '
            f'selection are NOT honored by this firmware revision. '
            f'Expected camera frames: {xDim} x {yDim} = {xDim * yDim}.'
        )
        for detectorName in (d for d in included if d in self._setupInfo.detectors):
            try:
                detector = self._master.detectorsManager[detectorName]
                exposure = float(detector.parameters['Real exposure time'].value)
                readout = float(detector.parameters['Readout time'].value)
            except Exception:
                continue
            frameTime = exposure + readout
            if frameTime > seqTime:
                self._logger.warning(
                    f'Camera "{detectorName}" cannot keep up with the scan: '
                    f'exposure + readout = {frameTime * 1e3:.2f} ms exceeds '
                    f'the dwell time {seqTime * 1e3:.2f} ms. Triggers '
                    f'arriving during exposure/readout are dropped '
                    f'(typically ~half the frames). Increase the sequence '
                    f'time above {frameTime * 1e3:.2f} ms or reduce the '
                    f'exposure/ROI.'
                )

    def _getScanLaserDevices(self):
        """Return the names of the lasers included in this scan.

        These are the TTL-included devices that are also registered as
        lasers in the setup. The LaserController uses this list (via
        sigScanBuilt) to arm exactly these lasers for the scan.
        """
        self.getParameters()
        return [d for d in self._digitalParameterDict['target_device']
                if d in self._setupInfo.lasers]

    def getBeadRecScanDims(self) -> tuple[int, int]:
        """Return scan dimensions as (X_pixels, Y_pixels) for BeadRec."""
        self.getParameters()
        axis_length = self._analogParameterDict.get('axis_length', [0, 0])
        axis_step_size = self._analogParameterDict.get('axis_step_size', [1, 1])
        
        x_dim = 0 if axis_step_size[0] == 0 else round(axis_length[0] / axis_step_size[0])
        y_dim = 0 if (len(axis_step_size) < 2 or axis_step_size[1] == 0) else round(axis_length[1] / axis_step_size[1])
        return (x_dim, y_dim)

    def getBeadRecStepSizes(self) -> tuple[float, float]:
        """Return scan step sizes as (X_step, Y_step) for BeadRec."""
        self.getParameters()
        axis_step_size = self._analogParameterDict.get('axis_step_size', [0.0, 0.0])
        x_step = axis_step_size[0] if len(axis_step_size) > 0 else 0.0
        y_step = axis_step_size[1] if len(axis_step_size) > 1 else 0.0
        return (x_step, y_step)

    def getNumScanPositions(self) -> int:
        """Return the total number of scan positions (pixels) of the raster
        scan. Consumed by the RecordingController in scan-once mode as the
        number of camera frames to record."""
        x_dim, y_dim = self.getBeadRecScanDims()
        return max(int(x_dim), 1) * max(int(y_dim), 1)

    def getNumCamTTL(self) -> dict:
        """Return camera TTL pulses per scan position, per detector.

        The raster firmware fires one p1 pulse per pixel, so every
        TTL-included detector receives at most one trigger per position.
        """
        self.getParameters()
        included = self._digitalParameterDict.get('target_device', [])
        return {d: 1 for d in included if d in self._setupInfo.detectors}

    def runScan(self) -> None:
        """Runs a scan with the set scanning parameters."""
        self.runScanAdvanced(sigScanStartingEmitted=False)


_attrCategoryStage = 'ScanStage'
_attrCategoryTTL = 'ScanTTL'
